"""
duyuru.py
==========
/duyuru komutu ile bota /start yapmış kullanıcılara toplu mesaj gönderir.
Önizleme sonrasında iki hedef seçeneği sunulur:
  • Abonelere Gönder  → yalnızca aktif abonelere
  • Tüm Üyelere Gönder → botla etkileşime girmiş herkese

Kullanım:
  /duyuru           →  İçerik ister
  İçerik gönder     →  Önizleme + Abonelere / Tüm Üyelere / Zamanlı / İptal butonları çıkar
  ⏰ Zamanlı Gönder  →  Tarih/saat ister (ör: 20.04.2026 21:00)
  📨 Abonelere Gönder  →  Anında sadece abonelere gönderilir
  📣 Tüm Üyelere Gönder →  Anında tüm aktif kullanıcılara gönderilir
  ❌ İptal          →  İptal edilir

Özellikler:
  - Sadece owner kullanabilir
  - Metin, tekli/çoklu fotoğraf, video, belge (zip/7z dahil) desteklenir
  - Media group (albüm) mesajları desteklenir
  - ⏰ Zamanlı gönderim: istenen tarih/saatte otomatik tetiklenir, önceden iptal edilebilir
  - 🔁 Retry kuyruğu: ilk turda başarısız olanlara üstel beklemeyle maks 3 kez yeniden deneme
  - 📊 Detaylı rapor: engelleme/başarısız kullanıcılar .txt dosyası olarak owner'a gönderilir
"""

import asyncio
import io
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from pyrogram import filters, Client
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
)
from pyrogram.enums import ParseMode, MessageMediaType
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated, PeerIdInvalid

from Backend import db
from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER

_TZ = ZoneInfo("Europe/Istanbul")

# ── In-memory state ───────────────────────────────────────────────────────────

# Bekleme durumu: {owner_uid: {"step": str, ...}}
_WAITING: dict[int, dict] = {}

# Media group toplama: {media_group_id: [Message, ...]}
_MEDIA_GROUPS: dict[str, list] = {}
_MEDIA_GROUP_TASKS: dict[str, asyncio.Task] = {}

# Zamanlı gönderimler: {job_id: asyncio.Task}
_SCHEDULED_JOBS: dict[str, asyncio.Task] = {}

# Desteklenen medya türleri
SUPPORTED_MEDIA = {
    MessageMediaType.PHOTO,
    MessageMediaType.VIDEO,
    MessageMediaType.DOCUMENT,
}

# Retry ayarları
MAX_RETRY = 3


# ── Klavye yardımcıları ───────────────────────────────────────────────────────

