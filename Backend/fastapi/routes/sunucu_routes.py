import logging
_logger = logging.getLogger(__name__)
"""
Sunucu Yönetimi API Route'ları
- GET  /api/sunucu/yukle-stream        → SSE: HTTPS'den indirme ilerlemesi + arşiv çıkarma
- POST /api/sunucu/bilgisayardan-yukle → Bilgisayardan dosya yükleme (multipart)
- GET  /api/sunucu/listele             → Klasör içeriğini listele
- DELETE /api/sunucu/sil               → Dosya veya klasör sil
- PUT  /api/sunucu/yeniden-adlandir    → Dosya/klasör adını değiştir
- POST /api/sunucu/metadata            → Dosya adından metadata sorgula + kaydet
- POST /api/sunucu/klasor-olustur      → Yeni klasör oluştur
"""

import os
import re
import time
import json
import asyncio
import shutil
import zipfile
import tarfile
import aiohttp
import aiofiles
from pathlib import Path
from fastapi import Request, Depends, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from Backend.fastapi.security.credentials import require_auth
from Backend.helper.metadata import metadata as fetch_metadata
from Backend import db, StartTime
from Backend.helper.pyro import get_readable_time
from Backend.logger import LOGGER

# ── Sunucu depolama kök dizini ────────────────────────────────────────────────
_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
SUNUCU_DIR = Path(os.getenv("SUNUCU_DIR", _DEFAULT_DIR))
SUNUCU_DIR.mkdir(parents=True, exist_ok=True)

try:
    import py7zr
    HAS_7Z = True
except ImportError:
    HAS_7Z = False

try:
    import rarfile
    HAS_RAR = True
except ImportError:
    HAS_RAR = False


import ipaddress
import socket
from urllib.parse import urlparse

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

    # Açık port kontrolü: sadece 80 ve 443'e izin ver
    port = parsed.port
    if port is not None and port not in (80, 443):
        return False, f"İzin verilmeyen port: {port}"

    # Host'u IP'ye çözümle ve özel aralıkları engelle
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


def _safe_path(relative: str) -> Path:
    """
    NOT: Eski implementasyon str(p).startswith(str(SUNUCU_DIR)) kullanıyordu.
    Bu, ayraç (separator) eklemeden yapılan bir prefix kontrolüydü ve
    SUNUCU_DIR ile aynı önekli KARDEŞ dizinlere erişime izin veriyordu
    (örn. SUNUCU_DIR="/data/sunucu" iken "../sunucu_GIZLI/x" payload'u
    "/data/sunucu_GIZLI/x" yoluna izin veriyordu çünkü bu yol da
    "/data/sunucu" ile başlıyor). PoC ile doğrulanmış path traversal
    zafiyeti — relative_to() ile kesin (segment bazlı) sınır kontrolüne
    geçildi.
    """
    base = SUNUCU_DIR.resolve()
    p = (base / relative.lstrip("/\\")).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        raise ValueError("Güvenli alan dışı erişim")
    return p


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _human_speed(bps: float) -> str:
    return _human_size(bps) + "/s"


def _human_eta(secs: float) -> str:
    if secs <= 0 or secs > 86400 * 7:
        return "—"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}d {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}s {m:02d}d"


def _entry_info(p: Path, base: Path) -> dict:
    stat = p.stat()
    return {
        "name":     p.name,
        "path":     str(p.relative_to(base)),
        "is_dir":   p.is_dir(),
        "size":     _human_size(stat.st_size) if p.is_file() else None,
        "raw_size": stat.st_size if p.is_file() else None,
        "modified": int(stat.st_mtime),
    }


