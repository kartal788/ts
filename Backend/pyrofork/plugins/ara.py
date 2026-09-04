"""
/ara komutu — aktif abonelik sahibi üyeler bu komutla veritabanındaki
içerikleri arayabilir.

Kullanım:
  /ara <IMDB veya TMDB linki>   → içerik doğrudan bulunur ve
                                    content_announcer.py'deki duyuru
                                    mesajı formatında (poster + başlık +
                                    puan + tür + açıklama) gönderilir.
  /ara <dizi/film adı>          → veritabanında arama yapılır, eşleşen
                                    içerikler TEK bir mesajda buton listesi
                                    olarak sunulur. Kullanıcı bir sonuca
                                    dokununca o içeriğin bilgisi (yukarıdaki
                                    gibi) ayrı bir mesaj olarak gönderilir.

Diziler için "🎞 Sezonlar" butonuyla sezon/bölüm listesi aynı mesaj
üzerinde (fotoğraf altyazısı + buton düzeni güncellenerek) gezilebilir;
bir bölüme dokunulduğunda o bölümün bilgisi (başlık, açıklama, yayın
tarihi, varsa bölüm görseli) aynı mesaja işlenir.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from typing import Optional, Tuple

import httpx
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from Backend import db
from Backend.config import Telegram
from Backend.helper.content_announcer import (
    _build_caption,
    _build_open_buttons,
    _detect_cam_quality_and_audio,
    _parse_target,
    _tmdb_original_size,
)
from Backend.helper.database import is_media_visible_to_member
from Backend.helper.settings_manager import SettingsManager
from Backend.pyrofork.bot import StreamBot


# ============================================================
# /ara komutunun kullanılabileceği sohbetler
# ============================================================
# İzin verilen yerler:
#   1) Özel mesaj (her zaman)
#   2) Sabit üyelik grubu (Telegram.SUBSCRIPTION_GROUP_ID)
#   3) Ayarlar sayfasındaki "Duyuru Kanalı / Grup Konusu"
#      (settings.announcement_channel) — bu ayar panelden anlık
#      değişebildiğinden burada her mesajda dinamik olarak okunur,
#      import anında sabitlenmez.
async def _ara_chat_filter(_, __, message: Message) -> bool:
    chat = message.chat
    if chat is None:
        return False

    if chat.type == enums.ChatType.PRIVATE:
        return True

    subscription_group_id = getattr(Telegram, "SUBSCRIPTION_GROUP_ID", 0)
    if subscription_group_id and chat.id == subscription_group_id:
        return True

    settings = SettingsManager.current()
    target_chat, _target_thread = _parse_target(getattr(settings, "announcement_channel", ""))
    if target_chat is None:
        return False

    if isinstance(target_chat, int):
        if chat.id != target_chat:
            return False
    else:
        target_username = str(target_chat).lstrip("@").lower()
        if (chat.username or "").lower() != target_username:
            return False

    #----- Duyuru hedefi belirli bir konuya (forum topic) işaret etse bile,
    #----- /ara o gruptaki TÜM konularda (ve konu dışı genel sohbette de)
    #----- çalışır — sadece duyuru mesajları o konuya özel kalır.
    return True


_ARA_ALLOWED_CHATS = filters.create(_ara_chat_filter)


def _membership_redirect_keyboard() -> Optional[InlineKeyboardMarkup]:
    """Aktif üyeliği olmayan kullanıcıyı üyelik almak için bota yönlendiren buton."""
    bot_username = getattr(StreamBot, "username", None)
    if not bot_username:
        return None
    settings = SettingsManager.current()
    app_name = (getattr(settings, "isim", "") or "").strip() or "Bot"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🤖 {app_name}'e git ve üyelik al", url=f"https://t.me/{bot_username}?start=uyelik")
    ]])

#----- Bir arama sonucu sayfasında gösterilecek maksimum içerik sayısı.
PAGE_SIZE = 8

# imdb.com/title/tt... (m.imdb.com, www.imdb.com dahil — alt alan adı
# aranmaz, sadece "imdb.com/title/ttXXXXXXX" alt dizesi yeterlidir)
_IMDB_RE = re.compile(r"imdb\.com/title/(tt\d+)", re.IGNORECASE)
_TMDB_MOV_RE = re.compile(r"themoviedb\.org/movie/(\d+)", re.IGNORECASE)
_TMDB_TV_RE = re.compile(r"themoviedb\.org/tv/(\d+)", re.IGNORECASE)

_RESULT_HEADER_RE = re.compile(r'🔍 "(.*?)" için')


# ============================================================
# Erişim kontrolü
# ============================================================
async def _check_access(user_id: int) -> Tuple[bool, str]:
    """Aktif abonelik + ban kontrolü. (False, sebep) veya (True, "") döner."""
    if await db.is_user_banned(user_id):
        return False, "🚫 <b>Hesabınız engellenmiştir.</b>"

    user = await db.get_user(user_id)
    if not user or user.get("subscription_status") != "active":
        return False, (
            "❌ <b>Aktif aboneliğiniz bulunmuyor.</b>\n"
            "<code>/ara</code> komutunu kullanabilmek için aktif bir aboneliğiniz olmalı.\n"
            "Üyelik almak için botla özelden iletişime geçebilirsiniz."
        )
    return True, ""


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    """HTML etiketlerini temizler — callback_query.answer(show_alert=True)
    HTML render etmediği için (ör. Telegram istemcisinde <code>/ara</code>
    olduğu gibi görünmesin diye) uyarı metinlerinde kullanılır."""
    return _HTML_TAG_RE.sub("", text)


# ============================================================
# Link ayrıştırma
# ============================================================
def _parse_link(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Metinden IMDB/TMDB linkini ayrıştırır.
    Dönüş: (kind, value)
      kind: "imdb" → value = imdb id (tt...)
            "tmdb_movie" / "tmdb_tv" → value = tmdb id (str)
            None → link değil (isim araması yapılacak)
    """
    m = _IMDB_RE.search(text)
    if m:
        return "imdb", m.group(1)

    m = _TMDB_MOV_RE.search(text)
    if m:
        return "tmdb_movie", m.group(1)

    m = _TMDB_TV_RE.search(text)
    if m:
        return "tmdb_tv", m.group(1)

    return None, None


