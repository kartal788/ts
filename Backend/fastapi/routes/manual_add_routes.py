"""
/media/manage sayfasındaki "Manuel İçerik Ekle" paneli için API.

Panelden başlık/poster/açıklama (ve dizi ise sezon/bölüm) girilip mod
başlatılır; Backend.MANUAL_MODE global durumu bu bilgiyi tutar. Ardından admin
video dosyalarını AUTH_CHANNEL'a iletir. reciever.py bu global durum açıkken
gelen dosyaları TMDB/IMDb sorgusu yapmadan doğrudan bu başlık altında kataloğa
ekler (bkz. Backend/helper/metadata.py -> build_manual_metadata).

İki içerik türü desteklenir:
  - "movie": Kişisel video / tek parça içerik. İletilen her dosya aynı kart
    altında farklı bir "kalite" satırı olarak birleşir (eskisi gibi).
  - "tv": Ders videoları gibi sezon/bölüm yapısına sahip içerik. Panelde
    sezon numarası ve başlangıç bölüm numarası belirlenir; her yeni dosya
    geldiğinde bölüm numarası otomatik artar (reciever.py + metadata.py).
    Dosya adında "S01E02", "1x02", "Sezon 2 Bölüm 5", "Bölüm 7", "Hafta 3"
    gibi bir kalıp varsa, otomatik sayaç yerine o değer kullanılır.
"""

from fastapi import HTTPException
import Backend
from Backend.logger import LOGGER


def _serialize_mode(mode: dict | None) -> dict:
    mode = mode or {}
    return {
        "active": bool(mode),
        "title": mode.get("title"),
        "poster": mode.get("poster"),
        "description": mode.get("description"),
        "media_type": mode.get("media_type", "movie"),
        "year": mode.get("year"),
        "rating": mode.get("rating"),
        "genres": mode.get("genres"),
        "season": mode.get("season"),
        "next_episode": mode.get("next_episode"),
    }


async def manual_add_status_api() -> dict:
    """Panelin sayfa açılışında / periyodik olarak mevcut durumu göstermesi için."""
    return _serialize_mode(Backend.MANUAL_MODE)


async def manual_add_start_api(payload: dict) -> dict:
    title = (payload.get("title") or "").strip()
    poster = (payload.get("poster") or "").strip() or None
    description = (payload.get("description") or "").strip() or None
    media_type = (payload.get("media_type") or "movie").strip().lower()

    if not title:
        raise HTTPException(status_code=400, detail="Başlık boş olamaz.")
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="Başlık çok uzun (maks. 200 karakter).")
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="Geçersiz içerik türü.")

    # Çıkış yılı opsiyoneldir; boş bırakılırsa year=None olarak kalır.
    year_raw = payload.get("year")
    year = None
    if year_raw not in (None, ""):
        try:
            year = int(year_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Çıkış yılı geçersiz.")
        if year < 1900 or year > 2100:
            raise HTTPException(status_code=400, detail="Çıkış yılı 1900-2100 arasında olmalı.")

    # Puan opsiyoneldir; boş bırakılırsa rating=None olarak kalır.
    rating_raw = payload.get("rating")
    rating = None
    if rating_raw not in (None, ""):
        try:
            rating = float(rating_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Puan geçersiz.")
        if rating < 0 or rating > 10:
            raise HTTPException(status_code=400, detail="Puan 0-10 arasında olmalı.")

    # Tür opsiyoneldir; virgülle ayrılmış bir liste veya string olarak gelebilir.
    genres_raw = payload.get("genres")
    genres: list[str] = []
    if isinstance(genres_raw, list):
        genres = [str(g).strip() for g in genres_raw if str(g).strip()]
    elif isinstance(genres_raw, str) and genres_raw.strip():
        genres = [g.strip() for g in genres_raw.split(",") if g.strip()]

    new_mode = {
        "title": title, "poster": poster, "description": description,
        "media_type": media_type, "year": year,
        "rating": rating, "genres": genres,
    }

    if media_type == "tv":
        try:
            season = int(payload.get("season") or 1)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Sezon numarası geçersiz.")
        try:
            start_episode = int(payload.get("start_episode") or 1)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Başlangıç bölüm numarası geçersiz.")
        if season < 1:
            raise HTTPException(status_code=400, detail="Sezon numarası en az 1 olmalı.")
        if start_episode < 1:
            raise HTTPException(status_code=400, detail="Bölüm numarası en az 1 olmalı.")
        new_mode["season"] = season
        new_mode["next_episode"] = start_episode
    else:
        new_mode["season"] = None
        new_mode["next_episode"] = None

    Backend.MANUAL_MODE = new_mode

    if media_type == "tv":
        LOGGER.info(
            f"[manual_add] Panel üzerinden manuel dizi ekleme modu açıldı: "
            f"'{title}' S{new_mode['season']:02d} — bölüm {new_mode['next_episode']}'den başlıyor."
        )
    else:
        LOGGER.info(f"[manual_add] Panel üzerinden manuel film ekleme modu açıldı: '{title}'")

    return {"success": True, **_serialize_mode(Backend.MANUAL_MODE)}


async def manual_add_stop_api() -> dict:
    was_active = bool(Backend.MANUAL_MODE)
    Backend.MANUAL_MODE = None
    if was_active:
        LOGGER.info("[manual_add] Panel üzerinden manuel ekleme modu kapatıldı.")
    return {"success": True, "active": False}


async def manual_add_set_season_api(payload: dict) -> dict:
    """Mod açıkken (media_type == 'tv') başlık/poster/açıklamayı koruyarak yeni bir
    sezona geçmeyi sağlar — mod kapatılıp yeniden açılmasına gerek kalmaz."""
    mode = Backend.MANUAL_MODE
    if not mode or mode.get("media_type") != "tv":
        raise HTTPException(status_code=400, detail="Aktif bir dizi ekleme modu yok.")

    try:
        season = int(payload.get("season"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Sezon numarası geçersiz.")
    if season < 1:
        raise HTTPException(status_code=400, detail="Sezon numarası en az 1 olmalı.")

    try:
        start_episode = int(payload.get("start_episode") or 1)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Başlangıç bölüm numarası geçersiz.")
    if start_episode < 1:
        raise HTTPException(status_code=400, detail="Bölüm numarası en az 1 olmalı.")

    mode["season"] = season
    mode["next_episode"] = start_episode
    LOGGER.info(f"[manual_add] Panelden sezon değiştirildi: S{season:02d}, bölüm {start_episode}'den başlıyor.")

    return {"success": True, **_serialize_mode(Backend.MANUAL_MODE)}


async def manual_add_set_next_episode_api(payload: dict) -> dict:
    """Bir sonraki dosyaya otomatik atanacak bölüm numarasını elle düzeltmek için
    (örn. bir dosya atlandıysa veya sıralama bozulduysa)."""
    mode = Backend.MANUAL_MODE
    if not mode or mode.get("media_type") != "tv":
        raise HTTPException(status_code=400, detail="Aktif bir dizi ekleme modu yok.")

    try:
        next_episode = int(payload.get("next_episode"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Bölüm numarası geçersiz.")
    if next_episode < 1:
        raise HTTPException(status_code=400, detail="Bölüm numarası en az 1 olmalı.")

    mode["next_episode"] = next_episode
    return {"success": True, **_serialize_mode(Backend.MANUAL_MODE)}
