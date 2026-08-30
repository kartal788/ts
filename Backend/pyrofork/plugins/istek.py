"""
/istek komutu — aboneler IMDB veya TMDB linki göndererek içerik talebinde bulunur.
Yönetici (APPROVER_IDS / OWNER_ID) onaylar veya reddeder.
"""
import re
from datetime import datetime

import httpx
from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from Backend.config import Telegram
from Backend import db
from Backend.helper.imdb import get_detail as _imdb_get_detail


async def _fetch_tmdb_title_poster(tmdb_id: int, media_type: str) -> tuple[str, str]:
    """
    Verilen tmdb_id + media_type (movie|tv) için TMDB API'den başlık ve poster
    URL'ini çeker. hatirlatmalar.html'deki /api/uye/tmdb-meta endpoint'iyle aynı
    mantığı kullanır; böylece bot üzerinden gelen /istek taleplerinde de
    web'den gönderilen taleplerdeki gibi poster ve isim gösterilebilir.
    """
    api_key = Telegram.TMDB_API
    if not api_key or not tmdb_id:
        return "", ""
    tv_or_movie = "tv" if media_type == "tv" else "movie"
    try:
        url = f"https://api.themoviedb.org/3/{tv_or_movie}/{tmdb_id}?api_key={api_key}&language=tr-TR"
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
        if not r.is_success:
            return "", ""
        meta = r.json()
        title = meta.get("title") or meta.get("name") or ""
        poster_path = meta.get("poster_path") or ""
        poster = f"https://image.tmdb.org/t/p/w300{poster_path}" if poster_path else ""
        return title, poster
    except Exception as _e:
        print(f"[istek] TMDB başlık/poster çekilemedi: {_e}")
        return "", ""

# IMDB & TMDB link desenleri
_IMDB_RE  = re.compile(r"imdb\.com/title/(tt\d+)", re.IGNORECASE)
_TMDB_MOV = re.compile(r"themoviedb\.org/movie/(\d+)", re.IGNORECASE)
_TMDB_TV  = re.compile(r"themoviedb\.org/tv/(\d+)", re.IGNORECASE)


def _parse_link(text: str):
    """
    Verilen metinden IMDB/TMDB linkini ayrıştırır.
    Dönüş: (link, media_type, tmdb_id_or_imdb_id, display_id)
           media_type: "movie" | "tv" | "unknown"
    """
    m = _IMDB_RE.search(text)
    if m:
        imdb_id = m.group(1)
        link = f"https://www.imdb.com/title/{imdb_id}/"
        return link, "unknown", 0, imdb_id, imdb_id

    m = _TMDB_MOV.search(text)
    if m:
        tid = int(m.group(1))
        link = f"https://www.themoviedb.org/movie/{tid}"
        return link, "movie", tid, str(tid), None

    m = _TMDB_TV.search(text)
    if m:
        tid = int(m.group(1))
        link = f"https://www.themoviedb.org/tv/{tid}"
        return link, "tv", tid, str(tid), None

    return None, None, 0, None, None


