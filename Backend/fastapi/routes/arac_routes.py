"""
/admin/araclar sayfası için API.

Bot tarafındaki /aynivideolarisil, /iceriksil ve /tara komutlarını panelden
tetiklemeyi ve ilerlemesini canlı izlemeyi sağlar. Her işlem arka planda
(asyncio.create_task) çalışır; panel periyodik olarak durum endpoint'lerini
poll ederek ilerleme çubuğunu günceller.
"""

import asyncio
import os
import time

from fastapi import HTTPException
from pymongo import MongoClient, UpdateOne

from Backend.logger import LOGGER
from Backend.config import Telegram


# ─────────────────────────────────────────────────────────────
# Ortak yardımcılar
# ─────────────────────────────────────────────────────────────

def _progress_bar(pct: float, width: int = 20) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _get_sync_db():
    """Senkron pymongo bağlantısı — executor içinde çağrılmalı."""
    db_raw = os.getenv("DATABASE", "")
    db_urls = [u.strip() for u in db_raw.split(",") if u.strip().startswith("mongodb")]
    if not db_urls:
        raise RuntimeError("DATABASE ortam değişkeni bulunamadı.")
    mongo_url = db_urls[1] if len(db_urls) > 1 else db_urls[0]
    mongo = MongoClient(mongo_url)
    db_name = mongo.list_database_names()[0]
    return mongo[db_name]


# ═════════════════════════════════════════════════════════════
# 1) /aynivideolarisil — mükerrer (aynı) videoları temizle
# ═════════════════════════════════════════════════════════════

AYNI_STATE = {
    "running": False, "processed": 0, "total": 0,
    "total_docs": 0, "total_removed": 0, "phase": "",
    "started_at": 0.0, "finished_at": 0.0, "error": None,
}


def _dedup_telegram(telegram: list) -> tuple[list, int]:
    grouped: dict = {}
    for idx, t in enumerate(telegram):
        key = (t.get("name"), t.get("size"))
        grouped.setdefault(key, []).append((idx, t))

    new_telegram, removed = [], 0
    for items in grouped.values():
        non_http = [
            (i, t) for i, t in items
            if not str(t.get("id", "")).lower().startswith(("http://", "https://"))
        ]
        _, keep_t = max(non_http if non_http else items, key=lambda x: x[0])
        new_telegram.append(keep_t)
        removed += len(items) - 1
    return new_telegram, removed


def _process_movie_doc(doc: dict):
    telegram = doc.get("telegram", [])
    if len(telegram) <= 1:
        return False, telegram, 0
    new_telegram, removed = _dedup_telegram(telegram)
    if removed == 0:
        return False, telegram, 0
    return True, new_telegram, removed


def _process_tv_doc(doc: dict):
    seasons = doc.get("seasons", [])
    total_removed = 0
    doc_changed = False
    for season in seasons:
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
    return doc_changed, seasons, total_removed


async def _run_aynivideolarisil():
    loop = asyncio.get_event_loop()
    s = AYNI_STATE
    s.update(running=True, processed=0, total=0, total_docs=0, total_removed=0,
              phase="Başlatılıyor…", started_at=time.time(), finished_at=0.0, error=None)
    try:
        db = await loop.run_in_executor(None, _get_sync_db)
        movie_col, series_col = db["movie"], db["tv"]

        total_movie = await loop.run_in_executor(None, movie_col.count_documents, {})
        total_tv = await loop.run_in_executor(None, series_col.count_documents, {})
        s["total"] = total_movie + total_tv

        BATCH_SIZE = 100
        for col, col_name, label in (
            (movie_col, "movie", "🎬 Filmler taranıyor"),
            (series_col, "tv", "📺 Diziler taranıyor"),
        ):
            s["phase"] = label

            def _get_cursor(c=col):
                return c.find({}, {"telegram": 1, "seasons": 1}).batch_size(200)

            cursor = await loop.run_in_executor(None, _get_cursor)
            bulk_ops = []
            while True:
                doc = await loop.run_in_executor(None, lambda c=cursor: next(c, None))
                if doc is None:
                    break
                s["processed"] += 1

                if col_name == "movie":
                    changed, new_telegram, removed = await loop.run_in_executor(
                        None, _process_movie_doc, doc
                    )
                    if changed:
                        bulk_ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"telegram": new_telegram}}))
                        s["total_docs"] += 1
                        s["total_removed"] += removed
                else:
                    changed, new_seasons, removed = await loop.run_in_executor(
                        None, _process_tv_doc, doc
                    )
                    if changed:
                        bulk_ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"seasons": new_seasons}}))
                        s["total_docs"] += 1
                        s["total_removed"] += removed

                if len(bulk_ops) >= BATCH_SIZE:
                    ops = bulk_ops[:]
                    bulk_ops.clear()
                    await loop.run_in_executor(None, lambda o=ops: col.bulk_write(o, ordered=False))

            if bulk_ops:
                ops = bulk_ops[:]
                await loop.run_in_executor(None, lambda o=ops: col.bulk_write(o, ordered=False))

        s["phase"] = "Tamamlandı"
    except Exception as e:
        LOGGER.error(f"[araclar/aynivideolarisil] Hata: {e}")
        s["error"] = str(e)
        s["phase"] = "Hata"
    finally:
        s["running"] = False
        s["finished_at"] = time.time()


