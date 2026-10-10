"""
/koleksiyonlar komutu — yalnızca aktif üyeliği olan kullanıcılara, özel mesajda,
Nuvio'ya aktarılacak koleksiyonlar.json bağlantılarını (Türkçe / Deutsch / English) gönderir.
"""
from datetime import datetime

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Backend import db
from Backend.config import Telegram


@Client.on_message(filters.command("koleksiyonlar") & filters.private, group=10)
async def koleksiyonlar_command(client: Client, message: Message):
    try:
        user_id = (message.from_user.id if message.from_user else None) or message.chat.id

        # Ban kontrolü
        if await db.is_user_banned(user_id):
            await message.reply_text(
                "🚫 <b>Hesabınız engellenmiştir.</b>",
                quote=True,
                parse_mode=enums.ParseMode.HTML,
            )
            return

        base_url = Telegram.BASE_URL
        token_str = None

        if Telegram.SUBSCRIPTION:
            # Yalnızca aktif üyeliği olanlar
            user = await db.get_user(user_id)
            is_active = False
            if user and user.get("subscription_status") == "active":
                expiry = user.get("subscription_expiry")
                if expiry and expiry > datetime.utcnow():
                    is_active = True
                else:
                    await db.mark_user_expired(user_id)

            if not is_active:
                await message.reply_text(
                    "🔒 <b>Bu komut yalnızca aktif üyeliği olan kullanıcılar içindir.</b>\n\n"
                    "Üyelik almak veya yenilemek için /start yazabilirsiniz.",
                    quote=True,
                    parse_mode=enums.ParseMode.HTML,
                )
                return

            all_tokens = await db.get_all_api_tokens()
            token_doc = next((t for t in all_tokens if t.get("user_id") == user_id), None)
            token_str = token_doc.get("token") if token_doc else None
        else:
            # Abonelik sistemi kapalıyken herkes üye sayılır (/start ile aynı mantık)
            try:
                name = (message.from_user.first_name or message.from_user.username or f"User {user_id}") \
                    if message.from_user else f"Chat {user_id}"
                token_doc = await db.add_api_token(name=name, user_id=user_id)
                token_str = token_doc.get("token")
            except Exception as e:
                print(f"DEBUG: /koleksiyonlar token error: {e}")

        if not token_str:
            await message.reply_text(
                "⚠️ Bağlantınız oluşturulurken bir sorun oluştu. Lütfen yönetici ile iletişime geçin.",
                quote=True,
                parse_mode=enums.ParseMode.HTML,
            )
            return

        tr_url = f"{base_url}/stremio/{token_str}/tr/koleksiyonlar.json"
        de_url = f"{base_url}/stremio/{token_str}/de/koleksiyonlar.json"
        en_url = f"{base_url}/stremio/{token_str}/en/koleksiyonlar.json"

        await message.reply_text(
            '📚 <b>Koleksiyonlar</b>\n\n'
            '🇹🇷 <b>Türkçe:</b>\n'
            f'<a href="{tr_url}">{tr_url}</a>\n\n'
            '🇩🇪 <b>Deutsch:</b>\n'
            f'<a href="{de_url}">{de_url}</a>\n\n'
            '🇬🇧 <b>English:</b>\n'
            f'<a href="{en_url}">{en_url}</a>\n\n'
            'Bağlantıyı tarayıcınızda açıp sayfadaki tüm JSON metnini kopyalayın. '
            'Nuvio’da Görünüm → Koleksiyonlar → İçe Aktar bölümünden içeriği yapıştırın.',
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        await message.reply_text(f"⚠️ Error: {e}")
        print(f"Error in /koleksiyonlar handler: {e}")
