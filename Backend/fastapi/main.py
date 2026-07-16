from fastapi import FastAPI, Request, Form, Depends, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import secrets
import os
from Backend import __version__
from Backend.fastapi.security.credentials import require_auth
from Backend.fastapi.security.csrf import CSRFMiddleware, ensure_csrf_secret, get_csrf_token
from Backend.fastapi.routes.stream_routes import router as stream_router, decay_client_failures
from Backend.helper.db_scheduler import start_scheduler, stop_scheduler
from Backend.fastapi.routes.yayin_routes import start_scheduler as start_yayin_scheduler, stop_scheduler as stop_yayin_scheduler
from Backend.fastapi.routes.stremio_routes import router as stremio_router, admin_catalog_router
from Backend.fastapi.routes.subtitle_routes import router as subtitle_router
from Backend.fastapi.routes.yayin_routes import router as yayin_router
from Backend.fastapi.routes.template_routes import (
    login_page, login_post, logout, set_theme, dashboard_page,
    media_management_page, edit_media_page,
    admin_dashboard_page, admin_subscriptions_page, admin_access_page, canli_page,
    link_ekle_page, istatistik_page, sunucu_page, settings_page,
    istekler_page, kataloglar_page, araclar_page
)
from Backend.fastapi.routes.arac_routes import (
    ayni_status_api, ayni_start_api,
    iceriksil_status_api, iceriksil_start_api,
    tara_status_api, tara_start_api, tara_iptal_api,
)
from Backend.fastapi.routes.link_ekle_routes import (
    link_ekle_query, link_ekle_save
)
from Backend.fastapi.routes.manual_add_routes import (
    manual_add_status_api, manual_add_start_api, manual_add_stop_api,
    manual_add_set_season_api, manual_add_set_next_episode_api
)
from Backend.fastapi.routes.attach_add_routes import (
    attach_mode_status_api, attach_mode_start_api, attach_mode_stop_api,
    attach_mode_set_season_api, attach_mode_set_next_episode_api
)
from Backend.fastapi.routes.sunucu_routes import (
    sunucu_yukle_stream, sunucu_bilgisayardan_yukle, sunucu_listele, sunucu_sil,
    sunucu_yeniden_adlandir, sunucu_metadata, sunucu_klasor_olustur,
    sunucu_sistem_durumu, sunucu_metadata_sorgu, sunucu_metadata_kaydet,
    sunucu_metadata_sil, sunucu_indir, sunucu_indir_klasor,
    sunucu_klasor_zip_baslat, sunucu_klasor_zip_durum,
    sunucu_dosya_durumu,
    sunucu_gdrive_listele, sunucu_gdrive_ekle,
    sunucu_gdrive_meta_sorgu, sunucu_gdrive_ekle_onay,
    sunucu_gdrive_db_listele, sunucu_gdrive_db_sil, sunucu_gdrive_migrate,
    sunucu_rclone_remotes, sunucu_rclone_listele,
    sunucu_rclone_meta_sorgu, sunucu_rclone_ekle_onay,
    sunucu_rclone_db_listele, sunucu_rclone_db_sil, sunucu_rclone_migrate,
)
from Backend.fastapi.routes.member_routes import (
    member_login_page, member_login_post, member_logout,
    member_catalog_page, member_media_api,
    member_hatirlatmalar_page,
    member_tv_detail_api, member_stream_url_api, member_usage_api,
    member_profile_api, member_db_size_api,
)
from Backend.fastapi.routes.notification_routes import (
    toggle_reminder,
    reminder_status,
    my_reminders,
    toggle_movie_reminder,
    movie_reminder_status,
    my_movie_reminders,
    submit_content_request,
    my_content_requests,
    admin_list_content_requests,
    admin_review_content_requests,
)
from Backend.fastapi.routes.api_routes import (
    list_media_api, delete_media_api, update_media_api, requery_media_api,
    delete_movie_quality_api, delete_tv_quality_api,
    delete_tv_episode_api, delete_tv_season_api,
    rename_movie_quality_api, rename_tv_quality_api,
    create_token_api, revoke_token_api, update_token_limits_api,
    speed_test_api, speed_test_stream_api,
    get_admin_stats_api, clear_cache_api, get_dead_links_api,
    get_stream_analytics_api, clear_analytics_api,
    get_subscription_plans_api, add_subscription_plan_api,
    update_subscription_plan_api, delete_subscription_plan_api,
    get_all_subscribers_api, manage_subscriber_api,
    get_all_tokens_api, assign_plan_api, link_token_user_api,
    get_settings_api, update_settings_api,
    export_settings_backup_api, import_settings_backup_api,
    invalidate_admin_sessions_api,
    get_db_stats_api, get_logs_api, download_logs_api,
    restart_bot_api,
)
from Backend.fastapi.routes.uyeler_routes import (
    admin_uyeler_page,
    admin_uye_detay_page,
    admin_uyeler_list_api,
    admin_uye_stream_history_api,
    admin_uye_reminders_api,
)

app = FastAPI(
    title="Telegram Stremio Media Server",
    description="A powerful, self-hosted Telegram Stremio Media Server built with FastAPI, MongoDB, and PyroFork seamlessly integrated with Stremio for automated media streaming and discovery.",
    version=__version__
)

# --- Middleware Setup ---
# Session secret key: config.env'den okunur — tanımlı olması ZORUNLUDUR.
from Backend.config import Telegram as _TG
import logging as _logging

if not _TG.SESSION_SECRET_KEY:
    raise RuntimeError(
        "\n\n"
        "KRİTİK GÜVENLİK HATASI — BOT DURDU\n"
        "SESSION_SECRET_KEY config.env'de tanımlı değil!\n\n"
        "Bu key olmadan oturumlar güvensiz olur ve her restart'ta\n"
        "tüm admin/üye oturumları sıfırlanır.\n\n"
        "Çözüm — config.env dosyasına şu satırı ekle:\n"
        "  SESSION_SECRET_KEY=\"<güçlü-rastgele-değer>\"\n\n"
        "Güvenli bir key üretmek için terminalde şunu çalıştır:\n"
        "  python3 -c \"import secrets; print(secrets.token_hex(32))\"\n"
    )

if not _TG.TOKEN_HMAC_SECRET:
    raise RuntimeError(
        "\n\n"
        "KRİTİK GÜVENLİK HATASI — BOT DURDU\n"
        "TOKEN_HMAC_SECRET config.env'de tanımlı değil!\n\n"
        "Bu key olmadan stream token'ları imzasız (güvensiz) çalışır;\n"
        "token manipülasyonu tespit edilemez.\n\n"
        "Çözüm — config.env dosyasına şu satırı ekle:\n"
        "  TOKEN_HMAC_SECRET=\"<güçlü-rastgele-değer>\"\n\n"
        "Güvenli bir key üretmek için terminalde şunu çalıştır:\n"
        "  python3 -c \"import secrets; print(secrets.token_hex(32))\"\n"
    )

_session_key = _TG.SESSION_SECRET_KEY

# HTTPS kontrolü: BASE_URL https ile başlıyorsa cookie'yi Secure yap
_https_only = _TG.BASE_URL.startswith("https://") if _TG.BASE_URL else False

# SessionMiddleware en sona (en dışa) taşındı — bkz. dosya sonu.
# ── CORS ──────────────────────────────────────────────────────────────────────
# Stremio eklenti endpoint'leri (/stremio/*, /dl/*) herkese açık olmalı —
# Stremio uygulaması app.strem.io, web.stremio.com veya null origin'den istek yapar.
# Admin/API endpoint'leri ise yalnızca kendi domain'imizden gelen isteklere izin verir.
#
# Strateji: tek bir CORSMiddleware tüm path'lere uygulanır.
# /stremio/* ve /dl/* → allow_origins=["*"], credentials=False
# Diğerleri          → allow_origins=[BASE_URL], credentials=True
# Bunu tek middleware ile yapmanın en temiz yolu: custom middleware.

