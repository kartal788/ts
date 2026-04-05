"""
eskiverileriyenile.py — Pyrogram bot eklentisi
================================================
/eskiverileriyenile komutuyla çalışır (sadece OWNER).
Veritabanındaki tüm film ve dizilerin TR/DE çevirilerini
ve TMDB görsel/sertifika alanlarını tamamlar.

Güvenli kurallar:
  - Mevcut dolu alanların üzerine YAZILMAZ.
  - Korunacak alanlar: title, description, genres, logo, poster,
    backdrop, overview (orijinal).
  - /iptal komutuyla işlem durdurulabilir.
  - Durum mesajı her 15 saniyede bir güncellenir (değişiklik yoksa düzenlenmez).

Kurulum:
  Bu dosyayı Backend/pyrofork/plugins/ klasörüne koyun.
  Gerekli paketler: deep-translator, httpx, psutil (zaten yüklü olmalı)
"""

from __future__ import annotations

import asyncio
import time
import os
import logging

import httpx
import psutil
from pymongo import MongoClient
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from deep_translator import GoogleTranslator

from Backend.config import Telegram
from Backend.helper.custom_filter import CustomFilters
from Backend.helper.metadata import GENRE_TUR_ALIASES, GENRE_DE_ALIASES
from Backend.logger import LOGGER

# ─────────────────────────────────────────────────────────────
# Sabitler
# ─────────────────────────────────────────────────────────────
TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_IMG_BASE   = "https://image.tmdb.org/t/p"
STATUS_INTERVAL = 15    # sn — durum mesajı güncelleme aralığı
CONCURRENCY     = 3     # eş zamanlı TMDB isteği
TRANSLATE_DELAY = 0.12  # çeviri istekleri arası bekleme (sn)
BATCH_SIZE      = 50    # tek seferinde çekilecek kayıt sayısı

# ─────────────────────────────────────────────────────────────
# Çalışma durumu (aynı anda tek iş çalışabilir)
# ─────────────────────────────────────────────────────────────
_running        = False
_cancel_event   = asyncio.Event()
_tmdb_sem: asyncio.Semaphore | None = None


# ─────────────────────────────────────────────────────────────
# Veritabanı bağlantısı (pymongo — senkron, hafif)
# ─────────────────────────────────────────────────────────────
def _get_storage_collections():
    """Tüm storage_N DB'lerindeki movie/tv koleksiyonlarını ve kayıt sayılarını döner."""
    db_urls = Telegram.DATABASE
    if not db_urls or len(db_urls) < 2:
        raise RuntimeError("En az 2 DATABASE URI gerekli (tracking + storage).")

    db_name = os.getenv("DB_NAME", "dbFyvio")
    result  = []
    for uri in db_urls[1:]:   # index-0 tracking DB, atla
        cli = MongoClient(uri, serverSelectionTimeoutMS=8000)
        db  = cli[db_name]
        for coll_name, mt in [("movie", "movie"), ("tv", "tv")]:
            col   = db[coll_name]
            count = col.count_documents({})
            if count:
                result.append((col, mt, count, cli))
    return result


