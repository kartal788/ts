"""
member_routes.py
================
Abone (üye) portalı route'ları.

Güvenlik özeti:
  - Giriş: DB'deki SHA-256 hash ile karşılaştırılır, plain-text şifre saklanmaz.
  - Oturum: HttpOnly + SameSite=Lax signed session cookie (Starlette SessionMiddleware).
  - Abonelik: Her korumalı istek öncesinde DB'den canlı olarak kontrol edilir.
  - İndirme linkleri: /dl/{token}/{file_id}/{indir_token}/... formatı —
    indir_token üyeye + dosyaya özgüdür, YENILEME saat sonra otomatik geçersiz olur.
  - Rate-limiting: 5 sn içinde 10'dan fazla başarısız giriş → 60 sn bekleme.
  - Media API: salt-okunur, sadece kendi token'larına ait stream URL üretilir,
    admin endpointlerine erişim yok.
"""

from __future__ import annotations

import asyncio
import logging
_logger = logging.getLogger(__name__)
import hashlib
import pathlib
import re as _re
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from Backend import db
from Backend.config import Telegram
from Backend.fastapi.themes import get_theme, get_all_themes
from Backend.fastapi.security.brute_force import (
    is_banned, is_banned_async, ban_remaining, record_failure, record_failure_async, record_success, get_client_ip
)
from Backend.fastapi.security.csrf import ensure_csrf_secret
from Backend.fastapi.security.captcha import set_captcha, verify_captcha, CaptchaData

templates = Jinja2Templates(directory="Backend/fastapi/templates")

_CONFIG_PATH = pathlib.Path("config.env")

def _get_websitesi() -> bool:
    """config.env'den WEBSITESI değerini runtime'da okur (bot restart gerekmez)."""
    try:
        text = _CONFIG_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        m = _re.search(r'^WEBSITESI\s*=\s*["\']?(.*?)["\']?\s*(?:#.*)?$', text, _re.MULTILINE)
        if m:
            return m.group(1).strip().lower() == "true"
    except Exception:
        pass
    return True  # Bulunamazsa varsayılan: açık

# ── Oturum yardımcıları ───────────────────────────────────────────────────────

def _get_member(request: Request) -> Optional[dict]:
    return request.session.get("member")


def _require_member(request: Request) -> dict:
    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=302, headers={"Location": "/uye/giris"})
    return member


def _is_owner(member: dict) -> bool:
    """Session'daki kullanıcı OWNER_ID mi?"""
    try:
        return int(member.get("user_id", -1)) == Telegram.OWNER_ID
    except (ValueError, TypeError):
        return False


def _check_website_access(member: dict) -> bool:
    """
    WEBSITESI=false iken sadece OWNER_ID erişebilir.
    True → erişim verildi, False → engellendi.
    """
    if _get_websitesi():
        return True          # site açık, herkes girebilir
    return _is_owner(member) # site kapalı, sadece owner


async def _check_subscription(user_id) -> bool:
    """DB'den canlı abonelik kontrolü. Admin (user_id='admin') her zaman True döner."""
    # Admin oturumu — string 'admin' gelirse abonelik kontrolü atla
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        # int'e çevrilemeyen user_id → admin oturumu, erişime izin ver
        return True
    user = await db.get_user(uid)
    if not user:
        return False
    if user.get("subscription_status") == "banned":
        return False
    if user.get("subscription_status") != "active":
        return False
    expiry = user.get("subscription_expiry")
    if not expiry:
        return False
    try:
        now = datetime.now(timezone.utc)
        # expiry datetime nesnesi ise
        if isinstance(expiry, datetime):
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry >= now
        # expiry string ise parse et
        if isinstance(expiry, str):
            expiry_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            return expiry_dt >= now
        # Başka tip (örn. int timestamp)
        return False
    except Exception:
        return False


def _bytes_to_gb(b: int) -> float:
    return round(b / 1_073_741_824, 3)



def _get_country_flag(ip: str) -> str:
    """IP adresinden ülke bayrağı emoji'si döndürür (basit RFC 1918 + geoip fallback)."""
    if not ip or ip in ("127.0.0.1", "::1", "unknown"):
        return ""
    # RFC 1918 private ranges → boş
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback:
            return ""
    except ValueError:
        return ""
    # Basit geoip: socket hostname veya doğrudan IP (production'da gerçek geoip lib kullanılabilir)
    # Şimdilik boş döndür; template'de JS tarafı window.navigator ile zenginleştirilebilir
    return ""

# ── Sayfa: Giriş ─────────────────────────────────────────────────────────────

async def member_login_page(request: Request):
    if _get_member(request):
        return RedirectResponse(url="/uye/katalog", status_code=302)
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    lang  = request.query_params.get("lang", "tr")
    maintenance = not _get_websitesi()
    captcha = set_captcha(request.session)
    return templates.TemplateResponse("member_login.html", {
        "request": request,
        "theme":   theme,
        "themes":  get_all_themes(),
        "current_theme": theme_name,
        "app_name": Telegram.ISIM,
        "lang": lang,
        "error": None,
        "maintenance": maintenance,
        "captcha": captcha,
    })


