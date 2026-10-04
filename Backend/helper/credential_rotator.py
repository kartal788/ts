"""
Üye ve yönetici şifrelerini belirli süre sonra (varsayılan 7 gün) GEÇERSİZ KILAR.

- Şifre, /start ile üretildiği andan (created_at) Ayarlar > Şifre Geçerlilik Süresi (CREDENTIAL_ROTATE_DAYS) gün sonra değişir.
- Yeni şifre OTOMATİK GÖNDERİLMEZ. Kullanıcı yeni şifreyi bota /start yazarak alır.
- Üyenin / yöneticinin açık oturumları da düşer.

Her saat kontrol eder. Panelden değiştirilir, yeniden başlatma gerekmez (varsayılan 7, 0 = kapalı).
"""

import asyncio

from Backend import db
from Backend.config import Telegram
from Backend.logger import LOGGER


def _rotate_days() -> int:
    """Ayarlar sayfasından (CREDENTIAL_ROTATE_DAYS) canlı okunur. 0 = kapalı."""
    try:
        return max(0, int(getattr(Telegram, "CREDENTIAL_ROTATE_DAYS", 7)))
    except (TypeError, ValueError):
        return 7

CHECK_INTERVAL = 3600  # saniye


async def _expire_members(members: list) -> None:
    for doc in members:
        user_id = doc.get("user_id")
        if user_id is None:
            continue
        try:
            await db.expire_member_credentials(int(user_id))
            LOGGER.info(f"Üye şifresi geçersiz kılındı (süre doldu): {user_id}")
        except Exception as e:
            LOGGER.error(f"Üye şifresi geçersiz kılınamadı ({user_id}): {e}")


async def _expire_admins(admins: list) -> None:
    for doc in admins:
        key = doc.get("_id")
        # OWNER kaydı: _id="admin" → admin_id=None; diğer yöneticiler: admin_user_id
        admin_id = None if key == "admin" else doc.get("admin_user_id")
        try:
            # Şifreyi siler + session_version'ı artırır (açık oturumlar düşer)
            await db.invalidate_admin_session(admin_id)
            LOGGER.info(f"Yönetici şifresi geçersiz kılındı (süre doldu): {key}")
        except Exception as e:
            LOGGER.error(f"Yönetici şifresi geçersiz kılınamadı ({key}): {e}")


async def credential_rotator_loop():
    while True:
        try:
            days = _rotate_days()
            if days > 0:
                old = await db.get_credentials_older_than(days)
                if old["members"] or old["admins"]:
                    LOGGER.info(
                        f"Şifre süresi dolanlar: {len(old['members'])} üye, {len(old['admins'])} yönetici"
                    )
                await _expire_members(old["members"])
                await _expire_admins(old["admins"])
        except Exception as e:
            LOGGER.error(f"Şifre yenileme döngüsünde hata: {e}")
        await asyncio.sleep(CHECK_INTERVAL)
