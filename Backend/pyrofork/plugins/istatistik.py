import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pymongo import MongoClient, UpdateOne
from collections import defaultdict
import psutil
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from deep_translator import GoogleTranslator
from Backend.helper.metadata import metadata
from Backend.helper.custom_filter import CustomFilters
from Backend.config import Telegram

import os

# ---------------- CONFIG ----------------
stop_event = asyncio.Event()
DOWNLOAD_DIR = "/"

# ---------------- DATABASE ----------------
db_raw = os.getenv("DATABASE", "")
if not db_raw:
    raise Exception("DATABASE ortam değişkeni bulunamadı!")

db_urls = [u.strip() for u in db_raw.split(",") if u.strip()]
MONGO_URL = db_urls[1] if len(db_urls) > 1 else db_urls[0]

client_db = MongoClient(MONGO_URL)
db_name = client_db.list_database_names()[0]
db = client_db[db_name]
movie_col = db["movie"]
series_col = db["tv"]

bot_start_time = time.time()


# ---------- benzerleri sil ----------
def _dedup_telegram(telegram: list) -> tuple[list, int]:
    """
    Verilen telegram listesinden (name, size) bazlı tekrarları kaldırır.
    Her grupta HTTP olmayan (gerçek TG file_id) olanı tercih eder, yoksa
    en son eklenenı tutar.
    Döndürür: (yeni_liste, silinen_sayisi)
    """
    grouped: dict = {}
    for idx, t in enumerate(telegram):
        key = (t.get("name"), t.get("size"))
        grouped.setdefault(key, []).append((idx, t))

    new_telegram = []
    removed = 0
    for items in grouped.values():
        non_http = [
            (i, t) for i, t in items
            if not str(t.get("id", "")).lower().startswith(("http://", "https://"))
        ]
        keep_i, keep_t = max(non_http if non_http else items, key=lambda x: x[0])
        new_telegram.append(keep_t)
        removed += len(items) - 1
    return new_telegram, removed


def _process_movie_doc(doc: dict) -> tuple[bool, list, int, list]:
    """
    Bir film dökümanını işler (sync — executor içinde çalışır).
    Döndürür: (degisti, new_telegram, silinen_sayi, log_satirlari)
    """
    telegram = doc.get("telegram", [])
    if len(telegram) <= 1:
        return False, telegram, 0, []

    new_telegram, removed = _dedup_telegram(telegram)
    if removed == 0:
        return False, telegram, 0, []

    logs = []
    original_ids = {id(t) for t in new_telegram}
    for t in telegram:
        if id(t) not in original_ids and t not in new_telegram:
            logs.append(
                f"[Koleksiyon] movie\n"
                f"ID: {doc.get('tmdb_id')}\n"
                f"Başlık: {doc.get('title')}\n"
                f"Name: {t.get('name')}\n"
                f"Size: {t.get('size')}\n"
                f"id: {t.get('id')}\n"
                f"{'-'*50}"
            )
    # log sayısını removed ile eşitle (id karşılaştırması yetersiz kalabilir)
    return True, new_telegram, removed, logs


def _process_tv_doc(doc: dict) -> tuple[bool, list, int, list]:
    """
    Bir dizi dökümanını işler (sync — executor içinde çalışır).
    Döndürür: (degisti, new_seasons, silinen_sayi, log_satirlari)
    """
    seasons = doc.get("seasons", [])
    total_removed = 0
    logs = []
    doc_changed = False

    for season in seasons:
        season_no = season.get("season_number")
        for ep in season.get("episodes", []):
            telegram = ep.get("telegram", [])
            if len(telegram) <= 1:
                continue
            new_telegram, removed = _dedup_telegram(telegram)
            if removed == 0:
                continue
            doc_changed = True
            total_removed += removed
            ep["telegram"] = new_telegram
            for t in telegram:
                if t not in new_telegram:
                    logs.append(
                        f"[Koleksiyon] tv\n"
                        f"ID: {doc.get('imdb_id')}\n"
                        f"Dizi: {doc.get('title')}\n"
                        f"Sezon: {season_no} | Bölüm: {ep.get('episode_number')}\n"
                        f"Name: {t.get('name')}\n"
                        f"Size: {t.get('size')}\n"
                        f"id: {t.get('id')}\n"
                        f"{'-'*50}"
                    )

    return doc_changed, seasons, total_removed, logs


