import logging
_logger = logging.getLogger(__name__)
from fastapi import APIRouter, HTTPException, Depends, Request
from typing import Optional
from urllib.parse import unquote, unquote_plus
from Backend.config import Telegram
from Backend import db, __version__
from Backend.helper.platform_catalog import platform_catalog, PLATFORM_LABELS
import PTN
from datetime import datetime, timezone, timedelta
from Backend.fastapi.security.tokens import verify_token
from Backend.fastapi.security.credentials import require_auth
import time as _time


# --- Configuration ---
BASE_URL = Telegram.BASE_URL
ADDON_NAME = Telegram.ISIM
ADDON_VERSION = __version__
PAGE_SIZE = 15

# ── "Sana Özel" Bellek Cache ────────────────────────────────────────────────────
# Yapı: { (token, lang): {"items": list, "cached_at": float} }
# Her üye için 60 içerik RAM'de tutulur, 30 dakikada bir yenilenir.
_SIMILAR_CACHE: dict = {}
_SIMILAR_CACHE_TTL = 30 * 60  # 30 dakika (saniye)


def _similar_cache_get(token: str, lang: str):
    """Cache'te geçerli kayıt varsa döner, yoksa None."""
    key = (token, lang)
    entry = _SIMILAR_CACHE.get(key)
    if entry is None:
        return None
    if _time.monotonic() - entry["cached_at"] > _SIMILAR_CACHE_TTL:
        del _SIMILAR_CACHE[key]
        return None
    return entry["items"]


def _similar_cache_set(token: str, lang: str, items: list) -> None:
    """60 içeriği cache'e yazar."""
    _SIMILAR_CACHE[(token, lang)] = {
        "items": items,
        "cached_at": _time.monotonic(),
    }


async def _similar_cache_cleanup_loop() -> None:
    """Her saat başı TTL'i dolmuş cache girişlerini temizler.
    Uygulama yeniden başlatılmadan uzun süre çalıştığında
    RAM'in şişmesini önler.
    main.py _startup() içinde create_task ile başlatılır."""
    import asyncio
    while True:
        await asyncio.sleep(3600)  # 1 saat bekle
        now = _time.monotonic()
        expired_keys = [
            k for k, v in list(_SIMILAR_CACHE.items())
            if now - v["cached_at"] > _SIMILAR_CACHE_TTL
        ]
        for k in expired_keys:
            _SIMILAR_CACHE.pop(k, None)


router = APIRouter(prefix="/stremio", tags=["Stremio Addon"])

# Define available genres
GENRES = [
    "Aile", "Aksiyon", "Aksiyon ve Macera", "Animasyon", "Belgesel",
    "Bilim Kurgu", "Bilim Kurgu ve Fantazi", "Biyografi", "Çocuklar",
    "Dram", "Fantastik", "Gerilim", "Gerçeklik", "Gizem", "Haberler",
    "Kara Film", "Komedi", "Korku", "Kısa", "Macera", "Müzik",
    "Müzikal", "Oyun Gösterisi", "Pembe Dizi", "Romantik", "Savaş",
    "Savaş ve Politika", "Spor", "Suç", "TV Filmi", "Talk-Show",
    "Tarih", "Vahşi Batı"
]

SUPPORTED_LANGS = ("tr", "de", "en")

LANG_LABELS = {
    "tr": {
        "new": "Yeni eklenen", "popular": "Popüler", "movies": "Filmler",
        "series": "Diziler", "collections": "Seriler", "live": "Canlı Yayın",
        "tmdb_trending": "🔥 Trendler",

        "yerli_movies": "🇹🇷 Yerli Filmler",
        "yerli_series": "🇹🇷 Yerli Diziler",
        "similar": "🎯 Sana Özel",
    },
    "de": {
        "new": "Neu hinzugefügt", "popular": "Beliebt", "movies": "Filme",
        "series": "Serien", "collections": "Filmreihen", "live": "Live TV",
        "tmdb_trending": "🔥 Trends",

        "yerli_movies": "🇹🇷 Türkische Filme",
        "yerli_series": "🇹🇷 Türkische Serien",
        "similar": "🎯 Empfohlen für Sie",
    },
    "en": {
        "new": "Recently Added", "popular": "Popular", "movies": "Movies",
        "series": "Series", "collections": "Collections", "live": "Live TV",
        "tmdb_trending": "🔥 Trending",

        "yerli_movies": "🇹🇷 Turkish Movies",
        "yerli_series": "🇹🇷 Turkish Series",
        "similar": "🎯 Recommended For You",
    },
}

def resolve_lang(lang: str) -> str:
    return lang if lang in SUPPORTED_LANGS else "tr"

def is_original_lang(lang: str) -> bool:
    return lang == "en"

GENRES_TR = GENRES  # Türkçe (mevcut liste)

GENRES_DE = [
    "Abenteuer", "Action", "Action & Abenteuer", "Animation", "Biografie",
    "Dokumentarfilm", "Drama", "Familie", "Fantasy", "Film Noir",
    "Geschichte", "Horror", "Kinder", "Komödie", "Krieg",
    "Krieg & Politik", "Krimi", "Kurzfilm", "Musical", "Musik",
    "Mystery", "Nachrichten", "Reality-TV", "Romantik", "Science-Fiction",
    "Science-Fiction & Fantasy", "Seifenoper", "Sport", "TV-Film", "Talkshow",
    "Thriller", "Western",
]

GENRES_EN = [
    "Action", "Action & Adventure", "Adventure", "Animation", "Biography",
    "Comedy", "Crime", "Documentary", "Drama", "Family",
    "Fantasy", "Film-Noir", "Game-Show", "History", "Horror",
    "Kids", "Music", "Musical", "Mystery", "News",
    "Reality", "Romance", "Sci-Fi", "Sci-Fi & Fantasy", "Soap",
    "Sport", "TV Movie", "Talk", "Thriller", "War",
    "War & Politics", "Western",
]

def get_genres_for_lang(lang: str) -> list:
    if lang == "de":
        return GENRES_DE
    elif lang == "en":
        return GENRES_EN
    return GENRES_TR

# Canlı yayın tür filtreleri (dile göre)
LIVE_GENRES_TR = ["Ulusal", "Haber", "Spor", "Belgesel", "Sinema", "Çocuk", "Müzik", "Eğlence", "Yaşam", "Dini"]
LIVE_GENRES_DE = ["National", "Nachrichten", "Sport", "Dokumentation", "Kino", "Kinder", "Musik", "Unterhaltung", "Lifestyle", "Religiös"]
LIVE_GENRES_EN = ["National", "News", "Sports", "Documentary", "Cinema", "Kids", "Music", "Entertainment", "Lifestyle", "Religious"]

def get_live_genres_for_lang(lang: str) -> list:
    if lang == "de":
        return LIVE_GENRES_DE
    elif lang == "en":
        return LIVE_GENRES_EN
    return LIVE_GENRES_TR

# Yıl kataloğu için sabit yıl listesi (2026'dan 2000'e)
YEAR_OPTIONS = [str(y) for y in range(2026, 1919, -1)]

LANG_LABELS_YEAR = {
    "tr": "Yıl",
    "de": "Jahr",
    "en": "Year",
}

# ── Admin panelinden açılıp kapatılabilen hazır (built-in) kataloglar ─────────
# Sözlük anahtarı: manifest'teki lang-bağımsız temel katalog id'si (_base_id).
# "tmdb_trending" ve "similar" film+dizi karışık olduğundan type=movie,
# platform katalogları ise sadece dizi (mevcut davranışla aynı).
TOGGLEABLE_BUILTIN_CATALOGS: dict = {
    "tmdb_trending": {"label": "🔥 Trendler", "type": "movie"},
    "similar":       {"label": "🎯 Sana Özel", "type": "movie"},
    **{
        f"platform_{key}": {"label": PLATFORM_LABELS[key], "type": "series"}
        for key in PLATFORM_LABELS
    },
}


def format_released_date(media):
    year = media.get("release_year")
    if year:
        try:
            return datetime(int(year), 1, 1).isoformat() + "Z"
        except:
            return None

    return None

# --- Helper Functions ---
def convert_to_stremio_meta(item: dict, lang: str = "tr") -> dict:
    media_type = "series" if item.get("media_type") == "tv" else "movie"
    lang = resolve_lang(lang)

    if lang == "de":
        name = item.get("title_de") or item.get("title", "")
        description = item.get("description_de") or item.get("description", "")
        genres = item.get("genres_de") or item.get("genres") or []
    elif lang == "en":
        name = item.get("title", "")
        description = item.get("description", "")
        genres = item.get("genres") or []
    else:  # tr
        name = item.get("title_tr") or item.get("title", "")
        description = item.get("description_tr") or item.get("description", "")
        genres = item.get("genres_tr") or item.get("genres") or []

    if lang == "de":
        poster = item.get("poster_de") or item.get("poster") or ""
        backdrop = item.get("backdrop_de") or item.get("backdrop") or ""
        logo = item.get("logo_de") or item.get("logo") or ""
    elif lang == "en":
        poster = item.get("poster") or ""
        backdrop = item.get("backdrop") or ""
        logo = item.get("logo") or ""
    else:  # tr
        poster = item.get("poster_tr") or item.get("poster") or ""
        backdrop = item.get("backdrop_tr") or item.get("backdrop") or ""
        logo = item.get("logo_tr") or item.get("logo") or ""

    meta = {
        "id": item.get('imdb_id'),
        "type": media_type,
        "name": name,
        "poster": poster,
        "logo": logo,
        "year": item.get("release_year"),
        "releaseInfo": str(item.get("release_year", "")),
        "imdb_id": item.get("imdb_id", ""),
        "moviedb_id": item.get("tmdb_id", ""),
        "background": backdrop,
        "genres": genres,
        "imdbRating": str(item.get("rating") or ""),
        "description": description,
        "cast": item.get("cast") or [],
        "runtime": item.get("runtime") or "",
    }

    return meta


# Dil kodu → bayrak emoji eşlemesi
_LANG_FLAGS: dict[str, str] = {
    "tr": "🇹🇷", "tur": "🇹🇷", "turkish": "🇹🇷",
    "en": "🇬🇧", "eng": "🇬🇧", "english": "🇬🇧",
    "de": "🇩🇪", "ger": "🇩🇪", "deu": "🇩🇪", "german": "🇩🇪",
    "fr": "🇫🇷", "fre": "🇫🇷", "fra": "🇫🇷", "french": "🇫🇷",
    "es": "🇪🇸", "spa": "🇪🇸", "spanish": "🇪🇸",
    "ita": "🇮🇹", "italian": "🇮🇹",
    "pt": "🇵🇹", "por": "🇵🇹", "portuguese": "🇵🇹",
    "ru": "🇷🇺", "rus": "🇷🇺", "russian": "🇷🇺",
    "ar": "🇸🇦", "ara": "🇸🇦", "arabic": "🇸🇦",
    "ja": "🇯🇵", "jpn": "🇯🇵", "japanese": "🇯🇵",
    "ko": "🇰🇷", "kor": "🇰🇷", "korean": "🇰🇷",
    "zh": "🇨🇳", "chi": "🇨🇳", "chinese": "🇨🇳",
    "pl": "🇵🇱", "pol": "🇵🇱", "polish": "🇵🇱",
    "nl": "🇳🇱", "dut": "🇳🇱", "dutch": "🇳🇱",
    "sv": "🇸🇪", "swe": "🇸🇪", "swedish": "🇸🇪",
    "no": "🇳🇴", "nor": "🇳🇴", "norwegian": "🇳🇴",
    "da": "🇩🇰", "dan": "🇩🇰", "danish": "🇩🇰",
    "fi": "🇫🇮", "fin": "🇫🇮", "finnish": "🇫🇮",
    "cs": "🇨🇿", "cze": "🇨🇿", "czech": "🇨🇿",
    "hu": "🇭🇺", "hun": "🇭🇺", "hungarian": "🇭🇺",
    "ro": "🇷🇴", "rum": "🇷🇴", "romanian": "🇷🇴",
    "el": "🇬🇷", "gre": "🇬🇷", "greek": "🇬🇷",
    "he": "🇮🇱", "heb": "🇮🇱", "hebrew": "🇮🇱",
    "hi": "🇮🇳", "hin": "🇮🇳", "hindi": "🇮🇳",
}

import re as _re

# "IT" tek başına iTunes anlamına gelir (dil değil); dil için "ITA" veya "ITALIAN" kullan
_IT_IS_ITUNES = {"it"}

# ── Arşiv / video tespiti (katalog filtresi için modül seviyesinde) ──────────
_ARCHIVE_EXTS_MOD = (".zip", ".7z", ".rar")
_ALLOWED_VIDEO_EXTS_MOD = (".mkv", ".avi", ".mpg", ".mpeg", ".mp4", ".ts", ".m4v", ".webm", ".flv", ".mov", ".wmv")

def _is_archive_fn(name: str) -> bool:
    n = name.lower()
    if n.endswith(_ARCHIVE_EXTS_MOD):
        return True
    if _re.search(r'\.(zip|7z|rar|z)\.\d+$', n):
        return True
    if _re.search(r'\.part\d+\.rar$', n):
        return True
    return False

def _is_split_video(q: dict) -> bool:
    """Split video dosyası mı? (.mkv.001 gibi) parts listesi varsa veya adı video+numeri ile bitiyorsa."""
    import re as _re_sv
    name = (q.get("name") or "").lower()
    if _re_sv.search(r'\.(mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv)\.\d+$', name):
        return True
    if q.get("parts") and not q.get("is_archive", False):
        return True
    return False

def _has_video_stream(item: dict) -> bool:
    """Film/dizi item'ının gerçek oynatılabilir video stream'i var mı kontrol eder.
    Sadece arşiv dosyası olan (zip/7z vb.) içerikler için False döner.
    Diziler için season/episode bazlı yapıyı da kontrol eder."""
    def _check_qualities(qualities):
        for q in qualities:
            name = q.get("name", "")
            if q.get("is_archive", False):
                continue
            if _is_archive_fn(name):
                continue
            # Split video dosyaları (.mkv.001) doğrudan geçer
            if _is_split_video(q):
                return True
            if any(name.lower().endswith(ext) for ext in _ALLOWED_VIDEO_EXTS_MOD):
                return True
        return False

    # Film: doğrudan item kökündeki telegram alanı
    qualities = item.get("telegram", [])
    if qualities:
        if _check_qualities(qualities):
            return True

    # Dizi: season > episode > telegram yapısını kontrol et
    for season in item.get("seasons", []):
        for episode in season.get("episodes", []):
            ep_qualities = episode.get("telegram", [])
            if ep_qualities and _check_qualities(ep_qualities):
                return True

    return False