def _confirm_keyboard(uid: int) -> InlineKeyboardMarkup:
    """Önizleme sonrası: Abonelere / Tüm Üyelere / Aboneliği Olmayanlar / Zamanlı / İptal."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📨 Abonelere Gönder",    callback_data=f"duyuru_send_subs:{uid}"),
            InlineKeyboardButton("📣 Herkese Gönder",  callback_data=f"duyuru_send_all:{uid}"),
        ],
        [
            InlineKeyboardButton("🔕 Aboneliği Olmayanlara Gönder", callback_data=f"duyuru_send_nonsub:{uid}"),
        ],
        [
            InlineKeyboardButton("⏰ Zamanlı Gönder (Aboneler)", callback_data=f"duyuru_schedule_subs:{uid}"),
            InlineKeyboardButton("⏰ Zamanlı Gönder (Herkes)",     callback_data=f"duyuru_schedule_all:{uid}"),
        ],
        [
            InlineKeyboardButton("⏰ Zamanlı Gönder (Aboneliği Olmayanlar)", callback_data=f"duyuru_schedule_nonsub:{uid}"),
        ],
        [
            InlineKeyboardButton("❌ İptal", callback_data=f"duyuru_cancel:{uid}"),
        ],
    ])


def _scheduled_cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🗑 Zamanlanmış Görevi İptal Et",
            callback_data=f"duyuru_job_cancel:{job_id}",
        )]
    ])


# ── Medya yardımcıları ────────────────────────────────────────────────────────

def _media_type_label(media_type: Optional[MessageMediaType]) -> str:
    return {
        MessageMediaType.PHOTO:    "📷 Fotoğraf",
        MessageMediaType.VIDEO:    "🎬 Video",
        MessageMediaType.DOCUMENT: "📎 Dosya",
    }.get(media_type, "📄 Medya")


async def _get_active_count() -> int | str:
    try:
        users = await db.get_all_users()
        return len(users)
    except Exception:
        return "?"


# ── /duyuru komutu ────────────────────────────────────────────────────────────

@Client.on_message(filters.command("duyuru") & filters.private & CustomFilters.owner)
async def cmd_duyuru(client: Client, message: Message):
    uid = message.from_user.id

    # /duyuru Metin şeklinde inline kullanım desteği
    inline_text = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            inline_text = parts[1].strip()

    if inline_text:
        shifted_ents = _shift_entities(
            list(message.entities) if message.entities else [],
            message.text,
            inline_text,
        )
        _WAITING[uid] = {
            "step": "awaiting_confirm",
            "content_type": "text",
            "text": inline_text,
            "entities": shifted_ents,
        }
        await _show_preview(client, message, _WAITING[uid])
        return

    await message.reply_text(
        "📣 **Duyuru**\n\n"
        "Göndermek istediğiniz içeriği gönderin:\n"
        "• Metin\n"
        "• Fotoğraf / Fotoğraf albümü\n"
        "• Video / Video albümü\n"
        "• Dosya (zip, 7z, vb.)\n\n"
        "_(İptal için /iptal yazın.)_",
        parse_mode=ParseMode.MARKDOWN,
        quote=True,
    )
    _WAITING[uid] = {"step": "awaiting_content"}


# ── /iptal komutu ─────────────────────────────────────────────────────────────

@Client.on_message(
    filters.private & CustomFilters.owner & filters.command("iptal"),
    group=2,
)
async def cmd_iptal(client: Client, message: Message):
    uid = message.from_user.id
    if uid in _WAITING:
        _WAITING.pop(uid, None)
        await message.reply_text("❌ Duyuru iptal edildi.", quote=True)


# ── Önizleme ──────────────────────────────────────────────────────────────────

async def _show_preview(client: Client, message: Message, state: dict):
    uid = message.from_user.id
    count = await _get_active_count()
    content_type = state.get("content_type")

    if content_type == "text":
        text = state.get("text", "")
        await message.reply_text(
            f"📋 **Önizleme**\n\n"
            f"─────────────────────\n"
            f"{text}\n"
            f"─────────────────────\n\n"
            f"👥 Toplam kullanıcı: **{count}**\n\n"
            f"Kime göndermek istersiniz?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_confirm_keyboard(uid),
            quote=True,
        )
    elif content_type == "media_group":
        msgs = state.get("media_msgs", [])
        types_str = ", ".join(_media_type_label(m.media) for m in msgs)
        await message.reply_text(
            f"📋 **Önizleme — Albüm ({len(msgs)} öğe)**\n"
            f"Tür: {types_str}\n\n"
            f"👥 Toplam kullanıcı: **{count}**\n\n"
            f"Kime göndermek istersiniz?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_confirm_keyboard(uid),
            quote=True,
        )
    else:
        media_msg: Message = state.get("media_msg")
        label = _media_type_label(media_msg.media if media_msg else None)
        cap = f"\n📝 Açıklama: _{media_msg.caption}_" if (media_msg and media_msg.caption) else ""
        await message.reply_text(
            f"📋 **Önizleme — {label}**{cap}\n\n"
            f"👥 Toplam kullanıcı: **{count}**\n\n"
            f"Kime göndermek istersiniz?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_confirm_keyboard(uid),
            quote=True,
        )


# ── İçerik alma handler'ı ─────────────────────────────────────────────────────

@Client.on_message(
    filters.private & CustomFilters.owner & ~filters.command(""),
    group=2,
)
async def duyuru_content_input(client: Client, message: Message):
    uid = message.from_user.id
    state = _WAITING.get(uid)

    # Zamanlı gönderim için tarih/saat bekleniyor
    if state and state.get("step") == "awaiting_schedule_time":
        await _handle_schedule_time_input(client, message, state)
        return

    if not state or state.get("step") != "awaiting_content":
        return

    if message.text and message.text.strip().lower() in ("/iptal", "iptal"):
        _WAITING.pop(uid, None)
        await message.reply_text("❌ Duyuru iptal edildi.", quote=True)
        return

    # ── Media group (albüm) ───────────────────────────────────────────────
    if message.media_group_id:
        group_id = message.media_group_id
        _MEDIA_GROUPS.setdefault(group_id, []).append(message)

        if group_id in _MEDIA_GROUP_TASKS:
            _MEDIA_GROUP_TASKS[group_id].cancel()

        async def _finalize_group(gid: str, owner_uid: int, last_msg: Message):
            await asyncio.sleep(1.2)
            msgs = _MEDIA_GROUPS.pop(gid, [])
            _MEDIA_GROUP_TASKS.pop(gid, None)
            if not msgs:
                return
            msgs.sort(key=lambda m: m.id)
            _WAITING[owner_uid] = {
                "step": "awaiting_confirm",
                "content_type": "media_group",
                "media_msgs": msgs,
            }
            await _show_preview(client, last_msg, _WAITING[owner_uid])

        _MEDIA_GROUP_TASKS[group_id] = asyncio.create_task(
            _finalize_group(group_id, uid, message)
        )
        return

    # ── Tekli metin ───────────────────────────────────────────────────────
    if message.text:
        # /duyuru komutuyla birlikte yazılmışsa prefix'i temizle
        original_text = message.text
        raw_text = original_text
        if raw_text.startswith("/duyuru"):
            parts = raw_text.split(maxsplit=1)
            raw_text = parts[1].strip() if len(parts) > 1 else ""
        if not raw_text:
            return
        # Entity offset'lerini temizlenmiş metne göre kaydır
        shifted_ents = _shift_entities(
            list(message.entities) if message.entities else [],
            original_text,
            raw_text,
        )
        _WAITING[uid] = {
            "step": "awaiting_confirm",
            "content_type": "text",
            "text": raw_text,
            "entities": shifted_ents,
        }
        await _show_preview(client, message, _WAITING[uid])
        return

    # ── Tekli medya ───────────────────────────────────────────────────────
    if message.media in SUPPORTED_MEDIA:
        _WAITING[uid] = {
            "step": "awaiting_confirm",
            "content_type": "single_media",
            "media_msg": message,
        }
        await _show_preview(client, message, _WAITING[uid])
        return

    await message.reply_text(
        "⚠️ Desteklenmeyen içerik türü.\n"
        "Lütfen **metin**, **fotoğraf**, **video** veya **dosya** gönderin.",
        parse_mode=ParseMode.MARKDOWN,
        quote=True,
    )


# ── Zamanlı gönderim: tarih/saat girişi ──────────────────────────────────────

async def _handle_schedule_time_input(client: Client, message: Message, state: dict):
    uid = message.from_user.id
    raw = (message.text or "").strip()

    if raw.lower() in ("/iptal", "iptal"):
        _WAITING.pop(uid, None)
        await message.reply_text("❌ Zamanlı gönderim iptal edildi.", quote=True)
        return

    dt_local = None
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            dt_local = datetime.strptime(raw, fmt).replace(tzinfo=_TZ)
            break
        except ValueError:
            continue

    if dt_local is None:
        await message.reply_text(
            "⚠️ Geçersiz format. Lütfen şu şekilde girin:\n"
            "`GG.AA.YYYY SS:DD`\n"
            "Örnek: `20.04.2026 21:00`",
            parse_mode=ParseMode.MARKDOWN,
            quote=True,
        )
        return

    delay = (dt_local - datetime.now(_TZ)).total_seconds()
    if delay <= 0:
        await message.reply_text(
            "⚠️ Girdiğiniz tarih/saat geçmişte. Lütfen gelecekte bir zaman girin.",
            quote=True,
        )
        return

    job_id = f"{uid}_{int(dt_local.timestamp())}"
    content_state = {k: v for k, v in state.items() if k != "step"}

    _SCHEDULED_JOBS[job_id] = asyncio.create_task(
        _scheduled_send_job(client, uid, job_id, content_state, delay)
    )
    _WAITING.pop(uid, None)

    dt_str = dt_local.strftime("%d.%m.%Y %H:%M")
    await message.reply_text(
        f"✅ **Duyuru Zamanlandı**\n\n"
        f"🕐 Gönderim zamanı: `{dt_str}` (TR saati)\n"
        f"🆔 Görev ID: `{job_id}`\n\n"
        f"İptal etmek için aşağıdaki butonu kullanın.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_scheduled_cancel_keyboard(job_id),
        quote=True,
    )


# ── Zamanlı gönderim job'u ────────────────────────────────────────────────────

async def _scheduled_send_job(
    client: Client,
    owner_uid: int,
    job_id: str,
    state: dict,
    delay: float,
):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        LOGGER.info("Duyuru job iptal edildi: %s", job_id)
        _SCHEDULED_JOBS.pop(job_id, None)
        return

    _SCHEDULED_JOBS.pop(job_id, None)

    try:
        await client.send_message(
            chat_id=owner_uid,
            text=f"⏰ **Zamanlanmış duyuru başlıyor…**\n🆔 `{job_id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass

    await _run_broadcast(client, owner_uid, state, target=state.get("target", "all"))


# ── Komut prefix temizleyici ─────────────────────────────────────────────────

def _strip_command_prefix(text: str) -> str:
    """Metinden /duyuru veya /duyuru@botname gibi komut prefix'ini temizler."""
    if not text:
        return text
    if text.startswith("/duyuru"):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
    return text


