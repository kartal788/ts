"""
daily_content_notifier.py
==========================

Saati değiştirmek için:
  Bu dosyanın başındaki NOTIFY_HOUR ve NOTIFY_MINUTE sabitlerini düzenleyin.
  Örneğin sabah 08:30'da göndermek için:
      NOTIFY_HOUR   = 8
      NOTIFY_MINUTE = 30

Entegrasyon (db_scheduler.py → start_scheduler fonksiyonuna ekle):
    from Backend.helper.daily_content_notifier import start_daily_content_notifier
    start_daily_content_notifier(main_loop=_main_loop)
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger("daily_content_notifier")

# ─── Bildirim saati ayarı ─────────────────────────────────────────────────────
# Saati değiştirmek için bu iki sabiti düzenleyin (UTC+3 / Türkiye saati).
# Örnek: sabah 08:30 → NOTIFY_HOUR = 8, NOTIFY_MINUTE = 30
NOTIFY_HOUR   = 0   # Saat (0-23)
NOTIFY_MINUTE = 5   # Dakika (0-59)
# ─────────────────────────────────────────────────────────────────────────────

# 25'ten fazla toplam içerik varsa mesaj yerine .txt dosyası gönderilir.
_TXT_THRESHOLD = 25

_content_notify_timer: threading.Timer | None = None
_running = False
_main_loop: asyncio.AbstractEventLoop | None = None

# Türkçe ay isimleri
_TR_MONTHS = [
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"
]


def _yesterday_label() -> str:
    """UTC+3 ile bir önceki günün Türkçe tarihini döner. Örn: '8 Mayıs 2026'"""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    tz        = ZoneInfo("Europe/Istanbul")
    yesterday = datetime.now(tz) - timedelta(days=1)
    return f"{yesterday.day} {_TR_MONTHS[yesterday.month]} {yesterday.year}"


def _get_platform_for(imdb_id) -> str:
    """
    Verilen imdb_id'nin ait olduğu platformu platform_catalog üzerinden döner.
    Bulunamazsa None.
    """
    if not imdb_id:
        return None
    try:
        from Backend.helper.platform_catalog import platform_catalog, PLATFORM_LABELS
        with platform_catalog._lock:
            for platform_key, id_set in platform_catalog._catalog.items():
                if imdb_id in id_set:
                    return PLATFORM_LABELS.get(platform_key, platform_key.capitalize())
    except Exception:
        pass
    return None


# ─── Zamanlama yardımcıları ───────────────────────────────────────────────────

def _seconds_until_notify_time() -> float:
    """UTC+3 bir sonraki NOTIFY_HOUR:NOTIFY_MINUTE'e kaç saniye kaldığını döner."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Istanbul")
    now = datetime.now(tz)

    # Bugün için hedef zamanı hesapla
    target = now.replace(hour=NOTIFY_HOUR, minute=NOTIFY_MINUTE, second=0, microsecond=0)

    # Eğer hedef zaman geçmişse yarına planla
    if target <= now:
        target += timedelta(days=1)

    return (target - now).total_seconds()


# ─── Son 24 saatte eklenen içerikleri getir ───────────────────────────────────

