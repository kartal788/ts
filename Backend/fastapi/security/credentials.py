"""
Admin kimlik doğrulaması — config'deki sabit kullanıcı adı/şifre kaldırıldı.
Kullanıcı adı ve şifre artık OWNER_ID'nin /start komutu ile ürettiği
tek kullanımlık değerlerdir (DB → tracking.admin_sessions).

verify_credentials: başarılıysa admin_doc (dict) döner, başarısızsa None.
Template_routes bu dönüşe göre display_name ve photo_url okur.
"""

from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer
from typing import Optional

security = HTTPBearer(auto_error=False)


def is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated", False)


def require_auth(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
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