_admin_cors_origins: list[str] = []
if _TG.BASE_URL:
    _admin_cors_origins.append(_TG.BASE_URL.rstrip("/"))
_extra_origins = [o.strip() for o in (os.getenv("CORS_ORIGINS", "")).split(",") if o.strip()]
_admin_cors_origins.extend(_extra_origins)

# Stremio public path prefix'leri
_PUBLIC_CORS_PREFIXES = ("/stremio/", "/dl/", "/subtitles/")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as _StarResponse
from starlette.types import ASGIApp

class SmartCORSMiddleware(BaseHTTPMiddleware):
    """
    Path bazlı CORS:
      - /stremio/* ve /dl/*  → herkese açık (allow_origins=*)
      - Diğer tüm path'ler  → sadece kendi domain'imiz (admin/API koruması)
    """
    async def dispatch(self, request, call_next):
        origin = request.headers.get("origin", "")
        path   = request.url.path
        is_public = any(path.startswith(p) for p in _PUBLIC_CORS_PREFIXES)

        if request.method == "OPTIONS":
            # Preflight
            response = _StarResponse(status_code=204)
        else:
            response = await call_next(request)

        if is_public:
            response.headers["Access-Control-Allow-Origin"]  = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Range"
            response.headers["Access-Control-Expose-Headers"] = "Content-Range, Content-Length, Accept-Ranges"
        elif origin:
            allowed = _admin_cors_origins if _admin_cors_origins else ["*"]
            if origin in allowed or allowed == ["*"]:
                response.headers["Access-Control-Allow-Origin"]      = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Allow-Methods"]     = "GET, POST, PUT, DELETE, HEAD, OPTIONS"
                response.headers["Access-Control-Allow-Headers"]     = "Content-Type, Authorization, X-Requested-With, Range"
                response.headers["Vary"] = "Origin"

        return response

app.add_middleware(SmartCORSMiddleware)

# ── Üye API Rate Limiter ──────────────────────────────────────────────────────
# Sliding-window rate limiting — hem üye hem admin endpoint'lerini kapsar.
#
# Kapsam:
#   /api/uye/*   → oturum sahibi üye başına (user_id bazlı)
#   /api/admin/* → oturum sahibi admin başına (IP bazlı fallback ile)
#   /api/media/* → admin panel medya yönetimi (IP bazlı)
#   /api/tokens/ → token CRUD (IP bazlı)
#   /api/sunucu/ → sunucu dosya yönetimi (IP bazlı)
#   /api/istatistik/ → istatistik endpoint'leri (IP bazlı)
#   /api/duyuru/ → duyuru yönetimi (IP bazlı)
#   /login (POST) → admin login (IP bazlı, çok sıkı)
#
# Limitler (config.env ile geçersiz kılınabilir):
#   RATE_WINDOW_SEC       — pencere süresi saniye cinsinden       (varsayılan: 60)
#   RATE_LIMIT_LIGHT      — üye hafif endpoint'ler için maksimum  (varsayılan: 120)
#   RATE_LIMIT_HEAVY      — üye ağır endpoint'ler için maksimum   (varsayılan: 30)
#   RATE_LIMIT_ADMIN      — admin endpoint'ler için maksimum      (varsayılan: 60)
#   RATE_LIMIT_ADMIN_HEAVY— admin yoğun işlemler için maksimum    (varsayılan: 20)
#   RATE_LIMIT_LOGIN      — login POST için maksimum              (varsayılan: 10)

import time as _time
from collections import defaultdict as _defaultdict
from Backend.fastapi.security.brute_force import get_client_ip as _get_client_ip

_RATE_WINDOW       = int(os.getenv("RATE_WINDOW_SEC",        "60"))
_RATE_LIGHT        = int(os.getenv("RATE_LIMIT_LIGHT",      "120"))
_RATE_HEAVY        = int(os.getenv("RATE_LIMIT_HEAVY",       "30"))
_RATE_ADMIN        = int(os.getenv("RATE_LIMIT_ADMIN",       "60"))
_RATE_ADMIN_HEAVY  = int(os.getenv("RATE_LIMIT_ADMIN_HEAVY", "20"))
_RATE_LOGIN        = int(os.getenv("RATE_LIMIT_LOGIN",        "5"))

# TMDB API anahtarı kullanan veya DB'ye yazan üye endpoint'leri
_HEAVY_PATHS = {
    "/api/uye/tmdb-trailer",
    "/api/uye/imdb-to-tmdb",
    "/api/uye/tmdb-meta",
    "/api/uye/hatirla",
    "/api/uye/film-hatirla",
}

# Sayfa ilk yüklenirken otomatik çekilen, salt-okunur üye endpoint'leri.
# Bunlar rate limit sayacına dahil edilmez; gereksiz 429 hatalarını önler.
_EXEMPT_PATHS = {
    "/api/uye/kullanim",
    "/api/uye/profil",
    "/api/uye/db-boyut",
    "/api/uye/medya",       # Ana katalog listesi — sayfa render için zorunlu
    "/api/uye/tv-detay",    # Dizi poster/kalite overlay — render için zorunlu
    "/api/uye/tmdb",        # Trending/yeni çıkanlar bölümü
}

# Admin panel içinde daha yoğun işlem yapan (DB yazma / dış API) endpoint'ler
_ADMIN_HEAVY_PATHS = {
    "/api/media/requery",
    "/api/admin/clear-cache",
    "/api/admin/clear-analytics",
    "/api/admin/fix-reminders",
    "/api/admin/fix-reminder-status",
    "/api/sunucu/yukle-stream",
    "/api/sunucu/bilgisayardan-yukle",
    "/api/sunucu/metadata",
    "/api/duyuru/hazirla",
}

# Admin rate limiting kapsamındaki path prefix'leri
_ADMIN_RATE_PREFIXES = (
    "/api/admin/",
    "/api/media/",
    "/api/tokens/",
    "/api/tokens",
    "/api/sunucu/",
    "/api/istatistik/",
    "/api/duyuru/",
    "/api/system/",
)

# bucket_key → [timestamp, ...]
_rate_buckets: dict[str, list[float]] = _defaultdict(list)


def _apply_rate_limit(bucket_key: str, limit: int, now: float):
    """
    Sliding window kontrolü. Limit aşılmışsa (retry_after, True) döner,
    aksi hâlde zaman damgasını kaydeder ve (0, False) döner.
    """
    _rate_buckets[bucket_key] = [
        t for t in _rate_buckets[bucket_key] if now - t < _RATE_WINDOW
    ]
    if len(_rate_buckets[bucket_key]) >= limit:
        retry_after = int(_RATE_WINDOW - (now - _rate_buckets[bucket_key][0])) + 1
        return retry_after, True
    _rate_buckets[bucket_key].append(now)
    return 0, False


