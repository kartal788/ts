import Backend
from Backend.helper.custom_filter import CustomFilters
from pyrogram import filters, Client, enums
from pyrogram.types import Message
from Backend.logger import LOGGER


@Client.on_message(filters.command('set') & filters.private & CustomFilters.owner, group=10)
async def manual(client: Client, message: Message):
    try:
        command = message.text.split(maxsplit=1)

        if len(command) == 2:
            url = command[1].strip()
            Backend.USE_DEFAULT_ID = url

            await message.reply_text(
                f"✅ <b>Varsayılan IMDB/TMDB URL'si Ayarlandı!</b>\n\n"
                f"Bot artık gönderdiğiniz dosyalar için bu URL'yi kullanacak:\n"
                f"<code>{Backend.USE_DEFAULT_ID}</code>\n\n"
                f"<b>Talimatlar:</b>\n"
                f"1. İlgili film veya dizi dosyalarını kanalınıza iletin.\n"
                f"2. Tüm dosyalar yüklendikten sonra, <code>/set</code> komutunu URL vermeden "
                f"göndererek varsayılan URL'yi temizleyin.",
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )
        else:
            Backend.USE_DEFAULT_ID = None
            await message.reply_text(
                "✅ <b>Varsayılan IMDB/TMDB URL'si Kaldırıldı!</b>\n\n"
                "Artık dosyaları varsayılan bir IMDB URL'sine bağlamadan manuel olarak yükleyebilirsiniz.",
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )

    except Exception as e:
        LOGGER.error(f"/set komutunda hata: {e}")
        await message.reply_text(f"⚠️ Bir hata oluştu: {e}")
        
