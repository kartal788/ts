import os
import psutil
import re
import aiohttp
import time
import asyncio  # <--- BU EKSİKTİ, EKLENDİ
from datetime import timezone, datetime
from dateutil.parser import parse as parse_date
from Backend.helper.metadata import tmdb
from pyrogram.errors import FloodWait

from pyrogram import Client, filters
from pyrogram.types import Message

from motor.motor_asyncio import AsyncIOMotorClient
from Backend.helper.custom_filter import CustomFilters
from Backend.helper.metadata import metadata
from Backend.logger import LOGGER
from aiofiles import open as aiopen


# ----------------- ENV -----------------
DATABASE_RAW = os.getenv("DATABASE", "")
db_urls = [u.strip() for u in DATABASE_RAW.split(",") if u.strip().startswith("mongodb")]
MONGO_URL = db_urls[1]
DB_NAME = "dbFyvio"

# ----------------- MongoDB -----------------
mongo = AsyncIOMotorClient(MONGO_URL)
db = mongo[DB_NAME]
movie_col = db["movie"]
series_col = db["tv"]


# ----------------- /SİL -----------------
awaiting_confirmation = {}

@Client.on_message(filters.command("sil") & filters.private & CustomFilters.owner)
async def sil(client: Client, message: Message):
    uid = message.from_user.id

    movie_count = await movie_col.count_documents({})
    tv_count = await series_col.count_documents({})

    if movie_count == 0 and tv_count == 0:
        return await message.reply_text("ℹ️ Veritabanı zaten boş.")

    awaiting_confirmation[uid] = True

    await message.reply_text(
        "⚠️ TÜM VERİLER SİLİNECEK ⚠️\n\n"
        f"🎬 Filmler: {movie_count}\n"
        f"📺 Diziler: {tv_count}\n\n"
        "Onaylamak için **Evet** yaz.\n"
        "İptal için **Hayır** yaz."
    )

@Client.on_message(filters.private & CustomFilters.owner & filters.regex("(?i)^(evet|hayır)$") & ~filters.command(""), group=1)
async def sil_onay(client: Client, message: Message):
    uid = message.from_user.id

    if uid not in awaiting_confirmation:
        return

    awaiting_confirmation.pop(uid)

    if message.text.lower() == "evet":
        m = await movie_col.count_documents({})
        t = await series_col.count_documents({})
        await movie_col.delete_many({})
        await series_col.delete_many({})
        await message.reply_text(
            f"✅ Silme tamamlandı\n🎬 {m} film\n📺 {t} dizi"
        )
    else:
        await message.reply_text("❌ Silme iptal edildi.")

# ------------------calismayanlinklerisil------------------
@Client.on_message(filters.command("calismayanlinklerisil") & filters.private & CustomFilters.owner)
async def calismayan_linkleri_sil(client: Client, message: Message):

    status = await message.reply_text("🔍 Linkler kontrol ediliyor...\n\n⏳ Bu işlem uzun sürebilir, lütfen bekleyin.")

    async def link_calismiyor_mu(url: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.head(url, allow_redirects=True) as r:
                    size = r.headers.get("Content-Length")
                    if not size:
                        return True
                    return int(size) < (5 * 1024 * 1024)
        except:
            return True

    silinen_film = 0
    silinen_dizi = 0
    silinen_bolum = 0
    silinen_link = 0

    silinen_isimler = []

    # ---------------- MOVIES ----------------
    toplam_film = await movie_col.count_documents({})
    islenen_film = 0
    son_guncelleme = asyncio.get_event_loop().time()

    async for movie in movie_col.find({}):
        telegramlar = movie.get("telegram", [])
        yeni_telegram = []

        for t in telegramlar:
            if await link_calismiyor_mu(t.get("id", "")):
                silinen_link += 1
                silinen_isimler.append(f"🎬 {t.get('name')}")
            else:
                yeni_telegram.append(t)

        if not yeni_telegram:
            await movie_col.delete_one({"_id": movie["_id"]})
            silinen_film += 1
        elif len(yeni_telegram) != len(telegramlar):
            await movie_col.update_one(
                {"_id": movie["_id"]},
                {"$set": {"telegram": yeni_telegram}}
            )

        islenen_film += 1
        simdi = asyncio.get_event_loop().time()
        if simdi - son_guncelleme >= 15:
            son_guncelleme = simdi
            try:
                await status.edit_text(
                    f"🔍 Filmler kontrol ediliyor...\n"
                    f"📊 {islenen_film}/{toplam_film} film işlendi\n"
                    f"🗑 Silinen link: {silinen_link}\n"
                    f"⏳ Devam ediyor..."
                )
            except Exception:
                pass

    # ---------------- TV ----------------
    toplam_dizi = await series_col.count_documents({})
    islenen_dizi = 0
    son_guncelleme = asyncio.get_event_loop().time()

    async for tv in series_col.find({}):
        sezonlar = []
        dizi_bos = True

        for season in tv.get("seasons", []):
            bolumler = []

            for ep in season.get("episodes", []):
                telegramlar = ep.get("telegram", [])
                yeni_telegram = []

                for t in telegramlar:
                    if await link_calismiyor_mu(t.get("id", "")):
                        silinen_link += 1
                        silinen_isimler.append(f"📺 {t.get('name')}")
                    else:
                        yeni_telegram.append(t)

                if yeni_telegram:
                    ep["telegram"] = yeni_telegram
                    bolumler.append(ep)
                    dizi_bos = False
                else:
                    silinen_bolum += 1

            if bolumler:
                season["episodes"] = bolumler
                sezonlar.append(season)

        if dizi_bos:
            await series_col.delete_one({"_id": tv["_id"]})
            silinen_dizi += 1
        else:
            await series_col.update_one(
                {"_id": tv["_id"]},
                {"$set": {"seasons": sezonlar}}
            )

        islenen_dizi += 1
        simdi = asyncio.get_event_loop().time()
        if simdi - son_guncelleme >= 15:
            son_guncelleme = simdi
            try:
                await status.edit_text(
                    f"🔍 Diziler kontrol ediliyor...\n"
                    f"📊 {islenen_dizi}/{toplam_dizi} dizi işlendi\n"
                    f"🗑 Silinen link: {silinen_link}\n"
                    f"⏳ Devam ediyor..."
                )
            except Exception:
                pass

    # ---------------- SONUÇ ----------------
    header = (
        "✅ Temizlik tamamlandı\n\n"
        f"🔗 Silinen link: {silinen_link}\n"
    )

    if len(silinen_isimler) <= 15:
        detay = "\n".join(silinen_isimler)
        await status.edit_text(header + detay)
    else:
        txt_path = "/tmp/silinen_linkler.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(silinen_isimler))

        await client.send_document(
            chat_id=message.chat.id,
            document=txt_path,
            caption=header + "\n📄 Silinen içerik listesi dosya olarak gönderildi."
        )
        await status.delete()

def format_time(seconds):
    """Saniyeyi HH:MM:SS formatına çevirir."""
    return time.strftime("%H:%M:%S", time.gmtime(seconds))
