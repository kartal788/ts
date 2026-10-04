"""
WebDAV Entegrasyonu API Route'ları
==================================
Tüm uç noktalar /api/sunucu/webdav/* altındadır (CSRF middleware'i bu öneki korur).

  GET    /api/sunucu/webdav/sunucular        → kayıtlı WebDAV sunucuları (şifresiz)
  POST   /api/sunucu/webdav/sunucu           → sunucu ekle / güncelle
  DELETE /api/sunucu/webdav/sunucu           → sunucu sil (+ o sunucuya ait tüm içerikleri kataloğdan kaldır)
  POST   /api/sunucu/webdav/test             → bağlantı testi
  GET    /api/sunucu/webdav/listele          → klasör içeriği
  POST   /api/sunucu/webdav/tara             → klasörü özyinelemeli tara (video listesi)
  POST   /api/sunucu/webdav/meta-sorgu       → tek dosya için metadata sorgula
  POST   /api/sunucu/webdav/ekle             → onaylanan metadata ile tek dosya ekle
  POST   /api/sunucu/webdav/toplu-ekle       → seçilen dosyaları otomatik metadata ile ekle
  POST   /api/sunucu/webdav/klasor-ekle      → bir klasörü (alt klasörleriyle) tek tıkla topluca ekle
  POST   /api/sunucu/webdav/sync             → WebDAV'dan silinen dosyaları katalogdan kaldır (dry_run destekli)
  GET    /api/sunucu/webdav/is/{job_id}      → toplu ekleme ilerlemesi
  GET    /api/sunucu/webdav/db-listele       → eklenmiş WebDAV içerikleri
  DELETE /api/sunucu/webdav/db-sil           → kaydı ve katalog girdisini kaldır
"""

import asyncio
import logging
import re
import secrets
import time
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from Backend import db
from Backend.fastapi.security.credentials import require_auth
from Backend.helper import webdav as dav
from Backend.helper.encrypt import encode_string
from Backend.helper.metadata import metadata as fetch_metadata, extract_default_id
from Backend.helper.pyro import clean_filename, remove_urls
from Backend.logger import LOGGER

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sunucu/webdav", dependencies=[Depends(require_auth)])

APPROVED_COLLECTION = "webdav_approved"
PAGE_SIZE = 20
MAX_BULK = 500
MAX_FOLDER = 3000          # tek klasör ekleme işinde işlenecek en fazla video
SYNC_PREVIEW_LIMIT = 200   # sync yanıtında sunucu başına gösterilecek en fazla kayıt


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _storages() -> list:
    """Tüm storage_* veritabanları (kayıtlar farklı DB'lere dağılmış olabilir)."""
    return [v for k, v in db.dbs.items() if k.startswith("storage_")]


def _current_storage():
    return db.dbs.get(f"storage_{db.current_db_index}")


async def _json(request: Request):
    try:
        body = await request.json()
        return body if isinstance(body, dict) else None
    except Exception:
        return None


async def _server_or_none(server_id: str):
    if not server_id:
        return None
    return await dav.get_server(server_id)


# ── Metadata yardımcıları ─────────────────────────────────────────────────────

_EPISODE_ONLY = re.compile(
    r"^(?:s\d{1,2}[\s._-]*e\d{1,3}|e\d{1,3}|\d{1,2}x\d{1,3}|ep?\.?\s?\d{1,3}|\d{1,3})\b",
    re.IGNORECASE,
)
_SEASON_DIR = re.compile(r"^(?:season|sezon|staffel|s)[\s._-]*\d{1,2}$", re.IGNORECASE)


def _meta_name(path: str) -> str:
    """
    Metadata sorgusu için dosya adı üretir. Dosya adı yalnızca bölüm bilgisi
    içeriyorsa (E01.mkv gibi) dizi adını üst klasörlerden alır:
      Dizi Adı/Season 1/E01.mkv  →  "Dizi Adı S01E01.mkv"
    """
    p = PurePosixPath(path)
    fname = p.name
    if not _EPISODE_ONLY.match(fname):
        return fname

    parents = [x for x in p.parent.parts]
    season = None
    show = None
    for part in reversed(parents):
        if _SEASON_DIR.match(part):
            m = re.search(r"\d+", part)
            season = season or (int(m.group()) if m else None)
            continue
        show = part
        break
    if not show:
        return fname

    stem = p.stem
    m = re.match(r"^(?:e|ep\.?\s?)?(\d{1,3})$", stem, re.IGNORECASE)
    if m and season is not None:
        return f"{show} S{season:02d}E{int(m.group(1)):02d}{p.suffix}"
    return f"{show} {fname}"