def _extract_lang_flags(filename: str) -> str:
    """
    Dosya adındaki dil etiketlerini tespit edip bayrak emojilerine çevirir.
    """
    name_upper = filename.upper()
    flags = []
    
    # 1. Öncelikli tam kelime kontrolü (Örn: German, Turkish vb.)
    if "GERMAN" in name_upper:
        flags.append("🇩🇪")
    if "TURKISH" in name_upper:
        flags.append("🇹🇷")
    if "ENGLISH" in name_upper:
        flags.append("🇬🇧")

    # 2. Köşeli parantez kontrolü [TR-EN]
    bracket_match = _re.search(r'\[([A-Z]{2,3}(?:[-. ][A-Z]{2,3})*)\]', name_upper)
    if bracket_match:
        parts = _re.split(r'[-. ]', bracket_match.group(1))
        for p in parts:
            key = p.lower()
            if key in _LANG_FLAGS:
                f = _LANG_FLAGS[key]
                if f not in flags:
                    flags.append(f)

    # 3. Nokta, Tire veya Alt Çizgi ile ayrılmış kısa kodlar: .TR. .DE. -EN-
    # Boşluk dahil edilmiyor — film adındaki kelimeler (ör. "ya DA diri") yanlış eşleşmesin
    inline = _re.findall(r'(?<=[.\-_])([A-Z]{2,3})(?=[.\-_])', name_upper)
    for code in inline:
        key = code.lower()
        if key in _IT_IS_ITUNES:
            continue
        if key in _LANG_FLAGS:
            f = _LANG_FLAGS[key]
            if f not in flags:
                flags.append(f)

    return " ".join(flags)

    # Nokta/tire ile ayrılmış dil kodları: .TR.EN. veya -TR-EN-
    inline = _re.findall(r'(?<=[.\-_])([A-Z]{2,3})(?=[.\-_])', name_upper)
    flags = []
    for code in inline:
        key = code.lower()
        if key in _IT_IS_ITUNES:
            continue  # IT = iTunes, bayrak ekleme
        if key in _LANG_FLAGS:
            f = _LANG_FLAGS[key]
            if f not in flags:
                flags.append(f)
    return " ".join(flags)


def _resolution_badge(resolution: str) -> str:
    """Çözünürlüğü Stremio'nun tanıdığı badge keyword'üne çevirir."""
    r = (resolution or "").lower()
    if r in ("2160p", "4k", "uhd"):
        return "4K"
    if r in ("1080p", "fhd"):
        return "FHD"
    if r in ("720p", "hd"):
        return "HD"
    if r in ("480p", "sd", "576p"):
        return "SD"
    return resolution.upper() if resolution else ""


def _audio_badge(audio: str) -> str:
    """
    Ses formatını Stremio'nun tanıdığı badge keyword'üne normalleştirir.
    Stremio: 'Dolby', 'Dolby Atmos', 'DTS', 'DTS-X', 'DD+', 'Digital+'
    """
    if not audio:
        return ""
    a = audio.upper()
    if "ATMOS" in a:
        return "Dolby Atmos"
    if "TRUEHD" in a:
        return "Dolby TrueHD"
    if "DDP" in a or "DD+" in a or "EAC3" in a or "EAC-3" in a:
        return "Digital+"
    if "DTS-X" in a or "DTSX" in a:
        return "DTS-X"
    if "DTS-HD" in a or "DTSHD" in a:
        return "DTS-HD"
    if "DTS" in a:
        return "DTS"
    if "AC3" in a or "AC-3" in a or "DD" in a:
        return "Dolby"
    if "AAC" in a:
        return "AAC"
    return audio  # bilinmiyorsa orijinalini döndür


def _channel_badge(audio: str) -> str:
    """5.1, 7.1 gibi kanal bilgisini döndürür."""
    if not audio:
        return ""
    m = _re.search(r'(\d\.\d)', audio)
    return m.group(1) if m else ""


def _clean_encoder(encoder: str) -> str:
    """
    PTN'nin 'encoder' alanı bazen gruptan önceki fazlalık metni de
    (dil/kaynak etiketleri vb.) beraberinde döndürür, örn:
    'TR.Yerli.Filmbol-Butche89' -> asıl grup adı yalnızca 'Butche89'.
    Gerçek grup adı her zaman son '-' işaretinden sonra gelir; varsa
    onu ayıklayıp döndürür, yoksa değeri olduğu gibi bırakır.
    """
    if not encoder:
        return encoder
    enc = str(encoder).strip()
    if "-" in enc:
        enc = enc.rsplit("-", 1)[-1].strip()
    # "TR.Filmbol.Yerli", "Türkçe.Altyazı.FilmbolSeries", "TR.ENG.FilmbolSeries"
    # gibi dil/altyazı etiketleriyle birlikte gelen varyasyonları sade
    # "Filmbol" olarak normalize et.
    if _re.search(r'(?i)filmbol', enc):
        return "Filmbol"
    return enc


def _hdr_badge(parsed: dict, filename: str) -> str:
    """HDR tipini döndürür: HDR10+, HDR10, DV, HDR."""
    hdr = parsed.get("hdr")
    fn_upper = filename.upper()
    if hdr:
        hdr_str = hdr if isinstance(hdr, str) else " ".join(hdr) if isinstance(hdr, list) else str(hdr)
        hdr_up = hdr_str.upper()
        if "DOLBY VISION" in hdr_up or "DV" in hdr_up:
            return "DV"
        if "HDR10+" in hdr_up:
            return "HDR10+"
        if "HDR10" in hdr_up:
            return "HDR10"
        return hdr_str
    # PTN bazen HDR'ı atlayabilir, dosya adından kontrol et
    if "DOLBY.VISION" in fn_upper or "DOVI" in fn_upper or ".DV." in fn_upper:
        return "DV"
    if "HDR10+" in fn_upper:
        return "HDR10+"
    if "HDR10" in fn_upper:
        return "HDR10"
    if "HDR" in fn_upper:
        return "HDR"
    return ""


def format_stream_details(filename: str, quality: str, size: str, file_id: str, certification: str = "", is_split: bool = False) -> tuple[str, str]:
    # Kaynak: Link mi Telegram mı?
    source_prefix = "Link" if file_id.startswith(("http://", "https://")) else Telegram.ISIM

    # Kesilmiş (parçalı/split) dosyalarda boyut emojisi 📦, normal dosyalarda 💾
    size_emoji = "📦" if is_split else "💾"

    try:
        parsed = PTN.parse(filename)
    except Exception:
        return (f"{source_prefix} {quality}", f"📁 {filename}\n{size_emoji} {size}")

    # --- Temel alanlar ---
    resolution   = parsed.get("resolution", quality)
    quality_type = parsed.get("quality", "")
    audio        = parsed.get("audio", "")
    codec        = parsed.get("codec", "")
    bit_depth    = parsed.get("bitDepth", "")
    encoder      = _clean_encoder(parsed.get("encoder", ""))
    subtitles    = parsed.get("subtitles", "")
    extended     = parsed.get("extended", False)
    proper       = parsed.get("proper", False)
    repack       = parsed.get("repack", False)
    container    = parsed.get("container", "")
    site_raw     = parsed.get("site", "")

    # --- Bilinen Türk encoder grupları (PTN bunları bazen kaçırır) ---
    _EXTRA_ENCODERS = {
        "filmbol", "turg", "hdt", "tsrg", "bitturk", "btrg", "tork", "butche89", "turkseed", "filmbolseries", "uhdfilmindir",
    }
    if not encoder:
        fn_lower = filename.lower()
        for enc in _EXTRA_ENCODERS:
            import re as _re_enc
            if _re_enc.search(r'(?<![a-z])' + enc + r'(?![a-z])', fn_lower):
                encoder = "Filmbol" if "filmbol" in enc else enc.upper()
                break

    # --- stream_name: "Kartal 1080p AMZN WEB-DL" ---
    name_parts = [source_prefix, resolution or quality]
    if site_raw:
        name_parts.append(site_raw.upper())
    if quality_type:
        name_parts.append(quality_type)
    stream_name = " ".join(p for p in name_parts if p).strip()

    # --- HDR ---
    hdr_badge = _hdr_badge(parsed, filename)

    # --- Platform adı ---
    _SITE_NAMES = {
        "amzn": "Amazon", "amazon": "Amazon",
        "nf": "Netflix", "netflix": "Netflix",
        "dsnp": "Disney+", "disney": "Disney+", "IT": "iTunes",
        "hmax": "Max", "hbo": "HBO Max", "exxen": "Exxen", "max": "Max",
        "atvp": "Apple TV+", "apple": "Apple TV+", "it": "iTunes",
        "hulu": "Hulu", "pcok": "Peacock", "peacock": "Peacock",
        "pmtp": "Paramount+", "paramount": "Paramount+",
        "itvx": "ITVX", "bbc": "BBC iPlayer",
    }
    site_display = _SITE_NAMES.get(site_raw.lower(), site_raw) if site_raw else ""

    # --- Altyazı dillerini bayraklara çevir ---
    def _subtitle_flags(subs) -> str:
        if not subs:
            return ""
        sub_list = subs if isinstance(subs, list) else [subs]
        flags = []
        for s in sub_list:
            key = s.lower().strip()
            f = _LANG_FLAGS.get(key)
            if f and f not in flags:
                flags.append(f)
            elif not f:
                flags.append(s)
        return " ".join(flags)

    sub_flags = _subtitle_flags(subtitles)

    # --- Dil bayrakları ---
    lang_flags = _extract_lang_flags(filename)

    # Dosya adında "dual" geçiyorsa 🇹🇷 ve 🇬🇧 ekle
    if "dual" in filename.lower():
        dual_flags = []
        if "🇹🇷" not in lang_flags:
            dual_flags.append("🇹🇷")
        if "🇬🇧" not in lang_flags:
            dual_flags.append("🇬🇧")
        if dual_flags:
            lang_flags = (lang_flags + "  " + " ".join(dual_flags)).strip()

    # --- FPS (dosya adından regex) ---
    import re as _re2
    fps_match = _re2.search(r'\b(23\.976|24|25|29\.97|30|48|50|59\.94|60)\s*(?:fps|p)\b', filename, _re2.IGNORECASE)
    fps = fps_match.group(1) if fps_match else ""

    # --- Yıl ---
    year = str(parsed.get("year", "")) if parsed.get("year") else ""

    # --- Satır 2: Boyut + Codec + Platform + Encoder ---
    line2 = [f"{size_emoji} {size}"]
    if codec:
        line2.append(f"🎥 {codec}")
    if site_display:
        line2.append(f"🎬 {site_display}")
    if encoder and "dual" not in encoder.lower():
        line2.append(f"👤 {encoder}")

    # --- Satır 3: Bit depth + HDR + Audio + FPS + Proper/Repack ---
    line3 = []
    if bit_depth:
        line3.append(f"🔟 {bit_depth}bit")
    if hdr_badge:
        line3.append(f"✨ {hdr_badge}")
    if audio:
        line3.append(f"🔊 {audio}")
    if fps:
        line3.append(f"🎞️ {fps}fps")
    if proper:
        line3.append("🔄 PROPER")
    elif repack:
        line3.append("🔄 REPACK")

    # --- Satır 4: Bayraklar + Yıl + Extended + Container ---
    line4 = []
    if lang_flags:
        line4.append(f"🌐 {lang_flags}")
    if certification:
        line4.append(f"🏅 {certification}")
    if sub_flags:
        line4.append(f"💬 {sub_flags}")
    if year:
        line4.append(f"📅 {year}")
    if extended:
        line4.append("✂️ Extended")
    if container:
        line4.append(f"📦 {container.upper()}")

    lines = [f"📁 {filename}"]
    if line2:
        lines.append("  ".join(line2))
    if line3:
        lines.append("  ".join(line3))
    if line4:
        lines.append("  ".join(line4))

    stream_title = "\n".join(lines)
    return (stream_name, stream_title)


def get_resolution_priority(stream_name: str) -> int:
    resolution_map = {
        "2160p": 2160, "4k": 2160, "uhd": 2160,
        "1080p": 1080, "fhd": 1080,
        "720p": 720, "hd": 720,
        "480p": 480, "sd": 480,
        "360p": 360,
    }
    for res_key, res_value in resolution_map.items():
        if res_key in stream_name.lower():
            return res_value
    return 1


def parse_size_to_bytes(size_str: str) -> float:
    """
    "2.5 GB", "800 MB", "1.2 TB" gibi boyut string'lerini byte cinsine çevirir.
    Bilinmeyen / boş değerler için 0 döner.
    """
    import re as _re_size
    if not size_str:
        return 0.0
    m = _re_size.search(r'([\d.,]+)\s*(TB|GB|MB|KB|B)', size_str.strip(), _re_size.IGNORECASE)
    if not m:
        return 0.0
    try:
        value = float(m.group(1).replace(",", "."))
    except ValueError:
        return 0.0
    unit = m.group(2).upper()
    multipliers = {"TB": 1024**4, "GB": 1024**3, "MB": 1024**2, "KB": 1024, "B": 1}
    return value * multipliers.get(unit, 0)