async def _resolve_imdb_from_tmdb(tmdb_id: str, media_type: str) -> Optional[str]:
    """TMDB id + türünden (movie/tv) imdb_id çözer (TMDB external_ids API)."""
    api_key = Telegram.TMDB_API
    if not api_key:
        return None
    tv_or_movie = "tv" if media_type == "tv" else "movie"
    try:
        url = f"https://api.themoviedb.org/3/{tv_or_movie}/{tmdb_id}/external_ids?api_key={api_key}"
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
        if not r.is_success:
            return None
        return (r.json() or {}).get("imdb_id") or None
    except Exception:
        return None


# ============================================================
# DB dokümanı → duyuru formatı yardımcıları
# ============================================================
def _to_announce_info(doc: dict, media_type: str) -> dict:
    """DB dokümanını content_announcer._build_caption'ın beklediği
    alan adlarına (özellikle rating → rate) uyarlar."""
    info = dict(doc)
    info["media_type"] = media_type
    info["rate"] = doc.get("rating")
    return info


def _poster_candidates(info: dict) -> list:
    is_cam, _ = _detect_cam_quality_and_audio(info)
    if is_cam:
        return [
            _tmdb_original_size(info.get("poster_tr")),
            info.get("poster"),
            info.get("backdrop_tr"),
            info.get("backdrop"),
            info.get("backdrop_de"),
            info.get("poster_de"),
        ]
    return [
        info.get("backdrop_tr"),
        info.get("backdrop"),
        info.get("backdrop_de"),
        _tmdb_original_size(info.get("poster_tr")),
        info.get("poster"),
        info.get("poster_de"),
    ]


