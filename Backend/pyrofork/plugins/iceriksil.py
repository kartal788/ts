"""
/iceriksil  <anahtar>  — veritabanındaki telegram[].name alanında
                          anahtar geçen videoları siler. Film tamamen
                          boşalırsa film de silinir; dizi için bölüm →
                          sezon → dizi kademeli olarak temizlenir.

/iceriksiltest <anahtar> — aynı mantık, silme yapmadan listeler ve
                            sonucu txt dosyası olarak gönderir.
"""

import os
from time import time

from pyrogram import Client, filters
from pyrogram.types import Message
from pymongo import MongoClient

from Backend.helper.custom_filter import CustomFilters

# ── bağlantı ──────────────────────────────────────────────────────────────
DATABASE_RAW = os.getenv("DATABASE", "")
db_urls = [u.strip() for u in DATABASE_RAW.split(",") if u.strip().startswith("mongodb")]
MONGO_URL = db_urls[1]


def _get_db():
    mongo = MongoClient(MONGO_URL)
    db_name = mongo.list_database_names()[0]
    return mongo[db_name]


# ── çekirdek fonksiyon ────────────────────────────────────────────────────

def _iceriksil_engine(keyword: str, test: bool) -> dict:
    """
    keyword ile telegram[].name alanını tara (büyük/küçük harf duyarsız).
    test=True  → hiçbir şeyi silme, sadece listele.
    test=False → eşleşen videoları sil; boşalan film/bölüm/sezon/diziyi de sil.

    Döndürür:
        {
            "films":  [{"title": ..., "videos": [...]}],
            "series": [{"title": ..., "season": ..., "episode": ..., "videos": [...]}],
        }
    """
    kw = keyword.lower()
    db = _get_db()

    result_films = []   # {"title": str, "videos": [str]}
    result_series = []  # {"title": str, "season": int, "episode": int, "videos": [str]}

    # ── FİLMLER ──────────────────────────────────────────────────────────
    for movie in list(db["movie"].find({})):
        telegram = movie.get("telegram", [])
        matched = [t for t in telegram if kw in (t.get("name") or "").lower()]
        if not matched:
            continue

        title = movie.get("title") or movie.get("name") or str(movie["_id"])
        for t in matched:
            result_films.append({
                "title": title,
                "video": t.get("name", "?"),
            })

        if not test:
            remaining = [t for t in telegram if t not in matched]
            if remaining:
                db["movie"].update_one(
                    {"_id": movie["_id"]},
                    {"$set": {"telegram": remaining}},
                )
            else:
                # Filme ait başka video yok → filmi de sil
                db["movie"].delete_one({"_id": movie["_id"]})

    # ── DİZİLER ──────────────────────────────────────────────────────────
    for tv in list(db["tv"].find({})):
        tv_title = tv.get("title") or tv.get("name") or str(tv["_id"])
        seasons = tv.get("seasons", [])
        tv_changed = False

        new_seasons = []
        for season in seasons:
            season_number = season.get("season_number")
            episodes = season.get("episodes", [])

            new_episodes = []
            for episode in episodes:
                ep_number = episode.get("episode_number")
                telegram = episode.get("telegram", [])
                matched = [t for t in telegram if kw in (t.get("name") or "").lower()]
                if not matched:
                    new_episodes.append(episode)
                    continue

                for t in matched:
                    result_series.append({
                        "title": tv_title,
                        "season": season_number,
                        "episode": ep_number,
                        "video": t.get("name", "?"),
                    })

                if not test:
                    remaining = [t for t in telegram if t not in matched]
                    if remaining:
                        episode["telegram"] = remaining
                        new_episodes.append(episode)
                    # else: bölümde başka video yok → bölümü düşür
                    tv_changed = True
                else:
                    new_episodes.append(episode)

            if new_episodes:
                season["episodes"] = new_episodes
                new_seasons.append(season)
            elif not test:
                # Sezona ait başka bölüm yok → sezonu düşür
                tv_changed = True

        if not test and tv_changed:
            if new_seasons:
                db["tv"].update_one(
                    {"_id": tv["_id"]},
                    {"$set": {"seasons": new_seasons}},
                )
            else:
                # Diziye ait hiç sezon kalmadı → diziyi de sil
                db["tv"].delete_one({"_id": tv["_id"]})

    return {"films": result_films, "series": result_series}


# ── çıktı gönderici ───────────────────────────────────────────────────────

async def _send_result(message: Message, data: dict, keyword: str, test: bool):
    films = data["films"]
    series = data["series"]
    total = len(films) + len(series)

    if total == 0:
        return await message.reply_text(
            f"🔍 <b>{keyword}</b> anahtar kelimesiyle eşleşen video bulunamadı."
        )

    prefix = "🔎 Silinecek" if test else "🗑 Silinen"
    lines = [f"{prefix} içerikler — anahtar: <b>{keyword}</b>\n"]

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
        caption = (
            f"{prefix} {total} video listelendi (anahtar: {keyword}).\n"
            "📄 Detaylar dosya olarak gönderildi."
        )
        await message.reply_document(path, caption=caption)
    else:
        await message.reply_text(full_text)


# ── /iceriksil ────────────────────────────────────────────────────────────

@Client.on_message(filters.command("iceriksil") & filters.private & CustomFilters.owner)
async def iceriksil_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "Kullanım: /iceriksil <anahtar>\n"
            "Örnek: /iceriksil blm\n\n"
            "Veritabanındaki tüm film ve dizilerin telegram[].name alanını tarar,\n"
            "eşleşen videoları siler. Film/bölüm/sezon/dizi boşalırsa o da silinir."
        )

    keyword = " ".join(message.command[1:])
    status = await message.reply_text(f"🔍 <b>{keyword}</b> taranıyor, lütfen bekleyin…")

    try:
        data = _iceriksil_engine(keyword, test=False)
        await status.delete()
        await _send_result(message, data, keyword, test=False)
    except Exception as e:
        await status.edit_text(f"❌ Hata oluştu:\n<code>{e}</code>")


# ── /iceriksiltest ────────────────────────────────────────────────────────

@Client.on_message(filters.command("iceriksiltest") & filters.private & CustomFilters.owner)
async def iceriksiltest_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "Kullanım: /iceriksiltest <anahtar>\n"
            "Örnek: /iceriksiltest blm\n\n"
            "Silme işlemi yapmadan eşleşecek videoları listeler ve txt olarak gönderir."
        )

    keyword = " ".join(message.command[1:])
    status = await message.reply_text(f"🔍 <b>{keyword}</b> taranıyor (test modu)…")

    try:
        data = _iceriksil_engine(keyword, test=True)
        await status.delete()
        await _send_result(message, data, keyword, test=True)
    except Exception as e:
        await status.edit_text(f"❌ Hata oluştu:\n<code>{e}</code>")