# --- Routes ---
@router.get("/{token}/manifest.json")
@router.get("/{token}/{lang}/manifest.json")
async def get_manifest(token: str, lang: str = "en", token_data: dict = Depends(verify_token)):
    lang = resolve_lang(lang)
    lbl = LANG_LABELS[lang]

    if Telegram.HIDE_CATALOG:
        resources = [
            "stream",
            {
                "name": "subtitles",
                "types": ["movie", "series"],
                "idPrefixes": ["tt"]
            }
        ]
        catalogs = []
    else:
        resources = [
            "catalog",
            "meta",
            "stream",
            {
                "name": "subtitles",
                "types": ["movie", "series"],
                "idPrefixes": ["tt"]
            }
        ]
        # --- Tüm olası katalogları oluştur ---
        all_catalogs = [
            # ── Öneri kataloğu (izleme geçmişine dayalı — film + dizi karışık) ──
            {
                "type": "movie",
                "id": f"similar_{lang}",
                "name": lbl["similar"],
                "extra": [{"name": "skip"}],
                "extraSupported": ["skip"],
            },
            # ── TMDB: Trendler (film+dizi, global+TR birleşik) ──────────────
            {
                "type": "movie",
                "id": f"tmdb_trending_{lang}",
                "name": lbl["tmdb_trending"],
                "extra": [{"name": "skip"}],
                "extraSupported": ["skip"],
            },
            # 1. Yeni eklenen filmler  (seri filmler dahil)
            {
                "type": "movie",
                "id": f"latest_movies_{lang}",
                "name": lbl["new"],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": get_genres_for_lang(lang)},
                    {"name": "skip"}
                ],
                "extraSupported": ["genre", "skip"]
            },
            # 2. Popüler filmler  (seri filmler dahil)
            {
                "type": "movie",
                "id": f"top_movies_{lang}",
                "name": lbl["popular"],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": get_genres_for_lang(lang)},
                    {"name": "skip"},
                    {"name": "search", "isRequired": False}
                ],
                "extraSupported": ["genre", "skip", "search"]
            },
            # 3. Seri filmler
            {
                "type": "movie",
                "id": f"collcat_{lang}",
                "name": lbl["collections"],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": get_genres_for_lang(lang)},
                    {"name": "skip"},
                ],
                "extraSupported": ["genre", "skip"],
            },
            # 4. Yeni eklenen diziler
            {
                "type": "series",
                "id": f"latest_series_{lang}",
                "name": lbl["new"],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": get_genres_for_lang(lang)},
                    {"name": "skip"}
                ],
                "extraSupported": ["genre", "skip"]
            },
            # 5. Popüler diziler
            {
                "type": "series",
                "id": f"top_series_{lang}",
                "name": lbl["popular"],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": get_genres_for_lang(lang)},
                    {"name": "skip"},
                    {"name": "search", "isRequired": False}
                ],
                "extraSupported": ["genre", "skip", "search"]
            },
            # 6-15. Platform katalogları (Netflix → TV+ sırası)
            *[
                {
                    "type": "series",
                    "id": f"platform_{platform_key}_{lang}",
                    "name": PLATFORM_LABELS[platform_key],
                    "extra": [
                        {"name": "genre", "isRequired": False, "options": get_genres_for_lang(lang)},
                        {"name": "skip"},
                    ],
                    "extraSupported": ["genre", "skip"],
                }
                for platform_key in [
                    "netflix", "disney", "amazon", "hbo",
                    "bein", "exxen", "gain", "apple", "tabii", "tvplus",
                ]
            ],
            # Son. Yıl kataloğu (film + dizi, genre alanında yıl filtresi)
            {
                "type": "movie",
                "id": f"yearcatalog_movie_{lang}",
                "name": LANG_LABELS_YEAR[lang],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": YEAR_OPTIONS},
                    {"name": "skip"},
                ],
                "extraSupported": ["genre", "skip"],
            },
            {
                "type": "series",
                "id": f"yearcatalog_series_{lang}",
                "name": LANG_LABELS_YEAR[lang],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": YEAR_OPTIONS},
                    {"name": "skip"},
                ],
                "extraSupported": ["genre", "skip"],
            },
            # Yerli Filmler (original_language = "tr")
            {
                "type": "movie",
                "id": f"yerli_movies_{lang}",
                "name": lbl["yerli_movies"],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": get_genres_for_lang(lang)},
                    {"name": "skip"},
                ],
                "extraSupported": ["genre", "skip"],
            },
            # Yerli Diziler (original_language = "tr")
            {
                "type": "series",
                "id": f"yerli_series_{lang}",
                "name": lbl["yerli_series"],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": get_genres_for_lang(lang)},
                    {"name": "skip"},
                ],
                "extraSupported": ["genre", "skip"],
            },
            # Canlı Yayın kataloğu
            {
                "type": "channel",
                "id": f"live_{lang}",
                "name": lbl["live"],
                "extra": [
                    {"name": "genre", "isRequired": False, "options": get_live_genres_for_lang(lang)},
                    {"name": "skip"},
                ],
                "extraSupported": ["genre", "skip"],
            },
        ]

        from Backend import db as _db_cat

        # --- Admin: globalde kapatılmış hazır katalogları çıkar ---
        _global_settings = await _db_cat.get_catalog_global_settings()
        _globally_disabled = set(_global_settings.get("disabled", []))

        def _builtin_base_id(cat_id: str, cat_lang: str) -> str:
            suffix = f"_{cat_lang}"
            return cat_id[:-len(suffix)] if cat_id.endswith(suffix) else cat_id

        all_catalogs = [
            c for c in all_catalogs
            if _builtin_base_id(c["id"], lang) not in _globally_disabled
        ]

        # --- Admin: aktif özel katalogları ekle ---
        custom_catalogs = await _db_cat.get_custom_catalogs(active_only=True)
        for cc in custom_catalogs:
            cc_media_type = cc.get("media_type", "mixed")
            catalog_type = "series" if cc_media_type == "series" else "movie"
            cc_name = cc.get(f"name_{lang}") or cc.get("name", "Katalog")
            all_catalogs.append({
                "type": catalog_type,
                "id": f"custom_{cc['_id']}_{lang}",
                "name": cc_name,
                "extra": [{"name": "skip"}],
                "extraSupported": ["skip"],
            })

        # --- Kullanıcının gizlediği ve sıraladığı katalogları uygula ---
        cat_doc = await _db_cat.get_catalog_prefs_full(token)
        hidden = cat_doc.get("hidden_catalogs", []) if isinstance(cat_doc, dict) else (cat_doc or [])
        catalog_order = cat_doc.get("catalog_order", []) if isinstance(cat_doc, dict) else []

        def _base_id(cat_id: str) -> str:
            for sfx in ("_tr", "_de", "_en", "_original"):
                if cat_id.endswith(sfx):
                    return cat_id[:-len(sfx)]
            return cat_id

        # Gizli katalogları çıkar
        filtered = [c for c in all_catalogs if _base_id(c["id"]) not in hidden]

        # Kullanıcının özel sırasını uygula (varsa)
        if catalog_order:
            order_map = {base_id: idx for idx, base_id in enumerate(catalog_order)}
            default_start = len(catalog_order)
            filtered.sort(key=lambda c: order_map.get(_base_id(c["id"]), default_start + all_catalogs.index(c)))

        catalogs = filtered

    # Build dynamic name/description/version with subscription info
    addon_name = ADDON_NAME
    addon_desc = Telegram.EKLENTI_ACIKLAMASI
    addon_version = ADDON_VERSION
    expiry_obj = None

    # Dile göre abonelik metinleri
    _SUB_I18N = {
        "tr": {
            "validity":    "Geçerlilik Süresi",
            "active":      "Aktif",
            "expires_on":  "Aboneliğiniz {date} tarihinde sona erecektir.",
            "unlimited":   "Aboneliğiniz süresizdir. Film ve dizi izleyebilirsiniz.",
            "active_ok":   "✅ Aboneliğiniz aktif.\nFilm ve dizi izleyebilirsiniz.",
            "date_locale": "tr_TR",
        },
        "de": {
            "validity":    "Gültigkeitsdauer",
            "active":      "Aktiv",
            "expires_on":  "Ihr Abonnement läuft am {date} ab.",
            "unlimited":   "Ihr Abonnement ist unbegrenzt gültig.",
            "active_ok":   "✅ Ihr Abonnement ist aktiv.",
            "date_locale": "de_DE",
        },
        "en": {
            "validity":    "Valid Until",
            "active":      "Active",
            "expires_on":  "Your subscription expires on {date}.",
            "unlimited":   "Your subscription is unlimited.",
            "active_ok":   "✅ Your subscription is active.",
            "date_locale": "en_GB",
        },
    }

    def _format_expiry_date(dt, lang: str) -> str:
        """Tarihi dile göre formatlar: TR → 5 Ocak 2026, DE → 5. Januar 2026, EN → 5 January 2026"""
        _MONTHS_TR = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
                      "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
        _MONTHS_DE = ["Januar","Februar","März","April","Mai","Juni",
                      "Juli","August","September","Oktober","November","Dezember"]
        if lang == "tr":
            return f"{dt.day} {_MONTHS_TR[dt.month - 1]} {dt.year}"
        elif lang == "de":
            return f"{dt.day}. {_MONTHS_DE[dt.month - 1]} {dt.year}"
        else:
            return dt.strftime("%-d %B %Y")   # "5 January 2026"

    if Telegram.SUBSCRIPTION:
        user_id = token_data.get("user_id")
        if user_id:
            from Backend import db as _db
            try:
                user = await _db.get_user(int(user_id))
                if user and user.get("subscription_status") == "active":
                    i18n = _SUB_I18N.get(lang, _SUB_I18N["en"])
                    expiry_obj = user.get("subscription_expiry")
                    plan_label = user.get("plan_label", "")  # isteğe bağlı plan adı

                    name_suffix = f" — {plan_label}" if plan_label else f" — {i18n['active']}"

                    if expiry_obj:
                        date_str = _format_expiry_date(expiry_obj, lang)
                        addon_name = f"{ADDON_NAME}{name_suffix}: {date_str}"
                        addon_desc = f"📅 {i18n['expires_on'].format(date=date_str)}"
                        epoch_tag = format(int(expiry_obj.timestamp()) & 0xFFFF, "x")
                        addon_version = f"{ADDON_VERSION}-{epoch_tag}"
                    else:
                        # Sınırsız abonelik (expiry_obj yok)
                        addon_name = f"{ADDON_NAME}{name_suffix}"
                        addon_desc = f"♾️ {i18n['unlimited']}"
            except Exception:
                pass  # Fallback to defaults on error

    # Configure URL — opening this reinstalls the addon with latest manifest
    configure_url = f"{Telegram.BASE_URL}/stremio/{token}/configure"

    manifest_url = f"{Telegram.BASE_URL}/stremio/{token}/{lang}/manifest.json"

    return {
        "id": f"telegram.media.{token[:8]}.{lang}",   # dil bazlı bağımsız ID
        "version": addon_version,
        "name": addon_name,
        "logo": Telegram.EKLENTI_LOGOSU or None,
        "description": addon_desc,
        "types": ["movie", "series", "channel"],
        "resources": resources,
        "catalogs": catalogs,
        "idPrefixes": ["tt", "manual-", "live_", "yayin_"],
        "behaviorHints": {
            "configurable": True,
            "configurationRequired": False
        },
        "config": [
            {
                "key": "manifest_url",
                "title": "Your Addon URL (copy to reinstall)",
                "type": "text",
                "default": manifest_url
            }
        ]
    }


