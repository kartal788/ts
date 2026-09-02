"""
notification_routes.py
======================
Dizi ve film hatırlatma / bildirim sistemi route'ları.

Üyeler member_catalog.html'deki "Hatırlat" butonuna basarak
bir dizi veya filme abone olabilir. Yeni içerik eklendiğinde
Telegram botu üzerinden bildirim gönderilir.

MongoDB koleksiyonları:
  tracking.tv_reminders    → dizi hatırlatmaları
  tracking.movie_reminders → film hatırlatmaları

Döküman yapısı (her iki koleksiyon için aynı):
  {
    "_id": ObjectId,
    "tmdb_id": int,         ← TEK eşleşme anahtarı (db_index artık kriter değil)
    "db_index": int,        ← metadata, bildirim tetiklenince güncellenir
    "title": str,
    "poster": str,
    "user_ids": [int, ...]  ← abone olan Telegram user_id'leri
  }

Bildirim sistemi — 2 dakika beklet + birleştir:
  İçerik eklendiğinde bildirim HEMEN gönderilmez. Bunun yerine
  "pending buffer"a yazılır ve 2 dakika beklenir. Bu süre içinde
  aynı dizi/filme ait yeni bölümler/kaliteler de gelirse hepsi
  tek bir Telegram mesajında birleştirilir.

API endpoint'leri:
  POST   /api/uye/hatirla              → dizi/filme abone ol / iptal et (toggle)
  GET    /api/uye/hatirlatmalarim      → oturumdaki üyenin tüm hatırlatmaları
  GET    /api/uye/hatirla/durum        → belirli bir içeriğe abone mi?
  POST   /api/uye/film-hatirla         → filme abone ol / iptal et (toggle)
  GET    /api/uye/film-hatirlatmalarim → üyenin film hatırlatmaları
  GET    /api/uye/film-hatirla/durum   → belirli bir filme abone mi?

Dahili yardımcılar (diğer route'lardan çağrılır):
  schedule_tv_reminder(tmdb_id, db_index, title, poster, new_season, new_episode)
  schedule_movie_reminder(tmdb_id, db_index, title, poster, quality_label)
  send_tv_reminder_notifications(...)    ← eski imza korundu (geriye dönük uyumluluk)
  send_movie_reminder_notifications(...) ← eski imza korundu (geriye dönük uyumluluk)
"""

from __future__ import annotations

import asyncio
import logging
import html as _html
import re
from typing import Optional
from urllib.parse import urlparse

from pyrogram import enums
from pyrogram.types import InlineKeyboardMarkup

from fastapi import Request, Query, HTTPException
from fastapi.responses import JSONResponse

from Backend import db
from Backend.helper.database import is_media_visible_to_member
from Backend.config import Telegram
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.content_announcer import _build_open_buttons
from Backend.helper.webpush import notify_admins as _notify_admins_push

_logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# BEKLETİCİ / BİRLEŞTİRİCİ MOTOR
# ─────────────────────────────────────────────────────────────────────────────
#
# Her (media_type, tmdb_id) çifti için:
#   _pending_tv[tmdb_id]    = { "info": {...}, "episodes": [...], "timer_task": Task }
#   _pending_movie[tmdb_id] = { "info": {...}, "qualities": [...], "timer_task": Task }
#
# Yeni içerik gelince:
#   - Timer task varsa iptal et (süreyi sıfırla)
#   - Yeni içeriği listeye ekle
#   - 1 dakika sonra tetiklenecek yeni timer task oluştur

NOTIFY_DELAY_SECONDS = 60   # 1 dakika

_pending_tv: dict[int, dict]    = {}
_pending_movie: dict[int, dict] = {}


# ── MongoDB koleksiyonu yardımcısı ───────────────────────────────────────────

# ── Güvenlik: İzin verilen poster domain'leri ────────────────────────────────

_ALLOWED_POSTER_HOSTS = {
    "image.tmdb.org",
    "www.themoviedb.org",
    "t.me",
    "images.metahub.space",
}


def _validate_poster_url(poster: str) -> str:
    """
    Poster URL'sini doğrular; yalnızca izin verilen domain'lerden
    gelen HTTPS URL'lerine izin verir. Geçersiz URL'ler boş string
    olarak geri döner (bildirim poster'siz gönderilir).

    İzin verilen domain'ler: image.tmdb.org, www.themoviedb.org, t.me
    """
    if not poster:
        return ""
    try:
        parsed = urlparse(poster)
    except Exception:
        return ""
    if parsed.scheme != "https":
        _logger.warning("Poster URL reddedildi (scheme=%s): %s", parsed.scheme, poster[:200])
        return ""
    host = (parsed.netloc or "").lower().split(":")[0]   # port varsa at
    if host not in _ALLOWED_POSTER_HOSTS:
        _logger.warning("Poster URL reddedildi (host=%s): %s", host, poster[:200])
        return ""
    return poster


# ── Bildirimde daha kaliteli poster: yalnızca kullanıcıya giden görsel ──────
#
# Veritabanındaki "poster" alanı DEĞİŞTİRİLMEZ (küçük boyut olarak kalır);
# yalnızca Telegram'a gönderilirken URL'deki boyut segmenti "original" ile
# değiştirilir. Böylece kullanıcıya daha kaliteli/büyük bir resim gider.
#
#   https://images.metahub.space/poster/small/tt10986410/img
#     -> https://images.metahub.space/poster/original/tt10986410/img
#   https://image.tmdb.org/t/p/w500/xxxx.jpg
#     -> https://image.tmdb.org/t/p/original/xxxx.jpg

_METAHUB_SIZE_RE = re.compile(r"(images\.metahub\.space/poster/)[^/]+(/)")
_TMDB_SIZE_RE = re.compile(r"(image\.tmdb\.org/t/p/)w\d+(/)")


def _upgrade_poster_quality(poster: str) -> str:
    """Bildirimde gönderilecek poster URL'sini en yüksek kaliteye yükseltir.

    Sadece bilinen boyut segmentlerini ("small", "w500", "w300" vb.)
    "original" ile değiştirir; eşleşme yoksa poster olduğu gibi döner.
    """
    if not poster:
        return poster
    upgraded = _METAHUB_SIZE_RE.sub(r"\1original\2", poster)
    upgraded = _TMDB_SIZE_RE.sub(r"\1original\2", upgraded)
    return upgraded


def _reminders_col():
    return db.dbs["tracking"]["tv_reminders"]


def _movie_reminders_col():
    return db.dbs["tracking"]["movie_reminders"]


# ── Oturum yardımcısı ────────────────────────────────────────────────────────

def _get_member(request: Request) -> Optional[dict]:
    return request.session.get("member")


def _require_member(request: Request) -> dict:
    member = _get_member(request)
    if not member:
        raise HTTPException(status_code=401, detail="Oturum açılmamış")
    return member


# ─────────────────────────────────────────────────────────────────────────────
# MESAJ GÖNDERİCİ (gerçek Telegram gönderimi)
# ─────────────────────────────────────────────────────────────────────────────

