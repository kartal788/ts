import asyncio
from pyrogram import Client, enums
from Backend.config import Telegram
from Backend import db
from datetime import datetime, timezone, timedelta

_TZ_TR = timezone(timedelta(hours=3))  # UTC+3 Türkiye saati
from Backend.logger import LOGGER

async def subscription_checker_loop(bot: Client):
    while True:
        try:
            if not Telegram.SUBSCRIPTION:
                await asyncio.sleep(300)
                continue

            LOGGER.info("Abonelik kontrolcüsü çalışıyor...")

            # Süresi dolmuş kullanıcıları getir ve gruptan çıkar
            expired_users = await db.get_expired_users()
            for user in expired_users:
                user_id = user["_id"]

                # 1. Önce DB'yi güncelle — tekrar tetiklenmesin
                await db.mark_user_expired(user_id)

                # 2. Kullanıcıyı bilgilendir
                try:
                    await bot.send_message(
                        user_id,
                        "❌ Abonelik süreniz sona erdi.\n"
                        "Aboneliğinizi yenilemek için /start komutunu gönderin.",
                        parse_mode=enums.ParseMode.DISABLED
                    )
                except Exception as e:
                    LOGGER.error(f"Bildirim gönderilemedi {user_id}: {e}")

                # 3. Gruptan çıkar (ban hata verse de mesaj gitmiş olur)
                try:
                    await bot.ban_chat_member(Telegram.SUBSCRIPTION_GROUP_ID, user_id)
                    await bot.unban_chat_member(Telegram.SUBSCRIPTION_GROUP_ID, user_id)
                    LOGGER.info(f"Süresi dolmuş kullanıcı gruptan çıkarıldı: {user_id}")
                except Exception as e:
                    LOGGER.error(f"Kullanıcı gruptan çıkarılamadı {user_id}: {e}")

            # 24 saat içinde süresi dolacak kullanıcılara hatırlatma gönder
            expiring_users = await db.get_expiring_users(hours=24)
            for user in expiring_users:
                user_id = user["_id"]
                expiry = user["subscription_expiry"]
                try:
                    expiry_tr = expiry.replace(tzinfo=timezone.utc).astimezone(_TZ_TR)
                    await bot.send_message(
                        user_id,
                        f"⚠️ Aboneliğiniz {expiry_tr.strftime('%d.%m.%Y %H:%M')} tarihinde sona erecek.\n",
                        parse_mode=enums.ParseMode.DISABLED
                    )
                    await db.mark_reminder_sent(user_id)
                    LOGGER.info(f"Abonelik hatırlatması gönderildi: {user_id}")
                except Exception as e:
                    LOGGER.error(f"Kullanıcıya hatırlatma gönderilirken hata oluştu {user_id}: {e}")

            # Her 5 dakikada bir kontrol et
            await asyncio.sleep(300)

        except Exception as e:
            LOGGER.error(f"Abonelik kontrolcüsü döngüsünde hata: {e}")
            await asyncio.sleep(300)  # Hata durumunda 5 dakika bekle ve tekrar dene
