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

# ---------------- /ISTATISTIK ----------------
@Client.on_message(filters.command("istatistik") & filters.private & CustomFilters.owner)
async def istatistik(client: Client, message: Message):
    total_movies = movie_col.count_documents({})
    total_series = series_col.count_documents({})

    def count_links_qualities(collection, is_series=False):
        link_set = set()
        telegram_set = set()
        quality_count = defaultdict(lambda: {"Link": 0, "Telegram": 0})

        if is_series:
            for doc in collection.find({}, {"seasons.episodes.telegram": 1}):
                for season in doc.get("seasons", []):
                    for ep in season.get("episodes", []):
                        for t in ep.get("telegram", []):
                            _id = t.get("id", "")
                            q = t.get("quality", "Unknown")
                            if _id.startswith("http://") or _id.startswith("https://"):
                                if _id not in link_set:
                                    link_set.add(_id)
                                    quality_count[q]["Link"] += 1
                            else:
                                if _id not in telegram_set:
                                    telegram_set.add(_id)
                                    quality_count[q]["Telegram"] += 1
        else:
            for doc in collection.find({}, {"telegram": 1}):
                for t in doc.get("telegram", []):
                    _id = t.get("id", "")
                    q = t.get("quality", "Unknown")
                    if _id.startswith("http://") or _id.startswith("https://"):
                        if _id not in link_set:
                            link_set.add(_id)
                            quality_count[q]["Link"] += 1
                    else:
                        if _id not in telegram_set:
                            telegram_set.add(_id)
                            quality_count[q]["Telegram"] += 1
        return len(link_set), len(telegram_set), dict(quality_count)

    movie_link, movie_tg, movie_quality_counts = count_links_qualities(movie_col)
    series_link, series_tg, series_quality_counts = count_links_qualities(series_col, is_series=True)

    def format_quality_stats(q_dict):
        order = ["2160p", "1920p", "1440p", "1080p", "720p", "576p", "480p"]
        sorted_items = sorted(
            q_dict.items(),
            key=lambda x: (order.index(x[0]) if x[0] in order else len(order), x[0])
        )
        return "\n".join(
            f"   ┠ {q} → Link: {c['Link']} | Telegram: {c['Telegram']}"
            for q, c in sorted_items
        )

    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/")
    free_disk_gb = round(disk.free / (1024**3), 2)
    free_percent = disk.percent

    # -------- BOT UPTIME + YENİ GÖSTERİM KURALLARI --------
    uptime_seconds = int(time.time() - bot_start_time)
    days, rem = divmod(uptime_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    if days >= 1:
        uptime_str = f"{days}g{hours}s{minutes}d{seconds}s"
    elif hours >= 1:
        uptime_str = f"{hours}s{minutes}d{seconds}s"
    else:
        uptime_str = f"{minutes}d{seconds}s"
    # -----------------------------------------------------

    stats = db.command("dbstats")
    storage_mb = round(stats.get("storageSize", 0) / (1024 * 1024), 2)
    storage_percent = round((storage_mb / 512) * 100, 1)

    text = (
        f"⌬ <b>İstatistik</b>\n\n"
        f"┠ Filmler : {total_movies}\n"
        f"┃  ┠ Link     : {movie_link}\n"
        f"┃  ┖ Telegram : {movie_tg}\n"
        f"{format_quality_stats(movie_quality_counts)}\n\n"
        f"┠ Diziler : {total_series}\n"
        f"┃  ┠ Link     : {series_link}\n"
        f"┃  ┖ Telegram : {series_tg}\n"
        f"{format_quality_stats(series_quality_counts)}\n\n"
        f"┖ Depolama: {storage_mb} MB (%{storage_percent})\n\n"
        f"┟ CPU → {cpu}% | Boş → {free_disk_gb}GB [{free_percent}%]\n"
        f"┖ RAM → {ram}% | Süre → {uptime_str}"
    )

    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

# ---------- benzerleri sil ----------
@Client.on_message(filters.command("aynivideolarisil") & filters.private & CustomFilters.owner)
async def benzerleri_sil(client: Client, message: Message):
    status = await message.reply_text("🔍 Arşiv taranıyor, kayıt sayısı hesaplanıyor...")

    loop = asyncio.get_event_loop()

    # Toplam kayıt sayısını önceden çek (ilerleme % için)
    total_movie = await loop.run_in_executor(None, movie_col.count_documents, {})
    total_tv    = await loop.run_in_executor(None, series_col.count_documents, {})
    total_all   = total_movie + total_tv

    total_docs    = 0
    total_removed = 0
    processed     = 0
    log_lines     = []
    last_edit     = time.time()
    PROGRESS_INTERVAL = 15  # saniye

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
        (movie_col, "movie"),
        (series_col, "tv")
    ]

    for col, col_name in collections:
        col_label = "🎬 Filmler" if col_name == "movie" else "📺 Diziler"
        docs = await loop.run_in_executor(
            None,
            lambda c=col: list(c.find({}, {"telegram": 1, "seasons": 1, "title": 1, "tmdb_id": 1, "imdb_id": 1}))
        )

        for doc in docs:
            doc_updated = False
            processed += 1

            # ---------- FILM ----------
            if col_name == "movie" and "telegram" in doc:
                telegram = doc.get("telegram", [])
                grouped = {}

                for idx, t in enumerate(telegram):
                    key = (t.get("name"), t.get("size"))
                    if key not in grouped:
                        grouped[key] = []
                    grouped[key].append((idx, t))

                new_telegram = []

                for (name, size), items in grouped.items():
                    non_http_items = []
                    for i, t in items:
                        tid = str(t.get("id", "")).lower()
                        if not (tid.startswith("http://") or tid.startswith("https://")):
                            non_http_items.append((i, t))

                    if non_http_items:
                        keep_i, keep_t = max(non_http_items, key=lambda x: x[0])
                    else:
                        keep_i, keep_t = max(items, key=lambda x: x[0])

                    new_telegram.append(keep_t)

                    for i, t in items:
                        if t is not keep_t:
                            total_removed += 1
                            doc_updated = True
                            log_lines.append(
                                f"[Koleksiyon] movie\n"
                                f"ID: {doc.get('tmdb_id')}\n"
                                f"Başlık: {doc.get('title')}\n"
                                f"Name: {t.get('name')}\n"
                                f"Size: {t.get('size')}\n"
                                f"id: {t.get('id')}\n"
                                f"{'-'*50}"
                            )

                if doc_updated:
                    await loop.run_in_executor(
                        None,
                        lambda: col.update_one({"_id": doc["_id"]}, {"$set": {"telegram": new_telegram}})
                    )
                    total_docs += 1

            # ---------- DİZİ / BÖLÜM ----------
            if col_name == "tv":
                seasons = doc.get("seasons", [])

                for season in seasons:
                    season_no = season.get("season_number")
                    episodes = season.get("episodes", [])

                    for ep in episodes:
                        if "telegram" not in ep:
                            continue

                        telegram = ep.get("telegram", [])
                        grouped = {}

                        for idx, t in enumerate(telegram):
                            key = (t.get("name"), t.get("size"))
                            if key not in grouped:
                                grouped[key] = []
                            grouped[key].append((idx, t))

                        new_telegram = []

                        for (name, size), items in grouped.items():
                            non_http_items = []
                            for i, t in items:
                                tid = str(t.get("id", "")).lower()
                                if not (tid.startswith("http://") or tid.startswith("https://")):
                                    non_http_items.append((i, t))

                            if non_http_items:
                                keep_i, keep_t = max(non_http_items, key=lambda x: x[0])
                            else:
                                keep_i, keep_t = max(items, key=lambda x: x[0])

                            new_telegram.append(keep_t)

                            for i, t in items:
                                if t is not keep_t:
                                    total_removed += 1
                                    doc_updated = True
                                    log_lines.append(
                                        f"[Koleksiyon] tv\n"
                                        f"ID: {doc.get('imdb_id')}\n"
                                        f"Dizi: {doc.get('title')}\n"
                                        f"Sezon: {season_no} | Bölüm: {ep.get('episode_number')}\n"
                                        f"Name: {t.get('name')}\n"
                                        f"Size: {t.get('size')}\n"
                                        f"id: {t.get('id')}\n"
                                        f"{'-'*50}"
                                    )

                        if doc_updated:
                            ep["telegram"] = new_telegram

                if doc_updated:
                    await loop.run_in_executor(
                        None,
                        lambda d=doc, s=seasons: col.update_one({"_id": d["_id"]}, {"$set": {"seasons": s}})
                    )
                    total_docs += 1

            await maybe_update_progress(col_label)

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