@router.get("/{token}/configure")
@router.get("/{token}/{lang}/configure")
async def configure_addon(token: str, lang: str = "en"):
    """
    Configure/update page — dil seçimli (TR / DE / EN).
    """
    from fastapi.responses import HTMLResponse
    from Backend import db as _db

    lang = resolve_lang(lang)
    base = Telegram.BASE_URL

    # --- i18n çevirileri ---
    i18n = {
        "tr": {
            "html_lang": "tr",
            "page_title": "Telegram Stremio Eklentisi",
            "heading": "Telegram Stremio Eklentisi",
            "subtitle": "Dil seçin, ardından Stremio'ya yükleyin.",
            "label_user": "Kullanıcı",
            "label_status": "Durum",
            "label_expiry": "Bitiş Tarihi",
            "status_active": "✅ Aktif",
            "status_expired": "🔴 Süresi dolmuş",
            "status_unknown": "❓ Bilinmiyor",
            "lang_section_label": "🌐 Dil Seçin",
            "btn_install": "⚡ Stremio'da Yükle / Güncelle",
            "btn_copy": "📋 Bağlantıyı Kopyala",
            "btn_copied": "✅ Kopyalandı!",
            "manual_title": "Manuel kurulum:",
            "manual_steps": [
                "Stremio'yu açın → Eklentiler (Add-ons) tıklayın.",
                "Add addon kısmına tıklayın.",
                "Yukarıdaki bağlantıyı yapıştırın, Enter'a basın.",
            ],
            "catalog_section": "📂 Katalog Görünürlüğü",
            "catalog_subtitle": "Katalogları sürükleyerek sıralayın, görmek istemediklerinizi kapatın.",
            "cat_type_movie": "Film",
            "cat_type_series": "Dizi",
            "cat_type_channel": "Canlı",
            "btn_save_catalogs": "💾 Tercihleri Kaydet",
            "btn_saving": "Kaydediliyor...",
            "save_ok": "✅ Kaydedildi! Eklentiyi güncellemek için silip tekrar yükleyin.",
            "save_err": "❌ Kaydetme başarısız.",
            "channel_section": "📡 Canlı Kanal Sırası",
            "channel_subtitle": "Kanalları sürükleyerek kendi sıranıza göre düzenleyin.",
            "btn_save_channels": "💾 Kanal Sırasını Kaydet",
            "channel_save_ok": "✅ Kanal sırası kaydedildi!",
            "channel_save_err": "❌ Kanal sırası kaydedilemedi.",
            "label_daily_used": "Günlük Kullanılan",
            "label_monthly_used": "Aylık Kullanılan",
            "label_daily_limit": "Günlük Limit",
            "label_monthly_limit": "Aylık Limit",
            "unlimited": "Sınırsız",
        },
        "de": {
            "html_lang": "de",
            "page_title": "Telegram Stremio-Erweiterung",
            "heading": "Telegram Stremio-Erweiterung",
            "subtitle": "Sprache wählen und dann in Stremio installieren.",
            "label_user": "Benutzer",
            "label_status": "Status",
            "label_expiry": "Ablaufdatum",
            "status_active": "✅ Aktiv",
            "status_expired": "🔴 Abgelaufen",
            "status_unknown": "❓ Unbekannt",
            "lang_section_label": "🌐 Sprache wählen",
            "btn_install": "⚡ In Stremio installieren / aktualisieren",
            "btn_copy": "📋 Link kopieren",
            "btn_copied": "✅ Kopiert!",
            "manual_title": "Manuelle Installation:",
            "manual_steps": [
                "Stremio öffnen → Add-ons anklicken.",
                'Auf "Add-on hinzufügen" klicken.',
                "Den obigen Link einfügen und Enter drücken.",
            ],
            "catalog_section": "📂 Katalog-Sichtbarkeit",
            "catalog_subtitle": "Kataloge per Drag & Drop sortieren, unerwünschte deaktivieren.",
            "cat_type_movie": "Film",
            "cat_type_series": "Serie",
            "cat_type_channel": "Live",
            "btn_save_catalogs": "💾 Einstellungen speichern",
            "btn_saving": "Speichern...",
            "save_ok": "✅ Gespeichert! Entfernen Sie das Add-on und installieren Sie es erneut, um es zu aktualisieren.",
            "save_err": "❌ Speichern fehlgeschlagen.",
            "channel_section": "📡 Reihenfolge der Live-Kanäle",
            "channel_subtitle": "Kanäle per Drag & Drop in Ihrer Wunschreihenfolge sortieren.",
            "btn_save_channels": "💾 Kanalreihenfolge speichern",
            "channel_save_ok": "✅ Kanalreihenfolge gespeichert!",
            "channel_save_err": "❌ Speichern der Kanalreihenfolge fehlgeschlagen.",
            "label_daily_used": "Heute verbraucht",
            "label_monthly_used": "Monat verbraucht",
            "label_daily_limit": "Tageslimit",
            "label_monthly_limit": "Monatslimit",
            "unlimited": "Unbegrenzt",
        },
        "en": {
            "html_lang": "en",
            "page_title": "Telegram Stremio Add-on",
            "heading": "Telegram Stremio Add-on",
            "subtitle": "Select a language, then install in Stremio.",
            "label_user": "User",
            "label_status": "Status",
            "label_expiry": "Expiry Date",
            "status_active": "✅ Active",
            "status_expired": "🔴 Expired",
            "status_unknown": "❓ Unknown",
            "lang_section_label": "🌐 Select Language",
            "btn_install": "⚡ Install / Update in Stremio",
            "btn_copy": "📋 Copy Link",
            "btn_copied": "✅ Copied!",
            "manual_title": "Manual installation:",
            "manual_steps": [
                "Open Stremio → click Add-ons.",
                'Click "Add add-on".',
                "Paste the link above and press Enter.",
            ],
            "catalog_section": "📂 Catalog Visibility",
            "catalog_subtitle": "Drag to reorder catalogs, toggle to show/hide on Stremio home.",
            "cat_type_movie": "Movie",
            "cat_type_series": "Series",
            "cat_type_channel": "Live",
            "btn_save_catalogs": "💾 Save Preferences",
            "btn_saving": "Saving...",
            "save_ok": "✅ Saved! To update the add-on, remove it and install it again.",
            "save_err": "❌ Failed to save.",
            "channel_section": "📡 Live Channel Order",
            "channel_subtitle": "Drag channels to set your personal viewing order.",
            "btn_save_channels": "💾 Save Channel Order",
            "channel_save_ok": "✅ Channel order saved!",
            "channel_save_err": "❌ Failed to save channel order.",
            "label_daily_used": "Daily Used",
            "label_monthly_used": "Monthly Used",
            "label_daily_limit": "Daily Limit",
            "label_monthly_limit": "Monthly Limit",
            "unlimited": "Unlimited",
        },
    }

    t = i18n.get(lang, i18n["en"])

    # Kullanıcı bilgilerini çek
    token_doc = await _db.get_api_token(token)
    user_name = "Unknown"
    expiry_str = "N/A"
    status_color = "#ef4444"
    status_text = t["status_unknown"]

    # Katalog tercihlerini çek
    cat_prefs = await _db.get_catalog_prefs_full(token)
    hidden_catalogs = cat_prefs.get("hidden_catalogs", []) if isinstance(cat_prefs, dict) else (cat_prefs or [])
    saved_order = cat_prefs.get("catalog_order", []) if isinstance(cat_prefs, dict) else []




    # Kanal sırası tercihlerini çek
    saved_channel_order = await _db.get_channel_order(token)
    all_channels = await _db.get_live_channels()
    if saved_channel_order:
        ch_order_map = {cid: idx for idx, cid in enumerate(saved_channel_order)}
        default_ch_start = len(saved_channel_order)
        all_channels.sort(key=lambda ch: ch_order_map.get(ch.get("_id", ""), default_ch_start))
    channel_items_html = ""
    for ch in all_channels:
        ch_id = ch.get("_id", "")
        ch_name = ch.get("name", ch_id)
        ch_logo = ch.get("logo", "") or ch.get("poster", "")
        logo_html = f'<img src="{ch_logo}" style="width:22px;height:22px;border-radius:4px;object-fit:cover;flex-shrink:0;">' if ch_logo else '<span style="width:22px;height:22px;background:#374151;border-radius:4px;flex-shrink:0;display:inline-block;"></span>'
        channel_items_html += f"""
      <div class="cat-item" draggable="true" data-ch-id="{ch_id}">
        <span class="drag-handle">⠿</span>
        {logo_html}
        <span class="cat-name">{ch_name}</span>
      </div>"""

    _MONTHS_TR = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
                  "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
    _MONTHS_DE = ["Januar","Februar","März","April","Mai","Juni",
                  "Juli","August","September","Oktober","November","Dezember"]

    def _fmt_date(dt, lg: str) -> str:
        if lg == "tr":
            return f"{dt.day} {_MONTHS_TR[dt.month - 1]} {dt.year}"
        elif lg == "de":
            return f"{dt.day}. {_MONTHS_DE[dt.month - 1]} {dt.year}"
        return dt.strftime("%-d %B %Y")

    if token_doc:
        uid = token_doc.get("user_id")
        if uid:
            try:
                user = await _db.get_user(int(uid))
                if user:
                    user_name = user.get("first_name") or user.get("username") or f"User {uid}"
                    sub_status = user.get("subscription_status", "")
                    expiry = user.get("subscription_expiry")
                    if expiry:
                        expiry_str = _fmt_date(expiry, lang)
                    elif sub_status == "active":
                        expiry_str = "∞" if lang == "en" else ("Sınırsız" if lang == "tr" else "Unbegrenzt")
                    if sub_status == "active":
                        status_color = "#22c55e"
                        status_text = t["status_active"]
                    else:
                        status_color = "#ef4444"
                        status_text = t["status_expired"]
            except Exception:
                pass

    # Kullanım ve limit bilgilerini hesapla
    def _fmt_gb(val_gb: float, unlimited_label: str) -> str:
        if val_gb <= 0:
            return unlimited_label
        if val_gb >= 1:
            return f"{val_gb:.1f} GB".rstrip("0").rstrip(".")
        return f"{val_gb * 1024:.0f} MB"

    def _fmt_bytes(val_bytes: int) -> str:
        if val_bytes <= 0:
            return "0 MB"
        gb = val_bytes / (1024 ** 3)
        if gb >= 1:
            return f"{gb:.2f} GB"
        return f"{val_bytes / (1024 ** 2):.0f} MB"

    _unlimited = t.get("unlimited", "∞")
    daily_used_str   = "—"
    monthly_used_str = "—"
    daily_limit_str  = "—"
    monthly_limit_str= "—"

    if token_doc:
        from Backend.helper.database import _daily_key as _dk
        today_str = _dk()
        from zoneinfo import ZoneInfo as _ZI
        month_str = __import__("datetime").datetime.now(_ZI("Europe/Istanbul")).strftime("%Y-%m")

        usage   = token_doc.get("usage", {})
        limits  = token_doc.get("limits", {})

        daily_bucket = usage.get("daily", {})
        daily_bytes  = daily_bucket.get("bytes", 0) if daily_bucket.get("date") == today_str else 0

        monthly_bucket = usage.get("monthly", {})
        monthly_bytes  = monthly_bucket.get("bytes", 0) if monthly_bucket.get("month") == month_str else 0

        daily_limit_gb   = float(limits.get("daily_limit_gb",   0) or 0)
        monthly_limit_gb = float(limits.get("monthly_limit_gb", 0) or 0)

        daily_used_str    = _fmt_bytes(daily_bytes)
        monthly_used_str  = _fmt_bytes(monthly_bytes)
        daily_limit_str   = _fmt_gb(daily_limit_gb,   _unlimited)
        monthly_limit_str = _fmt_gb(monthly_limit_gb, _unlimited)

    # Seçili dil kartını belirle
    sel_tr       = ' sel' if lang == 'tr'       else ''
    sel_de       = ' sel' if lang == 'de'       else ''
    sel_original = ' sel' if lang == 'en' else ''

    # Başlangıç manifest URL'si (sayfanın açıldığı dile göre)
    initial_url = f"{base}/stremio/{token}/{lang}/manifest.json"
    base_no_scheme = base.replace('https://','').replace('http://','')

    # Manuel adımları <li> listesine çevir
    steps_html = "\n".join(f"      <li>{s}</li>" for s in t["manual_steps"])

    # --- Katalog tanımları (dil bağımsız base ID + görünen isim) ---
    from Backend.helper.platform_catalog import PLATFORM_LABELS as _PL
    _lbl = LANG_LABELS[lang]
    _CATALOGS_DEF = [
        ("similar",            "🎯 " + ("Sana Özel" if lang=="tr" else ("Empfohlen für Sie" if lang=="de" else "For You")), "movie"),
        ("tmdb_trending",   _lbl['tmdb_trending'],  "movie"),
        ("latest_movies",   f"🎬 {_lbl['new']}",            "movie"),
        ("top_movies",      f"🎬 {_lbl['popular']}",        "movie"),
        ("collcat",         f"🎬 {_lbl['collections']}",    "movie"),
        ("latest_series",   f"📺 {_lbl['new']}",            "series"),
        ("top_series",      f"📺 {_lbl['popular']}",        "series"),
        *[(f"platform_{k}", f"📡 {v}", "series") for k, v in _PL.items()],
        ("yearcatalog_movie",   "📅 " + ("Yıla Göre" if lang=="tr" else ("Nach Jahr" if lang=="de" else "By Year")), "movie"),
        ("yearcatalog_series",  "📅 " + ("Yıla Göre" if lang=="tr" else ("Nach Jahr" if lang=="de" else "By Year")), "series"),
        ("yerli_movies",    _lbl["yerli_movies"],  "movie"),
        ("yerli_series",    _lbl["yerli_series"],  "series"),
        ("live",            f"📡 {_lbl['live']}",            "channel"),
    ]
    _TYPE_LABELS = {
        "movie":   t["cat_type_movie"],
        "series":  t["cat_type_series"],
        "channel": t["cat_type_channel"],
    }

    # Kullanıcının kaydettiği sırayı uygula
    if saved_order:
        order_map = {base_id: idx for idx, base_id in enumerate(saved_order)}
        default_idx = len(saved_order)
        # Orijinal indeksleri sort öncesinde sabitle (in-place sort sırasında .index() patlar)
        _original_idx = {x[0]: i for i, x in enumerate(_CATALOGS_DEF)}
        _CATALOGS_DEF.sort(key=lambda x: order_map.get(x[0], default_idx + _original_idx[x[0]]))

    catalog_items_html = ""
    for base_id, cat_name, cat_type in _CATALOGS_DEF:
        is_hidden = base_id in hidden_catalogs
        checked = "" if is_hidden else "checked"
        type_label = _TYPE_LABELS.get(cat_type, cat_type)
        catalog_items_html += f"""
      <div class="cat-item {'cat-off' if is_hidden else ''}" draggable="true" data-id="{base_id}">
        <span class="drag-handle">⠿</span>
        <input type="checkbox" data-id="{base_id}" {checked} onchange="toggleCatalog(this)">
        <span class="cat-name">{cat_name}</span>
        <span class="cat-type">{type_label}</span>
      </div>"""

    html = f"""<!DOCTYPE html>
<html lang="{t['html_lang']}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{t['page_title']}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      background:#0f0f1a;color:#e2e8f0;min-height:100vh;display:flex;
      align-items:center;justify-content:center;padding:24px}}
    .card{{background:#1e1e2e;border:1px solid #2d2d44;border-radius:16px;
      padding:36px 28px;max-width:500px;width:100%;text-align:center}}
    .logo{{font-size:44px;margin-bottom:10px}}
    h1{{font-size:1.4rem;font-weight:700;color:#f8fafc;margin-bottom:4px}}
    .sub-title{{color:#94a3b8;font-size:.88rem;margin-bottom:22px}}
    .info-row{{display:flex;justify-content:space-between;align-items:center;
      background:#2a2a3e;border-radius:10px;padding:11px 15px;
      margin-bottom:10px;font-size:.88rem}}
    .info-label{{color:#94a3b8}}
    .info-val{{font-weight:600;color:#f1f5f9}}
    .status-badge{{display:inline-block;padding:2px 10px;border-radius:999px;
      font-size:.78rem;font-weight:700;
      background:{status_color}22;color:{status_color}}}
    .lang-section{{margin:18px 0 6px;text-align:left}}
    .lang-label{{font-size:.8rem;color:#64748b;text-transform:uppercase;
      letter-spacing:.06em;margin-bottom:8px}}
    .lang-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:20px}}
    .lang-card{{border:2px solid #2e2e3e;border-radius:10px;padding:14px 10px;
      cursor:pointer;transition:border-color .2s,background .2s;text-align:center}}
    .lang-card:hover{{border-color:#7c3aed}}
    .lang-card.sel{{border-color:#7c3aed;background:rgba(124,58,237,.12)}}
    .lang-card .flag{{font-size:1.8rem;display:block;margin-bottom:4px}}
    .lang-card .lname{{font-weight:600;font-size:.9rem}}
    .lang-card .lsub{{font-size:.75rem;color:#64748b}}
    .url-box{{background:#111827;border:1px solid #374151;border-radius:8px;
      padding:10px 13px;font-family:monospace;font-size:.75rem;
      color:#94a3b8;word-break:break-all;text-align:left;margin-top:4px}}
    .btn-install{{display:block;width:100%;
      background:linear-gradient(135deg,#7c3aed,#4f46e5);
      color:#fff;font-weight:700;font-size:.95rem;
      padding:13px 20px;border-radius:12px;border:none;
      cursor:pointer;text-decoration:none;margin:18px 0 10px;
      transition:opacity .2s}}
    .btn-install:hover{{opacity:.85}}
    .btn-copy{{width:100%;padding:10px;
      background:#1e293b;border:1px solid #374151;color:#94a3b8;
      border-radius:8px;cursor:pointer;font-size:.85rem;transition:all .2s;
      margin-top:8px}}
    .btn-copy:hover{{background:#334155;color:#f1f5f9}}
    .catalog-section{{margin-top:22px;text-align:left}}
    .catalog-label{{font-size:.8rem;color:#64748b;text-transform:uppercase;
      letter-spacing:.06em;margin-bottom:4px}}
    .catalog-subtitle{{font-size:.78rem;color:#475569;margin-bottom:10px}}
    .catalog-list{{display:flex;flex-direction:column;gap:6px;max-height:400px;
      overflow-y:auto;padding-right:4px}}
    .catalog-list::-webkit-scrollbar{{width:4px}}
    .catalog-list::-webkit-scrollbar-track{{background:#1e293b}}
    .catalog-list::-webkit-scrollbar-thumb{{background:#374151;border-radius:2px}}
    .cat-item{{display:flex;align-items:center;gap:10px;background:#2a2a3e;
      border-radius:8px;padding:9px 13px;cursor:default;transition:opacity .2s,background .15s;
      border:1px solid transparent;user-select:none}}
    .cat-item.dragging{{opacity:.4;border-color:#7c3aed}}
    .cat-item.drag-over{{border-color:#7c3aed;background:#32325a}}
    .drag-handle{{font-size:1.1rem;color:#4a5568;cursor:grab;flex-shrink:0;line-height:1}}
    .drag-handle:active{{cursor:grabbing}}
    .cat-item input{{width:16px;height:16px;accent-color:#7c3aed;cursor:pointer;flex-shrink:0}}
    .cat-name{{flex:1;font-size:.85rem;color:#e2e8f0}}
    .cat-type{{font-size:.72rem;color:#64748b;background:#1e293b;padding:2px 7px;
      border-radius:999px;white-space:nowrap}}
    .cat-off .cat-name{{opacity:.4;text-decoration:line-through}}
    .btn-save{{display:block;width:100%;margin-top:12px;
      background:linear-gradient(135deg,#059669,#0d9488);
      color:#fff;font-weight:700;font-size:.9rem;padding:11px 20px;
      border-radius:10px;border:none;cursor:pointer;transition:opacity .2s}}
    .btn-save:hover{{opacity:.85}}
    .save-msg{{margin-top:8px;font-size:.82rem;text-align:center;min-height:18px;color:#94a3b8}}
    .steps{{background:#2a2a3e;border-radius:10px;padding:13px 16px;
      margin:14px 0;text-align:left;font-size:.83rem;color:#cbd5e1}}
    .steps b{{color:#f1f5f9}}
    .steps ol{{margin:7px 0 0 16px;line-height:1.8}}
  </style>
</head>
<body>
<div class="card">
  <div class="logo">🎬</div>
  <h1>{t['heading']}</h1>
  <p class="sub-title">{t['subtitle']}</p>

  <div class="info-row">
    <span class="info-label">{t['label_user']}</span>
    <span class="info-val">{user_name}</span>
  </div>
  <div class="info-row">
    <span class="info-label">{t['label_status']}</span>
    <span class="status-badge">{status_text}</span>
  </div>
  <div class="info-row">
    <span class="info-label">{t['label_expiry']}</span>
    <span class="info-val">{expiry_str}</span>
  </div>
  <div class="info-row">
    <span class="info-label">{t['label_daily_used']}</span>
    <span class="info-val">{daily_used_str}</span>
  </div>
  <div class="info-row">
    <span class="info-label">{t['label_daily_limit']}</span>
    <span class="info-val">{daily_limit_str}</span>
  </div>
  <div class="info-row">
    <span class="info-label">{t['label_monthly_used']}</span>
    <span class="info-val">{monthly_used_str}</span>
  </div>
  <div class="info-row">
    <span class="info-label">{t['label_monthly_limit']}</span>
    <span class="info-val">{monthly_limit_str}</span>
  </div>

  <div class="lang-section">
    <div class="lang-label">{t['lang_section_label']}</div>
    <div class="lang-grid">
      <div class="lang-card{sel_tr}" onclick="selectLang('tr',this)">
        <span class="flag">🇹🇷</span>
        <div class="lname">Türkçe</div>
        <div class="lsub">Turkish</div>
      </div>
      <div class="lang-card{sel_de}" onclick="selectLang('de',this)">
        <span class="flag">🇩🇪</span>
        <div class="lname">Deutsch</div>
        <div class="lsub">German</div>
      </div>
      <div class="lang-card{sel_original}" onclick="selectLang('en',this)">
        <span class="flag">🇬🇧</span>
        <div class="lname">English</div>
        <div class="lsub">EN</div>
      </div>
    </div>
  </div>

  <div class="url-box" id="addonUrl">{initial_url}</div>

  <button onclick="copyUrl()" class="btn-copy" id="copyBtn">{t['btn_copy']}</button>

  <div class="steps">
    <b>{t['manual_title']}</b>
    <ol>
{steps_html}
    </ol>
  </div>

  <!-- Katalog Görünürlüğü -->
  <div class="catalog-section">
    <div class="catalog-label">{t['catalog_section']}</div>
    <div class="catalog-subtitle">{t['catalog_subtitle']}</div>
    <div class="catalog-list">
{catalog_items_html}
    </div>
    <button class="btn-save" id="saveBtn" onclick="saveCatalogs()">{t['btn_save_catalogs']}</button>
    <div class="save-msg" id="saveMsg"></div>
  </div>

  <!-- Canlı Kanal Sırası -->
  <div class="catalog-section" style="margin-top:22px">
    <div class="catalog-label">{t['channel_section']}</div>
    <div class="catalog-subtitle">{t['channel_subtitle']}</div>
    <div class="catalog-list" id="channelList">
{channel_items_html}
    </div>
    <button class="btn-save" id="saveChBtn" onclick="saveChannels()" style="background:linear-gradient(135deg,#7c3aed,#4f46e5)">{t['btn_save_channels']}</button>
    <div class="save-msg" id="saveChMsg"></div>
  </div>

</div>

<script>
  const BASE = "{base}";
  const TOKEN = "{token}";
  const BTN_COPY_LABEL = "{t['btn_copy']}";
  const BTN_COPIED_LABEL = "{t['btn_copied']}";
  const BTN_SAVING = "{t['btn_saving']}";
  const BTN_SAVE_LABEL = "{t['btn_save_catalogs']}";
  const SAVE_OK = "{t['save_ok']}";
  const SAVE_ERR = "{t['save_err']}";
  const BTN_SAVE_CH_LABEL = "{t['btn_save_channels']}";
  const SAVE_CH_OK = "{t['channel_save_ok']}";
  const SAVE_CH_ERR = "{t['channel_save_err']}";
  let currentLang = "{lang}";

  function selectLang(lang, el) {{
    currentLang = lang;
    document.querySelectorAll('.lang-card').forEach(c => c.classList.remove('sel'));
    el.classList.add('sel');
    updateUrl();
  }}

  function updateUrl() {{
    const url = BASE + "/stremio/" + TOKEN + "/" + currentLang + "/manifest.json";
    document.getElementById('addonUrl').textContent = url;
  }}

  function copyUrl() {{
    const url = document.getElementById('addonUrl').textContent;
    navigator.clipboard.writeText(url).then(() => {{
      const b = document.getElementById('copyBtn');
      b.textContent = BTN_COPIED_LABEL;
      setTimeout(() => b.textContent = BTN_COPY_LABEL, 2000);
    }});
  }}

  function toggleCatalog(cb) {{
    const item = cb.closest('.cat-item');
    if (cb.checked) item.classList.remove('cat-off');
    else item.classList.add('cat-off');
  }}

  // ── Drag-and-Drop sıralama (katalog ve kanal listeleri) ───────────────────
  let dragSrc = null;

  function _initDragList(list) {{
    list.addEventListener('dragstart', e => {{
      const item = e.target.closest('.cat-item[draggable]');
      if (!item) return;
      dragSrc = item;
      setTimeout(() => item.classList.add('dragging'), 0);
    }});
    list.addEventListener('dragend', e => {{
      const item = e.target.closest('.cat-item');
      if (!item) return;
      item.classList.remove('dragging');
      list.querySelectorAll('.cat-item').forEach(i => i.classList.remove('drag-over'));
      dragSrc = null;
    }});
    list.addEventListener('dragover', e => {{
      e.preventDefault();
      const target = e.target.closest('.cat-item[draggable]');
      if (!target || target === dragSrc) return;
      list.querySelectorAll('.cat-item').forEach(i => i.classList.remove('drag-over'));
      target.classList.add('drag-over');
    }});
    list.addEventListener('drop', e => {{
      e.preventDefault();
      const target = e.target.closest('.cat-item[draggable]');
      if (!target || target === dragSrc || !dragSrc) return;
      target.classList.remove('drag-over');
      const items = [...list.querySelectorAll('.cat-item')];
      const srcIdx = items.indexOf(dragSrc);
      const tgtIdx = items.indexOf(target);
      if (srcIdx < tgtIdx) {{
        list.insertBefore(dragSrc, target.nextSibling);
      }} else {{
        list.insertBefore(dragSrc, target);
      }}
    }});
  }}

  document.addEventListener('DOMContentLoaded', () => {{
    document.querySelectorAll('.catalog-list').forEach(list => _initDragList(list));
  }});

  async function saveCatalogs() {{
    const btn = document.getElementById('saveBtn');
    const msg = document.getElementById('saveMsg');
    btn.disabled = true;
    btn.textContent = BTN_SAVING;
    msg.textContent = '';

    const hidden = [];
    const order = [];
    document.querySelectorAll('.catalog-list:not(#channelList) .cat-item[draggable]').forEach(item => {{
      const id = item.dataset.id;
      order.push(id);
      const cb = item.querySelector('input[type=checkbox]');
      if (cb && !cb.checked) hidden.push(id);
    }});

    try {{
      const res = await fetch(BASE + '/stremio/' + TOKEN + '/catalog-prefs', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{hidden_catalogs: hidden, catalog_order: order}})
      }});
      msg.style.color = res.ok ? '#22c55e' : '#ef4444';
      msg.textContent = res.ok ? SAVE_OK : SAVE_ERR;
    }} catch(e) {{
      msg.style.color = '#ef4444';
      msg.textContent = SAVE_ERR;
    }}
    btn.disabled = false;
    btn.textContent = BTN_SAVE_LABEL;
  }}

  async function saveChannels() {{
    const btn = document.getElementById('saveChBtn');
    const msg = document.getElementById('saveChMsg');
    btn.disabled = true;
    btn.textContent = BTN_SAVING;
    msg.textContent = '';

    const channel_order = [];
    document.querySelectorAll('#channelList .cat-item[draggable]').forEach(item => {{
      channel_order.push(item.dataset.chId);
    }});

    try {{
      const res = await fetch(BASE + '/stremio/' + TOKEN + '/channel-order', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{channel_order}})
      }});
      msg.style.color = res.ok ? '#22c55e' : '#ef4444';
      msg.textContent = res.ok ? SAVE_CH_OK : SAVE_CH_ERR;
    }} catch(e) {{
      msg.style.color = '#ef4444';
      msg.textContent = SAVE_CH_ERR;
    }}
    btn.disabled = false;
    btn.textContent = BTN_SAVE_CH_LABEL;
  }}
</script>
</body>
</html>"""
    return HTMLResponse(html)