async def member_login_post(
    request: Request,
    username:         str = Form(...),
    password:         str = Form(...),
    lang:             str = Form("tr"),
    captcha_selected: str = Form(""),
    captcha_token:    str = Form(""),
):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)

    ip = get_client_ip(request)

    def _err(msg: str, maintenance: bool = False):
        return templates.TemplateResponse("member_login.html", {
            "request": request,
            "theme":   theme,
            "themes":  get_all_themes(),
            "current_theme": theme_name,
            "app_name": Telegram.ISIM,
            "lang":  lang,
            "error": msg,
            "maintenance": maintenance,
            "captcha": set_captcha(request.session),
        })

    # ── Brute-force kontrolü ─────────────────────────────────────────────────
    if await is_banned_async(ip):
        remaining = ban_remaining(ip)
        return _err({
            "tr": f"Çok fazla başarısız giriş denemesi. IP adresiniz engellendi. {remaining} saniye bekleyin.",
            "en": f"Too many failed attempts. Your IP is blocked. Try again in {remaining} seconds.",
            "de": f"Zu viele Fehlversuche. Ihre IP ist gesperrt. Warten Sie {remaining} Sekunden.",
        }.get(lang, f"IP blocked. Wait {remaining}s."))

    # ── CAPTCHA doğrulama ────────────────────────────────────────────────────
    if not verify_captcha(request.session, captcha_selected, captcha_token):
        return _err({
            "tr": "CAPTCHA hatalı. Lütfen tüm resimleri doğru seçin.",
            "en": "Incorrect CAPTCHA. Please select all correct images.",
            "de": "Falsches CAPTCHA. Bitte alle richtigen Bilder auswählen.",
        }.get(lang, "Incorrect CAPTCHA."))

    # Kullanıcı adı / şifre doğrulama
    username = username.strip()[:64]
    password = password.strip()[:128]

    # Önce admin OTP'yi dene — bakım modunda da admin girebilmeli
    admin_doc = await db.verify_admin_credentials(username, password)
    if admin_doc:
        # Yönetici şifresiyle üye kataloğuna giriş engellendi.
        # Admin girişleri yalnızca /login (yönetici paneli) üzerinden yapılmalıdır.
        return _err({
            "tr": "Yönetici şifresiyle üye kataloğuna giriş yapılamaz. Lütfen üye şifrenizi kullanın.",
            "en": "Admin password cannot be used to access the member catalog. Please use your member password.",
            "de": "Das Admin-Passwort kann nicht für den Mitgliederkatalog verwendet werden. Bitte verwenden Sie Ihr Mitgliedspasswort.",
        }.get(lang, "Admin login is not allowed here."))

    # Üye OTP doğrula
    session_doc = await db.verify_member_otp(username, password)

    # Bakım modu — OWNER_ID değilse üye girişini engelle
    if not _get_websitesi():
        is_owner_login = session_doc and int(session_doc.get("user_id", -1)) == Telegram.OWNER_ID
        if not is_owner_login:
            return _err({
                "tr": "Websitemiz şu an bakım çalışmasındadır. Lütfen daha sonra tekrar deneyin.",
                "en": "Our website is currently under maintenance. Please try again later.",
                "de": "Unsere Website befindet sich derzeit im Wartungsmodus. Bitte versuchen Sie es später erneut.",
            }.get(lang, "Under maintenance."), maintenance=True)

    if not session_doc:
        newly_banned = await record_failure_async(ip, endpoint="/uye/giris")
        if newly_banned:
            return _err({
                "tr": f"Çok fazla başarısız giriş denemesi. IP adresiniz {Telegram.BRUTE_BAN} saniye boyunca engellendi.",
                "en": f"Too many failed attempts. Your IP is blocked for {Telegram.BRUTE_BAN} seconds.",
                "de": f"Zu viele Fehlversuche. Ihre IP ist für {Telegram.BRUTE_BAN} Sekunden gesperrt.",
            }.get(lang, f"IP blocked for {Telegram.BRUTE_BAN}s."))
        return _err({
            "tr": "Kullanıcı adı veya şifre hatalı ya da aboneliğiniz sona ermiş.",
            "en": "Invalid credentials or your subscription has expired.",
            "de": "Ungültige Anmeldedaten oder Ihr Abonnement ist abgelaufen.",
        }.get(lang, "Invalid credentials."))

    # Abonelik canlı doğrulama (sadece üye girişi için)
    if not await _check_subscription(session_doc["user_id"]):
        return _err({
            "tr": "Aboneliğiniz sona ermiş veya aktif değil.",
            "en": "Your subscription has expired or is not active.",
            "de": "Ihr Abonnement ist abgelaufen oder nicht aktiv.",
        }.get(lang, "Subscription not active."))

    # Abonelik bitiş tarihini session'a ekle
    _user_doc = await db.get_user(int(session_doc["user_id"]))
    _expiry = _user_doc.get("subscription_expiry") if _user_doc else None
    _sub_end_str = None
    if _expiry is not None:
        if isinstance(_expiry, datetime):
            _sub_end_str = _expiry.strftime("%d.%m.%Y")
        elif isinstance(_expiry, str):
            try:
                _sub_end_str = datetime.fromisoformat(_expiry.replace("Z", "+00:00")).strftime("%d.%m.%Y")
            except ValueError:
                _sub_end_str = _expiry  # olduğu gibi sakla

    # Session'a yaz (sadece güvenli alanlar)
    record_success(ip)
    request.session["member"] = {
        "user_id":          session_doc["user_id"],
        "name":             session_doc.get("display_name", username),
        "photo_url":        session_doc.get("photo_url", ""),
        "token":            session_doc.get("token"),
        "lang":             lang,
        "subscription_end": _sub_end_str,
        "is_admin":         False,
        "session_id":       session_doc.get("session_id", ""),  # /start'ta yenilenir → eski cookie geçersiz
    }
    # Login başarılı → CSRF secret üret/yenile
    ensure_csrf_secret(request)
    return RedirectResponse(url=f"/uye/katalog?lang={lang}", status_code=302)


async def member_logout(request: Request):
    member = _get_member(request)
    if member:
        try:
            await db.invalidate_member_session(member["user_id"])
        except Exception:
            pass
    # Session'daki tum veriyi temizle (pop yerine clear kullanilmali)
    request.session.clear()
    response = RedirectResponse(url="/uye/giris", status_code=302)
    # Session cookie'yi tarayicida da sil (max_age=0 -> aninda sona erdir)
    response.delete_cookie(
        key="session",
        path="/",
        httponly=True,
        samesite="lax",
    )
    return response


