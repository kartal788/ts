"""
sunucu_file_checker.py
======================
Bot başladığında çalışır.

DB'deki sunucu dosyası kayıtlarını tarar:
- Fiziksel dosya VARSA  → DB'de bırak (dokunma)
- Fiziksel dosya YOKSA  → DB kaydını sil
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("sunucu_file_checker")

# sunucu_routes.py:  Backend/fastapi/routes/sunucu_routes.py → dirname×3 → proje_koku/uploads
# Bu dosya:          Backend/helper/sunucu_file_checker.py   → dirname×2 → Backend/uploads
_DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "uploads"
)
SUNUCU_DIR = Path(os.getenv("SUNUCU_DIR", _DEFAULT_DIR))


def _resolve_local_path(stream_id: str) -> Path | None:
    """
    stream_id bir sunucu dosyasına işaret ediyorsa Path döner, değilse None.

    Desteklenen formatlar:
      1. https://host/api/sunucu/indir?path=klasor/film.mkv
      2. /app/Backend/uploads/film.mkv  (mutlak yol)
    """
    sid = stream_id.strip()

    # Format 1: /api/sunucu/indir URL'si
    if sid.startswith(("http://", "https://")):
        if "/api/sunucu/indir" not in sid:
            return None  # Telegram / GDrive / harici — dokunma
        qs = parse_qs(urlparse(sid).query)
        rel = qs.get("path", [""])[0]
        if not rel:
            return None
        candidate = (SUNUCU_DIR / rel.lstrip("/\\")).resolve()
        try:
            candidate.relative_to(SUNUCU_DIR.resolve())  # path-traversal koruması
        except ValueError:
            return None
        return candidate

    # Format 2: Mutlak dosya yolu
    if sid.startswith("/") or (len(sid) > 1 and sid[1] == ":"):
        return Path(sid)

    return None  # Telegram file_id / encoded_string — dokunma


async def check_and_clean_missing_sunucu_files() -> None:
    """
    Startup'ta asyncio.create_task() ile arka planda çağrılır.
    Fiziksel dosyası olmayan sunucu kayıtlarını DB'den siler.
    Dosya sunucuda duruyorsa hiçbir şeye dokunmaz.
    """
    try:
        from Backend import db as _db
    except Exception as e:
        logger.error("[sunucu-checker] DB import hatası: %s", e)
        return

    logger.info("[sunucu-checker] Sunucu dosyası kontrolü başlıyor… (SUNUCU_DIR=%s)", SUNUCU_DIR)
    checked = removed = 0

    try:
        for i in range(1, _db.current_db_index + 1):
            col_db = _db.dbs[f"storage_{i}"]

            # ── Filmler ───────────────────────────────────────────────────────
            async for movie in col_db["movie"].find({}):
                missing_ids = []
                for q in movie.get("telegram", []):
                    sid = q.get("id", "")
                    local = _resolve_local_path(sid)
                    if local is None:
                        continue  # Sunucu dosyası değil — atla
                    checked += 1
                    if local.exists():
                        continue  # Dosya var — dokunma
                    logger.warning(
                        "[sunucu-checker] Dosya yok, DB'den silinecek (film): %s", local
                    )
                    missing_ids.append(sid)

                for sid in missing_ids:
                    if await _db.delete_media_by_stream_id(sid):
                        removed += 1
                        logger.info("[sunucu-checker] DB kaydı silindi (film): %s", sid)

            # ── Diziler ───────────────────────────────────────────────────────
            async for tv in col_db["tv"].find({}):
                for season in tv.get("seasons", []):
                    for episode in season.get("episodes", []):
                        for q in episode.get("telegram", []):
                            sid = q.get("id", "")
                            local = _resolve_local_path(sid)
                            if local is None:
                                continue
                            checked += 1
                            if local.exists():
                                continue  # Dosya var — dokunma
                            logger.warning(
                                "[sunucu-checker] Dosya yok, DB'den silinecek (dizi): %s", local
                            )
                            if await _db.delete_media_by_stream_id(sid):
                                removed += 1
                                logger.info("[sunucu-checker] DB kaydı silindi (dizi): %s", sid)

    except Exception as e:
        logger.exception("[sunucu-checker] Tarama hatası: %s", e)
        return

    logger.info(
        "[sunucu-checker] Tamamlandı — kontrol: %d, silinen: %d", checked, removed
    )
