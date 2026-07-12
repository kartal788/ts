"""
db_scheduler.py
================
Uygulama açılınca platform kataloğunu hemen yükler.
Periyodik yenileme YOKTUR — katalog yalnızca yeni içerik
eklendiğinde platform_catalog.schedule_refresh() ile tetiklenir.

Ayrıca UTC+3 (Europe/Istanbul) gece 00:00'da tüm API token
günlük kullanımlarını otomatik sıfırlar — video izlenmese bile.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import TimeoutError as _FutureTimeoutError
from datetime import datetime, timedelta

logger = logging.getLogger("db_scheduler")

_SIMILAR_INTERVAL = 30 * 60  # "Sana Özel" cache yenileme aralığı: 30 dakika
_ANALYTICS_RETENTION_DAYS = 10   # stream_analytics maksimum saklama süresi
_ANALYTICS_MAX_RECORDS    = 20   # stream_analytics maksimum kayıt sayısı
_daily_timer: threading.Timer | None = None
_similar_timer: threading.Timer | None = None
_analytics_cleanup_timer: threading.Timer | None = None
_expiry_notify_timer: threading.Timer | None = None
_tv_status_timer_ref: threading.Timer | None = None  # tv_status_updater iç referansı
_running = False

# FastAPI startup sırasında kaydedilen ana event loop.
# threading.Timer callback'lerinden run_coroutine_threadsafe ile kullanılır.
_main_loop: asyncio.AbstractEventLoop | None = None


# ─── Platform kataloğu ────────────────────────────────────────────────────────
# Periyodik yenileme kaldırıldı.
# Katalog yalnızca şu iki durumda güncellenir:
#   1. İlk başlangıç  : _first_run() içinde platform_catalog.refresh() çağrılır.
#   2. Yeni içerik    : İçerik ekleyen kod platform_catalog.schedule_refresh() çağırır.


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
            # In-memory bildirim set'lerini de temizle
            try:
                from Backend.fastapi.routes.stream_routes import _daily_warn_sent, _daily_finished_sent
                _daily_warn_sent.clear()
                _daily_finished_sent.clear()
                logger.info("[daily-reset] Bildirim set'leri temizlendi.")
            except Exception as e:
                logger.warning("[daily-reset] Bildirim set'leri temizlenemedi: %s", e)
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


# ─── "Sana Özel" Cache Yenileme ──────────────────────────────────────────────

async def _get_recently_active_tokens(db, window_hours: int = 24) -> set:
    """
    Son `window_hours` saat içinde stream_analytics tablosunda kaydı olan
    token'ları döner. Bu token'lar gerçekten aktif izleme yapmış demektir.
    """
    try:
        cutoff = datetime.utcnow() - timedelta(hours=window_hours)
        col = db.dbs["tracking"]["stream_analytics"]
        # distinct sorgu: tüm dokümanları çekmek yerine sadece token listesi
        tokens = await col.distinct("user_token", {"logged_at": {"$gte": cutoff}})
        return set(t for t in tokens if t)
    except Exception as e:
        logger.warning("[similar-cache] Son aktif token'lar alınamadı, tüm liste kullanılacak: %s", e)
        return set()


async def _refresh_similar_cache_async() -> None:
    """
    Son 24 saatte izleme yapan kullanıcıların "Sana Özel" cache'ini yeniler.
    Uyuyan/pasif üyeler atlanır — gereksiz DB sorgusu ve RAM kullanımı önlenir.
    """
    try:
        from Backend import db as _db
        from Backend.fastapi.routes.stremio_routes import (
            _similar_cache_set,
            _SIMILAR_CACHE,
        )

        all_tokens = await _db.get_all_api_tokens()
        all_valid = {
            t["token"] for t in all_tokens
            if not t.get("is_expired") and t.get("token")
        }

        if not all_valid:
            logger.info("[similar-cache] Geçerli kullanıcı yok, atlanıyor.")
            return

        # Son 24 saatte aktif olanlarla kesişim
        recently_active = await _get_recently_active_tokens(_db, window_hours=24)
        tokens_to_refresh = all_valid & recently_active

        skipped = len(all_valid) - len(tokens_to_refresh)
        if skipped:
            logger.info(
                "[similar-cache] %d pasif kullanıcı atlandı (son 24 saatte izleme yok).",
                skipped,
            )

        if not tokens_to_refresh:
            logger.info("[similar-cache] Son 24 saatte aktif kullanıcı yok, atlanıyor.")
            return

        logger.info("[similar-cache] %d kullanıcı için cache yenileniyor…", len(tokens_to_refresh))

        #----- Önceden tamamen sıralı (sequential) çalışıyordu: her kullanıcı için
        #----- 3 dil x DB sorgusu birbiri ardına yapılıyordu. Kullanıcı sayısı
        #----- arttıkça bu, 120 sn'lik zaman aşımını kolayca aşıyordu. Şimdi
        #----- sınırlı eşzamanlılıkla (aynı anda en fazla 5 kullanıcı) paralel çalışır.
        refresh_semaphore = asyncio.Semaphore(5)
        refreshed_counter = {"n": 0}

        async def _refresh_one(token: str) -> None:
            async with refresh_semaphore:
                try:
                    # get_watch_history_rich dil bağımsız — döngü dışına alındı
                    history_rich = await _db.get_watch_history_rich(token, limit=40)
                    if not history_rich:
                        return
                    watched_ids = [r["imdb_id"] for r in history_rich]
                    last_watched_id = watched_ids[0] if watched_ids else None
                    for lang in ("tr", "en", "de"):
                        items = await _db.get_similar_items(
                            watched_imdb_ids=watched_ids,
                            page=1,
                            page_size=60,
                            lang=lang,
                            last_watched_id=last_watched_id,
                            watch_history_rich=history_rich,
                        )
                        if items:
                            _similar_cache_set(token, lang, items)
                    refreshed_counter["n"] += 1
                except Exception as e:
                    logger.warning("[similar-cache] Token %s yenilenemedi: %s", token[:8], e)

        await asyncio.gather(*(_refresh_one(token) for token in tokens_to_refresh))
        refreshed = refreshed_counter["n"]

        logger.info("[similar-cache] %d/%d kullanıcı cache'i yenilendi.", refreshed, len(tokens_to_refresh))
    except Exception as e:
        logger.exception("[similar-cache] Cache yenileme hatası: %s", e)


_similar_refresh_in_progress = False


def _on_similar_refresh_done(future: "asyncio.Future") -> None:
    """future.result(timeout=...) süresi dolduğunda bile arka planda çalışmaya
    devam eden görev sonunda burada işaretlenir; böylece bir sonraki
    zamanlanan tur, hâlâ süren bir yenilemenin üzerine binmez."""
    global _similar_refresh_in_progress
    _similar_refresh_in_progress = False
    try:
        exc = future.exception()
        if exc:
            logger.warning("[similar-cache] Arka planda geç tamamlanan yenileme hata ile bitti: %s", exc)
    except Exception:
        pass


def _run_similar_refresh() -> None:
    """threading.Timer callback — async fonksiyonu ana loop'ta çalıştırır."""
    global _similar_refresh_in_progress
    if not _running:
        return

    if _similar_refresh_in_progress:
        logger.warning("[similar-cache] Önceki yenileme hâlâ sürüyor, bu tur atlanıyor.")
        _schedule_next_similar()
        return

    loop = _main_loop
    if loop is not None and loop.is_running():
        _similar_refresh_in_progress = True
        future = asyncio.run_coroutine_threadsafe(_refresh_similar_cache_async(), loop)
        future.add_done_callback(_on_similar_refresh_done)
        try:
            future.result(timeout=180)
        except _FutureTimeoutError:
            # Görev iptal edilmedi — arka planda (ana loop'ta) çalışmaya devam
            # ediyor ve bitince _on_similar_refresh_done bayrağı temizleyecek.
            # Bu artık bir hata değil, sadece "beklenenden uzun sürdü" bilgisidir.
            logger.warning(
                "[similar-cache] Yenileme 180 sn içinde bitmedi, arka planda çalışmaya devam ediyor "
                "(bir sonraki tur, bu bitene kadar atlanacak)."
            )
        except Exception as e:
            logger.exception("[similar-cache] run_coroutine_threadsafe hatası: %s", e)
    else:
        logger.warning("[similar-cache] Ana loop bulunamadı, asyncio.run() ile çalışıyor.")
        asyncio.run(_refresh_similar_cache_async())
    _schedule_next_similar()