# ── Sayfa: Katalog ───────────────────────────────────────────────────────────

async def member_catalog_page(request: Request):
    member = _get_member(request)
    if not member:
        return RedirectResponse(url="/uye/giris", status_code=302)

    # Bakım modu — sadece OWNER_ID erişebilir
    if not _check_website_access(member):
        request.session.pop("member", None)
        return RedirectResponse(url="/uye/giris", status_code=302)

    # Yönetici oturumu ile üye kataloğuna erişim engellendi
    if member.get("is_admin"):
        request.session.pop("member", None)
        return RedirectResponse(url="/uye/giris", status_code=302)

    # Canlı abonelik kontrolü
    if not await _check_subscription(member["user_id"]):
        request.session.pop("member", None)
        return RedirectResponse(url="/uye/giris?expired=1", status_code=302)

    # Session_id kontrolü: /start'ta yeni OTP üretildiğinde DB'deki session_id değişir.
    # Cookie'deki eski session_id artık eşleşmez → kullanıcı login sayfasına yönlendirilir.
    cookie_sid = member.get("session_id", "")
    db_sid = await db.get_member_session_id(int(member["user_id"]))
    if db_sid and cookie_sid != db_sid:
        request.session.pop("member", None)
        return RedirectResponse(url="/uye/giris", status_code=302)

    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    lang  = request.query_params.get("lang", member.get("lang", "tr"))

    # Kullanım bilgisi
    token_doc = None
    usage_info = {"daily_gb": 0, "monthly_gb": 0, "total_gb": 0,
                  "daily_limit": 0, "monthly_limit": 0,
                  "daily_remaining": None, "monthly_remaining": None}
    try:
        all_tokens = await db.get_all_api_tokens()
        token_doc  = next(
            (t for t in all_tokens if t.get("user_id") == member["user_id"]), None
        )
        if token_doc:
            usage  = token_doc.get("usage", {})
            limits = token_doc.get("limits", {})
            usage_info = {
                "daily_gb":      _bytes_to_gb(usage.get("daily",   {}).get("bytes", 0)),
                "monthly_gb":    _bytes_to_gb(usage.get("monthly", {}).get("bytes", 0)),
                "total_gb":      _bytes_to_gb(usage.get("total_bytes", 0)),
                "daily_limit":   limits.get("daily_limit_gb",   0) or 0,
                "monthly_limit": limits.get("monthly_limit_gb", 0) or 0,
            }
            # Kalan hesapla
            if usage_info["daily_limit"] > 0:
                usage_info["daily_remaining"] = max(
                    0, round(usage_info["daily_limit"] - usage_info["daily_gb"], 3)
                )
            else:
                usage_info["daily_remaining"] = None   # sınırsız

            if usage_info["monthly_limit"] > 0:
                usage_info["monthly_remaining"] = max(
                    0, round(usage_info["monthly_limit"] - usage_info["monthly_gb"], 3)
                )
            else:
                usage_info["monthly_remaining"] = None

            # Hız limiti: kişisel > global config
            from Backend.config import Telegram as _Cfg
            _personal_speed = float(limits.get("speed_limit_mbps") or 0)
            _global_speed = 0.0
            try:
                _global_speed = float((_Cfg.HIZ_LIMITI or "").strip()) if (_Cfg.HIZ_LIMITI or "").strip() else 0.0
            except (ValueError, AttributeError):
                pass
            _effective_speed = _personal_speed if _personal_speed > 0 else _global_speed
            usage_info["speed_limit_mbps"]  = _effective_speed
            usage_info["speed_is_personal"] = _personal_speed > 0

            # Eşzamanlı izleme limiti
            # Token'ın kendi limiti yoksa (0) config'deki DEFAULT_DEVICE_LIMIT'e düşülür.
            _cfg_device_limit = int(getattr(_Cfg, "DEFAULT_DEVICE_LIMIT", 0) or 0)
            usage_info["device_limit"]       = int(limits.get("device_limit") or 0) or _cfg_device_limit
            usage_info["active_device_count"] = await db.get_active_device_count(token_doc["token"])
    except Exception:
        pass

    # IP'den ülke bayrağı tespiti
    client_ip = request.client.host if request.client else ""
    country_flag = _get_country_flag(client_ip)

    # Abonelik bitiş tarihini canlı DB'den al
    subscription_end_str = member.get("subscription_end")
    try:
        live_user = await db.get_user(int(member["user_id"]))
        if live_user:
            _exp = live_user.get("subscription_expiry")
            if _exp is not None:
                if isinstance(_exp, datetime):
                    subscription_end_str = _exp.strftime("%d.%m.%Y")
                elif isinstance(_exp, str):
                    # ISO string → datetime parse et
                    _exp_dt = datetime.fromisoformat(_exp.replace("Z", "+00:00"))
                    subscription_end_str = _exp_dt.strftime("%d.%m.%Y")
                else:
                    subscription_end_str = str(_exp)
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning("subscription_end parse hatası: %s", _e)

    return templates.TemplateResponse("member_catalog.html", {
        "request":          request,
        "theme":            theme,
        "themes":           get_all_themes(),
        "current_theme":    theme_name,
        "app_name": Telegram.ISIM,
        "member":           member,
        "member_name":      member.get("name", ""),
        "usage":            usage_info,
        "lang":             lang,
        "country_flag":     country_flag,
        "subscription_end": subscription_end_str,
        "photo_url":        member.get("photo_url", ""),
        "is_admin":         member.get("is_admin", False),
    })


# ── API: Medya listesi (salt-okunur, sadece kendi token'ı) ───────────────────