def _content_info_keyboard(doc: dict, media_type: str, settings) -> Optional[InlineKeyboardMarkup]:
    info = _to_announce_info(doc, media_type)
    rows = []
    open_buttons = _build_open_buttons(info, settings)
    if open_buttons:
        rows.append(open_buttons)
    if media_type == "tv":
        tmdb_id = doc.get("tmdb_id")
        db_index = doc.get("db_index")
        rows.append([
            InlineKeyboardButton("🎞 Sezonlar", callback_data=f"ara_ssn|{tmdb_id}|{db_index}")
        ])
    return InlineKeyboardMarkup(rows) if rows else None


async def _send_content_info(client: Client, chat_id: int, doc: dict) -> Optional[Message]:
    """content_announcer.py'deki duyuru mesajıyla aynı görünümde (poster +
    başlık + puan + tür + kategori + açıklama) bir mesaj gönderir."""
    settings = SettingsManager.current()
    media_type = doc.get("media_type") or doc.get("type") or "movie"
    info = _to_announce_info(doc, media_type)
    caption = _build_caption(info)
    markup = _content_info_keyboard(doc, media_type, settings)

    for poster in _poster_candidates(info):
        if not poster:
            continue
        try:
            return await client.send_photo(
                chat_id, poster, caption=caption,
                parse_mode=enums.ParseMode.HTML, reply_markup=markup,
            )
        except FloodWait as e:
            await asyncio.sleep(int(getattr(e, "value", 0) or 0) + 1)
            try:
                return await client.send_photo(
                    chat_id, poster, caption=caption,
                    parse_mode=enums.ParseMode.HTML, reply_markup=markup,
                )
            except Exception:
                continue
        except Exception:
            continue

    # Hiçbir görsel gönderilemedi → düz metin.
    return await client.send_message(
        chat_id, caption, parse_mode=enums.ParseMode.HTML,
        reply_markup=markup, disable_web_page_preview=True,
    )


