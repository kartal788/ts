"""
Admin kimlik doğrulaması — config'deki sabit kullanıcı adı/şifre kaldırıldı.
Kullanıcı adı ve şifre artık OWNER_ID'nin /start komutu ile ürettiği
tek kullanımlık değerlerdir (DB → tracking.admin_sessions).

verify_credentials: başarılıysa admin_doc (dict) döner, başarısızsa None.
Template_routes bu dönüşe göre display_name ve photo_url okur.

Oturum geçersiz kılma (invalidation) kuralları:
  1. Bot yeniden başlatıldığında   → session_version DB'de artırılır
  2. /start komutu atıldığında     → session_version DB'de artırılır
  3. 72 saat geçtiğinde            → login_at cookie alanı kontrol edilir
"""

from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer
from typing import Optional
import time

SESSION_MAX_AGE = 72 * 3600  # 72 saat (saniye cinsinden)

security = HTTPBearer(auto_error=False)


def is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated", False)


async def require_auth(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    # ── 1) 72 saatlik süre kontrolü ────────────────────────────────────────
    login_at = request.session.get("login_at", 0)
    if (time.time() - login_at) > SESSION_MAX_AGE:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Session expired")

    # ── 2) session_version kontrolü (bot restart / /start invalidation) ────
    stored_version = request.session.get("session_version", -1)
    try:
        from Backend import db
        current_version = await db.get_admin_session_version()
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