class MemberApiRateLimiter(BaseHTTPMiddleware):
    """
    Sliding-window rate limiting:
      • /api/uye/*          → user_id bazlı
      • /api/admin/* vb.   → IP bazlı (admin oturumu)
      • POST /login         → IP bazlı (sıkı)
    """
    async def dispatch(self, request, call_next):
        path    = request.url.path
        method  = request.method
        now     = _time.monotonic()

        # ── 1. Admin login — IP bazlı, çok sıkı ────────────────────────────
        if path == "/login" and method == "POST":
            client_ip  = _get_client_ip(request)
            bucket_key = f"login:{client_ip}"
            retry_after, exceeded = _apply_rate_limit(bucket_key, _RATE_LOGIN, now)
            if exceeded:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Çok fazla giriş denemesi. Lütfen bekleyin."},
                    headers={"Retry-After": str(retry_after)},
                )
            return await call_next(request)

        # ── 2. Üye API — user_id bazlı ──────────────────────────────────────
        if path.startswith("/api/uye/"):
            member  = request.session.get("member")
            if not member:
                return await call_next(request)
            user_id = str(member.get("user_id", ""))
            if not user_id:
                return await call_next(request)

            if path in _EXEMPT_PATHS:
                return await call_next(request)

            is_heavy   = path in _HEAVY_PATHS
            limit      = _RATE_HEAVY if is_heavy else _RATE_LIGHT
            bucket_key = f"uye:{user_id}:{path if is_heavy else 'light'}"
            retry_after, exceeded = _apply_rate_limit(bucket_key, limit, now)
            if exceeded:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Çok fazla istek. Lütfen bekleyin."},
                    headers={"Retry-After": str(retry_after)},
                )
            return await call_next(request)

        # ── 3. Admin API — IP bazlı ──────────────────────────────────────────
        is_admin_path = any(path.startswith(p) for p in _ADMIN_RATE_PREFIXES)
        if is_admin_path:
            client_ip  = _get_client_ip(request)
            is_heavy   = path in _ADMIN_HEAVY_PATHS or method in ("POST", "PUT", "DELETE", "PATCH")
            limit      = _RATE_ADMIN_HEAVY if is_heavy else _RATE_ADMIN
            # Ağır işlemler path bazlı sayılır; hafifler IP grubunda toplanır
            bucket_key = f"admin:{client_ip}:{path if is_heavy else 'light'}"
            retry_after, exceeded = _apply_rate_limit(bucket_key, limit, now)
            if exceeded:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Çok fazla istek. Lütfen bekleyin."},
                    headers={"Retry-After": str(retry_after)},
                )
            return await call_next(request)

        return await call_next(request)

app.add_middleware(MemberApiRateLimiter)

# ── CSRF Koruması ─────────────────────────────────────────────────────────────
# State-değiştiren (POST/PUT/DELETE/PATCH) admin & API endpoint'lerinde
# X-CSRF-Token başlığını doğrular. Starlette middleware'leri TERS sırada
# çalıştığından CSRFMiddleware, SessionMiddleware'den sonra eklenmelidir;
# böylece request.session'a erişebilir.
app.add_middleware(CSRFMiddleware)

# ── SessionMiddleware — en dışta çalışması için en son ekleniyor ──────────────
# Starlette/FastAPI'de add_middleware çağrıları TERS sırada yürütülür:
# en son eklenen middleware isteği en önce görür (en dış katman).
# MemberApiRateLimiter ve SmartCORSMiddleware dispatch içinde
# request.session'a eriştiğinden, SessionMiddleware onların dışında
# (daha önce) çalışmalıdır → en son add_middleware çağrısı olmalıdır.
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_key,
    https_only=_https_only,
    same_site="lax",
    max_age=259200,     # 72 saatlik oturum
)

try:
    app.mount("/static", StaticFiles(directory="Backend/fastapi/static"), name="static")
except Exception:
    pass

@app.on_event("startup")
async def _startup():
    import asyncio

    # NOT: Bot yeniden başladığında admin ve üye oturumları geçersiz kılınmaz.
    # Admin şifresi yalnızca OWNER /start attığında yenilenir.
    asyncio.create_task(decay_client_failures())

    # ── Sana Özel cache temizleyici (TTL'i dolmuş RAM girişlerini sil) ──
    from Backend.fastapi.routes.stremio_routes import _similar_cache_cleanup_loop
    asyncio.create_task(_similar_cache_cleanup_loop())

    # ── DB yedekleme + platform kataloğu zamanlayıcısı ──
    from Backend.config import Telegram as _TG
    mongo_uri = _TG.DATABASE[0] if _TG.DATABASE else ""
    if mongo_uri:
        start_scheduler(mongo_uri)

    # ── Periyodik IP ban cleanup (süresi dolmuş kayıtları DB'den temizle) ──
    async def _ip_ban_cleanup_loop():
        from Backend.fastapi.security.brute_force import cleanup_expired_bans
        while True:
            await asyncio.sleep(600)   # 10 dakikada bir temizle (ban süresiyle uyumlu)
            await cleanup_expired_bans()
    asyncio.create_task(_ip_ban_cleanup_loop())

    # NOT: cleanup_local_path_records() __main__.py üzerinden başlatılıyor.
    # Burada tekrar çağrılmaz; zipmodu.py artık kullanılmamaktadır.

    # ── Sunucu dosyası kontrolü: fiziksel dosya yoksa DB'den sil ──
    from Backend.helper.sunucu_file_checker import check_and_clean_missing_sunucu_files
    asyncio.create_task(check_and_clean_missing_sunucu_files())

    # ── Yayın zamanlayıcısı (zamanlanmış yayınları otomatik başlat/durdur) ──
    start_yayin_scheduler()
    # ── Stream token periyodik temizliği ──
    from Backend.helper.stream_token import media_token_manager as _mtm
    await _mtm.start_cleanup_task()

@app.on_event("shutdown")
async def _shutdown():
    stop_scheduler()
    stop_yayin_scheduler()
    from Backend.helper.stream_token import media_token_manager as _mtm
    await _mtm.stop_cleanup_task()


# --- Include existing API routers ---
app.include_router(stream_router)
app.include_router(stremio_router)
app.include_router(admin_catalog_router)
app.include_router(yayin_router)
app.include_router(subtitle_router)

# --- Public Routes (No Authentication Required) ---
@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return await login_page(request)

@app.post("/login", response_class=HTMLResponse)
async def login_post_route(
    request:          Request,
    username:         str = Form(...),
    password:         str = Form(...),
    captcha_selected: str = Form(""),
    captcha_token:    str = Form(""),
):
    return await login_post(request, username, password, captcha_selected, captcha_token)

@app.get("/logout")
async def logout_route(request: Request):
    return await logout(request)

# ── CSRF Token endpoint'i ─────────────────────────────────────────────────────
# Oturum açmış admin veya üye, sayfa yüklendiğinde bu endpoint'i çağırarak
# CSRF token'ını alır; ardından state-değiştiren tüm fetch isteklerine
# X-CSRF-Token başlığı olarak ekler.
@app.get("/api/csrf-token")
async def csrf_token_endpoint(request: Request):
    # Oturum yoksa boş token dön — auth katmanı zaten 401 verir
    if not request.session.get("authenticated") and not request.session.get("member"):
        return JSONResponse({"token": ""})
    token = ensure_csrf_secret(request)
    return JSONResponse({"token": token})

@app.post("/set-theme")
async def set_theme_route(request: Request, theme: str = Form(...)):
    return await set_theme(request, theme)

# ── Üye (Abone) Portalı ──────────────────────────────────────────────────────
@app.get("/uye/giris", response_class=HTMLResponse)
async def uye_giris_get(request: Request):
    return await member_login_page(request)

@app.post("/uye/giris", response_class=HTMLResponse)
async def uye_giris_post(
    request:          Request,
    username:         str = Form(...),
    password:         str = Form(...),
    lang:             str = Form("tr"),
    captcha_selected: str = Form(""),
    captcha_token:    str = Form(""),
):
    return await member_login_post(request, username, password, lang, captcha_selected, captcha_token)

@app.get("/uye/cikis")
async def uye_cikis(request: Request):
    return await member_logout(request)

@app.get("/uye/katalog", response_class=HTMLResponse)
async def uye_katalog(request: Request):
    return await member_catalog_page(request)

@app.get("/uye/hatirlatmalar", response_class=HTMLResponse)
async def uye_hatirlatmalar(request: Request):
    return await member_hatirlatmalar_page(request)

@app.get("/api/uye/medya")
async def uye_medya_api(
    request:    Request,
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page:       int = Query(1, ge=1),
    page_size:  int = Query(24, ge=1, le=50),
    search:     str = Query("", max_length=100),
    lang:       str = Query("tr"),
    sort:       str = Query("newest", max_length=32),
    genre:      str = Query("", max_length=64),
    year:       str = Query("", max_length=8),
    cast_name:  str = Query("", max_length=100),
    platform:   str = Query("", max_length=32),
):
    return await member_media_api(request, media_type, page, page_size, search, lang, sort, genre, year, cast_name, platform)

