import httpx
import re
import asyncio
import unicodedata
from difflib import SequenceMatcher
from typing import Optional, Dict, Any

BASE_URL = "https://v3-cinemeta.strem.io"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True
            )
        return _client

def extract_first_year(year_string) -> int:
    if not year_string:
        return 0
    year_str = str(year_string)
    year_match = re.search(r'(\d{4})', year_str)
    if year_match:
        return int(year_match.group(1))
    return 0

# Türkçe karakterleri ASCII karşılıklarına indirger (başlık karşılaştırması için)
_TR_CHAR_MAP = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
})

# Yıl uyuşmasa bile başlığı bu oranın üzerinde benzeyen sonuç kabul edilir
# (ör. dosya adındaki yıl dizinin ilk yayın yılı değil, sezon yılı olduğunda).
TITLE_MATCH_THRESHOLD = 0.9


def normalize_title(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").translate(_TR_CHAR_MAP))
    text = text.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def title_similarity(a, b) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def pick_best_meta(metas: list, query: str, year: int, prefer_year: bool = False) -> Optional[Dict[str, Any]]:
    """
    Cinemeta arama sonuçları arasından dosya adındaki başlık + yıla en uygun olanı seçer.
    Ne yıl (±1) ne de başlık uyuyorsa None döner; böylece alakasız bir dizi/film
    (ör. "Bir Ege Hayali" yerine "Bir Zamanlar Çukurova") yanlışlıkla eşleşmez.

    prefer_year=True (filmler için): yılı uyan (±1) en az bir aday varsa yalnızca
    onlar değerlendirilir; başlığı benzeyen ama yılı tutmayan aday (ör. aynı/benzer
    adlı başka bir film) yıl uyan adayın önüne geçemez. Latin harfe indirgenemeyen
    adlar (ör. Farsça "هرجایی") karşılaştırılamayacağı için nötr benzerlik alır;
    bu durumda yıl + Cinemeta sıralaması belirleyicidir.
    """
    scored = []
    for idx, meta in enumerate(metas[:10]):
        name = meta.get("name", "")
        sim = title_similarity(query, name)
        if prefer_year and not normalize_title(name):
            sim = 0.5
        meta_year = extract_first_year(meta.get("releaseInfo") or meta.get("year"))
        if meta_year:
            year_ok = abs(meta_year - year) <= 1
        else:
            year_ok = None  # yıl bilgisi yok → yıl üzerinden elenmez

        if year_ok is False and sim < TITLE_MATCH_THRESHOLD:
            continue

        score = sim + (0.5 if year_ok else 0.0) - idx * 0.001
        scored.append((meta, score, year_ok))

    if prefer_year and any(ok is True for _, _, ok in scored):
        scored = [t for t in scored if t[2] is True]

    if not scored:
        return None
    return max(scored, key=lambda t: t[1])[0]


async def search_title(query: str, type: str, year: Optional[int] = None, prefer_year: bool = False) -> Optional[Dict[str, Any]]:
    client = await _get_client()
    cinemeta_type = "series" if type == "tvSeries" else type
    url = f"{BASE_URL}/catalog/{cinemeta_type}/imdb/search={query}.json"
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data and 'metas' in data and data['metas']:
            metas = data['metas']
            # Yıl verilmişse sonuçları başlık+yıl ile doğrula; verilmemişse
            # eski davranış (ilk sonuç) korunur.
            # Sorgu "<başlık> <yıl>" biçiminde gelebilir (Cinemeta yıllı sorguda daha
            # iyi sıralıyor); benzerlik hesabında sondaki yıl başlığa dahil edilmez.
            sim_query = re.sub(rf"\s+{int(year)}\s*$", "", query) if year else query
            meta = pick_best_meta(metas, sim_query, int(year), prefer_year) if year else metas[0]
            if not meta:
                return None
            return {
                'id': meta.get('imdb_id', meta.get('id', '')),
                'type': type,
                'title': meta.get('name', ''),
                'year': meta.get('releaseInfo', ''),
                'poster': meta.get('poster', '')
            }
        return None
    except Exception:
        return None

async def get_detail(imdb_id: str, media_type: str) -> Optional[Dict[str, Any]]:
    client = await _get_client()
    cinemeta_type = "series" if media_type in ["tvSeries", "tv"] else "movie"

    try:
        url = f"{BASE_URL}/meta/{cinemeta_type}/{imdb_id}.json"
        resp = await client.get(url)

        if resp.status_code != 200:
            return None

        data = resp.json()
        meta = data.get("meta")
        if not meta:
            return None

        year_value = 0
        for field in ["year", "releaseInfo", "released"]:
            if meta.get(field):
                year_value = extract_first_year(meta[field])
                if year_value:
                    break

        return {
            "id": meta.get("imdb_id") or meta.get("id"),
            "moviedb_id": meta.get("moviedb_id"),
            "type": meta.get("type", media_type),
            "title": meta.get("name", ""),
            "plot": meta.get("description", ""),
            "genre": meta.get("genres") or meta.get("genre", []),
            "releaseDetailed": {"year": year_value},
            "rating": {"star": float(meta.get("imdbRating", 0) or 0)},
            "poster": meta.get("poster", ""),
            "background": meta.get("background", ""),
            "logo": meta.get("logo", ""),
            "runtime": meta.get("runtime") or 0,
            "director": meta.get("director", []),
            "cast": meta.get("cast", []),
            "videos": meta.get("videos", [])
        }

    except Exception:
        return None

async def get_season(imdb_id: str, season_id: int, episode_id: int) -> Optional[Dict[str, Any]]:
    client = await _get_client()
    try:
        url = f"{BASE_URL}/meta/series/{imdb_id}.json"
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if 'meta' in data and 'videos' in data['meta']:
            for video in data['meta']['videos']:
                if (str(video.get('season', '')) == str(season_id) and
                        str(video.get('episode', '')) == str(episode_id)):
                    return {
                        'title': video.get('title', f'Episode {episode_id}'),
                        'no': str(episode_id),
                        'season': str(season_id),
                        'image': video.get('thumbnail', ''),
                        'plot': video.get('overview', ''),
                        'released': video.get('released', '')
                    }
        return None
    except Exception:
        return None