async def _dispatch_tv(tmdb_id: int):
    """
    2 dakika bekleme sona erdi — biriken dizi bölümlerini tek mesajda gönder.
    """
    pending = _pending_tv.pop(tmdb_id, None)
    if not pending:
        return

    info     = pending["info"]
    episodes = pending["episodes"]   # list of (season, episode) tuples

    db_index    = info["db_index"]
    title       = info["title"]
    poster      = info["poster"]

    col = _reminders_col()

    doc = await col.find_one({"tmdb_id": tmdb_id})
    if not doc:
        _logger.info("TV hatırlatma: tmdb_id=%s için kayıt bulunamadı.", tmdb_id)
        return

    user_ids: list[int] = doc.get("user_ids") or []
    if not user_ids:
        _logger.info("TV hatırlatma: tmdb_id=%s kaydı var ama user_ids boş.", tmdb_id)
        return

    # İçerik seviyesinde görünürlük (media_edit.html → "Sadece seçtiğim üye(ler)") —
    # hatırlatma kaydedildikten sonra içerik kısıtlanmış olabilir; bildirim
    # sadece görme/erişim izni olan üyelere gönderilir.
    media_doc = await db.get_document("tv", tmdb_id, db_index)
    user_ids = [uid for uid in user_ids if is_media_visible_to_member(media_doc, uid)]
    if not user_ids:
        _logger.info("TV hatırlatma: tmdb_id=%s görünürlük kısıtlaması nedeniyle hiçbir üyeye gönderilmiyor.", tmdb_id)
        return

    _logger.info(
        "TV hatırlatma gönderiliyor: tmdb_id=%s '%s' — %d bölüm, %d abone",
        tmdb_id, title, len(episodes), len(user_ids),
    )

    # db_index güncelle
    await col.update_one(
        {"tmdb_id": tmdb_id},
        {"$set": {"db_index": db_index, "title": title, "poster": poster}},
    )

    safe_title = _html.escape(title or "Bilinmeyen Dizi")

    _base = (Telegram.BASE_URL or "").rstrip("/")
    _sub  = (Telegram.SUBSCRIPTION_URL or "").rstrip("/")
    catalog_url = f"{_base}/uye/hatirlatmalar" if _base else (_sub if _sub else "")

    # Bölüm listesini oluştur
    if episodes:
        # Tekrar eden girişleri temizle, sırala
        unique_eps = sorted(set(episodes))
        if len(unique_eps) == 1:
            s, e = unique_eps[0]
            if s and e:
                episode_lines = f"📺 <b>{s}. sezon {e}. bölüm</b> eklendi."
            elif s:
                episode_lines = f"🆕 <b>{s}. sezon</b> eklendi."
            elif e:
                episode_lines = f"▶️ <b>{e}. bölüm</b> eklendi."
            else:
                episode_lines = "🔔 Yeni içerik eklendi."
        else:
            if len(unique_eps) > 4:
                # 4'ten fazla bölüm varsa sezon bazında grupla
                seasons_seen = []
                for s, e in unique_eps:
                    if s and s not in seasons_seen:
                        seasons_seen.append(s)
                if seasons_seen:
                    lines = [f"🔹 {s}.Sezon" for s in sorted(seasons_seen)]
                    episode_lines = "📺 Eklenen Sezonlar:\n" + "\n".join(lines)
                else:
                    lines = []
                    for s, e in unique_eps:
                        if s and e:
                            lines.append(f"🔹 {s}.Sezon {e}.Bölüm")
                        elif s:
                            lines.append(f"🔹 {s}.Sezon")
                        elif e:
                            lines.append(f"🔹 {e}.Bölüm")
                    episode_lines = "📺 Eklenen Bölümler:\n" + "\n".join(lines)
            else:
                lines = []
                for s, e in unique_eps:
                    if s and e:
                        lines.append(f"🔹 {s}.Sezon {e}.Bölüm")
                    elif s:
                        lines.append(f"🔹 {s}.Sezon")
                    elif e:
                        lines.append(f"🔹 {e}.Bölüm")
                episode_lines = "📺 Eklenen Bölümler:\n" + "\n".join(lines)
    else:
        episode_lines = "🔔 Yeni içerik eklendi."

    text = (
        f"🎬 <b>{safe_title}</b>\n\n"
        f"{episode_lines}\n\n"
        + (f'<a href="{catalog_url}">🔔 Bildirimleri kapat.</a>' if catalog_url else "")
    )

    #----- content_announcer.py'deki genel Telegram kanal duyurularıyla aynı
    #----- mantıkla, ayarlardaki "Yönlendirme Alan Adı" (redirect_base_url) ve
    #----- içeriğin imdb_id'si mevcutsa "Stremio'da Aç" / "Nuvio'da Aç"
    #----- butonları da üye hatırlatma bildirimine eklenir.
    settings = SettingsManager.current()
    open_buttons = _build_open_buttons(
        {"media_type": "tv", "imdb_id": (media_doc or {}).get("imdb_id")},
        settings,
    )
    markup = InlineKeyboardMarkup([open_buttons]) if open_buttons else None

    await _send_to_users(user_ids, poster, text, f"TV tmdb_id={tmdb_id}", reply_markup=markup)


async def _dispatch_movie(tmdb_id: int):
    """
    2 dakika bekleme sona erdi — biriken film kalitelerini tek mesajda gönder.
    """
    pending = _pending_movie.pop(tmdb_id, None)
    if not pending:
        return

    info      = pending["info"]
    qualities = pending["qualities"]   # list of quality label strings

    db_index = info["db_index"]
    title    = info["title"]
    poster   = info["poster"]

    col = _movie_reminders_col()

    doc = await col.find_one({"tmdb_id": tmdb_id})
    if not doc:
        _logger.info("Film hatırlatma: tmdb_id=%s için kayıt bulunamadı.", tmdb_id)
        return

    user_ids: list[int] = doc.get("user_ids") or []
    if not user_ids:
        _logger.info("Film hatırlatma: tmdb_id=%s kaydı var ama user_ids boş.", tmdb_id)
        return

    # İçerik seviyesinde görünürlük (media_edit.html → "Sadece seçtiğim üye(ler)") —
    # hatırlatma kaydedildikten sonra içerik kısıtlanmış olabilir; bildirim
    # sadece görme/erişim izni olan üyelere gönderilir.
    media_doc = await db.get_document("movie", tmdb_id, db_index)
    user_ids = [uid for uid in user_ids if is_media_visible_to_member(media_doc, uid)]
    if not user_ids:
        _logger.info("Film hatırlatma: tmdb_id=%s görünürlük kısıtlaması nedeniyle hiçbir üyeye gönderilmiyor.", tmdb_id)
        return

    _logger.info(
        "Film hatırlatma gönderiliyor: tmdb_id=%s '%s' — %d kalite, %d abone",
        tmdb_id, title, len(qualities), len(user_ids),
    )

    # db_index güncelle
    await col.update_one(
        {"tmdb_id": tmdb_id},
        {"$set": {"db_index": db_index, "title": title, "poster": poster}},
    )

    safe_title = _html.escape(title or "Bilinmeyen Film")

    _base = (Telegram.BASE_URL or "").rstrip("/")
    _sub  = (Telegram.SUBSCRIPTION_URL or "").rstrip("/")
    catalog_url = f"{_base}/uye/hatirlatmalar" if _base else (_sub if _sub else "")

    # Kalite listesini oluştur
    unique_q = list(dict.fromkeys(q for q in qualities if q))  # sıra koruyarak dedupe

    def _is_camrip(q: str) -> bool:
        return q.strip().lower() in ("camrip", "cam")

    def _is_german_camrip(q: str) -> bool:
        return q.strip().lower() == "germancamrip"

    def _is_german(q: str) -> bool:
        return q.strip().lower().startswith("german:")

    def _german_base_quality(q: str) -> str:
        """'German:1080p' -> '1080p' döndürür."""
        return q.split(":", 1)[1] if ":" in q else ""

    def _format_quality(q: str) -> str:
        """Özel etiketler için açıklama, diğerleri için kalite adı döndürür."""
        if _is_german_camrip(q):
            return "🇩🇪 Almanca sinema çekimi olarak eklendi."
        if _is_german(q):
            base = _german_base_quality(q)
            if base:
                return f"🇩🇪 Almanca <b>{_html.escape(base)}</b> kalitesinde eklendi."
            return "🇩🇪🎞️ Almanca eklendi."
        if _is_camrip(q):
            return "🎟️ Sinema çekimi olarak eklendi."
        return f"🎞️ <b>{_html.escape(q)}</b> kalitesinde eklendi."

    if unique_q:
        # Özel etiketleri ve normal kaliteri ayır
        german_camrip_entries = [q for q in unique_q if _is_german_camrip(q)]
        german_entries        = [q for q in unique_q if _is_german(q)]
        camrip_entries        = [q for q in unique_q if _is_camrip(q)]
        normal_entries        = [q for q in unique_q if not _is_camrip(q)
                                  and not _is_german(q) and not _is_german_camrip(q)]

        parts = []
        if german_camrip_entries:
            parts.append("🇩🇪 <b>Almanca sinema çekimi</b> eklendi.")
        if german_entries:
            bases = [_german_base_quality(q) for q in german_entries]
            bases = [b for b in bases if b]
            if bases:
                escaped = [_html.escape(b) for b in bases]
                if len(escaped) == 1:
                    parts.append(f"🇩🇪 Almanca <b>{escaped[0]}</b> kalitesinde eklendi.")
                else:
                    joined = " ve ".join(f"<b>{b}</b>" for b in escaped)
                    parts.append(f"🇩🇪 Almanca {joined} kalitesinde eklendi.")
            else:
                parts.append("🇩🇪 <b>Almanca</b> eklendi.")
        if camrip_entries:
            parts.append("🎟️ <b>Sinema çekimi</b> olarak eklendi.")
        if normal_entries:
            escaped = [_html.escape(q) for q in normal_entries]
            if len(escaped) == 1:
                parts.append(f"🎞️ <b>{escaped[0]}</b> kalitesinde eklendi.")
            elif len(escaped) == 2:
                parts.append(f"🎞️ <b>{escaped[0]}</b> ve <b>{escaped[1]}</b> kalitesinde eklendi.")
            else:
                joined = ", ".join(f"<b>{q}</b>" for q in escaped[:-1])
                parts.append(f"🎞️ {joined} ve <b>{escaped[-1]}</b> kalitesinde eklendi.")
        quality_lines = "\n".join(parts)
    else:
        quality_lines = "🎬 Kataloğa eklendi."

    text = (
        f"🎬 <b>{safe_title}</b>\n\n"
        f"{quality_lines}\n\n"
        + (f'<a href="{catalog_url}">🔔 Bildirimleri kapat.</a>' if catalog_url else "")
    )

    #----- content_announcer.py'deki genel Telegram kanal duyurularıyla aynı
    #----- mantıkla, ayarlardaki "Yönlendirme Alan Adı" (redirect_base_url) ve
    #----- içeriğin imdb_id'si mevcutsa "Stremio'da Aç" / "Nuvio'da Aç"
    #----- butonları da üye hatırlatma bildirimine eklenir.
    settings = SettingsManager.current()
    open_buttons = _build_open_buttons(
        {"media_type": "movie", "imdb_id": (media_doc or {}).get("imdb_id")},
        settings,
    )
    markup = InlineKeyboardMarkup([open_buttons]) if open_buttons else None

    await _send_to_users(user_ids, poster, text, f"Film tmdb_id={tmdb_id}", reply_markup=markup)


