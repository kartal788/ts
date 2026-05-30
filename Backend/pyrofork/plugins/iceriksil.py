"""
/iceriksil  <anahtar>  — veritabanındaki telegram[].name alanında
                          anahtar geçen videoları siler.
/iceriksiltest <anahtar> — silme yapmadan listeler.
"""

import asyncio
import os
from time import time

from pyrogram import Client, filters
from pyrogram.types import Message
from pymongo import MongoClient

from Backend.helper.custom_filter import CustomFilters

DATABASE_RAW = os.getenv("DATABASE", "")
db_urls = [u.strip() for u in DATABASE_RAW.split(",") if u.strip().startswith("mongodb")]
MONGO_URL = db_urls[1]


def _get_db():
    mongo = MongoClient(MONGO_URL)
    db_name = mongo.list_database_names()[0]
    return mongo[db_name]


def _count(col_name: str) -> int:
    return _get_db()[col_name].count_documents({})


def _next_doc(cursor):
    """Cursor'dan bir sonraki dökümanı döndürür, bitmişse None."""
    return next(cursor, None)


def _get_cursor(col_name: str, projection: dict):
    return _get_db()[col_name].find({}, projection).batch_size(100)


def _do_movie_update(db_ref, movie_id, remaining):
    if remaining:
        db_ref["movie"].update_one({"_id": movie_id}, {"$set": {"telegram": remaining}})
    else:
        db_ref["movie"].delete_one({"_id": movie_id})


def _do_tv_update(db_ref, tv_id, new_seasons):
    if new_seasons:
        db_ref["tv"].update_one({"_id": tv_id}, {"$set": {"seasons": new_seasons}})
    else:
        db_ref["tv"].delete_one({"_id": tv_id})


def _progress_bar(pct: float, width: int = 14) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


async def _run(client: Client, message: Message, keyword: str, test: bool):
    loop = asyncio.get_event_loop()
    mode = "test" if test else "silme"
    kw = keyword.lower()

    status = await message.reply_text(f"🔍 Kayıt sayısı hesaplanıyor…")

    # Toplam sayıları al
    total_movies = await loop.run_in_executor(None, _count, "movie")
    total_tv     = await loop.run_in_executor(None, _count, "tv")
    total_all    = total_movies + total_tv

    films  = []
    series = []
    processed = 0
    last_edit = 0.0
    INTERVAL  = 4  # saniye

    async def update_progress(phase: str):
        nonlocal last_edit
        now = time()
        if now - last_edit < INTERVAL:
            return
        last_edit = now
        pct = (processed / total_all * 100) if total_all else 0
        bar = _progress_bar(pct)
        hits = len(films) + len(series)
        try:
            await status.edit_text(
                f"⏳ [{bar}] {pct:.0f}%\n\n"
                f"📂 Şu an: {phase}\n"
                f"🔢 İşlenen: {processed:,} / {total_all:,}\n"
                f"🎯 Eşleşen: {hits:,} video  ({mode} modu)"
            )
        except Exception:
            pass

    # ── FİLMLER ──────────────────────────────────────────────────────────
    db = await loop.run_in_executor(None, _get_db)
    movie_cursor = await loop.run_in_executor(
        None, _get_cursor, "movie", {"telegram": 1, "title": 1, "name": 1}
    )

    while True:
        movie = await loop.run_in_executor(None, _next_doc, movie_cursor)
        if movie is None:
            break
        processed += 1

        telegram = movie.get("telegram", [])
        matched  = [t for t in telegram if kw in (t.get("name") or "").lower()]
        if matched:
            title = movie.get("title") or movie.get("name") or str(movie["_id"])
            for t in matched:
                films.append({"title": title, "video": t.get("name", "?")})
            if not test:
                remaining = [t for t in telegram if t not in matched]
                await loop.run_in_executor(None, _do_movie_update, db, movie["_id"], remaining)

        await update_progress("🎬 Filmler")

    # ── DİZİLER ──────────────────────────────────────────────────────────
    tv_cursor = await loop.run_in_executor(
        None, _get_cursor, "tv", {"seasons": 1, "title": 1, "name": 1}
    )

    while True:
        tv = await loop.run_in_executor(None, _next_doc, tv_cursor)
        if tv is None:
            break
        processed += 1

        tv_title  = tv.get("title") or tv.get("name") or str(tv["_id"])
        seasons   = tv.get("seasons", [])
        tv_changed = False
        new_seasons = []

        for season in seasons:
            season_no    = season.get("season_number")
            new_episodes = []
            for episode in season.get("episodes", []):
                telegram = episode.get("telegram", [])
                matched  = [t for t in telegram if kw in (t.get("name") or "").lower()]
                if not matched:
                    new_episodes.append(episode)
                    continue
                for t in matched:
                    series.append({
                        "title": tv_title, "season": season_no,
                        "episode": episode.get("episode_number"),
                        "video": t.get("name", "?"),
                    })
                if not test:
                    remaining = [t for t in telegram if t not in matched]
                    tv_changed = True
                    if remaining:
                        episode["telegram"] = remaining
                        new_episodes.append(episode)
                else:
                    new_episodes.append(episode)

            if new_episodes:
                season["episodes"] = new_episodes
                new_seasons.append(season)
            elif not test:
                tv_changed = True

        if not test and tv_changed:
            await loop.run_in_executor(None, _do_tv_update, db, tv["_id"], new_seasons)

        await update_progress("📺 Diziler")

    # ── SONUÇ ─────────────────────────────────────────────────────────────
    await status.delete()

    total = len(films) + len(series)
    if total == 0:
        return await message.reply_text(
            f"🔍 <b>{keyword}</b> ile eşleşen video bulunamadı."
        )

    prefix = "🔎 Silinecek" if test else "🗑 Silinen"
    lines  = [f"{prefix} içerikler — anahtar: <b>{keyword}</b>\n"]

    if films:
        lines.append(f"🎬 <b>Filmler ({len(films)} video):</b>")
        for i, f in enumerate(films, 1):
            lines.append(f"  {i}) [{f['title']}] {f['video']}")

    if series:
        lines.append(f"\n📺 <b>Diziler ({len(series)} video):</b>")
        for i, s in enumerate(series, 1):
            lines.append(
                f"  {i}) [{s['title']}] "
                f"S{s['season']:02d}E{s['episode']:02d} — {s['video']}"
            )

    lines.append(f"\n📊 Toplam: {total} video")
    full_text = "\n".join(lines)

    if total > 20:
        path = f"/tmp/iceriksil_{int(time())}.txt"
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(full_text)
        await message.reply_document(
            path,
            caption=f"{prefix} {total} video — anahtar: {keyword}"
        )
    else:
        await message.reply_text(full_text)


# ── /iceriksil ─────────────────────────────────────────────────────────────

@Client.on_message(filters.command("iceriksil") & filters.private & CustomFilters.owner)
async def iceriksil_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "Kullanım: /iceriksil <anahtar>\n"
            "Örnek: /iceriksil blm"
        )
    await _run(client, message, " ".join(message.command[1:]), test=False)


# ── /iceriksiltest ─────────────────────────────────────────────────────────

@Client.on_message(filters.command("iceriksiltest") & filters.private & CustomFilters.owner)
async def iceriksiltest_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "Kullanım: /iceriksiltest <anahtar>\n"
            "Örnek: /iceriksiltest blm"
        )
    await _run(client, message, " ".join(message.command[1:]), test=True)
