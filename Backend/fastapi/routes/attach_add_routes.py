"""
/media/edit sayfasındaki "İçerik Ekle" butonu için API.

Manuel içerik ekleme modundan (manual_add_routes.py) farkı: burada yeni bir
kart/kayıt oluşturulmaz. Admin, var olan bir film/dizinin düzenleme
sayfasında bu modu başlatır; Backend.ATTACH_MODE global durumu hedef
içeriğin tmdb_id/imdb_id/title bilgisini tutar. Ardından admin video
dosyalarını AUTH_CHANNEL'a iletir; reciever.py bu global durum açıkken
gelen dosyaları TMDB/IMDb sorgusu yapmadan, doğrudan bu tmdb_id/imdb_id ile
etiketleyip kataloğa ekler (bkz. Backend/helper/metadata.py ->
build_manual_metadata). insert_media() var olan kaydı tmdb_id/imdb_id ile
eşleştirdiği için yeni dosyalar ayrı bir kart oluşturmaz; film ise yeni bir
kalite satırı, dizi ise ilgili sezona yeni bir bölüm olarak eklenir.

Mod durdurulduğunda (Durdur) Backend.ATTACH_MODE tekrar None olur ve bot
kanaldan gelen dosyalar için normal TMDB/IMDb sorgulama akışına geri döner.
"""

from fastapi import HTTPException
import Backend
from Backend.logger import LOGGER


def _serialize_attach_mode(mode: dict | None) -> dict:
    mode = mode or {}
    return {
        "active": bool(mode),
        "tmdb_id": mode.get("tmdb_id"),
        "imdb_id": mode.get("imdb_id"),
        "title": mode.get("title"),
        "poster": mode.get("poster"),
        "media_type": mode.get("media_type", "movie"),
        "season": mode.get("season"),
        "next_episode": mode.get("next_episode"),
    }


async def attach_mode_status_api() -> dict:
    """Sayfa açılışında / periyodik olarak mevcut durumu göstermesi için."""
    return _serialize_attach_mode(Backend.ATTACH_MODE)


async def attach_mode_start_api(payload: dict) -> dict:
    try:
        tmdb_id = int(payload.get("tmdb_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Geçersiz TMDB ID.")

    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Başlık boş olamaz.")

    imdb_id = (payload.get("imdb_id") or "").strip() or None
    poster = (payload.get("poster") or "").strip() or None
    media_type = (payload.get("media_type") or "movie").strip().lower()
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="Geçersiz içerik türü.")

    new_mode = {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": title,
        "poster": poster,
        "media_type": media_type,
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

    Backend.ATTACH_MODE = new_mode

    if media_type == "tv":
        LOGGER.info(
            f"[attach_mode] '{title}' (tmdb:{tmdb_id}) içeriğine ekleme modu açıldı: "
            f"S{new_mode['season']:02d} — bölüm {new_mode['next_episode']}'den başlıyor."
        )
    else:
        LOGGER.info(f"[attach_mode] '{title}' (tmdb:{tmdb_id}) içeriğine ekleme modu açıldı.")

    return {"success": True, **_serialize_attach_mode(Backend.ATTACH_MODE)}


async def attach_mode_stop_api() -> dict:
    was_active = bool(Backend.ATTACH_MODE)
    Backend.ATTACH_MODE = None
    if was_active:
        LOGGER.info("[attach_mode] İçerik ekleme modu kapatıldı, bot normal sorgulama akışına döndü.")
    return {"success": True, "active": False}


async def attach_mode_set_season_api(payload: dict) -> dict:
    """Mod açıkken (media_type == 'tv') başka bir sezona geçmeyi sağlar —
    mod kapatılıp yeniden açılmasına gerek kalmaz."""
    mode = Backend.ATTACH_MODE
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
    LOGGER.info(f"[attach_mode] Sezon değiştirildi: S{season:02d}, bölüm {start_episode}'den başlıyor.")

    return {"success": True, **_serialize_attach_mode(Backend.ATTACH_MODE)}


async def attach_mode_set_next_episode_api(payload: dict) -> dict:
    """Bir sonraki dosyaya otomatik atanacak bölüm numarasını elle düzeltmek için
    (örn. bir dosya atlandıysa veya sıralama bozulduysa)."""
    mode = Backend.ATTACH_MODE
    if not mode or mode.get("media_type") != "tv":
        raise HTTPException(status_code=400, detail="Aktif bir dizi ekleme modu yok.")

    try:
        next_episode = int(payload.get("next_episode"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Bölüm numarası geçersiz.")
    if next_episode < 1:
        raise HTTPException(status_code=400, detail="Bölüm numarası en az 1 olmalı.")

    mode["next_episode"] = next_episode
    return {"success": True, **_serialize_attach_mode(Backend.ATTACH_MODE)}
