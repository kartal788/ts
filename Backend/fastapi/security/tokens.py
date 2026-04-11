from fastapi import HTTPException
from datetime import datetime
from Backend import db
from Backend.config import Telegram


def _configure_url(token: str, lang: str = "tr") -> str:
    base = Telegram.BASE_URL.rstrip("/")
    return f"{base}/stremio/{token}/{lang}/configure"


async def verify_token(token: str):
    token_data = await db.get_api_token(token)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired API token")

    limits = token_data.get("limits", {})
    usage = token_data.get("usage", {})

    token_data["limit_exceeded"] = None
    token_data["limit_video"] = None
    token_data["limit_token"] = token
    token_data["subscription_expired"] = False

    # --- Token seviyesi geçerlilik kontrolü (expires_at) ---
    # Dashboard'dan "Geçerlilik (Gün)" girildiğinde token'a expires_at set edilir.
    # Bu kontrol SUBSCRIPTION ayarından bağımsız olarak her zaman çalışır.
    token_expires_at = token_data.get("expires_at")
    if token_expires_at:
        # convert_objectid_to_str ISO string'e çevirmiş olabilir
        if isinstance(token_expires_at, str):
            try:
                token_expires_at = datetime.fromisoformat(token_expires_at.replace("Z", ""))
            except (ValueError, AttributeError):
                token_expires_at = None
        if token_expires_at and token_expires_at < datetime.utcnow():
            token_data["subscription_expired"] = True
            return token_data
    else:
        token_expires_at = None  # Açıkça None garantile

    # --- Subscription expiry check (only when SUBSCRIPTION feature is enabled) ---
    if Telegram.SUBSCRIPTION:
        user_id = token_data.get("user_id")

        if not user_id:
            # Token'a bağlı kullanıcı yok.
            # Eğer token bazlı geçerlilik varsa (expires_at) ve süresi dolmamışsa izin ver.
            # Yoksa expired say.
            if not token_expires_at:
                token_data["subscription_expired"] = True
                return token_data
            # token_expires_at geçerliyse (yukarıda kontrol edildi) devam et
        else:
            user = await db.get_user(int(user_id))
            if not user or user.get("subscription_status") != "active":
                # Kullanıcı aktif değil; token bazlı geçerlilik yoksa expired say
                if not token_expires_at:
                    token_data["subscription_expired"] = True
                    return token_data
            else:
                expiry = user.get("subscription_expiry")
                if not expiry:
                    # Kullanıcı abonelik tarihi yok; token bazlı süre de yoksa expired
                    if not token_expires_at:
                        token_data["subscription_expired"] = True
                        return token_data
                else:
                    # Kullanıcı abonelik tarihini kontrol et
                    now = datetime.utcnow()
                    try:
                        if expiry.tzinfo is not None:
                            from datetime import timezone
                            now = datetime.now(timezone.utc)
                    except AttributeError:
                        pass
                    if expiry < now:
                        # Kullanıcı aboneliği bitmiş; token bazlı süre de yoksa expired
                        if not token_expires_at:
                            token_data["subscription_expired"] = True
                            return token_data

    if daily_limit := limits.get("daily_limit_gb"):
        if daily_limit > 0:
            current_daily_gb = usage.get("daily", {}).get("bytes", 0) / (1024 ** 3)
            if current_daily_gb >= daily_limit:
                token_data["limit_exceeded"] = "daily"
                token_data["limit_video"] = _configure_url(token)
                return token_data

    if monthly_limit := limits.get("monthly_limit_gb"):
        if monthly_limit > 0:
            current_monthly_gb = usage.get("monthly", {}).get("bytes", 0) / (1024 ** 3)
            if current_monthly_gb >= monthly_limit:
                token_data["limit_exceeded"] = "monthly"
                token_data["limit_video"] = _configure_url(token)
                return token_data

    return token_data