def _schedule_next_similar() -> None:
    global _similar_timer
    if not _running:
        return
    _similar_timer = threading.Timer(_SIMILAR_INTERVAL, _run_similar_refresh)
    _similar_timer.daemon = True
    _similar_timer.name = "similar-cache-refresh"
    _similar_timer.start()


# ─── Analytics Temizleme ─────────────────────────────────────────────────────

async def _cleanup_analytics_async() -> None:
    """
    stream_analytics koleksiyonunu temizler:
    - 10 günden eski tüm kayıtları siler
    - Her kullanıcı için en fazla 20 kayıt bırakır (en yeniler kalır)
    """
    try:
        from Backend import db as _db
        col = _db.dbs["tracking"]["stream_analytics"]

        # 1) 10 günden eski kayıtları sil
        cutoff = datetime.utcnow() - timedelta(days=_ANALYTICS_RETENTION_DAYS)
        old_result = await col.delete_many({"logged_at": {"$lt": cutoff}})
        if old_result.deleted_count:
            logger.info(
                "[analytics-cleanup] %d eski kayıt silindi (>%d gün).",
                old_result.deleted_count, _ANALYTICS_RETENTION_DAYS,
            )

        # 2) Her kullanıcı için en fazla 20 kayıt bırak
        # Kullanıcı başına 20'den fazla kayıt varsa, en eskilerini sil
        pipeline = [
            {"$group": {"_id": "$user_token", "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": _ANALYTICS_MAX_RECORDS}}},
        ]
        over_limit = await col.aggregate(pipeline).to_list(None)
        total_trimmed = 0
        for row in over_limit:
            token = row["_id"]
            # En yeni 20 kaydın _id listesini al
            keep_cursor = col.find(
                {"user_token": token},
                {"_id": 1}
            ).sort("logged_at", -1).limit(_ANALYTICS_MAX_RECORDS)
            keep_ids = [doc["_id"] async for doc in keep_cursor]
            # Bunlar dışındakileri sil
            trim_result = await col.delete_many({
                "user_token": token,
                "_id": {"$nin": keep_ids},
            })
            total_trimmed += trim_result.deleted_count

        if total_trimmed:
            logger.info(
                "[analytics-cleanup] %d fazla kayıt kırpıldı (kullanıcı başına max %d).",
                total_trimmed, _ANALYTICS_MAX_RECORDS,
            )

    except Exception as e:
        logger.exception("[analytics-cleanup] Temizleme hatası: %s", e)