@app.get("/api/uye/tv-detay")
async def uye_tv_detay(
    request:  Request,
    tmdb_id:  int = Query(...),
    db_index: int = Query(-1),   # -1 → tüm shardlarda ara
    lang:     str = Query("tr"),
):
    return await member_tv_detail_api(request, tmdb_id, db_index, lang)

@app.get("/api/uye/stream-url")
async def uye_stream_url(
    request:  Request,
    file_id:  str = Query(..., max_length=2048),
    filename: str = Query("video.mkv", max_length=512),
):
    return await member_stream_url_api(request, file_id, filename)

@app.get("/api/uye/kullanim")
async def uye_kullanim(request: Request):
    return await member_usage_api(request)

@app.get("/api/uye/db-boyut")
async def uye_db_boyut(request: Request):
    return await member_db_size_api(request)

@app.get("/api/uye/profil")
async def uye_profil(request: Request):
    return await member_profile_api(request)


@app.post("/api/uye/hatirla")
async def uye_hatirla(request: Request):
    return await toggle_reminder(request)

@app.get("/api/uye/hatirla/durum")
async def uye_hatirla_durum(
    request: Request,
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
):
    return await reminder_status(request, tmdb_id, db_index)

@app.get("/api/uye/hatirlatmalarim")
async def uye_hatirlatmalarim(request: Request):
    return await my_reminders(request)

@app.post("/api/uye/film-hatirla")
async def uye_film_hatirla(request: Request):
    return await toggle_movie_reminder(request)

@app.get("/api/uye/film-hatirla/durum")
async def uye_film_hatirla_durum(
    request: Request,
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
):
    return await movie_reminder_status(request, tmdb_id, db_index)

@app.get("/api/uye/film-hatirlatmalarim")
async def uye_film_hatirlatmalarim(request: Request):
    return await my_movie_reminders(request)

@app.post("/api/uye/icerik-iste")
async def uye_icerik_iste(request: Request):
    return await submit_content_request(request)

@app.get("/api/uye/isteklerim")
async def uye_isteklerim(request: Request):
    return await my_content_requests(request)

@app.get("/api/uye/tmdb")
async def uye_tmdb(request: Request, kind: str = Query("trending", regex="^(trending|new)$")):
    from Backend.fastapi.routes.member_routes import _get_member
    member = _get_member(request)
    if not member:
        from fastapi import HTTPException
        raise HTTPException(status_code=401)
    from Backend.helper.tmdb_catalog import tmdb_catalog
    if kind == "new":
        items = tmdb_catalog.get_new_releases()
    else:
        items = tmdb_catalog.get_trending()
    return {"items": items, "loaded": tmdb_catalog.is_loaded()}

@app.get("/api/uye/tmdb-trailer")
async def uye_tmdb_trailer(
    request: Request,
    tmdb_id: int = Query(...),
    type: str = Query("movie"),
    lang: str = Query("tr"),
):
    from Backend.fastapi.routes.member_routes import _get_member
    member = _get_member(request)
    if not member:
        from fastapi import HTTPException
        raise HTTPException(status_code=401)
    from Backend.config import Telegram
    import httpx
    api_key = Telegram.TMDB_API
    if not api_key:
        return {"video_id": None}
    media_type = "tv" if type == "tv" else "movie"
    # Önce kullanıcının diliyle dene, bulamazsa İngilizce'ye düş
    for attempt_lang in [lang, "en"]:
        try:
            url = (
                f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/videos"
                f"?api_key={api_key}&language={attempt_lang}-{attempt_lang.upper()}"
            )
            with httpx.Client(timeout=8) as c:
                r = c.get(url)
            if not r.is_success:
                continue
            videos = r.json().get("results", [])
            # Önce resmi fragman, yoksa ilk YouTube videosu
            yt = next(
                (v for v in videos if v.get("site") == "YouTube" and v.get("type") == "Trailer"),
                None,
            ) or next(
                (v for v in videos if v.get("site") == "YouTube"),
                None,
            )
            if yt:
                return {"video_id": yt["key"], "title": yt.get("name", "")}
        except Exception:
            continue
    return {"video_id": None}

# ── GET /api/uye/imdb-to-tmdb ────────────────────────────────────────────────
@app.get("/api/uye/imdb-to-tmdb")
async def uye_imdb_to_tmdb(
    request: Request,
    imdb_id: str = Query(...),
):
    """
    IMDB ID'sini (tt1234567) TMDB ID'sine ve media_type'a çevirir.
    Döner: { "tmdb_id": int, "media_type": "movie"|"tv" }
    """
    from Backend.fastapi.routes.member_routes import _get_member
    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=401, detail="Oturum açılmamış")

    from Backend.config import Telegram
    import httpx

    api_key = Telegram.TMDB_API
    if not api_key:
        raise HTTPException(status_code=503, detail="TMDB API anahtarı yapılandırılmamış")

    url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={api_key}&external_source=imdb_id"
    try:
        with httpx.Client(timeout=8) as c:
            r = c.get(url)
        if not r.is_success:
            raise HTTPException(status_code=502, detail="TMDB API hatası")
        data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TMDB bağlantı hatası: {e}")

    movie_results = data.get("movie_results") or []
    tv_results    = data.get("tv_results") or []

    if movie_results:
        return {"tmdb_id": movie_results[0]["id"], "media_type": "movie"}
    if tv_results:
        return {"tmdb_id": tv_results[0]["id"], "media_type": "tv"}

    raise HTTPException(status_code=404, detail="IMDB ID ile eşleşen içerik bulunamadı")


# ── GET /api/uye/tmdb-meta ───────────────────────────────────────────────────
@app.get("/api/uye/tmdb-meta")
async def uye_tmdb_meta(
    request: Request,
    tmdb_id:    int  = Query(...),
    type:       str  = Query(None),
    media_type: str  = Query(None),
):
    """
    TMDB ID + type/media_type (movie|tv) → başlık, poster ve db_index döner.
    Önce kendi storage shard DB'lerinde arar (db_index için), bulamazsa TMDB API'den çeker.
    Döner: { "tmdb_id": int, "media_type": str, "title": str, "poster": str, "db_index": int, "status": str }
    """
    from Backend.fastapi.routes.member_routes import _get_member
    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=401, detail="Oturum açılmamış")

    from Backend.config import Telegram
    from Backend import db as _db
    import httpx

    # type veya media_type parametrelerinden birini kabul et
    raw_type     = type or media_type or "movie"
    media_type_v = "tv" if raw_type == "tv" else "movie"
    col_name     = "tv_shows" if media_type_v == "tv" else "movies"
    title        = ""
    poster       = ""
    db_index     = 0
    status       = ""

    # Kendi storage shard'larında ara
    try:
        storage_keys = sorted(
            k for k in _db.dbs if k.startswith("storage_")
        )
        for shard_key in storage_keys:
            try:
                doc = await _db.dbs[shard_key][col_name].find_one(
                    {"tmdb_id": tmdb_id},
                    {"_id": 0, "title": 1, "name": 1, "poster": 1, "db_index": 1, "status": 1}
                )
                if doc:
                    title    = doc.get("title") or doc.get("name") or ""
                    poster   = doc.get("poster") or ""
                    db_index = doc.get("db_index", 0)
                    status   = doc.get("status") or ""
                    break
            except Exception:
                continue
    except Exception:
        pass

    # Bulunamadıysa TMDB API'den başlık/poster/status çek
    if not title:
        api_key = Telegram.TMDB_API
        if api_key:
            try:
                url = (
                    f"https://api.themoviedb.org/3/{media_type_v}/{tmdb_id}"
                    f"?api_key={api_key}&language=tr-TR"
                )
                with httpx.Client(timeout=8) as c:
                    r = c.get(url)
                if r.is_success:
                    meta        = r.json()
                    title       = meta.get("title") or meta.get("name") or ""
                    poster_path = meta.get("poster_path") or ""
                    if poster_path:
                        poster = f"https://image.tmdb.org/t/p/w300{poster_path}"
                    if not status and media_type_v == "tv":
                        status = meta.get("status") or ""
            except Exception:
                pass

    return {
        "tmdb_id":    tmdb_id,
        "media_type": media_type_v,
        "title":      title,
        "poster":     poster,
        "db_index":   db_index,
        "status":     status,
    }