# ─────────────────────────────────────────────────────────────
# İlerleme takibi
# ─────────────────────────────────────────────────────────────
class Progress:
    def __init__(self, total: int):
        self.total      = total
        self.done       = 0
        self.changed    = 0
        self.skipped    = 0
        self.errors     = 0
        self.start_time = time.time()

    def eta(self) -> str:
        elapsed = time.time() - self.start_time
        if self.done == 0:
            return "hesaplanıyor"
        rate      = self.done / elapsed
        remaining = (self.total - self.done) / rate
        m, s      = divmod(int(remaining), 60)
        h, m      = divmod(m, 60)
        if h: return f"{h}s {m}d {s}sn"
        if m: return f"{m}d {s}sn"
        return f"{s}sn"

    def sys_info(self) -> str:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        return (
            f"🖥 CPU: {cpu:.1f}%  "
            f"RAM: {ram.used // 1024 // 1024}MB"
            f"/{ram.total // 1024 // 1024}MB"
            f" ({ram.percent:.1f}%)"
        )

    def build_msg(self, current: str = "") -> str:
        pct    = (self.done / self.total * 100) if self.total else 0
        filled = int(15 * pct / 100)
        bar    = "█" * filled + "░" * (15 - filled)
        lines  = [
            "🔄 <b>Veri Yenileme İşlemi</b>",
            f"<code>[{bar}]</code> <b>%{pct:.1f}</b>",
            f"✏️ Güncellenen : {self.changed}  ⏭ Atlanan : {self.skipped}  ❌ Hata : {self.errors}",
            f"⏱ Kalan süre  : <b>{self.eta()}</b>",
            self.sys_info(),
        ]
        if current:
            short = current[:45] + ("…" if len(current) > 45 else "")
            lines.append(f"🎬 Şu an : <i>{short}</i>")
        lines.append("\n📌 Durdurmak için /iptal gönderin")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# TMDB yardımcıları
# ─────────────────────────────────────────────────────────────
def _img(path: str, size: str = "w500") -> str:
    return f"{TMDB_IMG_BASE}/{size}{path}" if path else ""


async def _tmdb_get(endpoint: str, params: dict | None = None) -> dict | None:
    if not Telegram.TMDB_API:
        return None
    p = {"api_key": Telegram.TMDB_API, **(params or {})}
    async with _tmdb_sem:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=12) as c:
                    r = await c.get(f"{TMDB_BASE}/{endpoint}", params=p)
                if r.status_code == 429:
                    await asyncio.sleep(6 * (attempt + 1))
                    continue
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
            except Exception as e:
                LOGGER.debug("TMDB [%s] hata: %s", endpoint, e)
                await asyncio.sleep(2)
    return None


def _pick_lang(items: list, lang: str, size: str) -> str:
    for item in items:
        if item.get("iso_639_1") == lang and item.get("file_path"):
            return _img(item["file_path"], size)
    return ""


async def _tmdb_lang_images(tmdb_id: int, media_type: str, lang: str) -> dict:
    ep   = "movie" if media_type == "movie" else "tv"
    data = await _tmdb_get(
        f"{ep}/{tmdb_id}/images",
        {"include_image_language": f"{lang},null"}
    )
    if not data:
        return {}
    result = {}
    p = _pick_lang(data.get("posters",   []), lang, "w500")
    b = _pick_lang(data.get("backdrops", []), lang, "original")
    lo = _pick_lang(data.get("logos",    []), lang, "w300")
    if p:  result[f"poster_{lang}"]   = p
    if b:  result[f"backdrop_{lang}"] = b
    if lo: result[f"logo_{lang}"]     = lo
    return result


async def _tmdb_certs(tmdb_id: int, media_type: str) -> dict:
    cmap   = {"TR": "certification_tr", "DE": "certification_de", "US": "certification_us"}
    result = {}
    if media_type == "movie":
        data = await _tmdb_get(f"movie/{tmdb_id}/release_dates")
        if data:
            for entry in data.get("results", []):
                iso = entry.get("iso_3166_1")
                if iso in cmap:
                    cert = next(
                        (r.get("certification", "") for r in entry.get("release_dates", [])
                         if r.get("certification")), ""
                    )
                    if cert: result[cmap[iso]] = cert
    else:
        data = await _tmdb_get(f"tv/{tmdb_id}/content_ratings")
        if data:
            for entry in data.get("results", []):
                iso = entry.get("iso_3166_1")
                if iso in cmap:
                    rating = entry.get("rating", "")
                    if rating: result[cmap[iso]] = rating
    return result


async def _tmdb_main(tmdb_id: int, media_type: str) -> dict:
    ep   = "movie" if media_type == "movie" else "tv"
    data = await _tmdb_get(f"{ep}/{tmdb_id}")
    if not data:
        return {}
    result = {}
    lang = data.get("original_language")
    if lang: result["original_language"] = lang
    if media_type == "movie":
        col = data.get("belongs_to_collection")
        if col and col.get("id"):
            result["collection_id"] = col["id"]
    return result