_TELEGRAM_CAPTION_LIMIT = 1024  # Telegram send_photo caption max karakter sayısı
_TELEGRAM_MESSAGE_LIMIT = 4096  # Telegram send_message max karakter sayısı


def _truncate(text: str, limit: int) -> str:
    """Metni Telegram limitini aşmayacak şekilde kırpar."""
    if len(text) <= limit:
        return text
    suffix = "…"
    return text[: limit - len(suffix)] + suffix


async def _send_to_users(
    user_ids: list[int],
    poster: str,
    text: str,
    log_label: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """Kullanıcılara Telegram mesajı/fotoğrafı gönderir.

    reply_markup verilmişse (ör. "Stremio'da Aç" / "Nuvio'da Aç" butonları),
    metin caption sığmayıp iki parçaya bölündüğünde butonlar HER ZAMAN asıl
    metnin gönderildiği mesaja eklenir (fotoğraf caption'sız gittiğinde bile
    kullanıcı butonlara ulaşabilsin diye).
    """
    try:
        from Backend.pyrofork.bot import StreamBot
    except Exception:
        _logger.warning("StreamBot import edilemedi, bildirimler gönderilemedi.")
        return

    # Veritabanındaki poster küçük boyutta kalır; kullanıcıya giden bildirimde
    # yalnızca daha kaliteli (original) versiyonu kullanılır.
    poster = _upgrade_poster_quality(poster)

    sent = 0
    for user_id in user_ids:
        try:
            if poster:
                # send_photo caption limiti 1024 karakter.
                # Metin sığmıyorsa: önce fotoğrafı caption'sız gönder,
                # ardından tam metni ayrı mesaj olarak ilet.
                if len(text) > _TELEGRAM_CAPTION_LIMIT:
                    await StreamBot.send_photo(
                        chat_id=user_id,
                        photo=poster,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    await StreamBot.send_message(
                        chat_id=user_id,
                        text=_truncate(text, _TELEGRAM_MESSAGE_LIMIT),
                        parse_mode=enums.ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=reply_markup,
                    )
                else:
                    await StreamBot.send_photo(
                        chat_id=user_id,
                        photo=poster,
                        caption=text,
                        parse_mode=enums.ParseMode.HTML,
                        reply_markup=reply_markup,
                    )
            else:
                await StreamBot.send_message(
                    chat_id=user_id,
                    text=_truncate(text, _TELEGRAM_MESSAGE_LIMIT),
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
                )
            sent += 1
        except Exception as e:
            _logger.warning("Bildirim gönderilemedi user_id=%s: %s", user_id, e)
        await asyncio.sleep(1)

    _logger.info("%s bildirimi → %d/%d kullanıcıya gönderildi", log_label, sent, len(user_ids))


# ─────────────────────────────────────────────────────────────────────────────
# ZAMANLAYICI BAŞLATICI — dışarıdan çağrılan fonksiyonlar
# ─────────────────────────────────────────────────────────────────────────────

async def schedule_tv_reminder(
    tmdb_id:     int,
    db_index:    int,
    title:       str,
    poster:      str,
    new_season:  Optional[int] = None,
    new_episode: Optional[int] = None,
):
    """
    Dizi bölümünü pending buffer'a ekler ve 2 dakika timer başlatır (ya da sıfırlar).
    Aynı dizi için birden fazla çağrı gelirse timer sıfırlanır, hepsi tek mesajda çıkar.
    """
    ep_key = (new_season, new_episode)

    if tmdb_id in _pending_tv:
        # Mevcut timer'ı iptal et, süreyi sıfırla
        old = _pending_tv[tmdb_id]
        old["timer_task"].cancel()
        old["episodes"].append(ep_key)
        # Bilgileri güncelle (en son gelen db_index/poster geçerli)
        old["info"].update({"db_index": db_index, "title": title, "poster": poster})
        _logger.info(
            "TV hatırlatma tamponu güncellendi: tmdb_id=%s, biriken bölüm=%d, timer sıfırlandı.",
            tmdb_id, len(old["episodes"]),
        )
    else:
        _pending_tv[tmdb_id] = {
            "info": {"db_index": db_index, "title": title, "poster": poster},
            "episodes": [ep_key],
            "timer_task": None,
        }
        _logger.info(
            "TV hatırlatma tampona alındı: tmdb_id=%s s=%s e=%s — %ds sonra gönderilecek.",
            tmdb_id, new_season, new_episode, NOTIFY_DELAY_SECONDS,
        )

    # Yeni timer başlat
    task = asyncio.ensure_future(_delayed_tv(tmdb_id))
    _pending_tv[tmdb_id]["timer_task"] = task


async def schedule_movie_reminder(
    tmdb_id:       int,
    db_index:      int,
    title:         str,
    poster:        str,
    quality_label: str = "",
):
    """
    Film kalitesini pending buffer'a ekler ve 2 dakika timer başlatır (ya da sıfırlar).
    """
    if tmdb_id in _pending_movie:
        old = _pending_movie[tmdb_id]
        old["timer_task"].cancel()
        old["qualities"].append(quality_label)
        old["info"].update({"db_index": db_index, "title": title, "poster": poster})
        _logger.info(
            "Film hatırlatma tamponu güncellendi: tmdb_id=%s, biriken kalite=%d, timer sıfırlandı.",
            tmdb_id, len(old["qualities"]),
        )
    else:
        _pending_movie[tmdb_id] = {
            "info": {"db_index": db_index, "title": title, "poster": poster},
            "qualities": [quality_label],
            "timer_task": None,
        }
        _logger.info(
            "Film hatırlatma tampona alındı: tmdb_id=%s kalite=%r — %ds sonra gönderilecek.",
            tmdb_id, quality_label, NOTIFY_DELAY_SECONDS,
        )

    task = asyncio.ensure_future(_delayed_movie(tmdb_id))
    _pending_movie[tmdb_id]["timer_task"] = task


async def _delayed_tv(tmdb_id: int):
    """NOTIFY_DELAY_SECONDS bekle, sonra gönder."""
    try:
        await asyncio.sleep(NOTIFY_DELAY_SECONDS)
        await _dispatch_tv(tmdb_id)
    except asyncio.CancelledError:
        pass  # Timer sıfırlandı, yeni task devralacak


async def _delayed_movie(tmdb_id: int):
    """NOTIFY_DELAY_SECONDS bekle, sonra gönder."""
    try:
        await asyncio.sleep(NOTIFY_DELAY_SECONDS)
        await _dispatch_movie(tmdb_id)
    except asyncio.CancelledError:
        pass  # Timer sıfırlandı, yeni task devralacak


# ─────────────────────────────────────────────────────────────────────────────
# GERİYE DÖNÜK UYUMLULUK SARMALAYICILARI
# reciever.py ve link_ekle_routes.py bu isimleri çağırıyor — değiştirme gerek yok.
# ─────────────────────────────────────────────────────────────────────────────

async def send_tv_reminder_notifications(
    tmdb_id:     int,
    db_index:    int,
    title:       str,
    poster:      str,
    new_season:  Optional[int] = None,
    new_episode: Optional[int] = None,
) -> int:
    """Geriye dönük uyumluluk: schedule_tv_reminder'a yönlendirir."""
    await schedule_tv_reminder(
        tmdb_id=tmdb_id,
        db_index=db_index,
        title=title,
        poster=poster,
        new_season=new_season,
        new_episode=new_episode,
    )
    return 1  # Anlık gönderim yok, tampon sistemi devreye girdi


async def send_movie_reminder_notifications(
    tmdb_id:  int,
    db_index: int,
    title:    str,
    poster:   str,
    quality_label: str = "",
) -> int:
    """Geriye dönük uyumluluk: schedule_movie_reminder'a yönlendirir."""
    await schedule_movie_reminder(
        tmdb_id=tmdb_id,
        db_index=db_index,
        title=title,
        poster=poster,
        quality_label=quality_label,
    )
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# TOGGLE / DURUM / LİSTE ENDPOİNTLERİ
# ─────────────────────────────────────────────────────────────────────────────

# ── POST /api/uye/hatirla ────────────────────────────────────────────────────

async def toggle_reminder(request: Request):
    """
    Gelen JSON:
      { "tmdb_id": int, "db_index": int, "title": str, "poster": str }

    Döner:
      { "subscribed": true/false, "message": "..." }

    Eşleşme kriteri: sadece tmdb_id (db_index değişkendir, kriter değil).
    """
    member = _require_member(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Geçersiz JSON")

    tmdb_id  = body.get("tmdb_id")
    db_index = body.get("db_index", 0)
    title    = body.get("title", "")
    poster   = _validate_poster_url(body.get("poster", ""))
    status   = body.get("status", "")

    if tmdb_id is None:
        raise HTTPException(status_code=400, detail="tmdb_id zorunlu")

    try:
        user_id = int(member["user_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    col = _reminders_col()
    existing = await col.find_one({"tmdb_id": tmdb_id})

    if existing is None:
        await col.insert_one({
            "tmdb_id":  tmdb_id,
            "db_index": db_index,
            "title":    title,
            "poster":   poster,
            "status":   status,
            "user_ids": [user_id],
        })
        return {"subscribed": True, "message": "Hatırlatma aktif edildi"}

    if user_id in (existing.get("user_ids") or []):
        await col.update_one(
            {"tmdb_id": tmdb_id},
            {"$pull": {"user_ids": user_id}},
        )
        return {"subscribed": False, "message": "Hatırlatma iptal edildi"}
    else:
        set_fields = {"db_index": db_index, "title": title, "poster": poster}
        if status:
            set_fields["status"] = status
        await col.update_one(
            {"tmdb_id": tmdb_id},
            {
                "$addToSet": {"user_ids": user_id},
                "$set": set_fields,
            },
        )
        return {"subscribed": True, "message": "Hatırlatma aktif edildi"}


# ── GET /api/uye/hatirla/durum ───────────────────────────────────────────────

async def reminder_status(
    request:  Request,
    tmdb_id:  int = Query(...),
    db_index: int = Query(0),
):
    member = _require_member(request)
    try:
        user_id = int(member["user_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    col = _reminders_col()
    doc = await col.find_one({"tmdb_id": tmdb_id})
    subscribed = bool(doc and user_id in (doc.get("user_ids") or []))
    return {"subscribed": subscribed}


# ── GET /api/uye/hatirlatmalarim ─────────────────────────────────────────────

async def my_reminders(request: Request):
    member = _require_member(request)
    try:
        user_id = int(member["user_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    col = _reminders_col()
    cursor = col.find(
        {"user_ids": user_id},
        {"_id": 0, "tmdb_id": 1, "db_index": 1, "title": 1, "poster": 1, "status": 1},
    )
    items = await cursor.to_list(length=200)

    # Her dizi için güncel status'u media DB'den çek
    storage_keys = sorted(k for k in db.dbs if k.startswith("storage_"))
    for item in items:
        tmdb_id = item.get("tmdb_id")
        if not tmdb_id:
            continue
        for shard_key in storage_keys:
            try:
                doc = await db.dbs[shard_key]["tv"].find_one(
                    {"tmdb_id": tmdb_id},
                    {"_id": 0, "status": 1}
                )
                if doc and doc.get("status"):
                    item["status"] = doc["status"]
                    # tv_reminders'ı da güncelle (önbellekle)
                    await col.update_one(
                        {"tmdb_id": tmdb_id},
                        {"$set": {"status": doc["status"]}}
                    )
                    break
            except Exception:
                continue

    return {"reminders": items}


# ── POST /api/uye/film-hatirla ───────────────────────────────────────────────

async def toggle_movie_reminder(request: Request):
    """
    Eşleşme kriteri: sadece tmdb_id (db_index değişkendir, kriter değil).
    """
    member = _require_member(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Geçersiz JSON")

    tmdb_id  = body.get("tmdb_id")
    db_index = body.get("db_index", 0)
    title    = body.get("title", "")
    poster   = _validate_poster_url(body.get("poster", ""))
    status   = body.get("status", "")

    if tmdb_id is None:
        raise HTTPException(status_code=400, detail="tmdb_id zorunlu")

    try:
        user_id = int(member["user_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    col = _movie_reminders_col()
    existing = await col.find_one({"tmdb_id": tmdb_id})

    if existing is None:
        await col.insert_one({
            "tmdb_id":  tmdb_id,
            "db_index": db_index,
            "title":    title,
            "poster":   poster,
            "status":   status,
            "user_ids": [user_id],
        })
        return {"subscribed": True, "message": "Film hatırlatması aktif edildi"}

    if user_id in (existing.get("user_ids") or []):
        await col.update_one(
            {"tmdb_id": tmdb_id},
            {"$pull": {"user_ids": user_id}},
        )
        return {"subscribed": False, "message": "Film hatırlatması iptal edildi"}
    else:
        set_fields = {"db_index": db_index, "title": title, "poster": poster}
        if status:
            set_fields["status"] = status
        await col.update_one(
            {"tmdb_id": tmdb_id},
            {
                "$addToSet": {"user_ids": user_id},
                "$set": set_fields,
            },
        )
        return {"subscribed": True, "message": "Film hatırlatması aktif edildi"}


# ── GET /api/uye/film-hatirla/durum ─────────────────────────────────────────

async def movie_reminder_status(
    request:  Request,
    tmdb_id:  int = Query(...),
    db_index: int = Query(0),
):
    member = _require_member(request)
    try:
        user_id = int(member["user_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    col = _movie_reminders_col()
    doc = await col.find_one({"tmdb_id": tmdb_id})
    subscribed = bool(doc and user_id in (doc.get("user_ids") or []))
    return {"subscribed": subscribed}


# ── GET /api/uye/film-hatirlatmalarim ───────────────────────────────────────

async def my_movie_reminders(request: Request):
    member = _require_member(request)
    try:
        user_id = int(member["user_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    col = _movie_reminders_col()
    cursor = col.find(
        {"user_ids": user_id},
        {"_id": 0, "tmdb_id": 1, "db_index": 1, "title": 1, "poster": 1, "status": 1},
    )
    items = await cursor.to_list(length=200)
    return {"reminders": items}


# ─────────────────────────────────────────────────────────────────────────────
# İÇERİK İSTEĞİ (web sayfasından /istek komutu gibi)
# ─────────────────────────────────────────────────────────────────────────────

import re as _re
from datetime import datetime as _datetime

_IMDB_RE_W  = _re.compile(r"imdb\.com/title/(tt\d+)", _re.IGNORECASE)
_TMDB_MOV_W = _re.compile(r"themoviedb\.org/movie/(\d+)", _re.IGNORECASE)
_TMDB_TV_W  = _re.compile(r"themoviedb\.org/tv/(\d+)", _re.IGNORECASE)

def _parse_link_web(text: str):
    m = _IMDB_RE_W.search(text)
    if m:
        imdb_id = m.group(1)
        return f"https://www.imdb.com/title/{imdb_id}/", "unknown", 0, imdb_id
    m = _TMDB_MOV_W.search(text)
    if m:
        tid = int(m.group(1))
        return f"https://www.themoviedb.org/movie/{tid}", "movie", tid, str(tid)
    m = _TMDB_TV_W.search(text)
    if m:
        tid = int(m.group(1))
        return f"https://www.themoviedb.org/tv/{tid}", "tv", tid, str(tid)
    return None, None, 0, None


def _content_requests_col():
    return db.dbs["tracking"]["content_requests"]


# İstekler admin sayfası için basit poster/başlık önbelleği (tmdb_id bazlı).
# add_content_request (bot /istek) poster/title kaydetmediğinden, eksik
# olanlar burada TMDB'den tamamlanır.
_ISTEKLER_TMDB_CACHE: dict[str, dict] = {}


async def _fetch_tmdb_basic(media_type: str, tmdb_id: int) -> dict:
    """tmdb_id için sadece poster ve başlığı çeker (hafif, tek API çağrısı)."""
    if not tmdb_id or media_type not in ("movie", "tv"):
        return {}

    cache_key = f"{media_type}:{tmdb_id}"
    if cache_key in _ISTEKLER_TMDB_CACHE:
        return _ISTEKLER_TMDB_CACHE[cache_key]

    try:
        from Backend.helper.metadata import tmdb_tr, format_tmdb_image, API_SEMAPHORE
        async with API_SEMAPHORE:
            if media_type == "movie":
                details = await tmdb_tr.movie(tmdb_id).details()
                title = getattr(details, "title", "") or getattr(details, "original_title", "")
            else:
                details = await tmdb_tr.tv(tmdb_id).details()
                title = getattr(details, "name", "") or getattr(details, "original_name", "")

        poster_path = getattr(details, "poster_path", None)
        info = {
            "title": title or "",
            "poster": format_tmdb_image(poster_path) if poster_path else "",
        }
    except Exception as e:
        _logger.warning("TMDB detay çekilemedi (tmdb_id=%s, type=%s): %s", tmdb_id, media_type, e)
        info = {}

    if info.get("poster") or info.get("title"):
        _ISTEKLER_TMDB_CACHE[cache_key] = info
    return info


def _extract_imdb_id_from_link(link: str) -> str:
    """Bir içerik talebindeki linkten IMDB ID'yi (varsa) çıkarır."""
    if not link:
        return ""
    m = _IMDB_RE_W.search(link)
    return m.group(1) if m else ""


def _imdb_fallback_poster(imdb_id: str) -> str:
    """
    tmdb_id çözülemeyen eski talepler için Metahub üzerinden anında poster
    URL'i üretir (ekstra API çağrısı gerekmez).
    """
    if not imdb_id:
        return ""
    try:
        from Backend.helper.metadata import format_imdb_images
        return format_imdb_images(imdb_id).get("poster", "")
    except Exception:
        return ""


# ── POST /api/uye/icerik-iste ────────────────────────────────────────────────

async def submit_content_request(request: Request):
    """
    Üye web sayfasından içerik talebi gönderir.
    Gelen JSON: { "link": str, "note": str (opsiyonel) }
    - İsteği DB'ye kaydeder (bot /istek komutuyla aynı koleksiyon)
    - Yöneticiye Telegram bildirimi gönderir
    - Ayrıca isteği hatırlatma olarak da kaydeder (TMDB link ise)
    """
    member = _require_member(request)
    try:
        user_id = int(member["user_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Geçersiz JSON")

    raw_link = (body.get("link") or "").strip()
    note     = (body.get("note") or "").strip()[:200]
    title    = (body.get("title") or "").strip()[:200]
    poster   = _validate_poster_url(body.get("poster") or "")

    if not raw_link:
        raise HTTPException(status_code=400, detail="Link zorunlu")

    link, media_type, tmdb_id, display_id = _parse_link_web(raw_link)
    if link is None:
        raise HTTPException(status_code=400, detail="Geçersiz link. IMDB veya TMDB linki girin.")

    # Aylık limit kontrolü — count_user_requests_this_month created_at bazlı sayar (tutarlı)
    request_limit = await db.get_user_request_limit(user_id)
    monthly_count = await db.count_user_requests_this_month(user_id)
    if request_limit > 0 and monthly_count >= request_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Aylık istek limitine ulaştınız ({request_limit}). Bir sonraki ay sıfırlanır."
        )

    # DB'ye kaydet
    now = _datetime.utcnow()
    doc = {
        "user_id":    user_id,
        "link":       link,
        "media_type": media_type,
        "title":      title,
        "tmdb_id":    tmdb_id,
        "note":       note,
        "poster":     poster,
        "status":     "pending",
        "month":      _datetime.utcnow().strftime("%Y-%m"),
        "created_at": now,
        "source":     "web",   # bot'tan gelenlerden ayırt etmek için
    }
    result = await _content_requests_col().insert_one(doc)
    request_id = str(result.inserted_id)

    # Yöneticinin tarayıcısına Web Push bildirimi gönder (Telegram'dan bağımsız)
    _push_type_label = {"movie": "🎬 Film", "tv": "📺 Dizi", "unknown": "🎥 İçerik"}.get(media_type, "🎥 İçerik")
    asyncio.create_task(_notify_admins_push(
        title="Yeni İçerik Talebi",
        body=f"{_push_type_label} talebi: {title or raw_link}",
        url="/istekler",
        tag="istek-icerik",
    ))

    # Aynı zamanda hatırlatma da kur (TMDB link ise)
    reminder_set = False
    if media_type in ("tv", "movie") and tmdb_id:
        try:
            col = _reminders_col() if media_type == "tv" else _movie_reminders_col()
            existing = await col.find_one({"tmdb_id": tmdb_id})
            if existing is None:
                await col.insert_one({
                    "tmdb_id":  tmdb_id,
                    "db_index": 0,
                    "title":    title,
                    "poster":   poster,
                    "status":   "",
                    "user_ids": [user_id],
                })
            elif user_id not in (existing.get("user_ids") or []):
                await col.update_one(
                    {"tmdb_id": tmdb_id},
                    {"$addToSet": {"user_ids": user_id},
                     "$set": {"title": title or existing.get("title",""), "poster": poster or existing.get("poster","")}},
                )
            reminder_set = True
        except Exception as _e:
            _logger.warning("İstek sonrası hatırlatma kurulamadı: %s", _e)

    # Yöneticiye Telegram bildirimi gönder
    # Web session'da Telegram username'i bulunmadığından DB'den çekiyoruz
    try:
        _user_doc      = await db.get_user(user_id)
        username_val   = (_user_doc or {}).get("username") or member.get("name") or str(user_id)
    except Exception:
        username_val   = member.get("name") or str(user_id)
    first_name_val = member.get("name") or username_val or str(user_id)
    type_label     = {"movie": "🎬 Film", "tv": "📺 Dizi", "unknown": "🎥 Bilinmiyor"}.get(media_type, "?")
    title_str      = f"\n<b>📌 Başlık:</b> {_html.escape(title)}" if title else ""
    note_str       = f"\n<b>💬 Not:</b> {_html.escape(note)}" if note else ""
    limit_info     = f"\n📊 Bu ay: <b>{monthly_count + 1}/{request_limit}</b> istek" if request_limit > 0 else ""

    admin_text = (
        f"<b>🌐 Yeni İçerik Talebi (Web)</b>\n\n"
        f"<b>👤 Kullanıcı:</b> {_html.escape(first_name_val)}\n"
        f"<b>🔗 Kullanıcı Adı:</b> @{_html.escape(username_val)}\n"
        f"<b>🆔 Telegram ID:</b> <code>{user_id}</code>\n"
        f"<b>📂 Tür:</b> {type_label}{title_str}\n"
        f"<b>🔗 Link:</b> {link}{note_str}{limit_info}\n\n"
        f"Talebi onaylayın veya reddedin."
    )

    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Onayla", callback_data=f"req_approve_{request_id}_{user_id}"),
        InlineKeyboardButton("❌ Reddet", callback_data=f"req_reject_{request_id}_{user_id}"),
    ]])

    approver_ids = Telegram.APPROVER_IDS if Telegram.APPROVER_IDS else [Telegram.OWNER_ID]
    try:
        from Backend.pyrofork.bot import StreamBot as _StreamBot
    except Exception:
        _StreamBot = None
        _logger.warning("StreamBot import edilemedi, istek bildirimi gönderilemedi.")

    admin_messages = []
    if _StreamBot:
        for approver_id in approver_ids:
            try:
                sent = await _StreamBot.send_message(
                    approver_id,
                    admin_text,
                    reply_markup=keyboard,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                admin_messages.append({"chat_id": approver_id, "message_id": sent.id})
            except Exception as e:
                _logger.warning("İstek admin bildirimi gönderilemedi (%s): %s", approver_id, e)

    if admin_messages:
        try:
            await _content_requests_col().update_one(
                {"_id": _ObjectId(request_id)},
                {"$set": {"admin_messages": admin_messages}},
            )
        except Exception as e:
            _logger.warning("Admin mesaj id'leri kaydedilemedi (%s): %s", request_id, e)

    remaining = None
    if request_limit > 0:
        remaining = request_limit - (monthly_count + 1)

    return {
        "ok":          True,
        "request_id":  request_id,
        "reminder_set": reminder_set,
        "remaining":   remaining,
        "message":     "İsteğiniz alındı!" + (" Hatırlatma da kuruldu." if reminder_set else ""),
    }


# ── GET /api/uye/isteklerim ──────────────────────────────────────────────────

async def my_content_requests(request: Request):
    """Oturumdaki üyenin tüm içerik taleplerini döndürür."""
    member = _require_member(request)
    try:
        user_id = int(member["user_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    cursor = _content_requests_col().find(
        {"user_id": user_id},
        {"_id": 1, "link": 1, "media_type": 1, "title": 1, "poster": 1,
         "status": 1, "note": 1, "created_at": 1, "source": 1},
    ).sort("created_at", -1).limit(100)

    docs = await cursor.to_list(length=100)
    items = []
    for d in docs:
        items.append({
            "id":         str(d["_id"]),
            "link":       d.get("link", ""),
            "media_type": d.get("media_type", "unknown"),
            "title":      d.get("title", ""),
            "poster":     d.get("poster", ""),
            "status":     d.get("status", "pending"),
            "note":       d.get("note", ""),
            "source":     d.get("source", "bot"),
            "created_at": d["created_at"].isoformat() if d.get("created_at") else "",
        })
    # Limit bilgisini ekle
    request_limit = await db.get_user_request_limit(user_id)
    used_this_month = await db.count_user_requests_this_month(user_id)
    remaining = (request_limit - used_this_month) if request_limit > 0 else None

    return {
        "requests": items,
        "request_limit": request_limit,       # 0 = sınırsız
        "used_this_month": used_this_month,
        "remaining": remaining,               # None = sınırsız
    }


# ─────────────────────────────────────────────────────────────────────────────
# YÖNETİCİ PANELİ — İçerik İstekleri (istekler.html)
# ─────────────────────────────────────────────────────────────────────────────
# GET  /api/admin/istekler                → tüm talepleri içerik bazında gruplayarak döner
# POST /api/admin/istekler/aksiyon        → seçilen taleplerin tümünü onaylar/reddeder
#      Body: { "request_ids": ["..."], "action": "approve" | "reject" }

from bson import ObjectId as _ObjectId


async def admin_list_content_requests() -> dict:
    """
    Tüm içerik taleplerini (bot + web kaynaklı) aynı içeriğe (tmdb_id/link) göre
    gruplayarak döner. Aynı içeriği birden fazla üye istemişse hepsinin adı
    tek bir grupta listelenir.
    """
    cursor = _content_requests_col().find({}).sort("created_at", -1).limit(2000)
    docs = await cursor.to_list(length=2000)

    # Talep sahiplerinin bilgilerini toplu çek (isim/kullanıcı adı için)
    user_ids = {d.get("user_id") for d in docs if d.get("user_id")}
    users_map: dict = {}
    if user_ids:
        try:
            ucursor = db.dbs["tracking"]["users"].find(
                {"_id": {"$in": list(user_ids)}},
                {"_id": 1, "first_name": 1, "username": 1},
            )
            async for u in ucursor:
                users_map[u["_id"]] = u
        except Exception as e:
            _logger.warning("Kullanıcı bilgileri çekilemedi: %s", e)

    groups: dict = {}
    order: list = []

    for d in docs:
        media_type = d.get("media_type") or "unknown"
        tmdb_id = d.get("tmdb_id") or 0
        link = d.get("link", "")
        key = f"{media_type}:{tmdb_id}" if tmdb_id else f"link:{link}"

        created_at = d.get("created_at")

        if key not in groups:
            groups[key] = {
                "group_id": key,
                "media_type": media_type,
                "tmdb_id": tmdb_id,
                "link": link,
                "title": d.get("title", ""),
                "poster": d.get("poster", ""),
                "requesters": [],
                "request_ids": [],
                "statuses": set(),
                "first_requested_at": created_at,
                "last_requested_at": created_at,
            }
            order.append(key)

        g = groups[key]
        if not g["title"] and d.get("title"):
            g["title"] = d["title"]
        if not g["poster"] and d.get("poster"):
            g["poster"] = d["poster"]

        if created_at:
            if not g["first_requested_at"] or created_at < g["first_requested_at"]:
                g["first_requested_at"] = created_at
            if not g["last_requested_at"] or created_at > g["last_requested_at"]:
                g["last_requested_at"] = created_at

        uid = d.get("user_id")
        u = users_map.get(uid, {})
        name = u.get("first_name") or u.get("username") or (f"Kullanıcı {uid}" if uid else "Bilinmeyen")
        status = d.get("status", "pending")

        # Aynı kullanıcı aynı içeriği birden fazla kez istediyse tekilleştir,
        # en güncel talebi esas al.
        existing_req = next((r for r in g["requesters"] if r["user_id"] == uid), None)
        if existing_req:
            if created_at and (not existing_req.get("_created_at") or created_at > existing_req["_created_at"]):
                existing_req["status"] = status
                existing_req["_created_at"] = created_at
                existing_req["created_at"] = created_at.isoformat() if created_at else ""
                existing_req["request_id"] = str(d["_id"])
        else:
            g["requesters"].append({
                "user_id": uid,
                "name": name,
                "username": u.get("username", ""),
                "status": status,
                "request_id": str(d["_id"]),
                "created_at": created_at.isoformat() if created_at else "",
                "_created_at": created_at,
            })

        g["request_ids"].append(str(d["_id"]))
        g["statuses"].add(status)

    result = []
    counts = {"all": 0, "pending": 0, "approved": 0, "rejected": 0}

    for key in order:
        g = groups[key]
        statuses = g["statuses"]
        if "pending" in statuses:
            group_status = "pending"
        elif "approved" in statuses:
            group_status = "approved"
        else:
            group_status = "rejected" if statuses else "pending"

        counts["all"] += 1
        counts[group_status] = counts.get(group_status, 0) + 1

        requesters = sorted(
            g["requesters"], key=lambda r: r["_created_at"] or "", reverse=True
        )
        for r in requesters:
            r.pop("_created_at", None)

        result.append({
            "group_id": g["group_id"],
            "media_type": g["media_type"],
            "tmdb_id": g["tmdb_id"],
            "link": g["link"],
            "title": g["title"],
            "poster": g["poster"],
            "status": group_status,
            "requesters": requesters,
            "request_ids": g["request_ids"],
            "request_count": len(requesters),
            "first_requested_at": g["first_requested_at"].isoformat() if g["first_requested_at"] else "",
            "last_requested_at": g["last_requested_at"].isoformat() if g["last_requested_at"] else "",
        })

    result.sort(key=lambda g: g["last_requested_at"], reverse=True)

    # Poster veya başlığı eksik olan gruplar için TMDB'den tamamla
    # (bot /istek komutuyla gelen talepler poster/title kaydetmez).
    fetch_targets = [g for g in result if g["tmdb_id"] and (not g["poster"] or not g["title"])]
    if fetch_targets:
        fetched = await asyncio.gather(
            *[_fetch_tmdb_basic(g["media_type"], g["tmdb_id"]) for g in fetch_targets],
            return_exceptions=True,
        )
        for g, info in zip(fetch_targets, fetched):
            if isinstance(info, dict):
                if not g["poster"] and info.get("poster"):
                    g["poster"] = info["poster"]
                if not g["title"] and info.get("title"):
                    g["title"] = info["title"]

    # tmdb_id hiç çözülememiş eski talepler (media_type "unknown", tmdb_id 0)
    # için linkten IMDB ID çıkarıp Metahub posteri ile tamamla.
    for g in result:
        if not g["poster"]:
            imdb_id = _extract_imdb_id_from_link(g["link"])
            if imdb_id:
                g["poster"] = _imdb_fallback_poster(imdb_id)

    return {"groups": result, "counts": counts}


async def _notify_requester(user_id: int, doc: dict, new_status: str) -> None:
    """Talep sahibine onay/red durumunu Telegram üzerinden bildirir."""
    type_label = {"movie": "🎬 Film", "tv": "📺 Dizi", "unknown": "🎥 Bilinmiyor"}.get(
        doc.get("media_type", "unknown"), "?"
    )
    title_str = f"\n<b>📌 Başlık:</b> {_html.escape(doc.get('title',''))}" if doc.get("title") else ""
    link = doc.get("link", "")

    if new_status == "approved":
        text = (
            f"✅ <b>İçerik Talebiniz Onaylandı!</b>\n\n"
            f"<b>📂 Tür:</b> {type_label}{title_str}\n"
            f"<b>🔗 Link:</b> {link}\n\n"
            "Talebiniz yönetici tarafından onaylandı. İçerik en kısa sürede platforma eklenecektir."
        )
    else:
        text = (
            f"❌ <b>İçerik Talebiniz Reddedildi</b>\n\n"
            f"<b>📂 Tür:</b> {type_label}{title_str}\n"
            f"<b>🔗 Link:</b> {link}\n\n"
            "Maalesef talebiniz yönetici tarafından reddedildi."
        )

    try:
        from Backend.pyrofork.bot import StreamBot as _StreamBot
        if _StreamBot:
            await _StreamBot.send_message(user_id, text, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        _logger.warning("Kullanıcıya bildirim gönderilemedi (%s): %s", user_id, e)


async def _notify_admins_web_action(
    admin_name: str,
    requester_names: list,
    title: str,
    media_type: str,
    link: str,
    new_status: str,
) -> None:
    """
    Panelden (istekler.html) onay/red işlemi yapıldığında yöneticinin botuna
    (APPROVER_IDS / OWNER_ID) yeni bir bilgilendirme mesajı gönderir.
    Örn: "Ahmet'in Inception talebi web üzerinden onaylandı."
    """
    try:
        from Backend.pyrofork.bot import StreamBot as _StreamBot
    except Exception:
        _StreamBot = None
        _logger.warning("StreamBot import edilemedi, panel-aksiyon admin bildirimi gönderilemedi.")
        return
    if not _StreamBot:
        return

    label = "✅ Onaylandı" if new_status == "approved" else "❌ Reddedildi"
    type_label = {"movie": "🎬 Film", "tv": "📺 Dizi", "unknown": "🎥 Bilinmiyor"}.get(
        media_type, "?"
    )
    names_str = ", ".join(_html.escape(str(n)) for n in requester_names) if requester_names else "Bilinmeyen"
    title_str = f"\n<b>📌 Başlık:</b> {_html.escape(title)}" if title else ""
    link_str = f"\n<b>🔗 Link:</b> {link}" if link else ""
    admin_str = f"\n<b>👮 İşlemi Yapan:</b> {_html.escape(admin_name)}" if admin_name else ""

    text = (
        f"<b>🌐 Web Panelinden İşlem — {label}</b>\n\n"
        f"<b>👤 Talep Eden:</b> {names_str}\n"
        f"<b>📂 Tür:</b> {type_label}{title_str}{link_str}{admin_str}"
    )

    approver_ids = Telegram.APPROVER_IDS if Telegram.APPROVER_IDS else [Telegram.OWNER_ID]
    for approver_id in approver_ids:
        try:
            await _StreamBot.send_message(
                approver_id,
                text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            _logger.warning("Panel aksiyonu admin bildirimi gönderilemedi (%s): %s", approver_id, e)


async def admin_review_content_requests(request: Request) -> dict:
    """
    Yönetici panelinden toplu onay/red işlemi.
    Body: { "request_ids": ["<id>", ...], "action": "approve" | "reject" }
    Bir gruptaki tüm talepler (aynı içeriği isteyen tüm üyeler) tek seferde
    onaylanır/reddedilir ve her üyeye Telegram bildirimi gönderilir.
    Ayrıca yöneticinin botuna da (APPROVER_IDS / OWNER_ID) işlemi özetleyen
    yeni bir mesaj gönderilir.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Geçersiz JSON")

    request_ids = body.get("request_ids") or []
    action = body.get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Geçersiz aksiyon")
    if not request_ids:
        raise HTTPException(status_code=400, detail="request_ids zorunlu")

    new_status = "approved" if action == "approve" else "rejected"
    label = "✅ Onaylandı" if new_status == "approved" else "❌ Reddedildi"
    updated = 0
    notified_users: set = set()

    # İşlemi yapan yöneticinin adı (panel oturumundan).
    # NOT: get_current_user()'ın döndürdüğü "name", session["username"]
    # yani rastgele üretilmiş OTP giriş kullanıcı adıdır (ör. "kızılaslan7430"),
    # Telegram adı DEĞİLDİR. Gerçek Telegram görünen adı login sırasında
    # session["member"]["name"] içine admin_doc["display_name"] olarak
    # yazılır (bkz. template_routes.py login endpoint) — o yüzden burada
    # onu kullanıyoruz.
    try:
        admin_name = (request.session.get("member") or {}).get("name") or "Yönetici"
    except Exception:
        admin_name = "Yönetici"

    # Panel botuna gönderilecek özet mesaj için toplanan bilgiler
    action_requester_names: list = []
    action_title = ""
    action_media_type = ""
    action_link = ""

    try:
        from Backend.pyrofork.bot import StreamBot as _StreamBot
    except Exception:
        _StreamBot = None
        _logger.warning("StreamBot import edilemedi, panel-onay bot senkronizasyonu atlanacak.")

    for rid in request_ids:
        try:
            doc = await _content_requests_col().find_one({"_id": _ObjectId(rid)})
        except Exception:
            doc = None
        if not doc:
            continue
        if doc.get("status") == new_status:
            continue

        await _content_requests_col().update_one(
            {"_id": doc["_id"]},
            {"$set": {"status": new_status, "updated_at": _datetime.utcnow()}},
        )
        updated += 1

        if not action_title and doc.get("title"):
            action_title = doc["title"]
        if not action_media_type:
            action_media_type = doc.get("media_type", "unknown")
        if not action_link and doc.get("link"):
            action_link = doc["link"]

        user_id = doc.get("user_id")
        if user_id and user_id not in notified_users:
            notified_users.add(user_id)
            await _notify_requester(user_id, doc, new_status)

            # Talep sahibinin görünen adını yakala (panel botu mesajı için)
            try:
                _u_doc = await db.get_user(user_id)
                _r_name = (_u_doc or {}).get("first_name") or (_u_doc or {}).get("username") or str(user_id)
            except Exception:
                _r_name = str(user_id)
            action_requester_names.append(_r_name)

        # Bu talep için yöneticilere gönderilmiş bot mesajlarını güncelle:
        # onayla/reddet butonlarını kaldır ve panelden alınan kararı göster.
        # Bu adım olmadan talep panelden onaylansa/reddedilse bile botta
        # "beklemede" görünmeye ve butonlar görünmeye devam eder.
        admin_messages = doc.get("admin_messages") or []
        if _StreamBot and admin_messages:
            link = doc.get("link", "")
            type_label = {"movie": "🎬 Film", "tv": "📺 Dizi", "unknown": "🎥 Bilinmiyor"}.get(
                doc.get("media_type", "unknown"), "?"
            )
            status_section = (
                f"\n\n{'─' * 30}\n"
                f"<b>{label}</b> — 🌐 Web panelinden\n"
                f"<b>📂 Tür:</b> {type_label}\n"
                f"<b>🔗 Link:</b> {link}"
            )
            for am in admin_messages:
                try:
                    existing_msg = await _StreamBot.get_messages(am["chat_id"], am["message_id"])
                    original_text = existing_msg.text or existing_msg.caption or ""
                except Exception:
                    original_text = ""
                try:
                    await _StreamBot.edit_message_text(
                        chat_id=am["chat_id"],
                        message_id=am["message_id"],
                        text=f"{original_text}{status_section}" if original_text else status_section,
                        parse_mode=enums.ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=None,
                    )
                except Exception as e:
                    _logger.warning(
                        "Panel onayı sonrası admin mesajı güncellenemedi (%s/%s): %s",
                        am.get("chat_id"), am.get("message_id"), e
                    )

    # Yöneticinin botuna işlemi özetleyen yeni bir mesaj gönder
    # (örn. "Ahmet'in Inception talebi web üzerinden onaylandı.")
    if updated > 0:
        await _notify_admins_web_action(
            admin_name=admin_name,
            requester_names=action_requester_names,
            title=action_title,
            media_type=action_media_type,
            link=action_link,
            new_status=new_status,
        )

    return {"ok": True, "updated": updated, "status": new_status}


# ─────────────────────────────────────────────────────────────────────────────
# İstekler sayacı (base.html sidebar rozeti) + Web Push (yönetici bildirimleri)
# ─────────────────────────────────────────────────────────────────────────────
# GET  /api/admin/istekler/sayac        → bekleyen içerik + abonelik talebi sayısı
# GET  /api/admin/push/public-key       → tarayıcının abone olması için VAPID public key
# POST /api/admin/push/abone-ol         → tarayıcının Push aboneliğini kaydeder
# POST /api/admin/push/abonelik-iptal   → tarayıcının Push aboneliğini siler

async def admin_istekler_counter() -> dict:
    """
    base.html'deki 'İstekler' sidebar linkinin yanındaki rozet için bekleyen
    talep sayısını döner. Örn: 2 bekleyen film/dizi talebi + 5 bekleyen
    abonelik talebi varsa total=7 döner ve arayüzde '7' olarak gösterilir.
    """
    return await db.get_istekler_pending_count()


async def admin_push_public_key() -> dict:
    """Tarayıcının PushManager.subscribe() çağrısında kullanacağı VAPID public key."""
    vapid = await db.get_or_create_vapid_keys()
    return {"public_key": vapid["public_key"]}


async def admin_push_subscribe(request: Request) -> dict:
    """
    Yönetici tarayıcısı bildirim iznini verdikten sonra tarayıcının ürettiği
    PushSubscription nesnesini kaydeder.
    Body: { "subscription": { "endpoint": str, "keys": {"p256dh": str, "auth": str} } }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Geçersiz JSON")

    subscription = body.get("subscription")
    if not subscription or not subscription.get("endpoint"):
        raise HTTPException(status_code=400, detail="Geçersiz abonelik verisi")

    user_agent = request.headers.get("user-agent", "")
    await db.add_push_subscription(subscription, user_agent=user_agent)
    return {"ok": True}


async def admin_push_unsubscribe(request: Request) -> dict:
    """Yönetici bildirimleri kapattığında ilgili aboneliği DB'den siler."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    endpoint = (body or {}).get("endpoint", "")
    if endpoint:
        await db.remove_push_subscription(endpoint)
    return {"ok": True}
