"""
tmdb_catalog.py
================
TMDB'den trendler verisini çeker,
MongoDB veritabanındaki içeriklerle karşılaştırarak
sadece veritabanında bulunanları katalog olarak sunar.

2 katalog üretir:
  - trending     : Haftalık trendler (film + dizi, global + TR birleşik, tekrarsız)

- Bot yeniden başlayınca ilk yükleme yapılır.
- Yeni içerik eklendiğinde notify_new_content() çağrılır.
- Son yeni içerik eklenmesinden 30 dakika (TMDB_REFRESH_MIN) sonra güncellenir.
- Yeni içerik gelmezse güncelleme yapılmaz.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("tmdb_catalog")

_TMDB_BASE = "https://api.themoviedb.org/3"
_INTERVAL_SECONDS = int(os.getenv("TMDB_REFRESH_MIN", "30")) * 60


def _get_api_key() -> str:
    try:
        from Backend.config import Telegram
        return Telegram.TMDB_API
    except Exception:
        return os.getenv("TMDB_API", "")


# ──────────────────────────────────────────────────────────────────────────────
# TMDB API yardımcıları
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_pages(endpoint: str, params: dict, max_pages: int = 3) -> List[dict]:
    api_key = _get_api_key()
    if not api_key:
        logger.warning("TMDB_API anahtarı tanımlı değil.")
        return []
    results: List[dict] = []
    for page in range(1, max_pages + 1):
        try:
            with httpx.Client(timeout=15) as c:
                r = c.get(
                    f"{_TMDB_BASE}/{endpoint}",
                    params={**params, "api_key": api_key, "page": page},
                )
                r.raise_for_status()
                data = r.json()
                results.extend(data.get("results", []))
                if page >= data.get("total_pages", 1):
                    break
        except Exception as e:
            logger.warning("TMDB isteği başarısız (%s sayfa %d): %s", endpoint, page, e)
            break
    return results


def _imdb_id_from_tmdb(tmdb_id: int, media_type: str) -> Optional[str]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        ep = "movie" if media_type == "movie" else "tv"
        with httpx.Client(timeout=10) as c:
            r = c.get(
                f"{_TMDB_BASE}/{ep}/{tmdb_id}/external_ids",
                params={"api_key": api_key},
            )
            r.raise_for_status()
            return r.json().get("imdb_id") or None
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# DB yardımcıları
# ──────────────────────────────────────────────────────────────────────────────

def _load_db_map(collection_name: str) -> Dict[str, dict]:
    """imdb_id → doc  ve  tmdb_<id> → doc  haritası döndürür."""
    try:
        from Backend.config import Telegram
        uris = Telegram.DATABASE
    except Exception:
        return {}
    if not uris:
        return {}
    try:
        import pymongo
    except ImportError:
        logger.error("pymongo yüklü değil.")
        return {}

    projection = {
        "imdb_id": 1, "tmdb_id": 1,
        "title": 1, "title_tr": 1, "title_de": 1,
        "poster": 1, "poster_tr": 1, "poster_de": 1,
        "backdrop": 1, "backdrop_tr": 1,
        "logo": 1, "logo_tr": 1,
        "genres": 1, "genres_tr": 1, "genres_de": 1,
        "description": 1, "description_tr": 1, "description_de": 1,
        "rating": 1, "release_year": 1,
        "cast": 1, "runtime": 1, "media_type": 1,
        # Film kaliteleri (stream kontrolü için)
        "telegram.name": 1, "telegram.quality": 1,
        "telegram.size": 1, "telegram.id": 1, "telegram.is_archive": 1,
        # Dizi stream kontrolü için seasons yapısı
        "seasons.episodes.telegram.name": 1,
        "seasons.episodes.telegram.is_archive": 1,
    }
    db_name = os.getenv("MONGO_DB_NAME", "dbFyvio")
    # Tüm URI'leri tara — ilkini atlama, içerik her DB'de olabilir
    result: Dict[str, dict] = {}
    for uri in uris:
        try:
            client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=10000)
            for doc in client[db_name][collection_name].find({}, projection):
                iid = doc.get("imdb_id")
                if iid:
                    result[iid] = doc
                tid = doc.get("tmdb_id")
                if tid:
                    result[f"tmdb_{tid}"] = doc
            client.close()
        except Exception as e:
            logger.warning("DB bağlantı hatası (%s): %s", uri[:30], e)
    return result


def _doc_to_meta(doc: dict) -> dict:
    # Film icin telegram -> qualities donusumu
    qualities = []
    for q in doc.get("telegram", []):
        if q.get("id"):
            qualities.append({
                "quality": q.get("quality", ""),
                "name":    q.get("name", ""),
                "size":    q.get("size", ""),
                "id":      q.get("id"),
            })
    return {
        "imdb_id":        doc.get("imdb_id", ""),
        "tmdb_id":        doc.get("tmdb_id"),
        "db_index":       doc.get("db_index"),
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
        "media_type":     doc.get("media_type", "movie"),
        "qualities":      qualities,
        # _has_video_stream() için gerekli ham alanlar
        "telegram":       doc.get("telegram", []),
        "seasons":        doc.get("seasons", []),
    }


def _match(
    tmdb_items: List[dict],
    db_movie: Dict[str, dict],
    db_tv: Dict[str, dict],
    seen: set,
    force_type: Optional[str] = None,
    min_year: Optional[int] = None,
) -> List[dict]:
    """
    TMDB listesini her iki DB haritasıyla karşılaştırır.
    Eşleşen ve daha önce görülmemiş (seen) kayıtları döndürür.

    force_type: None ise her item'in kendi media_type'ına bakılır.
                "movie" veya "tv" ise tüm item'lar o tip olarak işlenir
                (setdefault ile üzerine yazılmış olsa bile TMDB'nin
                 orijinal media_type alanı varsa o kullanılır).

    min_year:   Belirtilirse TMDB'nin release_date / first_air_date yılı
                bu değerden küçük olan içerikler atlanır. Eski filmlerin
                yeni çıkanlar kataloğuna sızmasını önler (gerektiğinde kullanılabilir).
    """
    matched: List[dict] = []
    for item in tmdb_items:
        tid = item.get("id")
        if not tid:
            continue

        # ── Yayın yılı filtresi (yalnızca min_year verilmişse) ──────────────
        if min_year is not None:
            date_str = item.get("release_date") or item.get("first_air_date") or ""
            if date_str:
                try:
                    item_year = int(date_str[:4])
                    if item_year < min_year:
                        continue
                except (ValueError, TypeError):
                    pass  # Tarih parse edilemezse geç, filtreleme
        # ────────────────────────────────────────────────────────────────────

        # media_type'ı doğru belirle:
        # TMDB'nin kendi alanı varsa önceliklidir, yoksa force_type kullan
        raw_type = item.get("media_type") or force_type or (
            "movie" if "title" in item else "tv"
        )
        # TMDB zaman zaman "person" döndürebilir, atla
        if raw_type not in ("movie", "tv"):
            continue

        db_map = db_movie if raw_type == "movie" else db_tv

        # 1. tmdb_id ile direkt eşleştir
        doc = db_map.get(f"tmdb_{tid}")

        # 2. Bulunamazsa diğer koleksiyona da bak (tip tahmini yanlış olabilir)
        if doc is None:
            alt_map = db_tv if raw_type == "movie" else db_movie
            doc = alt_map.get(f"tmdb_{tid}")

        # 3. Hâlâ bulunamazsa TMDB → IMDb ID dönüşümü dene
        if doc is None:
            iid = _imdb_id_from_tmdb(tid, raw_type)
            if iid:
                doc = db_map.get(iid) or db_tv.get(iid) or db_movie.get(iid)

        if doc is None:
            continue

        imdb_id = doc.get("imdb_id", "")
        if not imdb_id or imdb_id in seen:
            continue
        seen.add(imdb_id)
        matched.append(_doc_to_meta(doc))
    return matched


# ──────────────────────────────────────────────────────────────────────────────
# Ana katalog sınıfı
# ──────────────────────────────────────────────────────────────────────────────

class TmdbCatalog:
    """
    1 birleşik katalog:
      trending     — haftalık trendler  (film+dizi, global+TR, tekrarsız)

    Sadece veritabanında mevcut olan içerikler döner.
    """

    def __init__(self) -> None:
        self._lock          = threading.RLock()
        self._trending:     List[dict] = []
        self._loaded        = False
        self._last_refresh: float = 0.0

    def refresh(self) -> None:
        logger.info("TMDB kataloğu yenileniyor…")
        t0 = time.time()

        movie_db = _load_db_map("movie")
        tv_db    = _load_db_map("tv")

        # ── Trendler: global film → global dizi → TR film → TR dizi ──────────
        seen_trend: set = set()
        trend: List[dict] = []
        for endpoint, raw_type, extra_params in [
            ("trending/movie/week", "movie", {}),
            ("trending/tv/week",    "tv",    {}),
            ("trending/movie/week", "movie", {"region": "TR"}),
            ("trending/tv/week",    "tv",    {"region": "TR"}),
        ]:
            items = _fetch_pages(endpoint, {"language": "tr-TR", **extra_params})
            # Trending endpoint'leri kendi media_type'ını içeriyor — setdefault
            # ile yalnızca eksik olanlara raw_type ata, mevcut olanları koru
            for it in items:
                it.setdefault("media_type", raw_type)
            trend.extend(_match(items, movie_db, tv_db, seen_trend))

        with self._lock:
            self._trending     = trend
            self._loaded       = True
            self._last_refresh = time.time()

        logger.info(
            "TMDB kataloğu hazır (%.1fs) — trendler: %d",
            time.time() - t0, len(trend),
        )

    def get_trending(self) -> List[dict]:
        with self._lock:
            return list(self._trending)

    def is_loaded(self) -> bool:
        with self._lock:
            return self._loaded

    def last_refresh_ts(self) -> float:
        with self._lock:
            return self._last_refresh

    def stats(self) -> dict:
        with self._lock:
            return {
                "trending":     len(self._trending),
            }


# ──────────────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────────────

tmdb_catalog = TmdbCatalog()


# ──────────────────────────────────────────────────────────────────────────────
# Zamanlayıcı  (içerik-tetiklemeli)
#
# Çalışma mantığı:
#   • notify_new_content() her yeni içerik eklendiğinde çağrılır.
#   • Bu çağrı, "_last_content_added_at" zaman damgasını günceller ve
#     mevcut zamanlayıcıyı iptal edip INTERVAL_SECONDS sonraya yeni bir
#     zamanlayıcı kurar ("debounce" etkisi).
#   • Zamanlayıcı tetiklendiğinde refresh() çalışır.
#   • Hiç yeni içerik gelmemişse zamanlayıcı kurulmaz → güncelleme olmaz.
# ──────────────────────────────────────────────────────────────────────────────

_tmdb_timer:             threading.Timer | None = None
_tmdb_running:           bool = False
_last_content_added_at:  float = 0.0   # son notify_new_content() çağrısının zamanı
_tmdb_lock:              threading.Lock = threading.Lock()


def _tmdb_fire() -> None:
    """Zamanlayıcı süresi dolunca çalışır; refresh yapar."""
    if not _tmdb_running:
        return
    try:
        tmdb_catalog.refresh()
    except Exception as e:
        logger.exception("TMDB yenileme hatası: %s", e)


def _arm_timer() -> None:
    """
    Mevcut zamanlayıcıyı iptal edip INTERVAL_SECONDS sonraya yeni birini kurar.
    _tmdb_lock altında çağrılmalıdır.
    """
    global _tmdb_timer
    if _tmdb_timer is not None:
        _tmdb_timer.cancel()
    _tmdb_timer = threading.Timer(_INTERVAL_SECONDS, _tmdb_fire)
    _tmdb_timer.daemon = True
    _tmdb_timer.start()


def notify_new_content() -> None:
    """
    Yeni bir içerik eklendiğinde çağrılır.

    Her çağrı zamanlayıcıyı sıfırlar (debounce):
    son çağrıdan INTERVAL_SECONDS sonra katalog güncellenir.
    Aynı süre içinde birden fazla içerik eklenirse zamanlayıcı
    yalnızca bir kez ateşlenir.
    """
    global _last_content_added_at
    if not _tmdb_running:
        return
    with _tmdb_lock:
        _last_content_added_at = time.time()
        _arm_timer()
    logger.debug(
        "TMDB: yeni içerik bildirimi alındı, %d dk sonra güncellenecek.",
        _INTERVAL_SECONDS // 60,
    )


def start_tmdb_scheduler() -> None:
    """
    Zamanlayıcıyı başlatır ve ilk yüklemeyi arka planda yapar.
    İlk yükleme her zaman çalışır (bot yeni başlamış, katalog boş).
    Sonraki güncellemeler yalnızca notify_new_content() ile tetiklenir.
    """
    global _tmdb_running
    _tmdb_running = True

    def _first_run():
        logger.info("TMDB kataloğu ilk kez yükleniyor…")
        try:
            tmdb_catalog.refresh()
        except Exception as e:
            logger.exception("TMDB ilk yükleme hatası: %s", e)

    t = threading.Thread(target=_first_run, daemon=True, name="tmdb-catalog-init")
    t.start()
    logger.info(
        "TMDB zamanlayıcısı başlatıldı — yeni içerik eklenince %d dk sonra güncellenir.",
        _INTERVAL_SECONDS // 60,
    )


def stop_tmdb_scheduler() -> None:
    global _tmdb_running, _tmdb_timer
    _tmdb_running = False
    with _tmdb_lock:
        if _tmdb_timer:
            _tmdb_timer.cancel()
            _tmdb_timer = None
    logger.info("TMDB zamanlayıcısı durduruldu.")
