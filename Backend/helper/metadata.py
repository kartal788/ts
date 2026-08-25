import asyncio
import traceback
import time
import threading
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
# İngilizce (orijinal dil) TMDB istemcisi — "description"/"genres" gibi
# orijinal dilde kalması gereken alanlar için (bkz. media_edit.html "Yeniden
# Sorgula" akışı). tmdb_tr/tmdb_de zaten TR/DE dilinde döndüğünden bu iki
# istemci orijinal/İngilizce veri için kullanılamaz.
tmdb_en = aioTMDb(key=Telegram.TMDB_API, language="en-US")
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


async def _tmdb_episode_details(tv_id, season, episode, client=None):
    """client verilmezse tmdb_tr (varsayılan `tmdb`) kullanılır. Almanca bölüm
    başlığı/özeti için tmdb_de ile ayrıca çağrılabilir — her dil kendi cache
    anahtarı altında (tv_id, season, episode, lang) tutulur."""
    client = client or tmdb
    lang_tag = "de" if client is tmdb_de else "tr"
    key = (tv_id, season, episode, lang_tag)
    if key in EPISODE_CACHE:
        return EPISODE_CACHE[key]
    try:
        async with API_SEMAPHORE:
            # details() parametresiz; dil client init'inden geliyor (tr-TR / de-DE)
            details = await client.episode(tv_id, season, episode).details()
        EPISODE_CACHE[key] = details
        return details
    except Exception:
        EPISODE_CACHE[key] = None
        return None

#----- deep_translator, Google Translate'in web arayüzünü kazıyarak (scraping)
#----- çalışır. Google bazen (rate limit / geçici sunucu sorunu vb. nedenle)
#----- gerçek çeviri yerine HTTP 200 ile birlikte kendi jenerik hata sayfasını
#----- ("Error 500 (Server Error)!!1 500. That's an error. ...") döner; bu
#----- durumda kütüphane bir exception FIRLATMAZ, sayfadan kazıdığı bu hata
#----- metnini "çeviri" diye geri verir ve bu metin veritabanına yazılır.
#----- Bu fonksiyon, çeviri sonucunun bu bilinen Google hata sayfası imzasını
#----- taşıyıp taşımadığını tespit eder.
_TRANSLATE_ERROR_SIGNATURES = (
    "that's an error",
    "that's all we know",
    "error 500 (server error)",
)