def _run_analytics_cleanup() -> None:
    """threading.Timer callback — analytics temizliğini ana loop'ta çalıştırır."""
    if not _running:
        return
    loop = _main_loop
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_cleanup_analytics_async(), loop)
        try:
            future.result(timeout=60)
        except Exception as e:
            logger.exception("[analytics-cleanup] run_coroutine_threadsafe hatası: %s", e)
    else:
        asyncio.run(_cleanup_analytics_async())
    _schedule_next_analytics_cleanup()


def _schedule_next_analytics_cleanup() -> None:
    global _analytics_cleanup_timer
    if not _running:
        return
    # Her gün bir kez çalıştır
    _analytics_cleanup_timer = threading.Timer(24 * 3600, _run_analytics_cleanup)
    _analytics_cleanup_timer.daemon = True
    _analytics_cleanup_timer.name = "analytics-cleanup"
    _analytics_cleanup_timer.start()


# ─── Abonelik sona erme bildirimi ────────────────────────────────────────────

async def _send_expiry_notifications_async() -> None:
    """
    Aboneliği bugün sona eren ve henüz bildirilmemiş kullanıcılara
    Telegram üzerinden mesaj gönderir.
    """
    try:
        from Backend import db as _db
        from Backend.pyrofork.bot import StreamBot
        from Backend.config import Telegram
        from pyrogram.errors import UserIsBlocked, InputUserDeactivated, PeerIdInvalid

        users = await _db.get_expired_today_unnotified()
        if not users:
            logger.info("[expiry-notify] Bugün bildirilecek süresi dolan kullanıcı yok.")
            return

        logger.info("[expiry-notify] %d kullanıcıya sona erme bildirimi gönderiliyor…", len(users))
        sent = 0
        failed = 0

        for user in users:
            user_id = user.get("_id")
            if not user_id:
                continue
            try:
                await StreamBot.send_message(
                    chat_id=user_id,
                    text=(
                        f"⏰ <b>Aboneliğiniz sona erdi.</b>\n\n"
                        f"<b>{Telegram.ISIM}</b> hizmetine erişiminiz bugün itibarıyla sona ermiştir.\n\n"
                        f"Yeniden abone olmak için /start yazabilirsiniz. 🎬"
                    ),
                    parse_mode="html",
                )
                await _db.mark_expiry_notified(user_id)
                sent += 1
            except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid) as e:
                logger.warning("[expiry-notify] Kullanıcı %s ulaşılamaz: %s", user_id, e)
                await _db.mark_expiry_notified(user_id)  # tekrar deneme
                failed += 1
            except Exception as e:
                logger.warning("[expiry-notify] Kullanıcı %s gönderilemedi: %s", user_id, e)
                failed += 1

        logger.info("[expiry-notify] Gönderildi: %d, Başarısız: %d", sent, failed)

    except Exception as e:
        logger.exception("[expiry-notify] Bildirim hatası: %s", e)


