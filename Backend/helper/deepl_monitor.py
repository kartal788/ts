"""
deepl_monitor.py
================
DeepL çeviri API'sinin kotası/süresi azaldığında TÜM yöneticilere (OWNER_ID +
APPROVER_IDS) uyarı bildirimi gönderen arka plan görevi.

Uyarı koşulları (biri yeterli, her biri ayrı ayrı bildirilir):
  1. Karakter kotasından geriye ``DEEPL_CHAR_THRESHOLD`` (5.000) veya daha az
     karakter kalmışsa  → DeepL /v2/usage (Backend.helper.metadata.get_deepl_usage)
  2. Abonelik bitiş/yenileme tarihine ``DEEPL_DAYS_THRESHOLD`` (2) veya daha az
     gün kalmışsa      → Ayarlar > deepl_renewal_date (DeepL bu tarihi API'den vermez)

Her uyarı yalnızca BİR KEZ gönderilir (durum DB'de tutulur, bot yeniden başlasa
da tekrar etmez):
  - Karakter uyarısı: kalan karakter tekrar eşiğin üzerine çıkınca (kota
    yenilenince / paket yükseltilince) sıfırlanır, böylece sonraki dönemde yine uyarır.
  - Süre uyarısı: yenileme tarihi başına bir kez; tarih değiştirilince
    (abonelik yenilenince) yeni tarih için tekrar uyarı gönderilir.

Bildirim kanalları: Telegram (bot mesajı) + tarayıcı Web Push (varsa).
"""

import asyncio
import html as _html
from datetime import datetime, timedelta, timezone

from pyrogram import enums

from Backend import db
from Backend.config import Telegram
from Backend.logger import LOGGER

DEEPL_CHAR_THRESHOLD = 5000      # kalan karakter bu değer veya altına inince uyar
DEEPL_DAYS_THRESHOLD = 2         # kalan gün bu değer veya altına inince uyar
CHECK_INTERVAL_SECONDS = 30 * 60  # 30 dakikada bir kontrol

_TZ_TR = timezone(timedelta(hours=3))  # UTC+3 Türkiye saati


def _admin_ids() -> list:
    """OWNER_ID + panelden eklenen tüm yöneticiler (APPROVER_IDS), tekrarsız."""
    ids = []
    for uid in [Telegram.OWNER_ID] + list(Telegram.APPROVER_IDS or []):
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        if uid and uid not in ids:
            ids.append(uid)
    return ids


async def _notify_all_admins(bot, text: str, push_title: str, push_body: str) -> None:
    """Uyarıyı tüm yöneticilere Telegram'dan, ayrıca Web Push ile gönderir."""
    for admin_id in _admin_ids():
        try:
            await bot.send_message(
                admin_id,
                text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            LOGGER.warning(f"[deepl_monitor] Yöneticiye uyarı gönderilemedi ({admin_id}): {e}")

    try:
        from Backend.helper.webpush import notify_admins
        await notify_admins(push_title, push_body, url="/admin/settings", tag="deepl")
    except Exception as e:
        LOGGER.warning(f"[deepl_monitor] Web push gönderilemedi: {e}")


def _fmt_int(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")


async def check_deepl_once(bot) -> None:
    """Tek seferlik kontrol: eşik aşıldıysa ve daha önce bildirilmediyse uyarı gönderir."""
    api_key = (getattr(Telegram, "DEEPL_API", "") or "").strip()
    if not api_key:
        return  # DeepL kullanılmıyor

    state = await db.get_deepl_alert_state()
    new_state = dict(state)

    # ---------- 1) Karakter kotası ----------
    from Backend.helper.metadata import get_deepl_usage
    usage = await get_deepl_usage()
    if usage and not usage.get("error"):
        used = usage.get("character_count")
        limit = usage.get("character_limit")
        if used is not None and limit:
            remaining = max(int(limit) - int(used), 0)
            if remaining <= DEEPL_CHAR_THRESHOLD:
                if not state.get("chars_alerted"):
                    text = (
                        "⚠️ <b>DeepL Karakter Limiti Azaldı</b>\n\n"
                        f"<b>📉 Kalan karakter:</b> {_fmt_int(remaining)}\n"
                        f"<b>📊 Kullanılan:</b> {_fmt_int(used)} / {_fmt_int(limit)}\n\n"
                        "Kota biterse çeviri zinciri DeepL'i atlayıp Google/MyMemory'ye düşer. "
                        "Paketi yükseltmeyi veya yeni dönemi beklemeyi değerlendirin."
                    )
                    await _notify_all_admins(
                        bot, text,
                        "DeepL karakter limiti azaldı",
                        f"Kalan karakter: {_fmt_int(remaining)} ({_fmt_int(used)}/{_fmt_int(limit)})",
                    )
                    new_state["chars_alerted"] = True
                    LOGGER.info(f"[deepl_monitor] Karakter uyarısı gönderildi (kalan: {remaining}).")
            elif state.get("chars_alerted"):
                # Kota yenilendi / yükseltildi → bir sonraki düşüşte tekrar uyarabilmek için sıfırla
                new_state["chars_alerted"] = False

    # ---------- 2) Abonelik süresi ----------
    renewal_str = ""
    try:
        from Backend.helper.settings_manager import SettingsManager
        renewal_str = str(getattr(SettingsManager.current(), "deepl_renewal_date", "") or "").strip()
    except Exception:
        renewal_str = ""

    if renewal_str:
        try:
            renewal = datetime.strptime(renewal_str, "%Y-%m-%d").date()
            today = datetime.now(_TZ_TR).date()
            days_left = (renewal - today).days
        except ValueError:
            days_left = None

        if days_left is not None and 0 <= days_left <= DEEPL_DAYS_THRESHOLD:
            if state.get("days_alerted_for") != renewal_str:
                date_tr = renewal.strftime("%d.%m.%Y")
                if days_left == 0:
                    left_txt = "bugün bitiyor"
                else:
                    left_txt = f"{days_left} gün kaldı"
                text = (
                    "⚠️ <b>DeepL Abonelik Süresi Dolmak Üzere</b>\n\n"
                    f"<b>⏳ Durum:</b> {_html.escape(left_txt)}\n"
                    f"<b>📅 Bitiş/Yenileme tarihi:</b> {date_tr}\n\n"
                    "Aboneliği yenileyip Ayarlar'dan tarihi güncellemeyi unutmayın."
                )
                await _notify_all_admins(
                    bot, text,
                    "DeepL abonelik süresi dolmak üzere",
                    f"Bitiş/yenileme tarihi {date_tr} — {left_txt}.",
                )
                new_state["days_alerted_for"] = renewal_str
                LOGGER.info(f"[deepl_monitor] Süre uyarısı gönderildi ({renewal_str}, {days_left} gün).")

    if new_state != state:
        await db.set_deepl_alert_state(new_state)


async def deepl_monitor_loop(bot) -> None:
    """Periyodik DeepL kota/süre kontrolü (bkz. check_deepl_once)."""
    await asyncio.sleep(60)  # açılışta servisler otursun
    while True:
        try:
            await check_deepl_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOGGER.error(f"[deepl_monitor] Kontrol hatası: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
