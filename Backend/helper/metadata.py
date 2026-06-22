import asyncio
import traceback
import httpx
import PTN
import re
from re import compile, IGNORECASE
from Backend.helper.imdb import get_detail, get_season, search_title
from themoviedb import aioTMDb
from Backend.config import Telegram
import Backend
from Backend.logger import LOGGER
from Backend.helper.encrypt import encode_string
from Backend.helper.split_files import parse_split_info
from deep_translator import GoogleTranslator

# ----------------- Configuration -----------------
DELAY = 0
tmdb_tr = aioTMDb(key=Telegram.TMDB_API, language="tr-TR", region="TR")
tmdb_de = aioTMDb(key=Telegram.TMDB_API, language="de-DE", region="DE")
tmdb = tmdb_tr  # geriye dönük uyumluluk

# ── LRU Cache ─────────────────────────────────────────────────────────────────
# Bellekte sabit boyut tutmak için OrderedDict tabanlı basit LRU.
# Sınırsız dict yerine kullanılır; en uzun süredir erişilmeyen giriş otomatik düşer.
from collections import OrderedDict

class _LRUCache(OrderedDict):
    """Thread-safe olmayan, asyncio ortamı için yeterli LRU cache."""
    def __init__(self, maxsize: int = 1000):
        super().__init__()
        self.maxsize = maxsize

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)   # en son kullanılan sona geçer
        return value

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            self.popitem(last=False)  # en eski (baştaki) silinir

    def get(self, key, default=None):
        if key in self:
            return self[key]   # move_to_end tetiklenir
        return default

# Cache dictionaries (per run)
#   maxsize değerleri kütüphane büyüklüğüne göre ayarlanabilir:
#   - IMDB_CACHE        : başlık → IMDb ID  (küçük veri, yüksek tekrar)
#   - TMDB_SEARCH_CACHE : arama sonuçları   (küçük-orta veri)
#   - TMDB_DETAILS_CACHE: film/dizi detayı  (büyük veri — poster, oyuncu, sertifika)
#   - EPISODE_CACHE     : bölüm detayları   (orta veri, çok sayıda giriş olabilir)
#   - TRANSLATE_CACHE   : TR çeviri         (küçük veri, metin başına bir giriş)
#   - TRANSLATE_DE_CACHE: DE çeviri         (küçük veri)
IMDB_CACHE:         _LRUCache = _LRUCache(maxsize=2000)
TMDB_SEARCH_CACHE:  _LRUCache = _LRUCache(maxsize=2000)
TMDB_DETAILS_CACHE: _LRUCache = _LRUCache(maxsize=2000)
EPISODE_CACHE:      _LRUCache = _LRUCache(maxsize=5000)
TRANSLATE_CACHE:    _LRUCache = _LRUCache(maxsize=3000)
TRANSLATE_DE_CACHE: _LRUCache = _LRUCache(maxsize=3000)

# Aynı anda aynı içeriğe gelen paralel metadata çağrılarını tek sefere indirger.
# key → asyncio.Future  (sonuç gelince tüm bekleyenler aynı Future'ı okur)
_METADATA_IN_FLIGHT: dict = {}

GENRE_TUR_ALIASES = {
  "action": "Aksiyon",
  "sci-fi": "Bilim Kurgu",
  "science fiction": "Bilim Kurgu",
  "film-noir": "Kara Film",
  "game-show": "Oyun Gösterisi",
  "short": "Kısa",
  "sport": "Spor",
  "adventure": "Macera",
  "animation": "Animasyon",
  "biography": "Biyografi",
  "comedy": "Komedi",
  "crime": "Suç",
  "documentary": "Belgesel",
  "drama": "Dram",
  "family": "Aile",
  "news": "Haberler",
  "fantasy": "Fantastik",
  "history": "Tarih",
  "horror": "Korku",
  "music": "Müzik",
  "musical": "Müzikal",
  "mystery": "Gizem",
  "romance": "Romantik",
  "tv movie": "TV Filmi",
  "thriller": "Gerilim",
  "war": "Savaş",
  "western": "Vahşi Batı",
  "action & adventure": "Aksiyon ve Macera",
  "kids": "Çocuklar",
  "reality": "Gerçeklik",
  "reality-tv": "Gerçeklik",
  "sci-fi & fantasy": "Bilim Kurgu ve Fantazi",
  "soap": "Pembe Dizi",
  "war & politics": "Savaş ve Politika",
  "talk": "Talk-Show",
}


GENRE_DE_ALIASES = {
  "action": "Action",
  "sci-fi": "Science-Fiction",
  "science fiction": "Science-Fiction",
  "film-noir": "Film Noir",
  "game-show": "Game-Show",
  "short": "Kurzfilm",
  "sport": "Sport",
  "adventure": "Abenteuer",
  "animation": "Animation",
  "biography": "Biografie",
  "comedy": "Komödie",
  "crime": "Krimi",
  "documentary": "Dokumentarfilm",
  "drama": "Drama",
  "family": "Familie",
  "news": "Nachrichten",
  "fantasy": "Fantasy",
  "history": "Geschichte",
  "horror": "Horror",
  "music": "Musik",
  "musical": "Musical",
  "mystery": "Mystery",
  "romance": "Romantik",
  "tv movie": "TV-Film",
  "thriller": "Thriller",
  "war": "Krieg",
  "western": "Western",
  "action & adventure": "Action & Abenteuer",
  "kids": "Kinder",
  "reality": "Reality-TV",
  "reality-tv": "Reality-TV",
  "sci-fi & fantasy": "Science-Fiction & Fantasy",
  "soap": "Seifenoper",
  "war & politics": "Krieg & Politik",
  "talk": "Talkshow",
}

# Concurrency semaphore for external API calls
API_SEMAPHORE = asyncio.Semaphore(12)

# ----------------- Helpers -----------------
def format_tmdb_image(path: str, size="w500") -> str:
    if not path:
        return ""
    return f"https://image.tmdb.org/t/p/{size}{path}"

def get_tmdb_logo(images) -> str:
    if not images:
        return ""
    logos = getattr(images, "logos", None)
    if not logos:
        return ""
    for logo in logos:
        iso_lang = getattr(logo, "iso_639_1", None)
        file_path = getattr(logo, "file_path", None)
        if iso_lang == "en" and file_path:
            return format_tmdb_image(file_path, "w300")
    for logo in logos:
        file_path = getattr(logo, "file_path", None)
        if file_path:
            return format_tmdb_image(file_path, "w300")
    return ""