def _run_expiry_notifications() -> None:
    """threading.Timer callback — ana loop'ta çalıştırır."""
    if not _running:
        return
    loop = _main_loop
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_send_expiry_notifications_async(), loop)
        try:
            future.result(timeout=60)
        except Exception as e:
            logger.exception("[expiry-notify] run_coroutine_threadsafe hatası: %s", e)
    else:
        logger.warning("[expiry-notify] Ana loop bulunamadı, asyncio.run() ile çalışıyor.")
        asyncio.run(_send_expiry_notifications_async())
    _schedule_next_expiry_notify()


def _schedule_next_expiry_notify() -> None:
    global _expiry_notify_timer
    if not _running:
        return
    # Günlük sıfırlamadan 5 dakika sonra çalışsın (00:05 UTC+3)
    delay = _seconds_until_midnight_utc3() + 300
    logger.info("[expiry-notify] Bir sonraki bildirim %.0f saniye sonra (UTC+3 00:05).", delay)
    _expiry_notify_timer = threading.Timer(delay, _run_expiry_notifications)
    _expiry_notify_timer.daemon = True
    _expiry_notify_timer.name = "expiry-notify"
    _expiry_notify_timer.start()


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
        try:
            from Backend.helper.platform_catalog import platform_catalog
            platform_catalog.refresh()
        except Exception as e:
            logger.exception("Katalog ilk yükleme hatası: %s", e)

    t = threading.Thread(target=_first_run, daemon=True, name="platform-catalog-init")
    t.start()
    logger.info("Katalog ilk yükleme başlatıldı (sonraki yenilemeler içerik eklenince tetiklenir).")

    # "Sana Özel" cache — uygulama açılınca hemen yükle, sonra 30 dk'da bir yenile
    def _similar_first_run():
        logger.info("[similar-cache] İlk yükleme başlıyor…")
        _run_similar_refresh()

    ts = threading.Thread(target=_similar_first_run, daemon=True, name="similar-cache-init")
    ts.start()
    logger.info("[similar-cache] Zamanlayıcı başlatıldı (aralık: 30 dk).")

    # Günlük sıfırlama zamanlayıcısını başlat
    _schedule_next_daily()

    # Abonelik sona erme bildirimleri — her gün UTC+3 00:05'te
    _schedule_next_expiry_notify()
    logger.info("[expiry-notify] Zamanlayıcı başlatıldı (günlük, UTC+3 00:05).")

    # Analytics temizleme — uygulama açılınca hemen çalıştır, sonra günlük tekrarla
    def _analytics_first_run():
        logger.info("[analytics-cleanup] İlk temizleme başlıyor…")
        _run_analytics_cleanup()

    ta = threading.Thread(target=_analytics_first_run, daemon=True, name="analytics-cleanup-init")
    ta.start()
    logger.info("[analytics-cleanup] Zamanlayıcı başlatıldı (günlük, max %d kayıt, max %d gün).",
                _ANALYTICS_MAX_RECORDS, _ANALYTICS_RETENTION_DAYS)

    # TMDB katalog zamanlayıcısını da başlat
    try:
        from Backend.helper.tmdb_catalog import start_tmdb_scheduler
        start_tmdb_scheduler()
    except Exception as e:
        logger.warning("TMDB zamanlayıcısı başlatılamadı: %s", e)

    # TV dizi status güncelleme zamanlayıcısını başlat (her gün UTC+3 05:00)
    try:
        from Backend.helper.tv_status_updater import start_tv_status_scheduler
        start_tv_status_scheduler(main_loop=_main_loop)
    except Exception as e:
        logger.warning("TV status zamanlayıcısı başlatılamadı: %s", e)

    # Günlük yeni içerik bildirimi — her gün UTC+3 00:01'de
    try:
        from Backend.helper.daily_content_notifier import start_daily_content_notifier
        start_daily_content_notifier(main_loop=_main_loop)
    except Exception as e:
        logger.warning("Günlük içerik bildirimi zamanlayıcısı başlatılamadı: %s", e)