async def member_media_api(
    request:    Request,
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page:       int = Query(1,  ge=1),
    page_size:  int = Query(24, ge=1, le=50),
    search:     str = Query("", max_length=100),
    lang:       str = Query("tr"),
    sort:              str = Query("newest",   max_length=32),
    genre:             str = Query("",         max_length=64),
    year:              str = Query("",         max_length=8),
    cast_name:         str = Query("",         max_length=100),
    platform:          str = Query("",         max_length=32),
):
    # cast_name: boşlukları temizle, MongoDB $regex'e gitmeden önce
    # özel regex karakterlerini kaçır → ReDoS ve NoSQL injection önlemi
    cast_name = _re.escape(cast_name.strip())

    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=401, detail="Oturum açılmamış")

    if not _check_website_access(member):
        raise HTTPException(status_code=403, detail="Erişim kapalı")

    if not await _check_subscription(member["user_id"]):
        raise HTTPException(status_code=403, detail="Abonelik sona ermiş")

    # ── Platform filtresi: platform_catalog üzerinden imdb_id seti ──────────
    platform_imdb_ids = None
    if platform:
        from Backend.helper.platform_catalog import platform_catalog, PLATFORM_LABELS
        if platform in PLATFORM_LABELS:
            if not platform_catalog.is_loaded():
                import asyncio
                loop = asyncio.get_event_loop()
                platform_catalog.schedule_refresh()
            else:
                items_meta = platform_catalog.get(platform)
                platform_imdb_ids = {m["imdb_id"] for m in items_meta if m.get("imdb_id")}

    # ── Seri Filmler — platform_catalog üzerinden (stremio ile aynı mantık) ──
    if sort == "collection" and media_type == "movie" and not search:
        from Backend.helper.platform_catalog import platform_catalog as _pc
        if not _pc.is_loaded():
            import asyncio
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, _pc.refresh)
            return {"total_count": 0, "current_page": 1, "total_pages": 1, "movies": []}

        all_coll = _pc.get_collection_movies()

        if genre:
            gf = "genres_de" if lang == "de" else "genres_tr"
            all_coll = [i for i in all_coll if genre in (i.get(gf) or i.get("genres") or [])]
        if year:
            try:
                y = int(year)
                all_coll = [i for i in all_coll if i.get("release_year") == y]
            except ValueError:
                pass
        if cast_name:
            cn = cast_name.lower()
            all_coll = [i for i in all_coll if any(cn in (c or "").lower() for c in (i.get("cast") or []))]
        if platform_imdb_ids is not None:
            all_coll = [i for i in all_coll if i.get("imdb_id") in platform_imdb_ids]

        total = len(all_coll)
        start_idx = (page - 1) * page_size
        return {
            "total_count":  total,
            "current_page": page,
            "total_pages":  max(1, (total + page_size - 1) // page_size),
            "movies": _strip_admin_fields(all_coll[start_idx: start_idx + page_size]),
        }

    _SORT_MAP = {
        "newest":      ("updated_on",    "desc"),
        "rating_desc": ("rating",        "desc"),
        "rating_asc":  ("rating",        "asc"),
        "year_desc":   ("release_year",  "desc"),
        "year_asc":    ("release_year",  "asc"),
        "az":          ("title",         "asc"),
        "za":          ("title",         "desc"),
        "collection":  ("collection_id", "asc"),
        "yerli_film":  ("updated_on",    "desc"),
        "yerli_dizi":  ("updated_on",    "desc"),
    }
    sort_field, sort_dir = _SORT_MAP.get(sort, ("updated_on", "desc"))
    sort_params = [(sort_field, sort_dir)]

    try:
        if search:
            result = await db.search_documents(search, page, page_size)
            items  = [i for i in result["results"] if i.get("media_type") == media_type]
            if genre:
                gf = "genres_de" if lang == "de" else "genres_tr"
                items = [i for i in items if genre in (i.get(gf) or i.get("genres") or [])]
            if year:
                try:
                    y = int(year)
                    items = [i for i in items if i.get("release_year") == y]
                except ValueError:
                    pass
            if cast_name:
                cn = cast_name.lower()
                items = [i for i in items if any(cn in (c or "").lower() for c in (i.get("cast") or []))]
            if platform_imdb_ids is not None:
                items = [i for i in items if i.get("imdb_id") in platform_imdb_ids]

            total = len(items)
            start_idx = (page - 1) * page_size
            items = items[start_idx: start_idx + page_size]
            return {
                "total_count":  total,
                "current_page": page,
                "total_pages":  max(1, (total + page_size - 1) // page_size),
                ("movies" if media_type == "movie" else "tv_shows"): _strip_admin_fields(items),
            }
        else:
            extra_filter: dict = {}
            # Yerli filmler / diziler — stremio_routes ile aynı mantık
            if sort in ("yerli_film", "yerli_dizi"):
                extra_filter["original_language"] = "tr"
            if genre:
                gf = "genres_de" if lang == "de" else "genres_tr"
                extra_filter[gf] = {"$in": [genre]}
            if year:
                try:
                    extra_filter["release_year"] = int(year)
                except ValueError:
                    pass
            if cast_name:
                extra_filter["cast"] = {"$regex": cast_name, "$options": "i"}
            if platform_imdb_ids is not None:
                extra_filter["imdb_id"] = {"$in": list(platform_imdb_ids)}

            if media_type == "movie":
                raw = await db.sort_movies(sort_params, page, page_size, lang=lang,
                                           extra_filter=extra_filter)
                raw["movies"] = _strip_admin_fields(raw.get("movies", []))
            else:
                raw = await db.sort_tv_shows(sort_params, page, page_size, lang=lang,
                                             extra_filter=extra_filter)
                raw["tv_shows"] = _strip_admin_fields(raw.get("tv_shows", []))
            return raw
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


def _strip_admin_fields(items: list) -> list:
    """Admin-only alanları kaldır: telegram file_id'leri, DB bilgileri vs."""
    safe = []
    for item in items:
        keep = {
            "tmdb_id":           item.get("tmdb_id"),
            "imdb_id":           item.get("imdb_id"),
            "db_index":          item.get("db_index"),
            "title":             item.get("title"),
            "title_tr":          item.get("title_tr"),
            "title_de":          item.get("title_de"),
            "description":       item.get("description"),
            "description_tr":    item.get("description_tr"),
            "description_de":    item.get("description_de"),
            "rating":            item.get("rating"),
            "release_year":      item.get("release_year"),
            "poster":            item.get("poster"),
            "poster_tr":         item.get("poster_tr"),
            "poster_de":         item.get("poster_de"),
            "backdrop":          item.get("backdrop"),
            "certification_tr":  item.get("certification_tr"),
            "certification_de":  item.get("certification_de"),
            "certification_us":  item.get("certification_us"),
            "genres":            item.get("genres"),
            "genres_tr":         item.get("genres_tr"),
            "genres_de":         item.get("genres_de"),
            "cast":              item.get("cast"),          # Oyuncu listesi modal için gerekli
            "media_type":        item.get("media_type"),
            "language":          item.get("language", ""),   # dil bayrağı için
            "original_language": item.get("original_language"),  # yerli içerik tespiti
            # Film için kalite listesi (file_id korunur çünkü stream URL'de lazım,
            # ama DB_KEY / chat_id gibi hassas alanlar yoktur QualityDetail'de)
            # Film kaliteleri: telegram varsa oradan üret, yoksa qualities alanını kullan (platform_catalog)
            "qualities":         _safe_qualities(item.get("telegram", [])) or item.get("qualities", []),
            # TV için sezon/bölüm özeti (detaylar ayrı endpoint'te)
            "season_count":      len(item.get("seasons", [])),
            # Film süresi (dakika)
            "runtime":           item.get("runtime"),
            # Dizi yayın durumu (devam ediyor / bitti / iptal vs.)
            "status":            item.get("status", ""),
        }
        safe.append(keep)
    return safe


def _safe_qualities(telegram: list) -> list:
    """QualityDetail'den sadece güvenli alanları döndür."""
    result = []
    for q in (telegram or []):
        qid = q.get("id", "")
        # Kaynak tespiti: encoded_string'i senkron olarak kontrol et
        source = "telegram"
        try:
            import json as _json, base64 as _b64
            raw = _b64.urlsafe_b64decode(qid + "==")
            decoded = _json.loads(raw)
            if isinstance(decoded, dict):
                if decoded.get("gdrive_file_id"):
                    source = "gdrive"
                elif decoded.get("rclone_remote"):
                    source = f"rclone:{decoded.get('rclone_remote', '')}"
        except Exception:
            pass
        result.append({
            "quality": q.get("quality"),
            "name":    q.get("name"),
            "size":    q.get("size"),
            "id":      qid,
            "source":  source,
        })
    return result


# ── API: TV Bölümleri ────────────────────────────────────────────────────────

async def member_tv_detail_api(
    request:    Request,
    tmdb_id:    int = Query(...),
    db_index:   int = Query(-1),   # -1 → tüm shardlarda ara (TMDB kartları için)
    lang:       str = Query("tr"),
):
    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=401)
    if not _check_website_access(member):
        raise HTTPException(status_code=403)
    if not await _check_subscription(member["user_id"]):
        raise HTTPException(status_code=403)

    doc = None
    if db_index >= 0:
        doc = await db.get_document("tv", tmdb_id, db_index)
    else:
        # db_index bilinmiyor — tüm storage shardlarını tara
        storage_keys = [k for k in db.dbs if k.startswith("storage_")]
        for key in sorted(storage_keys):
            idx = int(key.split("_")[1])
            candidate = await db.get_document("tv", tmdb_id, idx)
            if candidate:
                doc = candidate
                break

    if not doc:
        raise HTTPException(status_code=404)

    # Sadece season/episode yapısını döndür (file_id'ler dahil — stream için)
    seasons = []
    for s in doc.get("seasons", []):
        episodes = []
        for ep in s.get("episodes", []):
            episodes.append({
                "episode_number": ep.get("episode_number"),
                "title":          ep.get("title"),
                "title_tr":       ep.get("title_tr"),
                "title_de":       ep.get("title_de"),
                "released":       ep.get("released"),
                "qualities":      _safe_qualities(ep.get("telegram", [])),
            })
        seasons.append({"season_number": s.get("season_number"), "episodes": episodes})

    return {
        "tmdb_id":    doc.get("tmdb_id"),
        "title":      doc.get("title"),
        "title_tr":   doc.get("title_tr"),
        "title_de":   doc.get("title_de"),
        "seasons":    seasons,
    }


# ── API: Stream URL üret ─────────────────────────────────────────────────────

async def member_stream_url_api(
    request:  Request,
    file_id:  str = Query(..., max_length=2048),
    filename: str = Query("video.mkv", max_length=512),  # Gerçek dosya adı katalogdan gelir
):
    """
    Sadece oturumu açık aboneye ait token üzerinden stream URL döndürür.
    file_id manipüle edilse bile URL yalnızca abone tokenına bağlıdır.
    """
    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=401)
    if not _check_website_access(member):
        raise HTTPException(status_code=403)
    if not await _check_subscription(member["user_id"]):
        raise HTTPException(status_code=403)

    token = member.get("token")
    if not token:
        raise HTTPException(status_code=403, detail="Token bulunamadı")

    # Limit kontrolü
    token_doc = await db.get_api_token(token)
    if token_doc:
        limits = token_doc.get("limits", {})
        usage  = token_doc.get("usage", {})
        dl = limits.get("daily_limit_gb", 0) or 0
        ml = limits.get("monthly_limit_gb", 0) or 0
        if dl > 0 and _bytes_to_gb(usage.get("daily", {}).get("bytes", 0)) >= dl:
            raise HTTPException(status_code=429, detail="daily_limit")
        if ml > 0 and _bytes_to_gb(usage.get("monthly", {}).get("bytes", 0)) >= ml:
            raise HTTPException(status_code=429, detail="monthly_limit")

    from Backend.helper.stream_token import media_token_manager
    from Backend.helper.encrypt import encode_string
    base   = Telegram.BASE_URL.rstrip("/")

    # ── Yardımcı: proxy URL üret ────────────────────────────────────────────
    def _apply_proxy(direct_url: str) -> str | None:
        """PROXY aktifse ve HTTP_PROXY_URL doluysa proxy URL döner, aksi halde None."""
        if Telegram.PROXY and Telegram.HTTP_PROXY_URL:
            return f"{Telegram.HTTP_PROXY_URL}{direct_url}"
        return None

    # Harici URL'ler — proxy moduna göre dön
    if file_id.startswith(("http://", "https://")):
        proxy_url = _apply_proxy(file_id)
        if proxy_url and Telegram.PROXY_MODE == 3:
            return {"url": proxy_url}
        if proxy_url and Telegram.PROXY_MODE == 2:
            return {"url": proxy_url, "url_direct": file_id}
        return {"url": file_id}

    # Yerel sunucu dosyaları (sunucu panelinden eklenen) — local_path olarak encode et
    if file_id.startswith("/") or (len(file_id) > 1 and file_id[1] == ":"):
        import os as _os
        from pathlib import Path as _Path
        from urllib.parse import quote as _quote

        # ── Path Traversal Koruması ──────────────────────────────────────────
        # file_id'yi encode etmeden önce izin verilen dizin içinde olduğunu doğrula.
        # Bu kontrol yapılmazsa üye, /etc/passwd gibi keyfi yolları encode edip
        # stream endpoint'ine iletebilir; local_file_streamer'daki kontrol son savunma
        # hattıdır, birincil kontrol burada olmalıdır.
        _default_sunucu = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__)))),
            "uploads",
        )
        _SUNUCU_DIR = _Path(_os.getenv("SUNUCU_DIR", _default_sunucu)).resolve()
        try:
            _resolved = _Path(file_id).resolve()
        except Exception:
            raise HTTPException(status_code=400, detail="Geçersiz dosya yolu.")

        if not (
            str(_resolved).startswith(str(_SUNUCU_DIR) + _os.sep)
            or _resolved == _SUNUCU_DIR
        ):
            from Backend.logger import LOGGER as _LOGGER
            _LOGGER.warning(
                "[member_stream_url] Path traversal girişimi engellendi — "
                "file_id=%r token=%s...",
                file_id,
                token[:8],
            )
            raise HTTPException(status_code=403, detail="Erişim reddedildi.")
        # ── Path Traversal Koruması sonu ─────────────────────────────────────

        safe_filename = _quote(filename, safe=".-_")
        encoded_id = await encode_string({"local_path": file_id})
        indir_token = media_token_manager.create(token, encoded_id, kind="indir")
        url = f"{base}/dl/{token}/{encoded_id}/{indir_token}/{safe_filename}?dl=1"
        proxy_url = _apply_proxy(url)
        if proxy_url and Telegram.PROXY_MODE == 3:
            return {"url": proxy_url}
        if proxy_url and Telegram.PROXY_MODE == 2:
            return {"url": proxy_url, "url_direct": url}
        return {"url": url}

    # Dosya adını URL-güvenli hale getir (UTF-8 encoding)
    from urllib.parse import quote
    safe_filename = quote(filename, safe=".-_")
    # Arşiv dosyalarına .mkv ekleme; sadece uzantısız video dosyalarına ekle
    import re as _re_mr
    _video_exts = (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts")
    _lower = safe_filename.lower()
    _is_arch = (
        _lower.endswith((".zip", ".7z", ".rar"))
        or bool(_re_mr.search(r'\.(zip|7z|rar|z)\.\d+$', _lower))
        or bool(_re_mr.search(r'\.part\d+\.rar$', _lower))
    )
    if not any(_lower.endswith(e) for e in _video_exts) and not _is_arch:
        safe_filename = safe_filename + ".mkv"

    # Üyeye + dosyaya özgü indirme tokeni üret (YENILEME saat geçerli)
    indir_token = media_token_manager.create(token, file_id, kind="indir")
    url = f"{base}/dl/{token}/{file_id}/{indir_token}/{safe_filename}?dl=1"

    # Kaynak tespiti — encoded_string'i çöz
    is_gdrive  = False
    is_rclone  = False
    source_label = ""
    try:
        from Backend.helper.encrypt import decode_string as _dec
        decoded = await _dec(file_id)
        if isinstance(decoded, dict):
            if decoded.get("gdrive_file_id"):
                is_gdrive    = True
                source_label = "gdrive"
            elif decoded.get("rclone_remote") and decoded.get("rclone_path"):
                is_rclone    = True
                source_label = f"rclone:{decoded['rclone_remote']}"
    except Exception:
        pass

    # ── Proxy moduna göre son URL'i belirle ──────────────────────────────────
    proxy_url = _apply_proxy(url)
    if proxy_url and Telegram.PROXY_MODE == 3:
        return {"url": proxy_url, "is_gdrive": is_gdrive, "is_rclone": is_rclone, "source": source_label}
    if proxy_url and Telegram.PROXY_MODE == 2:
        return {"url": proxy_url, "url_direct": url, "is_gdrive": is_gdrive, "is_rclone": is_rclone, "source": source_label}
    return {"url": url, "is_gdrive": is_gdrive, "is_rclone": is_rclone, "source": source_label}


# ── API: Kullanım bilgisi ────────────────────────────────────────────────────

async def member_usage_api(request: Request):
    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=401)
    if not _check_website_access(member):
        raise HTTPException(status_code=403)

    all_tokens = await db.get_all_api_tokens()
    token_doc  = next(
        (t for t in all_tokens if t.get("user_id") == member["user_id"]), None
    )
    if not token_doc:
        return {"daily_gb": 0, "monthly_gb": 0, "total_gb": 0,
                "daily_limit": 0, "monthly_limit": 0,
                "daily_remaining": None, "monthly_remaining": None}

    # Türkiye saatiyle gün/ay değişmişse sıfırla (stream olmadan da tetiklenir)
    await db.reset_token_usage_if_needed(token_doc["token"])

    # Sıfırlama sonrası güncel veriyi yeniden çek
    all_tokens = await db.get_all_api_tokens()
    token_doc  = next(
        (t for t in all_tokens if t.get("user_id") == member["user_id"]), None
    )

    usage  = token_doc.get("usage", {})
    limits = token_doc.get("limits", {})
    d_gb   = _bytes_to_gb(usage.get("daily",   {}).get("bytes", 0))
    m_gb   = _bytes_to_gb(usage.get("monthly", {}).get("bytes", 0))
    dl     = limits.get("daily_limit_gb",   0) or 0
    ml     = limits.get("monthly_limit_gb", 0) or 0

    # Hız limiti: kişisel > global config
    from Backend.config import Telegram as _Cfg
    _personal_speed = float(limits.get("speed_limit_mbps") or 0)
    _global_speed = 0.0
    try:
        _global_speed = float((_Cfg.HIZ_LIMITI or "").strip()) if (_Cfg.HIZ_LIMITI or "").strip() else 0.0
    except (ValueError, AttributeError):
        pass
    _effective_speed = _personal_speed if _personal_speed > 0 else _global_speed

    return {
        "daily_gb":          d_gb,
        "monthly_gb":        m_gb,
        "total_gb":          _bytes_to_gb(usage.get("total_bytes", 0)),
        "daily_limit":       dl,
        "monthly_limit":     ml,
        "daily_remaining":   max(0, round(dl - d_gb, 3)) if dl > 0 else None,
        "monthly_remaining": max(0, round(ml - m_gb, 3)) if ml > 0 else None,
        "speed_limit_mbps":  _effective_speed,
        "speed_is_personal": _personal_speed > 0,
        "device_limit":       int(limits.get("device_limit") or 0) or int(getattr(_Cfg, "DEFAULT_DEVICE_LIMIT", 0) or 0),
        "active_device_count": await db.get_active_device_count(token_doc["token"]),
    }