def get_lang_images(images, lang: str) -> dict:
    """TMDB images nesnesinden belirtilen dile ait poster/backdrop/logo döndürür."""
    result = {"poster": "", "backdrop": "", "logo": ""}
    if not images:
        return result
    for p in getattr(images, "posters", []):
        if getattr(p, "iso_639_1", None) == lang and getattr(p, "file_path", None):
            result["poster"] = format_tmdb_image(p.file_path, "w500")
            break
    for b in getattr(images, "backdrops", []):
        if getattr(b, "iso_639_1", None) == lang and getattr(b, "file_path", None):
            result["backdrop"] = format_tmdb_image(b.file_path, "original")
            break
    for l in getattr(images, "logos", []):
        if getattr(l, "iso_639_1", None) == lang and getattr(l, "file_path", None):
            result["logo"] = format_tmdb_image(l.file_path, "w300")
            break
    return result
    

def format_imdb_images(imdb_id: str) -> dict:
    if not imdb_id:
        return {"poster": "", "backdrop": "", "logo": ""}
    return {
        "poster": f"https://images.metahub.space/poster/small/{imdb_id}/img",
        "backdrop": f"https://images.metahub.space/background/medium/{imdb_id}/img",
        "logo": f"https://images.metahub.space/logo/medium/{imdb_id}/img",
    }

def extract_default_id(url: str) -> tuple[str | None, str | None]:
    """
    Returns (id, media_type) where media_type is 'movie', 'tv', or None.
    - IMDb tt-IDs     -> (tt..., None)
    - TMDb /tv/...    -> (id, 'tv')
    - TMDb /movie/... -> (id, 'movie')
    - Plain digit str -> (id, None)
    """
    if not url:
        return None, None

    # IMDb
    imdb_match = re.search(r'/title/(tt\d+)', url)
    if imdb_match:
        return imdb_match.group(1), None

    # TMDb – captures the type segment too
    tmdb_match = re.search(r'/(movie|tv)/(\d+)', url)
    if tmdb_match:
        return tmdb_match.group(2), tmdb_match.group(1)  # (id, 'movie'|'tv')

    # Plain numeric id with no path context
    plain = url.strip()
    if re.fullmatch(r'\d+', plain):
        return plain, None

    return None, None

async def safe_imdb_search(title: str, type_: str) -> str | None:
    key = f"imdb::{type_}::{title}"
    if key in IMDB_CACHE:
        return IMDB_CACHE[key]
    try:
        async with API_SEMAPHORE:
            result = await search_title(query=title, type=type_)
        imdb_id = result["id"] if result else None
        IMDB_CACHE[key] = imdb_id
        return imdb_id
    except Exception as e:
        LOGGER.warning(f"IMDb search failed for '{title}' [{type_}]: {e}")
        return None

async def safe_tmdb_search(title: str, type_: str, year=None):
    key = f"tmdb_search::{type_}::{title}::{year}"
    if key in TMDB_SEARCH_CACHE:
        return TMDB_SEARCH_CACHE[key]
    try:
        async with API_SEMAPHORE:
            if type_ == "movie":
                results = await tmdb.search().movies(query=title, year=year) if year else await tmdb.search().movies(query=title)
            else:
                results = await tmdb.search().tv(query=title)
        res = results[0] if results else None
        TMDB_SEARCH_CACHE[key] = res
        return res
    except Exception as e:
        LOGGER.error(f"TMDb search failed for '{title}' [{type_}]: {e}")
        TMDB_SEARCH_CACHE[key] = None
        return None


