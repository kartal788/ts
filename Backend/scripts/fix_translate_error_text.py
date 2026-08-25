"""
fix_translate_error_text.py
=============================
Backend/helper/metadata.py içindeki eski Google Translate (deep_translator)
tabanlı çeviri akışı, Google'ın web arayüzünü kazıyarak (scraping) çalıştığı
için bazen gerçek çeviri yerine kendi jenerik hata sayfasını (HTTP 200 ile
birlikte "Error 500 (Server Error)!!1 500. That's an error. There was an
error. Please try again later. That's all we know.") döndürüyordu; eski kod
bunu bir hata olarak ALGILAMIYORDU ve bu metni "çeviri" sanıp doğrudan
veritabanına yazıyordu (title_tr/title_de, description_tr/description_de,
genres_tr/genres_de, bölüm başlığı/özeti hep bu akıştan geçiyordu).

metadata.py artık TR/DE alanlarını (imdb eşleşmesi doğrulandığında) doğrudan
TMDB'den alıyor; Google çeviri yalnızca TMDB'de veri yoksa bir yedek (fallback)
olarak kullanılıyor. Ancak bu düzeltmeden ÖNCE eklenmiş, veritabanında hâlâ bu
bozuk Google hata metnini taşıyan eski kayıtlar bulunabilir. Bu betik, tüm
storage_N veritabanlarını (movie + tv, dizilerde ayrıca her sezon/bölümü de
dahil) tarar; aşağıdaki alanlardan herhangi biri bu hata imzasını taşıyorsa
karşılık gelen kaydı TMDB'den (tmdb_id veya imdb_id üzerinden) yeniden çeker
ve o alanı TMDB'den gelen gerçek veriyle DEĞİŞTİRİR (Google çeviri KULLANMAZ):

  title_tr / title_de
  description_tr / description_de
  genres_tr / genres_de
  seasons[].episodes[].title_tr / title_de
  seasons[].episodes[].overview_tr / overview_de

TMDB'de kayıt bulunamazsa veya ilgili alan TMDB'de de boşsa, alan olduğu gibi
bırakılır (bozuk metin kalır) ve bir sonraki başlangıçta tekrar denenir —
işlem idempotenttir.

İMDB EŞLEŞME DOĞRULAMASI: TMDB, aranan imdb_id ile eşleşmeyen bir kayıt
döndürebilir. Bu durumda TMDB'nin döndürdüğü metin YANLIŞ içeriğe ait
olabileceğinden, TMDB'nin external_ids.imdb_id'si veritabanındaki imdb_id ile
uyuşmuyorsa (her ikisi de doluysa) o kayıt atlanır ve bir UYARI loglanır.

MANUEL KAYITLAR: Panelden "Manuel İçerik Ekle" ile eklenmiş kayıtlarda
title_tr/title_de ve description_tr/description_de TASARIM GEREĞİ kullanıcı
tarafından girilmiştir; bu kayıtlar taranmaz (bkz. _is_manual_record).

OTOMATİK ÇALIŞMA: Bu betik Backend/__main__.py -> start_services() içinden,
bot her başladığında arka planda (fire-and-forget) otomatik olarak da
çağrılır (bkz. run_fix_translate_error_text). Elle çalıştırmaya gerek
yoktur.

CursorNotFound'a karşı önlem: MongoDB'den `find({})` ile açılan CANLI bir
cursor üzerinde `async for` ile dolaşıp her kayıt için ağ çağrısı (TMDB)
yapmak, cursor'ı uzun süre "boşta" bırakarak zaman aşımına
(pymongo.errors.CursorNotFound) yol açabilir. Bu yüzden kayıtlar önce
`.to_list()` ile BELLEĞE ALINIP cursor hemen kapatılır; TMDB çağrıları
bellekteki liste üzerinde yapılır.

TMDB'ye art arda çok hızlı istek atmamak için her onarım denemesi arasında
REPAIR_DELAY_SECONDS kadar beklenir. OTOMATİK (bot açılışı) çalıştırmalarda
tek seferde en fazla AUTO_RUN_MAX_REPAIRS kadar alan onarılır; sınıra takılan
kayıtlar bir sonraki başlangıçta kaldığı yerden devam eder. Elle çalıştırılan
CLI (`--apply`) bu sınıra tabi değildir.

Elle çalıştırma / rapor almak isteyenler için CLI hâlâ kullanılabilir:
    python -m Backend.scripts.fix_translate_error_text          # dry-run (sadece rapor)
    python -m Backend.scripts.fix_translate_error_text --apply  # gerçekten günceller (sınırsız)

Not: Bu betik tüm storage_N veritabanlarını (movie + tv) tarar.
"""