def _shift_entities(entities, original_text: str, stripped_text: str):
    """
    Prefix çıkarıldıktan sonra entity offset'lerini yeni metne göre kaydırır.
    Yeni metnin dışına taşan veya sıfırdan küçük olan entity'leri atar.
    """
    if not entities or not original_text or not stripped_text:
        return None

    # Orijinal metinde stripped_text'in başladığı konum = kaydırma miktarı
    try:
        shift = original_text.index(stripped_text)
    except ValueError:
        # Stripped text orijinal içinde bulunamazsa entity'leri güvenli at
        return None

    adjusted = []
    stripped_len = len(stripped_text)
    for ent in entities:
        new_offset = ent.offset - shift
        # Entity tamamen prefix içindeyse veya sınır dışındaysa atla
        if new_offset + ent.length <= 0 or new_offset >= stripped_len:
            continue
        # Kısmen taşanları kırp
        if new_offset < 0:
            ent.length += new_offset  # length küçülür
            new_offset = 0
        if new_offset + ent.length > stripped_len:
            ent.length = stripped_len - new_offset
        if ent.length <= 0:
            continue
        ent.offset = new_offset
        adjusted.append(ent)

    return adjusted or None


# ── Tek kullanıcıya gönderim ──────────────────────────────────────────────────

