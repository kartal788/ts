from pyrogram import Client, filters, enums
from pyrogram.types import Message
from Backend.helper.custom_filter import CustomFilters
from Backend import db

# -------------------------- /unban komutu ----------------------
@Client.on_message(filters.command("engelkaldir") & filters.private & CustomFilters.owner)
async def unban_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "⚠️ Kullanım: <code>/engelkaldir &lt;user_id&gt;</code>\n\n"
            "Örnek: <code>/engelkaldir 123456789</code>",
            parse_mode=enums.ParseMode.HTML
        )

    try:
        target_id = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ Geçersiz kullanıcı ID. Sayısal bir ID giriniz.")

    is_banned = await db.is_user_banned(target_id)
    if not is_banned:
        return await message.reply_text(f"ℹ️ Kullanıcı <code>{target_id}</code> zaten banlı değil.", parse_mode=enums.ParseMode.HTML)

    success = await db.unban_user(target_id)
    if success:
        try:
            await client.send_message(
                target_id,
                "🔓 <b>Engeliniz Kaldırıldı</b>\n\nHesabınızdaki engel yönetici tarafından kaldırıldı. "
                "/start yazarak plan seçebilirsiniz.",
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass
        await message.reply_text(
            f"✅ Kullanıcı <code>{target_id}</code> engeli kaldırıldı.\n"
            f"Artık /start yazarak plan seçebilir.",
            parse_mode=enums.ParseMode.HTML
        )
    else:
        await message.reply_text(f"❌ Engel kaldırma işlemi başarısız oldu. Kullanıcı ID: <code>{target_id}</code>", parse_mode=enums.ParseMode.HTML)

