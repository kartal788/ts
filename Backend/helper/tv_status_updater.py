"""
tv_status_updater.py
=====================
Her gün Türkiye saatinde (UTC+3) 05:00'da veritabanındaki tüm dizilerin
"status" alanını TMDB'den sorgulayarak günceller.

TMDB status değerleri:
  - "Returning Series"  → devam eden dizi
  - "Ended"             → sona ermiş
  - "Canceled"          → iptal edilmiş
  - "In Production"     → yapım aşamasında
  - "Planned"           → planlanmış

Çalışma mantığı:
  1. Tüm storage_N veritabanlarındaki "tv" koleksiyonunu tara.
  2. tmdb_id alanı olan her doküman için TMDB'den status çek.
  3. Mevcut değerden farklıysa güncelle, aynıysa atla.
  4. Her istek arasında kısa bekleme uygula (rate-limit koruması).
  5. Hata alan dizileri geç, loglayıp devam et.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger("tv_status_updater")

_TMDB_BASE = "https://api.themoviedb.org/3"
_REQUEST_DELAY = float(os.getenv("TV_STATUS_UPDATE_DELAY", "0.25"))  # saniye / istek

_status_timer: threading.Timer | None = None
_status_running: bool = False
_main_loop: asyncio.AbstractEventLoop | None = None

_REQUEST_TIMEOUT = float(os.getenv("TV_STATUS_REQUEST_TIMEOUT", "20"))
_MAX_RETRIES = int(os.getenv("TV_STATUS_MAX_RETRIES", "3"))

_http_client_lock = threading.Lock()
_http_client: httpx.Client | None = None


def _get_http_client() -> httpx.Client:
    """Tüm istekler için tek, yeniden kullanılan bir httpx.Client döner.
    Her istekte yeni bağlantı açıp kapatmak yerine bağlantı havuzunu (keep-alive)
    kullanır — bu, ardışık 'read timeout' hatalarının en sık nedenlerinden biri
    olan tekrarlayan TCP/TLS el sıkışmasını ortadan kaldırır."""
    global _http_client
    with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.Client(
                timeout=httpx.Timeout(_REQUEST_TIMEOUT, connect=10),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
        return _http_client


def _reset_http_client() -> None:
    global _http_client
    with _http_client_lock:
        if _http_client is not None:
            try:
                _http_client.close()
            except Exception:
                pass
        _http_client = None


# ── TMDB yardımcıları ──────────────────────────────────────────────────────────

def _get_api_key() -> str:
    try:
        from Backend.config import Telegram
        return Telegram.TMDB_API
    except Exception:
        return os.getenv("TMDB_API", "")


def _fetch_tv_status(tmdb_id: int) -> Optional[str]:
    """TMDB'den senkron olarak tek bir dizinin status değerini çeker.
    Geçici ağ hatalarında (timeout/bağlantı hatası) kısa aralıklarla birkaç
    kez daha dener; kalıcı hatalarda (404, geçersiz anahtar) hemen vazgeçer."""
    api_key = _get_api_key()
    if not api_key:
        return None

    client = _get_http_client()
    last_error: Exception | None = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            r = client.get(
                f"{_TMDB_BASE}/tv/{tmdb_id}",
                params={"api_key": api_key, "language": "en-US"},
            )
            r.raise_for_status()
            return r.json().get("status") or None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug("[tv-status] TMDB %d → 404, atlanıyor.", tmdb_id)
            else:
                logger.warning("[tv-status] TMDB %d HTTP hatası: %s", tmdb_id, e)
            return None  # HTTP hatası tekrar denenmez
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                logger.debug(
                    "[tv-status] TMDB %d isteği zaman aşımına uğradı (deneme %d/%d), "
                    "tekrar denenecek: %s", tmdb_id, attempt, _MAX_RETRIES, e
                )
                time.sleep(1.5 * attempt)  # artan bekleme (1.5s, 3s, ...)
                # Bağlantı sorunlarında istemciyi yenile — takılı kalan bir
                # bağlantı havuzuna tekrar tekrar çarpmayı önler.
                _reset_http_client()
                client = _get_http_client()
            continue
        except Exception as e:
            last_error = e
            break

    logger.warning("[tv-status] TMDB %d isteği başarısız: %s", tmdb_id, last_error)
    return None


# ── Zamanlama yardımcıları ────────────────────────────────────────────────────

def _seconds_until_05_utc3() -> float:
    """UTC+3 bir sonraki 05:00'a kaç saniye kaldığını döner."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Istanbul")
    now = datetime.now(tz)
    target = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ── Ana güncelleme işlevi ─────────────────────────────────────────────────────