async def _get_new_content(db) -> dict:
    """
    Tüm storage_* veritabanlarını tarayarak son 24 saatte
    updated_on alanı güncellenen film ve dizi belgelerini döner.

    Dönüş:
        {
            "movies": [ {title, poster, rating, release_year, ...}, ... ],
            "tv":     [ {title, poster, rating, release_year, ...}, ... ],
        }
    """
    cutoff = datetime.utcnow() - timedelta(hours=24)
    query = {"updated_on": {"$gte": cutoff}}
    projection = {
        "title": 1,
        "title_tr": 1,
        "poster": 1,
        "rating": 1,
        "release_year": 1,
        "genres_tr": 1,
        "genres": 1,
        "updated_on": 1,
        "tmdb_id": 1,
        "imdb_id": 1,
        "media_type": 1,
    }

    movies: list[dict] = []
    tv_shows: list[dict] = []

    # Multi-db: storage_1, storage_2, ...
    for i in range(1, db.current_db_index + 1):
        db_key = f"storage_{i}"
        if db_key not in db.dbs:
            continue
        storage = db.dbs[db_key]

        # Film koleksiyonu
        try:
            movie_cursor = storage["movie"].find(query, projection).sort("updated_on", -1)
            async for doc in movie_cursor:
                doc.pop("_id", None)
                movies.append(doc)
        except Exception as e:
            logger.warning("[content-notify] storage_%d movie sorgusu hatası: %s", i, e)

        # Dizi koleksiyonu
        try:
            tv_cursor = storage["tv"].find(query, {
                "title": 1, "title_tr": 1, "poster": 1, "rating": 1,
                "release_year": 1, "genres_tr": 1, "genres": 1,
                "updated_on": 1, "tmdb_id": 1, "imdb_id": 1, "media_type": 1,
            }).sort("updated_on", -1)
            async for doc in tv_cursor:
                doc.pop("_id", None)
                tv_shows.append(doc)
        except Exception as e:
            logger.warning("[content-notify] storage_%d tv sorgusu hatası: %s", i, e)

    # Aynı içerik birden fazla DB'de olabilir — imdb_id/tmdb_id bazlı deduplikasyon
    movies   = _dedup(movies)
    tv_shows = _dedup(tv_shows)

    return {"movies": movies, "tv": tv_shows}


def _dedup(items: list[dict]) -> list[dict]:
    """imdb_id veya tmdb_id bazında tekrarlananları kaldırır."""
    seen: set = set()
    result: list[dict] = []
    for item in items:
        key = item.get("imdb_id") or item.get("tmdb_id") or item.get("title", "")
        if key and key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


# ─── Dizi bölüm/sezon özeti ───────────────────────────────────────────────────

def _format_tv_episodes(seasons: list[dict]) -> str:
    """
    Bir dizinin sezon/bölüm listesini okunabilir metne çevirir.

    Kural:
      - Bir sezonda 4'ten fazla bölüm varsa → "X. Sezon eklendi"
      - 4 veya daha az bölümse      → "S01E01, S01E02, ..." şeklinde listeler
    """
    if not seasons:
        return ""

    parts: list[str] = []

    for season in sorted(seasons, key=lambda s: s.get("season_number", 0)):
        season_num = season.get("season_number", 0)
        episodes   = season.get("episodes", [])

        if not episodes:
            continue

        if len(episodes) > 4:
            # Çok bölüm → sezon olarak özetle
            parts.append(f"{season_num}. Sezon eklendi")
        else:
            # Az bölüm → tek tek listele
            ep_tags = [
                f"S{season_num:02d}E{ep.get('episode_number', 0):02d}"
                for ep in sorted(episodes, key=lambda e: e.get("episode_number", 0))
            ]
            parts.append(", ".join(ep_tags))

    return " | ".join(parts) if parts else ""


# ─── Mesaj formatı ────────────────────────────────────────────────────────────

# 25'ten fazla toplam içerik varsa mesaj yerine .txt dosyası gönderilir.
_TXT_THRESHOLD = 25


def _sort_alphabetically(items: list[dict]) -> list[dict]:
    """İçerikleri başlığa göre alfabetik olarak sıralar (Türkçe karakterlere duyarlı)."""
    import locale
    try:
        locale.setlocale(locale.LC_COLLATE, "tr_TR.UTF-8")
        return sorted(items, key=lambda x: locale.strxfrm(
            (x.get("title_tr") or x.get("title") or "").lower()
        ))
    except locale.Error:
        return sorted(items, key=lambda x: (
            (x.get("title_tr") or x.get("title") or "").lower()
        ))