def _archive_type(filename: str) -> str | None:
    n = filename.lower()
    if n.endswith(".zip"):                                        return "zip"
    if n.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")): return "tar"
    if n.endswith(".7z"):                                         return "7z"
    if n.endswith(".rar"):                                        return "rar"
    return None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Arşiv çıkarma (sync, executor'da çalışır) ────────────────────────────────

def _extract_zip(src: Path, dest: Path):
    """ZIP Slip korumalı çıkarma: üst dizine kaçan yollar reddedilir."""
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(src, "r") as zf:
        for member in zf.infolist():
            # Windows yol ayraçlarını normalleştir
            safe_name = member.filename.replace("\\", "/")
            target = (dest / safe_name).resolve()
            if not str(target).startswith(str(dest_resolved) + "/") and target != dest_resolved:
                raise ValueError(f"ZIP Slip tespit edildi: {member.filename!r}")
        zf.extractall(dest)

def _extract_tar(src: Path, dest: Path):
    """TAR Slip korumalı çıkarma: Python 3.12+ filter, önceki sürümler manuel kontrol."""
    dest_resolved = dest.resolve()
    with tarfile.open(src, "r:*") as tf:
        try:
            # Python 3.12+ — resmi güvenli çıkarma filtresi
            tf.extractall(dest, filter="data")
        except TypeError:
            # Python < 3.12 — manuel kontrol
            for member in tf.getmembers():
                target = (dest / member.name).resolve()
                if not str(target).startswith(str(dest_resolved) + "/") and target != dest_resolved:
                    raise ValueError(f"TAR Slip tespit edildi: {member.name!r}")
            tf.extractall(dest)

def _extract_7z(src: Path, dest: Path):
    import py7zr
    with py7zr.SevenZipFile(src, mode="r") as zf:
        zf.extractall(path=dest)

def _extract_rar(src: Path, dest: Path):
    import rarfile
    with rarfile.RarFile(src) as rf:
        rf.extractall(dest)


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/sunucu/yukle-stream  (SSE)
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_yukle_stream(request: Request, _: bool = Depends(require_auth)):
    """
    Query params: url, dest_path, custom_filename, extract (0|1)

    SSE events:
      info     { filename, total_size, total_str }
      progress { downloaded, total, percent, downloaded_str, total_str, speed, eta, elapsed }
      extract  { status, message, extract_path? }
      meta     { title, title_tr, media_type, year, quality, poster, db_id } | { error }
      done     { filename, size, dest_path, db_id, extracted }
      error    { message }
    """
    url        = request.query_params.get("url", "").strip()
    dest_rel   = request.query_params.get("dest_path", "").strip()
    custom_fn  = request.query_params.get("custom_filename", "").strip() or None
    # Güvenlik: custom_filename yalnızca dosya adı olabilir, yol bileşeni kesinlikle reddedilir
    if custom_fn:
        custom_fn = Path(custom_fn).name.strip() or None
        if custom_fn in (None, ".", ".."):
            custom_fn = None
    do_extract = request.query_params.get("extract", "1") == "1"

    async def generate():
        # ── Google Drive link tespiti ─────────────────────────────────────
        import re as _re2
        _GDRIVE_RE = _re2.compile(
            r"(?:drive\.google\.com/(?:file/d/|open\?id=)|"
            r"docs\.google\.com/(?:document|spreadsheets|presentation)/d/)"
            r"([a-zA-Z0-9_-]{10,})"
        )
        _gdrive_match = _GDRIVE_RE.search(url)
        is_gdrive_url  = bool(_gdrive_match)
        gdrive_file_id = _gdrive_match.group(1) if _gdrive_match else None

        # ── Doğrulama ─────────────────────────────────────────────────────
        if not url:
            yield _sse("error", {"message": "URL gerekli"})
            return
        if not is_gdrive_url and not url.startswith("https://"):
            yield _sse("error", {"message": "Geçerli bir HTTPS URL ya da Google Drive linki gerekli"})
            return

        # SSRF Koruması: dahili ağlara erişimi engelle
        if not is_gdrive_url:
            _safe_ok, _safe_err = _is_safe_url(url)
            if not _safe_ok:
                yield _sse("error", {"message": f"Güvenlik hatası: {_safe_err}"})
                return

        try:
            dest_dir = _safe_path(dest_rel)
        except ValueError as e:
            _logger.error("SSE stream hatası", exc_info=True)

            yield _sse("error", {"message": "İndirme sırasında bir hata oluştu"})
            return

        dest_dir.mkdir(parents=True, exist_ok=True)

        filename   = custom_fn
        total_size = 0
        downloaded = 0

        if is_gdrive_url:
            # ── Google Drive indirme ───────────────────────────────────────
            import pickle
            _gdrive_token = Path(__file__).parent.parent.parent.parent / "gdrive_token.pickle"
            if not _gdrive_token.exists():
                yield _sse("error", {"message": "Google Drive token.pickle bulunamadı. Ayarlar → token.pickle yükle."})
                return
            try:
                from googleapiclient.discovery import build
                from googleapiclient.http import MediaIoBaseDownload
                from google.auth.transport.requests import Request as GRequest
                with open(_gdrive_token, "rb") as _tf:
                    creds = pickle.load(_tf)
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(GRequest())
                    with open(_gdrive_token, "wb") as _tf:
                        pickle.dump(creds, _tf)
                svc = build("drive", "v3", credentials=creds, cache_discovery=False)
            except Exception as e:
                _logger.error("Google Drive kimlik doğrulama hatası", exc_info=True)

                yield _sse("error", {"message": "İşlem sırasında bir hata oluştu"})
                return

            try:
                loop = asyncio.get_event_loop()
                meta = await loop.run_in_executor(
                    None, lambda: svc.files().get(fileId=gdrive_file_id, fields="name,size").execute()
                )
                if not filename:
                    filename = meta.get("name", "dosya")
                total_size = int(meta.get("size", 0))
            except Exception as e:
                _logger.error("Drive dosya bilgisi alınamadı", exc_info=True)

                yield _sse("error", {"message": "İşlem sırasında bir hata oluştu"})
                return

            atype = _archive_type(filename)
            dest_file = dest_dir / filename

            yield _sse("info", {
                "filename":   filename,
                "total_size": total_size,
                "total_str":  _human_size(total_size) if total_size else "bilinmiyor",
                "is_archive": atype is not None,
                "arch_type":  atype,
            })

            start_time = time.monotonic()
            progress_q: asyncio.Queue = asyncio.Queue()

            def _gdrive_dl_thread():
                nonlocal downloaded
                req_dl = svc.files().get_media(fileId=gdrive_file_id)
                last_t = time.monotonic()
                try:
                    with open(dest_file, "wb") as fout:
                        dlr = MediaIoBaseDownload(fout, req_dl, chunksize=8 * 1024 * 1024)
                        done = False
                        while not done:
                            status, done = dlr.next_chunk()
                            downloaded = int(status.resumable_progress) if total_size else downloaded
                            now = time.monotonic()
                            if now - last_t >= 0.5:
                                last_t  = now
                                elapsed = max(now - start_time, 0.001)
                                spd     = downloaded / elapsed
                                pct     = round(downloaded / total_size * 100, 1) if total_size else 0
                                rem     = total_size - downloaded if total_size else 0
                                eta     = rem / spd if spd > 0 and rem > 0 else 0
                                loop.call_soon_threadsafe(progress_q.put_nowait, {
                                    "downloaded": downloaded, "total": total_size,
                                    "percent": pct,
                                    "downloaded_str": _human_size(downloaded),
                                    "total_str": _human_size(total_size) if total_size else "?",
                                    "speed": _human_speed(spd),
                                    "speed_raw": round(spd),
                                    "eta": _human_eta(eta),
                                    "elapsed": round(elapsed, 1),
                                })
                except Exception as _te:
                    loop.call_soon_threadsafe(progress_q.put_nowait, {"_error": str(_te)})
                    return
                loop.call_soon_threadsafe(progress_q.put_nowait, None)

            dl_fut = loop.run_in_executor(None, _gdrive_dl_thread)
            while True:
                item = await progress_q.get()
                if item is None:
                    break
                if "_error" in item:
                    dest_file.unlink(missing_ok=True)
                    yield _sse("error", {"message": f"Drive indirme hatası: {item['_error']}"})
                    return
                yield _sse("progress", item)
            try:
                await dl_fut
            except Exception as e:
                dest_file.unlink(missing_ok=True)
                _logger.error("Drive indirme hatası", exc_info=True)

                yield _sse("error", {"message": "İşlem sırasında bir hata oluştu"})
                return

            # downloaded'ı gerçek dosya boyutundan güncelle
            if dest_file.exists():
                downloaded = dest_file.stat().st_size

            elapsed_total = time.monotonic() - start_time
            avg_speed = downloaded / max(elapsed_total, 0.001)
            yield _sse("progress", {
                "downloaded": downloaded, "total": downloaded,
                "percent": 100.0,
                "downloaded_str": _human_size(downloaded),
                "total_str": _human_size(downloaded),
                "speed": _human_speed(avg_speed),
                "speed_raw": round(avg_speed),
                "eta": "0s",
                "elapsed": round(elapsed_total, 1),
            })

        else:
            # ── Normal HTTPS indirme ───────────────────────────────────────
            UA = {"User-Agent": "Mozilla/5.0 (compatible; SunucuBot/1.0)"}
            timeout_dl = aiohttp.ClientTimeout(total=None, connect=30)

            # HEAD: dosya adı + boyut
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                    async with s.head(url, headers=UA, allow_redirects=True) as head:
                        total_size = int(head.headers.get("Content-Length", 0))
                        if not filename:
                            cd = head.headers.get("Content-Disposition", "")
                            m = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, re.IGNORECASE)
                            if m:
                                filename = m.group(1).strip().strip('"\'')
            except Exception:
                pass

            if not filename:
                filename = url.split("?")[0].rstrip("/").split("/")[-1] or "dosya"

            atype = _archive_type(filename)
            dest_file = dest_dir / filename

            yield _sse("info", {
                "filename":   filename,
                "total_size": total_size,
                "total_str":  _human_size(total_size) if total_size else "bilinmiyor",
                "is_archive": atype is not None,
                "arch_type":  atype,
            })

            start_time  = time.monotonic()
            last_report = start_time
            window: list[tuple[float, int]] = []
            CHUNK = 512 * 1024

            try:
                async with aiohttp.ClientSession(timeout=timeout_dl) as session:
                    async with session.get(url, headers=UA, allow_redirects=True) as resp:
                        if resp.status not in (200, 206):
                            yield _sse("error", {"message": f"İndirme hatası: HTTP {resp.status}"})
                            return
                        if not total_size:
                            total_size = int(resp.headers.get("Content-Length", 0))

                        async with aiofiles.open(dest_file, "wb") as f:
                            async for chunk in resp.content.iter_chunked(CHUNK):
                                await f.write(chunk)
                                downloaded += len(chunk)
                                now = time.monotonic()

                                window.append((now, len(chunk)))
                                window = [(t, b) for t, b in window if now - t <= 5]

                                if now - last_report >= 0.5:
                                    last_report  = now
                                    elapsed      = now - start_time
                                    w_bytes      = sum(b for _, b in window)
                                    w_dur        = (now - window[0][0]) if len(window) > 1 else max(elapsed, 0.001)
                                    speed_bps    = w_bytes / max(w_dur, 0.001)
                                    percent      = round(downloaded / total_size * 100, 1) if total_size else 0
                                    remaining    = total_size - downloaded if total_size else 0
                                    eta          = remaining / speed_bps if speed_bps > 0 and remaining > 0 else 0

                                    yield _sse("progress", {
                                        "downloaded":     downloaded,
                                        "total":          total_size,
                                        "percent":        percent,
                                        "downloaded_str": _human_size(downloaded),
                                        "total_str":      _human_size(total_size) if total_size else "?",
                                        "speed":          _human_speed(speed_bps),
                                        "speed_raw":      round(speed_bps),
                                        "eta":            _human_eta(eta),
                                        "elapsed":        round(elapsed, 1),
                                    })

            except asyncio.CancelledError:
                dest_file.unlink(missing_ok=True)
                return
            except Exception as e:
                dest_file.unlink(missing_ok=True)
                _logger.error("İndirme sırasında hata", exc_info=True)

                yield _sse("error", {"message": "İşlem sırasında bir hata oluştu"})
                return

            elapsed_total = time.monotonic() - start_time
            avg_speed = downloaded / max(elapsed_total, 0.001)
            yield _sse("progress", {
                "downloaded":     downloaded,
                "total":          downloaded,
                "percent":        100.0,
                "downloaded_str": _human_size(downloaded),
                "total_str":      _human_size(downloaded),
                "speed":          _human_speed(avg_speed),
                "speed_raw":      round(avg_speed),
                "eta":            "0s",
                "elapsed":        round(elapsed_total, 1),
            })

        # ── İndirme bitti: arşivse çıkar ─────────────────────────────────
        extracted_path = None
        final_file     = dest_file  # arşiv çıkarılırsa video dosyasına güncellenir

        if do_extract and dest_file.exists():
            atype = _archive_type(dest_file.name)
            if atype:
                extract_dir = dest_file.parent / dest_file.stem
                extract_dir.mkdir(parents=True, exist_ok=True)
                try:
                    loop = asyncio.get_event_loop()
                    yield _sse("extract", {"status": "start", "message": f"{atype.upper()} çıkarılıyor..."})
                    if atype == "zip":
                        await loop.run_in_executor(None, _extract_zip, dest_file, extract_dir)
                    elif atype == "tar":
                        await loop.run_in_executor(None, _extract_tar, dest_file, extract_dir)
                    elif atype == "7z":
                        await loop.run_in_executor(None, _extract_7z, dest_file, extract_dir)
                    elif atype == "rar":
                        await loop.run_in_executor(None, _extract_rar, dest_file, extract_dir)
                    dest_file.unlink(missing_ok=True)
                    extracted_path = str(extract_dir.relative_to(SUNUCU_DIR))
                    # Çıkarılan video dosyasını bul
                    _video_exts = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m4v"}
                    _found = sorted(
                        [p for p in extract_dir.rglob("*") if p.is_file() and p.suffix.lower() in _video_exts],
                        key=lambda p: p.stat().st_size, reverse=True
                    )
                    if _found:
                        final_file = _found[0]
                    yield _sse("extract", {"status": "ok", "extract_path": extracted_path})
                except Exception as ex:
                    yield _sse("extract", {"status": "error", "message": str(ex)})

        # ── Metadata sorgula ──────────────────────────────────────────────
        meta_event = {}
        try:
            from Backend.helper.pyro import clean_filename
            from Backend.helper.metadata import extract_default_id
            _meta_name   = final_file.name if final_file.exists() else filename
            _clean       = clean_filename(_meta_name)
            _override, _ = extract_default_id(_clean)
            _meta_info   = await fetch_metadata(_clean, 0, 0, override_id=_override)
            if _meta_info:
                _rel_path = str(final_file.relative_to(SUNUCU_DIR)) if final_file.exists() else (extracted_path or str(dest_file.relative_to(SUNUCU_DIR)))
                meta_event = {
                    "title":         _meta_info.get("title"),
                    "title_tr":      _meta_info.get("title_tr"),
                    "media_type":    _meta_info.get("media_type"),
                    "year":          _meta_info.get("year"),
                    "poster":        _meta_info.get("poster"),
                    "meta_path":     _rel_path,
                    "meta_filename": _meta_name,
                    "metadata":      _meta_info,
                    "needs_review":  True,
                }
                yield _sse("meta", meta_event)
            else:
                yield _sse("meta", {"error": "Metadata bulunamadı"})
        except Exception as me:
            LOGGER.warning(f"[sunucu-stream] Metadata hatası: {me}")
            yield _sse("meta", {"error": str(me)})

        # ── done eventi ───────────────────────────────────────────────────
        _dest_final = extracted_path or (str(dest_file.relative_to(SUNUCU_DIR)) if dest_file.exists() else filename)
        _size_final = _human_size(final_file.stat().st_size) if final_file.exists() else _human_size(downloaded)
        yield _sse("done", {
            "filename":  filename,
            "size":      _size_final,
            "dest_path": _dest_final,
            "extracted": extracted_path is not None,
        })

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )



