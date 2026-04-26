"""
platform_catalog.py
====================
MongoDB'ye doğrudan bağlanarak TV koleksiyonunu okur;
her bölümün telegram[].name alanında geçen platform
etiketlerine göre dizileri gruplar.

Yenileme stratejisi:
  - Uygulama ilk başladığında bir kez yüklenir.
  - Yeni içerik eklendiğinde schedule_refresh() çağrılır.
  - schedule_refresh() 15 dakikalık bir geri sayım başlatır.
  - Geri sayım süresince yeni içerik gelirse sayaç sıfırlanır (debounce).
  - 15 dakika boyunca hiç içerik gelmezse yenileme yapılmaz.
  - Periyodik zamanlayıcı yoktur.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional, Set

logger = logging.getLogger("platform_catalog")

# Yeni içerik eklendikten sonra kaç saniye beklenecek (debounce).
# Bu süre içinde yeni içerik gelirse sayaç sıfırlanır.
REFRESH_DELAY_SEC = int(os.getenv("CATALOG_REFRESH_DELAY_SEC", str(15 * 60)))

# -------------------------------------------------------------------
# Platform etiketleri
# -------------------------------------------------------------------
def _tok(tag: str) -> str:
    return r'(?:^|[.\-_ \[])' + re.escape(tag) + r'(?=[.\-_ \].]|$)'

PLATFORM_PATTERNS: Dict[str, List[re.Pattern]] = {
    "netflix": [re.compile(p, re.IGNORECASE) for p in [_tok("nf"), _tok("netflix")]],
    "disney":  [re.compile(p, re.IGNORECASE) for p in [_tok("dsnp"), _tok("disney"), _tok("disneyplus")]],
    "amazon":  [re.compile(p, re.IGNORECASE) for p in [_tok("amzn"), _tok("amazon")]],
    "hbo":     [re.compile(p, re.IGNORECASE) for p in [_tok("hmax"), _tok("hbo")]],
    "bein":    [re.compile(p, re.IGNORECASE) for p in [_tok("tod"), _tok("bein")]],
    "exxen":   [re.compile(p, re.IGNORECASE) for p in [_tok("exxen")]],
    "gain":    [re.compile(p, re.IGNORECASE) for p in [_tok("gain")]],
    "apple":   [re.compile(p, re.IGNORECASE) for p in [_tok("atvp"), _tok("appletv"), _tok("apple")]],
    "tabii":   [re.compile(p, re.IGNORECASE) for p in [_tok("tabii")]],
    "tvplus":  [re.compile(p, re.IGNORECASE) for p in [_tok("tvplus"), _tok("tv+"), _tok("dsgo"), _tok("tivibu"), _tok("tivibü")]],
}

PLATFORM_LABELS: Dict[str, str] = {
    "netflix": "Netflix",
    "disney":  "Disney+",
    "amazon":  "Amazon Prime",
    "hbo":     "HBO Max",
    "bein":    "Bein/TOD",
    "exxen":   "Exxen",
    "gain":    "Gain",
    "apple":   "Apple TV",
    "tabii":   "Tabii",
    "tvplus":  "TV+",
}


_DB_NAME = os.getenv("MONGO_DB_NAME", "dbFyvio")


def _detect_platform(filename: str) -> Optional[str]:
    for platform, patterns in PLATFORM_PATTERNS.items():
        for pat in patterns:
            if pat.search(filename):
                return platform
    return None


def _doc_to_meta(doc: dict) -> dict:
    return {
        "imdb_id":        doc.get("imdb_id", ""),
        "tmdb_id":        doc.get("tmdb_id"),
        "title":          doc.get("title", ""),
        "title_tr":       doc.get("title_tr", ""),
        "title_de":       doc.get("title_de", ""),
        "poster":         doc.get("poster", ""),
        "poster_tr":      doc.get("poster_tr", ""),
        "poster_de":      doc.get("poster_de", ""),
        "backdrop":       doc.get("backdrop", ""),
        "backdrop_tr":    doc.get("backdrop_tr", ""),
        "logo":           doc.get("logo", ""),
        "logo_tr":        doc.get("logo_tr", ""),
        "genres":         doc.get("genres", []),
        "genres_tr":      doc.get("genres_tr", []),
        "genres_de":      doc.get("genres_de", []),
        "description":    doc.get("description", ""),
        "description_tr": doc.get("description_tr", ""),
        "description_de": doc.get("description_de", ""),
        "rating":         doc.get("rating"),
        "release_year":   doc.get("release_year"),
        "cast":           doc.get("cast", []),
        "runtime":        doc.get("runtime", ""),
        "media_type":     doc.get("media_type", "tv"),
        "collection_id":  doc.get("collection_id"),
        "telegram":       doc.get("telegram", []),
        "language":       doc.get("language", ""),
    }


class PlatformCatalog:
    """
    Platform bazlı katalog yöneticisi.

    Yenileme yalnızca iki durumda tetiklenir:
      1. İlk başlangıç  : refresh() doğrudan çağrılır.
      2. Yeni içerik    : schedule_refresh() → 15 dk debounce → refresh().

    schedule_refresh() her çağrıldığında sayaç sıfırlanır;
    15 dk boyunca hiç içerik gelmezse zamanlayıcı ateşlenmez.
    """

    def __init__(self) -> None:
        self._lock    = threading.RLock()
        self._catalog: Dict[str, Set[str]] = {p: set() for p in PLATFORM_PATTERNS}
        self._meta:    Dict[str, dict]     = {}
        self._collection_ids: Set[str]     = set()
        self._movie_meta: Dict[str, dict]  = {}
        self._loaded  = False

        # Debounce zamanlayıcısı
        self._refresh_timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        # Son schedule_refresh() çağrısının zamanı (debug/log için)
        self._last_schedule_ts: float = 0.0

    # ------------------------------------------------------------------
    # Yenileme tetikleyici (debounce)
    # ------------------------------------------------------------------

    def schedule_refresh(self) -> None:
        """
        Yeni içerik eklendiğinde çağrılır.

        REFRESH_DELAY_SEC saniye sonra refresh() çalıştırır.
        Bu süre içinde tekrar çağrılırsa sayaç sıfırlanır —
        yoğun yükleme seanslarında katalog yalnızca bir kez yenilenir.
        """
        now = time.monotonic()
        with self._timer_lock:
            self._last_schedule_ts = now
            # Bekleyen zamanlayıcıyı iptal et
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()

            self._refresh_timer = threading.Timer(
                REFRESH_DELAY_SEC, self._deferred_refresh
            )
            self._refresh_timer.daemon = True
            self._refresh_timer.name = "platform-catalog-deferred"
            self._refresh_timer.start()

        logger.info(
            "[catalog] Yenileme planlandı — %d dk sonra çalışacak "
            "(yeni içerik gelirse sayaç sıfırlanır).",
            REFRESH_DELAY_SEC // 60,
        )

    def cancel_scheduled_refresh(self) -> None:
        """Bekleyen debounce zamanlayıcısını iptal eder (shutdown için)."""
        with self._timer_lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
                self._refresh_timer = None

    def _deferred_refresh(self) -> None:
        """Zamanlayıcı callback'i — thread güvenli."""
        with self._timer_lock:
            self._refresh_timer = None
        logger.info("[catalog] Debounce süresi doldu, katalog yenileniyor…")
        self.refresh()

    # ------------------------------------------------------------------
    # Ana yenileme
    # ------------------------------------------------------------------

    def refresh(self, db_uris: Optional[List[str]] = None) -> None:
        """
        MongoDB'ye doğrudan bağlanarak TV ve Movie koleksiyonlarını tarar.
        db_uris verilmezse config'den okur.
        """
        try:
            import pymongo
        except ImportError:
            logger.error("pymongo yüklü değil.")
            return

        if db_uris is None:
            try:
                from Backend.config import Telegram
                db_uris = Telegram.DATABASE
            except Exception as e:
                logger.error("Config okunamadı: %s", e)
                return

        if not db_uris:
            logger.error("DATABASE config boş.")
            return

        new_catalog: Dict[str, Set[str]] = {p: set() for p in PLATFORM_PATTERNS}
        new_meta:    Dict[str, dict]     = {}
        new_collection_ids: Set[str]     = set()
        new_movie_meta:     Dict[str, dict] = {}

        storage_uris = db_uris[1:] if len(db_uris) > 1 else db_uris

        _tv_projection = {
            "imdb_id": 1, "tmdb_id": 1,
            "title": 1, "title_tr": 1, "title_de": 1,
            "poster": 1, "poster_tr": 1, "poster_de": 1,
            "backdrop": 1, "backdrop_tr": 1,
            "logo": 1, "logo_tr": 1,
            "genres": 1, "genres_tr": 1, "genres_de": 1,
            "description": 1, "description_tr": 1, "description_de": 1,
            "rating": 1, "release_year": 1,
            "cast": 1, "runtime": 1, "media_type": 1,
            "seasons.episodes.telegram.name": 1,
        }

        for uri in storage_uris:
            try:
                client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=10000)
                mdb = client[_DB_NAME]

                for doc in mdb["tv"].find({}, _tv_projection):
                    imdb_id = doc.get("imdb_id")
                    if not imdb_id:
                        continue
                    if imdb_id not in new_meta:
                        new_meta[imdb_id] = _doc_to_meta(doc)
                    for season in doc.get("seasons", []):
                        for episode in season.get("episodes", []):
                            for quality in episode.get("telegram", []):
                                filename = quality.get("name", "")
                                if not filename:
                                    continue
                                platform = _detect_platform(filename)
                                if platform:
                                    new_catalog[platform].add(imdb_id)

                _movie_proj = {
                    "imdb_id": 1, "tmdb_id": 1,
                    "title": 1, "title_tr": 1, "title_de": 1,
                    "poster": 1, "poster_tr": 1, "poster_de": 1,
                    "backdrop": 1, "backdrop_tr": 1,
                    "logo": 1, "logo_tr": 1,
                    "genres": 1, "genres_tr": 1, "genres_de": 1,
                    "description": 1, "description_tr": 1, "description_de": 1,
                    "rating": 1, "release_year": 1,
                    "cast": 1, "runtime": 1, "media_type": 1,
                    "collection_id": 1,
                    "telegram": 1,
                    "language": 1,
                }
                for doc in mdb["movie"].find({}, _movie_proj):
                    imdb_id = doc.get("imdb_id")
                    if not imdb_id:
                        continue
                    if imdb_id not in new_movie_meta:
                        new_movie_meta[imdb_id] = _doc_to_meta(doc)
                    if doc.get("collection_id"):
                        new_collection_ids.add(imdb_id)
                    for quality in doc.get("telegram", []):
                        filename = quality.get("name", "")
                        if not filename:
                            continue
                        platform = _detect_platform(filename)
                        if platform:
                            new_catalog[platform].add(imdb_id)

                client.close()
            except Exception as e:
                logger.exception("MongoDB bağlantı hatası (%s): %s", uri[:30], e)

        with self._lock:
            self._catalog         = new_catalog
            self._meta            = new_meta
            self._collection_ids  = new_collection_ids
            self._movie_meta      = new_movie_meta
            self._loaded          = True

        logger.info("Dizi kataloğu: %s", {k: len(v) for k, v in new_catalog.items()})
        logger.info("Seri filmleri: %d adet", len(new_collection_ids))

    # ------------------------------------------------------------------
    # Okuma metodları (değişmedi)
    # ------------------------------------------------------------------

    def get(self, platform: str) -> List[dict]:
        with self._lock:
            ids = self._catalog.get(platform, set())
            result = []
            for i in ids:
                if i in self._meta:
                    result.append(self._meta[i])
                elif i in self._movie_meta:
                    result.append(self._movie_meta[i])
            return result

    def get_collection_ids(self) -> Set[str]:
        with self._lock:
            return set(self._collection_ids)

    def get_year_catalog(self, media_type: Optional[str] = None) -> List[dict]:
        with self._lock:
            items: List[dict] = []
            if media_type in (None, "movie"):
                items.extend(self._movie_meta.values())
            if media_type in (None, "tv"):
                items.extend(self._meta.values())
        return items

    def get_collection_movies(self) -> List[dict]:
        with self._lock:
            movies = [self._movie_meta[i] for i in self._collection_ids if i in self._movie_meta]
        movies.sort(key=lambda m: (
            str(m.get("collection_id") or ""),
            int(m.get("release_year") or 0),
        ))
        return movies

    def is_loaded(self) -> bool:
        with self._lock:
            return self._loaded

    def stats(self) -> Dict[str, int]:
        with self._lock:
            result = {p: len(v) for p, v in self._catalog.items()}
            result["collections"] = len(self._collection_ids)
            return result


platform_catalog = PlatformCatalog()
