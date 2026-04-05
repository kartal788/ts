import asyncio
from pyrogram import Client
from Backend.config import Telegram
from Backend import db
from datetime import datetime
from Backend.logger import LOGGER

async def subscription_checker_loop(bot: Client):
    while True:
        try:
            if not Telegram.SUBSCRIPTION:
                await asyncio.sleep(3600)
                continue

            LOGGER.info("Abonelik kontrolcüsü çalışıyor...")

            # 1. Süresi dolmuş kullanıcıları getir ve gruptan çıkar
            expired_users = await db.get_expired_users()
            for user in expired_users:
                user_id = user["_id"]
                try:
                    # Kullanıcıyı kalıcı ban atmadan gruptan çıkarmak için ban + unban uygula
                    await bot.ban_chat_member(Telegram.SUBSCRIPTION_GROUP_ID, user_id)
                    await bot.unban_chat_member(Telegram.SUBSCRIPTION_GROUP_ID, user_id)

                    await db.mark_user_expired(user_id)

                    # Kullanıcıyı bilgilendir
                    await bot.send_message(
                        user_id,
                        "❌ <b>Abonelik Süresi Doldu</b>\n\n"
                        "Aboneliğinizin süresi doldu ve özel gruptan çıkarıldınız.\n"
                        f"Aboneliğinizi yenilemek ve gruba tekrar erişim sağlamak için {Telegram.SUBSCRIPTION_URL} adresine gidin ve /start komutunu gönderin."
                    )
                    LOGGER.info(f"Süresi dolmuş kullanıcı gruptan çıkarıldı: {user_id}")
                except Exception as e:
                    LOGGER.error(f"Kullanıcı gruptan çıkarılırken/bildirim gönderilirken hata oluştu {user_id}: {e}")

            # 2. 24 saat içinde süresi dolacak kullanıcılara hatırlatma gönder
            expiring_users = await db.get_expiring_users(hours=24)
            for user in expiring_users:
                user_id = user["_id"]
                expiry = user["subscription_expiry"]
                try:
                    await bot.send_message(
                        user_id,
                        f"⚠️ <b>Abonelik Süresi Yakında Doluyor</b>\n\n"
                        f"Aboneliğiniz <b>{expiry.strftime('%Y-%m-%d %H:%M UTC')}</b> tarihinde sona erecek.\n"
                        f"Gruba erişiminizi kaybetmeden önce aboneliğinizi yenilemek için {Telegram.SUBSCRIPTION_URL} adresine gidin ve /start komutunu gönderin!"
                    )
                    await db.mark_reminder_sent(user_id)
                    LOGGER.info(f"Abonelik hatırlatması gönderildi: {user_id}")
                except Exception as e:
                    LOGGER.error(f"Kullanıcıya hatırlatma gönderilirken hata oluştu {user_id}: {e}")

            # Her saat kontrol et
            await asyncio.sleep(3600)

        except Exception as e:
            LOGGER.error(f"Abonelik kontrolcüsü döngüsünde hata: {e}")
            await asyncio.sleep(300)  # Hata durumunda 5 dakika bekle ve tekrar dene
