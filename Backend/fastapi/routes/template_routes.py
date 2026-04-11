from fastapi import Request, Form, HTTPException, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from Backend.fastapi.security.credentials import verify_credentials, require_auth, is_authenticated, get_current_user
from Backend.fastapi.security.brute_force import is_banned_async, ban_remaining, record_failure_async, record_success, get_client_ip
from Backend.fastapi.security.captcha import set_captcha, verify_captcha, CaptchaData
from Backend.fastapi.themes import get_theme, get_all_themes
from Backend.config import Telegram
from Backend import db
from Backend.pyrofork.bot import work_loads, multi_clients, StreamBot
from Backend.helper.pyro import get_readable_time
from Backend import StartTime, __version__
import time
from datetime import datetime
from Backend.helper.custom_dl import ACTIVE_STREAMS, RECENT_STREAMS

templates = Jinja2Templates(directory="Backend/fastapi/templates")

# Jinja2 filter: Unix timestamp → Türkçe tarih/saat formatı
def _datetimeformat(value):
    try:
        return datetime.fromtimestamp(int(value)).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "—"

templates.env.filters["datetimeformat"] = _datetimeformat


async def _get_owner_name() -> str:
    """OWNER_ID'ye kayıtlı Telegram kullanıcısının adını döner."""
    try:
        owner_doc = await db.get_user(Telegram.OWNER_ID)
        if owner_doc and owner_doc.get("first_name"):
            return owner_doc["first_name"]
    except Exception:
        pass
    return ""

async def admin_dashboard_page(request: Request, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()

    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "owner_name": owner_name,
    })

async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)

    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    captcha = set_captcha(request.session)

    return templates.TemplateResponse("login.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "captcha": captcha,
    })

async def login_post(
    request: Request,
    username:         str = Form(...),
    password:         str = Form(...),
    captcha_selected: str = Form(""),
    captcha_token:    str = Form(""),
):
    ip = get_client_ip(request)

    # ── Brute-force kontrolü ─────────────────────────────────────────────────
    if await is_banned_async(ip):
        remaining = ban_remaining(ip)
        theme_name = request.session.get("theme", "purple_gradient")
        theme = get_theme(theme_name)
        captcha = set_captcha(request.session)
        return templates.TemplateResponse("login.html", {
            "request": request,
            "theme": theme,
            "themes": get_all_themes(),
            "current_theme": theme_name,
            "app_name": Telegram.ISIM,
            "captcha": captcha,
            "error": f"Çok fazla başarısız giriş denemesi. IP adresiniz engellendi. {remaining} saniye bekleyin.",
        })

    # ── CAPTCHA doğrulama ────────────────────────────────────────────────────
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)

    if not verify_captcha(request.session, captcha_selected, captcha_token):
        captcha = set_captcha(request.session)
        return templates.TemplateResponse("login.html", {
            "request": request,
            "theme": theme,
            "themes": get_all_themes(),
            "current_theme": theme_name,
            "app_name": Telegram.ISIM,
            "captcha": captcha,
            "error": "CAPTCHA hatalı. Lütfen tüm resimleri doğru seçin.",
        })

    admin_doc = await verify_credentials(username, password)
    if admin_doc:
        record_success(ip)
        request.session["authenticated"] = True
        request.session["username"]      = username
        # Katalog için member session da aç (photo_url ve display_name ile)
        if not request.session.get("member"):
            request.session["member"] = {
                "user_id":          "admin",
                "name":             admin_doc.get("display_name") or "Yönetici",
                "photo_url":        admin_doc.get("photo_url", ""),
                "token":            None,
                "lang":             "tr",
                "subscription_end": None,
                "is_admin":         True,
            }
        return RedirectResponse(url="/", status_code=302)
    else:
        # Başarısız giriş → kaydet
        newly_banned = await record_failure_async(ip, endpoint="/login")

        theme_name = request.session.get("theme", "purple_gradient")
        theme = get_theme(theme_name)

        if newly_banned:
            error_msg = f"Çok fazla başarısız giriş denemesi. IP adresiniz {Telegram.BRUTE_BAN} saniye boyunca engellendi."
        else:
            error_msg = "Kullanıcı adı veya şifre hatalı."

        captcha = set_captcha(request.session)
        return templates.TemplateResponse("login.html", {
            "request": request,
            "theme": theme,
            "themes": get_all_themes(),
            "current_theme": theme_name,
            "app_name": Telegram.ISIM,
            "captcha": captcha,
            "error": error_msg,
        })

async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

async def set_theme(request: Request, theme: str = Form(...)):
    if theme in get_all_themes():
        request.session["theme"] = theme
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=302)

