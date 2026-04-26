from fastapi import HTTPException
from datetime import datetime, timezone
from Backend import db
from Backend.config import Telegram


def _utcnow() -> datetime:
    """Her zaman timezone-aware UTC datetime döner (naive datetime.utcnow() yerine kullanılır)."""
    return datetime.now(timezone.utc)


def _to_aware(dt: datetime) -> datetime:
    """
    Naive datetime'ı UTC-aware'e çevirir.
    Zaten aware ise dokunmaz. None gelirse None döner.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _configure_url(token: str, lang: str = "tr") -> str:
    base = Telegram.BASE_URL.rstrip("/")
    return f"{base}/stremio/{token}/{lang}/configure"


async def verify_token(token: str):
    """
    Temel token doğrulaması: geçerlilik, abonelik, günlük/aylık limit.
    IP ve cihaz limiti kontrolü için check_ip_device_limits() kullanın.
    """
    token_data = await db.get_api_token(token)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired API token")

    limits = token_data.get("limits", {})
    usage  = token_data.get("usage", {})

    token_data["limit_exceeded"]       = None
    token_data["limit_video"]          = None
    token_data["limit_token"]          = token
    token_data["subscription_expired"] = False

    # --- Token seviyesi geçerlilik kontrolü (expires_at) ---
    token_expires_at = token_data.get("expires_at")
    if token_expires_at:
        if isinstance(token_expires_at, str):
            try:
                # fromisoformat() Python 3.11+ öncesinde "Z" suffix'ini desteklemez;
                # replace() ile "+00:00"'a çeviriyoruz ki aware datetime parse edilsin.
                token_expires_at = datetime.fromisoformat(
                    token_expires_at.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                token_expires_at = None
        # DB'den naive datetime gelebilir; aware'e normalize et
        token_expires_at = _to_aware(token_expires_at)
        if token_expires_at and token_expires_at < _utcnow():
            token_data["subscription_expired"] = True
            return token_data
    else:
        token_expires_at = None

    # --- Subscription expiry check ---
    if Telegram.SUBSCRIPTION:
        user_id = token_data.get("user_id")
        if not user_id:
            if not token_expires_at:
                token_data["subscription_expired"] = True
                return token_data
        else:
            user = await db.get_user(int(user_id))
            if not user or user.get("subscription_status") != "active":
                if not token_expires_at:
                    token_data["subscription_expired"] = True
                    return token_data
            else:
                expiry = user.get("subscription_expiry")
                if not expiry:
                    if not token_expires_at:
                        token_data["subscription_expired"] = True
                        return token_data
                else:
                    # Her iki tarafı da aware'e normalize ederek güvenli karşılaştırma yap.
                    # DB'den naive veya aware gelebilir; _to_aware() her ikisini de doğru işler.
                    if _to_aware(expiry) < _utcnow():
                        if not token_expires_at:
                            token_data["subscription_expired"] = True
                            return token_data

    # --- Günlük limit aşımı nedeniyle devre dışı bırakılan token kontrolü ---
    if token_data.get("daily_limit_disabled"):
        token_data["limit_exceeded"] = "daily"
        token_data["limit_video"]    = _configure_url(token)
        return token_data

    # --- Günlük / Aylık limit kontrolü ---
    if daily_limit := limits.get("daily_limit_gb"):
        if daily_limit > 0:
            current_daily_gb = usage.get("daily", {}).get("bytes", 0) / (1024 ** 3)
            if current_daily_gb >= daily_limit:
                token_data["limit_exceeded"] = "daily"
                token_data["limit_video"]    = _configure_url(token)
                return token_data

    if monthly_limit := limits.get("monthly_limit_gb"):
        if monthly_limit > 0:
            current_monthly_gb = usage.get("monthly", {}).get("bytes", 0) / (1024 ** 3)
            if current_monthly_gb >= monthly_limit:
                token_data["limit_exceeded"] = "monthly"
                token_data["limit_video"]    = _configure_url(token)
                return token_data

    return token_data


async def check_ip_device_limits(token: str, token_data: dict, request) -> dict:
    """
    IP ve cihaz (eşzamanlı stream) limitlerini kontrol eder.
    verify_token()'dan dönen token_data üzerinde çalışır.
    Yalnızca stream endpoint'lerinden çağrılır; request nesnesi doğrudan geçilir.
    """
    if token_data.get("limit_exceeded") or token_data.get("subscription_expired"):
        return token_data

    limits = token_data.get("limits", {})

    # --- Global varsayılan limit (DEFAULT_DEVICE_LIMIT) ---
    # Token'ın kendi limiti 0 (sınırsız) ise config'deki varsayılana düşülür.
    # Bu sayede config değişince mevcut tüm tokenlar anında etkilenir.
    _cfg_device_limit = int(getattr(Telegram, "DEFAULT_DEVICE_LIMIT", 0) or 0)

    # --- Cihaz (eşzamanlı stream) limiti ---
    # Limit tanımlıysa engelle; tanımlı değilse sadece sayımı okumak stream_routes görevi.
    device_limit = int(limits.get("device_limit") or 0) or _cfg_device_limit
    if device_limit > 0:
        active_count = await db.get_active_device_count(token)
        if active_count >= device_limit:
            token_data["limit_exceeded"] = "device"
            token_data["limit_video"]    = _configure_url(token)
            return token_data

    return token_data