# ── GET /api/admin/fix-reminders ────────────────────────────────────────────
@app.get("/api/admin/fix-reminders")
async def admin_fix_reminders(_: bool = Depends(require_auth)):
    """
    MongoDB'deki eski hatırlatma kayıtlarını düzeltir.
    Eski kod {tmdb_id, db_index} ile insert ediyordu; aynı tmdb_id için birden fazla
    kayıt oluşmuş olabilir veya kullanıcı yanlış kayda yazılmış olabilir.
    Bu endpoint tüm kayıtları tmdb_id bazında birleştirir (user_ids union).
    """
    from Backend import db as _db
    results = {}

    for col_name, col_label in [("tv_reminders", "tv"), ("movie_reminders", "movie")]:
        col = _db.dbs["tracking"][col_name]
        all_docs = await col.find({}).to_list(length=10000)

        # tmdb_id başına gruplama
        from collections import defaultdict
        groups = defaultdict(list)
        for doc in all_docs:
            tid = doc.get("tmdb_id")
            if tid is not None:
                groups[tid].append(doc)

        merged = 0
        for tmdb_id, docs in groups.items():
            if len(docs) <= 1:
                continue  # Sorun yok

            # Tüm user_ids'leri birleştir
            all_user_ids = set()
            latest = docs[0]
            for doc in docs:
                all_user_ids.update(doc.get("user_ids") or [])
                if doc.get("db_index", 0) > latest.get("db_index", 0):
                    latest = doc

            # En yüksek db_index'li kaydı güncelle
            await col.update_one(
                {"_id": latest["_id"]},
                {"$set": {"user_ids": list(all_user_ids)}},
            )
            # Diğerlerini sil
            other_ids = [d["_id"] for d in docs if d["_id"] != latest["_id"]]
            if other_ids:
                await col.delete_many({"_id": {"$in": other_ids}})
            merged += 1

        results[col_label] = {
            "total_records": len(all_docs),
            "merged_groups": merged,
        }

    return {"status": "ok", "results": results}


# ── GET /api/admin/fix-reminder-status ───────────────────────────────────────
@app.get("/api/admin/fix-reminder-status")
async def admin_fix_reminder_status(_: bool = Depends(require_auth)):
    """
    MongoDB'deki mevcut hatırlatma kayıtlarına status alanını backfill eder.
    Storage shard'larına bakar, bulamazsa TMDB API'ye düşer.
    """
    from Backend import db as _db
    from Backend.config import Telegram
    import httpx

    tmdb_key = Telegram.TMDB_API or None
    results = {"tv": {"updated": 0, "skipped": 0},
               "movie": {"updated": 0, "skipped": 0}}

    for col_name, col_label, media_type in [
        ("tv_reminders", "tv", "tv"),
        ("movie_reminders", "movie", "movie"),
    ]:
        col = _db.dbs["tracking"][col_name]
        docs = await col.find(
            {"$or": [{"status": {"$exists": False}}, {"status": ""}]}
        ).to_list(length=10000)

        for doc in docs:
            tmdb_id = doc.get("tmdb_id")
            if not tmdb_id:
                results[col_label]["skipped"] += 1
                continue

            status = ""

            # 1) Storage shard'larından ara
            for shard_name, shard_db in _db.dbs.items():
                if shard_name in ("tracking", "users"):
                    continue
                coll_key = "movies" if media_type == "movie" else "tv_shows"
                try:
                    shard_doc = await shard_db[coll_key].find_one(
                        {"tmdb_id": tmdb_id},
                        {"_id": 0, "status": 1},
                    )
                    if shard_doc and shard_doc.get("status"):
                        status = shard_doc["status"]
                        break
                except Exception:
                    continue

            # 2) TMDB API fallback (sadece TV için)
            if not status and tmdb_key and media_type == "tv":
                try:
                    async with httpx.AsyncClient(timeout=8) as client:
                        r = await client.get(
                            f"https://api.themoviedb.org/3/tv/{tmdb_id}",
                            params={"api_key": tmdb_key},
                        )
                        if r.status_code == 200:
                            status = r.json().get("status", "")
                except Exception:
                    pass

            if status:
                await col.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"status": status}},
                )
                results[col_label]["updated"] += 1
            else:
                results[col_label]["skipped"] += 1

    return {"status": "ok", "results": results}

# ─────────────────────────────────────────────────────────────────────────────

# --- Protected Routes (Authentication Required) ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    from Backend.fastapi.security.credentials import is_authenticated
    if not is_authenticated(request):
        return RedirectResponse(url="/uye/giris", status_code=302)
    return await dashboard_page(request, True)

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, _: bool = Depends(require_auth)):
    return await admin_dashboard_page(request, _)

@app.get("/admin/kataloglar", response_class=HTMLResponse)
async def admin_kataloglar(request: Request, _: bool = Depends(require_auth)):
    return await kataloglar_page(request, _)

@app.get("/admin/araclar", response_class=HTMLResponse)
async def admin_araclar(request: Request, _: bool = Depends(require_auth)):
    return await araclar_page(request, _)

@app.get("/api/araclar/aynivideolarisil/status")
async def araclar_ayni_status(_: bool = Depends(require_auth)):
    return await ayni_status_api()

@app.post("/api/araclar/aynivideolarisil/start")
async def araclar_ayni_start(_: bool = Depends(require_auth)):
    return await ayni_start_api()

@app.get("/api/araclar/iceriksil/status")
async def araclar_iceriksil_status(_: bool = Depends(require_auth)):
    return await iceriksil_status_api()

@app.post("/api/araclar/iceriksil/start")
async def araclar_iceriksil_start(payload: dict, _: bool = Depends(require_auth)):
    return await iceriksil_start_api(payload)

@app.get("/api/araclar/tara/status")
async def araclar_tara_status(_: bool = Depends(require_auth)):
    return await tara_status_api()

@app.post("/api/araclar/tara/start")
async def araclar_tara_start(payload: dict, _: bool = Depends(require_auth)):
    return await tara_start_api(payload)

@app.post("/api/araclar/tara/iptal")
async def araclar_tara_iptal(_: bool = Depends(require_auth)):
    return await tara_iptal_api()

@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request, _: bool = Depends(require_auth)):
    return await settings_page(request, _)

@app.get("/api/admin/settings")
async def get_settings(_: bool = Depends(require_auth)):
    return await get_settings_api()

@app.put("/api/admin/settings")
async def update_settings(payload: dict, _: bool = Depends(require_auth)):
    return await update_settings_api(payload)

@app.get("/api/admin/settings/backup")
async def export_settings_backup(_: bool = Depends(require_auth)):
    return await export_settings_backup_api()

@app.post("/api/admin/settings/backup/import")
async def import_settings_backup(payload: dict, _: bool = Depends(require_auth)):
    return await import_settings_backup_api(payload)

@app.post("/api/admin/settings/invalidate-sessions")
async def invalidate_admin_sessions(_: bool = Depends(require_auth)):
    return await invalidate_admin_sessions_api()

@app.get("/api/admin/stats")
async def admin_db_stats(_: bool = Depends(require_auth)):
    return await get_db_stats_api()