@Client.on_message(filters.command("aynivideolarisil") & filters.private & CustomFilters.owner)
async def benzerleri_sil(client: Client, message: Message):
    status = await message.reply_text("🔍 Arşiv taranıyor, kayıt sayısı hesaplanıyor...")

    loop = asyncio.get_event_loop()
    PROGRESS_INTERVAL = 5   # saniye — daha sık güncelle
    BATCH_SIZE        = 100  # bulk_write için batch büyüklüğü

    # Toplam kayıt sayısını önceden çek (ilerleme % için)
    total_movie = await loop.run_in_executor(None, movie_col.count_documents, {})
    total_tv    = await loop.run_in_executor(None, series_col.count_documents, {})
    total_all   = total_movie + total_tv

    total_docs    = 0
    total_removed = 0
    processed     = 0
    log_lines     = []
    last_edit     = time.time()

    def progress_bar(pct: float, width: int = 16) -> str:
        filled = int(width * pct / 100)
        return "█" * filled + "░" * (width - filled)

    async def maybe_update_progress(col_label: str):
        nonlocal last_edit
        now = time.time()
        if now - last_edit >= PROGRESS_INTERVAL:
            last_edit = now
            pct = (processed / total_all * 100) if total_all else 0
            bar = progress_bar(pct)
            try:
                await status.edit_text(
                    f"⏳ İşleniyor... [{bar}] {pct:.1f}%\n\n"
                    f"📂 Şu an: {col_label}\n"
                    f"🔢 İşlenen: {processed:,} / {total_all:,}\n"
                    f"📄 Güncellenen kayıt: {total_docs:,}\n"
                    f"🗑️ Silinen video: {total_removed:,}"
                )
            except Exception:
                pass

    collections = [
        (movie_col, "movie", "🎬 Filmler"),
        (series_col, "tv",   "📺 Diziler"),
    ]

    for col, col_name, col_label in collections:
        # Tüm dökümanları RAM'e almak yerine cursor ile ilerle
        # batch_size=200 → MongoDB driver her seferinde 200 döküman getirir
        def _get_cursor(c=col):
            return c.find(
                {},
                {"telegram": 1, "seasons": 1, "title": 1, "tmdb_id": 1, "imdb_id": 1}
            ).batch_size(200)

        cursor = await loop.run_in_executor(None, _get_cursor)

        bulk_ops   = []

        while True:
            # Bir sonraki dökümanı executor'da çek (sync MongoDB driver)
            doc = await loop.run_in_executor(None, lambda c=cursor: next(c, None))
            if doc is None:
                break

            processed += 1

            if col_name == "movie":
                changed, new_telegram, removed, logs = await loop.run_in_executor(
                    None, _process_movie_doc, doc
                )
                if changed:
                    bulk_ops.append(
                        UpdateOne({"_id": doc["_id"]}, {"$set": {"telegram": new_telegram}})
                    )
                    total_docs    += 1
                    total_removed += removed
                    log_lines.extend(logs)

            else:  # tv
                changed, new_seasons, removed, logs = await loop.run_in_executor(
                    None, _process_tv_doc, doc
                )
                if changed:
                    bulk_ops.append(
                        UpdateOne({"_id": doc["_id"]}, {"$set": {"seasons": new_seasons}})
                    )
                    total_docs    += 1
                    total_removed += removed
                    log_lines.extend(logs)

            # Toplu yazma (belleği boşalt)
            if len(bulk_ops) >= BATCH_SIZE:
                ops_to_write = bulk_ops[:]
                bulk_ops.clear()
                await loop.run_in_executor(
                    None, lambda ops=ops_to_write: col.bulk_write(ops, ordered=False)
                )

            await maybe_update_progress(col_label)

        # Koleksiyon sonu — kalan işlemleri yaz
        if bulk_ops:
            await loop.run_in_executor(
                None, lambda ops=bulk_ops[:]: col.bulk_write(ops, ordered=False)
            )
            bulk_ops.clear()

    # ---------- LOG DOSYASI ----------
    if log_lines:
        log_path = "silinenler.txt"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))

        await client.send_document(
            chat_id=Telegram.OWNER_ID,
            document=log_path,
            caption="🗑️ Silinen videolar"
        )

    await status.edit_text(
        f"✅ İşlem tamamlandı\n\n"
        f"📄 Etkilenen kayıt: {total_docs:,}\n"
        f"🗑️ Silinen videolar: {total_removed:,}"
    )


