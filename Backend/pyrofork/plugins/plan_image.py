"""
/plan komutu — Yönetici, abonelik planları ekranında gösterilecek
resmi bu komutla ayarlar veya kaldırır.

/plan2 komutu — Yönetici, /yukselt komutunda gösterilecek
resmi bu komutla ayarlar veya kaldırır.

Kullanım:
  /plan          → Mevcut resmi gösterir ve yönetim butonları sunar
  /plan (resimle birlikte) → Gönderilen resmi direkt kaydeder
  /plan2         → /yukselt ekranı resmini gösterir ve yönetim butonları sunar
  /plan2 (resimle birlikte) → Gönderilen resmi /yukselt için kaydeder
"""

from pyrogram import filters, Client, enums
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from Backend.config import Telegram
from Backend import db
from Backend.helper.custom_filter import CustomFilters

print("DEBUG: plan_image.py PLUGIN LOADED SUCCESSFULLY!")


# ─── Sadece owner kullanabilir (callback handler'lar için) ───────────────────
def _is_owner(message_or_query) -> bool:
    if hasattr(message_or_query, "from_user") and message_or_query.from_user:
        return message_or_query.from_user.id == Telegram.OWNER_ID
    return False


# ─── /plan komutu ─────────────────────────────────────────────────────────────
@Client.on_message(filters.command("plan") & filters.private & CustomFilters.owner)
async def plan_command(client: Client, message: Message):
    # Eğer komutla birlikte bir fotoğraf gönderildiyse direkt kaydet
    if message.photo:
        await _save_photo(message, message.photo.file_id)
        return

    # Eğer /plan bir fotoğraf mesajına reply olarak gönderildiyse onu kaydet
    if message.reply_to_message and message.reply_to_message.photo:
        await _save_photo(message, message.reply_to_message.photo.file_id)
        return

    # Mevcut resmi göster + yönetim butonları
    current = await db.get_plan_image()

    manage_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Resim Gönder / Değiştir", callback_data="plan_img_change")],
        [InlineKeyboardButton("🗑️ Resmi Kaldır", callback_data="plan_img_delete")],
    ])

    if current:
        await message.reply_photo(
            photo=current,
            caption=(
                "✅ <b>Plan resmi ayarlı.</b>\n\n"
                "Değiştirmek için yeni resim gönderin ya da kaldırmak için butona basın."
            ),
            reply_markup=manage_keyboard,
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await message.reply_text(
            "ℹ️ Henüz bir plan resmi ayarlanmamış.\n\n"
            "Ayarlamak için bu mesajı yanıtlayarak (reply) veya /plan komutuyla birlikte fotoğraf gönderin.",
            reply_markup=manage_keyboard,
            quote=True,
        )


# ─── Callback: "Resim Gönder / Değiştir" ──────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^plan_img_change$"))
async def plan_img_change_cb(client: Client, callback_query: CallbackQuery):
    if not _is_owner(callback_query):
        return await callback_query.answer("🚫 Yetki yok.", show_alert=True)

    await callback_query.answer()
    await callback_query.message.reply_text(
        "📸 Lütfen yeni plan resmini gönderin.\n"
        "<i>Bu mesajı yanıtlayarak veya direkt fotoğraf göndererek ayarlayabilirsiniz.</i>",
        parse_mode=enums.ParseMode.HTML,
        quote=True,
    )


# ─── Callback: "Resmi Kaldır" ─────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^plan_img_delete$"))
async def plan_img_delete_cb(client: Client, callback_query: CallbackQuery):
    if not _is_owner(callback_query):
        return await callback_query.answer("🚫 Yetki yok.", show_alert=True)

    ok = await db.delete_plan_image()
    if ok:
        await callback_query.answer("✅ Plan resmi kaldırıldı.", show_alert=True)
        await callback_query.message.edit_caption(
            "🗑️ Plan resmi kaldırıldı. Bundan sonra abonelik mesajı düz metin olarak gönderilecek."
        )
    else:
        await callback_query.answer("❌ Bir hata oluştu.", show_alert=True)


# ─── Gelen fotoğraf mesajları (owner'dan, private chat) ───────────────────────
@Client.on_message(filters.photo & filters.private, group=20)
async def incoming_photo(client: Client, message: Message):
    """
    Owner özel sohbette fotoğraf gönderdiğinde otomatik plan resmi olarak kaydeder.
    Ancak yalnızca bot /plan komutundan sonra bekliyorsa değil, her zaman yakalar —
    kullanıcı deneyimi açısından /plan ile birlikte göndermek daha temiz.
    Bu handler yedek olarak çalışır; /plan komutuyla gelen fotoğraf zaten üstte yakalanır.
    """
    if not _is_owner(message):
        return  # Owner değilse bu handler'ı atla

    # /plan ile birlikte gelen fotoğraf zaten plan_command() tarafından işlendi.
    # Burada kalan, ayrı gönderilen fotoğraflardır.
    # Kullanıcı kafa karışıklığını önlemek için sadece /plan komutuyla gelen
    # fotoğrafı kabul ediyoruz — bu handler'ı pasif bırakıyoruz.
    # Aktif etmek isterseniz aşağıdaki satırı kaldırın:
    return


# ─── Yardımcı: fotoğrafı kaydet ve onayla ────────────────────────────────────
async def _save_photo(message: Message, file_id: str):
    ok = await db.set_plan_image(file_id)
    if ok:
        await message.reply_text(
            "✅ <b>Plan resmi başarıyla kaydedildi!</b>\n\n"
            "Bundan böyle abone olmayan kullanıcılar /start yazdığında "
            "bu resim ile birlikte plan listesi gösterilecek.\n\n"
            "<i>Bot yeniden başlasa bile resim kaybolmaz.</i>",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await message.reply_text(
            "❌ Resim kaydedilirken bir hata oluştu. Lütfen tekrar deneyin.",
            quote=True,
        )


# ─── /plan2 komutu (/yukselt resmi) ──────────────────────────────────────────
@Client.on_message(filters.command("plan2") & filters.private & CustomFilters.owner)
async def plan2_command(client: Client, message: Message):
    if message.photo:
        await _save_upgrade_photo(message, message.photo.file_id)
        return

    if message.reply_to_message and message.reply_to_message.photo:
        await _save_upgrade_photo(message, message.reply_to_message.photo.file_id)
        return

    current = await db.get_upgrade_image()

    manage_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Resim Gönder / Değiştir", callback_data="upgrade_img_change")],
        [InlineKeyboardButton("🗑️ Resmi Kaldır", callback_data="upgrade_img_delete")],
    ])

    if current:
        await message.reply_photo(
            photo=current,
            caption=(
                "✅ <b>/yukselt resmi ayarlı.</b>\n\n"
                "Değiştirmek için yeni resim gönderin ya da kaldırmak için butona basın."
            ),
            reply_markup=manage_keyboard,
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await message.reply_text(
            "ℹ️ Henüz bir /yukselt resmi ayarlanmamış.\n\n"
            "Ayarlamak için bu mesajı yanıtlayarak (reply) veya /plan2 komutuyla birlikte fotoğraf gönderin.",
            reply_markup=manage_keyboard,
            quote=True,
        )


# ─── Callback: /yukselt resim değiştir ───────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^upgrade_img_change$"))
async def upgrade_img_change_cb(client: Client, callback_query: CallbackQuery):
    if not _is_owner(callback_query):
        return await callback_query.answer("🚫 Yetki yok.", show_alert=True)

    await callback_query.answer()
    await callback_query.message.reply_text(
        "📸 Lütfen /yukselt için yeni resmi gönderin.\n"
        "<i>Bu mesajı yanıtlayarak veya direkt fotoğraf göndererek ayarlayabilirsiniz.</i>",
        parse_mode=enums.ParseMode.HTML,
        quote=True,
    )


# ─── Callback: /yukselt resim kaldır ─────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^upgrade_img_delete$"))
async def upgrade_img_delete_cb(client: Client, callback_query: CallbackQuery):
    if not _is_owner(callback_query):
        return await callback_query.answer("🚫 Yetki yok.", show_alert=True)

    ok = await db.delete_upgrade_image()
    if ok:
        await callback_query.answer("✅ /yukselt resmi kaldırıldı.", show_alert=True)
        try:
            await callback_query.message.edit_caption(
                "🗑️ /yukselt resmi kaldırıldı. Bundan sonra mesaj düz metin olarak gönderilecek."
            )
        except Exception:
            await callback_query.message.edit_text(
                "🗑️ /yukselt resmi kaldırıldı. Bundan sonra mesaj düz metin olarak gönderilecek."
            )
    else:
        await callback_query.answer("❌ Bir hata oluştu.", show_alert=True)


# ─── Yardımcı: /yukselt fotoğrafını kaydet ───────────────────────────────────
async def _save_upgrade_photo(message: Message, file_id: str):
    ok = await db.set_upgrade_image(file_id)
    if ok:
        await message.reply_text(
            "✅ <b>/yukselt resmi başarıyla kaydedildi!</b>\n\n"
            "Bundan böyle aboneler /yukselt yazdığında bu resim gösterilecek.\n\n"
            "<i>Bot yeniden başlasa bile resim kaybolmaz.</i>",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await message.reply_text(
            "❌ Resim kaydedilirken bir hata oluştu. Lütfen tekrar deneyin.",
            quote=True,
        )