# ============================================================
# İsim araması — tek mesajda sonuç listesi
# ============================================================
def _build_results_keyboard(results: list, page: int, total_count: int) -> InlineKeyboardMarkup:
    rows = []
    for r in results:
        media_type = r.get("media_type") or "movie"
        title = r.get("title_tr") or r.get("title") or "?"
        year = r.get("release_year") or ""
        icon = "📺" if media_type == "tv" else "🎬"
        label = f"{icon} {title}" + (f" ({year})" if year else "")
        if len(label) > 60:
            label = label[:59] + "…"
        cb = f"ara_sel|{media_type}|{r.get('tmdb_id')}|{r.get('db_index')}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️ Önceki", callback_data=f"ara_pg|{page - 1}"))
    if page * PAGE_SIZE < total_count:
        nav.append(InlineKeyboardButton("Sonraki ▶️", callback_data=f"ara_pg|{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


async def _render_search_results(query_text: str, page: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    result = await db.search_documents(query_text, page, PAGE_SIZE)
    results = result.get("results") or []
    total = result.get("total_count") or 0
    safe_query = html.escape(query_text)

    if not results:
        return f'🔍 "{safe_query}" için sonuç bulunamadı.', None

    start = (page - 1) * PAGE_SIZE + 1
    end = min(page * PAGE_SIZE, total)
    text = (
        f'🔍 "{safe_query}" için {total} sonuç bulundu ({start}-{end}):\n'
        "Bilgilerini görmek istediğiniz içeriğe dokunun."
    )
    return text, _build_results_keyboard(results, page, total)


# ============================================================
# /ara komutu
# ============================================================
@Client.on_message(filters.command("ara") & _ARA_ALLOWED_CHATS)
async def ara_command(client: Client, message: Message):
    user_id = message.from_user.id
    ok, reason = await _check_access(user_id)
    if not ok:
        return await message.reply_text(
            reason,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_membership_redirect_keyboard(),
            quote=True,
        )

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return await message.reply_text(
            "ℹ️ <b>Kullanım:</b>\n"
            "<code>/ara &lt;IMDB veya TMDB linki&gt;</code>\n"
            "<code>/ara &lt;dizi veya film adı&gt;</code>\n\n"
            "<b>Örnekler:</b>\n"
            "• <code>/ara https://m.imdb.com/title/tt10986410/</code>\n"
            "• <code>/ara https://www.themoviedb.org/movie/550</code>\n"
            "• <code>/ara Breaking Bad</code>",
            parse_mode=enums.ParseMode.HTML,
            quote=True,
        )

    query_text = parts[1].strip()
    kind, value = _parse_link(query_text)

    if kind is None:
        text, markup = await _render_search_results(query_text, page=1)
        return await message.reply_text(
            text, reply_markup=markup, disable_web_page_preview=True, quote=True,
        )

    # ---- Link ile doğrudan arama ----
    if kind == "imdb":
        imdb_id = value
    else:
        media_type_hint = "movie" if kind == "tmdb_movie" else "tv"
        imdb_id = await _resolve_imdb_from_tmdb(value, media_type_hint)
        if not imdb_id:
            return await message.reply_text(
                "❌ <b>Bu TMDB linkindeki içeriğin IMDB kaydı bulunamadı.</b>",
                parse_mode=enums.ParseMode.HTML, quote=True,
            )

    doc = await db.get_media_details(imdb_id)
    if not doc:
        return await message.reply_text(
            "❌ <b>Bu içerik veritabanında bulunamadı.</b>\n"
            "Yalnızca botta daha önce eklenmiş içerikler için bilgi gönderilebilir.",
            parse_mode=enums.ParseMode.HTML, quote=True,
        )

    if not is_media_visible_to_member(doc, user_id):
        return await message.reply_text(
            "🚫 <b>Bu içeriğe erişim izniniz yok.</b>",
            parse_mode=enums.ParseMode.HTML, quote=True,
        )

    await _send_content_info(client, message.chat.id, doc)


# ============================================================
# Callback: sayfalama (isim araması sonuçları)
# ============================================================
@Client.on_callback_query(filters.regex(r"^ara_pg\|(\d+)$"))
async def ara_page_callback(client: Client, callback_query: CallbackQuery):
    ok, reason = await _check_access(callback_query.from_user.id)
    if not ok:
        return await callback_query.answer(_plain(reason), show_alert=True)

    page = int(callback_query.matches[0].group(1))
    header = callback_query.message.text or ""
    m = _RESULT_HEADER_RE.search(header)
    if not m:
        return await callback_query.answer(
            "Arama bilgisi bulunamadı, lütfen /ara ile tekrar arayın.", show_alert=True
        )

    await callback_query.answer()
    query_text = html.unescape(m.group(1))
    text, markup = await _render_search_results(query_text, page)
    try:
        await callback_query.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except MessageNotModified:
        pass


# ============================================================
# Callback: bir arama sonucuna dokunulunca içerik bilgisini gönder
# ============================================================
@Client.on_callback_query(filters.regex(r"^ara_sel\|(movie|tv)\|(\d+)\|(\d+)$"))
async def ara_select_callback(client: Client, callback_query: CallbackQuery):
    media_type, tmdb_id, db_index = callback_query.matches[0].groups()
    ok, reason = await _check_access(callback_query.from_user.id)
    if not ok:
        return await callback_query.answer(_plain(reason), show_alert=True)

    doc = await db.get_document(media_type, int(tmdb_id), int(db_index))
    if not doc:
        return await callback_query.answer("İçerik artık bulunamadı.", show_alert=True)
    if not is_media_visible_to_member(doc, callback_query.from_user.id):
        return await callback_query.answer("Bu içeriğe erişim izniniz yok.", show_alert=True)

    await callback_query.answer("Bilgiler gönderiliyor…")
    await _send_content_info(client, callback_query.message.chat.id, doc)


# ============================================================
# Callback: dizi → sezon listesi
# ============================================================
@Client.on_callback_query(filters.regex(r"^ara_ssn\|(\d+)\|(\d+)$"))
async def ara_seasons_callback(client: Client, callback_query: CallbackQuery):
    tmdb_id, db_index = callback_query.matches[0].groups()
    ok, reason = await _check_access(callback_query.from_user.id)
    if not ok:
        return await callback_query.answer(_plain(reason), show_alert=True)

    doc = await db.get_document("tv", int(tmdb_id), int(db_index))
    if not doc:
        return await callback_query.answer("İçerik artık bulunamadı.", show_alert=True)
    if not is_media_visible_to_member(doc, callback_query.from_user.id):
        return await callback_query.answer("Bu içeriğe erişim izniniz yok.", show_alert=True)

    seasons = sorted(doc.get("seasons") or [], key=lambda s: s.get("season_number", 0))
    if not seasons:
        return await callback_query.answer("Bu dizide henüz sezon bulunmuyor.", show_alert=True)

    await callback_query.answer()
    rows, row = [], []
    for s in seasons:
        sn = s.get("season_number")
        ep_count = len(s.get("episodes") or [])
        row.append(InlineKeyboardButton(f"Sezon {sn} ({ep_count})", callback_data=f"ara_eps|{tmdb_id}|{db_index}|{sn}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 Bilgiye Dön", callback_data=f"ara_back|{tmdb_id}|{db_index}")])

    try:
        await callback_query.message.edit_reply_markup(InlineKeyboardMarkup(rows))
    except MessageNotModified:
        pass


# ============================================================
# Callback: sezon → bölüm listesi
# ============================================================
@Client.on_callback_query(filters.regex(r"^ara_eps\|(\d+)\|(\d+)\|(\d+)$"))
async def ara_episodes_callback(client: Client, callback_query: CallbackQuery):
    tmdb_id, db_index, season_number = (int(g) for g in callback_query.matches[0].groups())
    ok, reason = await _check_access(callback_query.from_user.id)
    if not ok:
        return await callback_query.answer(_plain(reason), show_alert=True)

    doc = await db.get_document("tv", tmdb_id, db_index)
    if not doc:
        return await callback_query.answer("İçerik artık bulunamadı.", show_alert=True)
    if not is_media_visible_to_member(doc, callback_query.from_user.id):
        return await callback_query.answer("Bu içeriğe erişim izniniz yok.", show_alert=True)

    season = next((s for s in doc.get("seasons") or [] if s.get("season_number") == season_number), None)
    episodes = sorted((season or {}).get("episodes") or [], key=lambda e: e.get("episode_number", 0))
    if not episodes:
        return await callback_query.answer("Bu sezonda henüz bölüm bulunmuyor.", show_alert=True)

    await callback_query.answer()
    rows, row = [], []
    for e in episodes:
        en = e.get("episode_number")
        row.append(InlineKeyboardButton(f"{en}. Bölüm", callback_data=f"ara_epi|{tmdb_id}|{db_index}|{season_number}|{en}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 Sezonlara Dön", callback_data=f"ara_ssn|{tmdb_id}|{db_index}")])

    try:
        await callback_query.message.edit_reply_markup(InlineKeyboardMarkup(rows))
    except MessageNotModified:
        pass


# ============================================================
# Callback: bir bölüme dokununca o bölümün bilgisini göster
# ============================================================
def _format_date(value) -> str:
    """ISO tarih değerini (ör. 2023-11-19T09:00:00.000Z) member_catalog.html'deki
    fmtDate ile aynı, okunabilir DD.MM.YYYY biçimine çevirir. Ayrıştırılamazsa
    değeri olduğu gibi döner."""
    if not value:
        return ""
    text = str(value).strip()
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso_text).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return text


