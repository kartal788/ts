"""
pinger.py
==========
İki görevi vardır:

1. Ping Loop  — her 20 dakikada bir BASE_URL'e istek atar (sağlık kontrolü).

2. Limit Monitor Loop — her 1 dakikada bir TÜM token'ların günlük kullanımını
   kontrol eder. Limiti olan her üye için kendi Telegram hesabına bildirim gider.

   Bildirim kuralları (hergün UTC+3 00:00'da reset_all_daily_usage ile sıfırlanır):
   ────────────────────────────────────────────────────────────────────────────────
   • Kullanım ≥ %80 VE < %100 → Üyeye "%80 Uyarısı" bir kez gönderilir.
     (DB'de daily_limit_warned = True olarak işaretlenir)
   • Kullanım ≥ %100           → Üyeye "Günlük limit bitti" bir kez gönderilir.
     (DB'de daily_limit_finished = True olarak işaretlenir)
   • UTC+3 00:00'da her iki bayrak da False'a döner — hergün tekrar çalışır.
   • Kullanım verisi stream_analytics'ten (güvenilir kaynak) hesaplanır —
     token.usage.daily.bytes yerine, çünkü o alan arka planda asenkron
     güncellenir ve yoğun/paralel kısa stream'lerde veri kaybına açıktır.

   Token'da user_id yoksa veya daily_limit_gb = 0 ise o token atlanır.
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta, timezone

import aiohttp

from Backend.config import Telegram
from Backend.logger import LOGGER

# ── Sabitler ────────────────────────────────────────────────────────────────
_PING_INTERVAL_SECONDS = 1200   # 20 dakika — sağlık pingleri
_LIMIT_CHECK_INTERVAL  = 60     # 1 dakika  — limit kontrol döngüsü

_TZ_UTC3 = timezone(timedelta(hours=3))


# ── Yardımcı ────────────────────────────────────────────────────────────────

def _format_bytes(b: int) -> str:
    """Byte'ı okunabilir birime çevirir."""
    if b < 1024:
        return f"{b} B"
    for unit in ("KB", "MB", "GB", "TB"):
        b /= 1024
        if b < 1024:
            return f"{b:.2f} {unit}"
    return f"{b:.2f} PB"


def _format_remaining(remaining_mb: float) -> str:
    """Kalan miktarı akıllıca formatlar: 1000 MB üzerindeyse GB gösterir."""
    if remaining_mb >= 1000:
        remaining_gb = remaining_mb / 1024
        return f"{remaining_gb:.2f} GB"
    return f"{remaining_mb:.1f} MB"


# ── Ana giriş noktası ────────────────────────────────────────────────────────

async def ping() -> None:
    """
    Sağlık kontrol pingleri (20 dk)  +  üye bazlı limit izleme (1 dk).
    __main__.py: loop.create_task(ping()) ile çağrılır.
    """
    await asyncio.gather(
        _ping_loop(),
        _limit_monitor_loop(),
    )


# ── Ping döngüsü ─────────────────────────────────────────────────────────────

async def _ping_loop() -> None:
    """BASE_URL'i 20 dakikada bir pinglar.

    DNS veya bağlantı hatalarında exponential backoff uygular:
    hata durumunda 1 dk → 2 dk → 4 dk → 8 dk → (max 16 dk) bekler,
    başarıda normal 20 dk aralığına döner.
    """
    manifest_url = f"{Telegram.BASE_URL}/api/system/stats"
    _BACKOFF_BASE    = 60    # ilk yeniden deneme: 1 dakika
    _BACKOFF_MAX     = 960   # en fazla 16 dakika
    retry_delay: int = 0     # ilk çalışmada backoff yok

    while True:
        wait = retry_delay if retry_delay else _PING_INTERVAL_SECONDS
        await asyncio.sleep(wait)

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(manifest_url) as resp:
                    LOGGER.info(f"Pinged manifest URL — Status: {resp.status}")
            retry_delay = 0  # başarı → normal aralığa dön
        except asyncio.TimeoutError:
            retry_delay = min((retry_delay or _BACKOFF_BASE) * 2, _BACKOFF_MAX)
            LOGGER.warning(
                "Timeout: manifest URL'e bağlanılamadı. "
                f"%d saniye sonra yeniden denenecek.", retry_delay
            )
        except aiohttp.ClientConnectorDNSError as exc:
            retry_delay = min((retry_delay or _BACKOFF_BASE) * 2, _BACKOFF_MAX)
            LOGGER.warning(
                "DNS çözümlenemedi (%s). "
                f"%d saniye sonra yeniden denenecek.", exc.host, retry_delay
            )
        except Exception:
            retry_delay = min((retry_delay or _BACKOFF_BASE) * 2, _BACKOFF_MAX)
            LOGGER.error(
                "Ping başarısız. %d saniye sonra yeniden denenecek.\n%s",
                retry_delay, traceback.format_exc(),
            )


# ── Limit izleme döngüsü ─────────────────────────────────────────────────────