async def _tmdb_titles(tmdb_id: int, media_type: str) -> dict:
    """TMDB'den Türkçe ve Almanca başlık çeker (sadece title_tr / title_de)."""
    ep = "movie" if media_type == "movie" else "tv"
    result = {}

    # Türkçe başlık
    data_tr = await _tmdb_get(f"{ep}/{tmdb_id}", {"language": "tr-TR"})
    if data_tr:
        t = (data_tr.get("title") or data_tr.get("name") or "").strip()
        if t:
            result["title_tr"] = t

    # Almanca başlık
    data_de = await _tmdb_get(f"{ep}/{tmdb_id}", {"language": "de-DE"})
    if data_de:
        t = (data_de.get("title") or data_de.get("name") or "").strip()
        if t:
            result["title_de"] = t

    return result


async def _tmdb_imdb_id(tmdb_id: int, media_type: str) -> str:
    """TMDB'den external_ids üzerinden imdb_id çeker."""
    ep   = "movie" if media_type == "movie" else "tv"
    data = await _tmdb_get(f"{ep}/{tmdb_id}/external_ids")
    if data:
        return data.get("imdb_id") or ""
    return ""


async def _tmdb_full_lang(tmdb_id: int, media_type: str) -> dict:
    """
    TMDB'den Türkçe ve Almanca:
      - title_tr / title_de
      - description_tr / description_de
      - genres_tr / genres_de (alias tablosu ile)
    döner. Boş kalan alanlar dict'te yer almaz.
    """
    ep = "movie" if media_type == "movie" else "tv"
    result = {}

    data_tr = await _tmdb_get(f"{ep}/{tmdb_id}", {"language": "tr-TR"})
    if data_tr:
        t = (data_tr.get("title") or data_tr.get("name") or "").strip()
        o = (data_tr.get("overview") or "").strip()
        if t: result["title_tr"] = t
        if o: result["description_tr"] = o
        raw_genres = [g.get("name", "") for g in data_tr.get("genres", []) if g.get("name")]
        if raw_genres:
            aliases = GENRE_TUR_ALIASES
            result["genres_tr"] = [aliases.get(g.lower().strip(), g) for g in raw_genres]

    data_de = await _tmdb_get(f"{ep}/{tmdb_id}", {"language": "de-DE"})
    if data_de:
        t = (data_de.get("title") or data_de.get("name") or "").strip()
        o = (data_de.get("overview") or "").strip()
        if t: result["title_de"] = t
        if o: result["description_de"] = o
        raw_genres = [g.get("name", "") for g in data_de.get("genres", []) if g.get("name")]
        if raw_genres:
            aliases = GENRE_DE_ALIASES
            result["genres_de"] = [aliases.get(g.lower().strip(), g) for g in raw_genres]

    return result


async def _tmdb_episode_lang(tmdb_id: int, season_no: int, ep_no: int, lang: str) -> dict:
    """Tek bir TV bölümünün dil bazlı title ve overview bilgisini çeker."""
    data = await _tmdb_get(
        f"tv/{tmdb_id}/season/{season_no}/episode/{ep_no}",
        {"language": f"{lang}-{lang.upper()}"}
    )
    if not data:
        return {}
    result = {}
    t = (data.get("name") or "").strip()
    o = (data.get("overview") or "").strip()
    if t: result["title"] = t
    if o: result["overview"] = o
    return result


# ─────────────────────────────────────────────────────────────
# Çeviri yardımcıları (deep_translator — title/title_de hariç)
# ─────────────────────────────────────────────────────────────
_tr_cache: dict = {}


def _translate(text: str, target: str) -> str:
    if not text or not text.strip():
        return text
    key = (text[:100], target)
    if key in _tr_cache:
        return _tr_cache[key]
    try:
        out = GoogleTranslator(source="auto", target=target).translate(text)
        _tr_cache[key] = out or text
    except Exception:
        _tr_cache[key] = text
    return _tr_cache[key]