async def ayni_status_api() -> dict:
    s = AYNI_STATE
    pct = (s["processed"] / s["total"] * 100) if s["total"] else 0
    return {**s, "percent": round(pct, 1), "bar": _progress_bar(pct)}


async def ayni_start_api() -> dict:
    if AYNI_STATE["running"]:
        raise HTTPException(status_code=409, detail="Bu işlem zaten çalışıyor.")
    asyncio.create_task(_run_aynivideolarisil())
    return {"success": True}


# ═════════════════════════════════════════════════════════════
# 2) /iceriksil — anahtar kelimeye göre video silme / test
# ═════════════════════════════════════════════════════════════

ICERIKSIL_STATE = {
    "running": False, "processed": 0, "total": 0, "phase": "",
    "hits": 0, "keyword": "", "mode": "", "started_at": 0.0,
    "finished_at": 0.0, "error": None, "result": None,
}


async def _run_iceriksil(keyword: str, test: bool):
    loop = asyncio.get_event_loop()
    s = ICERIKSIL_STATE
    kw = keyword.lower()
    s.update(running=True, processed=0, total=0, phase="Başlatılıyor…", hits=0,
              keyword=keyword, mode="test" if test else "silme",
              started_at=time.time(), finished_at=0.0, error=None, result=None)
    try:
        db = await loop.run_in_executor(None, _get_sync_db)
        movie_col, tv_col = db["movie"], db["tv"]

        total_movies = await loop.run_in_executor(None, movie_col.count_documents, {})
        total_tv = await loop.run_in_executor(None, tv_col.count_documents, {})
        s["total"] = total_movies + total_tv

        films, series = [], []

        # ── Filmler ──────────────────────────────────────────
        s["phase"] = "🎬 Filmler"

        def _movie_cursor():
            return movie_col.find({}, {"telegram": 1, "title": 1, "name": 1}).batch_size(200)

        cursor = await loop.run_in_executor(None, _movie_cursor)
        while True:
            movie = await loop.run_in_executor(None, lambda c=cursor: next(c, None))
            if movie is None:
                break
            s["processed"] += 1

            telegram = movie.get("telegram", [])
            matched = [t for t in telegram if kw in (t.get("name") or "").lower()]
            if matched:
                title = movie.get("title") or movie.get("name") or str(movie["_id"])
                for t in matched:
                    films.append({"title": title, "video": t.get("name", "?")})
                if not test:
                    remaining = [t for t in telegram if t not in matched]
                    mid = movie["_id"]
                    if remaining:
                        await loop.run_in_executor(
                            None, lambda: movie_col.update_one({"_id": mid}, {"$set": {"telegram": remaining}})
                        )
                    else:
                        await loop.run_in_executor(None, lambda: movie_col.delete_one({"_id": mid}))
            s["hits"] = len(films) + len(series)

        # ── Diziler ──────────────────────────────────────────
        s["phase"] = "📺 Diziler"

        def _tv_cursor():
            return tv_col.find({}, {"seasons": 1, "title": 1, "name": 1}).batch_size(200)

        cursor = await loop.run_in_executor(None, _tv_cursor)
        while True:
            tv = await loop.run_in_executor(None, lambda c=cursor: next(c, None))
            if tv is None:
                break
            s["processed"] += 1

            tv_title = tv.get("title") or tv.get("name") or str(tv["_id"])
            seasons = tv.get("seasons", [])
            tv_changed = False
            new_seasons = []

            for season in seasons:
                season_no = season.get("season_number")
                new_episodes = []
                for episode in season.get("episodes", []):
                    telegram = episode.get("telegram", [])
                    matched = [t for t in telegram if kw in (t.get("name") or "").lower()]
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
                tvid = tv["_id"]
                if new_seasons:
                    await loop.run_in_executor(
                        None, lambda: tv_col.update_one({"_id": tvid}, {"$set": {"seasons": new_seasons}})
                    )
                else:
                    await loop.run_in_executor(None, lambda: tv_col.delete_one({"_id": tvid}))
            s["hits"] = len(films) + len(series)

        s["phase"] = "Tamamlandı"
        total_hits = len(films) + len(series)
        s["result"] = {
            "films": films[:200], "series": series[:200],
            "total": total_hits, "truncated": total_hits > 200,
        }
    except Exception as e:
        LOGGER.error(f"[araclar/iceriksil] Hata: {e}")
        s["error"] = str(e)
        s["phase"] = "Hata"
    finally:
        s["running"] = False
        s["finished_at"] = time.time()


