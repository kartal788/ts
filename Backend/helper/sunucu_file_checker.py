"""
sunucu_file_checker.py
======================
Sunucuda (SUNUCU_DIR) duran dosyalar için eklenen Stremio/DB kayıtlarını,
fiziksel dosya SİLİNİNCE otomatik temizler.

Neden gerekli?
  "HTTPS'den Dosya İndir" ile eklenen kayıtlar DB'de şifreli bir kimlik
  (encode_string({"local_path": ...})) olarak durur. Eski kontrol bu kimliği
  çözmediği için dosya sunucudan silinse bile kayıt Stremio'da kalıyordu.

Çalışma şekli:
  1) Bot açılışında bir kez,
  2) Ardından her CHECK_INTERVAL saniyede bir (panelden, bottan ya da SSH ile
     silinmiş olması fark etmez),
  3) sunucu.html'den dosya/klasör silinince hemen,
  4) Stremio bir akış listelerken dosya yoksa (debounce ile) tetiklenir.

Güvenlik: SUNUCU_DIR bağlı değilse / boşsa (ör. disk takılı değil) periyodik
tarama hiçbir şeyi silmez; yanlışlıkla tüm kataloğun silinmesi önlenir.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("sunucu_file_checker")

CHECK_INTERVAL = 120          # saniye: periyodik tarama
STARTUP_DELAY = 20            # saniye: açılıştan sonra ilk tarama gecikmesi
SOON_DELAY = 5                # saniye: "yakında tara" isteği debounce süresi
_CACHE_MAX = 50000

# sid -> Path | None   (None = sunucu dosyası değil / çözülemedi)
_path_cache: dict[str, Path | None] = {}
_scan_lock = asyncio.Lock()
_soon_task: asyncio.Task | None = None
_bg_tasks: set = set()


def _sunucu_root() -> Path:
    """Gerçek SUNUCU_DIR: sunucu_routes ile aynı kaynak (tek doğruluk noktası)."""
    try:
        from Backend.fastapi.routes.sunucu_routes import SUNUCU_DIR
        return Path(SUNUCU_DIR)
    except Exception:
        default = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
        return Path(os.getenv("SUNUCU_DIR", default))


async def resolve_local_path(stream_id: str) -> Path | None:
    """Kimlik bir sunucu (yerel) dosyasına işaret ediyorsa Path döner, değilse None.

    Desteklenen biçimler:
      1. Şifreli kimlik  → decode_string(...)["local_path"]   (HTTPS indir / panel)
      2. https://host/api/sunucu/indir?path=klasor/film.mkv     (eski tip)
      3. Mutlak yol      → /app/uploads/film.mkv
    Telegram / Drive / WebDAV / Rclone / harici linkler → None (dokunulmaz).
    """
    sid = (stream_id or "").strip()
    if not sid:
        return None
    if sid in _path_cache:
        return _path_cache[sid]

    result: Path | None = None
    try:
        if sid.startswith(("http://", "https://")):
            if "/api/sunucu/indir" in sid:
                rel = parse_qs(urlparse(sid).query).get("path", [""])[0]
                if rel:
                    root = _sunucu_root().resolve()
                    cand = (root / rel.lstrip("/\\")).resolve()
                    try:
                        cand.relative_to(root)
                        result = cand
                    except ValueError:
                        result = None
        elif sid.startswith("/") or (len(sid) > 1 and sid[1] == ":"):
            result = Path(sid)
        else:
            from Backend.helper.encrypt import decode_string
            try:
                decoded = await decode_string(sid)
            except Exception:
                decoded = None
            if isinstance(decoded, dict):
                lp = decoded.get("local_path")
                # Drive/WebDAV/Rclone kayıtlarında local_path yoktur → None
                if lp:
                    result = Path(lp)
    except Exception:
        result = None

    if len(_path_cache) >= _CACHE_MAX:
        _path_cache.clear()
    _path_cache[sid] = result
    return result


def _root_is_trustworthy() -> bool:
    """SUNUCU_DIR var mı ve en az bir öğe içeriyor mu? (bağlanmamış disk koruması)"""
    try:
        root = _sunucu_root()
        return root.is_dir() and any(root.iterdir())
    except Exception:
        return False


async def purge_missing_sunucu_records(force: bool = False) -> int:
    """Fiziksel dosyası olmayan sunucu kayıtlarını DB'den (Stremio) siler.

    force=True → kök dizin boş olsa bile çalışır (kullanıcı panelden bilerek sildiğinde).
    Dönüş: silinen kayıt sayısı.
    """
    try:
        from Backend import db as _db
    except Exception as e:
        logger.error("[sunucu-checker] DB import hatası: %s", e)
        return 0

    if not force and not _root_is_trustworthy():
        logger.warning("[sunucu-checker] SUNUCU_DIR boş/erişilemiyor; güvenlik için tarama atlandı.")
        return 0
    if not _sunucu_root().is_dir():
        logger.warning("[sunucu-checker] SUNUCU_DIR bulunamadı; tarama atlandı.")
        return 0

    async with _scan_lock:
        checked = 0
        missing: list[tuple[str, Path]] = []

        try:
            for i in range(1, _db.current_db_index + 1):
                col_db = _db.dbs[f"storage_{i}"]

                async for movie in col_db["movie"].find({}, {"telegram.id": 1}):
                    for q in movie.get("telegram", []) or []:
                        sid = q.get("id", "")
                        local = await resolve_local_path(sid)
                        if local is None:
                            continue
                        checked += 1
                        if not os.path.exists(local):
                            missing.append((sid, local))

                async for tv in col_db["tv"].find({}, {"seasons.episodes.telegram.id": 1}):
                    for season in tv.get("seasons", []) or []:
                        for ep in season.get("episodes", []) or []:
                            for q in ep.get("telegram", []) or []:
                                sid = q.get("id", "")
                                local = await resolve_local_path(sid)
                                if local is None:
                                    continue
                                checked += 1
                                if not os.path.exists(local):
                                    missing.append((sid, local))
        except Exception as e:
            logger.exception("[sunucu-checker] Tarama hatası: %s", e)
            return 0

        removed = 0
        for sid, local in missing:
            try:
                if await _db.delete_media_by_stream_id(sid):
                    removed += 1
                    logger.info("[sunucu-checker] Dosya yok → Stremio/DB kaydı silindi: %s", local)
            except Exception as e:
                logger.warning("[sunucu-checker] Kayıt silinemedi (%s): %s", local, e)
            _path_cache.pop(sid, None)

        # Bu dosyalar için bekleyen zamanlı silme kayıtları artık anlamsız
        if missing:
            try:
                from Backend.helper.scheduled_delete import _col as _sd_col
                paths = list({str(p) for _, p in missing})
                await _sd_col().delete_many({
                    "abs_path": {"$in": paths},
                    "status": {"$in": ["pending", "failed"]},
                })
            except Exception:
                pass

        if removed:
            try:
                from Backend.helper.platform_catalog import platform_catalog as _pc
                _pc.schedule_refresh()
            except Exception:
                pass

        logger.info("[sunucu-checker] Tamamlandı — kontrol: %d, silinen: %d", checked, removed)
        return removed


def request_purge_soon() -> None:
    """Hafif tetikleyici: birkaç saniye sonra tek bir tarama yaptırır (debounce).
    Stremio akış listelerken eksik dosya görülünce çağrılır."""
    global _soon_task
    if _soon_task is not None and not _soon_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run():
        await asyncio.sleep(SOON_DELAY)
        try:
            await purge_missing_sunucu_records()
        except Exception as e:
            logger.warning("[sunucu-checker] Hızlı tarama hatası: %s", e)

    _soon_task = loop.create_task(_run())


async def check_and_clean_missing_sunucu_files() -> None:
    """Açılışta başlatılır: kısa bir gecikmeden sonra ilk taramayı yapar ve sonra
    CHECK_INTERVAL aralığıyla sürekli tekrarlar (hiç durmaz)."""
    logger.info("[sunucu-checker] Başladı (SUNUCU_DIR=%s, aralık=%ds)", _sunucu_root(), CHECK_INTERVAL)
    await asyncio.sleep(STARTUP_DELAY)
    while True:
        try:
            await purge_missing_sunucu_records()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("[sunucu-checker] Döngü hatası: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)
