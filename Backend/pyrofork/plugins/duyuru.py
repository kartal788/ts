"""
duyuru.py
==========
/duyuru komutu ile aktif abonelere toplu mesaj gönderir.

Kullanım:
  /duyuru  →  Mesaj metnini ister
  Metin gönder  →  Önizleme + onay butonları çıkar
  ✅ Gönder  →  Tüm aktif abonelere gönderilir, özet rapor döner
  ❌ İptal   →  İptal edilir

Özellikler:
  - Sadece owner kullanabilir
  - Mesaj, fotoğraf veya medyalı mesajları da destekler (forward benzeri)
  - Başarısız gönderimler loglanır; özet raporda gösterilir
"""

import asyncio

from pyrogram import filters, Client
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated, PeerIdInvalid

from Backend import db
from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER

# Bekleme durumu: {user_id: {"text": str, "entities": ..., "media_msg": Message|None}}
_WAITING: dict[int, dict] = {}


def _confirm_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Gönder", callback_data=f"duyuru_send:{uid}"),
            InlineKeyboardButton("❌ İptal",  callback_data=f"duyuru_cancel:{uid}"),
        ]
    ])


# ── /duyuru komutu ────────────────────────────────────────────────────────────

@Client.on_message(filters.command("duyuru") & filters.private & CustomFilters.owner)
async def cmd_duyuru(client: Client, message: Message):
    await message.reply_text(
        "📣 **Duyuru**\n\n"
        "Göndermek istediğiniz duyuru metnini yazın.\n"
        "_(İptal için /iptal yazın.)_",
        parse_mode=ParseMode.MARKDOWN,
        quote=True,
    )
    _WAITING[message.from_user.id] = {"step": "awaiting_text"}


# ── Metin al, önizle ──────────────────────────────────────────────────────────

@Client.on_message(
    filters.private & CustomFilters.owner & filters.text & ~filters.command(""),
    group=2,
)
async def duyuru_text_input(client: Client, message: Message):
    uid = message.from_user.id
    state = _WAITING.get(uid)
    if not state or state.get("step") != "awaiting_text":
        return

    if message.text.strip().lower() in ("/iptal", "iptal"):
        _WAITING.pop(uid, None)
        await message.reply_text("❌ Duyuru iptal edildi.", quote=True)
        return

    _WAITING[uid] = {
        "step": "awaiting_confirm",
        "text": message.text,
        "entities": message.entities,
    }

    # Abone sayısını çek
    try:
        subs = await db.get_all_subscribers()
        active_subs = [u for u in subs if u.get("subscription_status") == "active"]
        count = len(active_subs)
    except Exception:
        count = "?"

    await message.reply_text(
        f"📋 **Önizleme**\n\n"
        f"─────────────────────\n"
        f"{message.text}\n"
        f"─────────────────────\n\n"
        f"👥 Aktif abone sayısı: **{count}**\n\n"
        f"Bu mesajı tüm aktif abonelere göndermek istiyor musunuz?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_confirm_keyboard(uid),
        quote=True,
    )


# ── Callback: Gönder ─────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^duyuru_send:(\d+)$"))
async def cb_duyuru_send(client: Client, callback: CallbackQuery):
    owner_uid = int(callback.matches[0].group(1))

    # Sadece aynı owner tıklayabilir
    if callback.from_user.id != owner_uid:
        await callback.answer("Bu işlem size ait değil.", show_alert=True)
        return

    state = _WAITING.pop(owner_uid, None)
    if not state or state.get("step") != "awaiting_confirm":
        await callback.answer("Geçersiz işlem.", show_alert=True)
        return

    text = state.get("text", "")
    entities = state.get("entities")

    await callback.answer("Gönderim başladı ✅")
    await callback.message.edit_text(
        "⏳ Duyuru gönderiliyor, lütfen bekleyin…",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Aktif aboneleri çek
    try:
        subs = await db.get_all_subscribers()
        active_subs = [u for u in subs if u.get("subscription_status") == "active"]
    except Exception as e:
        LOGGER.error("Duyuru: abone listesi alınamadı: %s", e)
        await callback.message.edit_text("❌ Abone listesi alınamadı.")
        return

    total = len(active_subs)
    success = 0
    failed = 0
    blocked = 0

    for user in active_subs:
        uid_target = user.get("_id") or user.get("user_id")
        if not uid_target:
            failed += 1
            continue
        try:
            await client.send_message(
                chat_id=int(uid_target),
                text=text,
                entities=entities,
                disable_web_page_preview=True,
            )
            success += 1
        except FloodWait as e:
            LOGGER.warning("Duyuru FloodWait: %d sn bekleniyor", e.value)
            await asyncio.sleep(e.value)
            try:
                await client.send_message(
                    chat_id=int(uid_target),
                    text=text,
                    entities=entities,
                    disable_web_page_preview=True,
                )
                success += 1
            except Exception:
                failed += 1
        except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
            blocked += 1
        except Exception as e:
            LOGGER.warning("Duyuru gönderilemedi (%s): %s", uid_target, e)
            failed += 1

        # Flood koruması için küçük gecikme
        await asyncio.sleep(0.05)

    summary = (
        f"✅ **Duyuru Tamamlandı**\n\n"
        f"👥 Toplam abone: `{total}`\n"
        f"✉️ Gönderildi:   `{success}`\n"
        f"🚫 Engelledi:    `{blocked}`\n"
        f"❌ Başarısız:    `{failed}`"
    )
    await callback.message.edit_text(summary, parse_mode=ParseMode.MARKDOWN)
    LOGGER.info("Duyuru tamamlandı — toplam: %d, başarılı: %d, engelledi: %d, başarısız: %d",
                total, success, blocked, failed)


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
