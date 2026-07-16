"""
fix_manual_tmdb_ids.py
========================
Eski kodla (tmdb_id=None düzeltmesinden ÖNCE) "Manuel İçerik Ekle" paneli
üzerinden eklenmiş film/dizi kayıtları veritabanında hâlâ tmdb_id=None
olarak duruyor. Bu yüzden:
  - /media/edit sayfası "int_parsing" hatası veriyor
  - Silme/kalite yeniden adlandırma/sezon-bölüm silme uçları
    "tmdb_id ... valid integer" hatası veriyor (silme butonunda
    "[object Object] hata verdi" olarak görünür)

Backend/helper/metadata.py artık yeni eklenen manuel içerikler için
başlıktan türetilen kararlı (deterministik) NEGATİF bir tmdb_id üretiyor
(bkz. _manual_tmdb_id). Bu betik AYNI fonksiyonu kullanarak, veritabanında
tmdb_id alanı eksik/None olan ama imdb_id'si "manual-" ile başlayan (yani
manuel eklenmiş) kayıtları geriye dönük olarak düzeltir.

Çalıştırma:
    python -m Backend.scripts.fix_manual_tmdb_ids          # dry-run (sadece rapor)
    python -m Backend.scripts.fix_manual_tmdb_ids --apply  # gerçekten günceller

Not: Bu betik tüm storage_N veritabanlarını (movie + tv) tarar.
"""

from __future__ import annotations

import asyncio
import sys

from Backend import db
from Backend.logger import LOGGER
from Backend.helper.metadata import _manual_tmdb_id

_MANUAL_QUERY = {
    "imdb_id": {"$regex": "^manual-"},
    "$or": [{"tmdb_id": None}, {"tmdb_id": {"$exists": False}}],
}


async def _run(apply: bool) -> None:
    await db.connect()

    storage_keys = sorted(k for k in db.dbs if k.startswith("storage_"))
    total_movies_fixed = 0
    total_tv_fixed = 0

    for db_key in storage_keys:
        storage = db.dbs[db_key]

        # ── Filmler ──────────────────────────────────────────────────────
        async for movie in storage["movie"].find(_MANUAL_QUERY):
            imdb_id = movie.get("imdb_id")
            new_tmdb_id = _manual_tmdb_id(imdb_id)
            total_movies_fixed += 1
            LOGGER.info(
                "[fix_manual_tmdb_ids] %s | film: %s (%s) -> tmdb_id=%d",
                db_key, movie.get("title") or movie.get("_id"), imdb_id, new_tmdb_id,
            )
            if apply:
                await storage["movie"].update_one(
                    {"_id": movie["_id"]},
                    {"$set": {"tmdb_id": new_tmdb_id}},
                )

        # ── Diziler ──────────────────────────────────────────────────────
        async for show in storage["tv"].find(_MANUAL_QUERY):
            imdb_id = show.get("imdb_id")
            new_tmdb_id = _manual_tmdb_id(imdb_id)
            total_tv_fixed += 1
            LOGGER.info(
                "[fix_manual_tmdb_ids] %s | dizi: %s (%s) -> tmdb_id=%d",
                db_key, show.get("title") or show.get("_id"), imdb_id, new_tmdb_id,
            )
            if apply:
                await storage["tv"].update_one(
                    {"_id": show["_id"]},
                    {"$set": {"tmdb_id": new_tmdb_id}},
                )

    mode = "UYGULANDI" if apply else "DRY-RUN (hiçbir şey yazılmadı)"
    print(
        f"\n[{mode}] Düzeltilen kayıt: "
        f"{total_movies_fixed} film + {total_tv_fixed} dizi"
    )
    if not apply and (total_movies_fixed or total_tv_fixed):
        print("Gerçekten uygulamak için: python -m Backend.scripts.fix_manual_tmdb_ids --apply")


if __name__ == "__main__":
    asyncio.run(_run(apply="--apply" in sys.argv))