# ── API: Veritabanı içerik boyutu ─────────────────────────────────────────────

async def member_db_size_api(request: Request):
    """Film ve dizi koleksiyonlarının GB cinsinden boyutunu döner."""
    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=401)
    if not _check_website_access(member):
        raise HTTPException(status_code=403)
    try:
        sizes = await db.get_content_sizes()
        return sizes
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


# ── API: Üye profil & istatistik ─────────────────────────────────────────────

async def member_profile_api(request: Request):
    """
    Üye profil sayfası için tüm verileri döner:
      - Üyelik başlangıç / bitiş tarihleri
      - Günlük / haftalık / aylık kullanım
      - Son 30 gün günlük breakdown (veri kullanımı)
      - Günlük limit sıfırlanmasına kalan süre (Europe/Istanbul gece 00:00)
    """
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Istanbul")

    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=401)
    if not _check_website_access(member):
        raise HTTPException(status_code=403)

    uid = int(member["user_id"])

    # ── Token ve kullanım verisi ──────────────────────────────────────────────
    all_tokens = await db.get_all_api_tokens()
    token_doc  = next((t for t in all_tokens if t.get("user_id") == uid), None)

    usage  = token_doc.get("usage", {})  if token_doc else {}
    limits = token_doc.get("limits", {}) if token_doc else {}
    d_gb   = _bytes_to_gb(usage.get("daily",   {}).get("bytes", 0))
    m_gb   = _bytes_to_gb(usage.get("monthly", {}).get("bytes", 0))
    total  = _bytes_to_gb(usage.get("total_bytes", 0))
    dl     = limits.get("daily_limit_gb",   0) or 0
    ml     = limits.get("monthly_limit_gb", 0) or 0

    # ── Üyelik tarihleri ──────────────────────────────────────────────────────
    live_user    = await db.get_user(uid)
    created_raw  = None
    expiry_raw   = None
    days_left    = None

    if live_user:
        created_raw = live_user.get("created_at")
        expiry_raw  = live_user.get("subscription_expiry")

    def _fmt_dt(v):
        if not v:
            return None
        if isinstance(v, datetime):
            return v.strftime("%d.%m.%Y %H:%M")
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).strftime("%d.%m.%Y %H:%M")
        except Exception:
            return str(v)

    def _fmt_date_only(v):
        if not v:
            return None
        if isinstance(v, datetime):
            return v.strftime("%d.%m.%Y")
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).strftime("%d.%m.%Y")
        except Exception:
            return str(v)

    # Kalan gün hesapla
    if expiry_raw:
        try:
            exp_dt = expiry_raw if isinstance(expiry_raw, datetime) else \
                     datetime.fromisoformat(str(expiry_raw).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            delta = exp_dt - now_utc
            days_left = max(0, delta.days)
        except Exception:
            days_left = None

    # ── Günlük sıfırlanmaya kalan süre (LIMIT_SIFIRLAMA — Türkiye saati UTC+3) ──
    from datetime import timedelta
    from Backend.config import Telegram as _Cfg
    _reset_raw = (_Cfg.LIMIT_SIFIRLAMA or "").strip()
    try:
        _rh, _rm = (int(x) for x in _reset_raw.split(":"))
    except Exception:
        _rh, _rm = 0, 0  # varsayilan: gece 00:00 Turkiye saati

    now_ist     = datetime.now(_TZ)
    reset_today = now_ist.replace(hour=_rh, minute=_rm, second=0, microsecond=0)
    if now_ist >= reset_today:
        reset_today = reset_today + timedelta(days=1)
    diff_secs   = int((reset_today - now_ist).total_seconds())
    reset_hours = diff_secs // 3600
    reset_mins  = (diff_secs % 3600) // 60
    reset_str   = f"{reset_hours}s {reset_mins}dk" if reset_hours > 0 else f"{reset_mins}dk"

    # ── Son 30 gün günlük kullanım breakdown ─────────────────────────────────
    daily_30 = []
    try:
        col = db.dbs["tracking"]["stream_analytics"]
        from datetime import timedelta as _td
        thirty_days_ago = datetime.now(timezone.utc) - _td(days=29)

        # Kullanıcıya ait token ile filtrele — tüm platform verisi sızmasın
        _user_token = token_doc.get("token") if token_doc else None
        _match: dict = {"logged_at": {"$gte": thirty_days_ago}}
        if _user_token:
            _match["meta.user_token"] = _user_token

        pipe = [
            {"$match": _match},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$logged_at"}},
                "bytes": {"$sum": "$total_bytes"},
                "streams": {"$sum": 1}
            }},
            {"$sort": {"_id": 1}}
        ]
        raw = await col.aggregate(pipe).to_list(None)

        # Tüm 30 günü doldur (veri olmayan günler 0)
        day_map = {r["_id"]: r for r in raw}
        for i in range(30):
            d = (thirty_days_ago + _td(days=i)).strftime("%Y-%m-%d")
            entry = day_map.get(d, {"_id": d, "bytes": 0, "streams": 0})
            daily_30.append({
                "date":    d,
                "gb":      round(entry["bytes"] / (1024**3), 3),
                "streams": entry.get("streams", 0)
            })
    except Exception:
        pass

    # ── Token belgesi yoksa token_doc bilgileri ───────────────────────────────
    token_created = _fmt_dt(token_doc.get("created_at")) if token_doc else None

    return {
        # Kimlik
        "name":             member.get("name", ""),
        "user_id":          uid,
        # Üyelik tarihleri
        "member_since":     _fmt_dt(created_raw) or token_created,
        "member_since_date": _fmt_date_only(created_raw),
        "expiry_date":      _fmt_date_only(expiry_raw),
        "expiry_datetime":  _fmt_dt(expiry_raw),
        "days_left":        days_left,
        # Kullanım
        "daily_gb":         round(d_gb, 3),
        "monthly_gb":       round(m_gb, 3),
        "total_gb":         round(total, 3),
        "daily_limit":      dl,
        "monthly_limit":    ml,
        "daily_remaining":  max(0, round(dl - d_gb, 3)) if dl > 0 else None,
        "monthly_remaining":max(0, round(ml - m_gb, 3)) if ml > 0 else None,
        # Reset countdown
        "reset_in_str":     reset_str,
        "reset_in_secs":    diff_secs,
        "reset_hours":      reset_hours,
        "reset_mins":       reset_mins,
        "reset_clock_hour": _rh,
        "reset_clock_min":  _rm,
        # Son 30 gün
        "daily_30":         daily_30,
    }


