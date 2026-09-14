"""
daily_content_notifier.py
==========================

Saati değiştirmek için:
  Bu dosyanın başındaki NOTIFY_HOUR ve NOTIFY_MINUTE sabitlerini düzenleyin.
  Örneğin sabah 08:30'da göndermek için:
      NOTIFY_HOUR   = 8
      NOTIFY_MINUTE = 30

Entegrasyon (db_scheduler.py → start_scheduler fonksiyonuna ekle):
    from Backend.helper.daily_content_notifier import start_daily_content_notifier
    start_daily_content_notifier(main_loop=_main_loop)
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger("daily_content_notifier")

# ─── Poster kolajı ayarları ────────────────────────────────────────────────
# Kolaja dahil edilecek maksimum poster sayısı (çok fazla içerik varsa
# görsel aşırı büyük/karmaşık olmasın diye sınırlanır).
# Kolajdaki satır sayısı (dikey) her zaman 2, 3 ya da 4 olacak şekilde
# seçilir; bu üç seçenekten, indirilen geçerli poster sayısını en az kayıpla
# tam bir ızgaraya sığdıran seçilir (ör. 19 poster → 4 satır x 4 sütun = 16
# değil, 3 satır x 6 sütun = 18 kullanılır, yalnızca 1 poster elenir).
# 5 ve altı poster için tek satır halinde yan yana dizilir (ör. 1x5).
# Hesaplama _compute_grid_layout() içinde yapılır.
_COLLAGE_MAX_POSTERS = 36
# 5 ve altında poster varsa ızgara yerine tek satır (yan yana) kullanılır.
_COLLAGE_SINGLE_ROW_THRESHOLD = 5
# Tek bir posterin kolajdaki VARSAYILAN (en büyük) hedef boyutu (px).
# Poster sayısı arttıkça, kolaj boyutu _COLLAGE_MAX_W x _COLLAGE_MAX_H'yi
# geçmeyecek şekilde bu boyuttan küçültülür (bkz. _compute_thumb_size()).
_COLLAGE_THUMB_SIZE = (200, 300)
_COLLAGE_PADDING = 8
_COLLAGE_BG_COLOR = (18, 18, 22)
# Kolaj görselinin asla geçemeyeceği maksimum toplam boyut.
_COLLAGE_MAX_W = 1920
_COLLAGE_MAX_H = 1080
# Poster küçültülürken inilebilecek en küçük boyut (okunabilirlik için).
_COLLAGE_MIN_THUMB_SIZE = (40, 60)

# ─── Poster kolajı — köşe yuvarlama, gölge ve dinamik arkaplan ─────────────
# Köşe yarıçapı, posterin küçük kenarının (min(genişlik, yükseklik)) bu
# oranı kadar olur; çok küçük/çok büyük poster boyutlarında abartılı
# görünmemesi için min/max ile sınırlanır.
_COLLAGE_CORNER_RADIUS_RATIO = 0.07
_COLLAGE_CORNER_RADIUS_MIN = 4
_COLLAGE_CORNER_RADIUS_MAX = 18
# Gölgenin bulanıklık (blur) yarıçapı da posterin küçük kenarına oranlanır.
_COLLAGE_SHADOW_BLUR_RATIO = 0.05
_COLLAGE_SHADOW_BLUR_MIN = 4
# Gölgenin opaklığı (0-255) ve poster'a göre kayma miktarı.
_COLLAGE_SHADOW_OPACITY = 110
# Baskın renklerden üretilen gradyan arkaplanın, posterler öne çıksın diye
# ne kadar karartılacağı (0 = tamamen siyah, 1 = orijinal renk).
_COLLAGE_GRADIENT_DARKEN = 0.30

# ─── Bildirim saati ayarı ─────────────────────────────────────────────────────
# Saati değiştirmek için bu iki sabiti düzenleyin (UTC+3 / Türkiye saati).
# Örnek: sabah 08:30 → NOTIFY_HOUR = 8, NOTIFY_MINUTE = 30
NOTIFY_HOUR   = 0   # Saat (0-23)
NOTIFY_MINUTE = 5   # Dakika (0-59)
# ─────────────────────────────────────────────────────────────────────────────

# 25'ten fazla toplam içerik varsa mesaj yerine .txt dosyası gönderilir.
_TXT_THRESHOLD = 25

_content_notify_timer: threading.Timer | None = None
_running = False
_main_loop: asyncio.AbstractEventLoop | None = None

# Türkçe ay isimleri
_TR_MONTHS = [
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"
]


def _yesterday_label() -> str:
    """UTC+3 ile bir önceki günün Türkçe tarihini döner. Örn: '8 Mayıs 2026'"""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    tz        = ZoneInfo("Europe/Istanbul")
    yesterday = datetime.now(tz) - timedelta(days=1)
    return f"{yesterday.day} {_TR_MONTHS[yesterday.month]} {yesterday.year}"


def _get_platform_for(imdb_id) -> str:
    """
    Verilen imdb_id'nin ait olduğu platformu platform_catalog üzerinden döner.
    Bulunamazsa None.
    """
    if not imdb_id:
        return None
    try:
        from Backend.helper.platform_catalog import platform_catalog, PLATFORM_LABELS
        with platform_catalog._lock:
            for platform_key, id_set in platform_catalog._catalog.items():
                if imdb_id in id_set:
                    return PLATFORM_LABELS.get(platform_key, platform_key.capitalize())
    except Exception:
        pass
    return None


# ─── Zamanlama yardımcıları ───────────────────────────────────────────────────

def _seconds_until_notify_time() -> float:
    """UTC+3 bir sonraki NOTIFY_HOUR:NOTIFY_MINUTE'e kaç saniye kaldığını döner."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Istanbul")
    now = datetime.now(tz)

    # Bugün için hedef zamanı hesapla
    target = now.replace(hour=NOTIFY_HOUR, minute=NOTIFY_MINUTE, second=0, microsecond=0)

    # Eğer hedef zaman geçmişse yarına planla
    if target <= now:
        target += timedelta(days=1)

    return (target - now).total_seconds()