from __future__ import annotations

import asyncio
import sys

from Backend import db
from Backend.logger import LOGGER
from Backend.helper.metadata import (
    _is_translate_error_page,
    _tmdb_movie_details,
    _tmdb_tv_details,
    _tmdb_episode_details,
    _resolve_tmdb_id_from_imdb,
    tur_genre_normalize,
    tmdb_tr,
    tmdb_de,
)

#----- TMDB'ye art arda çok hızlı istek atmamak için her onarım denemesi
#----- (bir kaydın TMDB'den yeniden çekilmesi) arasında beklenen süre.
REPAIR_DELAY_SECONDS = 2

#----- Otomatik (bot açılışı) çalıştırmalarda tek seferde onarılacak azami
#----- ALAN sayısı (title_tr, description_de, bölüm başlığı vb. her biri ayrı
#----- bir "alan"dır). Kalan kayıtlar sıradaki başlangıçta devam eder.
AUTO_RUN_MAX_REPAIRS = 300

#----- Onarılacak üst düzey (film/dizi geneli) metin alanları.
TOP_LEVEL_TEXT_FIELDS = ("title_tr", "title_de", "description_tr", "description_de")
TOP_LEVEL_LIST_FIELDS = ("genres_tr", "genres_de")

#----- Onarılacak bölüm (episode) alanları.
EPISODE_TEXT_FIELDS = ("title_tr", "title_de", "overview_tr", "overview_de")


def _is_manual_record(doc: dict) -> bool:
    """Panelden 'Manuel İçerik Ekle' ile eklenmiş kayıtlarda title_tr/title_de
    ve description_tr/description_de TASARIM GEREĞİ kullanıcı tarafından
    girilmiştir; bu kayıtlar bozuk sayılmaz ve taranmaz."""
    return str(doc.get("imdb_id") or "").startswith("manual-")


def _text_needs_repair(value) -> bool:
    """value bir string ise doğrudan, bir liste ise (genres_tr/de gibi)
    içindeki herhangi bir string hata imzasını taşıyorsa True döner."""
    if isinstance(value, str):
        return _is_translate_error_page(value)
    if isinstance(value, list):
        return any(isinstance(v, str) and _is_translate_error_page(v) for v in value)
    return False


class _Budget:
    """Otomatik çalıştırmalarda toplam onarım (alan) sayısını sınırlar.
    apply=False (dry-run) veya limit=None (elle --apply) iken sınırsızdır."""

    def __init__(self, limit: int | None):
        self.limit = limit
        self.used = 0
        self.exhausted_notice_logged = False

    def has_room(self) -> bool:
        if self.limit is None:
            return True
        if self.used < self.limit:
            return True
        if not self.exhausted_notice_logged:
            LOGGER.info(
                "[fix_translate_error_text] Bu çalıştırma için onarım sınırına (%d) ulaşıldı; "
                "kalan kayıtlar bir sonraki başlangıçta işlenecek.",
                self.limit,
            )
            self.exhausted_notice_logged = True
        return False

    def consume(self) -> None:
        self.used += 1


async def _resolve_tmdb_id(doc: dict, media_type: str) -> int | None:
    """Kaydın tmdb_id'sini (yoksa imdb_id üzerinden TMDB'de arayarak) döner."""
    raw_tmdb_id = doc.get("tmdb_id")
    if raw_tmdb_id and str(raw_tmdb_id).isdigit():
        return int(raw_tmdb_id)
    imdb_id = doc.get("imdb_id")
    if imdb_id and str(imdb_id).startswith("tt"):
        return await _resolve_tmdb_id_from_imdb(imdb_id, media_type)
    return None


def _imdb_mismatch(details, expected_imdb_id: str | None) -> bool:
    """TMDB'den dönen kaydın external_ids.imdb_id'si, veritabanındaki
    imdb_id'den FARKLI VE her ikisi de doluysa True (eşleşme doğrulanamadı,
    TMDB verisi kullanılmamalı) döner."""
    tmdb_ext_imdb = getattr(getattr(details, "external_ids", None), "imdb_id", None)
    if tmdb_ext_imdb and expected_imdb_id and tmdb_ext_imdb != expected_imdb_id:
        return True
    return False