@Client.on_message(filters.command("istek") & filters.private)
async def istek_command(client: Client, message: Message):
    """Kullanıcı /istek <link> gönderince içerik talebini işler."""
    user_id    = message.from_user.id
    first_name = message.from_user.first_name or ""
    username   = message.from_user.username or ""

    # Aktif abonelik kontrolü
    user = await db.get_user(user_id)
    if not user or user.get("subscription_status") != "active":
        sub_expiry = user.get("subscription_expiry") if user else None
        if sub_expiry and sub_expiry < datetime.utcnow():
            return await message.reply_text(
                "❌ <b>Aboneliğiniz sona ermiş.</b>\n"
                "İçerik talep edebilmek için aktif aboneliğiniz olması gerekir.",
                parse_mode=enums.ParseMode.HTML
            )
        return await message.reply_text(
            "❌ <b>Aktif aboneliğiniz bulunmuyor.</b>\n"
            "İçerik talep edebilmek için önce abone olmanız gerekir.",
            parse_mode=enums.ParseMode.HTML
        )

    # Kullanım: /istek <link>
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return await message.reply_text(
            "ℹ️ <b>Kullanım:</b> <code>/istek &lt;IMDB veya TMDB linki&gt;</code>\n\n"
            "<b>Örnekler:</b>\n"
            "• <code>/istek https://www.imdb.com/title/tt1234567/</code>\n"
            "• <code>/istek https://www.themoviedb.org/movie/550</code>\n"
            "• <code>/istek https://www.themoviedb.org/tv/1396</code>",
            parse_mode=enums.ParseMode.HTML
        )

    raw_text = parts[1].strip()
    link, media_type, tmdb_id, display_id, raw_imdb_id = _parse_link(raw_text)

    if link is None:
        return await message.reply_text(
            "❌ <b>Geçersiz link.</b>\n"
            "Lütfen geçerli bir IMDB veya TMDB linki gönderin.\n\n"
            "<b>Örnek:</b>\n"
            "• <code>https://www.imdb.com/title/tt1234567/</code>\n"
            "• <code>https://www.themoviedb.org/movie/550</code>",
            parse_mode=enums.ParseMode.HTML
        )

    # Aylık istek limiti kontrolü
    request_limit = await db.get_user_request_limit(user_id)
    if request_limit > 0:
        used = await db.count_user_requests_this_month(user_id)
        if used >= request_limit:
            yukselt_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Ek Paket Al", callback_data="open_yukselt")
            ]])
            return await message.reply_text(
                f"⛔ <b>Aylık istek limitinize ulaştınız.</b>\n"
                f"Bu ay için <b>{request_limit}</b> istek hakkınız var ve hepsini kullandınız.\n\n"
                f"Limitinizi artırmak için ek paket satın alabilirsiniz.\n"
                f"Ya da <code>/yukselt</code> komutunu kullanabilirsiniz.\n\n"
                f"Limitiniz bir sonraki ay başında sıfırlanır.",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=yukselt_keyboard,
            )
        remaining = request_limit - used - 1
    else:
        remaining = None  # sınırsız

    # Hatırlatma kur ve doğru Tür etiketini gösterebilmek için: TMDB veya IMDB
    # link olsun, DB'ye kaydetmeden ÖNCE tmdb_id / gerçek media_type'ı çözmeye çalış.
    reminder_set = False
    resolved_media_type = media_type  # IMDB linkinde "unknown" gelir, aşağıda güncellenir
    resolved_title  = ""
    resolved_poster = ""

    # IMDB linki ise Cinemeta üzerinden tmdb_id ve media_type'ı çöz
    if media_type == "unknown" and raw_imdb_id:
        try:
            for _try_type in ("movie", "tv"):
                _detail = await _imdb_get_detail(raw_imdb_id, _try_type)
                if _detail:
                    _resolved = _detail.get("moviedb_id")
                    if _resolved:
                        tmdb_id = int(_resolved)
                        resolved_media_type = "tv" if _detail.get("type") in ("series", "tv") else "movie"
                        resolved_title  = _detail.get("title", "") or ""
                        resolved_poster = _detail.get("poster", "") or ""
                        break
        except Exception as _ie:
            print(f"[istek] IMDB->TMDB çözümleme hatası: {_ie}")

        # Cinemeta bulamazsa TMDB find API'sini dene (metadata.py ile aynı yöntem)
        if not tmdb_id:
            try:
                from Backend.helper.metadata import _resolve_tmdb_id_from_imdb
                for _try_type in ("movie", "tv"):
                    _tid = await _resolve_tmdb_id_from_imdb(raw_imdb_id, _try_type)
                    if _tid:
                        tmdb_id = _tid
                        resolved_media_type = _try_type
                        break
            except Exception as _te:
                print(f"[istek] TMDB find API hatası: {_te}")

    # Başlık/poster henüz çözülemediyse (TMDB linkiyle doğrudan gelindiyse veya
    # Cinemeta üzerinden alınamadıysa) TMDB API'den çek — hatirlatmalar.html'deki
    # "İçerik İstekleri" listesinde poster ve isim gösterebilmek için gerekli.
    if not resolved_title and resolved_media_type in ("movie", "tv") and tmdb_id:
        resolved_title, resolved_poster = await _fetch_tmdb_title_poster(tmdb_id, resolved_media_type)

    # İsteği veritabanına kaydet — artık çözülmüş media_type/tmdb_id/title/poster ile
    # (IMDB linki başarıyla çözüldüyse "unknown" değil "movie"/"tv" olarak kaydedilir)
    await db.update_user_interaction(user_id, first_name, username)
    request_id = await db.add_content_request(
        user_id=user_id,
        link=link,
        media_type=resolved_media_type,
        tmdb_id=tmdb_id,
        title=resolved_title,
        poster=resolved_poster,
        source="bot",
    )

    # Yöneticinin tarayıcısına Web Push bildirimi gönder (Telegram'dan bağımsız)
    try:
        from Backend.helper.webpush import notify_admins as _notify_admins_push
        _push_type_label = {"movie": "🎬 Film", "tv": "📺 Dizi", "unknown": "🎥 İçerik"}.get(
            resolved_media_type, "🎥 İçerik"
        )
        import asyncio as _asyncio
        _asyncio.create_task(_notify_admins_push(
            title="Yeni İçerik Talebi",
            body=f"{_push_type_label} talebi: {first_name or username or user_id}",
            url="/istekler",
            tag="istek-icerik",
        ))
    except Exception as _pe:
        print(f"[istek] Web push bildirimi gönderilemedi: {_pe}")

    if resolved_media_type in ("tv", "movie") and tmdb_id:
        try:
            from Backend.fastapi.routes.notification_routes import (
                _reminders_col,
                _movie_reminders_col,
            )
            col = _reminders_col() if resolved_media_type == "tv" else _movie_reminders_col()
            existing = await col.find_one({"tmdb_id": tmdb_id})
            if existing is None:
                await col.insert_one({
                    "tmdb_id":  tmdb_id,
                    "db_index": 0,
                    "title":    resolved_title,
                    "poster":   resolved_poster,
                    "status":   "",
                    "user_ids": [user_id],
                })
            elif user_id not in (existing.get("user_ids") or []):
                await col.update_one(
                    {"tmdb_id": tmdb_id},
                    {"$addToSet": {"user_ids": user_id},
                     "$set": {
                         "title":  resolved_title or existing.get("title", ""),
                         "poster": resolved_poster or existing.get("poster", ""),
                     }},
                )
            reminder_set = True
        except Exception as _re:
            print(f"[istek] Hatırlatma kurulamadı: {_re}")

    # Kullanıcıya onay mesajı
    limit_info = ""
    if remaining is not None:
        limit_info = f"\n📊 Bu ay kalan istek hakkınız: <b>{remaining}</b>"

    reminder_info = "\n🔔 İçerik eklenince otomatik bildirim alacaksınız." if reminder_set else ""
    title_info = f"\n🎬 <b>Başlık:</b> {resolved_title}" if resolved_title else ""
    await message.reply_text(
        f"✅ <b>İsteğiniz alındı!</b>\n\n"
        f"🔗 <b>Link:</b> {link}{title_info}\n"
        f"📋 <b>Durum:</b> Yönetici incelemesi bekleniyor{limit_info}{reminder_info}",
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True
    )

    # Yöneticiye bildirim gönder
    user_mention   = message.from_user.mention
    username_str   = f"@{username}" if username else "N/A"
    type_label     = {"movie": "🎬 Film", "tv": "📺 Dizi", "unknown": "🎥 Bilinmiyor"}.get(resolved_media_type, "?")

    if request_limit > 0:
        used_now = await db.count_user_requests_this_month(user_id)
        limit_admin_info = f"\n📊 Kullanıcı aylık: <b>{used_now}/{request_limit}</b> istek"
    else:
        limit_admin_info = ""

    title_admin_info = f"\n<b>📌 Başlık:</b> {resolved_title}" if resolved_title else ""

    admin_text = (
        f"<b>🎬 Yeni İçerik Talebi</b>\n\n"
        f"<b>👤 Kullanıcı:</b> {user_mention}\n"
        f"<b>🆔 ID:</b> <code>{user_id}</code>\n"
        f"<b>🔗 Kullanıcı Adı:</b> {username_str}\n"
        f"<b>📂 Tür:</b> {type_label}{title_admin_info}\n"
        f"<b>🔗 Link:</b> {link}{limit_admin_info}\n\n"
        f"Talebi onaylayın veya reddedin."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Onayla", callback_data=f"req_approve_{request_id}_{user_id}"),
            InlineKeyboardButton("❌ Reddet", callback_data=f"req_reject_{request_id}_{user_id}"),
        ]
    ])

    approver_ids = Telegram.APPROVER_IDS if Telegram.APPROVER_IDS else [Telegram.OWNER_ID]
    admin_messages = []
    for approver_id in approver_ids:
        try:
            sent = await client.send_message(
                approver_id,
                admin_text,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )
            admin_messages.append({"chat_id": approver_id, "message_id": sent.id})
        except Exception as e:
            print(f"[istek] Admin bildirimi gönderilemedi ({approver_id}): {e}")

    if admin_messages:
        try:
            await db.set_content_request_admin_messages(request_id, admin_messages)
        except Exception as e:
            print(f"[istek] Admin mesaj id'leri kaydedilemedi: {e}")