def _build_episode_caption(tv_doc: dict, season_number: int, episode: dict) -> str:
    show_title = tv_doc.get("title_tr") or tv_doc.get("title") or "Bilinmiyor"
    ep_number = episode.get("episode_number")
    ep_title = episode.get("title_tr") or episode.get("title") or f"{ep_number}. Bölüm"

    lines = [
        f"📺 <b>{show_title}</b>",
        f"🎬 <b>Sezon {season_number}, Bölüm {ep_number}</b> — {ep_title}",
    ]

    if episode.get("released"):
        lines.append(f"📅 <b>Yayın Tarihi:</b> {_format_date(episode['released'])}")

    overview = (episode.get("overview_tr") or episode.get("overview") or "").strip()
    if overview:
        if len(overview) > 320:
            overview = overview[:317].rstrip() + "..."
        lines += ["", f"<i>{overview}</i>"]

    return "\n".join(lines)


@Client.on_callback_query(filters.regex(r"^ara_epi\|(\d+)\|(\d+)\|(\d+)\|(\d+)$"))
async def ara_episode_info_callback(client: Client, callback_query: CallbackQuery):
    tmdb_id, db_index, season_number, episode_number = (
        int(g) for g in callback_query.matches[0].groups()
    )
    ok, reason = await _check_access(callback_query.from_user.id)
    if not ok:
        return await callback_query.answer(_plain(reason), show_alert=True)

    doc = await db.get_document("tv", tmdb_id, db_index)
    if not doc:
        return await callback_query.answer("İçerik artık bulunamadı.", show_alert=True)
    if not is_media_visible_to_member(doc, callback_query.from_user.id):
        return await callback_query.answer("Bu içeriğe erişim izniniz yok.", show_alert=True)

    season = next((s for s in doc.get("seasons") or [] if s.get("season_number") == season_number), None)
    episode = next((e for e in (season or {}).get("episodes") or [] if e.get("episode_number") == episode_number), None)
    if not episode:
        return await callback_query.answer("Bölüm bulunamadı.", show_alert=True)

    await callback_query.answer()
    caption = _build_episode_caption(doc, season_number, episode)
    still = (
        episode.get("episode_backdrop")
        or doc.get("backdrop_tr") or doc.get("backdrop")
        or doc.get("poster_tr") or doc.get("poster")
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Bölümlere Dön", callback_data=f"ara_eps|{tmdb_id}|{db_index}|{season_number}")
    ]])

    try:
        if still and callback_query.message.photo:
            await callback_query.message.edit_media(
                InputMediaPhoto(still, caption=caption, parse_mode=enums.ParseMode.HTML),
                reply_markup=markup,
            )
        else:
            await callback_query.message.edit_caption(
                caption, parse_mode=enums.ParseMode.HTML, reply_markup=markup,
            )
    except MessageNotModified:
        pass
    except Exception:
        # Görsel değişimi başarısız oldu (bozuk link vb.) → sadece altyazıyı güncelle.
        try:
            await callback_query.message.edit_caption(
                caption, parse_mode=enums.ParseMode.HTML, reply_markup=markup,
            )
        except MessageNotModified:
            pass