@app.get("/api/admin/logs")
async def admin_logs(lines: int = Query(300, ge=1, le=2000), _: bool = Depends(require_auth)):
    return await get_logs_api(lines)

@app.get("/api/admin/logs/download")
async def admin_logs_download(_: bool = Depends(require_auth)):
    return await download_logs_api()

@app.post("/api/admin/restart")
async def admin_restart(_: bool = Depends(require_auth)):
    return await restart_bot_api()

@app.get("/media/manage", response_class=HTMLResponse)
async def media_management(request: Request, media_type: str = "movie", _: bool = Depends(require_auth)):
    return await media_management_page(request, media_type, _)

@app.get("/api/manual-add/status")
async def manual_add_status(_: bool = Depends(require_auth)):
    return await manual_add_status_api()

@app.post("/api/manual-add/start")
async def manual_add_start(payload: dict, _: bool = Depends(require_auth)):
    return await manual_add_start_api(payload)

@app.post("/api/manual-add/stop")
async def manual_add_stop(_: bool = Depends(require_auth)):
    return await manual_add_stop_api()

@app.post("/api/manual-add/set-season")
async def manual_add_set_season(payload: dict, _: bool = Depends(require_auth)):
    return await manual_add_set_season_api(payload)

@app.post("/api/manual-add/set-next-episode")
async def manual_add_set_next_episode(payload: dict, _: bool = Depends(require_auth)):
    return await manual_add_set_next_episode_api(payload)

@app.get("/api/attach-mode/status")
async def attach_mode_status(_: bool = Depends(require_auth)):
    return await attach_mode_status_api()

@app.post("/api/attach-mode/start")
async def attach_mode_start(payload: dict, _: bool = Depends(require_auth)):
    return await attach_mode_start_api(payload)

@app.post("/api/attach-mode/stop")
async def attach_mode_stop(_: bool = Depends(require_auth)):
    return await attach_mode_stop_api()

@app.post("/api/attach-mode/set-season")
async def attach_mode_set_season(payload: dict, _: bool = Depends(require_auth)):
    return await attach_mode_set_season_api(payload)

@app.post("/api/attach-mode/set-next-episode")
async def attach_mode_set_next_episode(payload: dict, _: bool = Depends(require_auth)):
    return await attach_mode_set_next_episode_api(payload)