@router.get("/{token}/catalog/{media_type}/{id}/{extra:path}.json")
@router.get("/{token}/catalog/{media_type}/{id}.json")
@router.get("/{token}/{lang}/catalog/{media_type}/{id}/{extra:path}.json")
@router.get("/{token}/{lang}/catalog/{media_type}/{id}.json")
async def get_catalog(token: str, media_type: str, id: str, extra: Optional[str] = None, lang: str = "tr", token_data: dict = Depends(verify_token)):
    lang = resolve_lang(lang)
    if Telegram.HIDE_CATALOG:
        raise HTTPException(status_code=404, detail="Catalog disabled")

    # Kullanıcının gizlediği katalogları çek
    from Backend import db as _db_cat_pref
    hidden_cats = await _db_cat_pref.get_catalog_prefs(token)

    # Admin panelinden globalde kapatılmış hazır katalogları çek
    _global_settings = await _db_cat_pref.get_catalog_global_settings()
    globally_disabled = set(_global_settings.get("disabled", []))

    if media_type not in ["movie", "series", "channel"]:
        raise HTTPException(status_code=404, detail="Invalid catalog type")

    genre_filter = None
    search_query = None
    stremio_skip = 0

    if extra:
        params = extra.replace("&", "/").split("/")
        for param in params:
            if param.startswith("genre="):
                genre_filter = unquote(param.removeprefix("genre="))
            elif param.startswith("search="):
                # unquote_plus: bazı istemciler boşluğu "+" olarak kodluyor
                # ("Tom+Hanks"); düz unquote bunu boşluğa çevirmediğinden
                # çok kelimeli oyuncu adı aramaları tek kelime gibi
                # algılanıp cast alanında hiç eşleşmiyordu.
                search_query = unquote_plus(param.removeprefix("search="))
            elif param.startswith("skip="):
                try:
                    stremio_skip = int(param.removeprefix("skip="))
                except ValueError:
                    stremio_skip = 0

    page = (stremio_skip // PAGE_SIZE) + 1

    try:
        if search_query:
            search_results = await db.search_documents(query=search_query, page=page, page_size=PAGE_SIZE)
            all_items = search_results.get("results", [])
            db_media_type = "tv" if media_type == "series" else "movie"
            items = [item for item in all_items if item.get("media_type") == db_media_type and _has_video_stream(item)]
        else:
            # ── Canlı Yayın kataloğu ──────────────────────────────────────
            if id.startswith("live_") and media_type == "channel":
                from Backend import db as _db
                channels = await _db.get_live_channels(scheduled_only=True)

                # Üyenin kayıtlı kanal sırasını uygula
                channel_order = await _db.get_channel_order(token)
                if channel_order:
                    order_map = {cid: idx for idx, cid in enumerate(channel_order)}
                    default_start = len(channel_order)
                    channels.sort(key=lambda ch: order_map.get(ch.get("_id", ""), default_start))

                # ── Aktif Yayınları kanallarla birleştir ve order değerine göre sırala ──
                active_broadcasts = await _db.get_active_broadcasts()

                combined = []
                for ch in channels:
                    combined.append({"_order": ch.get("order", 0), "_kind": "channel", "_data": ch})
                for bc in active_broadcasts:
                    combined.append({"_order": bc.get("order", 0), "_kind": "broadcast", "_data": bc})

                # order değerine göre sırala; eşitlerde kanallar önce
                combined.sort(key=lambda x: (x["_order"], 0 if x["_kind"] == "channel" else 1))

                # ── Tür filtresi ──────────────────────────────────────────
                if genre_filter:
                    def _item_has_genre(item: dict, gf: str) -> bool:
                        genres = item["_data"].get("genres") or []
                        return gf in genres
                    combined = [item for item in combined if _item_has_genre(item, genre_filter)]

                # Sayfalama birleşik listeye uygulanır
                combined_page = combined[stremio_skip: stremio_skip + PAGE_SIZE]

                metas = []
                for item in combined_page:
                    if item["_kind"] == "channel":
                        ch = item["_data"]
                        ch_id = ch.get("_id", "")
                        metas.append({
                            "id": f"live_{ch_id}",
                            "type": "channel",
                            "name": ch.get("name", ""),
                            "poster": ch.get("poster", "") or ch.get("logo", ""),
                            "logo": ch.get("logo", ""),
                            "background": ch.get("backdrop", ""),
                            "description": ch.get("description", ""),
                            "genres": ch.get("genres", []),
                            "posterShape": "square",
                        })
                    else:
                        bc = item["_data"]
                        bid = bc["_id"]
                        metas.append({
                            "id":          f"yayin_{bid}",
                            "type":        "channel",
                            "name":        bc.get("name", "Yayın"),
                            "poster":      bc.get("poster") or bc.get("logo") or "",
                            "background":  bc.get("poster") or "",
                            "logo":        bc.get("logo") or "",
                            "description": bc.get("description") or "",
                            "genres":      bc.get("genres") or [],
                            "posterShape": "square",
                        })

                return {"metas": metas}

            # ── Yıl kataloğu ─────────────────────────────────────────────
            elif id.startswith("yearcatalog_"):
                if not platform_catalog.is_loaded():
                    return {"metas": []}

                cat_media_type = "movie" if media_type == "movie" else "tv"
                all_items = platform_catalog.get_year_catalog(media_type=cat_media_type)

                # genre_filter aslında burada yıl filtresidir
                if genre_filter:
                    try:
                        year_int = int(genre_filter)
                        all_items = [i for i in all_items if int(i.get("release_year") or 0) == year_int]
                    except ValueError:
                        pass

                # Yıla göre azalan sırala
                all_items.sort(key=lambda m: int(m.get("release_year") or 0), reverse=True)
                # Sadece gerçek video stream'i olan içerikleri göster (arşiv-only içerikler gizle)
                all_items = [item for item in all_items if _has_video_stream(item)]
                all_items = all_items[stremio_skip: stremio_skip + PAGE_SIZE]
                metas = [convert_to_stremio_meta(item, lang) for item in all_items]
                return {"metas": metas}

            # ── Platform katalog isteği (diziler) ────────────────────────
            elif id.startswith("platform_"):
                parts_id = id.split("_")
                platform_key = parts_id[1] if len(parts_id) > 1 else ""

                if platform_key not in PLATFORM_LABELS:
                    return {"metas": []}
                if f"platform_{platform_key}" in globally_disabled:
                    return {"metas": []}
                if not platform_catalog.is_loaded():
                    return {"metas": []}

                all_items = platform_catalog.get(platform_key)
                items = [i for i in all_items if i.get("media_type") == "tv"]

                # Genre filtresi
                if genre_filter:
                    if lang == "de":
                        items = [i for i in items if genre_filter in (i.get("genres_de") or [])]
                    elif lang == "en":
                        items = [i for i in items if genre_filter in (i.get("genres") or [])]
                    else:
                        items = [i for i in items if genre_filter in (i.get("genres_tr") or [])]

                items = [item for item in items if _has_video_stream(item)]
                items = items[stremio_skip: stremio_skip + PAGE_SIZE]
                metas = [convert_to_stremio_meta(item, lang) for item in items]
                return {"metas": metas}


            # ── Yerli filmler / diziler (original_language = "tr") ───────
            elif id.startswith("yerli_movies_") or id.startswith("yerli_series_"):
                is_movie = id.startswith("yerli_movies_")
                col_type = "movie" if is_movie else "tv"
                sort_params = [("updated_on", "desc")]
                # original_language "tr" olanları göster;
                # alan hiç set edilmemiş (None/eksik) eski içerikler dahil edilmez —
                # sadece açıkça "tr" olarak işaretlenmiş içerikler katalogda çıkar.
                extra_filter = {
                    "$or": [
                        {"original_language": "tr"},
                        {"original_language": {"$regex": "^[Tt]ur", "$options": "i"}},
                    ]
                }

                if genre_filter:
                    gf = "genres_de" if lang == "de" else ("genres" if lang == "original" else "genres_tr")
                    extra_filter[gf] = {"$in": [genre_filter]}

                if is_movie:
                    data = await db.sort_movies(sort_params, page, PAGE_SIZE,
                                                lang=lang, extra_filter=extra_filter)
                    items = data.get("movies", [])
                else:
                    data = await db.sort_tv_shows(sort_params, page, PAGE_SIZE,
                                                  lang=lang, extra_filter=extra_filter)
                    items = data.get("tv_shows", [])

                items = [item for item in items if _has_video_stream(item)]
                metas = [convert_to_stremio_meta(item, lang) for item in items]
                return {"metas": metas}

            # ── Seri filmleri kataloğu ────────────────────────────────────
            elif id.startswith("collcat_"):
                if not platform_catalog.is_loaded():
                    return {"metas": []}

                all_items = platform_catalog.get_collection_movies()

                # Genre filtresi
                if genre_filter:
                    if lang == "de":
                        all_items = [i for i in all_items if genre_filter in (i.get("genres_de") or [])]
                    elif lang == "en":
                        all_items = [i for i in all_items if genre_filter in (i.get("genres") or [])]
                    else:
                        all_items = [i for i in all_items if genre_filter in (i.get("genres_tr") or [])]

                all_items = all_items[stremio_skip: stremio_skip + PAGE_SIZE]
                metas = [convert_to_stremio_meta(item, lang) for item in all_items]
                return {"metas": metas}

            # ── "Sana Özel" kataloğu (izleme geçmişine dayalı öneri) ───────────
            elif id.startswith("similar_"):
                if "similar" in globally_disabled:
                    return {"metas": []}

                from Backend import db as _db_similar

                # Cache'te geçerli veri varsa DB'ye gitmeden dön
                all_similar = _similar_cache_get(token, lang)

                if all_similar is None:
                    # Cache boş veya süresi dolmuş — DB'den hesapla
                    history_rich = await _db_similar.get_watch_history_rich(token, limit=40)

                    if not history_rich:
                        return {"metas": []}

                    watched_ids = [r["imdb_id"] for r in history_rich]
                    last_watched_id = watched_ids[0] if watched_ids else None

                    all_similar = await _db_similar.get_similar_items(
                        watched_imdb_ids=watched_ids,
                        page=1,
                        page_size=60,
                        lang=lang,
                        last_watched_id=last_watched_id,
                        watch_history_rich=history_rich,
                    )

                    if not all_similar:
                        return {"metas": []}

                    # 60 içeriği RAM'e yaz
                    _similar_cache_set(token, lang, all_similar)

                # Sayfalama: cache'teki 60 içerikten ilgili sayfayı dön
                skip = (page - 1) * PAGE_SIZE
                page_items = all_similar[skip: skip + PAGE_SIZE]

                if not page_items:
                    return {"metas": []}

                metas = [convert_to_stremio_meta(item, lang) for item in page_items]
                return {"metas": metas}

            # ── TMDB Katalogları ──────────────────────────────────────
            elif id.startswith("tmdb_"):
                if "tmdb_trending" in globally_disabled:
                    return {"metas": []}

                from Backend.helper.tmdb_catalog import tmdb_catalog as _tmdb

                if not _tmdb.is_loaded():
                    return {"metas": []}

                if id.startswith("tmdb_trending_"):
                    all_items = _tmdb.get_trending()
                else:
                    return {"metas": []}

                # Sadece gerçek video stream'i olan içerikleri göster
                all_items = [item for item in all_items if _has_video_stream(item)]
                # Film+dizi karışık liste — type filtresi yok
                all_items = all_items[stremio_skip: stremio_skip + PAGE_SIZE]
                metas = [convert_to_stremio_meta(item, lang) for item in all_items]
                return {"metas": metas}

            # ── Admin: Özel (manuel) katalog ────────────────────────────
            elif id.startswith("custom_"):
                from Backend import db as _db_custom

                # id biçimi: custom_<catalog_id>_<lang>
                raw = id[len("custom_"):]
                for sfx in ("_tr", "_de", "_en", "_original"):
                    if raw.endswith(sfx):
                        raw = raw[: -len(sfx)]
                        break
                catalog_id = raw

                catalog = await _db_custom.get_custom_catalog(catalog_id)
                if not catalog or not catalog.get("active", True):
                    return {"metas": []}

                catalog_items = catalog.get("items", [])
                page_items = catalog_items[stremio_skip: stremio_skip + PAGE_SIZE]

                metas = []
                for it in page_items:
                    imdb_id = it.get("imdb_id")
                    if not imdb_id:
                        continue
                    doc = await _db_custom.get_media_by_imdb(imdb_id)
                    if not doc or not _has_video_stream(doc):
                        continue
                    metas.append(convert_to_stremio_meta(doc, lang))
                return {"metas": metas}

            elif "latest" in id:
                sort_params = [("updated_on", "desc")]
            elif "top" in id:
                sort_params = [("rating", "desc")]
            else:
                sort_params = [("updated_on", "desc")]

            if media_type == "movie":
                # collcat gizliyse top/latest filmlerden de seri filmleri çıkar
                exclude_coll = "collcat" in hidden_cats
                data = await db.sort_movies(sort_params, page, PAGE_SIZE, genre_filter=genre_filter, lang=lang, exclude_collection=exclude_coll)
                items = data.get("movies", [])
            else:
                data = await db.sort_tv_shows(sort_params, page, PAGE_SIZE, genre_filter=genre_filter, lang=lang)
                items = data.get("tv_shows", [])
    except Exception as e:
        return {"metas": []}

    # Sadece video stream'i olan içerikleri kataloga ekle (arşiv-only içerikler gizle)
    items = [item for item in items if _has_video_stream(item)]
    metas = [convert_to_stremio_meta(item, lang) for item in items]
    return {"metas": metas}


@router.get("/{token}/meta/{media_type}/{id}.json")
@router.get("/{token}/{lang}/meta/{media_type}/{id}.json")
async def get_meta(token: str, media_type: str, id: str, lang: str = "tr", token_data: dict = Depends(verify_token)):
    lang = resolve_lang(lang)
    if Telegram.HIDE_CATALOG:
        raise HTTPException(status_code=404, detail="Catalog disabled")

    # ── Canlı Yayın meta ─────────────────────────────────────────────
    if media_type == "channel" and id.startswith("live_"):
        channel_id = id[len("live_"):]
        try:
            from Backend import db as _db
            ch = await _db.get_live_channel(channel_id)
        except Exception:
            return {"meta": {}}
        if not ch:
            return {"meta": {}}
        ch_id = ch.get("_id", "")
        return {
            "meta": {
                "id": f"live_{ch_id}",
                "type": "channel",
                "name": ch.get("name", ""),
                "poster": ch.get("poster", "") or ch.get("logo", ""),
                "logo": ch.get("logo", ""),
                "background": ch.get("backdrop", ""),
                "description": ch.get("description", ""),
                "genres": ch.get("genres", []),
                "posterShape": "square",
            }
        }

    # ── Canlı Yayın (yayin_) meta ────────────────────────────────────
    if media_type == "channel" and id.startswith("yayin_"):
        broadcast_id = id[len("yayin_"):]
        try:
            from Backend import db as _db
            bc = await _db.get_broadcast(broadcast_id)
        except Exception:
            return {"meta": {}}
        if not bc:
            return {"meta": {}}
        bid = bc.get("_id", "")
        return {
            "meta": {
                "id": f"yayin_{bid}",
                "type": "channel",
                "name": bc.get("name", "Yayın"),
                "poster": bc.get("poster") or bc.get("logo") or "",
                "logo": bc.get("logo") or "",
                "background": bc.get("poster") or "",
                "description": bc.get("description") or "",
                "genres": bc.get("genres") or [],
                "posterShape": "square",
            }
        }

    try:
        imdb_id = id
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid Stremio ID format")

    media = await db.get_media_details(imdb_id=imdb_id)
    if not media:
        return {"meta": {}}

    if lang == "de":
        name = media.get("title_de") or media.get("title", "")
        description = media.get("description_de") or media.get("description", "")
        meta_genres = media.get("genres_de") or media.get("genres", [])
    elif lang == "en":
        name = media.get("title", "")
        description = media.get("description", "")
        meta_genres = media.get("genres", [])
    else:  # tr
        name = media.get("title_tr") or media.get("title", "")
        description = media.get("description_tr") or media.get("description", "")
        meta_genres = media.get("genres_tr") or media.get("genres", [])

    if lang == "de":
        meta_poster = media.get("poster_de") or media.get("poster", "")
        meta_backdrop = media.get("backdrop_de") or media.get("backdrop", "")
        meta_logo = media.get("logo_de") or media.get("logo", "")
    elif lang == "en":
        meta_poster = media.get("poster") or ""
        meta_backdrop = media.get("backdrop") or ""
        meta_logo = media.get("logo") or ""
    else:  # tr
        meta_poster = media.get("poster_tr") or media.get("poster", "")
        meta_backdrop = media.get("backdrop_tr") or media.get("backdrop", "")
        meta_logo = media.get("logo_tr") or media.get("logo", "")

    meta_obj = {
        "id": id,
        "type": "series" if media.get("media_type") == "tv" else "movie",
        "name": name,
        "description": description,
        "year": str(media.get("release_year", "")),
        "imdbRating": str(media.get("rating", "")),
        "genres": meta_genres,
        "poster": meta_poster,
        "logo": meta_logo,
        "background": meta_backdrop,
        "imdb_id": media.get("imdb_id", ""),
        "releaseInfo": str(media.get("release_year", "")),
        "moviedb_id": media.get("tmdb_id", ""),
        "cast": media.get("cast") or [],
        "runtime": media.get("runtime") or "",
    }

    if media.get("media_type") == "movie":
        released_date = format_released_date(media)
        if released_date:
            meta_obj["released"] = released_date

    # --- Add Episodes ---
    if media_type == "series" and "seasons" in media:

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

        videos = []

        for season in sorted(media.get("seasons", []), key=lambda s: s.get("season_number")):
            for episode in sorted(season.get("episodes", []), key=lambda e: e.get("episode_number")):

                # Bölümün gerçek video stream'i var mı kontrol et (zip/arşiv olan bölümleri atla)
                ep_qualities = episode.get("telegram", [])
                has_real_video = False
                for _q in ep_qualities:
                    _qname = _q.get("name", "")
                    if _q.get("is_archive", False):
                        continue
                    if _is_archive_fn(_qname):
                        continue
                    # Split video dosyaları (.mkv.001) doğrudan geçer
                    if _is_split_video(_q):
                        has_real_video = True
                        break
                    if any(_qname.lower().endswith(ext) for ext in _ALLOWED_VIDEO_EXTS_MOD):
                        has_real_video = True
                        break
                if not has_real_video:
                    continue

                episode_id = f"{id}:{season['season_number']}:{episode['episode_number']}"

                # Dile göre bölüm başlığı
                if lang == "de":
                    ep_title = episode.get("title_de") or episode.get("title", f"Episode {episode['episode_number']}")
                    ep_overview = episode.get("overview_de") or episode.get("overview") or "Für diese Episode ist noch keine Beschreibung verfügbar."
                elif lang == "en":
                    ep_title = episode.get("title", f"Episode {episode['episode_number']}")
                    ep_overview = episode.get("overview") or "No description available for this episode yet."
                else:  # tr
                    ep_title = episode.get("title_tr") or episode.get("title", f"Episode {episode['episode_number']}")
                    ep_overview = episode.get("overview_tr") or episode.get("overview") or "Bu bölüm için henüz açıklama bulunmuyor."

                videos.append({
                    "id": episode_id,
                    "title": ep_title,
                    "season": season.get("season_number"),
                    "episode": episode.get("episode_number"),
                    "overview": ep_overview,
                    "released": episode.get("released") or yesterday,
                    "thumbnail": episode.get("episode_backdrop") or Telegram.BOLUM_RESIMI or None,
                    "imdb_id": episode.get("imdb_id") or media.get("imdb_id"),
                })

        meta_obj["videos"] = videos
    return {"meta": meta_obj}

@router.get("/{token}/stream/{media_type}/{id}.json")
@router.get("/{token}/{lang}/stream/{media_type}/{id}.json")
async def get_streams(
    token: str,
    media_type: str,
    id: str,
    lang: str = "tr",
    token_data: dict = Depends(verify_token)
):
    # Abonelik ve limit kontrolleri
    if token_data.get("subscription_expired"):
        from Backend.config import Telegram as _TG
        from Backend.fastapi.security.tokens import _configure_url
        _EXPIRED_MESSAGES = {
            "tr": ("🚫 Abonelik Süresi Doldu", "Aboneliğinizin süresi dolmuştur. Yenilemek için yönetici ile iletişime geçin."),
            "de": ("🚫 Abonnement abgelaufen", "Ihr Abonnement ist abgelaufen. Bitte wenden Sie sich an den Administrator, um es zu verlängern."),
            "en": ("🚫 Subscription Expired", "Your subscription has expired. Please contact the administrator to renew."),
        }
        _resolved = lang if lang in _EXPIRED_MESSAGES else "en"
        _exp_name, _exp_title = _EXPIRED_MESSAGES[_resolved]
        return {"streams": [{"name": _exp_name, "title": _exp_title, "url": _configure_url(token, _resolved)}]}

    if token_data.get("limit_exceeded"):
        from Backend.fastapi.security.tokens import _configure_url
        _LIMIT_MESSAGES = {
            "tr": ("🚫 Limit Aşıldı", "🚫 Kullanım limitiniz doldu – Aboneliğinizi yükseltmeniz gerekiyor."),
            "de": ("🚫 Limit erreicht", "🚫 Ihr Nutzungslimit wurde erreicht – Bitte upgraden Sie Ihr Abonnement."),
            "en": ("🚫 Limit Reached", "🚫 Your usage limit has been reached – Please upgrade your subscription."),
        }
        _lang_resolved = lang if lang in _LIMIT_MESSAGES else "en"
        _limit_name, _limit_title = _LIMIT_MESSAGES[_lang_resolved]
        return {"streams": [{"name": _limit_name, "title": _limit_title, "url": _configure_url(token, _lang_resolved)}]}

    # ── Canlı Yayın stream ───────────────────────────────────────────
    if media_type == "channel" and id.startswith("live_"):
        channel_id = id[len("live_"):]
        try:
            from Backend import db as _db
            ch = await _db.get_live_channel(channel_id)
        except Exception:
            return {"streams": []}
        if not ch:
            return {"streams": []}
        streams = []
        for lnk in ch.get("links", []):
            url = lnk.get("url", "")
            if not url:
                continue
            # Sadece etiket gösterilir — kanal adı stream listesinde yer almaz
            label = lnk.get("label", "").strip() or ch.get("name", "")
            # Her link için ayrı logo; yoksa kanalın genel logosu
            link_logo = lnk.get("logo", "").strip() or ch.get("logo", "")
            stream: dict = {
                "name": label,   # sadece etiket
                "title": "",
                "url": url,
                "behaviorHints": {"notWebReady": False},
            }
            if link_logo:
                stream["thumbnail"] = link_logo
            streams.append(stream)
        return {"streams": streams}

    # ── Canlı Yayın (yayin_) stream ──────────────────────────────────
    if media_type == "channel" and id.startswith("yayin_"):
        broadcast_id = id[len("yayin_"):]
        try:
            from Backend import db as _db
            # Önce tek broadcast'i almayı dene, yoksa aktif yayınlardan filtrele
            bc = None
            try:
                bc = await _db.get_broadcast(broadcast_id)
            except Exception:
                active = await _db.get_active_broadcasts()
                for item in active:
                    if str(item.get("_id", "")) == str(broadcast_id):
                        bc = item
                        break
        except Exception:
            return {"streams": []}
        if not bc:
            return {"streams": []}
        from Backend.config import Telegram as _TG
        BASE_URL = _TG.BASE_URL.rstrip("/")
        stream_url = f"{BASE_URL}/yayin/stream/{broadcast_id}/playlist.m3u8?token={token}"
        logo = bc.get("logo") or ""
        bc_name = bc.get("name", "Canlı Yayın")

        # ── Proxy Modu ────────────────────────────────────────────────
        # PROXY_MODE=1 → sadece normal link
        # PROXY_MODE=2 → önce proxy, sonra normal (her ikisi)
        # PROXY_MODE=3 → sadece proxy
        proxy_url = (
            f"{_TG.HTTP_PROXY_URL}{stream_url}"
            if _TG.PROXY and _TG.HTTP_PROXY_URL
            else None
        )

        # ── Yayın aktif değilse → standby video stream ekle ─────────
        is_active = bc.get("active", False)
        standby_url = None
        from Backend.fastapi.routes.yayin_routes import _standby_active as _sa
        sm = bc.get("standby_media") or {}
        # Sadece video tipi desteklenir (resim HLS ile oynatılamaz)
        if not is_active and sm.get("path") and sm.get("media_type") == "video" and _sa(bc):
            standby_url = f"{BASE_URL}/yayin/standby/{broadcast_id}/playlist.m3u8?token={token}"

        def _make_entry(name: str, url: str, title: str = "") -> dict:
            entry: dict = {
                "name": name,
                "title": title,
                "url": url,
                "behaviorHints": {"notWebReady": False},
            }
            if logo:
                entry["thumbnail"] = logo
            return entry

        streams_out = []

        if is_active:
            # Yayın canlı → normal stream
            if _TG.PROXY and proxy_url and _TG.PROXY_MODE == 2:
                streams_out = [
                    _make_entry(f"{bc_name} 🔀 Proxy", proxy_url),
                    _make_entry(f"{bc_name} ⚡ Direct", stream_url),
                ]
            elif _TG.PROXY and proxy_url and _TG.PROXY_MODE == 3:
                streams_out = [_make_entry(f"{bc_name} 🔀 Proxy", proxy_url)]
            else:
                streams_out = [_make_entry(bc_name, stream_url)]
        elif standby_url:
            # Yayın henüz başlamadı, standby medya tanımlı ve süresi geçmemiş
            media_type_label = "🎬 Video" if sm.get("media_type") == "video" else "🖼️ Resim"
            streams_out = [_make_entry(
                f"{bc_name} — Yayın Bekleniyor",
                standby_url,
                title=f"Yayın Öncesi {media_type_label}"
            )]
        # else: yayın yok, standby yok → boş liste

        return {"streams": streams_out}

    try:
        parts = id.split(":")
        imdb_id = parts[0]
        season_num = int(parts[1]) if len(parts) > 1 else None
        episode_num = int(parts[2]) if len(parts) > 2 else None
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid Stremio ID format")

    media_details = await db.get_media_details(
        imdb_id=imdb_id,
        season_number=season_num,
        episode_number=episode_num
    )

    if not media_details or "telegram" not in media_details:
        return {"streams": []}

    streams = []

    # Dile göre sertifikayı seç
    if lang == "de":
        cert = media_details.get("certification_de") or media_details.get("certification_us") or ""
    elif lang == "tr":
        cert = media_details.get("certification_tr") or media_details.get("certification_us") or ""
    else:
        cert = media_details.get("certification_us") or ""
    
    # 1. Döngü Bloğu (4 boşluk girinti)
    import re as _re_stremio
    _ARCHIVE_EXTS = (".zip", ".7z", ".rar")

    def _is_archive_filename(name: str) -> bool:
        """Multipart arşiv dahil tüm arşiv dosyalarını tespit eder.
        
        Arşiv sayılanlar: .zip, .7z, .rar (tek parça veya .zip.001 / .7z.001 gibi parçalar)
        Arşiv SAYILMAYANLAR: .part001.mkv, .part002.mkv gibi video parçaları
        """
        n = name.lower()
        if n.endswith(_ARCHIVE_EXTS):
            return True
        # Multipart arşiv parçaları: .zip.001, .7z.001, .z01, .z02
        if _re_stremio.search(r'\.(zip|7z|rar|z)\.\d+$', n):
            return True
        # .part1.rar, .part01.rar  → arşiv
        if _re_stremio.search(r'\.part\d+\.rar$', n):
            return True
        # .part001.mkv, .part01.mkv gibi video parçaları → arşiv DEĞİL, izin ver
        # (bu blok kasıtlı boş - sadece yukarıdakiler arşivdir)
        return False

    for quality in media_details.get("telegram", []):
        file_id = quality.get("id")

        # ── Split dosya: parts listesi varsa virtual stream URL üret ─────────
        parts_list = quality.get("parts")
        if parts_list and len(parts_list) >= 1:
            from Backend.helper.encrypt import encode_string as _enc_str
            from Backend.helper.stream_token import media_token_manager
            _parts_payload = [
                {"chat_id": p["chat_id"], "msg_id": p["msg_id"], "part_number": p["part_number"]}
                for p in sorted(parts_list, key=lambda x: x.get("part_number", 0))
            ]
            _encoded_parts_id = await _enc_str({"parts": _parts_payload})
            _vtok = media_token_manager.create(token, _encoded_parts_id, kind="video")
            _base_url = Telegram.BASE_URL.rstrip("/")
            filename = quality.get("name", "video.mkv")
            # Split dosya adındaki .001 suffix'ini temizle (video.mkv.001 → video.mkv)
            from Backend.helper.split_files import strip_part_suffix as _strip_suffix
            filename_clean = _strip_suffix(filename) if filename else "video.mkv"
            quality_str = quality.get("quality", "HD")
            size = quality.get("size", "")
            stream_name, stream_title = format_stream_details(
                filename_clean, quality_str, size, _encoded_parts_id, certification=cert, is_split=True
            )
            _safe_fn = __import__("urllib.parse", fromlist=["quote"]).quote(filename_clean or "video.mkv", safe=".-_")
            url = f"{_base_url}/dl/{token}/{_encoded_parts_id}/{_vtok}/{_safe_fn}"
            proxy_url = (
                f"{Telegram.HTTP_PROXY_URL}{url}"
                if Telegram.PROXY and Telegram.HTTP_PROXY_URL
                else None
            )
            if Telegram.PROXY and proxy_url and Telegram.PROXY_MODE == 2:
                streams.append({"name": f"{stream_name} 🔀 Proxy", "title": stream_title, "url": proxy_url, "_size_bytes": parse_size_to_bytes(size)})
                streams.append({"name": stream_name, "title": stream_title, "url": url, "_size_bytes": parse_size_to_bytes(size)})
            elif Telegram.PROXY and proxy_url and Telegram.PROXY_MODE == 3:
                streams.append({"name": f"{stream_name} 🔀 Proxy", "title": stream_title, "url": proxy_url, "_size_bytes": parse_size_to_bytes(size)})
            else:
                streams.append({"name": stream_name, "title": stream_title, "url": url, "_size_bytes": parse_size_to_bytes(size)})
            continue
        # ─────────────────────────────────────────────────────────────────────

        if not file_id:
            continue

        filename = quality.get("name", "")
        # Arşiv ve desteklenmeyen dosyaları Stremio'da gösterme
        if _is_archive_filename(filename) or quality.get("is_archive", False):
            continue
        # Sadece video uzantılarına izin ver: .mkv .avi .mpg .mp4
        _ALLOWED_VIDEO_EXTS = (".mkv", ".avi", ".mpg", ".mpeg", ".mp4", ".ts", ".m4v", ".webm", ".flv", ".mov", ".wmv")
        _fn_lower = filename.lower()

        # Split video dosyalarını (.mkv.001 gibi) da kabul et
        _is_split = _is_split_video(quality)
        if not _is_split and not any(_fn_lower.endswith(ext) for ext in _ALLOWED_VIDEO_EXTS):
            continue

        quality_str = quality.get("quality", "")
        size = quality.get("size", "")

        # Split dosya: tek parça file_id ile kaydedilmiş ama adı .mkv.001 ile bitiyor
        # Bu durumda dosya adındaki .001 suffix'ini temizleyerek format_stream_details'e gönder
        if _is_split and not quality.get("parts"):
            from Backend.helper.split_files import strip_part_suffix as _strip_suffix_single
            filename_for_display = _strip_suffix_single(filename) if filename else filename
        else:
            filename_for_display = filename

        stream_name, stream_title = format_stream_details(
            filename_for_display, quality_str, size, file_id, certification=cert, is_split=_is_split
        )

        if file_id.startswith(("http://", "https://")) and "/api/sunucu/indir" in file_id:
            # Sunucu panelinden eklenen yerel dosya — auth gerektiren URL'yi
            # token korumalı /dl/ yoluna dönüştür (Stremio cookie taşımaz)
            from Backend.helper.stream_token import media_token_manager
            from Backend.helper.encrypt import encode_string as _encode_str
            from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs, quote as _q
            from Backend.fastapi.routes.sunucu_routes import SUNUCU_DIR as _SUNUCU_DIR
            _parsed = _urlparse(file_id)
            _qs = _parse_qs(_parsed.query)
            _rel = _qs.get("path", [""])[0]
            if _rel:
                _abs_path = str((_SUNUCU_DIR / _rel.lstrip("/\\")).resolve())
                _encoded_id = await _encode_str({"local_path": _abs_path})
                _video_tok = media_token_manager.create(token, _encoded_id, kind="video")
                _safe_fn = _q(filename or "video.mkv", safe=".-_")
                _base_url = Telegram.BASE_URL.rstrip("/")
                url = f"{_base_url}/dl/{token}/{_encoded_id}/{_video_tok}/{_safe_fn}"
            else:
                url = file_id
        elif file_id.startswith(("http://", "https://")):
            url = file_id
        else:
            from Backend.helper.stream_token import media_token_manager
            from Backend.helper.encrypt import decode_string as _decode_str
            import asyncio as _asyncio
            # Google Drive encoded_string mi kontrol et
            _is_gdrive = False
            try:
                _decoded = await _decode_str(file_id)
                if _decoded.get("gdrive_file_id"):
                    _is_gdrive = True
            except Exception as _decode_err:
                import logging as _logging
                _logging.getLogger(__name__).warning(f"[stremio] encoded_string decode hatası, stream atlanıyor: {_decode_err}")
                continue

            # GDrive veya Telegram: gecici token ile URL üret
            video_tok = media_token_manager.create(token, file_id, kind="video")
            _base_url = Telegram.BASE_URL.rstrip("/")
            url = f"{_base_url}/dl/{token}/{file_id}/{video_tok}/video.mkv"

        # ── Proxy Modu ────────────────────────────────────────────────────────
        # PROXY_MODE=1 → sadece normal link
        # PROXY_MODE=2 → önce proxy, sonra normal (her ikisi)
        # PROXY_MODE=3 → sadece proxy
        proxy_url = (
            f"{Telegram.HTTP_PROXY_URL}{url}"
            if Telegram.PROXY and Telegram.HTTP_PROXY_URL
            else None
        )

        if Telegram.PROXY and proxy_url and Telegram.PROXY_MODE == 2:
            # Hem proxy hem normal — her ikisi de etiketli
            streams.append({
                "name": f"{stream_name} 🔀 Proxy",
                "title": stream_title,
                "url": proxy_url,
                "_size_bytes": parse_size_to_bytes(size),
            })
            streams.append({
                "name": f"{stream_name} ⚡ Direct",
                "title": stream_title,
                "url": url,
                "_size_bytes": parse_size_to_bytes(size),
            })
        elif Telegram.PROXY and proxy_url and Telegram.PROXY_MODE == 3:
            # Sadece proxy — etiket ekle
            streams.append({
                "name": f"{stream_name} 🔀 Proxy",
                "title": stream_title,
                "url": proxy_url,
                "_size_bytes": parse_size_to_bytes(size),
            })
        else:
            # PROXY_MODE=1 veya proxy kapalı → sadece normal, etiket yok
            streams.append({
                "name": stream_name,
                "title": stream_title,
                "url": url,
                "_size_bytes": parse_size_to_bytes(size),
            })

    # 2. Sıralama ve Düzenleme Bloğu
    streams.sort(
        key=lambda s: (
            # 1. Kriter: İsimde "Link" geçiyorsa 1, geçmiyorsa 0 (Link olanlar üstte olur)
            1 if s.get("name", "").startswith("Link") else 0,
            # 2. Kriter: Çözünürlük değeri (2160, 1080 vb.)
            get_resolution_priority(s.get("name", "")),
            # 3. Kriter: Aynı çözünürlükteki videolarda dosya boyutu büyük olan üstte
            s.get("_size_bytes", 0),
        ),
        reverse=True  # Her üç kriter için de en yüksek değer en üstte görünür
    )

    name_count: dict = {}
    for s in streams:
        name_count[s["name"]] = name_count.get(s["name"], 0) + 1

    seen: dict = {}
    for s in streams:
        if name_count[s["name"]] > 1:
            seen[s["name"]] = seen.get(s["name"], 0) + 1
            s["name"] = f"{s['name']} ({seen[s['name']]})"

    # Geçici sıralama alanını temizle
    for s in streams:
        s.pop("_size_bytes", None)

    # 3. Return (En dıştaki fonksiyon hızıyla aynı olmalı)
    return {"streams": streams}


# ── Katalog tercihleri kaydı ───────────────────────────────────────────────
@router.post("/{token}/catalog-prefs")
async def save_catalog_prefs(token: str, request: Request, token_data: dict = Depends(verify_token)):
    from fastapi.responses import JSONResponse
    from Backend import db as _db_prefs
    try:
        body = await request.json()
        hidden = body.get("hidden_catalogs", [])
        order = body.get("catalog_order", [])
        if not isinstance(hidden, list) or not isinstance(order, list):
            return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
        await _db_prefs.save_catalog_prefs_full(token, hidden, order)
        return JSONResponse({"ok": True})
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return JSONResponse({"ok": False, "error": "Sunucu hatası"}, status_code=500)


# ── Kanal sırası kaydı ────────────────────────────────────────────────────
@router.post("/{token}/channel-order")
async def save_channel_order(token: str, request: Request, token_data: dict = Depends(verify_token)):
    from fastapi.responses import JSONResponse
    from Backend import db as _db
    try:
        body = await request.json()
        channel_order = body.get("channel_order", [])

        if not isinstance(channel_order, list):
            return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)

        # DB'deki gerçek kanal ID'lerini çek — yabancı ID girilmesini engelle
        all_channels = await _db.get_live_channels()
        valid_ids = {ch["_id"] for ch in all_channels}
        filtered_order = [cid for cid in channel_order if cid in valid_ids]

        await _db.save_channel_order(token, filtered_order)
        return JSONResponse({"ok": True})
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return JSONResponse({"ok": False, "error": "Sunucu hatası"}, status_code=500)