async def _repair_top_level(doc: dict, media_type: str, budget: "_Budget") -> dict:
    """Film/dizi geneli alanları (title_tr/de, description_tr/de, genres_tr/de)
    TMDB'den onarır; değişen alanları set_fields olarak döner."""
    set_fields: dict = {}

    if _is_manual_record(doc):
        return set_fields

    needs_any = any(_text_needs_repair(doc.get(f)) for f in TOP_LEVEL_TEXT_FIELDS) or any(
        _text_needs_repair(doc.get(f)) for f in TOP_LEVEL_LIST_FIELDS
    )
    if not needs_any or not budget.has_room():
        return set_fields

    tmdb_id = await _resolve_tmdb_id(doc, media_type)
    if not tmdb_id:
        LOGGER.warning(
            "[fix_translate_error_text] TMDB id bulunamadı, atlanıyor: %s (%s)",
            doc.get("title") or doc.get("_id"), doc.get("imdb_id"),
        )
        return set_fields

    details = await (_tmdb_movie_details(tmdb_id) if media_type == "movie" else _tmdb_tv_details(tmdb_id))
    await asyncio.sleep(REPAIR_DELAY_SECONDS)
    if not details:
        return set_fields

    if _imdb_mismatch(details, doc.get("imdb_id")):
        LOGGER.warning(
            "[fix_translate_error_text] TMDB imdb_id eşleşmiyor, atlanıyor: tmdb_id=%s db_imdb_id=%s",
            tmdb_id, doc.get("imdb_id"),
        )
        return set_fields

    tr_title = details.title if media_type == "movie" else getattr(details, "name", None)
    if _text_needs_repair(doc.get("title_tr")) and tr_title and budget.has_room():
        set_fields["title_tr"] = tr_title
        budget.consume()

    de_title = getattr(details, "title_de", "") if media_type == "movie" else getattr(details, "name_de", "")
    if _text_needs_repair(doc.get("title_de")) and de_title and budget.has_room():
        set_fields["title_de"] = de_title
        budget.consume()

    if _text_needs_repair(doc.get("description_tr")) and getattr(details, "overview", "") and budget.has_room():
        set_fields["description_tr"] = details.overview
        budget.consume()

    if _text_needs_repair(doc.get("description_de")) and getattr(details, "overview_de", "") and budget.has_room():
        set_fields["description_de"] = details.overview_de
        budget.consume()

    if _text_needs_repair(doc.get("genres_tr")) and budget.has_room():
        genres = [g.name for g in (getattr(details, "genres", None) or [])]
        if genres:
            set_fields["genres_tr"] = tur_genre_normalize(genres)
            budget.consume()

    if _text_needs_repair(doc.get("genres_de")) and budget.has_room():
        genres_de = getattr(details, "genres_de", []) or []
        if genres_de:
            set_fields["genres_de"] = genres_de
            budget.consume()

    return set_fields


async def _repair_episode(
    show: dict, ep: dict, tv_tmdb_id: int | None, season_number: int, episode_number: int, budget: "_Budget",
) -> dict:
    """Bir bölümün title_tr/de, overview_tr/de alanlarını TMDB'den onarır."""
    ep_set_fields: dict = {}

    needs_any = any(_text_needs_repair(ep.get(f)) for f in EPISODE_TEXT_FIELDS)
    if not needs_any or not budget.has_room():
        return ep_set_fields

    if not tv_tmdb_id:
        return ep_set_fields

    needs_tr = _text_needs_repair(ep.get("title_tr")) or _text_needs_repair(ep.get("overview_tr"))
    needs_de = _text_needs_repair(ep.get("title_de")) or _text_needs_repair(ep.get("overview_de"))

    tmdb_ep_tr = tmdb_ep_de = None
    if needs_tr:
        tmdb_ep_tr = await _tmdb_episode_details(tv_tmdb_id, season_number, episode_number, tmdb_tr)
        await asyncio.sleep(REPAIR_DELAY_SECONDS)
    if needs_de:
        tmdb_ep_de = await _tmdb_episode_details(tv_tmdb_id, season_number, episode_number, tmdb_de)
        await asyncio.sleep(REPAIR_DELAY_SECONDS)

    if _text_needs_repair(ep.get("title_tr")) and tmdb_ep_tr and getattr(tmdb_ep_tr, "name", "") and budget.has_room():
        ep_set_fields["title_tr"] = tmdb_ep_tr.name
        budget.consume()

    if _text_needs_repair(ep.get("title_de")) and tmdb_ep_de and getattr(tmdb_ep_de, "name", "") and budget.has_room():
        ep_set_fields["title_de"] = tmdb_ep_de.name
        budget.consume()

    if _text_needs_repair(ep.get("overview_tr")) and tmdb_ep_tr and getattr(tmdb_ep_tr, "overview", "") and budget.has_room():
        ep_set_fields["overview_tr"] = tmdb_ep_tr.overview
        budget.consume()

    if _text_needs_repair(ep.get("overview_de")) and tmdb_ep_de and getattr(tmdb_ep_de, "overview", "") and budget.has_room():
        ep_set_fields["overview_de"] = tmdb_ep_de.overview
        budget.consume()

    return ep_set_fields


