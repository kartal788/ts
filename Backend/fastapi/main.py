from fastapi import FastAPI, Request, Form, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import secrets
import os
from Backend import __version__
from Backend.fastapi.security.credentials import require_auth
from Backend.fastapi.routes.stream_routes import router as stream_router, decay_client_failures
from Backend.helper.db_scheduler import start_scheduler, stop_scheduler
from Backend.fastapi.routes.stremio_routes import router as stremio_router
from Backend.fastapi.routes.template_routes import (
    login_page, login_post, logout, set_theme, dashboard_page,
    media_management_page, edit_media_page,
    admin_dashboard_page, admin_subscriptions_page, admin_access_page, canli_page,
    link_ekle_page, istatistik_page, sunucu_page
)
from Backend.fastapi.routes.link_ekle_routes import (
    link_ekle_query, link_ekle_save
)
from Backend.fastapi.routes.sunucu_routes import (
    sunucu_yukle_stream, sunucu_listele, sunucu_sil,
    sunucu_yeniden_adlandir, sunucu_metadata, sunucu_klasor_olustur,
    sunucu_sistem_durumu, sunucu_metadata_sorgu, sunucu_metadata_kaydet,
    sunucu_metadata_sil, sunucu_indir, sunucu_indir_klasor,
    sunucu_klasor_zip_baslat, sunucu_klasor_zip_durum,
    sunucu_gdrive_listele, sunucu_gdrive_ekle,
    sunucu_gdrive_meta_sorgu, sunucu_gdrive_ekle_onay,
    sunucu_gdrive_db_listele, sunucu_gdrive_db_sil, sunucu_gdrive_migrate,
)
from Backend.fastapi.routes.member_routes import (
    member_login_page, member_login_post, member_logout,
    member_catalog_page, member_media_api,
    member_tv_detail_api, member_stream_url_api, member_usage_api,
    member_profile_api, member_db_size_api,
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
    get_all_tokens_api, assign_plan_api, link_token_user_api
)
from Backend.fastapi.routes.uyeler_routes import (
    admin_uyeler_page,
    admin_uye_detay_page,
    admin_uyeler_list_api,
    admin_uye_stream_history_api,
)

app = FastAPI(
    title="Telegram Stremio Media Server",
    description="A powerful, self-hosted Telegram Stremio Media Server built with FastAPI, MongoDB, and PyroFork seamlessly integrated with Stremio for automated media streaming and discovery.",
    version=__version__
)

# --- Middleware Setup ---
# Session secret key: .env dosyasından okunur, yoksa güvenli rastgele key üretilir
from Backend.config import Telegram as _TG
import logging as _logging

if not _TG.SESSION_SECRET_KEY:
    _logging.getLogger("uvicorn").warning(
        "[GÜVENLİK] SESSION_SECRET_KEY config.env'de tanımlı değil! "
        "Her yeniden başlatmada oturumlar geçersiz kalacak. "
        "Lütfen config.env'e güçlü bir SESSION_SECRET_KEY ekleyin."
    )
_session_key = _TG.SESSION_SECRET_KEY or secrets.token_hex(32)

# HTTPS kontrolü: BASE_URL https ile başlıyorsa cookie'yi Secure yap
_https_only = _TG.BASE_URL.startswith("https://") if _TG.BASE_URL else False

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_key,
    https_only=_https_only,
    same_site="lax",
    max_age=3600,       # 1 saatlik oturum
)
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
_PUBLIC_CORS_PREFIXES = ("/stremio/", "/dl/")

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

try:
    app.mount("/static", StaticFiles(directory="Backend/fastapi/static"), name="static")
except Exception:
    pass

@app.on_event("startup")
async def _startup():
    import asyncio
    asyncio.create_task(decay_client_failures())

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

@app.on_event("shutdown")
async def _shutdown():
    stop_scheduler()

# --- Include existing API routers ---
app.include_router(stream_router)
app.include_router(stremio_router)

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
# ─────────────────────────────────────────────────────────────────────────────

# --- Protected Routes (Authentication Required) ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request, _: bool = Depends(require_auth)):
    return await dashboard_page(request, _)

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, _: bool = Depends(require_auth)):
    return await admin_dashboard_page(request, _)

@app.get("/media/manage", response_class=HTMLResponse)
async def media_management(request: Request, media_type: str = "movie", _: bool = Depends(require_auth)):
    return await media_management_page(request, media_type, _)

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
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/istatistik", response_class=HTMLResponse)
async def istatistik(request: Request, _: bool = Depends(require_auth)):
    return await istatistik_page(request, _)

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