def _build_content_lines(movies: list[dict], tv_shows: list[dict], date_label: str) -> list[str]:
    """Film ve dizi listesini düz metin satırlarına çevirir (HTML tag'siz, alfabetik sıralı)."""
    lines: list[str] = [f"{date_label} Eklenenler", ""]

    if movies:
        sorted_movies = _sort_alphabetically(movies)
        lines.append(f"🎥 Filmler ({len(sorted_movies)})")
        for m in sorted_movies:
            title     = m.get("title_tr") or m.get("title", "—")
            year      = m.get("release_year", "")
            rating    = m.get("rating")
            genres    = m.get("genres_tr") or m.get("genres") or []
            genre_str = ", ".join(genres[:2]) if genres else ""

            entry = f"• {title}"
            if year:
                entry += f" ({year})"
            if rating:
                entry += f" ⭐ {rating:.1f}"
            if genre_str:
                entry += f" — {genre_str}"
            lines.append(entry)
        lines.append("")

    if tv_shows:
        sorted_tv = _sort_alphabetically(tv_shows)
        lines.append(f"📺 Diziler ({len(sorted_tv)})")
        for t in sorted_tv:
            title     = t.get("title_tr") or t.get("title", "—")
            year      = t.get("release_year", "")
            rating    = t.get("rating")
            genres    = t.get("genres_tr") or t.get("genres") or []
            genre_str = ", ".join(genres[:2]) if genres else ""
            platform  = _get_platform_for(t.get("imdb_id"))
            entry = f"• {title}"
            if year:
                entry += f" ({year})"
            if rating:
                entry += f" ⭐ {rating:.1f}"
            if platform:
                entry += f" [{platform}]"
            if genre_str:
                entry += f" — {genre_str}"
            lines.append(entry)
        lines.append("")

    lines.append("🍿 İyi seyirler")
    return lines


def _format_notification_html(movies: list[dict], tv_shows: list[dict], service_name: str, date_label: str) -> str:
    """
    25 veya daha az toplam içerik için Telegram HTML mesajı oluşturur.
    Toplam içerik yoksa boş string döner.
    """
    if not movies and not tv_shows:
        return ""

    lines: list[str] = []
    lines.append(f"<b>{date_label} Eklenenler</b>\n")

    if movies:
        lines.append(f"🎥 <b>Filmler</b> ({len(movies)})")
        for m in _sort_alphabetically(movies):
            title     = m.get("title_tr") or m.get("title", "—")
            year      = m.get("release_year", "")
            rating    = m.get("rating")
            genres    = m.get("genres_tr") or m.get("genres") or []
            genre_str = ", ".join(genres[:2]) if genres else ""

            entry = f"• <b>{title}</b>"
            if year:
                entry += f" ({year})"
            if rating:
                entry += f" ⭐ {rating:.1f}"
            if genre_str:
                entry += f" — <i>{genre_str}</i>"
            lines.append(entry)
        lines.append("")

    if tv_shows:
        lines.append(f"📺 <b>Diziler</b> ({len(tv_shows)})")
        for t in _sort_alphabetically(tv_shows):
            title     = t.get("title_tr") or t.get("title", "—")
            year      = t.get("release_year", "")
            rating    = t.get("rating")
            genres    = t.get("genres_tr") or t.get("genres") or []
            genre_str = ", ".join(genres[:2]) if genres else ""
            platform  = _get_platform_for(t.get("imdb_id"))
            entry = f"• <b>{title}</b>"
            if year:
                entry += f" ({year})"
            if rating:
                entry += f" ⭐ {rating:.1f}"
            if platform:
                entry += f" [<i>{platform}</i>]"
            if genre_str:
                entry += f" — <i>{genre_str}</i>"
            lines.append(entry)
        lines.append("")

    lines.append("🍿 İyi seyirler")
    return "\n".join(lines)