async def run_fix_translate_error_text(
    apply: bool = True, max_repairs: int | None = AUTO_RUN_MAX_REPAIRS,
) -> tuple[int, int, int]:
    """Taramayı/onarımı yapar ve (düzeltilen film, dizi, bölüm-alanı) sayısını
    döner. apply=False ise hiçbir şey yazmadan sadece sayar (dry-run; bu
    modda TMDB'ye de gidilmez, sadece kaç alanın bozuk olduğu raporlanır).
    max_repairs, bu çalıştırmada onarılacak azami alan sayısıdır; None
    verilirse sınırsız çalışır (CLI --apply öntanımlısı).

    Çağıran taraf (Backend/__main__.py veya bu dosyanın CLI'ı) db.connect()'in
    zaten yapılmış olduğundan sorumludur.
    """
    storage_keys = sorted(k for k in db.dbs if k.startswith("storage_"))
    total_movies_fixed = 0
    total_tv_fixed = 0
    total_episodes_fixed = 0
    budget = _Budget(max_repairs if apply else None)

    for db_key in storage_keys:
        storage = db.dbs[db_key]

        # ── Filmler ──────────────────────────────────────────────────────
        movies = await storage["movie"].find({}).to_list(length=None)
        for movie in movies:
            if not budget.has_room():
                break
            if not any(_text_needs_repair(movie.get(f)) for f in (*TOP_LEVEL_TEXT_FIELDS, *TOP_LEVEL_LIST_FIELDS)):
                continue

            set_fields = await _repair_top_level(movie, "movie", budget) if apply else {}
            if not apply:
                # dry-run: sadece kaç alanın bozuk olduğunu say
                bad_fields = [f for f in (*TOP_LEVEL_TEXT_FIELDS, *TOP_LEVEL_LIST_FIELDS) if _text_needs_repair(movie.get(f))]
                if bad_fields:
                    total_movies_fixed += 1
                    LOGGER.info(
                        "[fix_translate_error_text] (dry-run) %s | film: %s (%s) -> bozuk alanlar: %s",
                        db_key, movie.get("title") or movie.get("_id"), movie.get("imdb_id"), ", ".join(bad_fields),
                    )
                continue

            if set_fields:
                total_movies_fixed += 1
                LOGGER.info(
                    "[fix_translate_error_text] %s | film: %s (%s) -> TMDB'den onarılan alanlar: %s",
                    db_key, movie.get("title") or movie.get("_id"), movie.get("imdb_id"), ", ".join(set_fields.keys()),
                )
                await storage["movie"].update_one({"_id": movie["_id"]}, {"$set": set_fields})

        # ── Diziler ──────────────────────────────────────────────────────
        shows = await storage["tv"].find({}).to_list(length=None)
        for show in shows:
            if not budget.has_room():
                break

            top_needs_repair = any(
                _text_needs_repair(show.get(f)) for f in (*TOP_LEVEL_TEXT_FIELDS, *TOP_LEVEL_LIST_FIELDS)
            )
            seasons = show.get("seasons") or []
            episode_needs_repair = any(
                _text_needs_repair(ep.get(f))
                for season in seasons
                for ep in (season.get("episodes") or [])
                for f in EPISODE_TEXT_FIELDS
            )
            if not top_needs_repair and not episode_needs_repair:
                continue

            if not apply:
                bad_top = [f for f in (*TOP_LEVEL_TEXT_FIELDS, *TOP_LEVEL_LIST_FIELDS) if _text_needs_repair(show.get(f))]
                bad_ep_count = sum(
                    1
                    for season in seasons
                    for ep in (season.get("episodes") or [])
                    if any(_text_needs_repair(ep.get(f)) for f in EPISODE_TEXT_FIELDS)
                )
                if bad_top or bad_ep_count:
                    total_tv_fixed += 1
                    total_episodes_fixed += bad_ep_count
                    LOGGER.info(
                        "[fix_translate_error_text] (dry-run) %s | dizi: %s (%s) -> bozuk üst düzey alanlar: %s, bozuk bölüm sayısı: %d",
                        db_key, show.get("title") or show.get("_id"), show.get("imdb_id"), ", ".join(bad_top), bad_ep_count,
                    )
                continue

            set_fields: dict = {}
            if top_needs_repair and budget.has_room():
                set_fields.update(await _repair_top_level(show, "tv", budget))

            show_episode_fix_count = 0
            if episode_needs_repair and budget.has_room():
                if _is_manual_record(show):
                    tv_tmdb_id = None
                else:
                    tv_tmdb_id = await _resolve_tmdb_id(show, "tv")
                if tv_tmdb_id:
                    for s_idx, season in enumerate(seasons):
                        if not budget.has_room():
                            break
                        episodes = season.get("episodes") or []
                        for e_idx, ep in enumerate(episodes):
                            if not budget.has_room():
                                break
                            season_number = season.get("season_number", s_idx)
                            episode_number = ep.get("episode_number", e_idx)
                            ep_set_fields = await _repair_episode(
                                show, ep, tv_tmdb_id, season_number, episode_number, budget,
                            )
                            for k, v in ep_set_fields.items():
                                set_fields[f"seasons.{s_idx}.episodes.{e_idx}.{k}"] = v
                            if ep_set_fields:
                                show_episode_fix_count += 1
                elif episode_needs_repair:
                    LOGGER.warning(
                        "[fix_translate_error_text] dizi için TMDB id bulunamadı, bölümler atlanıyor: %s (%s)",
                        show.get("title") or show.get("_id"), show.get("imdb_id"),
                    )

            if set_fields:
                total_tv_fixed += 1
                total_episodes_fixed += show_episode_fix_count
                LOGGER.info(
                    "[fix_translate_error_text] %s | dizi: %s (%s) -> %d alan (bunun %d'i bölüm) TMDB'den onarıldı",
                    db_key, show.get("title") or show.get("_id"), show.get("imdb_id"),
                    len(set_fields), show_episode_fix_count,
                )
                await storage["tv"].update_one({"_id": show["_id"]}, {"$set": set_fields})

    mode = "UYGULANDI" if apply else "DRY-RUN (hiçbir şey yazılmadı, TMDB'ye gidilmedi)"
    LOGGER.info(
        "[fix_translate_error_text] %s | Toplam: %d film + %d dizi (bunlardaki %d bölüm dahil, %d alan onarıldı)",
        mode, total_movies_fixed, total_tv_fixed, total_episodes_fixed, budget.used,
    )
    return total_movies_fixed, total_tv_fixed, total_episodes_fixed


async def _run_cli(apply: bool) -> None:
    await db.connect()
    #----- Elle çalıştırılan CLI, otomatik (bot açılışı) sınırına tabi değildir;
    #----- kullanıcı bilerek tetiklediği için tüm bozuk kayıtları tek seferde işler.
    movies_fixed, tv_fixed, episodes_fixed = await run_fix_translate_error_text(
        apply=apply, max_repairs=None,
    )

    mode = "UYGULANDI" if apply else "DRY-RUN (hiçbir şey yazılmadı)"
    print(
        f"\n[{mode}] Düzeltilen kayıt: "
        f"{movies_fixed} film + {tv_fixed} dizi "
        f"(bunlardaki toplam {episodes_fixed} bölüm dahil)"
    )
    if not apply and (movies_fixed or tv_fixed):
        print("Gerçekten uygulamak için: python -m Backend.scripts.fix_translate_error_text --apply")


if __name__ == "__main__":
    asyncio.run(_run_cli(apply="--apply" in sys.argv))