@app.get("/media/edit", response_class=HTMLResponse)
async def edit_media(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await edit_media_page(request, tmdb_id, db_index, media_type, _)

@app.get("/api/media/list")
async def list_media(
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: str = Query("", max_length=100),
    _: bool = Depends(require_auth)
):
    return await list_media_api(media_type, page, page_size, search)

@app.delete("/api/media/delete")
async def delete_media(tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await delete_media_api(tmdb_id, db_index, media_type)

@app.put("/api/media/update")
async def update_media(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await update_media_api(request, tmdb_id, db_index, media_type)

@app.post("/api/media/requery")
async def requery_media(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await requery_media_api(request, tmdb_id, db_index, media_type)

@app.delete("/api/media/delete-quality")
async def delete_movie_quality(tmdb_id: int, db_index: int, id: str, _: bool = Depends(require_auth)):
    return await delete_movie_quality_api(tmdb_id, db_index, id)

@app.delete("/api/media/delete-tv-quality")
async def delete_tv_quality(tmdb_id: int, db_index: int, season: int, episode: int, id: str, _: bool = Depends(require_auth)):
    return await delete_tv_quality_api(tmdb_id, db_index, season, episode, id)

@app.delete("/api/media/delete-tv-episode")
async def delete_tv_episode(tmdb_id: int, db_index: int, season: int, episode: int, _: bool = Depends(require_auth)):
    return await delete_tv_episode_api(tmdb_id, db_index, season, episode)

@app.delete("/api/media/delete-tv-season")
async def delete_tv_season(tmdb_id: int, db_index: int, season: int, _: bool = Depends(require_auth)):
    return await delete_tv_season_api(tmdb_id, db_index, season)

@app.put("/api/media/rename-quality")
async def rename_movie_quality(request: Request, tmdb_id: int, db_index: int, id: str, _: bool = Depends(require_auth)):
    return await rename_movie_quality_api(request, tmdb_id, db_index, id)

@app.put("/api/media/rename-tv-quality")
async def rename_tv_quality(request: Request, tmdb_id: int, db_index: int, season: int, episode: int, id: str, _: bool = Depends(require_auth)):
    return await rename_tv_quality_api(request, tmdb_id, db_index, season, episode, id)

@app.get("/api/system/workloads")
async def get_workloads(_: bool = Depends(require_auth)):
    try:
        from Backend.pyrofork.bot import work_loads
        return {
            "loads": {
                f"bot{c + 1}": l
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            } if work_loads else {}
        }
    except Exception as e:
        return {"loads": {}}

@app.post("/api/tokens")
async def create_token(payload: dict, _: bool = Depends(require_auth)):
    return await create_token_api(payload)

@app.put("/api/tokens/{token}")
async def update_token(token: str, payload: dict, _: bool = Depends(require_auth)):
    return await update_token_limits_api(token, payload)

@app.delete("/api/tokens/{token}")
async def revoke_token(token: str, delete_subscription: bool = False, user_id: int = None, _: bool = Depends(require_auth)):
    return await revoke_token_api(token, delete_subscription=delete_subscription, user_id=user_id)

@app.get("/api/system/stats")
async def get_system_stats(_: bool = Depends(require_auth)):
    from Backend.fastapi.routes.api_routes import get_system_stats_api
    return await get_system_stats_api()

@app.get("/api/admin/system-stats")
async def admin_system_stats(_: bool = Depends(require_auth)):
    return await get_admin_stats_api()

@app.post("/api/admin/clear-cache")
async def clear_cache(_: bool = Depends(require_auth)):
    return await clear_cache_api()

@app.get("/api/admin/dead-links")
async def get_dead_links(_: bool = Depends(require_auth)):
    return await get_dead_links_api()

@app.get("/api/admin/stream-analytics")
async def get_stream_analytics(_: bool = Depends(require_auth)):
    return await get_stream_analytics_api()

@app.post("/api/admin/clear-analytics")
async def clear_analytics(_: bool = Depends(require_auth)):
    return await clear_analytics_api()

@app.get("/admin/subscriptions", response_class=HTMLResponse)
async def admin_subscriptions(request: Request, _: bool = Depends(require_auth)):
    return await admin_subscriptions_page(request, _)

@app.get("/api/admin/subscriptions/plans")
async def get_subscription_plans(_: bool = Depends(require_auth)):
    return await get_subscription_plans_api()

@app.post("/api/admin/subscriptions/plans")
async def add_subscription_plan(payload: dict, _: bool = Depends(require_auth)):
    return await add_subscription_plan_api(payload)

@app.put("/api/admin/subscriptions/plans/{plan_id}")
async def update_subscription_plan(plan_id: str, payload: dict, _: bool = Depends(require_auth)):
    return await update_subscription_plan_api(plan_id, payload)

@app.delete("/api/admin/subscriptions/plans/{plan_id}")
async def delete_subscription_plan(plan_id: str, _: bool = Depends(require_auth)):
    return await delete_subscription_plan_api(plan_id)

# ---------------------------------------------------------------------------
# Ek Paketler (Addon Packages) API Routes
# ---------------------------------------------------------------------------
@app.get("/api/admin/addon-packages")
async def get_addon_packages(_: bool = Depends(require_auth)):
    from Backend import db as _db
    try:
        packages = await _db.get_addon_packages()
        return {"status": "success", "data": packages}
    except Exception:
        raise HTTPException(status_code=500, detail="Sunucu hatası")

@app.post("/api/admin/addon-packages")
async def add_addon_package(payload: dict, _: bool = Depends(require_auth)):
    from Backend import db as _db
    try:
        label = str(payload.get("label", "")).strip()
        price = float(payload.get("price", 0) or 0)
        extra_days = int(payload.get("extra_days", 0) or 0)
        extra_daily_gb = float(payload.get("extra_daily_gb", 0) or 0)
        extra_monthly_gb = float(payload.get("extra_monthly_gb", 0) or 0)
        extra_speed_mbps = float(payload.get("extra_speed_mbps", 0) or 0)
        extra_requests = int(payload.get("extra_requests", 0) or 0)
        if not label:
            raise HTTPException(status_code=400, detail="Paket adı gereklidir")
        pkg_id = await _db.add_addon_package(label, price, extra_days, extra_daily_gb, extra_monthly_gb, extra_speed_mbps, extra_requests)
        if pkg_id:
            return {"status": "success", "pkg_id": pkg_id}
        raise HTTPException(status_code=500, detail="Eklenemedi")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Sunucu hatası")

@app.put("/api/admin/addon-packages/{pkg_id}")
async def update_addon_package(pkg_id: str, payload: dict, _: bool = Depends(require_auth)):
    from Backend import db as _db
    try:
        label = str(payload.get("label", "")).strip()
        price = float(payload.get("price", 0) or 0)
        extra_days = int(payload.get("extra_days", 0) or 0)
        extra_daily_gb = float(payload.get("extra_daily_gb", 0) or 0)
        extra_monthly_gb = float(payload.get("extra_monthly_gb", 0) or 0)
        extra_speed_mbps = float(payload.get("extra_speed_mbps", 0) or 0)
        extra_requests = int(payload.get("extra_requests", 0) or 0)
        if not label:
            raise HTTPException(status_code=400, detail="Paket adı gereklidir")
        success = await _db.update_addon_package(pkg_id, label, price, extra_days, extra_daily_gb, extra_monthly_gb, extra_speed_mbps, extra_requests)
        if success:
            return {"status": "success"}
        raise HTTPException(status_code=404, detail="Paket bulunamadı")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Sunucu hatası")

@app.delete("/api/admin/addon-packages/{pkg_id}")
async def delete_addon_package(pkg_id: str, _: bool = Depends(require_auth)):
    from Backend import db as _db
    try:
        success = await _db.delete_addon_package(pkg_id)
        if success:
            return {"status": "success"}
        raise HTTPException(status_code=404, detail="Paket bulunamadı")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Sunucu hatası")

@app.get("/api/admin/subscriptions/users")
async def get_subscribers(_: bool = Depends(require_auth)):
    return await get_all_subscribers_api()

@app.post("/api/admin/subscriptions/users/{user_id}/manage")
async def manage_subscriber(user_id: int, payload: dict, _: bool = Depends(require_auth)):
    return await manage_subscriber_api(user_id, payload)

# --- Access Management ---
@app.get("/admin/access", response_class=HTMLResponse)
async def admin_access(request: Request, _: bool = Depends(require_auth)):
    return await admin_access_page(request, _)

# ── Üyeler Sayfası ────────────────────────────────────────────────────────────
@app.get("/admin/uyeler", response_class=HTMLResponse)
async def admin_uyeler(request: Request, _: bool = Depends(require_auth)):
    return await admin_uyeler_page(request)

@app.get("/admin/uyeler/{member_id}", response_class=HTMLResponse)
async def admin_uye_detay(member_id: str, request: Request, _: bool = Depends(require_auth)):
    return await admin_uye_detay_page(request, member_id)

@app.get("/api/admin/uyeler")
async def admin_uyeler_api(_: bool = Depends(require_auth)):
    return await admin_uyeler_list_api()

@app.get("/api/admin/uyeler/{member_id}/streams")
async def admin_uye_streams_api(member_id: str, _: bool = Depends(require_auth)):
    return await admin_uye_stream_history_api(member_id)

@app.get("/api/admin/uyeler/{member_id}/reminders")
async def admin_uye_reminders(member_id: str, _: bool = Depends(require_auth)):
    return await admin_uye_reminders_api(member_id)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/istatistik", response_class=HTMLResponse)
async def istatistik(request: Request, _: bool = Depends(require_auth)):
    return await istatistik_page(request, _)

# ── İstekler Sayfası (içerik talepleri onay/red) ──────────────────────────────
@app.get("/istekler", response_class=HTMLResponse)
async def istekler(request: Request, _: bool = Depends(require_auth)):
    return await istekler_page(request, _)

@app.get("/api/admin/istekler")
async def admin_istekler_api(_: bool = Depends(require_auth)):
    return await admin_list_content_requests()

@app.post("/api/admin/istekler/aksiyon")
async def admin_istekler_aksiyon_api(request: Request, _: bool = Depends(require_auth)):
    return await admin_review_content_requests(request)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/istatistik/bandwidth")
async def bandwidth_stats_api(_: bool = Depends(require_auth)):
    from Backend import db as _db
    return await _db.get_bandwidth_stats()

# ── Anlık upload hızı (sunucuya yükleme görevleri) ────────────────────────────
@app.get("/api/istatistik/upload_speed")
async def upload_speed_api(_: bool = Depends(require_auth)):
    """
    Sunucudan disari cikan toplam anlik hizi dondurur:
      - Aktif stream oturumlarinin kullanicilara gonderdigi hiz (ACTIVE_STREAMS)
      - Sunucuya yukleme gorevlerinin Telegram'dan indirdigi hiz (sunucuyayukle)

    istatistik.html sayfasi bu endpoint'i 15 saniyede bir cagırir.
    """
    # 1. Aktif stream oturumlari (kullanicilara gonderilen veri)
    stream_bps = 0
    stream_count = 0
    try:
        from Backend.helper.custom_dl import ACTIVE_STREAMS
        active_streams = [
            s for s in ACTIVE_STREAMS.values()
            if s.get("status") not in ("cancelled", "error", "done", None)
        ]
        stream_count = len(active_streams)
        stream_bps = int(sum(s.get("instant_mbps", 0) for s in active_streams) * 1024 * 1024 / 8)
    except Exception:
        pass

    # 2. Sunucuya yukleme gorevleri
    upload_speed_bps = 0
    upload_tasks = 0
    task_details = []
    try:
        from Backend.pyrofork.plugins.sunucuyayukle import _TASKS_STATE
        active = [t for t in _TASKS_STATE if t.get("status") not in ("Kuyrukta", None)]
        upload_tasks = len(active)
        upload_speed_bps = sum(t.get("speed", 0) for t in active)
        task_details = [
            {
                "fname":  t.get("fname", "?"),
                "status": t.get("status", "?"),
                "speed":  t.get("speed", 0),
                "pct":    round(t.get("pct", 0), 1),
                "engine": t.get("engine", "?"),
            }
            for t in active
        ]
    except Exception:
        pass

    return {
        "total_bps":    stream_bps + upload_speed_bps,
        "stream_bps":   stream_bps,
        "stream_count": stream_count,
        "speed_bps":    upload_speed_bps,
        "upload_tasks": upload_tasks,
        "tasks":        task_details,
    }



# --- Canlı Yayın Sayfası ---
@app.get("/canli", response_class=HTMLResponse)
async def canli(request: Request, _: bool = Depends(require_auth)):
    return await canli_page(request, _)

# --- Link ile İçerik Ekleme Sayfası ---
@app.get("/admin/sunucu", response_class=HTMLResponse)
async def sunucu(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_page(request, _)

@app.get("/api/sunucu/listele")
async def sunucu_listele_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_listele(request, _)

@app.get("/api/sunucu/yukle-stream")
async def sunucu_yukle_stream_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_yukle_stream(request, _)

@app.post("/api/sunucu/bilgisayardan-yukle")
async def sunucu_bilgisayardan_yukle_route(
    request: Request,
    file: UploadFile = File(...),
    dest_path: str = Form(default=""),
    _: bool = Depends(require_auth),
):
    return await sunucu_bilgisayardan_yukle(request, file, dest_path, _)

@app.delete("/api/sunucu/sil")
async def sunucu_sil_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_sil(request, _)

@app.put("/api/sunucu/yeniden-adlandir")
async def sunucu_yeniden_adlandir_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_yeniden_adlandir(request, _)

@app.post("/api/sunucu/metadata")
async def sunucu_metadata_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_metadata(request, _)

@app.post("/api/sunucu/klasor-olustur")
async def sunucu_klasor_olustur_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_klasor_olustur(request, _)

@app.get("/api/sunucu/sistem-durumu")
async def sunucu_sistem_durumu_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_sistem_durumu(request, _)

@app.post("/api/sunucu/metadata-sorgu")
async def sunucu_metadata_sorgu_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_metadata_sorgu(request, _)

@app.post("/api/sunucu/metadata-kaydet")
async def sunucu_metadata_kaydet_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_metadata_kaydet(request, _)

@app.delete("/api/sunucu/metadata-sil")
async def sunucu_metadata_sil_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_metadata_sil(request, _)

@app.get("/api/sunucu/indir")
async def sunucu_indir_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_indir(request, _)

@app.get("/api/sunucu/klasor-zip-baslat")
async def sunucu_klasor_zip_baslat_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_klasor_zip_baslat(request, _)

@app.get("/api/sunucu/klasor-zip-durum")
async def sunucu_klasor_zip_durum_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_klasor_zip_durum(request, _)

@app.get("/api/sunucu/indir-klasor")
async def sunucu_indir_klasor_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_indir_klasor(request, _)

@app.get("/api/sunucu/dosya-durumu")
async def sunucu_dosya_durumu_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_dosya_durumu(request, _)

@app.get("/api/sunucu/gdrive-listele")
async def sunucu_gdrive_listele_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_gdrive_listele(request, _)

@app.post("/api/sunucu/gdrive-ekle")
async def sunucu_gdrive_ekle_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_gdrive_ekle(request, _)

@app.post("/api/sunucu/gdrive-meta-sorgu")
async def sunucu_gdrive_meta_sorgu_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_gdrive_meta_sorgu(request, _)

@app.post("/api/sunucu/gdrive-ekle-onay")
async def sunucu_gdrive_ekle_onay_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_gdrive_ekle_onay(request, _)

@app.get("/api/sunucu/gdrive-db-listele")
async def sunucu_gdrive_db_listele_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_gdrive_db_listele(request, _)

@app.delete("/api/sunucu/gdrive-db-sil")
async def sunucu_gdrive_db_sil_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_gdrive_db_sil(request, _)

@app.post("/api/sunucu/gdrive-migrate")
async def sunucu_gdrive_migrate_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_gdrive_migrate(request, _)

# ── Rclone route'ları ─────────────────────────────────────────────────────────

@app.get("/api/sunucu/rclone-remotes")
async def sunucu_rclone_remotes_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_rclone_remotes(request, _)

@app.get("/api/sunucu/rclone-listele")
async def sunucu_rclone_listele_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_rclone_listele(request, _)

@app.post("/api/sunucu/rclone-meta-sorgu")
async def sunucu_rclone_meta_sorgu_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_rclone_meta_sorgu(request, _)

@app.post("/api/sunucu/rclone-ekle-onay")
async def sunucu_rclone_ekle_onay_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_rclone_ekle_onay(request, _)

@app.get("/api/sunucu/rclone-db-listele")
async def sunucu_rclone_db_listele_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_rclone_db_listele(request, _)

@app.delete("/api/sunucu/rclone-db-sil")
async def sunucu_rclone_db_sil_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_rclone_db_sil(request, _)

@app.post("/api/sunucu/rclone-migrate")
async def sunucu_rclone_migrate_route(request: Request, _: bool = Depends(require_auth)):
    return await sunucu_rclone_migrate(request, _)

@app.get("/link-ekle", response_class=HTMLResponse)
async def link_ekle(request: Request, _: bool = Depends(require_auth)):
    return await link_ekle_page(request, _)

@app.post("/api/link-ekle/query")
async def link_ekle_query_route(request: Request, _: bool = Depends(require_auth)):
    return await link_ekle_query(request, _)

@app.post("/api/link-ekle/save")
async def link_ekle_save_route(request: Request, _: bool = Depends(require_auth)):
    return await link_ekle_save(request, _)

# --- Canlı Yayın API ---
@app.get("/api/live")
async def live_list(_: bool = Depends(require_auth)):
    from Backend import db as _db
    channels = await _db.get_live_channels()
    return {"channels": channels}

@app.post("/api/live")
async def live_add(payload: dict, _: bool = Depends(require_auth)):
    from Backend import db as _db
    ch = await _db.add_live_channel(payload)
    return ch

@app.put("/api/live/{channel_id}")
async def live_update(channel_id: str, payload: dict, _: bool = Depends(require_auth)):
    from Backend import db as _db
    ok = await _db.update_live_channel(channel_id, payload)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Kanal bulunamadı")
    return {"ok": True}

@app.delete("/api/live/{channel_id}")
async def live_delete(channel_id: str, _: bool = Depends(require_auth)):
    from Backend import db as _db
    ok = await _db.delete_live_channel(channel_id)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Kanal bulunamadı")
    return {"ok": True}

@app.get("/api/admin/access/tokens")
async def get_access_tokens(_: bool = Depends(require_auth)):
    return await get_all_tokens_api()

@app.delete("/api/admin/access/tokens/{token}")
async def delete_access_token(token: str, delete_subscription: bool = False, user_id: int = None, _: bool = Depends(require_auth)):
    from Backend.fastapi.routes.api_routes import revoke_token_api as _revoke_token_api
    return await _revoke_token_api(token, delete_subscription=delete_subscription, user_id=user_id)

@app.post("/api/admin/access/users/{user_id}/assign-plan")
async def assign_access_plan(user_id: int, payload: dict, _: bool = Depends(require_auth)):
    days = int(payload.get("days", 0))
    return await assign_plan_api(user_id, days)

@app.patch("/api/admin/access/tokens/{token}/link-user")
async def link_token_to_user(token: str, payload: dict, _: bool = Depends(require_auth)):
    user_id = int(payload.get("user_id", 0))
    if not user_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="user_id is required.")
    return await link_token_user_api(token, user_id)

@app.get("/api/system/speedtest")
async def speed_test(
    quality_id: str = Query(...),
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(...),
    _: bool = Depends(require_auth)
):
    return await speed_test_api(quality_id, tmdb_id, db_index, media_type)

@app.get("/api/system/speedtest/stream")
async def speed_test_stream(
    quality_id: str = Query(...),
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(...),
    _: bool = Depends(require_auth)
):
    return await speed_test_stream_api(quality_id, tmdb_id, db_index, media_type)

@app.exception_handler(401)
async def auth_exception_handler(request: Request, exc):
    # /dl/ ve /stremio/ endpoint'leri için redirect değil JSON döndür
    if request.url.path.startswith(("/dl/", "/stremio/")):
        return JSONResponse({"error": "Geçersiz veya süresi dolmuş token"}, status_code=401)
    return RedirectResponse(url="/login", status_code=302)

@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Sunucu hatası oluştu. Lütfen tekrar deneyin."}, status_code=500)
    return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    from fastapi import HTTPException as _HTTPExc
    from fastapi.exception_handlers import http_exception_handler as _http_exc_handler
    # HTTPException'ları FastAPI'nin kendi handler'ına ilet — yutma
    if isinstance(exc, _HTTPExc):
        return await _http_exc_handler(request, exc)
    import traceback as _tb
    import logging as _log
    _log.getLogger("uvicorn.error").error(
        "Beklenmeyen hata [%s %s]: %s\n%s",
        request.method, request.url.path, exc, _tb.format_exc()
    )
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": f"Beklenmeyen hata: {str(exc)}"}, status_code=500)
    return JSONResponse({"error": "Sunucu hatası"}, status_code=500)