# ─── Son 24 saatte eklenen içerikleri getir ───────────────────────────────────

async def _get_new_content(db) -> dict:
    """
    Tüm storage_* veritabanlarını tarayarak son 24 saatte
    updated_on alanı güncellenen film ve dizi belgelerini döner.

    Dönüş:
        {
            "movies": [ {title, poster, rating, release_year, ...}, ... ],
            "tv":     [ {title, poster, rating, release_year, ...}, ... ],
        }
    """
    cutoff = datetime.utcnow() - timedelta(hours=24)
    query = {"updated_on": {"$gte": cutoff}}
    projection = {
        "title": 1,
        "title_tr": 1,
        "poster": 1,
        "poster_tr": 1,
        "poster_de": 1,
        "rating": 1,
        "release_year": 1,
        "genres_tr": 1,
        "genres": 1,
        "updated_on": 1,
        "tmdb_id": 1,
        "imdb_id": 1,
        "media_type": 1,
    }

    movies: list[dict] = []
    tv_shows: list[dict] = []

    # Multi-db: storage_1, storage_2, ...
    for i in range(1, db.current_db_index + 1):
        db_key = f"storage_{i}"
        if db_key not in db.dbs:
            continue
        storage = db.dbs[db_key]

        # Film koleksiyonu
        try:
            movie_cursor = storage["movie"].find(query, projection).sort("updated_on", -1)
            async for doc in movie_cursor:
                doc.pop("_id", None)
                movies.append(doc)
        except Exception as e:
            logger.warning("[content-notify] storage_%d movie sorgusu hatası: %s", i, e)

        # Dizi koleksiyonu
        try:
            tv_cursor = storage["tv"].find(query, {
                "title": 1, "title_tr": 1, "poster": 1, "poster_tr": 1, "poster_de": 1, "rating": 1,
                "release_year": 1, "genres_tr": 1, "genres": 1,
                "updated_on": 1, "tmdb_id": 1, "imdb_id": 1, "media_type": 1,
            }).sort("updated_on", -1)
            async for doc in tv_cursor:
                doc.pop("_id", None)
                tv_shows.append(doc)
        except Exception as e:
            logger.warning("[content-notify] storage_%d tv sorgusu hatası: %s", i, e)

    # Aynı içerik birden fazla DB'de olabilir — imdb_id/tmdb_id bazlı deduplikasyon
    movies   = _dedup(movies)
    tv_shows = _dedup(tv_shows)

    return {"movies": movies, "tv": tv_shows}


def _dedup(items: list[dict]) -> list[dict]:
    """imdb_id veya tmdb_id bazında tekrarlananları kaldırır."""
    seen: set = set()
    result: list[dict] = []
    for item in items:
        key = item.get("imdb_id") or item.get("tmdb_id") or item.get("title", "")
        if key and key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


# ─── Poster kolajı ────────────────────────────────────────────────────────────

# Bazı CDN'ler (TMDB dahil) User-Agent'siz veya yönlendirmesiz (redirect)
# isteklerde 403/404 dönebiliyor; bu header ve follow_redirects, poster
# indirmelerinin sessizce başarısız olmasını önlemek için eklendi.
_POSTER_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


async def _download_poster(client, url: str, retries: int = 1):
    """
    Bir poster URL'sini indirir, başarısız olursa None döner.
    Geçici hatalara (timeout, bağlantı hatası vb.) karşı `retries` kadar
    tekrar dener. Başarısızlık nedeni her zaman WARNING seviyesinde
    loglanır (eskiden DEBUG idi ve varsayılan log seviyesinde görünmüyordu,
    bu da eksik posterlerin fark edilmesini zorlaştırıyordu).
    """
    if not url:
        return None
    attempts = retries + 1
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.get(url, timeout=15.0)
            resp.raise_for_status()
            if not resp.content:
                raise ValueError("boş içerik döndü")
            return resp.content
        except Exception as e:
            last_err = e
            if attempt < attempts:
                await asyncio.sleep(0.5)
    logger.warning(
        "[content-notify] Poster indirilemedi (%s) — %d deneme sonrası: %s",
        url, attempts, last_err,
    )
    return None


async def _download_poster_with_fallback(client, item: dict):
    """
    Bir içerik için poster_tr → poster → poster_de sırasıyla indirmeyi dener.
    İlk başarılı indirilen görsel kullanılır. Hiçbir alan indirilemezse
    (veya hiçbiri tanımlı değilse) None döner ve bu içerik kolaja eklenmez.
    """
    for url in (item.get("poster_tr"), item.get("poster"), item.get("poster_de")):
        if not url:
            continue
        data = await _download_poster(client, url)
        if data:
            return data
    return None


_COLLAGE_ALLOWED_ROWS = (2, 3, 4)