async def _send_to_user(client: Client, uid_target: int, state: dict):
    content_type = state.get("content_type")

    if content_type == "text":
        await client.send_message(
            chat_id=uid_target,
            text=state["text"],
            entities=state.get("entities"),  # zaten kaydırılmış, bkz. duyuru_content_input
            disable_web_page_preview=True,
        )

    elif content_type == "single_media":
        msg: Message = state["media_msg"]
        # Caption'dan /duyuru komut prefix'ini temizle ve entity offset'lerini kaydır
        original_caption = msg.caption or ""
        clean_caption = _strip_command_prefix(original_caption) if original_caption else None
        shifted_entities = (
            _shift_entities(list(msg.caption_entities), original_caption, clean_caption)
            if (clean_caption and msg.caption_entities)
            else None
        )
        kwargs = {
            "chat_id": uid_target,
            "caption": clean_caption or None,
            "caption_entities": shifted_entities,
        }
        if msg.media == MessageMediaType.PHOTO:
            await client.send_photo(photo=msg.photo.file_id, **kwargs)
        elif msg.media == MessageMediaType.VIDEO:
            await client.send_video(video=msg.video.file_id, **kwargs)
        elif msg.media == MessageMediaType.DOCUMENT:
            await client.send_document(document=msg.document.file_id, **kwargs)

    elif content_type == "media_group":
        msgs: list[Message] = state["media_msgs"]
        media_list = []
        for i, m in enumerate(msgs):
            # İlk öğenin caption'ından /duyuru prefix'ini temizle ve entity'leri kaydır
            raw_cap  = m.caption if i == 0 else None
            cap      = _strip_command_prefix(raw_cap) if raw_cap else None
            cap_ents = (
                _shift_entities(list(m.caption_entities), raw_cap, cap)
                if (i == 0 and cap and m.caption_entities)
                else None
            )
            if m.media == MessageMediaType.PHOTO:
                media_list.append(InputMediaPhoto(
                    media=m.photo.file_id, caption=cap, caption_entities=cap_ents))
            elif m.media == MessageMediaType.VIDEO:
                media_list.append(InputMediaVideo(
                    media=m.video.file_id, caption=cap, caption_entities=cap_ents))
            elif m.media == MessageMediaType.DOCUMENT:
                media_list.append(InputMediaDocument(
                    media=m.document.file_id, caption=cap, caption_entities=cap_ents))
        if media_list:
            await client.send_media_group(chat_id=uid_target, media=media_list)


