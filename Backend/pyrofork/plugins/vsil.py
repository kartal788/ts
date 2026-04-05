from pyrogram import Client, filters
from pyrogram.types import Message
from Backend.helper.custom_filter import CustomFilters
from pymongo import MongoClient
import os, re
from time import time

# ---------------- SADECE ENV'DEN DATABASE AL ----------------
DATABASE_URLS = os.getenv("DATABASE", "")
db_urls = [u.strip() for u in DATABASE_URLS.split(",") if u.strip()]


# ------------------------------------------------------------------
#  UNIVERSAL ID PARSE
# ------------------------------------------------------------------

def extract_id(raw):
    raw = raw.strip()
    stremio = re.search(r"/detail/(movie|series)/(\d+)-", raw)
    if stremio:
        tmdb = stremio.group(2)
        return ("tmdb", tmdb, f"tt{tmdb}")

    if raw.isdigit():
        return ("tmdb", raw, f"tt{raw}")

    if raw.lower().startswith("tt"):
        return ("imdb", raw, None)

    tg = re.search(r"/dl/([A-Za-z0-9]+)", raw)
    if tg:
        return ("telegram", tg.group(1), None)

    if len(raw) > 30 and raw.isalnum():
        return ("telegram", raw, None)

    return ("filename", raw, None)


# ------------------------------------------------------------------
#  DELETE ENGINE
# ------------------------------------------------------------------

def process_delete(db, id_type, val, imdb_fallback=None, test=False,
                   category="all", season=None, episodes=None):

    deleted = []

    def allow(cat):
        return category == "all" or category == cat

    # --------------- TMDB ----------------
    if id_type == "tmdb":
        tmdb_id = int(val)
        movie_docs = list(db["movie"].find({"tmdb_id": tmdb_id})) if allow("movie") else []
        tv_docs = list(db["tv"].find({"tmdb_id": tmdb_id})) if allow("tv") else []

        if not movie_docs and not tv_docs and imdb_fallback:
            return process_delete(db, "imdb", imdb_fallback, None,
                                  test, category, season, episodes)

        # MOVIE
        for doc in movie_docs:
            for t in doc.get("telegram", []):
                deleted.append(t.get("name"))
            if not test:
                db["movie"].delete_one({"_id": doc["_id"]})

        # TV
        for doc in tv_docs:
            if season:
                for s in doc.get("seasons", []):
                    if s.get("season_number") == season:
                        remove_eps = []
                        for ep in s.get("episodes", []):
                            if episodes:
                                if ep.get("episode_number") in episodes:
                                    for t in ep.get("telegram", []):
                                        deleted.append(t.get("name"))
                                    if not test:
                                        s["episodes"].remove(ep)
                            else:
                                for t in ep.get("telegram", []):
                                    deleted.append(t.get("name"))
                                if not test:
                                    remove_eps.append(ep)

                        for ep in remove_eps:
                            s["episodes"].remove(ep)

                if not test:
                    doc_seasons = [s for s in doc.get("seasons", []) if s.get("episodes")]
                    if not doc_seasons:
                        db["tv"].delete_one({"_id": doc["_id"]})
                    else:
                        doc["seasons"] = doc_seasons
                        db["tv"].replace_one({"_id": doc["_id"]}, doc)

            else:
                for s in doc.get("seasons", []):
                    for e in s.get("episodes", []):
                        for t in e.get("telegram", []):
                            deleted.append(t.get("name"))
                if not test:
                    db["tv"].delete_one({"_id": doc["_id"]})

        return deleted

    # --------------- IMDb ----------------
    if id_type == "imdb":
        imdb_id = val

        movie_docs = list(db["movie"].find({"imdb_id": imdb_id})) if allow("movie") else []
        tv_docs = list(db["tv"].find({"imdb_id": imdb_id})) if allow("tv") else []

        for doc in movie_docs:
            for t in doc.get("telegram", []):
                deleted.append(t.get("name"))
            if not test:
                db["movie"].delete_one({"_id": doc["_id"]})

        for doc in tv_docs:
            for s in doc.get("seasons", []):
                for e in s.get("episodes", []):
                    for t in e.get("telegram", []):
                        deleted.append(t.get("name"))
            if not test:
                db["tv"].delete_one({"_id": doc["_id"]})

        return deleted

    # --------------- TELEGRAM / FILENAME ----------------
    target = val

    if allow("movie"):
        for doc in list(db["movie"].find({})):
            old = doc.get("telegram", [])
            new = [t for t in old if t.get("id") != target and t.get("name") != target]
            removed = [t.get("name") for t in old if t not in new]
            deleted.extend(removed)
            if removed and not test:
                if not new:
                    db["movie"].delete_one({"_id": doc["_id"]})
                else:
                    doc["telegram"] = new
                    db["movie"].replace_one({"_id": doc["_id"]}, doc)

    if allow("tv"):
        for doc in list(db["tv"].find({})):
            changed = False
            remove_seasons = []

            for s in doc.get("seasons", []):
                if season and s.get("season_number") != season:
                    continue

                remove_eps = []
                for ep in s.get("episodes", []):
                    if episodes and ep.get("episode_number") not in episodes:
                        continue
                    for t in ep.get("telegram", []):
                        deleted.append(t.get("name"))
                    if not test:
                        remove_eps.append(ep)
                        changed = True

                for e in remove_eps:
                    s["episodes"].remove(e)

                if not s["episodes"]:
                    remove_seasons.append(s)

            for s in remove_seasons:
                doc["seasons"].remove(s)

            if changed and not test:
                if not doc["seasons"]:
                    db["tv"].delete_one({"_id": doc["_id"]})
                else:
                    db["tv"].replace_one({"_id": doc["_id"]}, doc)

    return deleted