def _build_txt_bytes(movies: list[dict], tv_shows: list[dict], service_name: str, date_label: str) -> bytes:
    """15'ten fazla içerik için .txt dosyası içeriğini oluşturur."""
    header = []
    content_lines = _build_content_lines(movies, tv_shows, date_label)
    full_text = "\n".join(header + content_lines)
    return full_text.encode("utf-8")


# ─── Gönderim çekirdeği ───────────────────────────────────────────────────────

async def _send_daily_content_notifications() -> None:
    """
    Son 24 saatte eklenen içerikleri tüm kullanıcılara gönderir.
    15'ten fazla içerik varsa mesaj yerine .txt dosyası olarak iletir.
    """
    try:
        from Backend import db
        from Backend.pyrofork.bot import StreamBot
        from Backend.config import Telegram
        from pyrogram.enums import ParseMode
        from pyrogram.errors import (
            FloodWait,
            UserIsBlocked,
            InputUserDeactivated,
            PeerIdInvalid,
        )

        logger.info("[content-notify] Bildirim görevi başladı.")

        # ── 1. Yeni içerikleri getir ──────────────────────────────────────
        content  = await _get_new_content(db)
        movies   = content["movies"]
        tv_shows = content["tv"]

        total_content = len(movies) + len(tv_shows)
        logger.info(
            "[content-notify] Son 24 saatte: %d film, %d dizi bulundu.",
            len(movies), len(tv_shows),
        )

        if total_content == 0:
            logger.info("[content-notify] Yeni içerik yok, bildirim gönderilmeyecek.")
            if owner_id := Telegram.OWNER_ID:
                try:
                    await StreamBot.send_message(
                        chat_id=owner_id,
                        text="ℹ️ Bugün sisteme yeni içerik eklenmedi.",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.warning("[content-notify] Owner boş-içerik bildirimi gönderilemedi: %s", e)
            return

        # ── 2. Tarih etiketi ve mesaj mı, txt mi? ────────────────────────
        date_label  = _yesterday_label()
        use_txt     = total_content > _TXT_THRESHOLD
        if use_txt:
            txt_bytes   = _build_txt_bytes(movies, tv_shows, Telegram.ISIM, date_label)
            txt_caption = (
                f"<b>{date_label} Eklenenler</b>\n"
                f"<i>{len(movies)} film, {len(tv_shows)} dizi eklendi.</i>\n"
                f"📄 Tam liste ekte."
            )
            logger.info("[content-notify] 25+ içerik — .txt dosyası olarak gönderilecek.")
        else:
            message_text = _format_notification_html(movies, tv_shows, Telegram.ISIM, date_label)
            if not message_text:
                logger.info("[content-notify] Mesaj oluşturulamadı, atlanıyor.")
                return

        # ── 3. Tüm kullanıcıları getir ────────────────────────────────────
        all_users   = await db.get_all_users()
        total_users = len(all_users)
        logger.info("[content-notify] %d kullanıcıya bildirim gönderilecek.", total_users)

        sent       = 0
        blocked    = 0
        failed     = 0
        batch_sent = 0          # batch hız kontrolü için
        start_time = datetime.utcnow()

        sent_users    = []   # {"id": ..., "name": ..., "username": ...}
        blocked_users = []
        failed_users  = []

        for user in all_users:
            uid = user.get("_id") or user.get("user_id")
            if not uid:
                failed += 1
                failed_users.append({"id": "?", "name": "?", "username": None})
                continue

            uid_int  = int(uid)
            name     = " ".join(filter(None, [
                user.get("first_name", ""),
                user.get("last_name", ""),
            ])).strip() or "—"
            username = user.get("username") or None

            async def _send(uid_int=uid_int):
                if use_txt:
                    await StreamBot.send_document(
                        chat_id=uid_int,
                        document=io.BytesIO(txt_bytes),
                        file_name="gunluk_icerik.txt",
                        caption=txt_caption,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await StreamBot.send_message(
                        chat_id=uid_int,
                        text=message_text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )

            _user_info = {"id": uid_int, "name": name, "username": username}

            try:
                await _send()
                sent += 1
                sent_users.append(_user_info)

            except FloodWait as e:
                wait = max(e.value, 1)
                logger.warning("[content-notify] FloodWait: %d sn bekleniyor.", wait)
                await asyncio.sleep(wait)
                try:
                    await _send()
                    sent += 1
                    sent_users.append(_user_info)
                except Exception as retry_err:
                    logger.warning(
                        "[content-notify] Kullanıcı %d retry hatası: %s", uid_int, retry_err
                    )
                    failed += 1
                    failed_users.append(_user_info)

            except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
                blocked += 1
                blocked_users.append(_user_info)

            except OSError as e:
                logger.warning(
                    "[content-notify] Kullanıcı %d ağ/timeout hatası: %s — 5 sn sonra tekrar deneniyor.", uid_int, e
                )
                await asyncio.sleep(5)
                try:
                    await _send()
                    sent += 1
                    sent_users.append(_user_info)
                except Exception as retry_err:
                    logger.warning(
                        "[content-notify] Kullanıcı %d ağ retry hatası: %s", uid_int, retry_err
                    )
                    failed += 1
                    failed_users.append(_user_info)

            except Exception as e:
                logger.warning(
                    "[content-notify] Kullanıcı %d gönderilemedi: %s", uid_int, e
                )
                failed += 1
                failed_users.append(_user_info)

            # Telegram flood koruması — her mesaj sonrası kısa bekleme
            await asyncio.sleep(0.05)

            # Batch hız kontrolü — her 25 başarılı gönderimde 2 sn dinlen
            batch_sent += 1
            if batch_sent % 25 == 0:
                logger.debug("[content-notify] 25 mesaj gönderildi, 2 sn bekleniyor.")
                await asyncio.sleep(2)

        # ── 4. Owner'a özet rapor + kullanıcı detay txt gönder ───────────
        elapsed_sec = int((datetime.utcnow() - start_time).total_seconds())
        elapsed_str = f"{elapsed_sec // 60} dk {elapsed_sec % 60} sn"

        owner_id = Telegram.OWNER_ID
        if owner_id:
            summary = (
                f"📊 <b>Günlük İçerik Bildirimi Raporu</b>\n\n"
                f"🎥 Yeni film: <b>{len(movies)}</b>\n"
                f"📺 Yeni dizi: <b>{len(tv_shows)}</b>\n\n"
                f"👥 Toplam kullanıcı: <b>{total_users}</b>\n"
                f"✅ Gönderildi: <b>{sent}</b>\n"
                f"🚫 Engelledi/Çıktı: <b>{blocked}</b>\n"
                f"❌ Başarısız: <b>{failed}</b>\n"
                f"⏱ Süre: <b>{elapsed_str}</b>"
            )

            def _user_line(u: dict) -> str:
                line = f"  ID: {u['id']} | Ad: {u['name']}"
                if u.get("username"):
                    line += f" | @{u['username']}"
                return line

            report_lines = [
                f"📊 Günlük İçerik Bildirimi Raporu",
                f"Tarih: {date_label}",
                f"",
                f"🎥 Yeni film: {len(movies)}",
                f"📺 Yeni dizi: {len(tv_shows)}",
                f"",
                f"👥 Toplam kullanıcı: {total_users}",
                f"✅ Gönderildi: {sent}",
                f"🚫 Engelledi/Çıktı: {blocked}",
                f"❌ Başarısız: {failed}",
                f"⏱ Süre: {elapsed_str}",
                f"",
                f"─" * 40,
            ]

            if sent_users:
                report_lines.append(f"\n✅ Gönderilen Kullanıcılar ({sent}):")
                for u in sent_users:
                    report_lines.append(_user_line(u))

            if blocked_users:
                report_lines.append(f"\n🚫 Engelleyen / Çıkan Kullanıcılar ({blocked}):")
                for u in blocked_users:
                    report_lines.append(_user_line(u))

            if failed_users:
                report_lines.append(f"\n❌ Başarısız Kullanıcılar ({failed}):")
                for u in failed_users:
                    report_lines.append(_user_line(u))

            report_txt = "\n".join(report_lines).encode("utf-8")

            try:
                await StreamBot.send_document(
                    chat_id=owner_id,
                    document=io.BytesIO(report_txt),
                    file_name=f"rapor_{datetime.utcnow().strftime('%Y%m%d')}.txt",
                    caption=summary,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning("[content-notify] Owner raporu gönderilemedi: %s", e)
                # Dosya gönderilemezse sadece mesaj olarak dene
                try:
                    await StreamBot.send_message(
                        chat_id=owner_id,
                        text=summary,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e2:
                    logger.warning("[content-notify] Owner mesajı da gönderilemedi: %s", e2)

        logger.info(
            "[content-notify] Tamamlandı. Gönderildi: %d, Engelledi: %d, Başarısız: %d, Süre: %s",
            sent, blocked, failed, elapsed_str,
        )

    except Exception as e:
        logger.exception("[content-notify] Genel hata: %s", e)


# ─── threading.Timer callback + zamanlayıcı ──────────────────────────────────

def _run_content_notify() -> None:
    """threading.Timer callback — async fonksiyonu ana loop'ta çalıştırır."""
    if not _running:
        return

    loop = _main_loop
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            _send_daily_content_notifications(), loop
        )
        try:
            future.result(timeout=600)  # maksimum 10 dakika bekle
        except Exception as e:
            logger.exception("[content-notify] run_coroutine_threadsafe hatası: %s", e)
    else:
        logger.warning("[content-notify] Ana loop bulunamadı, asyncio.run() ile çalışıyor.")
        asyncio.run(_send_daily_content_notifications())

    # Bir sonraki günü planla
    _schedule_next_content_notify()


def _schedule_next_content_notify() -> None:
    """UTC+3 NOTIFY_HOUR:NOTIFY_MINUTE'de tetiklenecek zamanlayıcıyı kurar."""
    global _content_notify_timer
    if not _running:
        return

    delay = _seconds_until_notify_time()
    logger.info(
        "[content-notify] Bir sonraki bildirim %.0f saniye sonra (UTC+3 %02d:%02d).",
        delay, NOTIFY_HOUR, NOTIFY_MINUTE,
    )
    _content_notify_timer = threading.Timer(delay, _run_content_notify)
    _content_notify_timer.daemon = True
    _content_notify_timer.name = "daily-content-notify"
    _content_notify_timer.start()


# ─── Public API ───────────────────────────────────────────────────────────────

def start_daily_content_notifier(main_loop: asyncio.AbstractEventLoop | None = None) -> None:
    """
    db_scheduler.start_scheduler() içinden çağrılır.

    Kullanım (db_scheduler.py → start_scheduler fonksiyonu):
        from Backend.helper.daily_content_notifier import start_daily_content_notifier
        start_daily_content_notifier(main_loop=_main_loop)
    """
    global _running, _main_loop
    _running   = True
    _main_loop = main_loop

    _schedule_next_content_notify()
    logger.info(
        "[content-notify] Günlük içerik bildirimi zamanlayıcısı başlatıldı (UTC+3 %02d:%02d).",
        NOTIFY_HOUR, NOTIFY_MINUTE,
    )


def stop_daily_content_notifier() -> None:
    """
    db_scheduler.stop_scheduler() içinden çağrılır.

    Kullanım (db_scheduler.py → stop_scheduler fonksiyonu):
        from Backend.helper.daily_content_notifier import stop_daily_content_notifier
        stop_daily_content_notifier()
    """
    global _running, _content_notify_timer
    _running = False
    if _content_notify_timer:
        _content_notify_timer.cancel()
        _content_notify_timer = None
    logger.info("[content-notify] Günlük içerik bildirimi zamanlayıcısı durduruldu.")