# ── Dahili endpoint: scheduler tarafından tetiklenir ─────────────────────────
@router.post("/internal/platform-catalog/refresh")
@router.get("/internal/platform-catalog/refresh")
async def refresh_platform_catalog():
    """Mongodump klasöründen platform kataloğunu yeniden yükler."""
    import asyncio
    loop = asyncio.get_event_loop()
    platform_catalog.schedule_refresh()
    return {
        "ok": True,
        "stats": platform_catalog.stats(),
    }


@router.get("/internal/platform-catalog/stats")
async def platform_catalog_stats():
    """Her platformdaki içerik sayısını döndürür (debug için)."""
    return {
        "loaded": platform_catalog.is_loaded(),
        "stats":  platform_catalog.stats(),
    }


@router.post("/internal/tmdb-catalog/refresh")
@router.get("/internal/tmdb-catalog/refresh")
async def refresh_tmdb_catalog():
    """TMDB kataloğunu elle yeniler (debug / zorla güncelleme için)."""
    import asyncio
    from Backend.helper.tmdb_catalog import tmdb_catalog as _tmdb
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _tmdb.refresh)
    return {
        "ok": True,
        "stats": _tmdb.stats(),
    }


@router.get("/internal/tmdb-catalog/stats")
async def tmdb_catalog_stats():
    """TMDB katalogundaki içerik sayılarını döndürür (debug için)."""
    from Backend.helper.tmdb_catalog import tmdb_catalog as _tmdb
    import datetime
    last_ts = _tmdb.last_refresh_ts()
    last_str = (
        datetime.datetime.fromtimestamp(last_ts).isoformat() if last_ts else "never"
    )
    return {
        "loaded": _tmdb.is_loaded(),
        "last_refresh": last_str,
        "stats": _tmdb.stats(),
    }