# ──────────────────────────────────────────────────────────────────────────────
# POST /api/sunucu/bilgisayardan-yukle
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_bilgisayardan_yukle(
    request: Request,
    file: UploadFile = File(...),
    dest_path: str = Form(default=""),
    _: bool = Depends(require_auth),
):
    """
    Kullanıcının bilgisayarından dosya yükler.
    Multipart/form-data ile gelen dosyayı SUNUCU_DIR altındaki
    dest_path klasörüne kaydeder.
    """
    try:
        # Güvenli hedef klasörü belirle
        target_dir = _safe_path(dest_path) if dest_path.strip() else SUNUCU_DIR
        target_dir.mkdir(parents=True, exist_ok=True)

        # Dosya adını temizle (path traversal koruması)
        safe_name = Path(file.filename).name if file.filename else "dosya"
        # Geçersiz karakterleri temizle
        safe_name = re.sub(r'[<>:"/\\|?*\x00-]', '_', safe_name)
        if not safe_name:
            safe_name = "dosya"

        dest_file = target_dir / safe_name

        # Aynı isimde dosya varsa _1, _2 ... ekle
        if dest_file.exists():
            stem = dest_file.stem
            suffix = dest_file.suffix
            counter = 1
            while dest_file.exists():
                dest_file = target_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        # Dosyayı chunk'lar halinde yaz
        CHUNK = 1024 * 1024  # 1 MB
        total_written = 0
        async with aiofiles.open(dest_file, 'wb') as out:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                await out.write(chunk)
                total_written += len(chunk)

        LOGGER.info(f"[bilgisayardan-yukle] {dest_file} — {_human_size(total_written)}")
        return JSONResponse({
            "ok": True,
            "filename": dest_file.name,
            "dest_path": str(dest_file.relative_to(SUNUCU_DIR)),
            "size": _human_size(total_written),
        })

    except ValueError as ve:
        return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)
    except Exception as e:
        LOGGER.error(f"[bilgisayardan-yukle] Hata: {e}")
        _logger.error("bilgisayardan-yukle hatası", exc_info=True)
        return JSONResponse({"ok": False, "error": "Sunucu hatası"}, status_code=500)


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/sunucu/listele
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_listele(request: Request, _: bool = Depends(require_auth)):
    rel = request.query_params.get("path", "")
    try:
        target = _safe_path(rel)
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    if not target.exists():
        return JSONResponse({"error": "Klasör bulunamadı"}, status_code=404)
    if not target.is_dir():
        return JSONResponse({"error": "Belirtilen yol bir klasör değil"}, status_code=400)

    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            entries.append(_entry_info(child, SUNUCU_DIR))
    except PermissionError:
        return JSONResponse({"error": "İzin reddedildi"}, status_code=403)

    parts = []
    if rel:
        cumulative = ""
        for part in Path(rel).parts:
            cumulative = str(Path(cumulative) / part)
            parts.append({"name": part, "path": cumulative})

    return {"status": "success", "current_path": rel, "breadcrumbs": parts, "entries": entries}


# ──────────────────────────────────────────────────────────────────────────────
# DELETE /api/sunucu/sil
# ──────────────────────────────────────────────────────────────────────────────
async def _db_cleanup_after_delete(file_paths: list):
    """Silinen dosyalara ait DB kayıtlarını arka planda temizler."""
    from Backend.helper.encrypt import decode_string as _decode_string
    db_removed = 0
    try:
        for i in range(1, db.current_db_index + 1):
            storage = db.dbs[f"storage_{i}"]
            for col in ("movie", "tv"):
                async for doc in storage[col].find({}):
                    tg_list = []
                    if col == "movie":
                        tg_list = doc.get("telegram", [])
                    else:
                        for s in doc.get("seasons", []):
                            for ep in s.get("episodes", []):
                                tg_list += ep.get("telegram", [])
                    for q in tg_list:
                        qid = q.get("id", "")
                        try:
                            decoded = await _decode_string(qid)
                            lp = decoded.get("local_path")
                            if lp and lp in file_paths:
                                await db.delete_media_by_stream_id(qid)
                                db_removed += 1
                        except Exception:
                            pass
    except Exception as e:
        LOGGER.warning(f"[sunucu-sil] DB temizlik hatası: {e}")
    if db_removed:
        LOGGER.info(f"[sunucu-sil] DB temizlik tamamlandı: {db_removed} kayıt kaldırıldı")