async def _limit_monitor_loop() -> None:
    """
    Her 1 dakikada bir tüm api_token'ları tarar.
    Günlük limiti olan ve user_id'si kayıtlı her üye için:
      - %80 eşiği geçildiyse ve henüz bildirilmediyse → uyarı gönder, DB'yi işaretle.
      - %100 eşiği geçildiyse ve henüz bildirilmediyse → bitti mesajı gönder, DB'yi işaretle.
    Bayraklar UTC+3 00:00'daki reset_all_daily_usage çağrısıyla otomatik sıfırlanır.
    """
    LOGGER.info(f"[limit-monitor] Üye bazlı limit izleme başladı (her {_LIMIT_CHECK_INTERVAL} sn).")

    while True:
        await asyncio.sleep(_LIMIT_CHECK_INTERVAL)

        try:
            from Backend import db

            tokens = await db.get_all_api_tokens()
            # Tek bir toplu sorguyla tüm token'ların analytics tabanlı
            # kullanımını al — döngü başına 1 aggregation, N değil.
            analytics_usage = await db.get_analytics_usage_by_token()

            for token_doc in tokens:
                try:
                    await _check_token(db, token_doc, analytics_usage)
                except Exception:
                    LOGGER.error(
                        f"[limit-monitor] Token kontrolü hatası ({token_doc.get('token', '?')[:8]}):\n"
                        + traceback.format_exc()
                    )

        except Exception:
            LOGGER.error("[limit-monitor] Döngü hatası:\n" + traceback.format_exc())


async def _check_token(db, token_doc: dict, analytics_usage: dict) -> None:
    """Tek bir token için limit kontrolü yapar ve gerekirse bildirim gönderir."""

    # ── Ön filtreler ─────────────────────────────────────────────────────────
    user_id = token_doc.get("user_id")
    if not user_id:
        return  # Telegram kullanıcısıyla eşleşmemiş token — atla

    limits = token_doc.get("limits", {})
    daily_limit_gb: float = float(limits.get("daily_limit_gb") or 0)
    if daily_limit_gb <= 0:
        return  # Limitsiz token — atla

    # Süresi dolmuş token — atla (bildirim göndermek mantıksız)
    if token_doc.get("is_expired"):
        return

    # ── Günlük kullanımı oku ─────────────────────────────────────────────────
    # Not: token.usage.daily.bytes yerine stream_analytics'ten (güvenilir
    # kaynak) hesaplanan gerçek kullanım baz alınır — token.usage bucket'ları
    # arka planda asenkron güncellenir ve yoğun/paralel kısa stream'lerde
    # veri kaybına açıktır; bu da %80 uyarısının hiç gönderilmemesine yol açar.
    token_str = token_doc.get("token", "")
    daily_bytes: int = int(analytics_usage.get(token_str, {}).get("daily_bytes") or 0)
    limit_bytes: float = daily_limit_gb * 1024 ** 3

    ratio = daily_bytes / limit_bytes if limit_bytes > 0 else 0.0
    name      = token_doc.get("name") or f"User {user_id}"
    used_str  = _format_bytes(daily_bytes)
    limit_str = _format_bytes(int(limit_bytes))
    pct       = ratio * 100

    # ── %100 — Günlük limit bitti (sadece DB işareti, bildirim stream_routes'tan gider) ──
    if ratio >= 1.0:
        already_finished = token_doc.get("daily_limit_finished", False)
        if not already_finished:
            await db.mark_token_daily_limit_finished(token_str)
            LOGGER.warning(
                f"[limit-monitor] ❌ Limit bitti → user_id={user_id} ({name}): "
                f"{used_str} / {limit_str} (%{pct:.1f})"
            )
        return  # %100 geçilmişse %80 kontrolüne gerek yok

    # ── %80 — Uyarı ──────────────────────────────────────────────────────────
    if ratio >= 0.80:
        already_warned = token_doc.get("daily_limit_warned", False)
        if not already_warned:
            remaining_mb = round((limit_bytes - daily_bytes) / (1024 ** 2), 1)
            remaining_str = _format_remaining(remaining_mb)
            await db.mark_token_daily_limit_warned(token_str)
            await _send_user_message(
                user_id=user_id,
                text=(
                    f"⚠️ <b> Günlük limit uyarısı</b>\n\n"
                    f"🟡 Günlük kotanızın %{pct:.1f}'ini kullandınız.\n"
                    f"📊 Kalan: {remaining_str}\n\n"
                    f"Limitiniz dolduğunda yayın duraklatılacaktır."
                ),
            )
            LOGGER.warning(
                f"[limit-monitor] ⚠️ %80 uyarısı → user_id={user_id} ({name}): "
                f"{used_str} / {limit_str} (%{pct:.1f})"
            )


async def _send_user_message(user_id: int, text: str) -> None:
    """Belirtilen Telegram kullanıcısına mesaj gönderir."""
    try:
        from Backend.pyrofork.bot import StreamBot
        from pyrogram.errors import UserIsBlocked, InputUserDeactivated, PeerIdInvalid
        from pyrogram import enums

        await StreamBot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=enums.ParseMode.HTML,
        )
    except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid) as e:
        LOGGER.warning(f"[limit-monitor] Kullanıcı {user_id} ulaşılamaz: {e}")
    except Exception as e:
        LOGGER.error(f"[limit-monitor] Mesaj gönderilemedi (user_id={user_id}): {e}")
