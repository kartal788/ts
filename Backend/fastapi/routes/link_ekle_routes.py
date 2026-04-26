import logging
_logger = logging.getLogger(__name__)
"""
Link Ekleme API Route'ları
- /api/link-ekle/query  → URL'den dosya boyutu + adı al, metadata sorgula
- /api/link-ekle/save   → Onaylanan metadata'yı veritabanına yaz
"""

import re
import ipaddress
import socket
import aiohttp
from urllib.parse import urlparse
from fastapi import Request, Depends
from Backend.fastapi.security.credentials import require_auth
from Backend.helper.metadata import metadata as fetch_metadata
from Backend import db

# ── SSRF Koruması ─────────────────────────────────────────────────────────────
_SSRF_BLOCKED_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

def _is_safe_url(url: str) -> tuple[bool, str]:
    """
    URL'nin dahili ağlara veya meta-data servislerine yönlendirmediğini doğrular.
    Döner: (güvenli_mi, hata_mesajı)
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Geçersiz URL"

    if parsed.scheme not in ("https",):
        return False, "Yalnızca HTTPS desteklenmektedir"

    hostname = parsed.hostname
    if not hostname:
        return False, "Geçersiz host"

    port = parsed.port
    if port is not None and port not in (80, 443):
        return False, f"İzin verilmeyen port: {port}"

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False, "Host çözümlenemedi"

    for info in infos:
        addr_str = info[4][0]
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        for blocked in _SSRF_BLOCKED_NETS:
            if addr in blocked:
                return False, f"Dahili adrese erişim engellendi: {addr_str}"

    return True, ""


# ──────────────────────────────────────────────
# Yardımcı: dosya boyutunu okunabilir stringe çevir
# ──────────────────────────────────────────────
def _human_size(n_bytes: int) -> str:
    if n_bytes <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} PB"


# ──────────────────────────────────────────────
# Yardımcı: Pixeldrain URL dönüştürme
# ──────────────────────────────────────────────
def _normalize_url(url: str) -> str:
    """https://pixeldrain.com/u/XXXX  →  https://pixeldrain.com/api/file/XXXX"""
    m = re.match(r"^https://pixeldrain\.com/u/([A-Za-z0-9]+)$", url.strip())
    if m:
        return f"https://pixeldrain.com/api/file/{m.group(1)}"
    return url.strip()