# ── Kitlesel gönderim çekirdeği ───────────────────────────────────────────────

async def _run_broadcast(
    client: Client,
    owner_uid: int,
    state: dict,
    status_msg: Message = None,
    target: str = "all",   # "all" = tüm üyeler | "subs" = sadece aboneler
):
    """
    1. Tüm aktif abonelere gönderim (1. tur).
    2. Başarısız olanlar retry kuyruğuna alınır (üstel bekleme, maks MAX_RETRY deneme).
    3. Özet + detaylı rapor (.txt) owner'a gönderilir.
    """

    # ── Kullanıcı listesi ─────────────────────────────────────────────────
    try:
        if target == "subs":
            all_users = await db.get_active_subscribers()
            target_label = "aktif abone"
        elif target == "nonsub":
            all_users = await db.get_non_active_users()
            target_label = "aboneliği olmayanlar"
        else:
            all_users = await db.get_all_users()
            target_label = "tüm üye"
    except Exception as e:
        LOGGER.error("Duyuru: kullanıcı listesi alınamadı: %s", e)
        err = "❌ Kullanıcı listesi alınamadı."
        if status_msg:
            await status_msg.edit_text(err)
        else:
            await client.send_message(chat_id=owner_uid, text=err)
        return

    total = len(all_users)

    success_ids:   list[int]  = []
    success_users: list[dict] = []   # {id, name, username}
    blocked_users: list[dict] = []   # {id, name, username, reason}
    retry_queue:   list[dict] = []   # ilk turda başarısız → retry
    failed_final:  list[dict] = []   # retry sonrası da başarısız

    # ── 1. Tur ────────────────────────────────────────────────────────────
    if status_msg:
        await status_msg.edit_text(
            f"⏳ Duyuru gönderiliyor ({target_label})… (0 / {total})",
            parse_mode=ParseMode.MARKDOWN,
        )

    last_edit = 0

    for idx, user in enumerate(all_users):
        uid_target = user.get("_id") or user.get("user_id")
        if not uid_target:
            failed_final.append({"id": 0, "name": "Bilinmeyen", "reason": "ID yok"})
            continue

        uid_int  = int(uid_target)
        name     = user.get("first_name") or str(uid_int)
        username = user.get("username") or None

        try:
            await _send_to_user(client, uid_int, state)
            success_ids.append(uid_int)
            success_users.append({"id": uid_int, "name": name, "username": username})

        except FloodWait as e:
            wait = max(e.value, 1)
            LOGGER.warning("Duyuru FloodWait: %d sn", wait)
            await asyncio.sleep(wait)
            try:
                await _send_to_user(client, uid_int, state)
                success_ids.append(uid_int)
                success_users.append({"id": uid_int, "name": name, "username": username})
            except Exception as ex:
                retry_queue.append({"id": uid_int, "name": name, "username": username, "reason": str(ex)})

        except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid) as e:
            blocked_users.append({"id": uid_int, "name": name, "username": username, "reason": type(e).__name__})

        except Exception as e:
            retry_queue.append({"id": uid_int, "name": name, "username": username, "reason": str(e)})

        await asyncio.sleep(0.05)

        # Her 25 kullanıcıda bir ilerleme güncelle
        if status_msg and (idx + 1 - last_edit) >= 25:
            last_edit = idx + 1
            try:
                await status_msg.edit_text(
                    f"⏳ Gönderiliyor… ({idx + 1} / {total})\n"
                    f"✅ {len(success_ids)}  |  🔁 {len(retry_queue)}  |  🚫 {len(blocked_users)}",
                )
            except Exception:
                pass

    # ── 2. Tur: Retry kuyruğu ─────────────────────────────────────────────
    if retry_queue and status_msg:
        try:
            await status_msg.edit_text(
                f"🔁 Retry kuyruğu: {len(retry_queue)} kullanıcı yeniden deneniyor…"
            )
        except Exception:
            pass

    for entry in retry_queue:
        uid_int  = entry["id"]
        name     = entry["name"]
        username = entry.get("username")
        sent     = False

        for attempt in range(1, MAX_RETRY + 1):
            await asyncio.sleep(2 ** attempt)   # 2 → 4 → 8 sn
            try:
                await _send_to_user(client, uid_int, state)
                success_ids.append(uid_int)
                success_users.append({"id": uid_int, "name": name, "username": username})
                LOGGER.info("Retry başarılı (%s) — deneme %d", uid_int, attempt)
                sent = True
                break
            except FloodWait as e:
                await asyncio.sleep(max(e.value, 1))
            except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid) as e:
                blocked_users.append({"id": uid_int, "name": name, "username": username, "reason": type(e).__name__})
                sent = True   # artık retry'a gerek yok
                break
            except Exception as e:
                LOGGER.warning("Retry başarısız (%s) %d/%d: %s", uid_int, attempt, MAX_RETRY, e)

        if not sent:
            failed_final.append({"id": uid_int, "name": name, "username": username, "reason": entry["reason"]})

    # ── 3. Özet rapor ─────────────────────────────────────────────────────
    success_count = len(success_ids)
    blocked_count = len(blocked_users)
    failed_count  = len(failed_final)
    rate_str      = f"{success_count / total * 100:.1f}%" if total else "—"

    summary = (
        f"✅ **Duyuru Tamamlandı** ({'Aboneler' if target == 'subs' else 'Tüm Üyeler'})\n\n"
        f"👥 Hedef kullanıcı:  `{total}`\n"
        f"✉️ Gönderildi:    `{success_count}`\n"
        f"🚫 Engelledi:     `{blocked_count}`\n"
        f"❌ Başarısız:     `{failed_count}`\n"
        f"📊 Başarı oranı:  `{rate_str}`"
    )

    if status_msg:
        await status_msg.edit_text(summary, parse_mode=ParseMode.MARKDOWN)
    else:
        await client.send_message(
            chat_id=owner_uid, text=summary, parse_mode=ParseMode.MARKDOWN
        )

    LOGGER.info(
        "Duyuru tamamlandı — toplam: %d, başarılı: %d, engelledi: %d, başarısız: %d",
        total, success_count, blocked_count, failed_count,
    )

    # ── 4. Detaylı rapor (.txt) ───────────────────────────────────────────
    now_str = datetime.now(_TZ).strftime("%d.%m.%Y %H:%M")
    lines   = [
        f"Duyuru Detaylı Rapor — {now_str}\n",
        f"Hedef: {target_label} | Toplam: {total} | Gönderildi: {success_count} | "
        f"Engelledi: {blocked_count} | Başarısız: {failed_count}\n",
        "=" * 60 + "\n",
    ]

    def _user_line(u: dict, extra: str = "") -> str:
        uname = f" | @{u['username']}" if u.get("username") else ""
        suffix = f" | {extra}" if extra else ""
        return f"  ID: {u['id']:<12} | {u['name']:<20}{uname}{suffix}\n"

    # Gönderilen kullanıcılar
    lines.append(f"\n✉️ GÖNDERİLDİ ({success_count} kullanıcı)\n")
    lines.append("-" * 40 + "\n")
    for u in success_users:
        lines.append(_user_line(u))

    if blocked_users:
        lines.append(f"\n🚫 ENGELLEDİ / HESAP KAPALI ({blocked_count} kullanıcı)\n")
        lines.append("-" * 40 + "\n")
        for u in blocked_users:
            lines.append(_user_line(u, extra=f"Sebep: {u['reason']}"))

    if failed_final:
        lines.append(
            f"\n❌ BAŞARISIZ — TÜM DENEMELER (retry x{MAX_RETRY}) ({failed_count} kullanıcı)\n"
        )
        lines.append("-" * 40 + "\n")
        for u in failed_final:
            lines.append(_user_line(u, extra=f"Sebep: {u['reason']}"))

    report_buf = io.BytesIO("".join(lines).encode("utf-8"))
    report_buf.name = f"duyuru_rapor_{datetime.now(_TZ).strftime('%Y%m%d_%H%M')}.txt"

    try:
        await client.send_document(
            chat_id=owner_uid,
            document=report_buf,
            caption=(
                f"📊 **Detaylı Rapor**\n"
                f"✉️ Gönderildi: `{success_count}` | 🚫 Engelledi: `{blocked_count}` | ❌ Başarısız: `{failed_count}`"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        LOGGER.error("Detaylı rapor gönderilemedi: %s", e)


# ── Callback: Abonelere Gönder ────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^duyuru_send_subs:(\d+)$"))
async def cb_duyuru_send_subs(client: Client, callback: CallbackQuery):
    await _cb_send(client, callback, target="subs")


# ── Callback: Tüm Üyelere Gönder ─────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^duyuru_send_all:(\d+)$"))
async def cb_duyuru_send_all(client: Client, callback: CallbackQuery):
    await _cb_send(client, callback, target="all")


# ── Callback: Aboneliği Olmayanlara Gönder ───────────────────────────────────

@Client.on_callback_query(filters.regex(r"^duyuru_send_nonsub:(\d+)$"))
async def cb_duyuru_send_nonsub(client: Client, callback: CallbackQuery):
    await _cb_send(client, callback, target="nonsub")


async def _cb_send(client: Client, callback: CallbackQuery, target: str):
    owner_uid = int(callback.matches[0].group(1))

    if callback.from_user.id != owner_uid:
        await callback.answer("Bu işlem size ait değil.", show_alert=True)
        return

    state = _WAITING.pop(owner_uid, None)
    if not state or state.get("step") != "awaiting_confirm":
        await callback.answer("Geçersiz işlem.", show_alert=True)
        return

    label = "Abonelere" if target == "subs" else ("Aboneliği olmayanlara" if target == "nonsub" else "Tüm üyelere")
    await callback.answer(f"{label} gönderim başladı ✅")
    await callback.message.edit_text(
        f"⏳ Duyuru {label.lower()} gönderiliyor, lütfen bekleyin…",
        parse_mode=ParseMode.MARKDOWN,
    )

    await _run_broadcast(client, owner_uid, state, status_msg=callback.message, target=target)


# ── Callback: Zamanlı Gönder (Aboneler) ──────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^duyuru_schedule_subs:(\d+)$"))
async def cb_duyuru_schedule_subs(client: Client, callback: CallbackQuery):
    await _cb_schedule(client, callback, target="subs")


# ── Callback: Zamanlı Gönder (Tümü) ──────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^duyuru_schedule_all:(\d+)$"))
async def cb_duyuru_schedule_all(client: Client, callback: CallbackQuery):
    await _cb_schedule(client, callback, target="all")


# ── Callback: Zamanlı Gönder (Aboneliği Olmayanlar) ──────────────────────────

@Client.on_callback_query(filters.regex(r"^duyuru_schedule_nonsub:(\d+)$"))
async def cb_duyuru_schedule_nonsub(client: Client, callback: CallbackQuery):
    await _cb_schedule(client, callback, target="nonsub")


async def _cb_schedule(client: Client, callback: CallbackQuery, target: str):
    owner_uid = int(callback.matches[0].group(1))

    if callback.from_user.id != owner_uid:
        await callback.answer("Bu işlem size ait değil.", show_alert=True)
        return

    state = _WAITING.get(owner_uid)
    if not state or state.get("step") != "awaiting_confirm":
        await callback.answer("Geçersiz işlem.", show_alert=True)
        return

    state["step"]   = "awaiting_schedule_time"
    state["target"] = target
    _WAITING[owner_uid] = state

    label = "Abonelere" if target == "subs" else ("Aboneliği olmayanlara" if target == "nonsub" else "Tüm üyelere")
    await callback.answer()
    await callback.message.edit_text(
        f"⏰ **Zamanlı Gönderim** ({label})\n\n"
        "Gönderim tarih ve saatini girin (Türkiye saati):\n\n"
        "Format: `GG.AA.YYYY SS:DD`\n"
        "Örnek:  `20.04.2026 21:00`\n\n"
        "_(İptal için /iptal yazın.)_",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Callback: İptal ──────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^duyuru_cancel:(\d+)$"))
async def cb_duyuru_cancel(client: Client, callback: CallbackQuery):
    owner_uid = int(callback.matches[0].group(1))
    if callback.from_user.id != owner_uid:
        await callback.answer("Bu işlem size ait değil.", show_alert=True)
        return

    _WAITING.pop(owner_uid, None)
    await callback.answer("İptal edildi.")
    await callback.message.edit_text("❌ Duyuru iptal edildi.")


# ── Callback: Zamanlanmış görevi iptal et ────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^duyuru_job_cancel:(.+)$"))
async def cb_duyuru_job_cancel(client: Client, callback: CallbackQuery):
    job_id = callback.matches[0].group(1)

    try:
        job_owner_uid = int(job_id.split("_")[0])
    except (ValueError, IndexError):
        await callback.answer("Geçersiz görev.", show_alert=True)
        return

    if callback.from_user.id != job_owner_uid:
        await callback.answer("Bu işlem size ait değil.", show_alert=True)
        return

    task = _SCHEDULED_JOBS.pop(job_id, None)
    if task and not task.done():
        task.cancel()
        await callback.answer("Zamanlanmış görev iptal edildi.")
        await callback.message.edit_text(
            f"🗑 **Zamanlanmış Duyuru İptal Edildi**\n\n"
            f"🆔 Görev ID: `{job_id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await callback.answer(
            "Görev bulunamadı veya zaten tamamlandı.", show_alert=True
        )