# ============================================================
# Callback: sezon/bölüm gezintisinden ana içerik bilgisine dön
# ============================================================
@Client.on_callback_query(filters.regex(r"^ara_back\|(\d+)\|(\d+)$"))
async def ara_back_callback(client: Client, callback_query: CallbackQuery):
    tmdb_id, db_index = callback_query.matches[0].groups()
    ok, reason = await _check_access(callback_query.from_user.id)
    if not ok:
        return await callback_query.answer(_plain(reason), show_alert=True)

    doc = await db.get_document("tv", int(tmdb_id), int(db_index))
    if not doc:
        return await callback_query.answer("İçerik artık bulunamadı.", show_alert=True)
    if not is_media_visible_to_member(doc, callback_query.from_user.id):
        return await callback_query.answer("Bu içeriğe erişim izniniz yok.", show_alert=True)

    await callback_query.answer()
    settings = SettingsManager.current()
    info = _to_announce_info(doc, "tv")
    caption = _build_caption(info)
    markup = _content_info_keyboard(doc, "tv", settings)
    poster = next((p for p in _poster_candidates(info) if p), None)

    try:
        if poster and callback_query.message.photo:
            await callback_query.message.edit_media(
                InputMediaPhoto(poster, caption=caption, parse_mode=enums.ParseMode.HTML),
                reply_markup=markup,
            )
        else:
            await callback_query.message.edit_caption(
                caption, parse_mode=enums.ParseMode.HTML, reply_markup=markup,
            )
    except MessageNotModified:
        pass
    except Exception:
        try:
            await callback_query.message.edit_caption(
                caption, parse_mode=enums.ParseMode.HTML, reply_markup=markup,
            )
        except MessageNotModified:
            pass