@Client.on_callback_query(filters.regex(r"^open_yukselt$"))
async def open_yukselt_callback(client: Client, callback_query: CallbackQuery):
    """Limit bitince 'Ek Paket Al' butonuna basıldığında /yukselt akışını başlatır."""
    await callback_query.answer()

    user_id = callback_query.from_user.id

    # Ban kontrolü
    if await db.is_user_banned(user_id):
        return await callback_query.message.reply_text(
            "🚫 <b>Hesabınız engellenmiştir.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    # Aktif abonelik kontrolü — callback_query.from_user ile yap
    from datetime import datetime as _dt
    user = await db.get_user(user_id)
    now = _dt.utcnow()
    is_active = (
        user
        and user.get("subscription_status") == "active"
        and user.get("subscription_expiry")
        and user["subscription_expiry"] > now
    )
    if not is_active:
        return await callback_query.message.reply_text(
            "❌ <b>Aktif aboneliğiniz bulunamadı.</b>\n\n"
            "Abonelik satın almak için /start yazabilirsiniz.",
            parse_mode=enums.ParseMode.HTML,
        )

    # Ek paketleri çek
    packages = await db.get_addon_packages()
    if not packages:
        return await callback_query.message.reply_text(
            "ℹ️ Şu anda tanımlı bir ek paket bulunmamaktadır. Daha sonra tekrar deneyin."
        )

    keyboard_buttons = []
    for pkg in packages:
        label = pkg.get("label") or f"Ek Paket — {pkg.get('price', 0)} TL"
        price = pkg.get("price", 0)
        keyboard_buttons.append(
            [InlineKeyboardButton(f"{label} — {price} TL", callback_data=f"addon_{pkg['_id']}")]
        )

    keyboard = InlineKeyboardMarkup(keyboard_buttons)
    caption = (
        "<b>🚀 Aboneliğinizi Yükseltin</b>\n\n"
        "Aşağıdaki ek paketlerden birini seçerek günlük limit, aylık limit, "
        "hız limiti, istek limiti veya abonelik sürenizi artırabilirsiniz.\n\n"
        "💡 <i>Ek paket seçin:</i>"
    )

    upgrade_image_id = await db.get_upgrade_image()
    if upgrade_image_id:
        await client.send_photo(
            chat_id=user_id,
            photo=upgrade_image_id,
            caption=caption,
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await client.send_message(
            chat_id=user_id,
            text=caption,
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_callback_query(filters.regex(r"^req_(approve|reject)_([a-fA-F0-9]{24})_(\d+)$"))
async def istek_review(client: Client, callback_query: CallbackQuery):
    """Yönetici isteği onaylar veya reddeder."""
    approver_ids = Telegram.APPROVER_IDS if Telegram.APPROVER_IDS else [Telegram.OWNER_ID]
    if callback_query.from_user.id not in approver_ids:
        return await callback_query.answer("⛔ Bu işlem için yetkiniz yok.", show_alert=True)

    action     = callback_query.matches[0].group(1)   # "approve" | "reject"
    request_id = callback_query.matches[0].group(2)
    user_id    = int(callback_query.matches[0].group(3))

    new_status = "approved" if action == "approve" else "rejected"

    # DB'den talep bilgisini çek (link, tür, başlık vs.)
    req_doc = await db.get_content_request(request_id)
    req_link = req_doc.get("link", "") if req_doc else ""
    req_type = req_doc.get("media_type", "unknown") if req_doc else "unknown"
    req_title = req_doc.get("title", "") if req_doc else ""
    type_label = {"movie": "🎬 Film", "tv": "📺 Dizi", "unknown": "🎥 Bilinmiyor"}.get(req_type, "?")
    title_str = f"\n📌 <b>Başlık:</b> {req_title}" if req_title else ""

    await db.update_content_request_status(request_id, new_status)

    if action == "approve":
        label = "✅ Onaylandı"
        user_msg = (
            f"✅ <b>İçerik Talebiniz Onaylandı!</b>\n\n"
            f"<b>📂 Tür:</b> {type_label}{title_str}\n"
            f"<b>🔗 Link:</b> {req_link}\n\n"
            "Talebiniz yönetici tarafından onaylandı. "
            "İçerik en kısa sürede platforma eklenecektir."
        )
    else:
        label = "❌ Reddedildi"
        user_msg = (
            f"❌ <b>İçerik Talebiniz Reddedildi</b>\n\n"
            f"<b>📂 Tür:</b> {type_label}{title_str}\n"
            f"<b>🔗 Link:</b> {req_link}\n\n"
            "Maalesef talebiniz yönetici tarafından reddedildi."
        )

    await callback_query.answer(f"İstek {label.lower()}.", show_alert=False)

    # Admin mesajını güncelle — içerik bilgileri + durum açıkça göster
    try:
        reviewer = callback_query.from_user.mention
        # Orijinal mesajdan link satırını çekip durum mesajına ekle
        original = callback_query.message.text or ""
        # Link satırını bul
        link_line = ""
        for line in original.splitlines():
            if "🔗 Link:" in line or "Link:" in line:
                link_line = line.strip()
                break
        # Kullanıcı satırını bul
        user_line = ""
        for line in original.splitlines():
            if "👤 Kullanıcı:" in line:
                user_line = line.strip()
                break
        type_line = ""
        for line in original.splitlines():
            if "📂 Tür:" in line:
                type_line = line.strip()
                break

        status_section = (
            f"\n\n{'─' * 30}\n"
            f"<b>{label}</b> — {reviewer}\n"
            f"{user_line}\n"
            f"{type_line}\n"
            f"{link_line}"
        )

        updated_text = f"{original}{status_section}"

        await callback_query.message.edit_text(
            updated_text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=None
        )

        # Aynı talebin diğer yöneticilere gönderilmiş kopyalarını da senkronize et,
        # böylece herhangi bir yönetici onaylasa/reddetse diğerlerinde de
        # onayla/reddet butonları kaybolur ve durum görünür olur.
        acting_msg_id = callback_query.message.id
        for am in (req_doc.get("admin_messages") or []) if req_doc else []:
            if am.get("message_id") == acting_msg_id:
                continue
            try:
                await client.edit_message_text(
                    chat_id=am["chat_id"],
                    message_id=am["message_id"],
                    text=updated_text,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    except Exception:
        pass

    # Kullanıcıya bildir
    try:
        await client.send_message(
            user_id,
            user_msg,
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        print(f"[istek] Kullanıcıya bildirim gönderilemedi ({user_id}): {e}")