# ------------------------------------------------------------------
#  FORMATTED OUTPUT
# ------------------------------------------------------------------

async def send_output(message, data, prefix, is_tv=False, is_test=False):
    if not data:
        return await message.reply_text("⚠️ Dosya bulunamadı.")

    title = "Silinecek Diziler:" if (is_tv and is_test) else \
            "Silinen Diziler:" if is_tv else \
            "Silinecek Filmler:" if is_test else "Silinen Filmler:"

    numbered = "\n".join([f"{i+1}) {name}" for i, name in enumerate(data)])
    text = f"{title}\n{numbered}"

    if len(data) > 10:
        path = f"/tmp/{prefix}_{int(time())}.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        await message.reply_document(path, caption=f"{len(data)} dosya listelendi.")
    else:
        await message.reply_text(text)


# ------------------------------------------------------------------
#  /dizisil
# ------------------------------------------------------------------

@Client.on_message(filters.command("dizisil") & filters.private & CustomFilters.owner)
async def dizisil(client, message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım:\n/dizisil id\n/dizisil id s3\n/dizisil id s3e5e6")

    mongo = MongoClient(db_urls[1])
    db = mongo[mongo.list_database_names()[0]]

    idt, val, fb = extract_id(message.command[1])

    season = None
    episodes = None

    if len(message.command) >= 3:
        txt = message.command[2].lower()
        s = re.match(r"s(\d+)((?:e\d+)*)", txt)
        if s:
            season = int(s.group(1))
            eps_raw = s.group(2)
            if eps_raw:
                episodes = [int(x[1:]) for x in re.findall(r"e\d+", eps_raw)]

    data = process_delete(db, idt, val, fb, test=False,
                          category="tv", season=season, episodes=episodes)

    await send_output(message, data, "dizisil", is_tv=True, is_test=False)


# ------------------------------------------------------------------
#  /dizisiltest
# ------------------------------------------------------------------

@Client.on_message(filters.command("dizisiltest") & filters.private & CustomFilters.owner)
async def dizisiltest(client, message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım:\n/dizisiltest id\n/dizisiltest id s3\n/dizisiltest id s3e5e6")

    mongo = MongoClient(db_urls[1])
    db = mongo[mongo.list_database_names()[0]]

    idt, val, fb = extract_id(message.command[1])

    season = None
    episodes = None

    if len(message.command) >= 3:
        txt = message.command[2].lower()
        s = re.match(r"s(\d+)((?:e\d+)*)", txt)
        if s:
            season = int(s.group(1))
            eps_raw = s.group(2)
            if eps_raw:
                episodes = [int(x[1:]) for x in re.findall(r"e\d+", eps_raw)]

    data = process_delete(db, idt, val, fb, test=True,
                          category="tv", season=season, episodes=episodes)

    await send_output(message, data, "dizisiltest", is_tv=True, is_test=True)


# ------------------------------------------------------------------
#  /filmsil
# ------------------------------------------------------------------

@Client.on_message(filters.command("filmsil") & filters.private & CustomFilters.owner)
async def filmsil(client, message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: /filmsil id")

    mongo = MongoClient(db_urls[1])
    db = mongo[mongo.list_database_names()[0]]

    idt, val, fb = extract_id(message.command[1])

    data = process_delete(db, idt, val, fb, test=False, category="movie")

    await send_output(message, data, "filmsil", is_tv=False, is_test=False)


# ------------------------------------------------------------------
#  /filmsiltest
# ------------------------------------------------------------------

@Client.on_message(filters.command("filmsiltest") & filters.private & CustomFilters.owner)
async def filmsiltest(client, message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: /filmsiltest id")

    mongo = MongoClient(db_urls[1])
    db = mongo[mongo.list_database_names()[0]]

    idt, val, fb = extract_id(message.command[1])

    data = process_delete(db, idt, val, fb, test=True, category="movie")

    await send_output(message, data, "filmsiltest", is_tv=False, is_test=True)