async def _query_meta(path: str, name: str = None, override_id: str = None):
    """
    name        → metadata aramasında kullanılacak ad (None ise _meta_name(path))
    override_id → TMDB/IMDb id ya da linki (verilirse dosya adındaki id'nin yerine geçer)
    """
    name = name or _meta_name(path)
    clean = clean_filename(name)
    if override_id:
        found_id = override_id
    else:
        found_id, _ = extract_default_id(name)
    return await fetch_metadata(clean, 0, 0, override_id=found_id)


# ── Klasör ekleme için isim üretimi ──────────────────────────────────────────

_SEASON_DIR2 = re.compile(
    r"^(?:(?:season|sezon|staffel|s)[\s._-]*(\d{1,2})|(\d{1,2})\.?[\s._-]*(?:sezon|season))$",
    re.IGNORECASE,
)
_SXXEYY = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})")
_NXNN = re.compile(r"(?<!\d)(\d{1,2})[xX](\d{1,3})(?!\d)")
_EP_ONLY = (
    re.compile(r"(?:^|[\s._-])(?:e|ep|episode|bölüm|bolum|folge)[\s._-]*(\d{1,3})(?!\d)", re.IGNORECASE),
    re.compile(r"^(\d{1,3})(?!\d|[pP]\b)"),   # "01 - Başlık" (ama "720p" değil)
)
_RES = re.compile(r"(?<!\d)(2160|1080|720|576|480)p", re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_KINDS = ("auto", "tv", "movie")


def _res_token(fname: str) -> str:
    m = _RES.search(fname)
    return f" {m.group(1)}p" if m else ""


def _tv_search_name(path: str, show_override: str = ""):
    """
    Dizi klasörü için arama adı. Dizi adı: elle girilen > dosyanın ait olduğu ilk
    (sezon olmayan) klasör. Sezon: dosya adı > sezon klasörü > 1.
    Dosya adı başlık + SxxEyy içeriyor ve dizi adı elle verilmediyse dosya adı aynen kullanılır
    (kalite/kaynak bilgisi korunur). Başlıksız ("S01E03.mkv", "1x04.mkv") ise dizi adı klasörden
    alınır. Bölüm bulunamazsa None döner.
    """
    p = PurePosixPath(path)
    fname = p.name

    season_dir = None
    show = show_override
    for part in reversed(p.parent.parts):
        sm = _SEASON_DIR2.match(part)
        if sm:
            season_dir = season_dir or int(sm.group(1) or sm.group(2))
            continue
        if not show:
            show = part
        break

    season = ep = None
    full = _SXXEYY.search(fname) or _NXNN.search(fname)
    if full:
        season, ep = int(full.group(1)), int(full.group(2))
        has_title = bool(re.search(r"[^\W\d_]", fname[:full.start()]))
        if not show_override and has_title:
            return fname           # "Dizi.S01E03.1080p.mkv": başlık + bölüm zaten var, aynen kullan
    else:
        for rx in _EP_ONLY:
            m = rx.search(p.stem)
            if m:
                ep = int(m.group(1))
                break
        season = season_dir if season_dir is not None else 1

    if ep is None or not show:
        return None
    return f"{show} S{season:02d}E{ep:02d}{_res_token(fname)}{p.suffix}"


def _movie_search_name(path: str) -> str:
    """Dosya adında yıl yoksa ama klasör adında varsa ('Film (2010)/movie.mkv') klasör adını kullan."""
    p = PurePosixPath(path)
    if not _YEAR.search(p.stem) and p.parent.name and _YEAR.search(p.parent.name):
        return f"{p.parent.name}{_res_token(p.name)}{p.suffix}"
    return p.name


def _folder_search_name(path: str, kind: str, show: str = "") -> str:
    if kind == "tv":
        return _tv_search_name(path, show) or _meta_name(path)
    if kind == "movie":
        return _movie_search_name(path)
    return _meta_name(path)


def _normalize_meta(meta: dict) -> dict:
    """insert_media'nın beklediği alanlar (rclone akışıyla aynı varsayılanlar)."""
    if "rate" not in meta:
        meta["rate"] = meta.pop("rating", 0)
    try:
        meta["rate"] = float(meta["rate"] or 0)
    except (TypeError, ValueError):
        meta["rate"] = 0.0
    for key in ("year", "tmdb_id"):
        try:
            meta[key] = int(meta.get(key) or 0)
        except (TypeError, ValueError):
            meta[key] = 0

    for key, default in (
        ("imdb_id", ""), ("description", ""), ("backdrop", ""), ("logo", ""),
        ("cast", []), ("runtime", ""), ("genres", []),
        ("title_tr", ""), ("title_de", ""),
        ("description_tr", ""), ("description_de", ""),
        ("genres_tr", []), ("genres_de", []),
        ("poster_tr", ""), ("backdrop_tr", ""), ("logo_tr", ""),
        ("poster_de", ""), ("backdrop_de", ""), ("logo_de", ""),
        ("original_language", None), ("collection_id", None),
        ("certification_tr", None), ("certification_de", None),
        ("certification_us", None),
    ):
        meta.setdefault(key, default)

    if meta.get("media_type", "movie") != "movie":
        for key, default in (
            ("season_number", 1), ("episode_number", 1),
            ("episode_title", ""), ("episode_title_tr", ""), ("episode_title_de", ""),
            ("episode_backdrop", ""), ("episode_overview", ""),
            ("episode_overview_tr", ""), ("episode_overview_de", ""),
            ("episode_released", ""),
        ):
            meta.setdefault(key, default)
    return meta


async def _already_added(server_id: str, path: str):
    key = f"{server_id}:{path}"
    for st in _storages():
        try:
            doc = await st[APPROVED_COLLECTION].find_one({"webdav_key": key})
        except Exception:
            continue
        if doc:
            return doc
    return None


def _queue_announcement(meta: dict, file_name: str) -> None:
    """
    Telegram'dan gelen içerikte (reciever.py) yapıldığı gibi, yeni içeriği
    duyuru kuyruğuna ekler. Ayarlardan kapalıysa, imdb_id boşsa veya aynı başlık
    18 saat sınırı burada uygulanmaz (webdav.html'de işaretlendiyse gönderilir).
    """
    try:
        from Backend.helper.content_announcer import announce_new_content
        info = dict(meta)
        info["source_filename"] = file_name  # cam/telesync tespiti bu alana bakar
        info["announce_resolution"] = meta.get("quality")  # duyuruda "Çözünürlük" satırı
        info.setdefault("db_index", db.current_db_index)
        info["_force_announce"] = True   # webdav.html: 18 saat sınırı aranmaz
        announce_new_content(info)
    except Exception as e:
        LOGGER.warning("[webdav] Duyuru tetiklenemedi: %s", e)


def _trigger_reminders(meta: dict, file_name: str) -> None:
    """
    Telegram'dan gelen içerikte (reciever.py) yapıldığı gibi, yeni eklenen
    dizi bölümü / film için "hatırlatma" abonelerine bildirim tamponlar.
    Bildirim sistemi kendi tampon/debounce mekanizmasına sahiptir; burada
    yalnızca görev planlanır, hata olursa ekleme akışı bozulmaz.
    """
    media_type = meta.get("media_type", "movie")
    tmdb_id = meta.get("tmdb_id")
    if not tmdb_id:
        LOGGER.warning("[webdav] Hatırlatma atlandı: tmdb_id eksik (%s)", file_name)
        return

    try:
        from Backend.fastapi.routes.notification_routes import (
            send_movie_reminder_notifications,
            send_tv_reminder_notifications,
        )

        db_index = int(meta.get("db_index") or db.current_db_index)
        title = meta.get("title_tr") or meta.get("title") or file_name
        poster = meta.get("poster", "")

        if media_type == "tv":
            LOGGER.info(
                "[webdav] TV hatırlatma tampona alınıyor: tmdb_id=%s s=%s e=%s",
                tmdb_id, meta.get("season_number"), meta.get("episode_number"),
            )
            asyncio.create_task(
                send_tv_reminder_notifications(
                    tmdb_id=int(tmdb_id),
                    db_index=db_index,
                    title=title,
                    poster=poster,
                    new_season=meta.get("season_number"),
                    new_episode=meta.get("episode_number"),
                )
            )
        elif media_type == "movie":
            quality_label = meta.get("quality", "") or ""
            # Dosya adında "german" / "cam" geçiyorsa kalite etiketine yansıt
            # (reciever.py ile birebir aynı kural).
            raw = (file_name or "").lower()
            has_german = bool(re.search(r"\bgerman\b", raw))
            has_camrip = bool(re.search(r"\bcam[-_]?rip\b|\bcamrip\b|\bcam\b", raw))
            if has_german and has_camrip:
                quality_label = "GermanCamRip"
            elif has_german:
                quality_label = f"German:{quality_label}" if quality_label else "German"

            LOGGER.info(
                "[webdav] Film hatırlatma tampona alınıyor: tmdb_id=%s kalite=%r",
                tmdb_id, quality_label,
            )
            asyncio.create_task(
                send_movie_reminder_notifications(
                    tmdb_id=int(tmdb_id),
                    db_index=db_index,
                    title=title,
                    poster=poster,
                    quality_label=quality_label,
                )
            )
    except Exception as e:
        LOGGER.warning("[webdav] Hatırlatma bildirimi başlatılamadı: %s", e)


def _trigger_catalog_refresh() -> None:
    """Katalog yenilemesini (15 dk debounce) planlar — Telegram akışıyla aynı."""
    try:
        from Backend.helper.platform_catalog import platform_catalog
        platform_catalog.schedule_refresh()
    except Exception as e:
        LOGGER.warning("[webdav] Katalog yenileme planlanamadı: %s", e)


async def _add_media(server: dict, path: str, meta: dict, announce: bool = True) -> dict:
    """
    Tek dosyayı katalog + webdav_approved'a yazar.
    Dönüş: {"status": "ok"|"duplicate"|"error", ...}
    """
    sid = server["_id"]
    existing = await _already_added(sid, path)
    if existing:
        return {"status": "duplicate", "title": existing.get("title") or existing.get("file_name")}

    size_bytes = await dav.stat_size(server, path)
    if not size_bytes:
        return {"status": "error", "error": "Dosya bulunamadı veya boyut alınamadı"}
    size_str = _human_size(size_bytes)

    meta = _normalize_meta(dict(meta))
    encoded = await encode_string({"webdav_id": sid, "webdav_path": path})
    meta["encoded_string"] = encoded

    file_name = PurePosixPath(path).name
    display_name = remove_urls(file_name)
    if PurePosixPath(display_name).suffix.lower() not in dav.VIDEO_EXTS:
        display_name += ".mkv"

    inserted = await db.insert_media(
        meta, channel=0, msg_id=0, size=size_str, name=display_name, size_bytes=size_bytes,
    )
    if not inserted:
        return {"status": "error", "error": "Veritabanına yazılamadı"}

    storage = _current_storage()
    if storage is not None:
        try:
            await storage[APPROVED_COLLECTION].insert_one({
                "webdav_key": f"{sid}:{path}",
                "server_id": sid,
                "server_name": server.get("name", ""),
                "path": path,
                "file_name": file_name,
                "title": meta.get("title") or file_name,
                "media_type": meta.get("media_type", "movie"),
                "db_id": encoded,
                "size": size_str,
                "added_at": int(time.time()),
            })
        except Exception:
            LOGGER.warning("[webdav] approved kaydı yazılamadı", exc_info=True)

    # Telegram akışıyla aynı sıra: hatırlatma → katalog yenileme → duyuru
    _trigger_reminders(meta, file_name)
    _trigger_catalog_refresh()

    if announce:
        _queue_announcement(meta, file_name)

    return {
        "status": "ok",
        "title": meta.get("title") or file_name,
        "type": meta.get("media_type", "movie"),
        "size": size_str,
    }


# ── Sunucu profilleri ─────────────────────────────────────────────────────────

@router.get("/sunucular")
async def servers_list():
    return {"servers": await dav.list_servers()}


@router.post("/sunucu")
async def server_save(request: Request):
    body = await _json(request)
    if body is None:
        return _err("Geçersiz JSON")
    try:
        return {"server": await dav.save_server(body)}
    except dav.WebDAVError as e:
        return _err(str(e))
    except Exception:
        _logger.error("WebDAV sunucu kaydı hatası", exc_info=True)
        return _err("Sunucu hatası", 500)


@router.delete("/sunucu")
async def server_delete(request: Request):
    body = await _json(request)
    sid = (body or {}).get("id", "")
    if not sid:
        return _err("id gerekli")
    if any(j["state"] == "running" for j in _JOBS.values()):
        return _err("Devam eden bir toplu ekleme işi var, bitmesini bekleyin", 409)

    # Önce bu sunucuya ait tüm içerikleri kataloğdan ve webdav_approved'dan kaldır.
    # delete_media_by_stream_id son kalite/bölüm silinince bölümü, sezonu ve
    # (hiç içerik kalmadıysa) filmi/diziyi de siler.
    removed = failed = 0
    try:
        for st in _storages():
            col = st[APPROVED_COLLECTION]
            async for doc in col.find({"server_id": sid}):
                try:
                    if doc.get("db_id"):
                        await db.delete_media_by_stream_id(doc["db_id"])
                    await col.delete_one({"_id": doc["_id"]})
                    removed += 1
                except Exception:
                    failed += 1
                    LOGGER.warning("[webdav] sunucu silinirken içerik kaldırılamadı: %s",
                                   doc.get("path"), exc_info=True)
    except Exception:
        _logger.error("WebDAV sunucu silme temizliği hatası", exc_info=True)
        return _err("İçerikler temizlenirken hata oluştu; sunucu silinmedi", 500)

    if failed:
        # Kayıtları yetim bırakmamak için sunucuyu silme; kullanıcı tekrar denesin
        return _err(f"{failed} içerik kaldırılamadı; sunucu silinmedi. Tekrar deneyin.", 500)

    ok = await dav.delete_server(sid)
    return {"ok": ok, "removed": removed}


@router.post("/test")
async def server_test(request: Request):
    """Kayıtlı profili (id) veya formdaki henüz kaydedilmemiş değerleri test eder."""
    body = await _json(request)
    if body is None:
        return _err("Geçersiz JSON")
    try:
        if body.get("id"):
            server = await _server_or_none(body["id"])
            if not server:
                return _err("Sunucu bulunamadı", 404)
            # Formdan gelen (kaydedilmemiş) değişiklikler varsa üstüne yaz
            for k in ("url", "username", "auth_type", "verify_ssl", "root"):
                if k in body and body[k] not in (None, ""):
                    server = {**server, k: body[k]}
            if body.get("password"):
                server = {**server, "password": body["password"]}
            if "url" in body:
                server["url"] = dav._validate_url(server["url"])
            server["root"] = dav._clean_root(server.get("root", ""))
        else:
            server = {
                "_id": f"test-{secrets.token_hex(4)}",
                "url": dav._validate_url(body.get("url", "")),
                "username": (body.get("username") or "").strip(),
                "password": body.get("password") or "",
                "auth_type": body.get("auth_type", "basic"),
                "verify_ssl": bool(body.get("verify_ssl", True)),
                "root": dav._clean_root(body.get("root", "")),
            }
        try:
            return await dav.test_connection(server)
        finally:
            if server["_id"].startswith("test-") or body.get("id"):
                dav._clients.pop(server["_id"], None)
    except dav.WebDAVError as e:
        return _err(str(e))
    except Exception:
        _logger.error("WebDAV test hatası", exc_info=True)
        return _err("Sunucu hatası", 500)


# ── Gezinme / tarama ──────────────────────────────────────────────────────────

@router.get("/listele")
async def browse(request: Request):
    server = await _server_or_none(request.query_params.get("server", ""))
    if not server:
        return _err("Sunucu bulunamadı", 404)
    try:
        result = await dav.list_dir(server, request.query_params.get("path", ""))
    except dav.WebDAVError as e:
        return _err(str(e), 502)
    except Exception:
        _logger.error("WebDAV listele hatası", exc_info=True)
        return _err("Sunucu hatası", 500)

    added = set()
    for it in result["items"]:
        if not it["is_dir"] and await _already_added(server["_id"], it["path"]):
            added.add(it["path"])
    for it in result["items"]:
        it["added"] = it["path"] in added
    return result


@router.post("/tara")
async def scan(request: Request):
    """Klasörü özyinelemeli tarar; her video için önerilen arama adını da döner."""
    body = await _json(request)
    if body is None:
        return _err("Geçersiz JSON")
    server = await _server_or_none(body.get("server", ""))
    if not server:
        return _err("Sunucu bulunamadı", 404)
    try:
        files = await dav.walk_videos(server, body.get("path", ""))
    except dav.WebDAVError as e:
        return _err(str(e), 502)
    except Exception:
        _logger.error("WebDAV tarama hatası", exc_info=True)
        return _err("Sunucu hatası", 500)

    for f in files:
        f["added"] = bool(await _already_added(server["_id"], f["path"]))
        f["search_name"] = _meta_name(f["path"])
    return {"files": files, "count": len(files), "truncated": len(files) >= 5000}


# ── Tek dosya: sorgula → onayla ───────────────────────────────────────────────

@router.post("/meta-sorgu")
async def meta_query(request: Request):
    body = await _json(request)
    if body is None:
        return _err("Geçersiz JSON")
    server = await _server_or_none(body.get("server", ""))
    path = (body.get("path") or "").strip().strip("/")
    if not server or not path:
        return _err("server ve path gerekli")

    custom = (body.get("custom_name") or "").strip()
    try:
        if custom:
            override_id, _ = extract_default_id(custom)
            meta = await fetch_metadata(clean_filename(custom), 0, 0, override_id=override_id)
        else:
            meta = await _query_meta(path)
        size = await dav.stat_size(server, path)
    except Exception:
        _logger.error("WebDAV metadata hatası", exc_info=True)
        return _err("Sunucu hatası", 500)

    if not meta:
        return JSONResponse({"error": "Metadata bulunamadı", "file_name": PurePosixPath(path).name}, status_code=404)
    return {"meta": meta, "file_name": PurePosixPath(path).name, "size": size, "path": path}


@router.post("/ekle")
async def add_one(request: Request):
    body = await _json(request)
    if body is None:
        return _err("Geçersiz JSON")
    server = await _server_or_none(body.get("server", ""))
    path = (body.get("path") or "").strip().strip("/")
    meta = body.get("meta")
    if not server or not path or not isinstance(meta, dict):
        return _err("server, path ve meta gerekli")
    try:
        res = await _add_media(server, path, meta)
    except dav.WebDAVError as e:
        return _err(str(e), 502)
    except Exception:
        _logger.error("WebDAV ekleme hatası", exc_info=True)
        return _err("Sunucu hatası", 500)

    if res["status"] == "duplicate":
        return _err(f"Bu dosya zaten eklendi: {res.get('title', '?')}", 409)
    if res["status"] == "error":
        return _err(res.get("error", "Ekleme başarısız"), 500)
    return {"status": "success", **res}


# ── Toplu ekleme (arka plan işi) ──────────────────────────────────────────────

_JOBS: dict = {}


def _prune_jobs() -> None:
    finished = [k for k, v in _JOBS.items() if v["state"] != "running"]
    for k in sorted(finished, key=lambda k: _JOBS[k]["started"])[:-5]:
        _JOBS.pop(k, None)


_TASKS: set = set()


def _spawn(coro) -> None:
    """Arka plan görevini referansını tutarak başlat (GC ile yarıda kesilmesin)."""
    t = asyncio.create_task(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)


def _new_job(total: int, phase: str = "adding", **extra) -> dict:
    _prune_jobs()
    job_id = secrets.token_hex(6)
    _JOBS[job_id] = {
        "id": job_id, "state": "running", "phase": phase, "total": total, "done": 0,
        "added": 0, "skipped": 0, "no_meta": 0, "failed": 0,
        "current": "", "log": [], "started": time.time(), "error": "", "truncated": False,
        **extra,
    }
    return _JOBS[job_id]


async def _run_bulk(job_id: str, server: dict, items: list, announce: bool = True) -> None:
    """
    items: [{"path": str, "name": Optional[str]}]  (name = metadata arama adı)
    Zaten eklenmiş dosyalar metadata sorgusu yapılmadan atlanır; böylece aynı
    klasör tekrar eklendiğinde yalnızca yeni dosyalar işlenir.
    """
    job = _JOBS[job_id]
    override_id = job.get("override_id") or None
    for it in items:
        path = it["path"]
        if job.get("cancel"):
            break
        job["current"] = path
        slept = False
        try:
            if await _already_added(server["_id"], path):
                job["skipped"] += 1
            else:
                meta = await _query_meta(path, name=it.get("name"), override_id=override_id)
                slept = True
                if not meta:
                    job["no_meta"] += 1
                    job["log"].append({"path": path, "status": "no_meta"})
                else:
                    res = await _add_media(server, path, meta, announce=announce)
                    st = res["status"]
                    if st == "ok":
                        job["added"] += 1
                    elif st == "duplicate":
                        job["skipped"] += 1
                    else:
                        job["failed"] += 1
                    if st != "duplicate":
                        job["log"].append({"path": path, "status": st, "title": res.get("title"),
                                           "error": res.get("error")})
        except Exception:
            LOGGER.error("[webdav] toplu ekleme hatası: %s", path, exc_info=True)
            job["failed"] += 1
            job["log"].append({"path": path, "status": "error", "error": "İşlenemedi"})
        job["done"] += 1
        if slept:
            await asyncio.sleep(0.2)  # TMDB / DB üzerinde nazik ol
    job["current"] = ""
    job["phase"] = "done"
    job["state"] = "cancelled" if job.get("cancel") else "finished"


async def _run_folder(job_id: str, server: dict, base: str, kind: str, show: str,
                      announce: bool) -> None:
    """Klasörü özyinelemeli tara, ardından bulunan tüm videoları ekle."""
    job = _JOBS[job_id]
    try:
        files = await dav.walk_videos(server, base, max_files=MAX_FOLDER)
    except dav.WebDAVError as e:
        job.update(state="error", phase="done", error=str(e))
        return
    except Exception:
        LOGGER.error("[webdav] klasör tarama hatası: %s", base, exc_info=True)
        job.update(state="error", phase="done", error="Klasör taranamadı")
        return

    job["truncated"] = len(files) >= MAX_FOLDER
    items = [{"path": f["path"], "name": _folder_search_name(f["path"], kind, show)} for f in files]
    job["total"] = len(items)
    job["phase"] = "adding"
    if not items:
        job.update(state="finished", phase="done")
        return
    await _run_bulk(job_id, server, items, announce=announce)


@router.post("/toplu-ekle")
async def bulk_add(request: Request):
    body = await _json(request)
    if body is None:
        return _err("Geçersiz JSON")
    server = await _server_or_none(body.get("server", ""))
    paths = body.get("paths")
    if not server or not isinstance(paths, list) or not paths:
        return _err("server ve paths gerekli")
    if any(j["state"] == "running" for j in _JOBS.values()):
        return _err("Devam eden bir toplu ekleme işi var, bitmesini bekleyin", 409)

    clean_paths = []
    for p in paths[:MAX_BULK]:
        if isinstance(p, str) and p.strip():
            try:
                clean_paths.append(dav._norm_path(p))
            except dav.WebDAVError:
                continue
    if not clean_paths:
        return _err("Geçerli yol yok")

    job = _new_job(len(clean_paths))
    _spawn(_run_bulk(job["id"], server, [{"path": p} for p in clean_paths],
                     announce=bool(body.get("announce", True))))
    return {"job_id": job["id"], "total": len(clean_paths), "capped": len(paths) > MAX_BULK}


@router.post("/klasor-ekle")
async def folder_add(request: Request):
    """
    Bir klasörü (alt klasörleriyle birlikte) tek istekle ekler.

    body: {
      server, path,
      kind:        "auto" | "tv" | "movie"   (varsayılan auto)
      show_name:   (tv için isteğe bağlı) dizi adı; boşsa klasör adlarından bulunur
      override_id: (isteğe bağlı) TMDB linki / IMDb id — tüm dosyalara uygulanır
      announce:    yeni içerikleri duyur (varsayılan true)
    }
    İlerleme: GET /is/{job_id}  (phase = "scanning" → "adding" → "done")
    """
    body = await _json(request)
    if body is None:
        return _err("Geçersiz JSON")
    server = await _server_or_none(body.get("server", ""))
    if not server:
        return _err("Sunucu bulunamadı", 404)
    try:
        base = dav._norm_path(body.get("path", ""))
    except dav.WebDAVError as e:
        return _err(str(e))

    kind = (body.get("kind") or "auto").strip().lower()
    if kind not in _KINDS:
        return _err("Geçersiz tür (auto, tv veya movie olmalı)")
    show = (body.get("show_name") or "").strip()[:200]
    override_id = (body.get("override_id") or "").strip()[:200]

    if any(j["state"] == "running" for j in _JOBS.values()):
        return _err("Devam eden bir toplu ekleme işi var, bitmesini bekleyin", 409)

    job = _new_job(0, phase="scanning", path=base, kind=kind, override_id=override_id)
    _spawn(_run_folder(job["id"], server, base, kind, show, bool(body.get("announce", True))))
    return {"job_id": job["id"], "path": base, "kind": kind}


@router.get("/is/{job_id}")
async def job_status(job_id: str, since: int = 0):
    job = _JOBS.get(job_id)
    if not job:
        return _err("İş bulunamadı", 404)
    return {**{k: v for k, v in job.items() if k != "log"}, "log": job["log"][since:], "log_offset": since}


@router.post("/is-iptal")
async def job_cancel(request: Request):
    body = await _json(request)
    job = _JOBS.get((body or {}).get("job_id", ""))
    if not job:
        return _err("İş bulunamadı", 404)
    job["cancel"] = True
    return {"ok": True}


# ── Senkronizasyon: WebDAV'dan silinenleri katalogdan kaldır ──────────────────

@router.post("/sync")
async def sync(request: Request):
    """
    body: { server?: <profil id>, dry_run?: bool, force?: bool }
      dry_run=true → hiçbir şey silinmez, silinecekler listelenir
      force=true   → toplu silme güvenlik eşiğini yok say
    """
    body = (await _json(request)) or {}
    server_id = (body.get("server") or "").strip() or None

    from Backend.helper.webdav_sync import sync_webdav
    try:
        report = await sync_webdav(
            server_id=server_id,
            dry_run=bool(body.get("dry_run", False)),
            force=bool(body.get("force", False)),
        )
    except Exception:
        _logger.error("WebDAV sync hatası", exc_info=True)
        return _err("Sunucu hatası", 500)

    if report.get("error"):
        return _err(report["error"], 409)

    # Yanıtı küçük tut: sunucu başına en fazla SYNC_PREVIEW_LIMIT kayıt göster
    for entry in report["servers"]:
        entry["missing_count"] = len(entry["missing"])
        entry["missing"] = entry["missing"][:SYNC_PREVIEW_LIMIT]
        entry["unknown_count"] = len(entry["unknown"])
        entry["unknown"] = entry["unknown"][:SYNC_PREVIEW_LIMIT]
    return report


# ── Eklenmiş içerikler ────────────────────────────────────────────────────────

@router.get("/db-listele")
async def db_list(request: Request):
    try:
        page = max(0, int(request.query_params.get("page", "0")))
    except ValueError:
        page = 0
    try:
        items = []
        for st in _storages():
            try:
                items += await st[APPROVED_COLLECTION].find({}).to_list(length=None)
            except Exception:
                continue
        items.sort(key=lambda d: d.get("added_at", 0), reverse=True)
        total = len(items)
        chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        for it in chunk:
            it["_id"] = str(it["_id"])
        return {"items": chunk, "total": total, "page": page, "page_size": PAGE_SIZE}
    except Exception:
        _logger.error("WebDAV db-listele hatası", exc_info=True)
        return _err("Sunucu hatası", 500)


@router.delete("/db-sil")
async def db_delete(request: Request):
    body = await _json(request)
    doc_id = ((body or {}).get("doc_id") or "").strip()
    if not doc_id:
        return _err("doc_id gerekli")

    from bson import ObjectId
    try:
        oid = ObjectId(doc_id)
    except Exception:
        return _err("Geçersiz doc_id")

    try:
        for st in _storages():
            col = st[APPROVED_COLLECTION]
            doc = await col.find_one({"_id": oid})
            if not doc:
                continue
            if doc.get("db_id"):
                try:
                    await db.delete_media_by_stream_id(doc["db_id"])
                except Exception:
                    LOGGER.warning("[webdav] katalog girdisi silinemedi", exc_info=True)
            await col.delete_one({"_id": oid})
            return {"status": "success", "message": f"'{doc.get('title', doc.get('file_name', '?'))}' kaldırıldı."}
        return _err("Kayıt bulunamadı", 404)
    except Exception:
        _logger.error("WebDAV silme hatası", exc_info=True)
        return _err("Sunucu hatası", 500)