async def dashboard_page(request: Request, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()
    
    try:
        db_stats = await db.get_database_stats()
        total_movies = sum(stat.get("movie_count", 0) for stat in db_stats)
        total_tv_shows = sum(stat.get("tv_count", 0) for stat in db_stats)

        now = time.time()
        PRUNE_SECONDS = 3
        for sid, info in list(ACTIVE_STREAMS.items()):
            status = info.get("status")
            # Check end_ts first to see when it organically finished
            last_ts = info.get("end_ts") or info.get("last_ts") or info.get("start_ts", now)

            if status in ("cancelled", "error", "finished") and (now - last_ts > PRUNE_SECONDS):

                info["duration"] = round(now - info.get("start_ts", now), 1)
                info["stream_id"] = sid
                try:
                    RECENT_STREAMS.appendleft(info)
                    ACTIVE_STREAMS.pop(sid)
                except KeyError:
                    pass

        active_streams_data = []
        for stream_id, info in ACTIVE_STREAMS.items():
            # Sadece gerçekten aktif ve veri transfer etmiş stream'leri göster
            if info.get("status") != "active":
                continue
            if (info.get("total_bytes") or 0) <= 0:
                continue
            active_streams_data.append({
                "stream_id": stream_id,
                "msg_id": info.get("msg_id"),
                "chat_id": info.get("chat_id"),
                "status": info.get("status", "active"),
                "total_bytes": info.get("total_bytes", 0),
                "avg_mbps": round(info.get("avg_mbps", 0.0), 2),
                "instant_mbps": round(info.get("instant_mbps", 0.0), 2),
                "peak_mbps": round(info.get("peak_mbps", 0.0), 2),
                "client_index": info.get("client_index", 0),
                "dc_id": info.get("dc_id", 0),
                "duration": round(now - info.get("start_ts", now), 1),
                "meta": info.get("meta", {})
            })

        system_stats = {
            "server_status": "running",
            "uptime": get_readable_time(now - StartTime),
            "telegram_bot": f"@{StreamBot.username}" if StreamBot and StreamBot.username else "@StreamBot",
            "connected_bots": len(multi_clients),
            "loads": {
                f"bot{c+1}": l
                for c, (_, l) in enumerate(sorted(work_loads.items(), key=lambda x: x[1], reverse=True))
            } if work_loads else {},
            "version": __version__,
            "movies": total_movies,
            "tv_shows": total_tv_shows,
            "databases": db_stats,
            "total_databases": len(db_stats),
            "current_db_index": db.current_db_index,
            "active_streams": active_streams_data,
            "total_active_streams": len(active_streams_data)
        }

    except Exception as e:
        print(f"Dashboard error: {e}")
        system_stats = {
            "server_status": "error",
            "error": str(e),
            "uptime": "N/A",
            "telegram_bot": "@StreamBot",
            "connected_bots": 0,
            "loads": {},
            "version": __version__,
            "movies": 0,
            "tv_shows": 0,
            "databases": [],
            "total_databases": 0,
            "current_db_index": 1,
            "active_streams": [],
            "total_active_streams": 0
        }

    api_tokens = await db.get_all_api_tokens()
    # BASE_URL config'den alınır; yoksa request.base_url kullanılır (port bilgisi korunur)
    configured_base_url = Telegram.BASE_URL.rstrip("/") + "/" if Telegram.BASE_URL else None
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "owner_name": owner_name,
        "system_stats": system_stats,
        "api_tokens": api_tokens,
        "configured_base_url": configured_base_url,
        "subscription_mode": Telegram.SUBSCRIPTION
    })


async def media_management_page(request: Request, media_type: str = "movie", _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()
    
    return templates.TemplateResponse("media_management.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "owner_name": owner_name,
        "media_type": media_type
    })

async def edit_media_page(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()
    
    try:
        media_details = await db.get_document(media_type, tmdb_id, db_index)
        if not media_details:
            raise HTTPException(status_code=404, detail="Media not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    return templates.TemplateResponse("media_edit.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "tmdb_id": tmdb_id,
        "db_index": db_index,
        "media_type": media_type,
        "media_details": media_details,
        "gecici_token": ""
    })


async def admin_subscriptions_page(request: Request, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()
    
    return templates.TemplateResponse("subscriptions_manage.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "owner_name": owner_name,
    })


async def admin_access_page(request: Request, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()

    return templates.TemplateResponse("access_manage.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "owner_name": owner_name,
    })


async def canli_page(request: Request, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()

    return templates.TemplateResponse("canli.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "owner_name": owner_name,
    })


async def link_ekle_page(request: Request, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()

    return templates.TemplateResponse("link_ekle.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "owner_name": owner_name,
    })


async def sunucu_page(request: Request, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()

    return templates.TemplateResponse("sunucu.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "owner_name": owner_name,
    })


async def istatistik_page(request: Request, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name = await _get_owner_name()

    try:
        bw = await db.get_bandwidth_stats()
    except Exception as e:
        bw = {
            "instant": {"bytes": 0, "active_streams": 0},
            "hourly_bytes": 0, "daily_bytes": 0, "monthly_bytes": 0, "total_bytes": 0,
            "weekly": [], "hourly_24h": [], "top_users": [], "top_content": [], "top_content_week": []
        }

    api_tokens = await db.get_all_api_tokens()

    # ekle.py ile eklenen içerikleri çek (ekle_approved koleksiyonu)
    ekle_items = []
    ekle_total = 0
    try:
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is not None:
            col = storage["ekle_approved"]
            ekle_total = await col.count_documents({})
            cursor = col.find({}).sort("added_at", -1).limit(50)
            ekle_items = await cursor.to_list(length=50)
            # ObjectId'yi string'e çevir (JSON serileştirme için)
            for item in ekle_items:
                item["_id"] = str(item["_id"])
    except Exception:
        pass

    return templates.TemplateResponse("istatistik.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "current_user": current_user,
        "owner_name": owner_name,
        "bw": bw,
        "api_tokens": api_tokens,
        "ekle_items": ekle_items,
        "ekle_total": ekle_total,
    })
