from pyrogram import Client, filters
from pyrogram.types import Message
from Backend.helper.custom_filter import CustomFilters
from pymongo import MongoClient
import asyncio
import os
import json
from time import time

flood_wait = 30  # saniye
last_command_time = {}  # kullanıcı_id : zaman

# ---------------- SADECE ENV'DEN DATABASE AL ----------------
DATABASE_URLS = os.getenv("DATABASE", "")
db_urls = [u.strip() for u in DATABASE_URLS.split(",") if u.strip()]

# ---------------- Koleksiyonları JSON'a Çekme ----------------
def export_collections_to_json(url):
    # serverSelectionTimeoutMS: bağlantı kurulamazsa sonsuza kadar beklemesin,
    # en fazla 10 saniye sonra hata versin.
    client = MongoClient(url, serverSelectionTimeoutMS=10000)
    try:
        db_name_list = client.list_database_names()
        if not db_name_list:
            return None

        db = client[db_name_list[0]]

        movie_data = list(db["movie"].find({}, {"_id": 0}))
        tv_data = list(db["tv"].find({}, {"_id": 0}))

        return {"movie": movie_data, "tv": tv_data}
    finally:
        client.close()

# ---------------- /vindir Komutu ----------------
@Client.on_message(filters.command("vindir") & filters.private & CustomFilters.owner)
async def download_collections(client: Client, message: Message):
    user_id = message.from_user.id
    now = time()

    # Flood kontrolü
    if user_id in last_command_time and now - last_command_time[user_id] < flood_wait:
        await message.reply_text(f"⚠️ Lütfen {flood_wait} saniye bekleyin.", quote=True)
        return
    last_command_time[user_id] = now

    try:
        if not db_urls or len(db_urls) < 2:
            await message.reply_text("⚠️ İkinci veritabanı bulunamadı.")
            return

        # export_collections_to_json senkron (bloklayan) bir pymongo çağrısı.
        # Doğrudan await'siz çağrılırsa, bot tek bir asyncio event loop
        # üzerinde çalıştığından bu tüm loop'u bloke eder ve /vindir
        # tamamlanana kadar bot HİÇBİR kullanıcıya cevap veremez hale gelir.
        # Bu yüzden ayrı bir thread'de çalıştırılır.
        combined_data = await asyncio.to_thread(export_collections_to_json, db_urls[1])
        if combined_data is None:
            await message.reply_text("⚠️ Koleksiyonlar boş veya bulunamadı.")
            return

        file_path = "/tmp/dizi_ve_film_veritabanı.json"

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(combined_data, f, ensure_ascii=False, indent=2, default=str)

        await client.send_document(
            chat_id=message.chat.id,
            document=file_path,
            caption="📁 Film ve Dizi Koleksiyonları"
        )

    except Exception as e:
        await message.reply_text(f"⚠️ Hata: {e}")
        print("vindir hata:", e)