def stop_scheduler() -> None:
    global _running, _daily_timer, _similar_timer, _analytics_cleanup_timer, _expiry_notify_timer
    _running = False
    # Bekleyen debounce zamanlayıcısını iptal et
    try:
        from Backend.helper.platform_catalog import platform_catalog
        platform_catalog.cancel_scheduled_refresh()
    except Exception:
        pass
    if _daily_timer:
        _daily_timer.cancel()
        _daily_timer = None
    if _similar_timer:
        _similar_timer.cancel()
        _similar_timer = None
    if _analytics_cleanup_timer:
        _analytics_cleanup_timer.cancel()
        _analytics_cleanup_timer = None
    if _expiry_notify_timer:
        _expiry_notify_timer.cancel()
        _expiry_notify_timer = None
    logger.info("Katalog zamanlayıcısı durduruldu.")

    # TMDB zamanlayıcısını da durdur
    try:
        from Backend.helper.tmdb_catalog import stop_tmdb_scheduler
        stop_tmdb_scheduler()
    except Exception as e:
        logger.warning("TMDB zamanlayıcısı durdurulamadı: %s", e)

    # TV status zamanlayıcısını durdur
    try:
        from Backend.helper.tv_status_updater import stop_tv_status_scheduler
        stop_tv_status_scheduler()
    except Exception as e:
        logger.warning("TV status zamanlayıcısı durdurulamadı: %s", e)

    # Günlük içerik bildirimi zamanlayıcısını durdur
    try:
        from Backend.helper.daily_content_notifier import stop_daily_content_notifier
        stop_daily_content_notifier()
    except Exception as e:
        logger.warning("Günlük içerik bildirimi zamanlayıcısı durdurulamadı: %s", e)