def _compute_grid_layout(n: int) -> tuple[int, int, int]:
    """
    İndirilen geçerli poster sayısı (n) için ızgara düzenini belirler.

    Satır sayısı (dikey) her zaman 2, 3 ya da 4'ten biri olmalıdır.
    Bu üç seçenek arasından, n'i tam bölen (kalansız) ve en az poster
    kaybına yol açan satır sayısı seçilir; eşitlik durumunda ızgarayı
    en kareye yakın yapan (satır/sütun farkı en küçük olan) seçilir.

    Dönüş: (rows, cols, used_n) — used_n, kolajda gerçekten kullanılacak
    poster sayısıdır (n <= used_n değildir, used_n <= n).

    Örnek: 20 poster → 4x5 (20 kullanılır, kayıp yok)
           19 poster → 3x6 (18 kullanılır, yalnızca 1 poster elenir)
            5 poster → 1x5 (tek satır, yan yana, kayıp yok)
            1 poster → (1, 1, 1) — ızgara kurulmaz.
    """
    n = min(n, _COLLAGE_MAX_POSTERS)
    if n <= 0:
        return (1, 0, 0)

    # 5 ve altı poster: ızgara kurmak yerine tek satır halinde yan yana diz.
    if n <= _COLLAGE_SINGLE_ROW_THRESHOLD:
        return (1, n, n)

    best = None  # (used_n, -|rows-cols|, rows, cols)
    for rows in _COLLAGE_ALLOWED_ROWS:
        if rows > n:
            continue
        used = (n // rows) * rows
        if used == 0:
            continue
        cols = used // rows
        score = (used, -abs(rows - cols))
        if best is None or score > best[0]:
            best = (score, rows, cols)

    if best is None:
        # n, 2/3/4'ten hiçbirine bölünemiyor (n < 2 durumunda buraya
        # düşülmez, ama güvenlik amacıyla tek satır olarak döndür).
        return (1, n, n)

    _, rows, cols = best
    used = rows * cols
    return (rows, cols, used)


def _compute_thumb_size(rows: int, cols: int) -> tuple[int, int]:
    """
    Verilen satır/sütun sayısı için tek bir posterin kolajdaki boyutunu
    belirler. Poster sayısı arttıkça (dolayısıyla rows/cols büyüdükçe)
    poster boyutu küçültülür; böylece kolajın toplam boyutu her zaman
    _COLLAGE_MAX_W x _COLLAGE_MAX_H sınırının içinde kalır.

    Oranlar her zaman _COLLAGE_THUMB_SIZE'ın en-boy oranı (2:3) korunarak
    küçültülür; kolaj küçük olduğunda (az poster) varsayılan boyut
    (_COLLAGE_THUMB_SIZE) büyütülmeden aynen kullanılır.
    """
    default_w, default_h = _COLLAGE_THUMB_SIZE
    pad = _COLLAGE_PADDING
    rows = max(rows, 1)
    cols = max(cols, 1)

    avail_w = _COLLAGE_MAX_W - (cols + 1) * pad
    avail_h = _COLLAGE_MAX_H - (rows + 1) * pad

    max_w_by_cols = avail_w / cols
    max_h_by_rows = avail_h / rows

    # Varsayılan en-boy oranını koruyarak, hem genişlik hem yükseklik
    # sınırına uyan en büyük ölçeği bul (asla varsayılandan büyütme).
    scale = min(max_w_by_cols / default_w, max_h_by_rows / default_h, 1.0)
    scale = max(scale, 0.01)

    thumb_w = max(int(default_w * scale), _COLLAGE_MIN_THUMB_SIZE[0])
    thumb_h = max(int(default_h * scale), _COLLAGE_MIN_THUMB_SIZE[1])
    return thumb_w, thumb_h


def _get_dominant_color(img) -> tuple[int, int, int]:
    """
    Görselin baskın/ortalama rengini hızlıca hesaplar.

    Görseli 1x1 piksele küçültmek, PIL'in dahili box-filter'ı sayesinde
    aslında bir "ortalama renk" hesabı yapar — bu, K-means gibi ağır bir
    bağımlılık gerektirmeden posterin genel tonunu (ör. koyu/kırmızımsı,
    açık/mavimsi) yeterince iyi yakalar.
    """
    try:
        small = img.convert("RGB").resize((1, 1))
        return small.getpixel((0, 0))
    except Exception:
        return _COLLAGE_BG_COLOR


def _mix_colors(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """İki renk arasında t (0.0-1.0) oranında ara renk hesaplar."""
    t = max(0.0, min(1.0, t))
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _darken_color(c: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Rengi siyaha doğru karartır (posterler kolajda öne çıksın, arkaplan
    dikkat dağıtıcı olmasın diye)."""
    return tuple(int(v * factor) for v in c)


def _build_gradient_background(width: int, height: int, images: list):
    """
    Kolajdaki posterlerin baskın renklerinden, yukarıdan aşağıya yumuşak
    geçişli koyu tonlu bir arkaplan gradyanı üretir (Spotify Wrapped /
    Apple Music tarzı bir görünüm). Poster listesi boşsa eski sabit renge
    (_COLLAGE_BG_COLOR) geri döner.
    """
    from PIL import Image

    if not images:
        return Image.new("RGB", (width, height), _COLLAGE_BG_COLOR)

    # Tüm posterlerin ortalamasını almak yerine, aralarına eşit aralıklarla
    # yayılmış birkaç örnek (en fazla 6) kullanmak hem hızlı hem de kolajın
    # genelini temsil eden bir gradyan verir.
    step = max(1, len(images) // 6)
    sample = images[::step][:6] or images[:1]
    dominant_colors = [_get_dominant_color(im) for im in sample]

    top_color = _darken_color(dominant_colors[0], _COLLAGE_GRADIENT_DARKEN)
    bottom_color = _darken_color(dominant_colors[-1], _COLLAGE_GRADIENT_DARKEN)

    gradient = Image.new("RGB", (1, height))
    for y in range(height):
        gradient.putpixel((0, y), _mix_colors(top_color, bottom_color, y / max(height - 1, 1)))
    return gradient.resize((width, height))


def _apply_rounded_corners(img, radius: int):
    """Görsele yuvarlatılmış köşe maskesi uygular, RGBA olarak döner."""
    from PIL import Image, ImageDraw

    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (img.width - 1, img.height - 1)], radius=radius, fill=255
    )
    img.putalpha(mask)
    return img


def _make_poster_shadow(size: tuple[int, int], radius: int, blur: int, opacity: int):
    """
    Bir posterin arkasına konacak, yumuşak kenarlı, hafif bulanıklaştırılmış
    bir gölge katmanı üretir. Dönüş: (gölge_görseli, gölgenin her yöne
    taşan kenar payı) — bu pay, gölge kolaj tuvaline yerleştirilirken
    posterin konumundan çıkarılmalıdır.
    """
    from PIL import Image, ImageDraw, ImageFilter

    w, h = size
    pad = blur * 2
    shadow = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [(pad, pad), (pad + w - 1, pad + h - 1)], radius=radius, fill=(0, 0, 0, opacity)
    )
    return shadow.filter(ImageFilter.GaussianBlur(blur)), pad


async def _build_poster_collage(movies: list[dict], tv_shows: list[dict]):
    """
    Eklenen film/dizi posterlerinden bir kolaj görseli oluşturur.

    Filmler önce, sonra diziler olacak şekilde (alfabetik sıralı), en fazla
    _COLLAGE_MAX_POSTERS adet poster; satır sayısı 2, 3 ya da 4 olacak
    şekilde ızgara halinde birleştirilir (ör. 20 → 4x5, 24 → 4x6, 30 → 3x10).

    Dönüş: JPEG bytes, ya da hiç poster indirilemezse None.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("[content-notify] Pillow kurulu değil, kolaj oluşturulamıyor.")
        return None

    import httpx

    ordered = _sort_alphabetically(movies) + _sort_alphabetically(tv_shows)
    # En az bir poster alanı (poster_tr / poster / poster_de) tanımlı olan içerikler.
    candidates = [
        it for it in ordered
        if it.get("poster_tr") or it.get("poster") or it.get("poster_de")
    ]
    candidates = candidates[:_COLLAGE_MAX_POSTERS]

    if not candidates:
        logger.warning(
            "[content-notify] Kolaj için hiçbir içerikte poster alanı (poster_tr/poster/poster_de) yok "
            "— %d film, %d dizi arasında.", len(movies), len(tv_shows),
        )
        return None

    if len(candidates) < len(movies) + len(tv_shows):
        logger.warning(
            "[content-notify] %d içerikten %d tanesinde poster alanı yok, kolaja dahil edilmeyecek.",
            len(movies) + len(tv_shows), len(movies) + len(tv_shows) - len(candidates),
        )

    async with httpx.AsyncClient(headers=_POSTER_DOWNLOAD_HEADERS, follow_redirects=True) as client:
        # Her içerik için önce poster_tr, o başarısız olursa poster,
        # o da başarısız olursa poster_de denenir. Hiçbiri indirilemezse
        # ilgili içerik kolaja dahil edilmez.
        results = await asyncio.gather(
            *[_download_poster_with_fallback(client, it) for it in candidates]
        )

    raw_images = []
    for it, raw in zip(candidates, results):
        if not raw:
            continue
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            raw_images.append(img)
        except Exception as e:
            logger.warning(
                "[content-notify] Poster açılamadı (%s): %s",
                it.get("title_tr") or it.get("title"), e,
            )

    if not raw_images:
        logger.warning("[content-notify] %d adaydan hiçbiri indirilemedi, kolaj oluşturulamadı.", len(candidates))
        return None

    if len(raw_images) < len(candidates):
        logger.warning(
            "[content-notify] Kolaj: %d adaydan yalnızca %d poster indirilebildi.",
            len(candidates), len(raw_images),
        )

    # Satır sayısını (2, 3 ya da 4; 5 ve altı için tek satır) ve buna göre
    # kullanılacak nihai poster sayısını belirle — en az poster kaybıyla
    # tam bir ızgaraya sığdır.
    rows, cols, used_n = _compute_grid_layout(len(raw_images))
    if used_n < len(raw_images):
        raw_images = raw_images[:used_n]

    # Poster sayısı arttıkça (rows/cols büyüdükçe) kolaj 1920x1080'i
    # geçmeyecek şekilde tek bir posterin boyutu küçültülür.
    thumb_w, thumb_h = _compute_thumb_size(rows, cols)
    images = [img.resize((thumb_w, thumb_h)) for img in raw_images]

    pad = _COLLAGE_PADDING
    canvas_w = cols * thumb_w + (cols + 1) * pad
    canvas_h = rows * thumb_h + (rows + 1) * pad

    # Arkaplan: sabit koyu renk yerine, posterlerin baskın renklerinden
    # üretilen yumuşak bir gradyan (bkz. _build_gradient_background).
    canvas = _build_gradient_background(canvas_w, canvas_h, raw_images).convert("RGBA")

    # Poster boyutuna göre ölçeklenen köşe yarıçapı ve gölge bulanıklığı —
    # çok küçük thumbnail'larda (çok sayıda poster varken) köşeler/gölgeler
    # abartılı görünmesin diye min/max ile sınırlanır.
    min_side = min(thumb_w, thumb_h)
    corner_radius = max(_COLLAGE_CORNER_RADIUS_MIN,
                         min(_COLLAGE_CORNER_RADIUS_MAX, int(min_side * _COLLAGE_CORNER_RADIUS_RATIO)))
    shadow_blur = max(_COLLAGE_SHADOW_BLUR_MIN, int(min_side * _COLLAGE_SHADOW_BLUR_RATIO))
    shadow_offset = max(2, shadow_blur // 2)

    for idx, img in enumerate(images):
        row, col = divmod(idx, cols)
        x = pad + col * (thumb_w + pad)
        y = pad + row * (thumb_h + pad)

        # Önce gölgeyi (hafifçe sağa/aşağı kaymış) yerleştir, sonra
        # yuvarlatılmış köşeli posteri üzerine bindir.
        shadow, shadow_pad = _make_poster_shadow((thumb_w, thumb_h), corner_radius, shadow_blur, _COLLAGE_SHADOW_OPACITY)
        canvas.alpha_composite(shadow, (x - shadow_pad + shadow_offset, y - shadow_pad + shadow_offset))

        rounded_img = _apply_rounded_corners(img, corner_radius)
        canvas.alpha_composite(rounded_img, (x, y))

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="JPEG", quality=88)
    buf.seek(0)
    return buf.getvalue()


# ─── Poster kolajını gruba/konuya gönderme ────────────────────────────────────
# Ayarlar sayfasındaki "Yeni İçerik Duyuruları" bölümündeki aynı hedef
# (announce_new_content + announcement_channel) kullanılır — content_announcer.py
# ile aynı kanal/grup/konu ayarı paylaşılır.

async def _send_collage_to_group(collage_bytes: bytes, movies: list[dict], tv_shows: list[dict], date_label: str) -> None:
    """Kolaj görselini, ayarlarda tanımlı duyuru kanalına/grubuna/konusuna gönderir."""
    if not collage_bytes:
        return

    try:
        from pyrogram.enums import ParseMode
        from pyrogram.errors import FloodWait, TopicClosed

        from Backend.helper.settings_manager import SettingsManager
        from Backend.helper.content_announcer import _parse_target
        from Backend.pyrofork.bot import StreamBot

        settings = SettingsManager.current()
        if not getattr(settings, "announce_new_content", False):
            return

        chat, thread_id = _parse_target(getattr(settings, "announcement_channel", ""))
        if chat is None:
            return

        app_name = getattr(settings, "isim", "") or ""
        title_suffix = f" {app_name}'e" if app_name else ""
        caption = f"🎬 <b>{date_label}{title_suffix} Eklenenler</b>"

        async def _do_send():
            buf = io.BytesIO(collage_bytes)
            buf.name = "eklenenler_kolaj.jpg"
            return await StreamBot.send_photo(
                chat_id=chat,
                message_thread_id=thread_id,
                photo=buf,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )

        try:
            await _do_send()
        except TopicClosed:
            # Hedef konu kapalıysa geçici olarak aç, gönder, sonra tekrar kapat
            # (content_announcer.py ile aynı davranış).
            if thread_id is None:
                logger.error("[content-notify] Kolaj gönderilemedi: hedef konu kapalı (TOPIC_CLOSED).")
                return
            try:
                await StreamBot.reopen_forum_topic(chat, thread_id)
            except Exception as e:
                logger.error("[content-notify] Kolaj gönderilemedi, konu açılamadı: %s", e)
                return
            try:
                await _do_send()
            except FloodWait as e:
                await asyncio.sleep(max(e.value, 1))
                try:
                    await _do_send()
                except Exception as e2:
                    logger.error("[content-notify] Kolaj gruba gönderilemedi (retry): %s", e2)
            except Exception as e:
                logger.error("[content-notify] Konu geçici açıldı ama kolaj gönderilemedi: %s", e)
            finally:
                try:
                    await StreamBot.close_forum_topic(chat, thread_id)
                except Exception as e:
                    logger.warning("[content-notify] Kolaj sonrası konu tekrar kapatılamadı: %s", e)
        except FloodWait as e:
            await asyncio.sleep(max(e.value, 1))
            try:
                await _do_send()
            except Exception as e2:
                logger.error("[content-notify] Kolaj gruba gönderilemedi (retry): %s", e2)
        except Exception as e:
            logger.error("[content-notify] Kolaj gruba gönderilemedi: %s", e)

    except Exception as e:
        logger.exception("[content-notify] Kolaj grup gönderimi genel hata: %s", e)


# ─── Dizi bölüm/sezon özeti ───────────────────────────────────────────────────

def _format_tv_episodes(seasons: list[dict]) -> str:
    """
    Bir dizinin sezon/bölüm listesini okunabilir metne çevirir.

    Kural:
      - Bir sezonda 4'ten fazla bölüm varsa → "X. Sezon eklendi"
      - 4 veya daha az bölümse      → "S01E01, S01E02, ..." şeklinde listeler
    """
    if not seasons:
        return ""

    parts: list[str] = []

    for season in sorted(seasons, key=lambda s: s.get("season_number", 0)):
        season_num = season.get("season_number", 0)
        episodes   = season.get("episodes", [])

        if not episodes:
            continue

        if len(episodes) > 4:
            # Çok bölüm → sezon olarak özetle
            parts.append(f"{season_num}. Sezon eklendi")
        else:
            # Az bölüm → tek tek listele
            ep_tags = [
                f"S{season_num:02d}E{ep.get('episode_number', 0):02d}"
                for ep in sorted(episodes, key=lambda e: e.get("episode_number", 0))
            ]
            parts.append(", ".join(ep_tags))

    return " | ".join(parts) if parts else ""


# ─── Mesaj formatı ────────────────────────────────────────────────────────────

# 25'ten fazla toplam içerik varsa mesaj yerine .txt dosyası gönderilir.
_TXT_THRESHOLD = 25


def _sort_alphabetically(items: list[dict]) -> list[dict]:
    """İçerikleri başlığa göre alfabetik olarak sıralar (Türkçe karakterlere duyarlı)."""
    import locale
    try:
        locale.setlocale(locale.LC_COLLATE, "tr_TR.UTF-8")
        return sorted(items, key=lambda x: locale.strxfrm(
            (x.get("title_tr") or x.get("title") or "").lower()
        ))
    except locale.Error:
        return sorted(items, key=lambda x: (
            (x.get("title_tr") or x.get("title") or "").lower()
        ))


def _build_content_lines(movies: list[dict], tv_shows: list[dict], service_name: str, date_label: str) -> list[str]:
    """Film ve dizi listesini düz metin satırlarına çevirir (HTML tag'siz, alfabetik sıralı)."""
    title_suffix = f" {service_name}'e" if service_name else ""
    lines: list[str] = [f"{date_label}{title_suffix} Eklenenler", ""]

    if movies:
        sorted_movies = _sort_alphabetically(movies)
        lines.append(f"🎥 Filmler ({len(sorted_movies)})")
        for m in sorted_movies:
            title     = m.get("title_tr") or m.get("title", "—")
            year      = m.get("release_year", "")
            rating    = m.get("rating")
            genres    = m.get("genres_tr") or m.get("genres") or []
            genre_str = ", ".join(genres[:2]) if genres else ""

            entry = f"• {title}"
            if year:
                entry += f" ({year})"
            if rating:
                entry += f" ⭐ {rating:.1f}"
            if genre_str:
                entry += f" — {genre_str}"
            lines.append(entry)
        lines.append("")

    if tv_shows:
        sorted_tv = _sort_alphabetically(tv_shows)
        lines.append(f"📺 Diziler ({len(sorted_tv)})")
        for t in sorted_tv:
            title     = t.get("title_tr") or t.get("title", "—")
            year      = t.get("release_year", "")
            rating    = t.get("rating")
            genres    = t.get("genres_tr") or t.get("genres") or []
            genre_str = ", ".join(genres[:2]) if genres else ""
            platform  = _get_platform_for(t.get("imdb_id"))
            entry = f"• {title}"
            if year:
                entry += f" ({year})"
            if rating:
                entry += f" ⭐ {rating:.1f}"
            if platform:
                entry += f" [{platform}]"
            if genre_str:
                entry += f" — {genre_str}"
            lines.append(entry)
        lines.append("")

    lines.append("🍿 İyi seyirler")
    return lines


def _format_notification_html(movies: list[dict], tv_shows: list[dict], service_name: str, date_label: str) -> str:
    """
    25 veya daha az toplam içerik için Telegram HTML mesajı oluşturur.
    Toplam içerik yoksa boş string döner.
    """
    if not movies and not tv_shows:
        return ""

    lines: list[str] = []
    title_suffix = f" {service_name}'e" if service_name else ""
    lines.append(f"<b>{date_label}{title_suffix} Eklenenler</b>\n")

    if movies:
        lines.append(f"🎥 <b>Filmler</b> ({len(movies)})")
        for m in _sort_alphabetically(movies):
            title     = m.get("title_tr") or m.get("title", "—")
            year      = m.get("release_year", "")
            rating    = m.get("rating")
            genres    = m.get("genres_tr") or m.get("genres") or []
            genre_str = ", ".join(genres[:2]) if genres else ""

            entry = f"• <b>{title}</b>"
            if year:
                entry += f" ({year})"
            if rating:
                entry += f" ⭐ {rating:.1f}"
            if genre_str:
                entry += f" — <i>{genre_str}</i>"
            lines.append(entry)
        lines.append("")

    if tv_shows:
        lines.append(f"📺 <b>Diziler</b> ({len(tv_shows)})")
        for t in _sort_alphabetically(tv_shows):
            title     = t.get("title_tr") or t.get("title", "—")
            year      = t.get("release_year", "")
            rating    = t.get("rating")
            genres    = t.get("genres_tr") or t.get("genres") or []
            genre_str = ", ".join(genres[:2]) if genres else ""
            platform  = _get_platform_for(t.get("imdb_id"))
            entry = f"• <b>{title}</b>"
            if year:
                entry += f" ({year})"
            if rating:
                entry += f" ⭐ {rating:.1f}"
            if platform:
                entry += f" [<i>{platform}</i>]"
            if genre_str:
                entry += f" — <i>{genre_str}</i>"
            lines.append(entry)
        lines.append("")

    lines.append("🍿 İyi seyirler")
    return "\n".join(lines)


def _build_txt_bytes(movies: list[dict], tv_shows: list[dict], service_name: str, date_label: str) -> bytes:
    """15'ten fazla içerik için .txt dosyası içeriğini oluşturur."""
    header = []
    content_lines = _build_content_lines(movies, tv_shows, service_name, date_label)
    full_text = "\n".join(header + content_lines)
    return full_text.encode("utf-8")


# ─── Gönderim çekirdeği ───────────────────────────────────────────────────────

async def _send_daily_content_notifications() -> None:
    """
    Son 24 saatte eklenen içerikleri tüm kullanıcılara gönderir.
    15'ten fazla içerik varsa mesaj yerine .txt dosyası olarak iletir.
    """
    try:
        from Backend import db
        from Backend.pyrofork.bot import StreamBot
        from Backend.config import Telegram
        from pyrogram.enums import ParseMode
        from pyrogram.errors import (
            FloodWait,
            UserIsBlocked,
            InputUserDeactivated,
            PeerIdInvalid,
        )

        logger.info("[content-notify] Bildirim görevi başladı.")

        # ── 1. Yeni içerikleri getir ──────────────────────────────────────
        content  = await _get_new_content(db)
        movies   = content["movies"]
        tv_shows = content["tv"]

        total_content = len(movies) + len(tv_shows)
        logger.info(
            "[content-notify] Son 24 saatte: %d film, %d dizi bulundu.",
            len(movies), len(tv_shows),
        )

        if total_content == 0:
            logger.info("[content-notify] Yeni içerik yok, bildirim gönderilmeyecek.")
            if owner_id := Telegram.OWNER_ID:
                try:
                    await StreamBot.send_message(
                        chat_id=owner_id,
                        text="ℹ️ Bugün sisteme yeni içerik eklenmedi.",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.warning("[content-notify] Owner boş-içerik bildirimi gönderilemedi: %s", e)
            return

        # ── 2. Tarih etiketi ve mesaj mı, txt mi? ────────────────────────
        date_label  = _yesterday_label()
        use_txt     = total_content > _TXT_THRESHOLD

        # ── 2b. Poster kolajı ──────────────────────────────────────────────
        # Kolaj yalnızca ayarlardaki "Yeni İçerik Duyuruları" hedefine
        # (kanal/grup/konu) gönderilir; üyelere (bireysel kullanıcılara)
        # gönderilmez — üyeler yalnızca metin/txt bildirimini alır.
        collage_bytes = await _build_poster_collage(movies, tv_shows)
        await _send_collage_to_group(collage_bytes, movies, tv_shows, date_label)

        if use_txt:
            txt_bytes   = _build_txt_bytes(movies, tv_shows, Telegram.ISIM, date_label)
            txt_title_suffix = f" {Telegram.ISIM}'e" if Telegram.ISIM else ""
            txt_caption = (
                f"<b>{date_label}{txt_title_suffix} Eklenenler</b>\n"
                f"<i>{len(movies)} film, {len(tv_shows)} dizi eklendi.</i>\n"
                f"📄 Tam liste ekte."
            )
            logger.info("[content-notify] 25+ içerik — .txt dosyası olarak gönderilecek.")
        else:
            message_text = _format_notification_html(movies, tv_shows, Telegram.ISIM, date_label)
            if not message_text:
                logger.info("[content-notify] Mesaj oluşturulamadı, atlanıyor.")
                return

        # ── 3. Tüm kullanıcıları getir ────────────────────────────────────
        all_users   = await db.get_all_users()
        total_users = len(all_users)
        logger.info("[content-notify] %d kullanıcıya bildirim gönderilecek.", total_users)

        sent       = 0
        blocked    = 0
        failed     = 0
        batch_sent = 0          # batch hız kontrolü için
        start_time = datetime.utcnow()

        sent_users    = []   # {"id": ..., "name": ..., "username": ...}
        blocked_users = []
        failed_users  = []

        for user in all_users:
            uid = user.get("_id") or user.get("user_id")
            if not uid:
                failed += 1
                failed_users.append({"id": "?", "name": "?", "username": None})
                continue

            uid_int  = int(uid)
            name     = " ".join(filter(None, [
                user.get("first_name", ""),
                user.get("last_name", ""),
            ])).strip() or "—"
            username = user.get("username") or None

            async def _send(uid_int=uid_int):
                # Not: Poster kolajı üyelere gönderilmez — yalnızca yukarıda
                # _send_collage_to_group() ile duyuru hedefine (kanal/grup/
                # konu) gönderildi. Üyeler yalnızca metin/txt bildirimini alır.
                if use_txt:
                    await StreamBot.send_document(
                        chat_id=uid_int,
                        document=io.BytesIO(txt_bytes),
                        file_name="gunluk_icerik.txt",
                        caption=txt_caption,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await StreamBot.send_message(
                        chat_id=uid_int,
                        text=message_text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )

            _user_info = {"id": uid_int, "name": name, "username": username}

            try:
                await _send()
                sent += 1
                sent_users.append(_user_info)

            except FloodWait as e:
                wait = max(e.value, 1)
                logger.warning("[content-notify] FloodWait: %d sn bekleniyor.", wait)
                await asyncio.sleep(wait)
                try:
                    await _send()
                    sent += 1
                    sent_users.append(_user_info)
                except Exception as retry_err:
                    logger.warning(
                        "[content-notify] Kullanıcı %d retry hatası: %s", uid_int, retry_err
                    )
                    failed += 1
                    failed_users.append(_user_info)

            except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
                blocked += 1
                blocked_users.append(_user_info)

            except OSError as e:
                logger.warning(
                    "[content-notify] Kullanıcı %d ağ/timeout hatası: %s — 5 sn sonra tekrar deneniyor.", uid_int, e
                )
                await asyncio.sleep(5)
                try:
                    await _send()
                    sent += 1
                    sent_users.append(_user_info)
                except Exception as retry_err:
                    logger.warning(
                        "[content-notify] Kullanıcı %d ağ retry hatası: %s", uid_int, retry_err
                    )
                    failed += 1
                    failed_users.append(_user_info)

            except Exception as e:
                logger.warning(
                    "[content-notify] Kullanıcı %d gönderilemedi: %s", uid_int, e
                )
                failed += 1
                failed_users.append(_user_info)

            # Telegram flood koruması — her mesaj sonrası kısa bekleme
            await asyncio.sleep(0.05)

            # Batch hız kontrolü — her 25 başarılı gönderimde 2 sn dinlen
            batch_sent += 1
            if batch_sent % 25 == 0:
                logger.debug("[content-notify] 25 mesaj gönderildi, 2 sn bekleniyor.")
                await asyncio.sleep(2)

        # ── 4. Owner'a özet rapor + kullanıcı detay txt gönder ───────────
        elapsed_sec = int((datetime.utcnow() - start_time).total_seconds())
        elapsed_str = f"{elapsed_sec // 60} dk {elapsed_sec % 60} sn"

        owner_id = Telegram.OWNER_ID
        if owner_id:
            summary = (
                f"📊 <b>Günlük İçerik Bildirimi Raporu</b>\n\n"
                f"🎥 Yeni film: <b>{len(movies)}</b>\n"
                f"📺 Yeni dizi: <b>{len(tv_shows)}</b>\n\n"
                f"👥 Toplam kullanıcı: <b>{total_users}</b>\n"
                f"✅ Gönderildi: <b>{sent}</b>\n"
                f"🚫 Engelledi/Çıktı: <b>{blocked}</b>\n"
                f"❌ Başarısız: <b>{failed}</b>\n"
                f"⏱ Süre: <b>{elapsed_str}</b>"
            )

            def _user_line(u: dict) -> str:
                line = f"  ID: {u['id']} | Ad: {u['name']}"
                if u.get("username"):
                    line += f" | @{u['username']}"
                return line

            report_lines = [
                f"📊 Günlük İçerik Bildirimi Raporu",
                f"Tarih: {date_label}",
                f"",
                f"🎥 Yeni film: {len(movies)}",
                f"📺 Yeni dizi: {len(tv_shows)}",
                f"",
                f"👥 Toplam kullanıcı: {total_users}",
                f"✅ Gönderildi: {sent}",
                f"🚫 Engelledi/Çıktı: {blocked}",
                f"❌ Başarısız: {failed}",
                f"⏱ Süre: {elapsed_str}",
                f"",
                f"─" * 40,
            ]

            if sent_users:
                report_lines.append(f"\n✅ Gönderilen Kullanıcılar ({sent}):")
                for u in sent_users:
                    report_lines.append(_user_line(u))

            if blocked_users:
                report_lines.append(f"\n🚫 Engelleyen / Çıkan Kullanıcılar ({blocked}):")
                for u in blocked_users:
                    report_lines.append(_user_line(u))

            if failed_users:
                report_lines.append(f"\n❌ Başarısız Kullanıcılar ({failed}):")
                for u in failed_users:
                    report_lines.append(_user_line(u))

            report_txt = "\n".join(report_lines).encode("utf-8")

            try:
                await StreamBot.send_document(
                    chat_id=owner_id,
                    document=io.BytesIO(report_txt),
                    file_name=f"rapor_{datetime.utcnow().strftime('%Y%m%d')}.txt",
                    caption=summary,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning("[content-notify] Owner raporu gönderilemedi: %s", e)
                # Dosya gönderilemezse sadece mesaj olarak dene
                try:
                    await StreamBot.send_message(
                        chat_id=owner_id,
                        text=summary,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e2:
                    logger.warning("[content-notify] Owner mesajı da gönderilemedi: %s", e2)

        logger.info(
            "[content-notify] Tamamlandı. Gönderildi: %d, Engelledi: %d, Başarısız: %d, Süre: %s",
            sent, blocked, failed, elapsed_str,
        )

    except Exception as e:
        logger.exception("[content-notify] Genel hata: %s", e)


# ─── threading.Timer callback + zamanlayıcı ──────────────────────────────────

def _run_content_notify() -> None:
    """threading.Timer callback — async fonksiyonu ana loop'ta çalıştırır."""
    if not _running:
        return

    loop = _main_loop
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            _send_daily_content_notifications(), loop
        )
        try:
            future.result(timeout=600)  # maksimum 10 dakika bekle
        except Exception as e:
            logger.exception("[content-notify] run_coroutine_threadsafe hatası: %s", e)
    else:
        logger.warning("[content-notify] Ana loop bulunamadı, asyncio.run() ile çalışıyor.")
        asyncio.run(_send_daily_content_notifications())

    # Bir sonraki günü planla
    _schedule_next_content_notify()


def _schedule_next_content_notify() -> None:
    """UTC+3 NOTIFY_HOUR:NOTIFY_MINUTE'de tetiklenecek zamanlayıcıyı kurar."""
    global _content_notify_timer
    if not _running:
        return

    delay = _seconds_until_notify_time()
    logger.info(
        "[content-notify] Bir sonraki bildirim %.0f saniye sonra (UTC+3 %02d:%02d).",
        delay, NOTIFY_HOUR, NOTIFY_MINUTE,
    )
    _content_notify_timer = threading.Timer(delay, _run_content_notify)
    _content_notify_timer.daemon = True
    _content_notify_timer.name = "daily-content-notify"
    _content_notify_timer.start()


# ─── Public API ───────────────────────────────────────────────────────────────

def start_daily_content_notifier(main_loop: asyncio.AbstractEventLoop | None = None) -> None:
    """
    db_scheduler.start_scheduler() içinden çağrılır.

    Kullanım (db_scheduler.py → start_scheduler fonksiyonu):
        from Backend.helper.daily_content_notifier import start_daily_content_notifier
        start_daily_content_notifier(main_loop=_main_loop)
    """
    global _running, _main_loop
    _running   = True
    _main_loop = main_loop

    _schedule_next_content_notify()
    logger.info(
        "[content-notify] Günlük içerik bildirimi zamanlayıcısı başlatıldı (UTC+3 %02d:%02d).",
        NOTIFY_HOUR, NOTIFY_MINUTE,
    )


def stop_daily_content_notifier() -> None:
    """
    db_scheduler.stop_scheduler() içinden çağrılır.

    Kullanım (db_scheduler.py → stop_scheduler fonksiyonu):
        from Backend.helper.daily_content_notifier import stop_daily_content_notifier
        stop_daily_content_notifier()
    """
    global _running, _content_notify_timer
    _running = False
    if _content_notify_timer:
        _content_notify_timer.cancel()
        _content_notify_timer = None
    logger.info("[content-notify] Günlük içerik bildirimi zamanlayıcısı durduruldu.")