# ── Stremio Subtitles Endpoint ────────────────────────────────────────────────

@router.get("/{token}/subtitles/{media_type}/{id_path:path}.json")
@router.get("/{token}/{lang}/subtitles/{media_type}/{id_path:path}.json")
async def get_subtitles(
    token: str,
    media_type: str,
    id_path: str,
    lang: str = "tr",
    token_data: dict = Depends(verify_token),
):
    """
    Stremio Subtitles Addon API endpoint'i.

    Stremio id'yi bazen extra query bilgisiyle gönderir:
      tt1234567/filename=video.mkv&videoSize=123&videoHash=abc
    Bu yüzden {id_path:path} ile tüm path yakalanır, içinden tt... ID parse edilir.
    """
    if token_data.get("subscription_expired") or token_data.get("limit_exceeded"):
        return {"subtitles": []}

    # id_path'ten gerçek IMDb/Stremio ID'sini çıkar
    # Stremio bazen: "tt1234567/filename=video.mkv&videoSize=...&videoHash=..."
    # Bazen: "tt1234567:1:3/filename=..."  (dizi için)
    import re as _re_sub
    raw_id = id_path.split("/")[0]  # "/" dan öncesi gerçek ID
    raw_id = raw_id.split("?")[0]   # query string varsa temizle

    try:
        parts = raw_id.split(":")
        imdb_id = parts[0]
        season_num = int(parts[1]) if len(parts) > 1 else None
        episode_num = int(parts[2]) if len(parts) > 2 else None
    except (ValueError, IndexError):
        return {"subtitles": []}

    if not imdb_id or not imdb_id.startswith("tt"):
        return {"subtitles": []}

    # DB'den altyazıları çek
    try:
        subs = await db.get_subtitles(imdb_id, season_num, episode_num)
    except Exception:
        return {"subtitles": []}

    if not subs:
        return {"subtitles": []}

    base_url = BASE_URL.rstrip("/")

    # Dil etiketleri: Stremio ISO 639-1 kodu bekler
    _LANG_DISPLAY = {
        "tr": "Türkçe",
        "en": "English",
        "de": "Deutsch",
        "fr": "Français",
        "es": "Español",
        "it": "Italiano",
        "pt": "Português",
        "ru": "Русский",
        "ar": "العربية",
        "ja": "日本語",
        "ko": "한국어",
        "zh": "中文",
        "nl": "Nederlands",
        "pl": "Polski",
        "sv": "Svenska",
        "no": "Norsk",
        "da": "Dansk",
    }

    result = []
    for s in subs:
        sub_id = s.get("_id", "")
        sub_lang = s.get("lang", "tr")
        lang_label = _LANG_DISPLAY.get(sub_lang, s.get("lang_label", sub_lang.upper()))
        result.append({
            "id": sub_id,
            "url": f"{base_url}/subtitles/serve/{sub_id}",
            "lang": sub_lang,
            # Stremio bazı istemcilerde 'label' alanını gösterir
            "label": lang_label,
        })

    return {"subtitles": result}