# ---------- katalog yenile ----------
@Client.on_message(filters.command("katalogyenile") & filters.private & CustomFilters.owner)
async def katalog_yenile(client: Client, message: Message):
    import aiohttp
    from Backend.config import Telegram

    status = await message.reply_text("🔄 Platform kataloğu yenileniyor...")

    url = f"{Telegram.BASE_URL}/stremio/internal/platform-catalog/refresh"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    stats = data.get("stats", {})
                    etiketler = {
                        "netflix": "Netflix", "disney": "Disney+", "amazon": "Amazon Prime",
                        "hbo": "HBO Max", "bein": "Bein/TOD", "exxen": "Exxen", "gain": "Gain",
                        "apple": "Apple TV+", "tabii": "Tabii", "tvplus": "TV+",
                        "collections": "🎬 Seri Filmler"
                    }
                    satırlar = "\n".join(f"  • {etiketler.get(k, k)}: {v:,}" for k, v in stats.items())
                    await status.edit_text(
                        f"✅ Katalog yenilendi\n\n"
                        f"📊 İçerik sayıları:\n{satırlar}"
                    )
                else:
                    await status.edit_text(f"❌ Sunucu hatası: HTTP {resp.status}")
    except asyncio.TimeoutError:
        await status.edit_text("❌ Zaman aşımı — katalog yenileme 120 saniyeyi geçti.")
    except Exception as e:
        LOGGER.exception(f"katalogyenile hatası: {e}")
        await status.edit_text(f"❌ Hata oluştu:\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)


# ---------- linkleri sil ----------
@Client.on_message(filters.command("linklerisil") & filters.private & CustomFilters.owner)
async def linklerisil(client: Client, message: Message):
    status = await message.reply_text("🔄 Link kayıtları temizleniyor...")
    total_removed = 0
    total_docs = 0

    def is_valid_id(tid):
        return not (str(tid).startswith("http://") or str(tid).startswith("https://"))

    # ---------- FILMLER ----------
    for doc in movie_col.find({}, {"_id": 1, "telegram": 1, "title": 1, "tmdb_id":1}):
        telegram = doc.get("telegram", [])
        new_telegram = [t for t in telegram if is_valid_id(t.get("id", ""))]
        removed_count = len(telegram) - len(new_telegram)
        if removed_count > 0:
            total_removed += removed_count
            if new_telegram:
                movie_col.update_one({"_id": doc["_id"]}, {"$set": {"telegram": new_telegram}})
            else:
                movie_col.delete_one({"_id": doc["_id"]})
            total_docs += 1

    # ---------- DİZİLER ----------
    for doc in series_col.find({}, {"_id": 1, "seasons": 1, "title":1, "imdb_id":1}):
        seasons = doc.get("seasons", [])
        doc_updated = False
        for season in seasons:
            episodes = season.get("episodes", [])
            new_episodes = []
            for ep in episodes:
                telegram = ep.get("telegram", [])
                new_telegram = [t for t in telegram if is_valid_id(t.get("id", ""))]
                removed_count = len(telegram) - len(new_telegram)
                if removed_count > 0:
                    total_removed += removed_count
                if new_telegram:
                    ep["telegram"] = new_telegram
                    new_episodes.append(ep)
                else:
                    doc_updated = True  # bölüm silindi
            season["episodes"] = new_episodes
        # Sezonlar güncellendikten sonra hiçbir bölüm kalmamışsa dizi silinecek
        remaining_eps = sum(len(s.get("episodes", [])) for s in seasons)
        if remaining_eps > 0:
            series_col.update_one({"_id": doc["_id"]}, {"$set": {"seasons": seasons}})
            if doc_updated:
                total_docs += 1
        else:
            series_col.delete_one({"_id": doc["_id"]})
            total_docs += 1

    await status.edit_text(f"✅ İşlem tamamlandı\n\n📄 Etkilenen kayıt: {total_docs}\n🗑️ Silinen tekrar: {total_removed}")

@Client.on_message(filters.command("durdur") & filters.private & CustomFilters.owner)
async def durdur_komutu(client: Client, message: Message):
    global is_running
    if is_running:
        is_running = False
        await message.reply_text("⛔ İşlem durduruluyor... Lütfen bekleyin.")
    else:
        await message.reply_text("⚠️ Şu an çalışan bir işlem yok.")

# ---------------- /TURKCESIL KOMUTU ----------------
@Client.on_message(filters.command("turkcesil") & filters.private & CustomFilters.owner)
async def turkcesil_komutu(client: Client, message: Message):
    """
    Veritabanındaki tüm Türkçe (tr) ve Almanca (de) dillerine ait alanları,
    sertifika bilgilerini ve koleksiyon kimliklerini temizler.
    """
    status = await message.reply("🧹 Veritabanı temizleme işlemi başlatılıyor... Lütfen bekleyiniz.")
    
    # 1. TEMEL ALANLAR (Movie ve TV koleksiyonları için ortak ana alanlar)
    fields_to_remove = {
        "title_tr": "",
        "title_de": "",
        "genres_tr": "",
        "genres_de": "",
        "description_tr": "",
        "description_de": "",
        "poster_tr": "",
        "poster_de": "",
        "backdrop_tr": "",
        "backdrop_de": "",
        "logo_tr": "",
        "logo_de": "",
        "collection_id": "",
        "certification_tr": "",
        "certification_de": "",
        "certification_us": "",
        "overview_tr": "",
        "overview_de": ""
    }

    # 2. DİZİ BÖLÜMLERİ İÇİNDEKİ ALANLAR (Seasons -> Episodes altındaki veriler)
    # MongoDB $[ ] operatörü listedeki tüm elemanlara uygulanmasını sağlar.
    nested_fields_to_remove = {
        "seasons.$[].episodes.$[].title_tr": "",
        "seasons.$[].episodes.$[].title_de": "",
        "seasons.$[].episodes.$[].overview_tr": "",
        "seasons.$[].episodes.$[].overview_de": ""
    }

    try:
        # FİLMLERİ GÜNCELLE
        # movie_col: istatistik.py içinde tanımlı olan film koleksiyonu
        movie_result = movie_col.update_many(
            {}, 
            {"$unset": fields_to_remove}
        )
        
        # DİZİLERİ GÜNCELLE (Ana Dokümanlar)
        # series_col: istatistik.py içinde tanımlı olan dizi koleksiyonu
        tv_main_result = series_col.update_many(
            {}, 
            {"$unset": fields_to_remove}
        )
        
        # DİZİ BÖLÜMLERİNİ GÜNCELLE (İç içe geçmiş listeler)
        tv_nested_result = series_col.update_many(
            {}, 
            {"$unset": nested_fields_to_remove}
        )

        # Sonuç metnini hazırla
        basari_mesaji = (
            "✅ **Temizleme İşlemi Başarıyla Tamamlandı!**\n\n"
            f"🎬 **Filmler:** {movie_result.modified_count} adet film güncellendi.\n"
            f"📺 **Diziler:** {tv_main_result.modified_count} adet dizi ana verisi ve "
            f"alt bölümleri temizlendi.\n\n"
            "**Silinen Alanlar:**\n"
            "• Tüm `_tr` ve `_de` (başlık, tür, açıklama, poster, logo)\n"
            "• Tüm `certification_` (TR, DE, US)\n"
            "• `collection_id` ve `overview_` alanları."
        )

        await status.edit_text(basari_mesaji)

    except Exception as e:
        # Hata durumunda loglama ve kullanıcıya bildirme
        print(f"Hata: {e}")
        await status.edit_text(f"❌ **İşlem sırasında bir hata oluştu:**\n`{str(e)}`")

# ---------------------------------------------------

