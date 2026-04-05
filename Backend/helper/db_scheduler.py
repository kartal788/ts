"""
db_scheduler.py
================
Uygulama açılınca platform kataloğunu hemen yükler,
sonra her 15 dakikada bir yeniler.

Ayrıca UTC+3 (Europe/Istanbul) gece 00:00'da tüm API token
günlük kullanımlarını otomatik sıfırlar — video izlenmese bile.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta

logger = logging.getLogger("db_scheduler")

_INTERVAL = int(os.getenv("BACKUP_INTERVAL_MIN", "15")) * 60
_timer: threading.Timer | None = None
_daily_timer: threading.Timer | None = None
_running = False

# FastAPI startup sırasında kaydedilen ana event loop.
# threading.Timer callback'lerinden run_coroutine_threadsafe ile kullanılır.
_main_loop: asyncio.AbstractEventLoop | None = None


# ─── Platform kataloğu ────────────────────────────────────────────────────────

def _refresh() -> None:
    try:
        from Backend.helper.platform_catalog import platform_catalog
        platform_catalog.refresh()
    except Exception as e:
        logger.exception("Katalog yenileme hatası: %s", e)


def _schedule_next() -> None:
    global _timer
    if not _running:
        return
    _timer = threading.Timer(_INTERVAL, _cycle)
    _timer.daemon = True
    _timer.start()


def _cycle() -> None:
    if not _running:
        return
    logger.info("Periyodik katalog yenilemesi başlıyor…")
    _refresh()
    _schedule_next()


# ─── Günlük sıfırlama (UTC+3 00:00) ──────────────────────────────────────────

def _seconds_until_midnight_utc3() -> float:
    """UTC+3 bir sonraki 00:00'a kaç saniye kaldığını döner."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Istanbul")
    now = datetime.now(tz)
    tomorrow_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (tomorrow_midnight - now).total_seconds()


def _run_daily_reset() -> None:
    """Token günlük kullanımlarını sıfırlar, ardından bir sonraki geceyi planlar."""
    if not _running:
        return

    logger.info("[daily-reset] UTC+3 00:00 — günlük token kullanımları sıfırlanıyor…")

    async def _do_reset():
        try:
            from Backend import db
            count = await db.reset_all_daily_usage()
            logger.info("[daily-reset] %d token sıfırlandı.", count)
        except Exception as e:
            logger.exception("[daily-reset] Sıfırlama hatası: %s", e)

    # Startup sırasında kaydedilen ana loop'u kullan.
    # threading.Timer farklı bir thread'den çalıştığı için
    # asyncio.get_event_loop() Python 3.10+'da yeni (çalışmayan) bir loop döndürür.
    # run_coroutine_threadsafe ana loop'a güvenle coroutine gönderir.
    loop = _main_loop
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_do_reset(), loop)
        try:
            future.result(timeout=30)  # en fazla 30 sn bekle
        except Exception as e:
            logger.exception("[daily-reset] run_coroutine_threadsafe hatası: %s", e)
    else:
        # Fallback: loop henüz kaydedilmemişse veya durmuşsa yeni loop aç
        logger.warning("[daily-reset] Ana loop bulunamadı, asyncio.run() ile çalışıyor.")
        asyncio.run(_do_reset())

    _schedule_next_daily()


def _schedule_next_daily() -> None:
    global _daily_timer
    if not _running:
        return
    delay = _seconds_until_midnight_utc3()
    logger.info(
        "[daily-reset] Bir sonraki sıfırlama %.0f saniye sonra (UTC+3 00:00).", delay
    )
    _daily_timer = threading.Timer(delay, _run_daily_reset)
    _daily_timer.daemon = True
    _daily_timer.name = "daily-token-reset"
    _daily_timer.start()


# ─── Public API ───────────────────────────────────────────────────────────────

def start_scheduler(mongo_uri: str) -> None:
    """Startup'ta çağrılır. İlk yüklemeyi hemen arka planda başlatır."""
    global _running, _main_loop
    _running = True

    # FastAPI startup async context'inden çağrıldığı için
    # bu noktada ana event loop aktif — kaydet.
    try:
        _main_loop = asyncio.get_running_loop()
        logger.info("Ana event loop kaydedildi: %s", _main_loop)
    except RuntimeError:
        _main_loop = None
        logger.warning("start_scheduler async context dışından çağrıldı; loop kaydedilemedi.")

    def _first_run():
        logger.info("Platform kataloğu ilk kez yükleniyor…")
        _refresh()
        _schedule_next()

    t = threading.Thread(target=_first_run, daemon=True, name="platform-catalog-init")
    t.start()
    logger.info("Katalog zamanlayıcısı başlatıldı (aralık: %d dk).", _INTERVAL // 60)

    # Günlük sıfırlama zamanlayıcısını başlat
    _schedule_next_daily()

    # TMDB katalog zamanlayıcısını da başlat
    try:
        from Backend.helper.tmdb_catalog import start_tmdb_scheduler
        start_tmdb_scheduler()
    except Exception as e:
        logger.warning("TMDB zamanlayıcısı başlatılamadı: %s", e)


def stop_scheduler() -> None:
    global _running, _timer, _daily_timer
    _running = False
    if _timer:
        _timer.cancel()
        _timer = None
    if _daily_timer:
        _daily_timer.cancel()
        _daily_timer = None
    logger.info("Katalog zamanlayıcısı durduruldu.")

    # TMDB zamanlayıcısını da durdur
    try:
        from Backend.helper.tmdb_catalog import stop_tmdb_scheduler
        stop_tmdb_scheduler()
    except Exception as e:
        logger.warning("TMDB zamanlayıcısı durdurulamadı: %s", e)