async def iceriksil_status_api() -> dict:
    s = ICERIKSIL_STATE
    pct = (s["processed"] / s["total"] * 100) if s["total"] else 0
    return {**s, "percent": round(pct, 1), "bar": _progress_bar(pct)}


async def iceriksil_start_api(payload: dict) -> dict:
    keyword = (payload.get("keyword") or "").strip()
    test = bool(payload.get("test", False))
    if not keyword:
        raise HTTPException(status_code=400, detail="Anahtar kelime boş olamaz.")
    if len(keyword) > 100:
        raise HTTPException(status_code=400, detail="Anahtar kelime çok uzun.")
    if ICERIKSIL_STATE["running"]:
        raise HTTPException(status_code=409, detail="Bu işlem zaten çalışıyor.")
    asyncio.create_task(_run_iceriksil(keyword, test))
    return {"success": True}


# ═════════════════════════════════════════════════════════════
# 3) /tara — AUTH_CHANNEL kanallarını tarayıp kataloğa ekle
#    (mevcut pyrofork eklentisindeki tarama mantığı yeniden kullanılır)
# ═════════════════════════════════════════════════════════════

def _serialize_tara_state() -> dict:
    from Backend.pyrofork.plugins import tara as _tara_mod
    s = _tara_mod.state
    return {
        "running": s.running,
        "cancelled": s.cancelled,
        "channel_name": s.channel_name,
        "elapsed": s.elapsed,
        "total_found": s.total_found,
        "processed": s.processed,
        "indexed": s.indexed,
        "skipped_dup": s.skipped_dup,
        "skipped_meta": s.skipped_meta,
        "skipped_nonvid": s.skipped_nonvid,
        "errors": s.errors,
    }


async def _run_tara_web(purge: bool):
    from Backend.pyrofork.plugins import tara as _tara_mod
    from Backend.pyrofork.bot import StreamBot

    s = _tara_mod.state
    s.reset()
    s.running = True
    s.started_at = time.time()
    s.status_msg = None  # panelden tetiklenince Telegram'a canlı mesaj atılmaz

    channels = list(Telegram.AUTH_CHANNEL)
    try:
        if not channels:
            s.errors += 1
            LOGGER.error("[araclar/tara] AUTH_CHANNEL yapılandırılmamış.")
            return

        if purge:
            await _tara_mod._purge_all_media()

        for ch_str in channels:
            if s.cancelled:
                break
            try:
                ch_id = int(ch_str)
            except ValueError:
                LOGGER.warning(f"[araclar/tara] Geçersiz kanal ID: {ch_str}")
                continue
            await _tara_mod._scan_channel(StreamBot, ch_id)
    except Exception as e:
        LOGGER.error(f"[araclar/tara] Beklenmeyen hata: {e}")
    finally:
        s.running = False


async def tara_status_api() -> dict:
    return _serialize_tara_state()


async def tara_start_api(payload: dict) -> dict:
    from Backend.pyrofork.plugins import tara as _tara_mod

    if _tara_mod.state.running:
        raise HTTPException(status_code=409, detail="Zaten bir tarama çalışıyor.")

    mode = (payload.get("mode") or "full").strip().lower()
    if mode not in ("full", "db"):
        raise HTTPException(status_code=400, detail="Geçersiz mod.")

    asyncio.create_task(_run_tara_web(purge=(mode == "full")))
    return {"success": True}


async def tara_iptal_api() -> dict:
    from Backend.pyrofork.plugins import tara as _tara_mod

    if not _tara_mod.state.running:
        raise HTTPException(status_code=409, detail="Çalışan bir tarama yok.")
    _tara_mod.state.cancelled = True
    return {"success": True}