async def sunucu_sil(request: Request, _: bool = Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    rel = body.get("path", "").strip()
    if not rel:
        return JSONResponse({"error": "path gerekli"}, status_code=400)

    try:
        target = _safe_path(rel)
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    if not target.exists():
        return JSONResponse({"error": "Dosya/klasör bulunamadı"}, status_code=404)

    # ── Silinecek dosya yollarını topla (DB temizliği için) ────────────────
    try:
        files_to_check = list(target.rglob("*")) if target.is_dir() else [target]
        file_paths = [str(f) for f in files_to_check if f.is_file()]
    except Exception:
        file_paths = []

    # ── Dosyayı/Klasörü sil ────────────────────────────────────────────────
    try:
        if target.is_dir():
            shutil.rmtree(target)
            kind = "Klasör"
        else:
            target.unlink()
            kind = "Dosya"
    except Exception as e:
        _logger.error("Silme hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    # ── DB temizliğini arka planda başlat (kullanıcıyı beklettirmez) ───────
    if file_paths:
        asyncio.ensure_future(_db_cleanup_after_delete(file_paths))

    return {"status": "success", "message": f"{kind} silindi: {target.name}"}


# ──────────────────────────────────────────────────────────────────────────────
# PUT /api/sunucu/yeniden-adlandir
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_yeniden_adlandir(request: Request, _: bool = Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    rel      = body.get("path", "").strip()
    new_name = body.get("new_name", "").strip()

    if not rel or not new_name:
        return JSONResponse({"error": "path ve new_name gerekli"}, status_code=400)
    if "/" in new_name or "\\" in new_name or ".." in new_name:
        return JSONResponse({"error": "Geçersiz dosya adı"}, status_code=400)

    try:
        target = _safe_path(rel)
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    if not target.exists():
        return JSONResponse({"error": "Dosya/klasör bulunamadı"}, status_code=404)

    new_path = target.parent / new_name
    if new_path.exists():
        return JSONResponse({"error": f"'{new_name}' zaten mevcut"}, status_code=409)

    try:
        target.rename(new_path)
    except Exception as e:
        _logger.error("Yeniden adlandırma hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    # ── DB'deki local_path referanslarını güncelle ─────────────────────────────
    # Klasör adı değişince içindeki tüm dosyalar yeni yola taşınır.
    # Dosya adı değişince sadece o tek dosyanın yolu değişir.
    # Her iki durumda da: eski mutlak yol → yeni mutlak yol prefix eşleşmesiyle bulunur.
    db_updated = 0
    try:
        from Backend.helper.encrypt import encode_string as _encode_string, decode_string as _decode_string

        old_abs = str(target.resolve())   # rename öncesi resolve (artık new_path'te)
        new_abs = str(new_path.resolve())

        async def _repath_encoded(old_id: str) -> str | None:
            """
            Encoded string'i çöz; local_path varsa ve eski yolla başlıyorsa
            yeni yolla güncelle ve yeniden encode et. Değişiklik yoksa None döner.
            """
            try:
                decoded = await _decode_string(old_id)
            except Exception:
                return None
            lp = decoded.get("local_path")
            if not lp:
                return None
            # Dosya adı veya klasör içindeki herhangi bir dosya eşleşebilir
            if lp == old_abs or lp.startswith(old_abs + "/") or lp.startswith(old_abs + "\\"):
                new_lp = new_abs + lp[len(old_abs):]
                decoded["local_path"] = new_lp
                return await _encode_string(decoded)
            return None

        for i in range(1, db.current_db_index + 1):
            storage = db.dbs[f"storage_{i}"]
            for col_name in ("movie", "tv"):
                col = storage[col_name]
                async for doc in col.find({}):
                    changed = False
                    if col_name == "movie":
                        tg_list = doc.get("telegram", [])
                        for q in tg_list:
                            new_id = await _repath_encoded(q.get("id", ""))
                            if new_id:
                                q["id"] = new_id
                                changed = True
                        if changed:
                            await col.update_one({"_id": doc["_id"]}, {"$set": {"telegram": tg_list}})
                            db_updated += 1
                    else:
                        seasons = doc.get("seasons", [])
                        for season in seasons:
                            for ep in season.get("episodes", []):
                                tg_list = ep.get("telegram", [])
                                for q in tg_list:
                                    new_id = await _repath_encoded(q.get("id", ""))
                                    if new_id:
                                        q["id"] = new_id
                                        changed = True
                        if changed:
                            await col.update_one({"_id": doc["_id"]}, {"$set": {"seasons": seasons}})
                            db_updated += 1

    except Exception as e:
        LOGGER.warning(f"[yeniden-adlandir] DB güncelleme hatası: {e}")

    result = {
        "status":   "success",
        "old_name": target.name,
        "new_name": new_name,
        "new_path": str(new_path.relative_to(SUNUCU_DIR)),
    }
    if db_updated:
        result["db_updated"] = db_updated
    return result


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/sunucu/metadata
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_metadata(request: Request, _: bool = Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    rel             = body.get("path", "").strip()
    custom_filename = body.get("custom_filename") or None

    if not rel:
        return JSONResponse({"error": "path gerekli"}, status_code=400)

    try:
        target = _safe_path(rel)
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "Dosya bulunamadı"}, status_code=404)

    meta_filename = custom_filename or target.name
    size_str      = _human_size(target.stat().st_size)

    try:
        meta = await fetch_metadata(filename=meta_filename, channel=0, msg_id=0)
    except Exception as e:
        _logger.error("Metadata sorgusu başarısız", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    if not meta:
        return JSONResponse({"error": f"Metadata bulunamadı: {meta_filename!r}"}, status_code=404)

    try:
        result = await db.insert_media(
            metadata_info={**meta, "encoded_string": str(target)},
            channel=0, msg_id=0,
            size=size_str, name=meta_filename,
        )
        db_id = str(result) if result else None
        # Katalog yenilemesini debounce ile tetikle
        try:
            from Backend.helper.platform_catalog import platform_catalog as _pc
            _pc.schedule_refresh()
        except Exception:
            pass
    except Exception as e:
        _logger.error("DB kayıt hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    return {
        "status": "success", "filename": meta_filename, "size": size_str, "db_id": db_id,
        "title": meta.get("title"), "title_tr": meta.get("title_tr"),
        "media_type": meta.get("media_type"), "year": meta.get("year"),
        "quality": meta.get("quality"), "poster": meta.get("poster"),
        "tmdb_id": meta.get("tmdb_id"), "metadata": meta,
    }


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/sunucu/klasor-olustur
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_klasor_olustur(request: Request, _: bool = Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    parent_rel = body.get("path", "").strip()
    name       = body.get("name", "").strip()

    if not name or "/" in name or "\\" in name or ".." in name:
        return JSONResponse({"error": "Geçersiz klasör adı"}, status_code=400)

    try:
        new_dir = _safe_path(str(Path(parent_rel) / name))
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    if new_dir.exists():
        return JSONResponse({"error": "Klasör zaten mevcut"}, status_code=409)

    try:
        new_dir.mkdir(parents=True)
    except Exception as e:
        _logger.error("Klasör oluşturma hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    return {"status": "success", "path": str(new_dir.relative_to(SUNUCU_DIR)), "name": name}

# ──────────────────────────────────────────────────────────────────────────────
# GET /api/sunucu/dosya-durumu  → gdrive_token.pickle ve rclone.conf varlığı
# ──────────────────────────────────────────────────────────────────────────────
_DOSYA_GDRIVE_PATH = Path(__file__).parent.parent.parent.parent / "gdrive_token.pickle"
_DOSYA_RCLONE_PATH = Path(__file__).parent.parent.parent.parent / "rclone.conf"

async def sunucu_dosya_durumu(request: Request, _: bool = Depends(require_auth)):
    gdrive_ok = _DOSYA_GDRIVE_PATH.exists()
    rclone_ok = _DOSYA_RCLONE_PATH.exists()

    rclone_remotes: list[str] = []
    if rclone_ok:
        try:
            import configparser as _cp
            _rcp = _cp.ConfigParser()
            _rcp.read(str(_DOSYA_RCLONE_PATH))
            rclone_remotes = _rcp.sections()
        except Exception:
            pass

    return JSONResponse({
        "gdrive_token": gdrive_ok,
        "rclone_conf":  rclone_ok,
        "rclone_remotes": rclone_remotes,
    })


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/sunucu/sistem-durumu  → CPU, RAM, Disk, Bot uptime
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_sistem_durumu(request: Request, _: bool = Depends(require_auth)):
    try:
        import psutil
    except ImportError:
        return JSONResponse({"error": "psutil kurulu değil"}, status_code=500)

    try:
        disk_path = str(SUNUCU_DIR) if SUNUCU_DIR.exists() else "/"

        def _collect():
            cpu   = psutil.cpu_percent(interval=0.5)
            ram   = psutil.virtual_memory()
            disk  = psutil.disk_usage(disk_path)
            # Ağ hızı: 1 saniyelik örnekleme
            net1  = psutil.net_io_counters()
            import time as _t; _t.sleep(1)
            net2  = psutil.net_io_counters()
            dl_bps = max(net2.bytes_recv - net1.bytes_recv, 0)
            ul_bps = max(net2.bytes_sent - net1.bytes_sent, 0)
            return cpu, ram, disk, dl_bps, ul_bps

        loop = asyncio.get_event_loop()
        cpu, ram, disk, dl_bps, ul_bps = await loop.run_in_executor(None, _collect)

        def _net_str(bps):
            if bps >= 1_073_741_824:
                return f"{bps/1_073_741_824:.2f} GB/s"
            if bps >= 1_048_576:
                return f"{bps/1_048_576:.1f} MB/s"
            if bps >= 1024:
                return f"{bps/1024:.1f} KB/s"
            return f"{bps} B/s"

        bot_uptime = get_readable_time(time.time() - StartTime)
        return {
            "cpu_percent": round(cpu, 1),
            "ram_used": _human_size(ram.used),
            "ram_total": _human_size(ram.total),
            "ram_percent": round(ram.percent, 1),
            "disk_used": _human_size(disk.used),
            "disk_free": _human_size(disk.free),
            "disk_total": _human_size(disk.total),
            "disk_percent": round(disk.percent, 1),
            "bot_uptime": bot_uptime,
            "net_download": _net_str(dl_bps),
            "net_upload":   _net_str(ul_bps),
            "net_dl_bps":   dl_bps,
            "net_ul_bps":   ul_bps,
        }
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/sunucu/metadata-sorgu  → Sadece sorgula, kaydetme
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_metadata_sorgu(request: Request, _: bool = Depends(require_auth)):
    """Metadata'yı sorgular ama DB'ye KAYDETMEZ. Onay için kullanılır."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    rel             = body.get("path", "").strip()
    custom_filename = body.get("custom_filename") or None

    if not rel:
        return JSONResponse({"error": "path gerekli"}, status_code=400)

    try:
        target = _safe_path(rel)
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "Dosya bulunamadı"}, status_code=404)

    meta_filename = custom_filename or target.name
    size_str      = _human_size(target.stat().st_size)

    try:
        meta = await fetch_metadata(filename=meta_filename, channel=0, msg_id=0)
    except Exception as e:
        _logger.error("Metadata sorgusu başarısız", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    if not meta:
        return JSONResponse({"error": f"Metadata bulunamadı: {meta_filename!r}"}, status_code=404)

    return {
        "status": "found",
        "filename": meta_filename,
        "size": size_str,
        "path": rel,
        "title": meta.get("title"),
        "title_tr": meta.get("title_tr"),
        "media_type": meta.get("media_type"),
        "year": meta.get("year"),
        "quality": meta.get("quality"),
        "poster": meta.get("poster"),
        "backdrop": meta.get("backdrop"),
        "tmdb_id": meta.get("tmdb_id"),
        "description": meta.get("description") or meta.get("desc"),
        "rating": meta.get("rating"),
        "genres": meta.get("genres", []),
        "metadata": meta,
    }


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/sunucu/metadata-kaydet  → Onaylanan metadatayı DB'ye kaydet
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_metadata_kaydet(request: Request, _: bool = Depends(require_auth)):
    """Kullanıcının onayladığı (ve düzenleyebileceği) metadatayı DB'ye kaydeder."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    rel      = body.get("path", "").strip()
    filename = body.get("filename", "").strip()
    metadata = body.get("metadata")

    if not rel or not metadata:
        return JSONResponse({"error": "path ve metadata gerekli"}, status_code=400)

    try:
        target = _safe_path(rel)
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    size_str = _human_size(target.stat().st_size) if target.exists() else "?"

    # Sunucu dosyasını local_path encode_string ile kaydet.
    # stream_routes /dl/ handler decode_string({"local_path": ...}) ile
    # local_file_streamer'a yönlendirir; token doğrulaması ve bandwidth takibi sağlar.
    # Düz HTTP URL yerine bu yöntem: Drive kataloguyla karışmaz (gdrive_file_id yok),
    # member_stream_url_api ile tam uyumlu.
    from Backend.helper.encrypt import encode_string as _encode_string
    try:
        encoded_id = await _encode_string({"local_path": str(target.resolve())})
    except Exception as e:
        _logger.error("Dosya encode hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    try:
        result = await db.insert_media(
            metadata_info={**metadata, "encoded_string": encoded_id},
            channel=0, msg_id=0,
            size=size_str, name=filename or target.name,
        )
        db_id = str(result) if result else None
        # Katalog yenilemesini debounce ile tetikle
        try:
            from Backend.helper.platform_catalog import platform_catalog as _pc
            _pc.schedule_refresh()
        except Exception:
            pass
    except Exception as e:
        _logger.error("DB kayıt hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    return {
        "status": "success",
        "db_id": db_id,
        "title": metadata.get("title"),
        "title_tr": metadata.get("title_tr"),
    }

# ──────────────────────────────────────────────────────────────────────────────
# DELETE /api/sunucu/metadata-sil  → Reddet: dosyayı sunucudan sil
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_metadata_sil(request: Request, _: bool = Depends(require_auth)):
    """Kullanıcı metadata'yı reddedince dosyayı sunucudan siler."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    rel = body.get("path", "").strip()
    if not rel:
        return JSONResponse({"error": "path gerekli"}, status_code=400)

    try:
        target = _safe_path(rel)
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    try:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            return JSONResponse({"error": "Dosya/klasör bulunamadı"}, status_code=404)
    except Exception as e:
        _logger.error("Silme hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    return {"status": "success", "message": f"Silindi: {target.name}"}


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/sunucu/indir  → Dosyayı attachment olarak indir (tarayıcıda açmaz)
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_indir(request: Request, _: bool = Depends(require_auth)):
    """
    Sunucudaki dosyayı Content-Disposition: attachment ile serve eder.
    Tarayıcı dosyayı açmak yerine indirme diyaloğu gösterir.
    Stremio gibi harici oynatıcılar için de doğrudan stream URL'si olarak kullanılabilir.
    """
    rel = request.query_params.get("path", "").strip()
    if not rel:
        return JSONResponse({"error": "path gerekli"}, status_code=400)

    try:
        target = _safe_path(rel)
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    if not target.exists():
        return JSONResponse({"error": "Dosya bulunamadı"}, status_code=404)
    if not target.is_file():
        return JSONResponse({"error": "Belirtilen yol bir dosya değil"}, status_code=400)

    file_size = target.stat().st_size
    filename  = target.name

    # Range request desteği (Stremio ve video oynatıcılar için gerekli)
    range_header = request.headers.get("range", "")
    start, end = 0, file_size - 1

    if range_header:
        try:
            range_val = range_header.strip().replace("bytes=", "")
            range_start, range_end = range_val.split("-")
            start = int(range_start)
            end   = int(range_end) if range_end else file_size - 1
        except Exception:
            return JSONResponse({"error": "Geçersiz Range header"}, status_code=416)

    chunk_size  = 1024 * 1024  # 1 MB
    content_len = end - start + 1

    # Dosya uzantısına göre MIME tipi belirle
    ext = target.suffix.lower()
    mime_map = {
        ".mkv": "video/x-matroska",
        ".mp4": "video/mp4",
        ".avi": "video/x-msvideo",
        ".mov": "video/quicktime",
        ".wmv": "video/x-ms-wmv",
        ".flv": "video/x-flv",
        ".webm": "video/webm",
        ".ts":  "video/mp2t",
        ".m4v": "video/x-m4v",
        ".mpg": "video/mpeg",
        ".mpeg":"video/mpeg",
        ".zip": "application/zip",
        ".rar": "application/x-rar-compressed",
        ".7z":  "application/x-7z-compressed",
    }
    media_type = mime_map.get(ext, "application/octet-stream")

    async def file_iterator():
        async with aiofiles.open(target, "rb") as f:
            await f.seek(start)
            remaining = content_len
            while remaining > 0:
                read_size = min(chunk_size, remaining)
                chunk = await f.read(read_size)
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    status_code = 206 if range_header else 200
    headers = {
        "Content-Range":       f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges":       "bytes",
        "Content-Length":      str(content_len),
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control":       "no-cache",
    }

    return StreamingResponse(
        file_iterator(),
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Klasör ZIP işleri — geçici dosyalar için basit kayıt defteri
# ──────────────────────────────────────────────────────────────────────────────
import uuid as _uuid
import tempfile as _tempfile

_ZIP_JOBS: dict[str, dict] = {}   # job_id → {status, progress, total, zip_path, zip_name, error}
_ZIP_TEMP_DIR = Path(_tempfile.gettempdir()) / "sunucu_zip"
_ZIP_TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/sunucu/klasor-zip-baslat  → Arka planda ZIP işi başlat, job_id döner
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_klasor_zip_baslat(request: Request, _: bool = Depends(require_auth)):
    """
    Klasörü arka planda ZIP'lemeye başlar.
    Döner: { job_id, zip_name, total_files, total_size }
    """
    rel = request.query_params.get("path", "").strip()
    if not rel:
        return JSONResponse({"error": "path gerekli"}, status_code=400)

    try:
        target = _safe_path(rel)
    except ValueError as e:
        _logger.warning("Geçersiz yol isteği: %s", e)
        return JSONResponse({"error": "Geçersiz veya erişilemeyen yol"}, status_code=400)

    if not target.exists():
        return JSONResponse({"error": "Klasör bulunamadı"}, status_code=404)
    if not target.is_dir():
        return JSONResponse({"error": "Belirtilen yol bir klasör değil"}, status_code=400)

    files       = sorted([p for p in target.rglob("*") if p.is_file()])
    total_size  = sum(f.stat().st_size for f in files)
    total_files = len(files)

    job_id   = _uuid.uuid4().hex
    zip_name = target.name + ".zip"
    zip_path = _ZIP_TEMP_DIR / f"{job_id}.zip"

    job = {
        "status":      "running",
        "progress":    0,
        "total":       total_files,
        "done_size":   0,
        "total_size":  total_size,
        "zip_path":    str(zip_path),
        "zip_name":    zip_name,
        "error":       None,
        "started_at":  time.monotonic(),
    }
    _ZIP_JOBS[job_id] = job

    async def _build_async():
        """
        Her dosyayı ayrı ZipFile.open/close döngüsüyle ekler.
        Böylece her adımda ZIP tamamen flush edilir ve stat().st_size gerçek değeri verir.
        """
        try:
            loop = asyncio.get_event_loop()
            mode = "w"   # ilk dosya için yeni arşiv; sonrası "a" (append)

            for i, fp in enumerate(files):
                arcname = fp.relative_to(target.parent)
                _mode   = mode

                def _write(fp=fp, arcname=arcname, _mode=_mode):
                    with zipfile.ZipFile(zip_path, mode=_mode,
                                         compression=zipfile.ZIP_DEFLATED,
                                         allowZip64=True) as zf:
                        zf.write(fp, arcname)

                await loop.run_in_executor(None, _write)
                mode = "a"   # ikinci dosyadan itibaren append

                job["progress"]  = i + 1
                job["done_size"] = zip_path.stat().st_size if zip_path.exists() else 0

            job["status"]    = "done"
            job["done_size"] = zip_path.stat().st_size if zip_path.exists() else 0

        except Exception as ex:
            job["status"] = "error"
            job["error"]  = str(ex)
            zip_path.unlink(missing_ok=True)
            LOGGER.warning(f"[klasor-zip] Hata: {ex}")

    asyncio.ensure_future(_build_async())

    return JSONResponse({
        "job_id":      job_id,
        "zip_name":    zip_name,
        "total_files": total_files,
        "total_size":  total_size,
    })

# GET /api/sunucu/klasor-zip-durum  → SSE: ZIP ilerleme akışı
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_klasor_zip_durum(request: Request, _: bool = Depends(require_auth)):
    """
    SSE akışı. Events:
      progress  { progress, total, percent, done_size, total_size,
                  done_size_str, total_size_str, elapsed, eta, speed_str }
      done      { job_id, zip_name, zip_size, zip_size_str }
      error     { message }
    """
    job_id = request.query_params.get("job_id", "").strip()
    if not job_id or job_id not in _ZIP_JOBS:
        async def _err():
            yield _sse("error", {"message": "Geçersiz job_id"})
        return StreamingResponse(_err(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def generate():
        last_progress = -1
        while True:
            job = _ZIP_JOBS.get(job_id)
            if not job:
                yield _sse("error", {"message": "İş bulunamadı"})
                return

            progress   = job["progress"]
            total      = job["total"]
            done_size  = job["done_size"]
            total_size = job["total_size"]
            elapsed    = max(time.monotonic() - job["started_at"], 0.001)
            percent    = round(progress / total * 100, 1) if total else 0

            speed_bps  = done_size / elapsed if elapsed > 0 else 0
            remaining  = total_size - done_size
            eta        = _human_eta(remaining / speed_bps) if speed_bps > 0 and remaining > 0 else "—"

            if progress != last_progress or job["status"] != "running":
                last_progress = progress
                yield _sse("progress", {
                    "progress":       progress,
                    "total":          total,
                    "percent":        percent,
                    "done_size":      done_size,
                    "total_size":     total_size,
                    "done_size_str":  _human_size(done_size),
                    "total_size_str": _human_size(total_size) if total_size else "?",
                    "elapsed":        round(elapsed, 1),
                    "eta":            eta,
                    "speed_str":      _human_speed(speed_bps) if speed_bps > 0 else "—",
                })

            if job["status"] == "done":
                zip_size = Path(job["zip_path"]).stat().st_size
                yield _sse("done", {
                    "job_id":       job_id,
                    "zip_name":     job["zip_name"],
                    "zip_size":     zip_size,
                    "zip_size_str": _human_size(zip_size),
                })
                return

            if job["status"] == "error":
                yield _sse("error", {"message": job["error"] or "ZIP hatası"})
                return

            await asyncio.sleep(0.4)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/sunucu/indir-klasor  → Tamamlanmış ZIP'i indir ve geçici dosyayı sil
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_indir_klasor(request: Request, _: bool = Depends(require_auth)):
    """
    job_id ile tamamlanmış ZIP'i tarayıcıya gönderir.
    İndirme tamamlanınca geçici dosyayı temizler.
    """
    job_id = request.query_params.get("job_id", "").strip()
    if not job_id or job_id not in _ZIP_JOBS:
        return JSONResponse({"error": "Geçersiz job_id"}, status_code=400)

    job = _ZIP_JOBS[job_id]
    if job["status"] != "done":
        return JSONResponse({"error": "ZIP henüz hazır değil"}, status_code=425)

    zip_path = Path(job["zip_path"])
    zip_name = job["zip_name"]

    if not zip_path.exists():
        return JSONResponse({"error": "ZIP dosyası bulunamadı"}, status_code=404)

    async def file_iter_and_cleanup():
        try:
            async with aiofiles.open(zip_path, "rb") as f:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            zip_path.unlink(missing_ok=True)
            _ZIP_JOBS.pop(job_id, None)

    zip_size = zip_path.stat().st_size
    headers = {
        "Content-Disposition": f'attachment; filename="{zip_name}"',
        "Content-Length":      str(zip_size),
        "Cache-Control":       "no-cache",
        "X-Accel-Buffering":   "no",
    }

    return StreamingResponse(
        file_iter_and_cleanup(),
        media_type="application/zip",
        headers=headers,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Google Drive entegrasyonu (sunucu.html paneli için)
# ──────────────────────────────────────────────────────────────────────────────

GDRIVE_TOKEN_PATH = Path(__file__).parent.parent.parent.parent / "gdrive_token.pickle"

VIDEO_MIMES_GDRIVE = {
    "video/mp4", "video/x-matroska", "video/x-msvideo",
    "video/quicktime", "video/x-ms-wmv", "video/mpeg",
    "video/x-flv", "video/webm", "video/3gpp",
    "application/octet-stream",
}
VIDEO_EXTS_GDRIVE = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m4v", ".webm", ".flv", ".mpg", ".mpeg"}
GDRIVE_PAGE_SIZE  = 20


def _ensure_gdrive_packages():
    import importlib, subprocess, sys, shutil as _shutil, os as _os
    pkgs = {
        "googleapiclient": "google-api-python-client",
        "google.auth":     "google-auth",
    }
    for module, pip_name in pkgs.items():
        try:
            importlib.import_module(module)
        except ImportError:
            # uv varsa onu kullan, yoksa pip'e dön
            uv_bin = _shutil.which("uv")
            if not uv_bin and _os.path.exists("/app/.venv/bin/uv"):
                uv_bin = "/app/.venv/bin/uv"

            if uv_bin:
                cmd = [uv_bin, "pip", "install", pip_name]
            else:
                cmd = [sys.executable, "-m", "pip", "install",
                       "--break-system-packages", "--quiet", pip_name]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"{pip_name} yüklenemedi.\n"
                    f"Lütfen manuel olarak kurun: pip install {pip_name}\n"
                    f"Hata: {result.stderr[:300]}"
                )
            # Kurulum sonrası önbelleği temizle ve yeniden import et
            for cached in list(sys.modules.keys()):
                if cached == module or cached.startswith(module + "."):
                    del sys.modules[cached]
            importlib.import_module(module)  # kurulumu doğrula


def _gdrive_service():
    import pickle
    _ensure_gdrive_packages()
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request as GRequest
    if not GDRIVE_TOKEN_PATH.exists():
        raise FileNotFoundError("gdrive_token.pickle bulunamadı. /ayarlar → token.pickle yükle.")
    with open(GDRIVE_TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        creds.refresh(GRequest())
        with open(GDRIVE_TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _gdrive_list(folder_id: str = "root", page_token: str = "") -> dict:
    svc = _gdrive_service()
    query = (
        f"'{folder_id}' in parents and trashed = false and ("
        "mimeType = 'application/vnd.google-apps.folder' or "
        + " or ".join(f"mimeType = '{m}'" for m in VIDEO_MIMES_GDRIVE)
        + ")"
    )
    params = dict(
        q=query,
        fields="nextPageToken, files(id, name, mimeType, size)",
        orderBy="folder,name",
        pageSize=GDRIVE_PAGE_SIZE,
    )
    if page_token:
        params["pageToken"] = page_token
    resp  = svc.files().list(**params).execute()
    items = resp.get("files", [])
    next_tok = resp.get("nextPageToken", "")

    filtered = []
    for it in items:
        if it["mimeType"] == "application/vnd.google-apps.folder":
            filtered.append(it)
        elif it["mimeType"] in VIDEO_MIMES_GDRIVE:
            ext = Path(it["name"]).suffix.lower()
            if it["mimeType"] != "application/octet-stream" or ext in VIDEO_EXTS_GDRIVE:
                filtered.append(it)
        elif Path(it["name"]).suffix.lower() in VIDEO_EXTS_GDRIVE:
            filtered.append(it)

    folder_name = "Ana Klasör"
    if folder_id != "root":
        try:
            meta = svc.files().get(fileId=folder_id, fields="name").execute()
            folder_name = meta.get("name", folder_id)
        except Exception:
            folder_name = folder_id

    return {"items": filtered, "next_page_token": next_tok, "folder_name": folder_name}


async def sunucu_gdrive_listele(request: Request, _: bool = Depends(require_auth)):
    """GET /api/sunucu/gdrive-listele?folder_id=root&page_token="""
    folder_id  = request.query_params.get("folder_id", "root").strip() or "root"
    page_token = request.query_params.get("page_token", "").strip()
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: _gdrive_list(folder_id, page_token))
        return JSONResponse(result)
    except FileNotFoundError as e:
        _logger.error("GDrive listele hatası", exc_info=True)
        return JSONResponse({"error": "Drive klasörü bulunamadı"}, status_code=404)
    except Exception as e:
        _logger.error("GDrive listele 500", exc_info=True)
        return JSONResponse({"error": "Drive bağlantı hatası"}, status_code=500)


async def sunucu_gdrive_ekle(request: Request, _: bool = Depends(require_auth)):
    """
    POST /api/sunucu/gdrive-ekle
    Body: { "file_id": "...", "file_name": "...", "size": "..." }
    Dosyayı encode edip metadata sorgular, DB'ye kaydeder.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    file_id   = body.get("file_id", "").strip()
    file_name = body.get("file_name", "").strip()
    size_str  = body.get("size", "").strip()

    if not file_id or not file_name:
        return JSONResponse({"error": "file_id ve file_name gerekli"}, status_code=400)

    # Metadata sorgusu
    from Backend.helper.metadata import metadata as fetch_meta, extract_default_id
    from Backend.helper.pyro import clean_filename, remove_urls
    from Backend.helper.encrypt import encode_string

    override_id, _ = extract_default_id(file_name)
    clean_name = clean_filename(file_name)

    try:
        meta_info = await fetch_meta(clean_name, 0, 0, override_id=override_id)
    except Exception as e:
        _logger.error("Metadata hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    if not meta_info:
        return JSONResponse({"error": f"Metadata bulunamadı: {file_name!r}"}, status_code=404)

    # GDrive encoded_string
    try:
        drive_encoded = await encode_string({"gdrive_file_id": file_id})
        meta_info = dict(meta_info)
        meta_info["encoded_string"] = drive_encoded
    except Exception as e:
        _logger.error("Encode hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    display_name = remove_urls(file_name)
    if Path(display_name).suffix.lower() not in VIDEO_EXTS_GDRIVE:
        display_name += ".mkv"

    try:
        result = await db.insert_media(
            meta_info, channel=0, msg_id=0,
            size=size_str, name=display_name,
        )
        db_id = str(result) if result else None
        # Katalog yenilemesini debounce ile tetikle
        try:
            from Backend.helper.platform_catalog import platform_catalog as _pc
            _pc.schedule_refresh()
        except Exception:
            pass
    except Exception as e:
        _logger.error("DB kayıt hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    return JSONResponse({
        "status":  "success",
        "db_id":   db_id,
        "title":   meta_info.get("title"),
        "title_tr": meta_info.get("title_tr"),
        "encoded": drive_encoded,
    })


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/sunucu/gdrive-meta-sorgu  → Metadata sorgula, kaydetme
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_gdrive_meta_sorgu(request: Request, _: bool = Depends(require_auth)):
    """
    Drive dosyasının metadata'sını sorgular ama kaydetmez.
    Body: { "file_id": "...", "file_name": "...", "size": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    file_id   = body.get("file_id", "").strip()
    file_name = body.get("file_name", "").strip()
    size_str  = body.get("size", "").strip()

    if not file_id or not file_name:
        return JSONResponse({"error": "file_id ve file_name gerekli"}, status_code=400)

    from Backend.helper.metadata import metadata as fetch_meta, extract_default_id
    from Backend.helper.pyro import clean_filename

    override_id, _ = extract_default_id(file_name)
    clean_name = clean_filename(file_name)

    try:
        meta = await fetch_meta(clean_name, 0, 0, override_id=override_id)
    except Exception as e:
        _logger.error("Metadata hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    if not meta:
        return JSONResponse({"error": f"Metadata bulunamadı: {file_name!r}"}, status_code=404)

    return JSONResponse({
        "status":      "found",
        "filename":    file_name,
        "size":        size_str,
        "title":       meta.get("title"),
        "title_tr":    meta.get("title_tr"),
        "media_type":  meta.get("media_type"),
        "year":        meta.get("year"),
        "quality":     meta.get("quality"),
        "poster":      meta.get("poster"),
        "backdrop":    meta.get("backdrop"),
        "tmdb_id":     meta.get("tmdb_id"),
        "description": meta.get("description") or meta.get("desc"),
        "rating":      meta.get("rating"),
        "genres":      meta.get("genres", []),
        "metadata":    meta,
    })


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/sunucu/gdrive-ekle-onay  → Onaylanan metadatayla Drive içeriğini DB'ye ekle
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_gdrive_ekle_onay(request: Request, _: bool = Depends(require_auth)):
    """
    Kullanıcının düzenlediği metadata ile Drive dosyasını DB'ye kaydeder.
    Body: { "file_id": "...", "file_name": "...", "size": "...", "metadata": {...} }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    file_id   = body.get("file_id", "").strip()
    file_name = body.get("file_name", "").strip()
    size_str  = body.get("size", "").strip()
    metadata  = body.get("metadata")

    if not file_id or not file_name or not metadata:
        return JSONResponse({"error": "file_id, file_name ve metadata gerekli"}, status_code=400)

    from Backend.helper.pyro import remove_urls
    from Backend.helper.encrypt import encode_string

    try:
        drive_encoded = await encode_string({"gdrive_file_id": file_id})
        meta_info = dict(metadata)
        meta_info["encoded_string"] = drive_encoded
    except Exception as e:
        _logger.error("Encode hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    display_name = remove_urls(file_name)
    if Path(display_name).suffix.lower() not in VIDEO_EXTS_GDRIVE:
        display_name += ".mkv"

    try:
        result = await db.insert_media(
            meta_info, channel=0, msg_id=0,
            size=size_str, name=display_name,
        )
        db_id = str(result) if result else None
        # Katalog yenilemesini debounce ile tetikle
        try:
            from Backend.helper.platform_catalog import platform_catalog as _pc
            _pc.schedule_refresh()
        except Exception:
            pass
    except Exception as e:
        _logger.error("DB kayıt hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    # ekle_approved koleksiyonuna kaydet — sunucu.html listesinde görünsün
    try:
        APPROVED_COLLECTION = "ekle_approved"
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is not None:
            title = metadata.get("title_tr") or metadata.get("title") or display_name
            await storage[APPROVED_COLLECTION].insert_one({
                "file_id":   file_id,
                "file_name": file_name,
                "title":     title,
                "db_id":     drive_encoded,
                "size":      size_str,
                "folder_id": "root",
                "added_at":  int(time.time()),
            })
    except Exception as e:
        LOGGER.warning(f"[gdrive-ekle-onay] ekle_approved kayıt hatası: {e}")

    return JSONResponse({
        "status":   "success",
        "db_id":    db_id,
        "title":    metadata.get("title"),
        "title_tr": metadata.get("title_tr"),
    })



# ──────────────────────────────────────────────────────────────────────────────
# POST /api/sunucu/gdrive-migrate  → Stremio DB'deki mevcut gdrive içeriklerini ekle_approved'a aktar
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_gdrive_migrate(request: Request, _: bool = Depends(require_auth)):
    """
    Stremio DB'de telegram[].id içinde gdrive_file_id olan kayıtları tarar ve
    ekle_approved koleksiyonuna ekler. Zaten kayıtlı olanları atlar.
    Bir kez çalıştırılması yeterli.
    """
    from Backend.helper.encrypt import async_base62_decode, async_decompress_data
    import json as _json

    APPROVED_COLLECTION = "ekle_approved"

    async def _try_decode_gdrive(encoded: str):
        try:
            compressed = await async_base62_decode(encoded)
            json_str   = await async_decompress_data(compressed)
            data       = _json.loads(json_str)
            if isinstance(data, dict) and data.get("gdrive_file_id"):
                return data["gdrive_file_id"]
        except Exception:
            pass
        return None

    try:
        storage_keys = sorted(
            [(k, int(k.split("_")[1])) for k in db.dbs if k.startswith("storage_")],
            key=lambda x: x[1]
        )
        added = 0
        skipped = 0

        for db_key, db_index in storage_keys:
            storage = db.dbs.get(db_key)
            if storage is None:
                continue
            col = storage[APPROVED_COLLECTION]

            for col_name in ["movie", "tv"]:
                cursor = storage[col_name].find({})
                async for doc in cursor:
                    title = doc.get("title_tr") or doc.get("title") or "?"

                    if col_name == "tv":
                        quality_list = [
                            q
                            for season in (doc.get("seasons") or [])
                            for ep in (season.get("episodes") or [])
                            for q in (ep.get("telegram") or [])
                        ]
                    else:
                        quality_list = doc.get("telegram") or []

                    for q in quality_list:
                        encoded = q.get("id") or ""
                        if not encoded:
                            continue
                        gdrive_id = await _try_decode_gdrive(encoded)
                        if not gdrive_id:
                            continue

                        # Zaten var mı?
                        existing = await col.find_one({"db_id": encoded})
                        if existing:
                            skipped += 1
                            continue

                        await col.insert_one({
                            "file_id":   gdrive_id,
                            "file_name": q.get("name", ""),
                            "title":     title,
                            "db_id":     encoded,
                            "size":      q.get("size", ""),
                            "folder_id": "root",
                            "added_at":  int(time.time()),
                        })
                        added += 1
                        LOGGER.info(f"[gdrive-migrate] Aktarıldı: {title} | {q.get('name','')}")

        return JSONResponse({
            "status": "success",
            "added": added,
            "skipped": skipped,
            "message": f"{added} kayıt aktarıldı, {skipped} zaten mevcuttu."
        })

    except Exception as e:
        _logger.error("Migration hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/sunucu/gdrive-db-listele  → DB'deki Drive içeriklerini listele
# ──────────────────────────────────────────────────────────────────────────────
async def sunucu_gdrive_db_listele(request: Request, _: bool = Depends(require_auth)):
    """
    ekle.py'nin kaydettiği ekle_approved koleksiyonunu okur.
    ekle.py ile birebir aynı verileri döner: file_name, title, size, added_at, gdrive_file_id, db_id.
    """
    APPROVED_COLLECTION = "ekle_approved"
    try:
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is None:
            return JSONResponse({"error": "DB storage bulunamadı"}, status_code=500)

        col = storage[APPROVED_COLLECTION]
        # Yalnızca Drive kayıtlarını getir — rclone kayıtlarını hariç tut
        drive_query = {"source": {"$ne": "rclone"}, "rclone_path": {"$exists": False}}
        cursor = col.find(drive_query).sort("added_at", -1)
        docs = await cursor.to_list(length=500)

        items = []
        for doc in docs:
            items.append({
                "doc_id":         str(doc["_id"]),
                "db_id":          doc.get("db_id", ""),
                "db_index":       db.current_db_index,
                "file_name":      doc.get("file_name", ""),
                "title":          doc.get("title", doc.get("file_name", "—")),
                "size":           doc.get("size", ""),
                "gdrive_file_id": doc.get("file_id", ""),
                "folder_id":      doc.get("folder_id", "root"),
                "added_at":       doc.get("added_at", 0),
            })

        return JSONResponse({"status": "success", "items": items, "total": len(items)})

    except Exception as e:
        _logger.error("DB okuma hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)


async def sunucu_gdrive_db_sil(request: Request, _: bool = Depends(require_auth)):
    """
    ekle_approved koleksiyonundan kaydı siler ve Stremio DB'den ilgili medyayı kaldırır.
    ekle.py'deki _db_delete_approved mantığıyla aynı çalışır.
    Body: { "doc_id": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    doc_id = body.get("doc_id", "").strip()
    if not doc_id:
        return JSONResponse({"error": "doc_id gerekli"}, status_code=400)

    from bson import ObjectId
    APPROVED_COLLECTION = "ekle_approved"

    try:
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is None:
            return JSONResponse({"error": "DB storage bulunamadı"}, status_code=500)

        col = storage[APPROVED_COLLECTION]
        try:
            oid = ObjectId(doc_id)
        except Exception:
            return JSONResponse({"error": "Geçersiz doc_id"}, status_code=400)

        doc = await col.find_one({"_id": oid})
        if not doc:
            return JSONResponse({"error": "Kayıt bulunamadı"}, status_code=404)

        # Stremio DB'den sil (ekle.py: delete_media_by_stream_id)
        stream_id = doc.get("db_id", "")
        if stream_id:
            try:
                await db.delete_media_by_stream_id(stream_id)
            except Exception as e:
                LOGGER.warning(f"[gdrive-db-sil] Stremio medya silme hatası: {e}")

        # ekle_approved'dan sil
        await col.delete_one({"_id": oid})

        return JSONResponse({"status": "success", "message": f"'{doc.get('title', doc.get('file_name', '?'))}' kaldırıldı."})

    except Exception as e:
        _logger.error("Silme hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)


# ──────────────────────────────────────────────────────────────────────────────
# Rclone entegrasyonu (sunucu.html paneli için)
# ──────────────────────────────────────────────────────────────────────────────

RCLONE_CONF_PATH   = Path(__file__).parent.parent.parent.parent / "rclone.conf"
RCLONE_PAGE_SIZE   = 20
VIDEO_EXTS_RCLONE  = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m4v", ".webm", ".flv", ".mpg", ".mpeg"}
APPROVED_COLLECTION_RCLONE = "ekle_approved"


def _rclone_bin() -> str:
    """
    rclone binary yolunu bulur.
    Önce PATH'te arar, bulamazsa yaygın kurulum konumlarını dener.
    Bulunamazsa RuntimeError fırlatır.
    """
    import shutil as _sh
    # 1) PATH'te ara
    found = _sh.which("rclone")
    if found:
        return found
    # 2) Yaygın kurulum yolları
    candidates = [
        "/usr/bin/rclone",
        "/usr/local/bin/rclone",
        "/usr/sbin/rclone",
        "/opt/rclone/rclone",
        "/app/.venv/bin/rclone",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    raise RuntimeError(
        "rclone binary bulunamadı. "
        "Dockerfile'da 'RUN curl https://rclone.org/install.sh | bash' satırı eklenip "
        "image yeniden build edilmeli."
    )


def _rclone_list_remotes_sync() -> list:
    """rclone.conf'tan sürücü listesi okur — rclone binary'si gerekmez."""
    import configparser
    if not RCLONE_CONF_PATH.exists():
        raise FileNotFoundError("rclone.conf bulunamadı. /ayarlar → Dosya Ekle ile yükleyin.")
    rcp = configparser.ConfigParser()
    rcp.read(str(RCLONE_CONF_PATH))
    return rcp.sections()


def _rclone_list_dir_sync(remote: str, path: str = "") -> dict:
    """rclone lsjson ile dizini listeler."""
    import subprocess as _sp, json as _json
    rclone = _rclone_bin()
    remote_path = f"{remote}:{path}"
    result = _sp.run(
        [rclone, "lsjson", "--config", str(RCLONE_CONF_PATH), remote_path, "--no-modtime"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        raise RuntimeError(f"rclone lsjson hatası: {err[:300]}")

    items = _json.loads(result.stdout or "[]")
    files, folders = [], []
    for it in items:
        name   = it.get("Name", "")
        is_dir = it.get("IsDir", False)
        if is_dir:
            folders.append({"name": name, "path": (path + "/" + name).lstrip("/"),
                            "is_dir": True, "size": 0})
        else:
            ext = Path(name).suffix.lower()
            if ext in VIDEO_EXTS_RCLONE:
                files.append({"name": name, "path": (path + "/" + name).lstrip("/"),
                               "is_dir": False, "size": it.get("Size", 0)})

    folder_name = path.split("/")[-1] if path else f"{remote} (Kök)"
    return {"items": folders + files, "folder_name": folder_name, "remote": remote, "path": path}


def _rclone_file_meta_sync(remote: str, path: str) -> dict:
    import subprocess as _sp, json as _json
    rclone = _rclone_bin()
    parent = str(Path(path).parent).replace("\\", "/")
    if parent in (".", ""):
        parent = ""
    list_target = f"{remote}:{parent}"
    result = _sp.run(
        [rclone, "lsjson", "--config", str(RCLONE_CONF_PATH), list_target, "--no-modtime"],
        capture_output=True, text=True, timeout=60
    )
    name = Path(path).name
    size = 0
    if result.returncode == 0:
        try:
            for it in _json.loads(result.stdout or "[]"):
                if it.get("Name") == name:
                    size = it.get("Size", 0)
                    break
        except Exception:
            pass
    return {"name": name, "size": size, "remote": remote, "path": path}


async def sunucu_rclone_remotes(request: Request, _: bool = Depends(require_auth)):
    """GET /api/sunucu/rclone-remotes — sürücü listesi"""
    loop = asyncio.get_event_loop()
    try:
        remotes = await loop.run_in_executor(None, _rclone_list_remotes_sync)
        return JSONResponse({"remotes": remotes})
    except FileNotFoundError as e:
        _logger.error("Internal error", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=503)
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)


async def sunucu_rclone_listele(request: Request, _: bool = Depends(require_auth)):
    """GET /api/sunucu/rclone-listele?remote=gdrive&path="""
    remote = request.query_params.get("remote", "").strip()
    path   = request.query_params.get("path", "").strip()
    if not remote:
        return JSONResponse({"error": "remote parametresi gerekli"}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: _rclone_list_dir_sync(remote, path))
        return JSONResponse(result)
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)


async def sunucu_rclone_meta_sorgu(request: Request, _: bool = Depends(require_auth)):
    """
    POST /api/sunucu/rclone-meta-sorgu
    Body: { "remote": "gdrive", "path": "Films/Inception.mkv" }
    Metadata sorgular, kaydetmez.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    remote = body.get("remote", "").strip()
    path   = body.get("path", "").strip()
    if not remote or not path:
        return JSONResponse({"error": "remote ve path gerekli"}, status_code=400)

    loop = asyncio.get_event_loop()
    try:
        file_meta = await loop.run_in_executor(None, lambda: _rclone_file_meta_sync(remote, path))
    except Exception as e:
        _logger.error("Dosya meta hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    file_name = file_meta.get("name", Path(path).name)

    from Backend.helper.pyro import clean_filename
    clean_name = clean_filename(file_name)

    try:
        from Backend.helper.metadata import extract_default_id
        override_id, _ = extract_default_id(file_name)
        meta_info = await fetch_metadata(clean_name, 0, 0, override_id=override_id)
    except Exception as e:
        _logger.error("Metadata hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    if meta_info is None:
        return JSONResponse({"error": "Metadata bulunamadı", "file_name": file_name}, status_code=404)

    return JSONResponse({
        "meta":      meta_info,
        "file_name": file_name,
        "size":      file_meta.get("size", 0),
        "remote":    remote,
        "path":      path,
    })


async def sunucu_rclone_ekle_onay(request: Request, _: bool = Depends(require_auth)):
    """
    POST /api/sunucu/rclone-ekle-onay
    Body: { "remote": "gdrive", "path": "Films/Inception.mkv", "meta": {...} }
    Onaylanan metadatayla Rclone içeriğini DB'ye ekler.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    remote    = body.get("remote", "").strip()
    path      = body.get("path", "").strip()
    meta_info = body.get("meta")

    if not remote or not path or not meta_info:
        return JSONResponse({"error": "remote, path ve meta gerekli"}, status_code=400)

    rclone_key = f"{remote}:{path}"

    # storage'ı scope dışında tanımla — aşağıda kayıt için de kullanılacak
    storage = db.dbs.get(f"storage_{db.current_db_index}")

    # Daha önce eklendi mi?
    try:
        if storage is not None:
            existing = await storage[APPROVED_COLLECTION_RCLONE].find_one({"rclone_path": rclone_key})
            if existing:
                return JSONResponse(
                    {"error": f"Bu dosya zaten eklendi: {existing.get('file_name', '?')}"},
                    status_code=409
                )
    except Exception:
        pass

    loop = asyncio.get_event_loop()
    try:
        file_meta = await loop.run_in_executor(None, lambda: _rclone_file_meta_sync(remote, path))
    except Exception as e:
        _logger.error("Dosya meta hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    file_name = file_meta.get("name", Path(path).name)
    size_val  = file_meta.get("size", 0)
    size_str  = _human_size(size_val)

    # encoded_string üret
    rclone_encoded = ""
    try:
        from Backend.helper.encrypt import encode_string as _enc
        rclone_encoded = await _enc({"rclone_remote": remote, "rclone_path": path})
        meta_info["encoded_string"] = rclone_encoded
    except Exception as e:
        LOGGER.warning(f"[rclone-ekle-onay] encoded_string hatası: {e}")

    # ── JS → insert_media key normalizasyonu ──────────────────────────────
    # JS "rating" gönderir, insert_media "rate" bekler
    if "rate" not in meta_info:
        meta_info["rate"] = meta_info.pop("rating", 0)
    try:
        meta_info["rate"] = float(meta_info["rate"] or 0)
    except (TypeError, ValueError):
        meta_info["rate"] = 0.0

    # year → int
    try:
        meta_info["year"] = int(meta_info.get("year") or 0)
    except (TypeError, ValueError):
        meta_info["year"] = 0

    # tmdb_id → int (boş olabilir)
    try:
        meta_info["tmdb_id"] = int(meta_info.get("tmdb_id") or 0)
    except (TypeError, ValueError):
        meta_info["tmdb_id"] = 0

    # Ortak zorunlu alanlar için varsayılanlar
    meta_info.setdefault("imdb_id", "")
    meta_info.setdefault("description", "")
    meta_info.setdefault("backdrop", "")
    meta_info.setdefault("logo", "")
    meta_info.setdefault("cast", [])
    meta_info.setdefault("runtime", "")
    meta_info.setdefault("genres", [])
    meta_info.setdefault("title_tr", "")
    meta_info.setdefault("title_de", "")
    meta_info.setdefault("description_tr", "")
    meta_info.setdefault("description_de", "")
    meta_info.setdefault("genres_tr", [])
    meta_info.setdefault("genres_de", [])
    meta_info.setdefault("poster_tr", "")
    meta_info.setdefault("backdrop_tr", "")
    meta_info.setdefault("logo_tr", "")
    meta_info.setdefault("poster_de", "")
    meta_info.setdefault("backdrop_de", "")
    meta_info.setdefault("logo_de", "")
    meta_info.setdefault("original_language", None)
    meta_info.setdefault("collection_id", None)
    meta_info.setdefault("certification_tr", None)
    meta_info.setdefault("certification_de", None)
    meta_info.setdefault("certification_us", None)

    # TV dizisi için zorunlu episode alanları
    if meta_info.get("media_type", "movie") != "movie":
        meta_info.setdefault("season_number", 1)
        meta_info.setdefault("episode_number", 1)
        meta_info.setdefault("episode_title", "")
        meta_info.setdefault("episode_title_tr", "")
        meta_info.setdefault("episode_title_de", "")
        meta_info.setdefault("episode_backdrop", "")
        meta_info.setdefault("episode_overview", "")
        meta_info.setdefault("episode_overview_tr", "")
        meta_info.setdefault("episode_overview_de", "")
        meta_info.setdefault("episode_released", "")
    # ──────────────────────────────────────────────────────────────────────

    from Backend.helper.pyro import remove_urls, clean_filename
    display_name = remove_urls(file_name)
    if Path(display_name).suffix.lower() not in VIDEO_EXTS_RCLONE:
        display_name += ".mkv"

    try:
        updated_id = await db.insert_media(
            meta_info,
            channel=0,
            msg_id=0,
            size=size_str,
            name=display_name,
        )
    except Exception as e:
        LOGGER.error(f"[rclone-ekle-onay] DB insert hatası: {e}")
        _logger.error("DB hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)

    if not updated_id:
        return JSONResponse({"error": "DB kaydı başarısız"}, status_code=500)

    # ekle_approved'a kaydet
    # insert_media sonrası current_db_index değişmiş olabilir, storage'ı yenile
    storage = db.dbs.get(f"storage_{db.current_db_index}")
    record = {
        "rclone_path": rclone_key,
        "file_name":   file_name,
        "title":       meta_info.get("title", file_name),
        "db_id":       rclone_encoded,
        "size":        size_str,
        "remote":      remote,
        "source":      "rclone",
        "added_at":    int(time.time()),
    }
    try:
        if storage is not None:
            result = await storage[APPROVED_COLLECTION_RCLONE].insert_one(record)
            LOGGER.info(f"[rclone-ekle-onay] ekle_approved OK: {result.inserted_id} | db=storage_{db.current_db_index}")
        else:
            LOGGER.error(f"[rclone-ekle-onay] storage bulunamadı! current_db_index={db.current_db_index}")
    except Exception as e:
        LOGGER.error(f"[rclone-ekle-onay] approved insert hatası: {e}")

    LOGGER.info(f"[rclone-ekle-onay] ✅ Eklendi: {meta_info.get('title')} | {rclone_key}")
    return JSONResponse({
        "status":  "success",
        "title":   meta_info.get("title", file_name),
        "db_id":   rclone_encoded,
        "size":    size_str,
        "display": display_name,
        "type":    meta_info.get("media_type", "movie"),
    })


async def sunucu_rclone_db_listele(request: Request, _: bool = Depends(require_auth)):
    """GET /api/sunucu/rclone-db-listele?page=0"""
    try:
        page = int(request.query_params.get("page", "0"))
    except ValueError:
        page = 0

    page_size = 20
    try:
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is None:
            return JSONResponse({"items": [], "total": 0})
        col = storage[APPROVED_COLLECTION_RCLONE]
        # source="rclone" veya rclone_path alanı olan kayıtları getir
        query = {"$or": [{"source": "rclone"}, {"rclone_path": {"$exists": True}}]}
        total  = await col.count_documents(query)
        cursor = col.find(query).sort("added_at", -1).skip(page * page_size).limit(page_size)
        items  = await cursor.to_list(length=page_size)
        for it in items:
            it["_id"] = str(it["_id"])
            # doc_id alanını da ekle (JS uyumluluğu için)
            it["doc_id"] = it["_id"]
        return JSONResponse({"items": items, "total": total, "page": page, "page_size": page_size})
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)


async def sunucu_rclone_db_sil(request: Request, _: bool = Depends(require_auth)):
    """DELETE /api/sunucu/rclone-db-sil  Body: { "doc_id": "..." }"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçersiz JSON"}, status_code=400)

    doc_id = body.get("doc_id", "").strip()
    if not doc_id:
        return JSONResponse({"error": "doc_id gerekli"}, status_code=400)

    from bson import ObjectId
    try:
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is None:
            return JSONResponse({"error": "DB storage bulunamadı"}, status_code=500)

        col = storage[APPROVED_COLLECTION_RCLONE]
        try:
            oid = ObjectId(doc_id)
        except Exception:
            return JSONResponse({"error": "Geçersiz doc_id"}, status_code=400)

        doc = await col.find_one({"_id": oid})
        if not doc:
            return JSONResponse({"error": "Kayıt bulunamadı"}, status_code=404)

        stream_id = doc.get("db_id", "")
        if stream_id:
            try:
                await db.delete_media_by_stream_id(stream_id)
            except Exception as e:
                LOGGER.warning(f"[rclone-db-sil] Stremio medya silme hatası: {e}")

        await col.delete_one({"_id": oid})
        return JSONResponse({"status": "success", "message": f"'{doc.get('title', doc.get('file_name', '?'))}' kaldırıldı."})

    except Exception as e:
        _logger.error("Silme hatası", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)


async def sunucu_rclone_migrate(request: Request, _: bool = Depends(require_auth)):
    """
    POST /api/sunucu/rclone-migrate
    Stremio DB'deki mevcut rclone içeriklerini ekle_approved'a aktar.
    """
    migrated = 0
    errors   = 0
    try:
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is None:
            return JSONResponse({"error": "DB storage bulunamadı"}, status_code=500)
        col = storage[APPROVED_COLLECTION_RCLONE]

        async def _try_decode_rclone(encoded: str):
            try:
                from Backend.helper.encrypt import decode_string as _dec
                d = await _dec(encoded)
                if isinstance(d, dict) and d.get("rclone_remote") and d.get("rclone_path"):
                    return d
            except Exception:
                pass
            return None

        for cname in ("movie", "tv"):
            for i in range(1, db.current_db_index + 1):
                sdb = db.dbs.get(f"storage_{i}")
                if sdb is None:
                    continue
                cursor = sdb[cname].find({})
                async for doc in cursor:
                    try:
                        encoded = ""
                        if cname == "movie":
                            # telegram is List[QualityDetail], not a dict
                            tg_list = doc.get("telegram", [])
                            if isinstance(tg_list, list) and tg_list:
                                encoded = tg_list[0].get("id", "")
                            elif isinstance(tg_list, dict):
                                encoded = tg_list.get("id", "")
                        else:
                            for season in doc.get("seasons", []):
                                for ep in season.get("episodes", []):
                                    tg_list = ep.get("telegram", [])
                                    if isinstance(tg_list, list) and tg_list:
                                        encoded = tg_list[0].get("id", "")
                                    elif isinstance(tg_list, dict):
                                        encoded = tg_list.get("id", "")
                                    if encoded:
                                        break
                                if encoded:
                                    break

                        if not encoded:
                            continue

                        rc = await _try_decode_rclone(encoded)
                        if not rc:
                            continue

                        rclone_key = f"{rc['rclone_remote']}:{rc['rclone_path']}"
                        already = await col.find_one({"rclone_path": rclone_key})
                        if already:
                            continue

                        record = {
                            "rclone_path": rclone_key,
                            "file_name":   Path(rc["rclone_path"]).name,
                            "title":       doc.get("title", Path(rc["rclone_path"]).name),
                            "db_id":       encoded,
                            "size":        "",
                            "remote":      rc["rclone_remote"],
                            "source":      "rclone",
                            "added_at":    int(time.time()),
                        }
                        await col.insert_one(record)
                        migrated += 1
                    except Exception as e:
                        LOGGER.warning(f"[rclone-migrate] Hata: {e}")
                        errors += 1

        return JSONResponse({"status": "success", "migrated": migrated, "errors": errors})
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return JSONResponse({"error": "Sunucu hatası"}, status_code=500)
