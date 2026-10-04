"""
Admin kimlik doğrulaması — config'deki sabit kullanıcı adı/şifre kaldırıldı.
Kullanıcı adı ve şifre artık OWNER_ID'nin ve panelden eklenen yöneticilerin
(APPROVER_IDS) /start komutu ile ürettiği, kişiye özel tek kullanımlık
değerlerdir (DB → tracking.admin_sessions; OWNER: "admin", diğerleri: "admin_<id>").

verify_credentials: başarılıysa admin_doc (dict) döner, başarısızsa None.
Template_routes bu dönüşe göre display_name ve photo_url okur.

Oturum geçersiz kılma (invalidation) kuralları:
  1. Bot yeniden başlatıldığında   → session_version DB'de artırılır
  2. /start komutu atıldığında     → o yöneticinin session_version değeri artırılır
  2b. Yönetici listeden çıkarılırsa → açık oturumu bir sonraki istekte düşer
  3. /start'tan 4 gün sonra        → oturum/cookie silinir (SessionExpiryMiddleware)
  4. /start'tan 7 gün sonra        → şifre geçersiz olur (credential_rotator, yeni şifre GÖNDERİLMEZ)
"""

from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer
from typing import Optional
import time
from datetime import timezone

# /start (şifre üretimi) anından itibaren oturum/cookie ömrü: 4 gün
SESSION_MAX_AGE = 4 * 24 * 3600

security = HTTPBearer(auto_error=False)


def otp_started_ts(doc) -> float:
    """DB kaydındaki created_at (= /start anı) değerini unix timestamp'e çevirir.
    Okunamazsa şimdiki zamanı döner."""
    try:
        created = (doc or {}).get("created_at")
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return created.timestamp()
    except Exception:
        pass
    return time.time()


def is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated", False)


async def require_auth(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    # ── 2) session_version kontrolü (bot restart / /start invalidation) ────
    stored_version = request.session.get("session_version", -1)
    admin_key = request.session.get("admin_key", "admin")
    try:
        from Backend import db
        from Backend.config import Telegram

        # Panelden yönetici listesinden çıkarılan kişinin açık oturumu hemen düşer
        if admin_key != "admin":
            try:
                admin_uid = int(str(admin_key).replace("admin_", "", 1))
            except ValueError:
                admin_uid = None
            if admin_uid is None or admin_uid not in Telegram.APPROVER_IDS:
                request.session.clear()
                raise HTTPException(status_code=401, detail="Admin access revoked")

        current_version = await db.get_admin_session_version(admin_key)
        if stored_version != current_version:
            request.session.clear()
            raise HTTPException(status_code=401, detail="Session invalidated")
    except HTTPException:
        raise
    except Exception:
        # DB erişim hatası → ihtiyatlı olarak reddet
        request.session.clear()
        raise HTTPException(status_code=401, detail="Session validation error")

    return True


def get_current_user(request: Request) -> Optional[dict]:
    """Admin panel için oturum bilgilerini dict olarak döner."""
    if is_authenticated(request):
        return {
            "name":      request.session.get("username", "Yönetici"),
            "photo_url": request.session.get("photo_url", ""),
        }
    return None


async def verify_credentials(username: str, password: str) -> Optional[dict]:
    """
    Girilen kullanıcı adı ve şifreyi DB'deki admin_sessions kaydıyla karşılaştırır.
    Başarılıysa admin doc (display_name, photo_url vb. içerir) döner.
    Başarısızsa None döner.
    """
    from Backend import db
    return await db.verify_admin_credentials(username, password)