async def _tr(text: str, target: str) -> str:
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _translate, text, target)
    await asyncio.sleep(TRANSLATE_DELAY)
    return result


def _genres_tr(genres: list, lang: str) -> list:
    aliases = GENRE_TUR_ALIASES if lang == "tr" else GENRE_DE_ALIASES
    return [aliases.get(g.lower().strip(), _translate(g, lang)) for g in genres]


# ─────────────────────────────────────────────────────────────
# Tek doküman işleme
# ─────────────────────────────────────────────────────────────
async def _process(doc: dict, col, media_type: str) -> bool:
    """Bir kaydı günceller. True → en az bir alan değişti."""
    upd     = {}
    tmdb_id = doc.get("tmdb_id")
    imdb_id = doc.get("imdb_id", "")
    # tmdb_id ile imdb_id STRING olarak birebir eşitse görsel/sertifika için TMDB'ye gitme
    is_same = (tmdb_id is not None) and (str(tmdb_id) == str(imdb_id))

    # ── TMDB'den imdb_id çekip veritabanındakiyle karşılaştır ──────────────
    # imdb_id'ler eşleşiyorsa → TMDB dil verilerini öncelikli kullan, yoksa _tr ile doldur
    # imdb_id'ler farklıysa → direkt deep_translator
    tmdb_imdb_match = False
    full_lang: dict = {}
    if tmdb_id and imdb_id:
        tmdb_ext_imdb = await _tmdb_imdb_id(tmdb_id, media_type)
        if tmdb_ext_imdb and tmdb_ext_imdb == str(imdb_id):
            tmdb_imdb_match = True
            # Başlık, açıklama, tür için TMDB dil verisini çek
            needs_lang = (
                not doc.get("title_tr") or not doc.get("title_de") or
                not doc.get("description_tr") or not doc.get("description_de") or
                not doc.get("genres_tr") or not doc.get("genres_de")
            )
            if needs_lang:
                full_lang = await _tmdb_full_lang(tmdb_id, media_type)

    # ── Başlık ──────────────────────────────────────────────────────────────
    if not doc.get("title_tr"):
        val = full_lang.get("title_tr") if tmdb_imdb_match else None
        upd["title_tr"] = val if val else await _tr(doc.get("title", ""), "tr")
    if not doc.get("title_de"):
        val = full_lang.get("title_de") if tmdb_imdb_match else None
        upd["title_de"] = val if val else await _tr(doc.get("title", ""), "de")

    # ── Açıklama ────────────────────────────────────────────────────────────
    desc = doc.get("description", "")
    if desc:
        if not doc.get("description_tr"):
            val = full_lang.get("description_tr") if tmdb_imdb_match else None
            upd["description_tr"] = val if val else await _tr(desc, "tr")
        if not doc.get("description_de"):
            val = full_lang.get("description_de") if tmdb_imdb_match else None
            upd["description_de"] = val if val else await _tr(desc, "de")

    # ── Türler ──────────────────────────────────────────────────────────────
    genres = doc.get("genres", [])
    if genres:
        if not doc.get("genres_tr"):
            val = full_lang.get("genres_tr") if tmdb_imdb_match else None
            upd["genres_tr"] = val if val else _genres_tr(genres, "tr")
        if not doc.get("genres_de"):
            val = full_lang.get("genres_de") if tmdb_imdb_match else None
            upd["genres_de"] = val if val else _genres_tr(genres, "de")

    # ── TV bölüm başlık / overview ──────────────────────────────────────────
    if media_type == "tv":
        seasons       = doc.get("seasons", [])
        seasons_dirty = False
        for si, season in enumerate(seasons):
            season_no = season.get("season_number")
            for ei, ep in enumerate(season.get("episodes", [])):
                ep_no    = ep.get("episode_number")
                ep_title = ep.get("title", "")
                ep_ov    = ep.get("overview", "")
                dirty    = False

                needs_ep = (
                    (ep_title and (not ep.get("title_tr") or not ep.get("title_de"))) or
                    (ep_ov    and (not ep.get("overview_tr") or not ep.get("overview_de")))
                )
                # TMDB eşleşmesi varsa ve bölüm no biliniyorsa TMDB'den çek
                ep_lang_tr: dict = {}
                ep_lang_de: dict = {}
                if tmdb_imdb_match and needs_ep and tmdb_id and season_no is not None and ep_no is not None:
                    ep_lang_tr = await _tmdb_episode_lang(tmdb_id, season_no, ep_no, "tr")
                    ep_lang_de = await _tmdb_episode_lang(tmdb_id, season_no, ep_no, "de")

                if ep_title:
                    if not ep.get("title_tr"):
                        val = ep_lang_tr.get("title") if ep_lang_tr else None
                        seasons[si]["episodes"][ei]["title_tr"] = val if val else await _tr(ep_title, "tr")
                        dirty = True
                    if not ep.get("title_de"):
                        val = ep_lang_de.get("title") if ep_lang_de else None
                        seasons[si]["episodes"][ei]["title_de"] = val if val else await _tr(ep_title, "de")
                        dirty = True
                if ep_ov:
                    if not ep.get("overview_tr"):
                        val = ep_lang_tr.get("overview") if ep_lang_tr else None
                        seasons[si]["episodes"][ei]["overview_tr"] = val if val else await _tr(ep_ov, "tr")
                        dirty = True
                    if not ep.get("overview_de"):
                        val = ep_lang_de.get("overview") if ep_lang_de else None
                        seasons[si]["episodes"][ei]["overview_de"] = val if val else await _tr(ep_ov, "de")
                        dirty = True
                if dirty:
                    seasons_dirty = True
        if seasons_dirty:
            upd["seasons"] = seasons

    # ── TMDB görsel / sertifika alanları ───────────────────
    if tmdb_id and not is_same:
        # TR görseller
        if not (doc.get("poster_tr") and doc.get("logo_tr") and doc.get("backdrop_tr")):
            for k, v in (await _tmdb_lang_images(tmdb_id, media_type, "tr")).items():
                if not doc.get(k):
                    upd[k] = v

        # DE görseller
        if not (doc.get("poster_de") and doc.get("logo_de") and doc.get("backdrop_de")):
            for k, v in (await _tmdb_lang_images(tmdb_id, media_type, "de")).items():
                if not doc.get(k):
                    upd[k] = v

        # Sertifikalar
        if not (doc.get("certification_tr") and doc.get("certification_de") and doc.get("certification_us")):
            for k, v in (await _tmdb_certs(tmdb_id, media_type)).items():
                if not doc.get(k) and v:
                    upd[k] = v

        # original_language / collection_id
        need_extra = (not doc.get("original_language")) or \
                     (media_type == "movie" and not doc.get("collection_id"))
        if need_extra:
            for k, v in (await _tmdb_main(tmdb_id, media_type)).items():
                if not doc.get(k) and v:
                    upd[k] = v

    if upd:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: col.update_one({"_id": doc["_id"]}, {"$set": upd})
        )
        return True
    return False