def _is_translate_error_page(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(sig in lowered for sig in _TRANSLATE_ERROR_SIGNATURES)


#----- Google Translate scraping (deep_translator) resmi bir API olmadığı için
#----- rastgele/geçici olarak (özellikle art arda çok sayıda istek gittiğinde)
#----- kendi hata sayfasını dönebiliyor. Tek denemede pes edip orijinal metne
#----- düşmek yerine, kısa bir bekleme ile birkaç kez daha denemek çoğu geçici
#----- hatayı kendiliğinden çözüyor. Deneme sayısını abartmıyoruz ki zaten
#----- baskı altındaki Google'a daha da fazla istek göndermeyelim.
_TRANSLATE_MAX_ATTEMPTS = 3
_TRANSLATE_RETRY_DELAY_SECONDS = 20

#----- GÖZLEM: Google'ın kötüye kullanım tespiti, aynı oturum/IP'den art arda
#----- çok sayıda istek geldikçe KADEMELİ OLARAK SERTLEŞİYOR gibi görünüyor —
#----- bot yeni açıldığında ilk birkaç dakika neredeyse hiç hata yokken, aynı
#----- oturumda 15-20 dakika sonra hemen her çeviri başarısız olmaya başlıyor.
#----- Bunu yavaşlatmak için TÜM Google Translate istekleri (canlı içerik
#----- ekleme + retry denemeleri
#----- dahil), süreç genelinde ortak bir "gate" ile aralıklandırılır: art
#----- arda iki istek arasında en az bu kadar süre geçmesi zorunlu kılınır.
_TRANSLATE_RATE_LOCK = threading.Lock()
_TRANSLATE_LAST_CALL_TS = 0.0
_TRANSLATE_MIN_INTERVAL_SECONDS = 1.0


def _throttle_google_translate_call() -> None:
    global _TRANSLATE_LAST_CALL_TS
    with _TRANSLATE_RATE_LOCK:
        now = time.monotonic()
        wait = _TRANSLATE_LAST_CALL_TS + _TRANSLATE_MIN_INTERVAL_SECONDS - now
        if wait > 0:
            time.sleep(wait)
        _TRANSLATE_LAST_CALL_TS = time.monotonic()


#----- GÖZLEM: aynı bölüm/film birden fazla kalite dosyası ayrı ayrı
#----- yüklendiğinde (örn. 720p sonra 1080p) her biri kendi metadata
#----- fetch+çeviri döngüsünü tetikliyor. Çeviri başarısızlıkları KALICI
#----- olarak cache'lenmiyor (kasıtlı — bir sonraki denemede tekrar
#----- denenebilsin diye), bu yüzden aynı metin (örn. "Episode 4") birkaç
#----- saniye/dakika arayla iki kez baştan sona 3 kez daha denenip Google'a
#----- gereksiz ekstra yük bindiriyordu. Bu kısa ömürlü "az önce başarısız
#----- oldu" önbelleği, aynı metnin bu pencere içinde tekrar denenmesini
#----- (ve tekrar bir WARNING loglanmasını) atlayıp doğrudan orijinal metne
#----- düşer; pencere kapandıktan sonra normal şekilde yeniden denenir.
_RECENT_FAILURE_TTL_SECONDS = 120
TRANSLATE_RECENT_FAIL:    _LRUCache = _LRUCache(maxsize=1000)
TRANSLATE_DE_RECENT_FAIL: _LRUCache = _LRUCache(maxsize=1000)


def _translate_with_retry(text: str, target: str, cache: "_LRUCache", log_lang_label: str) -> str:
    last_result = text
    for attempt in range(1, _TRANSLATE_MAX_ATTEMPTS + 1):
        _throttle_google_translate_call()
        try:
            translated = GoogleTranslator(source="auto", target=target).translate(text)
        except Exception:
            translated = None

        if translated and not _is_translate_error_page(translated):
            cache[text] = translated
            return translated

        last_result = text
        if attempt < _TRANSLATE_MAX_ATTEMPTS:
            time.sleep(_TRANSLATE_RETRY_DELAY_SECONDS)

    #----- Tüm denemeler başarısız oldu: orijinal metne düş, SONUCU CACHE'LEME
    #----- ki bir sonraki gerçek çağrıda yeniden denenebilsin.
    LOGGER.warning(
        f"Çeviri ({log_lang_label}) {_TRANSLATE_MAX_ATTEMPTS} denemede de başarısız oldu "
        f"(Google hata sayfası/erişim sorunu), orijinal metin kullanılıyor: {text[:60]!r}"
    )
    return last_result


def translate_text_safe(text: str) -> str:
    if not text:
        return ""

    text = str(text).strip()

    # çok kısa metinleri çevirmiyoruz
    if len(text) < 3:
        return text

    if text in TRANSLATE_CACHE:
        return TRANSLATE_CACHE[text]

    fail_ts = TRANSLATE_RECENT_FAIL.get(text)
    if fail_ts is not None and (time.monotonic() - fail_ts) < _RECENT_FAILURE_TTL_SECONDS:
        return text

    result = _translate_with_retry(text, "tr", TRANSLATE_CACHE, "tr")
    if result == text:
        TRANSLATE_RECENT_FAIL[text] = time.monotonic()
    return result

def translate_text_safe_de(text: str) -> str:
    """Verilen metni Almancaya çevirir. Hata durumunda orijinal metni döner."""
    if not text:
        return ""

    text = str(text).strip()

    if len(text) < 3:
        return text

    if text in TRANSLATE_DE_CACHE:
        return TRANSLATE_DE_CACHE[text]

    fail_ts = TRANSLATE_DE_RECENT_FAIL.get(text)
    if fail_ts is not None and (time.monotonic() - fail_ts) < _RECENT_FAILURE_TTL_SECONDS:
        return text

    result = _translate_with_retry(text, "de", TRANSLATE_DE_CACHE, "de")
    if result == text:
        TRANSLATE_DE_RECENT_FAIL[text] = time.monotonic()
    return result

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
import hashlib


def _slugify_manual_title(title: str) -> str:
    """Başlığı 'manual-<slug>' biçiminde kararlı (deterministik) bir kimliğe çevirir.
    Aynı başlıkla gelen dosyalar aynı kayda (imdb_id eşleşmesiyle) düşer; farklı
    başlıklar farklı kartlar oluşturur."""
    normalized = title.strip().casefold()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"manual-{digest}"


def _manual_tmdb_id(manual_id: str) -> int:
    """Manuel içerik için başlıktan türetilen, kararlı (deterministik) NEGATİF
    bir tamsayı üretir.

    Admin panelindeki düzenle/sil/kalite yeniden adlandır/sezon-bölüm sil gibi
    tüm uçlar (bkz. Backend/fastapi/routes/template_routes.py,
    Backend/fastapi/routes/api_routes.py, Backend/fastapi/main.py) ve
    Backend/helper/database.py içindeki get_document/update_document/
    delete_document gibi fonksiyonlar kaydı bulmak için tmdb_id'yi int()'e
    çevirip sorguluyor. tmdb_id=None bırakılırsa "/media/edit?tmdb_id=None"
    gibi bir URL üretilip int_parsing hatası veriyordu ve kayıt hiçbir zaman
    bulunamıyordu.

    Gerçek TMDB id'leri her zaman pozitif olduğundan, negatif bir sayı
    kullanmak TMDB'den gelen içeriklerle çakışmayı imkansız kılar; aynı
    zamanda tüm bu uçların manuel içerik için de değişiklik yapmadan
    çalışmasını sağlar.
    """
    digest_int = int(hashlib.sha1(manual_id.encode("utf-8")).hexdigest()[:10], 16)
    return -digest_int


def _parse_quality_from_filename(filename: str) -> str:
    """metadata()'daki ana kalite tespiti mantığının küçük, bağımsız bir kopyası —
    manuel eklemede TMDB/IMDb sorgusu yapılmadığı için ayrı tutuldu."""
    try:
        parsed = PTN.parse(filename)
    except Exception:
        parsed = {}
    quality = parsed.get("resolution")
    if not quality:
        if re.search(r"dvdrip|\.avi", filename, re.IGNORECASE):
            quality = "576p"
        else:
            quality = "1080p"
    ptn_source = (parsed.get("quality") or "").lower()
    if re.search(r"\bcam[-_]?rip\b|\bcamrip\b|\bcam\b", filename, re.IGNORECASE) or \
       re.search(r"\bcam[-_]?rip\b|\bcamrip\b|\bcam\b", ptn_source):
        quality = "CamRip"
    return quality


def _parse_manual_season_episode(filename: str) -> tuple[int | None, int | None]:
    """Manuel ekleme modunda dosya adından sezon/bölüm numarasını çıkarmaya çalışır.
    Panelden ayarlanan sezon/otomatik bölüm sayacına göre öncelik taşır: dosya adında
    açıkça bir kalıp varsa (S01E02, 1x02, "Sezon 2 Bölüm 5", "Bölüm 7", "Hafta 3" vb.)
    o kullanılır; yoksa (None, None) döner ve çağıran taraf panel değerlerini kullanır.

    Dönüş: (season, episode) — season bulunamadıysa None olabilir (yalnızca bölüm
    numarası tespit edilmiş olabilir), episode bulunamadıysa (None, None) döner.
    """
    name = filename

    # S01E02 / S1E2
    m = re.search(r"\bS(\d{1,2})[\s._-]?E(\d{1,3})\b", name, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 1x02
    m = re.search(r"\b(\d{1,2})x(\d{1,3})\b", name, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # "Sezon 2 Bölüm 5"
    m = re.search(r"sezon\s*(\d{1,2}).{0,6}?b[oö]l[uü]m\s*(\d{1,3})", name, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Yalnızca bölüm/hafta/ep numarası (sezon panelden gelir)
    for pattern in (
        r"\bb[oö]l[uü]m\s*(\d{1,3})\b",
        r"\bhafta\s*(\d{1,3})\b",
        r"\bep(?:isode)?\.?\s*(\d{1,3})\b",
        r"\bE(\d{1,3})\b",
    ):
        m = re.search(pattern, name, re.IGNORECASE)
        if m:
            return None, int(m.group(1))

    return None, None


async def build_manual_metadata(
    filename: str,
    channel: int,
    msg_id,
    title: str,
    poster: str | None = None,
    description: str | None = None,
    media_type: str = "movie",
    season_number: int | None = None,
    episode_number: int | None = None,
    year: int | None = None,
    tmdb_id: int | None = None,
    imdb_id: str | None = None,
    rating: float | None = None,
    genres: list[str] | None = None,
    visibility_mode: str | None = None,
    visibility_member_ids: list | None = None,
) -> dict | None:
    """Manuel içerik ekleme modu için: TMDB/IMDb'de karşılığı olmayan (ders
    videosu, kişisel video vb.) dosyalar için TMDB/IMDb sorgusu yapmadan doğrudan
    kullanılabilir bir metadata_info sözlüğü üretir.

    media_type == "movie" (varsayılan): Kişisel video / film gibi tek içerik.
    Üretilen kayıt normal 'movie' şeması üzerinden veritabanına yazılır. Aynı
    başlıkla gönderilen farklı dosyalar aynı kart altında birleşir (imdb_id
    başlıktan deterministik türetilir), her biri o kartın altında ayrı bir
    "kalite" satırı olarak listelenir.

    Not: "quality" etiketine dosya adından türetilmiş kısa bir ayraç eklenir.
    Böylece aynı başlık altına gönderilen, çözünürlük bilgisi taşımayan farklı
    videolar (örn. Hafta1.mp4, Hafta2.mp4) REPLACE_MODE açıkken birbirinin
    yerine geçip silinmez — her biri ayrı bir satır olarak kalır.

    media_type == "tv": Ders videoları gibi sezon/bölüm yapısına sahip içerik.
    Gerçek 'tv' şeması üzerinden yazılır (seasons -> episodes -> telegram),
    böylece katalogda normal bir dizi gibi sezon/bölüm listesiyle görünür.
    season_number/episode_number çağıran taraf (reciever.py) tarafından panel
    ayarlarına göre hesaplanıp geçirilir; dosya adında açık bir S/E kalıbı varsa
    bu fonksiyon onu tercih eder.

    year: opsiyonel çıkış yılı (panelde boş bırakılabilir, None gönderilirse
    kayıtta year=None olarak kalır).

    visibility_mode / visibility_member_ids: panelde "İçerik Ekle" formundan
    seçilen görünürlük ayarı. mode="selected" ise yalnızca member_ids'teki
    üyeler bu içeriği görebilir/erişebilir; aksi halde (None veya
    "subscribers") tüm aktif abonelere açıktır (bkz. media_edit.html'deki
    aynı mekanizma — is_media_visible_to_member).

    tmdb_id/imdb_id: normalde başlıktan deterministik olarak türetilir (yeni
    "manuel" bir kayıt oluşturmak için). Ancak /media/edit sayfasındaki
    "İçerik Ekle" (var olan içeriğe ekleme) modu bu ikisini var olan kaydın
    gerçek tmdb_id/imdb_id'siyle geçirir; böylece insert_media() bu dosyayı
    yeni bir kart olarak değil, var olan kaydın altına (film ise yeni kalite,
    dizi ise yeni bölüm olarak) ekler.
    """
    if not title or not title.strip():
        LOGGER.warning("build_manual_metadata: boş başlıkla çağrıldı, atlanıyor.")
        return None

    visibility = None
    if visibility_mode == "selected":
        clean_ids = []
        for m in (visibility_member_ids or []):
            try:
                clean_ids.append(int(m))
            except (TypeError, ValueError):
                continue
        visibility = {"mode": "selected", "member_ids": sorted(set(clean_ids))}
    elif visibility_mode == "subscribers":
        visibility = {"mode": "subscribers", "member_ids": []}
    # visibility_mode None → visibility=None bırakılır; insert_media() bu
    # durumda şemadaki varsayılana (herkese açık) düşer.

    base_quality = _parse_quality_from_filename(filename)
    file_label = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename).strip()
    file_label = re.sub(r"\s+", " ", file_label)[:40]
    quality = f"{base_quality} • {file_label}" if file_label else base_quality

    data = {"chat_id": channel, "msg_id": msg_id}
    try:
        encoded_string = await encode_string(data)
    except Exception:
        LOGGER.error(f"build_manual_metadata: encode_string başarısız ({filename})")
        return None

    manual_id = _slugify_manual_title(title)
    resolved_tmdb_id = tmdb_id if tmdb_id is not None else _manual_tmdb_id(manual_id)
    resolved_imdb_id = imdb_id or manual_id

    if media_type != "tv":
        return {
            "media_type": "movie",
            "tmdb_id": resolved_tmdb_id,
            "imdb_id": resolved_imdb_id,
            "title": title.strip(),
            "title_tr": title.strip(),
            "title_de": title.strip(),
            "description": description or "",
            "description_tr": description or "",
            "description_de": description or "",
            "genres": genres or [],
            "genres_tr": genres or [],
            "genres_de": genres or [],
            "rate": rating or 0,
            "year": year,
            "poster": poster or "",
            "backdrop": poster or "",
            "logo": "",
            "cast": [],
            "runtime": None,
            "original_language": "tr",
            "quality": quality,
            "encoded_string": encoded_string,
            "group_key": None,
            "part_number": None,
            "visibility": visibility,
        }

    # ----- TV (dizi) modu: sezon/bölüm dosya adından tespit edilebiliyorsa
    # o kullanılır, yoksa panelden gelen değerlere düşülür.
    detected_season, detected_episode = _parse_manual_season_episode(filename)
    season = detected_season if detected_season is not None else season_number
    episode = detected_episode if detected_episode is not None else episode_number

    if not season or not episode:
        LOGGER.warning(
            f"build_manual_metadata: dizi modunda sezon/bölüm belirlenemedi "
            f"({filename}) — season={season}, episode={episode}"
        )
        return None

    episode_title = file_label or f"Sezon {season} Bölüm {episode}"

    return {
        "media_type": "tv",
        "tmdb_id": resolved_tmdb_id,
        "imdb_id": resolved_imdb_id,
        "title": title.strip(),
        "title_tr": title.strip(),
        "title_de": title.strip(),
        "description": description or "",
        "description_tr": description or "",
        "description_de": description or "",
        "genres": genres or [],
        "genres_tr": genres or [],
        "genres_de": genres or [],
        "rate": rating or 0,
        "year": year,
        "poster": poster or "",
        "backdrop": poster or "",
        "logo": "",
        "poster_tr": poster or "",
        "backdrop_tr": poster or "",
        "logo_tr": "",
        "poster_de": poster or "",
        "backdrop_de": poster or "",
        "logo_de": "",
        "cast": [],
        "runtime": None,
        "original_language": "tr",
        "status": None,
        "certification_tr": None,
        "certification_de": None,
        "certification_us": None,

        "season_number": season,
        "episode_number": episode,
        "episode_title": episode_title,
        "episode_title_tr": episode_title,
        "episode_title_de": episode_title,
        "episode_backdrop": "",
        "episode_overview": "",
        "episode_overview_tr": "",
        "episode_overview_de": "",
        "episode_released": "",

        "quality": quality,
        "encoded_string": encoded_string,
        "group_key": None,
        "part_number": None,
        "visibility": visibility,
    }


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
    # Bazı dosya adlarında bu sıralama tersine döner (ör. "Ölümcül Kaçamak -
    # Fatal Seduction"), bu yüzden tire sonrası ikinci aday olarak saklanır;
    # birincil başlıkla arama başarısız olursa bu alternatif denenir.
    alt_title = None
    if title and " - " in title:
        parts = [p.strip() for p in title.split(" - ", 1)]
        title, alt_title = parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)
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
            result = await fetch_tv_metadata(title, season, episode, encoded_string, year, quality, default_id, alt_title)
        else:
            LOGGER.info(f"Fetching Movie metadata: {title} ({year})")
            result = await fetch_movie_metadata(title, encoded_string, year, quality, default_id, alt_title)

        if result is not None:
            result["group_key"] = group_key
            result["part_number"] = part_number
        return result
    except Exception as e:
        LOGGER.error(f"Error while fetching metadata for {filename}: {e}\n{traceback.format_exc()}")
        return None

# ----------------- TV Metadata -----------------
async def fetch_tv_metadata(title, season, episode, encoded_string, year=None, quality=None, default_id=None, alt_title=None) -> dict | None:
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
            result = await _fetch_tv_metadata_impl(title, season, episode, encoded_string, year, quality, default_id, alt_title)
            fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            _METADATA_IN_FLIGHT.pop(dedup_key, None)

    return await _fetch_tv_metadata_impl(title, season, episode, encoded_string, year, quality, default_id, alt_title)


async def _fetch_tv_metadata_impl(title, season, episode, encoded_string, year=None, quality=None, default_id=None, alt_title=None) -> dict | None:
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
            if not tmdb_search and alt_title:
                # Bazı dosya adlarında Türkçe/İngilizce sıralaması ters
                # olabildiğinden ("Ölümcül Kaçamak - Fatal Seduction" gibi),
                # birincil başlık bulunamazsa alternatif başlıkla tekrar denenir.
                LOGGER.info(f"No TMDb TV result for '{title}' — trying alt title '{alt_title}'")
                tmdb_search = await safe_tmdb_search(alt_title, "tv", year)
                if tmdb_search:
                    title = alt_title
            if not tmdb_search:
                LOGGER.warning(f"No TMDb TV result for '{title}'")
                return None
            tmdb_id = tmdb_search.id

        # Fetch full TV show details
        tv = await _tmdb_tv_details(tmdb_id)
        if not tv:
            LOGGER.warning(f"TMDb TV details failed for id={tmdb_id}")
            return None

        # Fetch episode (TR ve DE ayrı ayrı — her ikisi de doğrudan TMDB'den)
        ep = await _tmdb_episode_details(tmdb_id, season, episode, tmdb_tr)
        ep_de = await _tmdb_episode_details(tmdb_id, season, episode, tmdb_de)

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

        # Bölüm başlığı/özeti: önce doğrudan TMDB (tr-TR / de-DE), boşsa Google çeviri
        ep_name_fallback = getattr(ep, "name", f"S{season}E{episode}") if ep else f"S{season}E{episode}"
        ep_overview_source = getattr(ep, "overview", "") if ep else ""
        ep_title_tr_tmdb = (getattr(ep, "name", "") or "") if ep else ""
        ep_title_de_tmdb = (getattr(ep_de, "name", "") or "") if ep_de else ""
        ep_overview_tr_tmdb = (getattr(ep, "overview", "") or "") if ep else ""
        ep_overview_de_tmdb = (getattr(ep_de, "overview", "") or "") if ep_de else ""
        final_ep_title_tr = ep_title_tr_tmdb if ep_title_tr_tmdb else (await asyncio.to_thread(translate_text_safe, ep_name_fallback) if ep else ep_name_fallback)
        final_ep_title_de = ep_title_de_tmdb if ep_title_de_tmdb else (await asyncio.to_thread(translate_text_safe_de, ep_name_fallback) if ep else ep_name_fallback)
        final_ep_overview_tr = ep_overview_tr_tmdb if ep_overview_tr_tmdb else (await asyncio.to_thread(translate_text_safe, ep_overview_source) if ep_overview_source else "")
        final_ep_overview_de = ep_overview_de_tmdb if ep_overview_de_tmdb else (await asyncio.to_thread(translate_text_safe_de, ep_overview_source) if ep_overview_source else "")

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
            # tv zaten tmdb_tr (tr-TR) ile çekildiği için overview'ı doğrudan
            # Türkçe'dir — Google çeviriye sadece TMDB boş döndüyse düşülür.
            "description_tr": tv.overview or await asyncio.to_thread(translate_text_safe, tv.overview),
            "description_de": getattr(tv, "overview_de", "") or await asyncio.to_thread(translate_text_safe_de, tv.overview),
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
            "episode_title": ep_name_fallback,
            "episode_title_tr": final_ep_title_tr,
            "episode_title_de": final_ep_title_de,
            "episode_backdrop": format_tmdb_image(getattr(ep, "still_path", None), "original") if ep else "",
            "episode_overview": ep_overview_source,
            "episode_overview_tr": final_ep_overview_tr,
            "episode_overview_de": final_ep_overview_de,
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
    tr_desc_tmdb = ""  # TMDB'den (tr-TR) gelen açıklama — eşleşme doğrulanınca dolar
    de_desc = ""  # Başlangıçta boş; TMDB'den Almanca açıklama alınacak
    tr_genres_tmdb: list = []  # TMDB'den (tr-TR) gelen tür adları — eşleşme doğrulanınca dolar
    poster_tr, backdrop_tr, logo_tr = "", "", ""
    poster_de, backdrop_de, logo_de = "", "", ""
    genres_de = []
    certification_tr = certification_de = certification_us = None
    series_status = None
    tv_details = None
    # TMDB, aradığı imdb_id ile eşleşmeyen (yanlış) bir kayıt döndürebilir.
    # Bu durumda TMDB'nin TR/DE metin verisi (başlık, açıklama, tür) YANLIŞ
    # içeriğe ait olabileceğinden kullanılmaz; sadece görsel/sertifika gibi
    # zaten var olan davranış korunur. imdb eşleştiği DOĞRULANMADIKÇA metin
    # alanları için TMDB'ye güvenilmez.
    imdb_match = False

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
                poster_tr = getattr(tv_details, "poster_tr", "") or ""
                backdrop_tr = getattr(tv_details, "backdrop_tr", "") or ""
                logo_tr = getattr(tv_details, "logo_tr", "") or ""
                poster_de = getattr(tv_details, "poster_de", "") or ""
                backdrop_de = getattr(tv_details, "backdrop_de", "") or ""
                logo_de = getattr(tv_details, "logo_de", "") or ""
                series_status = getattr(tv_details, "status", None)
                # TMDB'den gelen imdb_id ile DB'ye yazılacak imdb_id aynıysa
                # (ya da TMDB imdb_id döndürmüyorsa) eşleşme doğrulanmış sayılır;
                # sertifikalar VE TR/DE metin alanları (başlık, açıklama, tür)
                # bu durumda TMDB'den kullanılır — aksi halde Google çeviriye
                # düşülür.
                tmdb_ext_imdb = getattr(getattr(tv_details, "external_ids", None), "imdb_id", None)
                if not tmdb_ext_imdb or tmdb_ext_imdb == imdb_id:
                    imdb_match = True
                    certification_tr = getattr(tv_details, "certification_tr", None)
                    certification_de = getattr(tv_details, "certification_de", None)
                    certification_us = getattr(tv_details, "certification_us", None)
                    tr_title = tv_details.name or tr_title
                    de_title = getattr(tv_details, "name_de", "") or de_title
                    # TR açıklama: tmdb_tr (tr-TR) çağrısından gelen overview
                    tr_desc_tmdb = tv_details.overview or ""
                    # DE açıklama: TMDB'den (İngilizce IMDb plot değil)
                    de_desc = getattr(tv_details, "overview_de", "") or ""
                    tr_genres_tmdb = [g.name for g in (getattr(tv_details, "genres", None) or [])]
                    genres_de = getattr(tv_details, "genres_de", []) or []
        except Exception as e:
            LOGGER.warning(f"IMDb TV mode: TMDb TR/DE enrichment failed [{imdb_id}] -> {e}")

    # poster/backdrop/logo: her zaman metahub
    final_poster = imdb_images["poster"]
    final_backdrop = imdb_images["backdrop"]
    final_logo = imdb_images["logo"]

    # TR açıklama: imdb eşleşmesi doğrulanıp TMDB'den geldiyse onu kullan,
    # yoksa Google çeviriye düş.
    final_desc_tr = tr_desc_tmdb if tr_desc_tmdb else await asyncio.to_thread(translate_text_safe, imdb.get("plot", ""))
    # Almanca açıklama: TMDB'den Almanca geldiyse kullan, yoksa İngilizce plot'u çevir
    final_desc_de = de_desc if de_desc else await asyncio.to_thread(translate_text_safe_de, imdb.get("plot", ""))

    # ----- Bölüm (episode) alanları: önce TMDB'den denenir (imdb eşleşmesi
    # doğrulanmışsa), TMDB'de bulunamazsa/boşsa Google çeviriye düşülür.
    tmdb_ep_tr = tmdb_ep_de = None
    if imdb_match and fallback_tmdb_id:
        tmdb_ep_tr = await _tmdb_episode_details(fallback_tmdb_id, season, episode, tmdb_tr)
        tmdb_ep_de = await _tmdb_episode_details(fallback_tmdb_id, season, episode, tmdb_de)

    ep_title_tr_tmdb = (getattr(tmdb_ep_tr, "name", "") or "") if tmdb_ep_tr else ""
    ep_title_de_tmdb = (getattr(tmdb_ep_de, "name", "") or "") if tmdb_ep_de else ""
    ep_overview_tr_tmdb = (getattr(tmdb_ep_tr, "overview", "") or "") if tmdb_ep_tr else ""
    ep_overview_de_tmdb = (getattr(tmdb_ep_de, "overview", "") or "") if tmdb_ep_de else ""

    ep_title_source = ep.get("title", f"S{season}E{episode}")
    ep_overview_source = ep.get("plot", "")

    final_ep_title_tr = ep_title_tr_tmdb if ep_title_tr_tmdb else await asyncio.to_thread(translate_text_safe, ep_title_source)
    final_ep_title_de = ep_title_de_tmdb if ep_title_de_tmdb else await asyncio.to_thread(translate_text_safe_de, ep_title_source)
    final_ep_overview_tr = ep_overview_tr_tmdb if ep_overview_tr_tmdb else await asyncio.to_thread(translate_text_safe, ep_overview_source)
    final_ep_overview_de = ep_overview_de_tmdb if ep_overview_de_tmdb else await asyncio.to_thread(translate_text_safe_de, ep_overview_source)

    return {
        "tmdb_id": raw_tmdb_id or imdb_id.replace("tt", ""),
        "imdb_id": imdb_id,
        "title": imdb.get("title") or title,
        "title_tr": tr_title,
        "title_de": de_title,
        "year": imdb.get("releaseDetailed", {}).get("year", 0),
        "rate": imdb.get("rating", {}).get("star", 0),
        "description": imdb.get("plot", ""),
        "description_tr": final_desc_tr,
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
        "genres_tr": tur_genre_normalize(tr_genres_tmdb) if tr_genres_tmdb else tur_genre_normalize(imdb.get("genre", [])),
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
        "episode_title_tr": final_ep_title_tr,
        "episode_title_de": final_ep_title_de,
        "episode_backdrop": ep.get("image", ""),
        "episode_overview": ep.get("plot", ""),
        "episode_overview_tr": final_ep_overview_tr,
        "episode_overview_de": final_ep_overview_de,
        "episode_released": str(ep.get("released", "")),

        "quality": quality,
        "encoded_string": encoded_string,
    }


# ----------------- Movie Metadata -----------------
async def fetch_movie_metadata(title, encoded_string, year=None, quality=None, default_id=None, alt_title=None) -> dict | None:
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
            result = await _fetch_movie_metadata_impl(title, encoded_string, year, quality, default_id, alt_title)
            fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            _METADATA_IN_FLIGHT.pop(dedup_key, None)

    return await _fetch_movie_metadata_impl(title, encoded_string, year, quality, default_id, alt_title)


async def _fetch_movie_metadata_impl(title, encoded_string, year=None, quality=None, default_id=None, alt_title=None) -> dict | None:
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
            if not tmdb_result and alt_title:
                # Bkz. fetch_tv_metadata — bazı dosya adlarında başlık sırası
                # ters olabiliyor, alternatif başlıkla tekrar denenir.
                LOGGER.info(f"No TMDb movie result for '{title}' — trying alt title '{alt_title}'")
                tmdb_result = await safe_tmdb_search(alt_title, "movie", year)
                if tmdb_result:
                    title = alt_title
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

        # Yönetmen(ler): crew içindeki "Director" görevindekiler
        crew_arr = getattr(credits, "crew", []) or []
        director_names = [
            getattr(c, "name", None) or getattr(c, "original_name", None)
            for c in crew_arr
            if (getattr(c, "job", "") or "").lower() == "director"
        ]
        director_names = [d for d in director_names if d]

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
            # movie zaten tmdb_tr (tr-TR) ile çekildiği için overview'ı doğrudan
            # Türkçe'dir — Google çeviriye sadece TMDB boş döndüyse düşülür.
            "description_tr": movie.overview or await asyncio.to_thread(translate_text_safe, movie.overview),
            "description_de": getattr(movie, "overview_de", "") or await asyncio.to_thread(translate_text_safe_de, movie.overview),
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
            "director": director_names,
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
    tr_desc_tmdb = ""  # TMDB'den (tr-TR) gelen açıklama — eşleşme doğrulanınca dolar
    de_desc = ""  # Başlangıçta boş; TMDB'den Almanca açıklama alınacak
    tr_genres_tmdb: list = []  # TMDB'den (tr-TR) gelen tür adları — eşleşme doğrulanınca dolar
    poster_tr, backdrop_tr, logo_tr = "", "", ""
    poster_de, backdrop_de, logo_de = "", "", ""
    genres_de = []
    certification_tr = certification_de = certification_us = None
    _fallback_collection_id = None  # TMDB enrichment yoksa da tanımlı olsun
    movie_details = None

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
                poster_tr = getattr(movie_details, "poster_tr", "") or ""
                backdrop_tr = getattr(movie_details, "backdrop_tr", "") or ""
                logo_tr = getattr(movie_details, "logo_tr", "") or ""
                poster_de = getattr(movie_details, "poster_de", "") or ""
                backdrop_de = getattr(movie_details, "backdrop_de", "") or ""
                logo_de = getattr(movie_details, "logo_de", "") or ""
                # TMDB'den gelen imdb_id ile DB'ye yazılacak imdb_id aynıysa
                # (ya da TMDB imdb_id döndürmüyorsa) eşleşme doğrulanmış sayılır;
                # sertifikalar VE TR/DE metin alanları (başlık, açıklama, tür)
                # bu durumda TMDB'den kullanılır — aksi halde Google çeviriye
                # düşülür.
                tmdb_ext_imdb = getattr(getattr(movie_details, "external_ids", None), "imdb_id", None)
                if not tmdb_ext_imdb or tmdb_ext_imdb == imdb_id:
                    certification_tr = getattr(movie_details, "certification_tr", None)
                    certification_de = getattr(movie_details, "certification_de", None)
                    certification_us = getattr(movie_details, "certification_us", None)
                    tr_title = movie_details.title or tr_title
                    de_title = getattr(movie_details, "title_de", "") or de_title
                    # TR açıklama: tmdb_tr (tr-TR) çağrısından gelen overview
                    tr_desc_tmdb = movie_details.overview or ""
                    # DE açıklama: TMDB'den (İngilizce IMDb plot değil)
                    de_desc = getattr(movie_details, "overview_de", "") or ""
                    tr_genres_tmdb = [g.name for g in (getattr(movie_details, "genres", None) or [])]
                    genres_de = getattr(movie_details, "genres_de", []) or []
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

    # TR açıklama: imdb eşleşmesi doğrulanıp TMDB'den geldiyse onu kullan,
    # yoksa Google çeviriye düş.
    final_desc_tr = tr_desc_tmdb if tr_desc_tmdb else await asyncio.to_thread(translate_text_safe, imdb.get("plot", ""))
    # Almanca açıklama: TMDB'den Almanca geldiyse kullan, yoksa İngilizce plot'u çevir
    final_desc_de = de_desc if de_desc else await asyncio.to_thread(translate_text_safe_de, imdb.get("plot", ""))

    return {
        "tmdb_id": raw_tmdb_id or imdb_id.replace("tt", ""),
        "imdb_id": imdb_id,
        "title": imdb.get("title") or title,
        "title_tr": tr_title,
        "title_de": de_title,
        "year": imdb.get("releaseDetailed", {}).get("year", 0),
        "rate": imdb.get("rating", {}).get("star", 0),
        "description": imdb.get("plot", ""),
        "description_tr": final_desc_tr,
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
        "director": imdb.get("director", []),
        "runtime": str(imdb.get("runtime") or ""),
        "media_type": "movie",
        "genres": imdb.get("genre", []),
        "genres_tr": tur_genre_normalize(tr_genres_tmdb) if tr_genres_tmdb else tur_genre_normalize(imdb.get("genre", [])),
        "genres_de": de_genre_normalize(genres_de) if genres_de else de_genre_normalize(imdb.get("genre", [])),
        "collection_id": _fallback_collection_id,
        "certification_tr": certification_tr,
        "certification_de": certification_de,
        "certification_us": certification_us,
        "original_language": imdb.get("spokenLanguages", [{}])[0].get("id") if imdb.get("spokenLanguages") else None,
        "quality": quality,
        "encoded_string": encoded_string,
    }