async def _fetch_tmdb_images(media_type: str, tmdb_id: int):
    """
    TMDB images endpoint'ini doğrudan httpx ile çağırır.
    include_image_language=tr,de,en,null ile tüm dilleri alır.
    themoviedb kütüphanesi bu parametreyi desteklemediği için httpx kullanılır.
    """
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/images"
    params = {
        "api_key": Telegram.TMDB_API,
        "include_image_language": "tr,de,en,null",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 200:
            raw = resp.json()
            class _Images:
                pass
            class _Img:
                pass
            def _make_image(d):
                obj = _Img()
                obj.iso_639_1 = d.get("iso_639_1")
                obj.file_path = d.get("file_path")
                obj.vote_average = d.get("vote_average", 0)
                return obj
            obj = _Images()
            obj.posters = [_make_image(p) for p in raw.get("posters", [])]
            obj.backdrops = [_make_image(b) for b in raw.get("backdrops", [])]
            obj.logos = [_make_image(l) for l in raw.get("logos", [])]
            return obj
    except Exception as e:
        LOGGER.warning(f"TMDB images fetch failed for {media_type}/{tmdb_id}: {e}")
    return None


async def _resolve_tmdb_id_from_imdb(imdb_id: str, media_type: str) -> int | None:
    """
    IMDb ID'den TMDB ID'sini bulur (find endpoint ile).
    media_type: "movie" veya "tv"
    """
    if not imdb_id:
        return None
    url = f"https://api.themoviedb.org/3/find/{imdb_id}"
    params = {
        "api_key": Telegram.TMDB_API,
        "external_source": "imdb_id",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 200:
            raw = resp.json()
            if media_type == "movie":
                results = raw.get("movie_results", [])
            else:
                results = raw.get("tv_results", [])
            if results:
                return results[0].get("id")
    except Exception as e:
        LOGGER.warning(f"TMDB find by IMDb ID failed [{imdb_id}]: {e}")
    return None

async def _fetch_tmdb_movie_certifications(tmdb_id: int) -> dict:
    """
    Film için TMDB release_dates endpoint'inden TR, DE ve US sertifikalarını çeker.
    Dönen dict: {"certification_tr": "...", "certification_de": "...", "certification_us": "..."}
    """
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates"
    params = {"api_key": Telegram.TMDB_API}
    result = {"certification_tr": None, "certification_de": None, "certification_us": None}
    country_map = {"TR": "certification_tr", "DE": "certification_de", "US": "certification_us"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 200:
            raw = resp.json()
            for entry in raw.get("results", []):
                iso = entry.get("iso_3166_1")
                if iso in country_map:
                    releases = entry.get("release_dates", [])
                    cert = next((r.get("certification", "") for r in releases if r.get("certification")), "")
                    if cert:
                        result[country_map[iso]] = cert
    except Exception as e:
        LOGGER.warning(f"TMDB movie certifications fetch failed [{tmdb_id}]: {e}")
    return result


async def _fetch_tmdb_tv_certifications(tmdb_id: int) -> dict:
    """
    Dizi için TMDB content_ratings endpoint'inden TR, DE ve US sertifikalarını çeker.
    Dönen dict: {"certification_tr": "...", "certification_de": "...", "certification_us": "..."}
    """
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/content_ratings"
    params = {"api_key": Telegram.TMDB_API}
    result = {"certification_tr": None, "certification_de": None, "certification_us": None}
    country_map = {"TR": "certification_tr", "DE": "certification_de", "US": "certification_us"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 200:
            raw = resp.json()
            for entry in raw.get("results", []):
                iso = entry.get("iso_3166_1")
                if iso in country_map:
                    rating = entry.get("rating", "")
                    if rating:
                        result[country_map[iso]] = rating
    except Exception as e:
        LOGGER.warning(f"TMDB tv content_ratings fetch failed [{tmdb_id}]: {e}")
    return result


async def _tmdb_movie_details(movie_id):
    if movie_id in TMDB_DETAILS_CACHE:
        return TMDB_DETAILS_CACHE[movie_id]
    try:
        async with API_SEMAPHORE:
            # details() parametresiz; dil tmdb_tr init'inden geliyor (tr-TR)
            details = await tmdb_tr.movie(movie_id).details()
        # images: doğrudan httpx ile çağır → tr,de,en,null dahil
        images = await _fetch_tmdb_images("movie", movie_id)
        details.images = images

        # external_ids ayrı çağrı
        try:
            async with API_SEMAPHORE:
                ext_ids = await tmdb_tr.movie(movie_id).external_ids()
            details.external_ids = ext_ids
        except Exception:
            details.external_ids = None

        # credits ayrı çağrı
        try:
            async with API_SEMAPHORE:
                credits_data = await tmdb_tr.movie(movie_id).credits()
            details.credits = credits_data
        except Exception:
            details.credits = None

        # Almanca başlık, açıklama ve türler (tmdb_de nesnesi de-DE ile init edildi)
        try:
            async with API_SEMAPHORE:
                details_de = await tmdb_de.movie(movie_id).details()
            details.title_de = getattr(details_de, "title", "") or getattr(details_de, "original_title", "")
            details.overview_de = getattr(details_de, "overview", "")
            details.genres_de = de_genre_normalize([g.name for g in (getattr(details_de, "genres", None) or [])])
        except Exception:
            details.title_de = ""
            details.overview_de = ""
            details.genres_de = []

        # Türkçe ve Almanca dil bazlı görseller
        tr_imgs = get_lang_images(details.images, "tr")
        de_imgs = get_lang_images(details.images, "de")
        details.poster_tr = tr_imgs["poster"]
        details.backdrop_tr = tr_imgs["backdrop"]
        details.logo_tr = tr_imgs["logo"]
        details.poster_de = de_imgs["poster"]
        details.backdrop_de = de_imgs["backdrop"]
        details.logo_de = de_imgs["logo"]

        # Sertifikalar (TR, DE, US)
        try:
            certs = await _fetch_tmdb_movie_certifications(movie_id)
            details.certification_tr = certs.get("certification_tr")
            details.certification_de = certs.get("certification_de")
            details.certification_us = certs.get("certification_us")
        except Exception:
            details.certification_tr = None
            details.certification_de = None
            details.certification_us = None

        TMDB_DETAILS_CACHE[movie_id] = details
        return details
    except Exception as e:
        LOGGER.warning(f"TMDb movie details fetch failed for id={movie_id}: {e}")
        TMDB_DETAILS_CACHE[movie_id] = None
        return None


async def _tmdb_tv_details(tv_id):
    if tv_id in TMDB_DETAILS_CACHE:
        return TMDB_DETAILS_CACHE[tv_id]
    try:
        async with API_SEMAPHORE:
            # details() parametresiz; dil tmdb_tr init'inden geliyor (tr-TR)
            details = await tmdb_tr.tv(tv_id).details()
        # images: doğrudan httpx ile çağır → tr,de,en,null dahil
        images = await _fetch_tmdb_images("tv", tv_id)
        details.images = images

        # external_ids ayrı çağrı
        try:
            async with API_SEMAPHORE:
                ext_ids = await tmdb_tr.tv(tv_id).external_ids()
            details.external_ids = ext_ids
        except Exception:
            details.external_ids = None

        # credits ayrı çağrı
        try:
            async with API_SEMAPHORE:
                credits_data = await tmdb_tr.tv(tv_id).credits()
            details.credits = credits_data
        except Exception:
            details.credits = None

        # Almanca başlık, açıklama ve türler (tmdb_de nesnesi de-DE ile init edildi)
        try:
            async with API_SEMAPHORE:
                details_de = await tmdb_de.tv(tv_id).details()
            details.name_de = getattr(details_de, "name", "") or getattr(details_de, "original_name", "")
            details.overview_de = getattr(details_de, "overview", "")
            details.genres_de = de_genre_normalize([g.name for g in (getattr(details_de, "genres", None) or [])])
        except Exception:
            details.name_de = ""
            details.overview_de = ""
            details.genres_de = []

        # Türkçe ve Almanca dil bazlı görseller
        tr_imgs = get_lang_images(details.images, "tr")
        de_imgs = get_lang_images(details.images, "de")
        details.poster_tr = tr_imgs["poster"]
        details.backdrop_tr = tr_imgs["backdrop"]
        details.logo_tr = tr_imgs["logo"]
        details.poster_de = de_imgs["poster"]
        details.backdrop_de = de_imgs["backdrop"]
        details.logo_de = de_imgs["logo"]

        # Sertifikalar (TR, DE, US)
        try:
            certs = await _fetch_tmdb_tv_certifications(tv_id)
            details.certification_tr = certs.get("certification_tr")
            details.certification_de = certs.get("certification_de")
            details.certification_us = certs.get("certification_us")
        except Exception:
            details.certification_tr = None
            details.certification_de = None
            details.certification_us = None

        TMDB_DETAILS_CACHE[tv_id] = details
        return details
    except Exception as e:
        LOGGER.warning(f"TMDb tv details fetch failed for id={tv_id}: {e}")
        TMDB_DETAILS_CACHE[tv_id] = None
        return None


async def _tmdb_episode_details(tv_id, season, episode):
    key = (tv_id, season, episode)
    if key in EPISODE_CACHE:
        return EPISODE_CACHE[key]
    try:
        async with API_SEMAPHORE:
            # details() parametresiz; dil tmdb (tmdb_tr) init'inden geliyor
            details = await tmdb.episode(tv_id, season, episode).details()
        EPISODE_CACHE[key] = details
        return details
    except Exception:
        EPISODE_CACHE[key] = None
        return None

def translate_text_safe(text: str) -> str:
    if not text:
        return ""

    text = str(text).strip()

    # çok kısa metinleri çevirmiyoruz
    if len(text) < 3:
        return text

    if text in TRANSLATE_CACHE:
        return TRANSLATE_CACHE[text]

    try:
        translated = GoogleTranslator(source="auto", target="tr").translate(text)
    except Exception:
        translated = text

    TRANSLATE_CACHE[text] = translated
    return translated

def translate_text_safe_de(text: str) -> str:
    """Verilen metni Almancaya çevirir. Hata durumunda orijinal metni döner."""
    if not text:
        return ""

    text = str(text).strip()

    if len(text) < 3:
        return text

    if text in TRANSLATE_DE_CACHE:
        return TRANSLATE_DE_CACHE[text]

    try:
        translated = GoogleTranslator(source="auto", target="de").translate(text)
    except Exception:
        translated = text

    TRANSLATE_DE_CACHE[text] = translated
    return translated

def tur_genre_normalize(genres):
    """Türkçe genre normalize (geriye dönük uyumluluk için korunuyor)."""
    if not genres:
        return []
    out = []
    for g in genres:
        key_original = g.lower().strip()                   # "sci-fi & fantasy"
        key_space    = key_original.replace("-", " ")      # "sci fi & fantasy"
        key_hyphen   = key_original.replace(" ", "-")      # "sci-fi-&-fantasy"
        result = (
            GENRE_TUR_ALIASES.get(key_original) or
            GENRE_TUR_ALIASES.get(key_space) or
            GENRE_TUR_ALIASES.get(key_hyphen) or
            g
        )
        out.append(result)
    return out

def de_genre_normalize(genres):
    """İngilizce genre listesini Almancaya çevirir."""
    if not genres:
        return []
    out = []
    for g in genres:
        key_original = g.lower().strip()
        key_space    = key_original.replace("-", " ")
        key_hyphen   = key_original.replace(" ", "-")
        result = (
            GENRE_DE_ALIASES.get(key_original) or
            GENRE_DE_ALIASES.get(key_space) or
            GENRE_DE_ALIASES.get(key_hyphen) or
            g
        )
        out.append(result)
    return out

# ----------------- Main Metadata -----------------
async def metadata(filename: str, channel: int, msg_id, override_id: str = None) -> dict | None:
    try:
        filename = re.sub(r'\bm(1080p|720p|2160p|480p)\b', r'\1', filename, flags=re.IGNORECASE)
        # Strip any embedded URLs before PTN parsing so season/episode are not lost
        filename_clean = re.sub(r'https?://\S+', '', filename).strip()
        parsed = PTN.parse(filename_clean)
    except Exception as e:
        LOGGER.error(f"PTN parsing failed for {filename}: {e}\n{traceback.format_exc()}")
        return None

    # Skip combined/invalid files
    if "excess" in parsed and any("combined" in item.lower() for item in parsed["excess"]):
        LOGGER.info(f"Skipping {filename}: contains 'combined'")
        return None

    # Split dosya tespiti (.mkv.001 / .mkv.01 tarzı)
    split_info = parse_split_info(filename)
    part_number = split_info[1] if split_info else None

    # part/cd/disc/disk formatındaki bölünmüş dosyaları atla
    # (.mkv.001 formatı split_info ile handle ediliyor, bunlar farklı)
    multipart_pattern = compile(r'(?:part|cd|disc|disk)[s._-]*\d+(?=\.\w+$)', IGNORECASE)
    if multipart_pattern.search(filename):
        LOGGER.info(f"Skipping {filename}: seems to be a split/multipart file")
        return None

    title = parsed.get("title")
    # "Orijinal Başlık - Türkçe Çeviri" formatındaki başlıklarda
    # yalnızca tire öncesini TMDB araması için kullan.
    # Örnek: "Dune Part Two - Dune Çöl Gezegeni Bölüm İki" → "Dune Part Two"
    if title and " - " in title:
        title = title.split(" - ")[0].strip()
    season = parsed.get("season")
    episode = parsed.get("episode")
    year = parsed.get("year")
    quality = parsed.get("resolution")
    if not quality:
        # Çözünürlük yoksa dosya adında dvdrip veya .avi ara
        if re.search(r'dvdrip|\.avi', filename, re.IGNORECASE):
            quality = "576p"
        else:
            # Hiçbir şey bulunamazsa varsayılan 1080p
            quality = "1080p"

    # CamRip/CAM kaynak tespiti: PTN "quality" alanı veya doğrudan dosya adı kontrolü
    ptn_source = (parsed.get("quality") or "").lower()
    if re.search(r'\bcam[-_]?rip\b|\bcamrip\b|\bcam\b', filename, re.IGNORECASE) or \
       re.search(r'\bcam[-_]?rip\b|\bcamrip\b|\bcam\b', ptn_source):
        quality = "CamRip"
    if isinstance(season, list) or isinstance(episode, list):
        LOGGER.warning(f"Invalid season/episode format for {filename}: {parsed}")
        return None
    if season and not episode:
        LOGGER.warning(f"Missing episode in {filename}: {parsed}")
        return None
    if not quality:
        LOGGER.warning(f"Skipping {filename}: No resolution (parsed={parsed})")
        return None
    if not title:
        LOGGER.info(f"No title parsed from: {filename} (parsed={parsed})")
        return None


    default_id = None
    id_media_type = None  # 'tv' or 'movie' if extracted from a TMDb URL

    if override_id:
        try:
            _id, _mt = extract_default_id(override_id)
            default_id = _id or override_id
            id_media_type = _mt
        except Exception:
            pass

    if not default_id:
        try:
            _id, _mt = extract_default_id(Backend.USE_DEFAULT_ID)
            if _id:
                default_id = _id
                id_media_type = _mt
        except Exception:
            pass

    if not default_id:
        try:
            # Also scan the original filename for embedded TMDb/IMDb URLs
            _id, _mt = extract_default_id(filename)
            if _id:
                default_id = _id
                id_media_type = _mt
        except Exception:
            pass

    data = {"chat_id": channel, "msg_id": msg_id}
    try:
        encoded_string = await encode_string(data)
    except Exception:
        encoded_string = None

    group_key = None
    if split_info:
        group_key = f"{channel}:{quality}:{split_info[0]}"

    try:
        # Determine whether this is a TV or movie entry.
        # Priority: explicit TMDb URL type > season/episode presence in filename.
        is_tv = bool(season and episode)
        if id_media_type == "tv":
            is_tv = True
        elif id_media_type == "movie":
            is_tv = False

        if is_tv:
            if not (season and episode):
                LOGGER.warning(f"URL says TV but no season/episode parsed for {filename} ({parsed})")
                return None
            LOGGER.info(f"Fetching TV metadata: {title} S{season}E{episode}")
            result = await fetch_tv_metadata(title, season, episode, encoded_string, year, quality, default_id)
        else:
            LOGGER.info(f"Fetching Movie metadata: {title} ({year})")
            result = await fetch_movie_metadata(title, encoded_string, year, quality, default_id)

        if result is not None:
            result["group_key"] = group_key
            result["part_number"] = part_number
        return result
    except Exception as e:
        LOGGER.error(f"Error while fetching metadata for {filename}: {e}\n{traceback.format_exc()}")
        return None

# ----------------- TV Metadata -----------------
async def fetch_tv_metadata(title, season, episode, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    """
    Deduplication wrapper: aynı S/E için paralel çağrıları tek API isteğine indirger.
    """
    if not default_id:
        dedup_key = f"tv::{title.lower().strip()}::{year}::S{season}E{episode}"
        if dedup_key in _METADATA_IN_FLIGHT:
            try:
                result = await asyncio.shield(_METADATA_IN_FLIGHT[dedup_key])
                if result is not None:
                    result = dict(result)
                    result["encoded_string"] = encoded_string
                return result
            except Exception:
                pass
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        _METADATA_IN_FLIGHT[dedup_key] = fut
        try:
            result = await _fetch_tv_metadata_impl(title, season, episode, encoded_string, year, quality, default_id)
            fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            _METADATA_IN_FLIGHT.pop(dedup_key, None)

    return await _fetch_tv_metadata_impl(title, season, episode, encoded_string, year, quality, default_id)


async def _fetch_tv_metadata_impl(title, season, episode, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    imdb_id = None
    tmdb_id = None
    imdb_tv = None
    imdb_ep = None
    use_tmdb = False

    # -------------------------------------------------------
    # 1. Handle default ID (IMDb / TMDb)
    # -------------------------------------------------------
    if default_id:
        default_id = str(default_id)
        if default_id.startswith("tt"):
            imdb_id = default_id
            use_tmdb = False
        elif default_id.isdigit():
            tmdb_id = int(default_id)
            use_tmdb = True

    # -------------------------------------------------------
    # 2. If no ID → Try IMDb search first
    #    Kısa başlıklar (≤3 karakter) Cinemeta'da güvenilmez sonuç
    #    döndürdüğünden doğrudan TMDb'ye yönlendirilir.
    # -------------------------------------------------------
    if not imdb_id and not tmdb_id:
        if len(title.strip()) <= 3:
            LOGGER.info(f"Short title '{title}' (≤3 chars) — skipping IMDb search, using TMDb directly")
            use_tmdb = True
        else:
            imdb_id = await safe_imdb_search(title, "tvSeries")
            use_tmdb = not bool(imdb_id)

    # -------------------------------------------------------
    # 3. IMDb fetch (series + episode)
    # -------------------------------------------------------
    if imdb_id and not use_tmdb:
        try:
            # ----- series details
            if imdb_id in IMDB_CACHE:
                imdb_tv = IMDB_CACHE[imdb_id]
            else:
                async with API_SEMAPHORE:
                    imdb_tv = await get_detail(imdb_id=imdb_id, media_type="tvSeries")
                IMDB_CACHE[imdb_id] = imdb_tv

            # ----- episode details
            ep_key = f"{imdb_id}::{season}::{episode}"
            if ep_key in EPISODE_CACHE:
                imdb_ep = EPISODE_CACHE[ep_key]
            else:
                async with API_SEMAPHORE:
                    imdb_ep = await get_season(imdb_id=imdb_id, season_id=season, episode_id=episode)
                EPISODE_CACHE[ep_key] = imdb_ep

        except Exception as e:
            LOGGER.warning(f"IMDb TV fetch failed [{imdb_id}] → {e}")
            imdb_tv = None
            imdb_ep = None
            use_tmdb = True

    # -------------------------------------------------------
    # 4. Decide if TMDb required
    # -------------------------------------------------------
    must_use_tmdb = (
        use_tmdb or
        imdb_tv is None or
        imdb_tv == {}
    )

    # =======================================================
    #  5. TMDb MODE
    # =======================================================
    if must_use_tmdb:
        LOGGER.info(f"No valid IMDb TV data for '{title}' → using TMDb")

        # Search TMDb by title
        if not tmdb_id:
            tmdb_search = await safe_tmdb_search(title, "tv", year)
            if not tmdb_search:
                LOGGER.warning(f"No TMDb TV result for '{title}'")
                return None
            tmdb_id = tmdb_search.id

        # Fetch full TV show details
        tv = await _tmdb_tv_details(tmdb_id)
        if not tv:
            LOGGER.warning(f"TMDb TV details failed for id={tmdb_id}")
            return None

        # Fetch episode
        ep = await _tmdb_episode_details(tmdb_id, season, episode)

        # Cast list
        credits = getattr(tv, "credits", None) or {}
        cast_arr = getattr(credits, "cast", []) or []
        cast = [
            getattr(c, "name", None) or getattr(c, "original_name", None)
            for c in cast_arr
        ]

        # Runtime (prefer episode → series → empty)
        ep_runtime = getattr(ep, "runtime", None) if ep else None
        series_runtime = (
            tv.episode_run_time[0] if getattr(tv, "episode_run_time", None) else None
        )
        runtime_val = ep_runtime or series_runtime
        runtime = f"{runtime_val} min" if runtime_val else ""

        _tv_imdb_id = getattr(getattr(tv, "external_ids", None), "imdb_id", None)
        return {
            "tmdb_id": tv.id,
            "imdb_id": _tv_imdb_id,
            "title": tv.original_name or tv.name or title,
            "title_tr": tv.name or title,
            "title_de": getattr(tv, "name_de", "") or tv.original_name or title,
            "year": getattr(tv.first_air_date, "year", 0) if getattr(tv, "first_air_date", None) else 0,
            "rate": getattr(tv, "vote_average", 0) or 0,
            "description": tv.overview or "",
            "description_tr": translate_text_safe(tv.overview),
            "description_de": getattr(tv, "overview_de", "") or translate_text_safe_de(tv.overview),
            "poster": format_tmdb_image(tv.poster_path),
            "backdrop": format_tmdb_image(tv.backdrop_path, "original"),
            "logo": get_tmdb_logo(getattr(tv, "images", None)),
            "poster_tr": getattr(tv, "poster_tr", "") or "",
            "backdrop_tr": getattr(tv, "backdrop_tr", "") or "",
            "logo_tr": getattr(tv, "logo_tr", "") or "",
            "poster_de": getattr(tv, "poster_de", "") or "",
            "backdrop_de": getattr(tv, "backdrop_de", "") or "",
            "logo_de": getattr(tv, "logo_de", "") or "",
            "genres": [g.name for g in (tv.genres or [])],
            "genres_tr": tur_genre_normalize([g.name for g in (tv.genres or [])]),
            "genres_de": de_genre_normalize(getattr(tv, "genres_de", []) or []) or de_genre_normalize([g.name for g in (tv.genres or [])]),
            "certification_tr": getattr(tv, "certification_tr", None),
            "certification_de": getattr(tv, "certification_de", None),
            "certification_us": getattr(tv, "certification_us", None),
            "original_language": getattr(tv, "original_language", None),
            "media_type": "tv",
            "status": getattr(tv, "status", None),  # Returning Series / Ended / Canceled / In Production …
            "cast": cast,
            "runtime": str(runtime),

            "season_number": season,
            "episode_number": episode,
            "episode_title": getattr(ep, "name", f"S{season}E{episode}") if ep else f"S{season}E{episode}",
            "episode_title_tr": translate_text_safe(getattr(ep, "name", f"S{season}E{episode}")) if ep else f"S{season}E{episode}",
            "episode_title_de": translate_text_safe_de(getattr(ep, "name", f"S{season}E{episode}")) if ep else f"S{season}E{episode}",
            "episode_backdrop": format_tmdb_image(getattr(ep, "still_path", None), "original") if ep else "",
            "episode_overview": getattr(ep, "overview", "") if ep else "",
            "episode_overview_tr": translate_text_safe(getattr(ep, "overview", "")) if ep else "",
            "episode_overview_de": translate_text_safe_de(getattr(ep, "overview", "")) if ep else "",
            "episode_released": (
                ep.air_date.strftime("%Y-%m-%dT05:00:00.000Z")
                if getattr(ep, "air_date", None)
                else ""
            ),

            "quality": quality,
            "encoded_string": encoded_string,
        }

    # =======================================================
    #  6. IMDb MODE
    # =======================================================
    imdb = imdb_tv or {}
    ep = imdb_ep or {}

    imdb_images = format_imdb_images(imdb_id)

    # IMDb modunda da TMDb'den TR/DE baslik + gorsel cek (/turkce komutu mantigi)
    tr_title = title or imdb.get("title")
    de_title = imdb.get("title", title)
    de_desc = ""  # Başlangıçta boş; TMDB'den Almanca açıklama alınacak
    poster_tr, backdrop_tr, logo_tr = "", "", ""
    poster_de, backdrop_de, logo_de = "", "", ""
    genres_de = []
    certification_tr = certification_de = certification_us = None
    series_status = None

    raw_tmdb_id = imdb.get("moviedb_id")
    fallback_tmdb_id = int(raw_tmdb_id) if raw_tmdb_id and str(raw_tmdb_id).isdigit() else None
    _fallback_collection_id = None  # TMDB enrichment'ta doldurulacak

    # moviedb_id yoksa imdb_id üzerinden TMDB'de ara
    if not fallback_tmdb_id and imdb_id:
        fallback_tmdb_id = await _resolve_tmdb_id_from_imdb(imdb_id, "tv")
        if fallback_tmdb_id:
            raw_tmdb_id = str(fallback_tmdb_id)

    if fallback_tmdb_id:
        try:
            tv_details = await _tmdb_tv_details(fallback_tmdb_id)
            if tv_details:
                tr_title = tv_details.name or tr_title
                de_title = getattr(tv_details, "name_de", "") or de_title
                # Almanca açıklamayı TMDB'den al (İngilizce IMDb plot değil)
                de_desc = getattr(tv_details, "overview_de", "") or ""
                poster_tr = getattr(tv_details, "poster_tr", "") or ""
                backdrop_tr = getattr(tv_details, "backdrop_tr", "") or ""
                logo_tr = getattr(tv_details, "logo_tr", "") or ""
                poster_de = getattr(tv_details, "poster_de", "") or ""
                backdrop_de = getattr(tv_details, "backdrop_de", "") or ""
                logo_de = getattr(tv_details, "logo_de", "") or ""
                genres_de = getattr(tv_details, "genres_de", []) or []
                series_status = getattr(tv_details, "status", None)
                # TMDB'den gelen imdb_id ile DB'ye yazılacak imdb_id aynıysa sertifikaları al
                tmdb_ext_imdb = getattr(getattr(tv_details, "external_ids", None), "imdb_id", None)
                if not tmdb_ext_imdb or tmdb_ext_imdb == imdb_id:
                    certification_tr = getattr(tv_details, "certification_tr", None)
                    certification_de = getattr(tv_details, "certification_de", None)
                    certification_us = getattr(tv_details, "certification_us", None)
        except Exception as e:
            LOGGER.warning(f"IMDb TV mode: TMDb TR/DE enrichment failed [{imdb_id}] -> {e}")

    # poster/backdrop/logo: her zaman metahub
    final_poster = imdb_images["poster"]
    final_backdrop = imdb_images["backdrop"]
    final_logo = imdb_images["logo"]

    # Almanca açıklama: TMDB'den Almanca geldiyse kullan, yoksa İngilizce plot'u çevir
    final_desc_de = de_desc if de_desc else translate_text_safe_de(imdb.get("plot", ""))

    return {
        "tmdb_id": raw_tmdb_id or imdb_id.replace("tt", ""),
        "imdb_id": imdb_id,
        "title": imdb.get("title") or title,
        "title_tr": tr_title,
        "title_de": de_title,
        "year": imdb.get("releaseDetailed", {}).get("year", 0),
        "rate": imdb.get("rating", {}).get("star", 0),
        "description": imdb.get("plot", ""),
        "description_tr": translate_text_safe(imdb.get("plot", "")),
        "description_de": final_desc_de,
        "poster": final_poster,
        "backdrop": final_backdrop,
        "logo": final_logo,
        "poster_tr": poster_tr,
        "backdrop_tr": backdrop_tr,
        "logo_tr": logo_tr,
        "poster_de": poster_de,
        "backdrop_de": backdrop_de,
        "logo_de": logo_de,
        "cast": imdb.get("cast", []),
        "runtime": str(imdb.get("runtime") or ""),
        "genres": imdb.get("genre", []),
        "genres_tr": tur_genre_normalize(imdb.get("genre", [])),
        "genres_de": de_genre_normalize(genres_de) if genres_de else de_genre_normalize(imdb.get("genre", [])),
        "certification_tr": certification_tr,
        "certification_de": certification_de,
        "certification_us": certification_us,
        "original_language": imdb.get("spokenLanguages", [{}])[0].get("id") if imdb.get("spokenLanguages") else None,
        "media_type": "tv",
        "status": series_status,  # TMDB'den alınan dizi yayın durumu

        "season_number": season,
        "episode_number": episode,
        "episode_title": ep.get("title", f"S{season}E{episode}"),
        "episode_title_tr": translate_text_safe(ep.get("title", f"S{season}E{episode}")),
        "episode_title_de": translate_text_safe_de(ep.get("title", f"S{season}E{episode}")),
        "episode_backdrop": ep.get("image", ""),
        "episode_overview": ep.get("plot", ""),
        "episode_overview_tr": translate_text_safe(ep.get("plot", "")),
        "episode_overview_de": translate_text_safe_de(ep.get("plot", "")),
        "episode_released": str(ep.get("released", "")),

        "quality": quality,
        "encoded_string": encoded_string,
    }


# ----------------- Movie Metadata -----------------
async def fetch_movie_metadata(title, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    """
    Deduplication wrapper: aynı anda aynı film için birden fazla çağrı gelirse
    yalnızca ilki API'ye gider, diğerleri sonucu bekler ve encoded_string'ini
    kendi değerleriyle günceller (her kaydın stream ID'si farklıdır).
    """
    # default_id varsa deduplication uygulanmaz (farklı versiyonlar olabilir)
    if not default_id:
        dedup_key = f"movie::{title.lower().strip()}::{year}"
        if dedup_key in _METADATA_IN_FLIGHT:
            try:
                result = await asyncio.shield(_METADATA_IN_FLIGHT[dedup_key])
                if result is not None:
                    result = dict(result)
                    result["encoded_string"] = encoded_string
                return result
            except Exception:
                pass  # İlk çağrı hata verdiyse ikinci çağrı tekrar dener
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        _METADATA_IN_FLIGHT[dedup_key] = fut
        try:
            result = await _fetch_movie_metadata_impl(title, encoded_string, year, quality, default_id)
            fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            _METADATA_IN_FLIGHT.pop(dedup_key, None)

    return await _fetch_movie_metadata_impl(title, encoded_string, year, quality, default_id)


async def _fetch_movie_metadata_impl(title, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    imdb_id = None
    tmdb_id = None
    imdb_details = None
    use_tmdb = False

    # -------------------------------------------------------
    # 1. PROCESS DEFAULT ID (tt = IMDb, digits = TMDb)
    # -------------------------------------------------------
    if default_id:
        default_id = str(default_id).strip()

        if default_id.startswith("tt"):
            imdb_id = default_id
            use_tmdb = False                       
        elif default_id.isdigit():
            tmdb_id = int(default_id)
            use_tmdb = True                       

    # -------------------------------------------------------
    # 2. IF NO DEFAULT ID → SEARCH IMDb FIRST
    #    Kısa başlıklar (≤3 karakter) Cinemeta'da güvenilmez sonuç
    #    döndürdüğünden doğrudan TMDb'ye yönlendirilir.
    # -------------------------------------------------------
    if not imdb_id and not tmdb_id:
        if len(title.strip()) <= 3:
            LOGGER.info(f"Short title '{title}' (≤3 chars) — skipping IMDb search, using TMDb directly")
            use_tmdb = True
        else:
            imdb_id = await safe_imdb_search(
                f"{title} {year}" if year else title,
                "movie"
            )
            use_tmdb = not bool(imdb_id)

    # -------------------------------------------------------
    # 3. FETCH IMDb DETAILS (only if imdb_id exists)
    # -------------------------------------------------------
    if imdb_id and not use_tmdb:
        try:
            if imdb_id in IMDB_CACHE:
                imdb_details = IMDB_CACHE[imdb_id]
            else:
                async with API_SEMAPHORE:
                    imdb_details = await get_detail(
                        imdb_id=imdb_id,
                        media_type="movie"
                    )

                IMDB_CACHE[imdb_id] = imdb_details

        except Exception as e:
            LOGGER.warning(f"IMDb movie fetch failed [{title}] → {e}")
            imdb_details = None
            use_tmdb = True

    # -------------------------------------------------------
    # 4. DECIDE FINAL DATA SOURCE
    # -------------------------------------------------------
    must_use_tmdb = (
        use_tmdb or
        imdb_details is None or
        imdb_details == {}
    )

    # =======================================================
    #  5. TMDb MODE
    # =======================================================
    if must_use_tmdb:
        LOGGER.info(f"No valid IMDb data for '{title}' → using TMDb")

        # TMDb search if id unknown
        if not tmdb_id:
            tmdb_result = await safe_tmdb_search(title, "movie", year)
            if not tmdb_result:
                LOGGER.warning(f"No TMDb movie found for '{title}'")
                return None
            tmdb_id = tmdb_result.id

        # Fetch TMDb details
        movie = await _tmdb_movie_details(tmdb_id)
        if not movie:
            LOGGER.warning(f"TMDb details failed for {tmdb_id}")
            return None

        # Cast extraction
        credits = getattr(movie, "credits", None) or {}
        cast_arr = getattr(credits, "cast", []) or []
        cast_names = [
            getattr(c, "name", None) or getattr(c, "original_name", None)
            for c in cast_arr
        ]

        runtime_val = getattr(movie, "runtime", None)
        runtime = f"{runtime_val} min" if runtime_val else ""

        _movie_imdb_id = getattr(movie.external_ids, "imdb_id", None)
        return {
            "tmdb_id": movie.id,
            "imdb_id": _movie_imdb_id,
            "title": movie.original_title or movie.title or title,
            "title_tr": movie.title or title,
            "title_de": getattr(movie, "title_de", "") or movie.original_title or title,
            "year": getattr(movie.release_date, "year", 0) if getattr(movie, "release_date", None) else 0,
            "rate": getattr(movie, "vote_average", 0) or 0,
            "description": movie.overview or "",
            "description_tr": translate_text_safe(movie.overview),
            "description_de": getattr(movie, "overview_de", "") or translate_text_safe_de(movie.overview),
            "poster": format_tmdb_image(movie.poster_path),
            "backdrop": format_tmdb_image(movie.backdrop_path, "original"),
            "logo": get_tmdb_logo(getattr(movie, "images", None)),
            "poster_tr": getattr(movie, "poster_tr", "") or "",
            "backdrop_tr": getattr(movie, "backdrop_tr", "") or "",
            "logo_tr": getattr(movie, "logo_tr", "") or "",
            "poster_de": getattr(movie, "poster_de", "") or "",
            "backdrop_de": getattr(movie, "backdrop_de", "") or "",
            "logo_de": getattr(movie, "logo_de", "") or "",
            "cast": cast_names,
            "runtime": str(runtime),
            "media_type": "movie",
            "genres": [g.name for g in (movie.genres or [])],
            "genres_tr": tur_genre_normalize([g.name for g in (movie.genres or [])]),
            "genres_de": de_genre_normalize(getattr(movie, "genres_de", []) or []) or de_genre_normalize([g.name for g in (movie.genres or [])]),
            "collection_id": getattr(getattr(movie, "belongs_to_collection", None), "id", None),
            "certification_tr": getattr(movie, "certification_tr", None),
            "certification_de": getattr(movie, "certification_de", None),
            "certification_us": getattr(movie, "certification_us", None),
            "original_language": getattr(movie, "original_language", None),
            "quality": quality,
            "encoded_string": encoded_string,
        }

    # =======================================================
    #  6. IMDb MODE
    # =======================================================
    imdb_images = format_imdb_images(imdb_id)
    imdb = imdb_details or {}

    # IMDb modunda da TMDb'den TR/DE baslik + gorsel cek (/turkce komutu mantigi)
    tr_title = imdb.get("title") or title
    de_title = imdb.get("title") or title
    de_desc = ""  # Başlangıçta boş; TMDB'den Almanca açıklama alınacak
    poster_tr, backdrop_tr, logo_tr = "", "", ""
    poster_de, backdrop_de, logo_de = "", "", ""
    genres_de = []
    certification_tr = certification_de = certification_us = None
    _fallback_collection_id = None  # TMDB enrichment yoksa da tanımlı olsun

    raw_tmdb_id = imdb.get("moviedb_id")
    fallback_tmdb_id = int(raw_tmdb_id) if raw_tmdb_id and str(raw_tmdb_id).isdigit() else None

    # moviedb_id yoksa imdb_id üzerinden TMDB'de ara
    if not fallback_tmdb_id and imdb_id:
        fallback_tmdb_id = await _resolve_tmdb_id_from_imdb(imdb_id, "movie")
        if fallback_tmdb_id:
            raw_tmdb_id = str(fallback_tmdb_id)

    if fallback_tmdb_id:
        try:
            movie_details = await _tmdb_movie_details(fallback_tmdb_id)
            if movie_details:
                tr_title = movie_details.title or tr_title
                de_title = getattr(movie_details, "title_de", "") or de_title
                # Almanca açıklamayı TMDB'den al (İngilizce IMDb plot değil)
                de_desc = getattr(movie_details, "overview_de", "") or ""
                poster_tr = getattr(movie_details, "poster_tr", "") or ""
                backdrop_tr = getattr(movie_details, "backdrop_tr", "") or ""
                logo_tr = getattr(movie_details, "logo_tr", "") or ""
                poster_de = getattr(movie_details, "poster_de", "") or ""
                backdrop_de = getattr(movie_details, "backdrop_de", "") or ""
                logo_de = getattr(movie_details, "logo_de", "") or ""
                genres_de = getattr(movie_details, "genres_de", []) or []
                # TMDB'den gelen imdb_id ile DB'ye yazılacak imdb_id aynıysa sertifikaları al
                tmdb_ext_imdb = getattr(getattr(movie_details, "external_ids", None), "imdb_id", None)
                if not tmdb_ext_imdb or tmdb_ext_imdb == imdb_id:
                    certification_tr = getattr(movie_details, "certification_tr", None)
                    certification_de = getattr(movie_details, "certification_de", None)
                    certification_us = getattr(movie_details, "certification_us", None)
            _fallback_collection_id = getattr(
                getattr(movie_details, "belongs_to_collection", None), "id", None
            )
        except Exception as e:
            LOGGER.warning(f"IMDb Movie mode: TMDb TR/DE enrichment failed [{imdb_id}] -> {e}")
            _fallback_collection_id = None

    # poster/backdrop/logo: her zaman metahub
    final_poster = imdb_images["poster"]
    final_backdrop = imdb_images["backdrop"]
    final_logo = imdb_images["logo"]

    # Almanca açıklama: TMDB'den Almanca geldiyse kullan, yoksa İngilizce plot'u çevir
    final_desc_de = de_desc if de_desc else translate_text_safe_de(imdb.get("plot", ""))

    return {
        "tmdb_id": raw_tmdb_id or imdb_id.replace("tt", ""),
        "imdb_id": imdb_id,
        "title": imdb.get("title") or title,
        "title_tr": tr_title,
        "title_de": de_title,
        "year": imdb.get("releaseDetailed", {}).get("year", 0),
        "rate": imdb.get("rating", {}).get("star", 0),
        "description": imdb.get("plot", ""),
        "description_tr": translate_text_safe(imdb.get("plot", "")),
        "description_de": final_desc_de,
        "poster": final_poster,
        "backdrop": final_backdrop,
        "logo": final_logo,
        "poster_tr": poster_tr,
        "backdrop_tr": backdrop_tr,
        "logo_tr": logo_tr,
        "poster_de": poster_de,
        "backdrop_de": backdrop_de,
        "logo_de": logo_de,
        "cast": imdb.get("cast", []),
        "runtime": str(imdb.get("runtime") or ""),
        "media_type": "movie",
        "genres": imdb.get("genre", []),
        "genres_tr": tur_genre_normalize(imdb.get("genre", [])),
        "genres_de": de_genre_normalize(genres_de) if genres_de else de_genre_normalize(imdb.get("genre", [])),
        "collection_id": _fallback_collection_id,
        "certification_tr": certification_tr,
        "certification_de": certification_de,
        "certification_us": certification_us,
        "original_language": imdb.get("spokenLanguages", [{}])[0].get("id") if imdb.get("spokenLanguages") else None,
        "quality": quality,
        "encoded_string": encoded_string,
    }