# ── Sayfa: Hatırlatmalar ──────────────────────────────────────────────────────

async def member_hatirlatmalar_page(request: Request):
    member = _get_member(request)
    if not member:
        return RedirectResponse(url="/uye/giris", status_code=302)

    if member.get("is_admin"):
        request.session.pop("member", None)
        return RedirectResponse(url="/uye/giris", status_code=302)

    if not await _check_subscription(member["user_id"]):
        request.session.pop("member", None)
        return RedirectResponse(url="/uye/giris?expired=1", status_code=302)

    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    lang  = request.query_params.get("lang", member.get("lang", "tr"))

    # Kullanım bilgisi
    usage_info = {"daily_gb": 0, "monthly_gb": 0, "total_gb": 0,
                  "daily_limit": 0, "monthly_limit": 0,
                  "daily_remaining": None, "monthly_remaining": None}
    try:
        all_tokens = await db.get_all_api_tokens()
        token_doc  = next(
            (t for t in all_tokens if t.get("user_id") == member["user_id"]), None
        )
        if token_doc:
            usage  = token_doc.get("usage", {})
            limits = token_doc.get("limits", {})
            d_gb = _bytes_to_gb(usage.get("daily",   {}).get("bytes", 0))
            m_gb = _bytes_to_gb(usage.get("monthly", {}).get("bytes", 0))
            dl   = limits.get("daily_limit_gb",   0) or 0
            ml   = limits.get("monthly_limit_gb", 0) or 0
            usage_info = {
                "daily_gb":         d_gb,
                "monthly_gb":       m_gb,
                "total_gb":         _bytes_to_gb(usage.get("total_bytes", 0)),
                "daily_limit":      dl,
                "monthly_limit":    ml,
                "daily_remaining":  max(0, round(dl - d_gb, 3)) if dl > 0 else None,
                "monthly_remaining":max(0, round(ml - m_gb, 3)) if ml > 0 else None,
            }
    except Exception:
        pass

    # Abonelik bitiş
    subscription_end_str = member.get("subscription_end")
    try:
        live_user = await db.get_user(int(member["user_id"]))
        if live_user:
            _exp = live_user.get("subscription_expiry")
            if _exp is not None:
                if isinstance(_exp, datetime):
                    subscription_end_str = _exp.strftime("%d.%m.%Y")
                elif isinstance(_exp, str):
                    from datetime import timezone
                    _exp_dt = datetime.fromisoformat(_exp.replace("Z", "+00:00"))
                    subscription_end_str = _exp_dt.strftime("%d.%m.%Y")
                else:
                    subscription_end_str = str(_exp)
    except Exception:
        pass

    return templates.TemplateResponse("hatırlatmalar.html", {
        "request":          request,
        "theme":            theme,
        "current_theme":    theme_name,
        "app_name":         Telegram.ISIM,
        "member":           member,
        "member_name":      member.get("name", ""),
        "usage":            usage_info,
        "lang":             lang,
        "subscription_end": subscription_end_str,
    })
