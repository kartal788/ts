from os import getenv, path
from dotenv import load_dotenv

load_dotenv(path.join(path.dirname(path.dirname(__file__)), "config.env"), override=True)

class Telegram:
    API_ID = int(getenv("API_ID", "0"))
    API_HASH = getenv("API_HASH", "")
    BOT_TOKEN = getenv("BOT_TOKEN", "")
    HELPER_BOT_TOKEN = getenv("HELPER_BOT_TOKEN", "")

    BASE_URL = getenv("BASE_URL", "").rstrip('/')
    PORT = int(getenv("PORT", "8001"))

    PARALLEL = int(getenv("PARALLEL", "1"))
    PRE_FETCH = int(getenv("PRE_FETCH", "1"))

    AUTH_CHANNEL = [channel.strip() for channel in (getenv("AUTH_CHANNEL") or "").split(",") if channel.strip()]
    DATABASE = [db.strip() for db in (getenv("DATABASE") or "").split(",") if db.strip()]

    TMDB_API = getenv("TMDB_API", "")

    UPSTREAM_REPO = getenv("UPSTREAM_REPO", "")
    UPSTREAM_BRANCH = getenv("UPSTREAM_BRANCH", "")

    OWNER_ID = int(getenv("OWNER_ID", "0"))
    
    REPLACE_MODE = getenv("REPLACE_MODE", "true").lower() == "true"
    HIDE_CATALOG = getenv("HIDE_CATALOG", "false").lower() == "true"

    SESSION_SECRET_KEY = getenv("SESSION_SECRET_KEY", "")
    TOKEN_HMAC_SECRET  = getenv("TOKEN_HMAC_SECRET", "")   # Stream token imzalama anahtarı (boşsa SESSION_SECRET_KEY kullanılır)
    TRUSTED_PROXY_CIDRS = getenv("TRUSTED_PROXY_CIDRS", "")  # Güvenilir proxy CIDR'ları (ör: "10.0.0.0/8,172.16.0.0/12"). Boş → X-Forwarded-For başlığına güvenilmez.
    
    YENILEME       = getenv("YENILEME", "")        # Token geçerlilik süresi (saat). Boş → varsayılan 6 saat. Video izleme + indirme için geçerli.
    HIZ_LIMITI     = getenv("HIZ_LIMITI", "")      # Megabit/sn cinsinden global hız limiti ("" = limit yok)
    LIMIT_SIFIRLAMA = getenv("LIMIT_SIFIRLAMA", "") # Günlük limit sıfırlama saati (UTC). "SS:DD" formatında. Boş → gece 00:00 UTC'de sıfırlanır. Örn: "06:00" → 06:00 UTC'de sıfırlanır.

    SUBSCRIPTION = getenv("SUBSCRIPTION", "false").lower() == "true"
    SUBSCRIPTION_GROUP_ID = int(getenv("SUBSCRIPTION_GROUP_ID", "0"))
    SUBSCRIPTION_URL = getenv("SUBSCRIPTION_URL", "https://t.me/")
    APPROVER_IDS = [int(x.strip()) for x in (getenv("APPROVER_IDS") or "").split(",") if x.strip().isdigit()]

    WEBSITESI = getenv("WEBSITESI", "false").lower() == "true"  # False → bakım modu, abonelere giriş bilgisi gönderilmez

    ISIM = getenv("ISIM", "KARTAL")
    EKLENTI_ACIKLAMASI = getenv("EKLENTI_ACIKLAMASI", "Dizi ve film arşivi.")
    EKLENTI_LOGOSU = getenv("EKLENTI_LOGOSU", "")
    BOLUM_RESIMI = getenv("BOLUM_RESIMI", "")

    MAX_CONCURRENT_DOWNLOADS = getenv("MAX_CONCURRENT_DOWNLOADS", "")
    MAX_CONCURRENT_UPLOADS = getenv("MAX_CONCURRENT_UPLOADS", "1")

    # ── Proxy Ayarları ────────────────────────────────────────────────────────
    # Proxy=True → proxy aktif
    # ProxyType=HTTPS veya HTTP
    # HTTP_Proxy_URL → proxy URL'si  (örn: https://PROXYURL/?url=)
    # PROXY_MODE:
    #   1 → Sadece normal (proxy yok)
    #   2 → Hem proxy hem normal (ikisi birden gösterilir)
    #   3 → Sadece proxy
    PROXY      = getenv("Proxy", "false").lower() == "true"
    PROXY_TYPE = getenv("ProxyType", "HTTPS")
    HTTP_PROXY_URL = getenv("HTTP_Proxy_URL", "")
    PROXY_MODE = int(getenv("PROXY_MODE", "1"))  # 1=normal, 2=proxy+normal, 3=sadece proxy

    # ── Brute-force (kaba kuvvet) koruması ───────────────────────────────────
    # BRUTE_WINDOW  : Kaç saniye içindeki başarısız girişler sayılsın?    (varsayılan: 60 sn)
    # BRUTE_MAX     : Pencere içinde kaç hata sonrası IP banlansın?       (varsayılan: 10)
    # BRUTE_BAN     : IP kaç saniye boyunca engellensin?                  (varsayılan: 600 sn = 10 dk)
    BRUTE_WINDOW  = int(getenv("BRUTE_WINDOW", "60"))
    BRUTE_MAX     = int(getenv("BRUTE_MAX",    "10"))
    BRUTE_BAN     = int(getenv("BRUTE_BAN",    "600"))