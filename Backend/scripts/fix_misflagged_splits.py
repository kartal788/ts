"""
fix_misflagged_splits.py
=========================
/tara komutuyla taranmış bazı çok parçalı (split) video dosyaları, Telegram'ın
parçaya yanlış mime_type (örn. application/zip) atamasından dolayı yanlışlıkla
"arşiv" olarak işaretlenmiş ve isimlerine sahte bir ".zip" eki eklenmişti
(örn. "Film.1080p.mkv.001.zip"). Bu durum, indirme/birleştirme mantığının
(virtual stream / parts merge) bu kayıtlar için devre dışı kalmasına ve
sadece tek bir parçanın inmesine neden oluyordu.

Bu betik, kod düzeltmesi tara.py'ye uygulandıktan SONRA, daha önce taranıp
DB'ye bu hatalı haliyle yazılmış kayıtları tek seferlik olarak düzeltir:
  - is_archive bayrağını False yapar
  - İsimdeki sahte ".zip" ekini kaldırır (".mkv.001.zip" → ".mkv.001")

Çalıştırma:
    python -m Backend.scripts.fix_misflagged_splits          # dry-run (sadece rapor)
    python -m Backend.scripts.fix_misflagged_splits --apply  # gerçekten günceller

Not: Bu betik tüm storage_N veritabanlarını (movie + tv) tarar.
"""

from __future__ import annotations

import asyncio
import re
import sys

from Backend import db
from Backend.logger import LOGGER

# Sahte ".zip" eki almış gerçek video split dosyası deseni:
#   ...mkv.001.zip / ...mp4.002.zip / ...avi.003.zip vb.
_BOGUS_ZIP_SPLIT_RE = re.compile(
    r'^(?P<base>.+\.(?:mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv)\.\d{2,3})\.zip$',
    re.IGNORECASE,
)


def _fix_quality_list(qualities: list) -> int:
    """Bir telegram kalite listesindeki hatalı kayıtları düzeltir. Düzeltilen sayıyı döner."""
    fixed = 0
    for q in qualities or []:
        if not q.get("parts"):
            continue
        if not q.get("is_archive"):
            continue
        name = q.get("name") or ""
        m = _BOGUS_ZIP_SPLIT_RE.match(name)
        if not m:
            continue
        q["is_archive"] = False
        q["name"] = m.group("base")
        fixed += 1
    return fixed


async def _run(apply: bool) -> None:
    await db.connect()

    storage_keys = sorted(k for k in db.dbs if k.startswith("storage_"))
    total_movies_fixed = 0
    total_tv_fixed = 0

    for db_key in storage_keys:
        storage = db.dbs[db_key]

        # ── Filmler ──────────────────────────────────────────────────────
        async for movie in storage["movie"].find({"telegram.is_archive": True}):
            fixed = _fix_quality_list(movie.get("telegram", []))
            if fixed:
                total_movies_fixed += fixed
                LOGGER.info(
                    "[fix_misflagged_splits] %s | film: %s (%d kalite düzeltildi)",
                    db_key, movie.get("title") or movie.get("_id"), fixed,
                )
                if apply:
                    await storage["movie"].update_one(
                        {"_id": movie["_id"]},
                        {"$set": {"telegram": movie["telegram"]}},
                    )

        # ── Diziler ──────────────────────────────────────────────────────
        async for show in storage["tv"].find({"seasons.episodes.telegram.is_archive": True}):
            show_fixed = 0
            for season in show.get("seasons", []):
                for ep in season.get("episodes", []):
                    show_fixed += _fix_quality_list(ep.get("telegram", []))
            if show_fixed:
                total_tv_fixed += show_fixed
                LOGGER.info(
                    "[fix_misflagged_splits] %s | dizi: %s (%d kalite düzeltildi)",
                    db_key, show.get("title") or show.get("_id"), show_fixed,
                )
                if apply:
                    await storage["tv"].update_one(
                        {"_id": show["_id"]},
                        {"$set": {"seasons": show["seasons"]}},
                    )

    mode = "UYGULANDI" if apply else "DRY-RUN (hiçbir şey yazılmadı)"
    print(
        f"\n[{mode}] Toplam düzeltilen kalite kaydı: "
        f"{total_movies_fixed} film + {total_tv_fixed} dizi bölümü"
    )
    if not apply and (total_movies_fixed or total_tv_fixed):
        print("Gerçekten uygulamak için: python -m Backend.scripts.fix_misflagged_splits --apply")


if __name__ == "__main__":
    asyncio.run(_run(apply="--apply" in sys.argv))