# ──────────────────────────────────────────────
# Yardımcı: URL'den HEAD isteği ile dosya bilgisi al
# ──────────────────────────────────────────────
async def _fetch_file_info(url: str) -> dict:
    """
    Döndürür:
      filename  – Content-Disposition veya URL yolundan
      file_size – okunabilir string (örn. "4.2 GB")
      raw_size  – int (bytes)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MetaBot/1.0)",
        "Accept": "*/*",
    }
    timeout = aiohttp.ClientTimeout(total=30)

    # Pixeldrain API → JSON yanıt döner, HEAD yerine GET daha güvenilir
    is_pixeldrain_api = "pixeldrain.com/api/file/" in url

    async with aiohttp.ClientSession(timeout=timeout) as session:
        if is_pixeldrain_api:
            # Pixeldrain API info endpoint: /api/file/{id}/info
            info_url = url.rstrip("/") + "/info"
            async with session.get(info_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    raw_size = data.get("size", 0)
                    filename = data.get("name", "")
                    return {
                        "filename": filename,
                        "file_size": _human_size(raw_size),
                        "raw_size": raw_size,
                    }
                # Fallback: HEAD isteği
        
        # HEAD ile Content-Length ve Content-Disposition dene
        try:
            async with session.head(url, headers=headers, allow_redirects=True) as resp:
                raw_size = int(resp.headers.get("Content-Length", 0))
                cd = resp.headers.get("Content-Disposition", "")
                filename = ""
                if cd:
                    m = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, re.IGNORECASE)
                    if m:
                        filename = m.group(1).strip().strip('"\'')
                if not filename:
                    # URL'nin son segmentinden al
                    path = url.split("?")[0].rstrip("/")
                    filename = path.split("/")[-1]
        except Exception:
            # HEAD başarısız olursa GET ile sadece header oku
            async with session.get(url, headers={**headers, "Range": "bytes=0-0"}, allow_redirects=True) as resp:
                raw_size = 0
                cr = resp.headers.get("Content-Range", "")
                if cr:
                    m = re.search(r"/(\d+)$", cr)
                    if m:
                        raw_size = int(m.group(1))
                if not raw_size:
                    raw_size = int(resp.headers.get("Content-Length", 0))
                cd = resp.headers.get("Content-Disposition", "")
                filename = ""
                if cd:
                    m2 = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, re.IGNORECASE)
                    if m2:
                        filename = m2.group(1).strip().strip('"\'')
                if not filename:
                    path = url.split("?")[0].rstrip("/")
                    filename = path.split("/")[-1]

        return {
            "filename": filename,
            "file_size": _human_size(raw_size),
            "raw_size": raw_size,
        }


# ──────────────────────────────────────────────
# POST /api/link-ekle/query
# ──────────────────────────────────────────────
async def link_ekle_query(request: Request, _: bool = Depends(require_auth)):
    """
    Gelen JSON:
      { "url": "https://...", "custom_filename": "Firebrand 2024 1080p.mkv" | null }

    Dönen JSON (başarılı):
      {
        "url", "filename", "file_size", "raw_size",
        "title", "title_tr", "title_de",
        "imdb_id", "tmdb_id",
        "poster", "year", "quality",
        "media_type",   # "movie" | "tv"
        "season", "episode",
        "metadata"      # metadata() den dönen ham dict
      }
    """
    try:
        body = await request.json()
    except Exception:
        return {"error": "Geçersiz JSON gövdesi"}

    raw_url: str = body.get("url", "").strip()
    custom_filename: str | None = body.get("custom_filename") or None

    if not raw_url:
        return {"error": "URL gerekli"}

    url = _normalize_url(raw_url)

    # SSRF Koruması: dahili ağlara erişimi engelle
    _safe_ok, _safe_err = _is_safe_url(url)
    if not _safe_ok:
        return {"error": f"Güvenlik hatası: {_safe_err}"}

    # 1. Dosya bilgisini al
    try:
        file_info = await _fetch_file_info(url)
    except Exception as e:
        _logger.error("Dosya bilgisi alınamadı", exc_info=True)

        return {"error": "Sunucu hatası"}

    # 2. Hangi dosya adını metadata için kullanacağız?
    if custom_filename:
        meta_filename = custom_filename
    else:
        meta_filename = file_info["filename"]

    if not meta_filename:
        return {"error": "Dosya adı alınamadı ve custom_filename verilmedi"}

    # 3. metadata() ile sorgula
    # channel=0, msg_id=0 → link tabanlı eklemede Telegram bilgisi yok
    try:
        meta = await fetch_metadata(filename=meta_filename, channel=0, msg_id=0)
    except Exception as e:
        _logger.error("Metadata sorgusu başarısız", exc_info=True)

        return {"error": "Sunucu hatası"}

    if not meta:
        return {"error": f"Metadata bulunamadı: {meta_filename!r}"}

    return {
        "url": url,
        "original_url": raw_url,
        "filename": meta_filename,
        "file_size": file_info["file_size"],
        "raw_size": file_info["raw_size"],
        # Metadata alanları
        "title": meta.get("title"),
        "title_tr": meta.get("title_tr"),
        "title_de": meta.get("title_de"),
        "imdb_id": meta.get("imdb_id"),
        "tmdb_id": meta.get("tmdb_id"),
        "poster": meta.get("poster"),
        "year": meta.get("year"),
        "quality": meta.get("quality"),
        "media_type": meta.get("media_type"),
        "season": meta.get("season_number"),
        "episode": meta.get("episode_number"),
        "metadata": meta,
    }


# ──────────────────────────────────────────────
# POST /api/link-ekle/save
# ──────────────────────────────────────────────
async def link_ekle_save(request: Request, _: bool = Depends(require_auth)):
    """
    Onaylanan veriyi veritabanına yazar.

    Gelen JSON: link_ekle_query'nin döndürdüğü dict
    (içinde metadata, filename, file_size, url vs.)

    Döner: { "ok": true } veya { "error": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        return {"error": "Geçersiz JSON gövdesi"}

    meta: dict = body.get("metadata")
    if not meta:
        return {"error": "metadata alanı eksik"}

    filename: str = body.get("filename", "")
    file_size: str = body.get("file_size", "")
    url: str = body.get("url", "")

    # id alanına her zaman direkt URL yazılır (Telegram encoded string değil)
    # original_url varsa onu kullan (pixeldrain için dönüştürülmemiş hali),
    # yoksa normalize edilmiş url'yi kullan.
    stream_url = body.get("original_url") or url

    try:
        result = await db.insert_media(
            metadata_info={**meta, "encoded_string": stream_url},
            channel=meta.get("chat_id", 0),
            msg_id=meta.get("msg_id", 0),
            size=file_size,
            name=filename,
        )
        if result:
            # ── TV dizisi ise hatırlatma bildirimlerini tetikle ──────────────
            if meta.get("media_type") == "tv":
                try:
                    from Backend.fastapi.routes.notification_routes import (
                        send_tv_reminder_notifications,
                    )
                    import asyncio as _asyncio

                    tmdb_id  = meta.get("tmdb_id")
                    db_index = meta.get("db_index") or 0
                    title    = (
                        meta.get("title_tr")
                        or meta.get("title")
                        or filename
                    )
                    poster   = meta.get("poster", "")
                    season   = meta.get("season_number")
                    episode  = meta.get("episode_number")

                    if tmdb_id is not None:
                        _logger.info(
                            "TV hatırlatma tampona alınıyor: tmdb_id=%s s=%s e=%s",
                            tmdb_id, season, episode,
                        )
                        try:
                            loop = _asyncio.get_event_loop()
                            loop.create_task(
                                send_tv_reminder_notifications(
                                    tmdb_id=int(tmdb_id),
                                    db_index=int(db_index),
                                    title=title,
                                    poster=poster,
                                    new_season=season,
                                    new_episode=episode,
                                )
                            )
                        except RuntimeError:
                            _asyncio.ensure_future(
                                send_tv_reminder_notifications(
                                    tmdb_id=int(tmdb_id),
                                    db_index=int(db_index),
                                    title=title,
                                    poster=poster,
                                    new_season=season,
                                    new_episode=episode,
                                )
                            )
                    else:
                        _logger.warning(
                            "Hatırlatma atlandı: tmdb_id=%s eksik", tmdb_id,
                        )
                except Exception as _notif_err:
                    _logger.warning(
                        "Hatırlatma bildirimi başlatılamadı: %s", _notif_err, exc_info=True
                    )
            # ── Film ise hatırlatma bildirimlerini tetikle ───────────────────
            elif meta.get("media_type") == "movie":
                try:
                    from Backend.fastapi.routes.notification_routes import (
                        send_movie_reminder_notifications,
                    )
                    import asyncio as _asyncio

                    tmdb_id       = meta.get("tmdb_id")
                    db_index      = meta.get("db_index") or 0
                    title         = (
                        meta.get("title_tr")
                        or meta.get("title")
                        or filename
                    )
                    poster        = meta.get("poster", "")
                    quality_label = meta.get("quality", "")

                    # Dosya adında "german" geçiyorsa kalite etiketine ekle
                    import re as _re_german
                    _raw_fn = (filename or "").lower()
                    _has_german = bool(_re_german.search(r'\bgerman\b', _raw_fn))
                    _has_camrip = bool(_re_german.search(r'\bcam[-_]?rip\b|\bcamrip\b|\bcam\b', _raw_fn))
                    if _has_german and _has_camrip:
                        quality_label = "GermanCamRip"
                    elif _has_german:
                        quality_label = f"German:{quality_label}" if quality_label else "German"

                    if tmdb_id is not None:
                        _logger.info(
                            "Film hatırlatma tampona alınıyor: tmdb_id=%s kalite=%r",
                            tmdb_id, quality_label,
                        )
                        try:
                            loop = _asyncio.get_event_loop()
                            loop.create_task(
                                send_movie_reminder_notifications(
                                    tmdb_id=int(tmdb_id),
                                    db_index=int(db_index),
                                    title=title,
                                    poster=poster,
                                    quality_label=quality_label,
                                )
                            )
                        except RuntimeError:
                            _asyncio.ensure_future(
                                send_movie_reminder_notifications(
                                    tmdb_id=int(tmdb_id),
                                    db_index=int(db_index),
                                    title=title,
                                    poster=poster,
                                    quality_label=quality_label,
                                )
                            )
                    else:
                        _logger.warning(
                            "Film hatırlatma atlandı: tmdb_id=%s eksik", tmdb_id,
                        )
                except Exception as _notif_err:
                    _logger.warning(
                        "Film hatırlatma bildirimi başlatılamadı: %s", _notif_err, exc_info=True
                    )
            # ────────────────────────────────────────────────────────────────
            return {"ok": True, "id": str(result)}
        else:
            return {"error": "Veritabanına yazılamadı (sonuç None)"}
    except Exception as e:
        _logger.error("Veritabanı hatası", exc_info=True)

        return {"error": "Sunucu hatası"}