async def _run_status_update_async() -> None:
    """
    Tüm storage veritabanlarındaki TV dizilerini tarar,
    TMDB'den status alır ve değişenleri günceller.
    """
    try:
        from Backend import db as _db
    except Exception as e:
        logger.error("[tv-status] DB import hatası: %s", e)
        return

    # Kaç storage DB var?
    storage_keys = [k for k in _db.dbs if k.startswith("storage_")]
    if not storage_keys:
        logger.warning("[tv-status] Hiç storage DB bulunamadı, atlanıyor.")
        return

    logger.info("[tv-status] Güncelleme başlıyor — %d storage DB taranacak.", len(storage_keys))
    t0 = datetime.utcnow()

    total_checked = 0
    total_updated = 0
    total_skipped = 0
    total_errors  = 0
    consecutive_failures = 0
    _CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("TV_STATUS_CIRCUIT_BREAKER", "10"))

    for db_key in storage_keys:
        col = _db.dbs[db_key]["tv"]

        # tmdb_id alanı olan tüm dizileri çek (sadece gerekli alanlar)
        cursor = col.find(
            {"tmdb_id": {"$exists": True, "$ne": None}},
            {"_id": 1, "tmdb_id": 1, "imdb_id": 1, "title": 1, "status": 1},
        )

        async for doc in cursor:
            tmdb_id_raw = doc.get("tmdb_id")
            if not tmdb_id_raw:
                continue

            # tmdb_id string veya int olabilir
            try:
                tmdb_id = int(tmdb_id_raw)
            except (ValueError, TypeError):
                total_skipped += 1
                continue

            total_checked += 1
            old_status = doc.get("status")

            # Senkron TMDB isteğini thread pool'da çalıştır (event loop'u bloklamaz)
            try:
                new_status = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_tv_status, tmdb_id
                )
            except Exception as e:
                logger.warning(
                    "[tv-status] %s (tmdb=%d) sorgulama hatası: %s",
                    doc.get("imdb_id", "?"), tmdb_id, e
                )
                total_errors += 1
                consecutive_failures += 1
                new_status = None

            if new_status is None:
                total_errors += 1
                consecutive_failures += 1

                # ── Devre kesici: TMDB'ye art arda çok sayıda ulaşılamıyorsa
                # (ör. ağ/DNS kesintisi) her dizi için tek tek 20sn timeout
                # bekleyip loga aynı hatayı basmak yerine, bir süre tamamen
                # duraklat ve tek bir uyarı ver. ──────────────────────────
                if consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                    cooldown = 120
                    logger.warning(
                        "[tv-status] TMDB'ye art arda %d istek başarısız oldu — "
                        "olası ağ/servis kesintisi. %ds beklenip tekrar denenecek.",
                        consecutive_failures, cooldown,
                    )
                    _reset_http_client()
                    await asyncio.sleep(cooldown)
                    consecutive_failures = 0

                await asyncio.sleep(_REQUEST_DELAY)
                continue

            consecutive_failures = 0

            if new_status == old_status:
                total_skipped += 1
            else:
                try:
                    await col.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"status": new_status}},
                    )
                    logger.debug(
                        "[tv-status] %s (tmdb=%d): '%s' → '%s'",
                        doc.get("imdb_id", doc.get("title", "?")),
                        tmdb_id, old_status, new_status,
                    )
                    total_updated += 1
                except Exception as e:
                    logger.warning(
                        "[tv-status] %s (tmdb=%d) DB yazma hatası: %s",
                        doc.get("imdb_id", "?"), tmdb_id, e
                    )
                    total_errors += 1

            # Rate-limit koruması
            await asyncio.sleep(_REQUEST_DELAY)

    elapsed = (datetime.utcnow() - t0).total_seconds()
    logger.info(
        "[tv-status] Tamamlandı %.1fs — kontrol: %d, güncellendi: %d, "
        "değişmedi: %d, hata: %d",
        elapsed, total_checked, total_updated, total_skipped, total_errors,
    )


# ── threading.Timer callback'i ────────────────────────────────────────────────

def _run_status_update() -> None:
    """threading.Timer callback — async güncellemeyi ana loop'ta çalıştırır."""
    if not _status_running:
        return

    logger.info("[tv-status] UTC+3 05:00 — dizi status güncellemesi başlatılıyor…")

    loop = _main_loop
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_run_status_update_async(), loop)
        try:
            # Büyük koleksiyonlar için zaman aşımı yüksek tutuldu
            future.result(timeout=7200)
        except Exception as e:
            logger.exception("[tv-status] run_coroutine_threadsafe hatası: %s", e)
    else:
        logger.warning("[tv-status] Ana loop bulunamadı, asyncio.run() ile çalışıyor.")
        asyncio.run(_run_status_update_async())

    _schedule_next_status_update()


def _schedule_next_status_update() -> None:
    global _status_timer
    if not _status_running:
        return
    delay = _seconds_until_05_utc3()
    logger.info(
        "[tv-status] Bir sonraki güncelleme %.0f saniye sonra "
        "(UTC+3 05:00).", delay
    )
    _status_timer = threading.Timer(delay, _run_status_update)
    _status_timer.daemon = True
    _status_timer.name = "tv-status-updater"
    _status_timer.start()


# ── Public API ────────────────────────────────────────────────────────────────

def start_tv_status_scheduler(main_loop: asyncio.AbstractEventLoop | None = None) -> None:
    """
    db_scheduler.start_scheduler() içinden çağrılır.
    main_loop: FastAPI startup'ta aktif olan asyncio event loop'u.
    """
    global _status_running, _main_loop
    _status_running = True
    _main_loop = main_loop

    # Bot yeniden başladığında hemen bir kez çalıştır (daemon thread)
    def _startup_run() -> None:
        logger.info("[tv-status] Bot başlangıcında anlık güncelleme başlatılıyor…")
        loop = _main_loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(_run_status_update_async(), loop)
            try:
                future.result(timeout=7200)
            except Exception as e:
                logger.exception("[tv-status] Başlangıç güncellemesi hatası: %s", e)
        else:
            asyncio.run(_run_status_update_async())

    startup_thread = threading.Thread(target=_startup_run, daemon=True, name="tv-status-startup")
    startup_thread.start()

    _schedule_next_status_update()
    logger.info("[tv-status] Zamanlayıcı başlatıldı (bot başlangıcında + her gün UTC+3 05:00).")


def stop_tv_status_scheduler() -> None:
    global _status_running, _status_timer
    _status_running = False
    if _status_timer:
        _status_timer.cancel()
        _status_timer = None
    logger.info("[tv-status] Zamanlayıcı durduruldu.")