# ════════════════════════════════════════════════════════════════════════════
# ── ADMIN: Katalog Yönetimi ────────────────────────────────────────────────
#
# Not: Bu router BİLEREK "/stremio" prefix'i altında DEĞİL, "/api/admin/catalogs"
# altında tanımlanır. Çünkü CSRFMiddleware "/stremio/" ile başlayan tüm path'leri
# CSRF kontrolünden muaf tutar (üye tarafı public endpoint'leri için). Admin'in
# state değiştiren (POST/PUT/DELETE) istekleri ise CSRF korumasına tabi olmalı;
# "/api/admin/" prefix'i zaten CSRF_PROTECTED_PREFIXES içinde tanımlı.
# ════════════════════════════════════════════════════════════════════════════

admin_catalog_router = APIRouter(prefix="/api/admin/catalogs", tags=["Admin - Katalog Yönetimi"])


@admin_catalog_router.get("")
async def admin_list_catalogs(_: bool = Depends(require_auth)):
    """Hazır (built-in) katalogların açık/kapalı durumu + tüm özel katalogları döndürür."""
    from Backend import db as _db

    global_settings = await _db.get_catalog_global_settings()
    disabled = set(global_settings.get("disabled", []))

    builtin = [
        {
            "id": cat_id,
            "label": info["label"],
            "type": info["type"],
            "enabled": cat_id not in disabled,
        }
        for cat_id, info in TOGGLEABLE_BUILTIN_CATALOGS.items()
    ]

    custom_raw = await _db.get_custom_catalogs(active_only=False)
    custom = [
        {
            "_id": c["_id"],
            "name": c.get("name", ""),
            "media_type": c.get("media_type", "mixed"),
            "active": c.get("active", True),
            "order": c.get("order", 0),
            "item_count": len(c.get("items", [])),
            "keywords": c.get("keywords", []),
        }
        for c in custom_raw
    ]

    return {"builtin": builtin, "custom": custom}


@admin_catalog_router.post("/builtin/toggle")
async def admin_toggle_builtin_catalog(payload: dict, _: bool = Depends(require_auth)):
    """Hazır bir kataloğu (netflix, disney, trendler, sana özel, ...) global olarak açar/kapatır."""
    from Backend import db as _db

    catalog_id = payload.get("catalog_id", "")
    enabled = bool(payload.get("enabled", True))

    if catalog_id not in TOGGLEABLE_BUILTIN_CATALOGS:
        raise HTTPException(status_code=400, detail="Geçersiz katalog id")

    await _db.set_builtin_catalog_enabled(catalog_id, enabled)
    return {"ok": True, "catalog_id": catalog_id, "enabled": enabled}


@admin_catalog_router.get("/media-search")
async def admin_catalog_media_search(
    q: str = "",
    media_type: Optional[str] = None,
    _: bool = Depends(require_auth),
):
    """Kataloğa eklemek için yerel veritabanında film/dizi arar (başlık bazlı)."""
    if not q or not q.strip():
        return {"results": []}

    result = await db.search_documents(query=q, page=1, page_size=20)
    results = result.get("results", [])

    if media_type in ("movie", "tv"):
        results = [r for r in results if r.get("media_type") == media_type]

    formatted = [
        {
            "imdb_id":      r.get("imdb_id", ""),
            "title":        r.get("title_tr") or r.get("title", ""),
            "poster":       r.get("poster_tr") or r.get("poster", ""),
            "media_type":   r.get("media_type", ""),
            "release_year": r.get("release_year"),
        }
        for r in results
        if r.get("imdb_id")
    ]
    return {"results": formatted}


def _normalize_keywords(raw) -> list:
    """Katalog anahtar kelimelerini normalize eder.

    Hem liste (["kelime1", "kelime2"]) hem de virgülle ayrılmış tek bir
    string ("kelime1, kelime2") kabul edilir. Boş/tekrar eden kelimeler
    temizlenir. Kelime tanımlanmazsa (boş liste) otomatik ekleme devre dışı
    kalır ve katalog eskisi gibi sadece elle yönetilir.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        parts = raw
    else:
        return []

    seen = set()
    keywords = []
    for p in parts:
        kw = str(p).strip()
        if not kw:
            continue
        key = kw.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(kw)
    return keywords


@admin_catalog_router.post("/custom")
async def admin_create_custom_catalog(payload: dict, _: bool = Depends(require_auth)):
    """Yeni özel katalog oluşturur."""
    from Backend import db as _db

    name = (payload.get("name") or "").strip()
    media_type = payload.get("media_type", "mixed")

    if not name:
        raise HTTPException(status_code=400, detail="Katalog adı gerekli")
    if media_type not in ("movie", "series", "mixed"):
        media_type = "mixed"

    data = {
        "name": name,
        "media_type": media_type,
        "active": True,
        "items": [],
        "keywords": _normalize_keywords(payload.get("keywords")),
    }
    catalog = await _db.add_custom_catalog(data)
    return catalog


@admin_catalog_router.put("/custom/{catalog_id}")
async def admin_update_custom_catalog(catalog_id: str, payload: dict, _: bool = Depends(require_auth)):
    """Özel katalog adı/tipi/aktiflik durumunu günceller."""
    from Backend import db as _db

    update_data = {}
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Katalog adı boş olamaz")
        update_data["name"] = name
    if "media_type" in payload and payload["media_type"] in ("movie", "series", "mixed"):
        update_data["media_type"] = payload["media_type"]
    if "active" in payload:
        update_data["active"] = bool(payload["active"])
    if "order" in payload:
        try:
            update_data["order"] = int(payload["order"])
        except (TypeError, ValueError):
            pass
    if "keywords" in payload:
        update_data["keywords"] = _normalize_keywords(payload.get("keywords"))

    if not update_data:
        raise HTTPException(status_code=400, detail="Güncellenecek alan yok")

    ok = await _db.update_custom_catalog(catalog_id, update_data)
    if not ok:
        raise HTTPException(status_code=404, detail="Katalog bulunamadı")
    return {"ok": True}


_rescan_jobs: dict = {}  # job_id -> {status, checked, matched, total, collection, error, partial_error}


@admin_catalog_router.post("/custom/{catalog_id}/rescan")
async def admin_rescan_custom_catalog(catalog_id: str, _: bool = Depends(require_auth)):
    """Kataloğun kelimelerini, HALİHAZIRDA kütüphanede olan (kelime tanımlanmadan önce
    eklenmiş) film/dizilerin dosya adlarına karşı geriye dönük tarar ve eşleşenleri ekler.
    Bundan sonra eklenecek yeni dosyalar zaten insert_media() içinde otomatik taranıyor;
    bu endpoint sadece geçmişe dönük içerikleri yakalamak içindir.

    Tarama, özellikle büyük dizi kütüphanelerinde uzun sürebildiğinden (ve tek bir HTTP
    isteği içinde tutmak zaman aşımına yol açabildiğinden), tarama arka planda bir görev
    olarak başlatılır. Dönen job_id ile /rescan/status/{job_id} endpoint'i periyodik olarak
    sorgulanarak ilerleme takip edilebilir.
    """
    import uuid
    from asyncio import create_task
    from Backend import db as _db

    catalog = await _db.get_custom_catalog(catalog_id)
    if not catalog:
        raise HTTPException(status_code=404, detail="Katalog bulunamadı")
    if not [k for k in (catalog.get("keywords") or []) if k and k.strip()]:
        raise HTTPException(status_code=400, detail="Bu katalog için kelime tanımlı değil")

    job_id = str(uuid.uuid4())
    _rescan_jobs[job_id] = {
        "status": "running",
        "checked": 0,
        "matched": 0,
        "total": 0,
        "collection": "başlıyor",
        "error": None,
        "partial_error": None,
    }

    async def _run_rescan():
        job = _rescan_jobs[job_id]

        async def _on_progress(update: dict):
            job.update(update)

        try:
            result = await _db.rescan_custom_catalog_by_keywords(catalog_id, progress_cb=_on_progress)
            if result.get("error"):
                job["status"] = "error"
                job["error"] = result["error"]
            else:
                job["status"] = "done"
                job["checked"] = result.get("checked", 0)
                job["matched"] = result.get("matched", 0)
                job["partial_error"] = result.get("partial_error")
        except Exception as e:
            _logger.error(f"[custom_catalogs] Arka plan tarama hatası: {e}")
            job["status"] = "error"
            job["error"] = str(e)

    create_task(_run_rescan())
    return {"job_id": job_id}


@admin_catalog_router.get("/custom/rescan/status/{job_id}")
async def admin_rescan_status(job_id: str, _: bool = Depends(require_auth)):
    """Arka planda çalışan bir tarama görevinin ilerlemesini döner."""
    job = _rescan_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Tarama görevi bulunamadı")
    return job


@admin_catalog_router.delete("/custom/{catalog_id}")
async def admin_delete_custom_catalog(catalog_id: str, _: bool = Depends(require_auth)):
    """Özel kataloğu tamamen siler."""
    from Backend import db as _db

    ok = await _db.delete_custom_catalog(catalog_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Katalog bulunamadı")
    return {"ok": True}


@admin_catalog_router.get("/custom/{catalog_id}/items")
async def admin_list_custom_catalog_items(catalog_id: str, _: bool = Depends(require_auth)):
    """Bir özel kataloğa eklenmiş film/dizileri listeler."""
    from Backend import db as _db

    catalog = await _db.get_custom_catalog(catalog_id)
    if not catalog:
        raise HTTPException(status_code=404, detail="Katalog bulunamadı")
    return {"items": catalog.get("items", [])}


@admin_catalog_router.post("/custom/{catalog_id}/items")
async def admin_add_custom_catalog_item(catalog_id: str, payload: dict, _: bool = Depends(require_auth)):
    """Özel kataloğa bir film/dizi ekler (imdb_id ile)."""
    from Backend import db as _db

    imdb_id = (payload.get("imdb_id") or "").strip()
    if not imdb_id:
        raise HTTPException(status_code=400, detail="imdb_id gerekli")

    catalog = await _db.get_custom_catalog(catalog_id)
    if not catalog:
        raise HTTPException(status_code=404, detail="Katalog bulunamadı")

    media_doc = await _db.get_media_by_imdb(imdb_id)
    if not media_doc:
        raise HTTPException(status_code=404, detail="Bu içerik veritabanında bulunamadı")

    item = {
        "imdb_id":    imdb_id,
        "media_type": media_doc.get("media_type", "movie"),
        "title":      media_doc.get("title_tr") or media_doc.get("title", ""),
        "poster":     media_doc.get("poster_tr") or media_doc.get("poster", ""),
    }

    added = await _db.add_custom_catalog_item(catalog_id, item)
    if not added:
        raise HTTPException(status_code=409, detail="Bu içerik zaten katalogda ekli")
    return {"ok": True, "item": item}


@admin_catalog_router.delete("/custom/{catalog_id}/items/{imdb_id}")
async def admin_remove_custom_catalog_item(catalog_id: str, imdb_id: str, _: bool = Depends(require_auth)):
    """Özel katalogdan bir film/diziyi çıkarır."""
    from Backend import db as _db

    ok = await _db.remove_custom_catalog_item(catalog_id, imdb_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Kayıt bulunamadı")
    return {"ok": True}