# ─────────────────────────────────────────────────────────────
# Arka plan iş görevi
# ─────────────────────────────────────────────────────────────
async def _main_task(status_msg: Message):
    global _running, _cancel_event, _tmdb_sem

    _tmdb_sem = asyncio.Semaphore(CONCURRENCY)

    # Koleksiyonları yükle
    try:
        loop     = asyncio.get_event_loop()
        all_cols = await loop.run_in_executor(None, _get_storage_collections)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Veritabanı bağlantısı kurulamadı:\n<code>{e}</code>",
            parse_mode=enums.ParseMode.HTML
        )
        _running = False
        return

    if not all_cols:
        await status_msg.edit_text("ℹ️ Veritabanında işlenecek kayıt bulunamadı.")
        _running = False
        return

    total = sum(c for _, _, c, _ in all_cols)
    prog  = Progress(total)

    last_update  = 0.0
    last_msg_txt = ""

    async def _push_status(current_title: str = "", force: bool = False):
        nonlocal last_update, last_msg_txt
        now = time.time()
        if not force and (now - last_update) < STATUS_INTERVAL:
            return
        txt = prog.build_msg(current_title)
        if txt == last_msg_txt:
            return
        last_msg_txt = txt
        last_update  = now
        try:
            await status_msg.edit_text(txt, parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass

    # İlk durum mesajını hemen gönder
    await _push_status("Taranıyor…", force=True)

    for col, media_type, col_count, _ in all_cols:
        if _cancel_event.is_set():
            break

        LOGGER.info("Koleksiyon: %s — %d kayıt", media_type, col_count)
        skip = 0

        while not _cancel_event.is_set():
            docs = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda s=skip: list(col.find({}).skip(s).limit(BATCH_SIZE))
            )
            if not docs:
                break

            for doc in docs:
                if _cancel_event.is_set():
                    break
                title = doc.get("title", "?")
                try:
                    changed = await _process(doc, col, media_type)
                    if changed:
                        prog.changed += 1
                    else:
                        prog.skipped += 1
                except Exception as e:
                    LOGGER.error("İşleme hatası [%s]: %s", title, e)
                    prog.errors += 1
                prog.done += 1
                await _push_status(title)

            skip += BATCH_SIZE

    # ── Son mesaj ─────────────────────────────────────────
    elapsed = time.time() - prog.start_time
    m, s    = divmod(int(elapsed), 60)
    h, m    = divmod(m, 60)
    dur     = f"{h}s {m}d {s}sn" if h else (f"{m}d {s}sn" if m else f"{s}sn")

    if _cancel_event.is_set():
        final = (
            "⛔ <b>İşlem İptal Edildi</b>\n\n"
            f"📊 Tamamlandı  : <b>%{(prog.done/prog.total*100) if prog.total else 0:.1f}</b>\n"
            f"✏️ Güncellenen : {prog.changed}\n"
            f"⏭ Atlanan     : {prog.skipped}\n"
            f"❌ Hata        : {prog.errors}\n"
            f"⏱ Geçen süre  : {dur}"
        )
    else:
        final = (
            "✅ <b>Güncelleme Tamamlandı!</b>\n\n"
            f"📊 Tamamlandı  : <b>%100</b>\n"
            f"✏️ Güncellenen : {prog.changed}\n"
            f"⏭ Değişmedi   : {prog.skipped}\n"
            f"❌ Hata        : {prog.errors}\n"
            f"⏱ Toplam süre : <b>{dur}</b>"
        )

    try:
        await status_msg.edit_text(final, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass

    _running = False


# ─────────────────────────────────────────────────────────────
# Komut handler'ları
# ─────────────────────────────────────────────────────────────
@Client.on_message(
    filters.command("eskiverileriyenile") & filters.private & CustomFilters.owner,
    group=10
)
async def cmd_eskiverileriyenile(client: Client, message: Message):
    global _running, _cancel_event

    if _running:
        await message.reply_text(
            "⚠️ Zaten bir güncelleme işlemi çalışıyor!\n"
            "Durdurmak için /iptal gönderin.",
            parse_mode=enums.ParseMode.HTML
        )
        return

    _running      = True
    _cancel_event = asyncio.Event()

    status_msg = await message.reply_text(
        "⏳ <b>Veri yenileme başlatılıyor…</b>\n\nVeritabanı taranıyor, lütfen bekleyin.",
        parse_mode=enums.ParseMode.HTML
    )

    asyncio.create_task(_main_task(status_msg))


@Client.on_message(
    filters.command("iptal") & filters.private & CustomFilters.owner,
    group=10
)
async def cmd_iptal(client: Client, message: Message):
    global _cancel_event, _running

    if not _running:
        await message.reply_text("ℹ️ Şu an çalışan bir işlem yok.")
        return

    _cancel_event.set()
    await message.reply_text(
        "⛔ <b>İptal isteği alındı.</b>\n"
        "Mevcut kayıt tamamlandıktan sonra durulacak…",
        parse_mode=enums.ParseMode.HTML
    )
