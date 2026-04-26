"""
subtitle_routes.py
==================
Altyazı yönetimi için API endpoint'leri.

Desteklenen işlemler:
  POST   /api/subtitles/upload           → .srt / .vtt dosyasını Telegram AUTH_CHANNEL'a yükle
  GET    /api/subtitles/list             → Belirli içeriğin altyazılarını listele
  DELETE /api/subtitles/{subtitle_id}    → Altyazıyı sil (Telegram mesajı + DB kaydı)
  GET    /subtitles/serve/{subtitle_id}  → Ham altyazı dosyasını serve et (Stremio için)

Stremio subtitles endpoint'i stremio_routes.py içinde tanımlıdır:
  GET  /stremio/{token}/subtitles/{type}/{id}.json

NOT: Altyazı dosyaları artık diske kaydedilmez; doğrudan Telegram'daki
     AUTH_CHANNEL[0] kanalına yüklenir. DB'de dosya yolu yerine
     tg_chat_id + tg_message_id saklanır.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from Backend import db
from Backend.config import Telegram
from Backend.fastapi.security.credentials import require_auth
from Backend.logger import LOGGER

router = APIRouter(tags=["Subtitles"])

# İzin verilen dosya uzantıları
ALLOWED_EXTENSIONS = {".srt", ".vtt"}
MAX_FILE_SIZE_MB = 2  # SRT/VTT dosyaları çok küçük olur; 2 MB yeterli


LANG_LABELS = {
    "tr": "Türkçe",
    "en": "İngilizce",
    "de": "Almanca",
    "fr": "Fransızca",
    "es": "İspanyolca",
    "it": "İtalyanca",
    "pt": "Portekizce",
    "ru": "Rusça",
    "ar": "Arapça",
    "ja": "Japonca",
    "ko": "Korece",
    "zh": "Çince",
    "nl": "Flemenkçe",
    "pl": "Lehçe",
    "sv": "İsveççe",
    "no": "Norveççe",
    "da": "Danca",
}


def _get_auth_channel() -> int:
    """
    config.env'deki AUTH_CHANNEL listesinin ilk elemanını int olarak döner.
    AUTH_CHANNEL değerleri config.py'de string listesi olarak tutulur (örn. ["-100123456789"]).
    """
    channels = Telegram.AUTH_CHANNEL
    if not channels:
        raise HTTPException(
            status_code=500,
            detail="AUTH_CHANNEL tanımlı değil. Lütfen config.env dosyasını kontrol edin.",
        )
    raw = channels[0]
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(
            status_code=500,
            detail=f"AUTH_CHANNEL değeri geçersiz int: {raw!r}",
        )


# ─── Yükleme ─────────────────────────────────────────────────────────────────

@router.post("/api/subtitles/upload", dependencies=[Depends(require_auth)])
async def upload_subtitle(
    file: UploadFile = File(...),
    imdb_id: str = Form(...),
    media_type: str = Form(...),   # "movie" veya "tv"
    lang: str = Form("tr"),
    season: Optional[int] = Form(None),
    episode: Optional[int] = Form(None),
):
    """
    Admin panelinden .srt / .vtt altyazı yükler.
    Dosya diske kaydedilmez; doğrudan Telegram AUTH_CHANNEL[0]'a iletilir.
    """
    from Backend.pyrofork.bot import StreamBot

    # Uzantı kontrolü
    original_name = file.filename or "subtitle.srt"
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Desteklenmeyen dosya türü: {ext}. Sadece .srt ve .vtt kabul edilir.",
        )

    # Boyut kontrolü
    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=413,
            detail=f"Dosya çok büyük ({size_mb:.1f} MB). Maksimum {MAX_FILE_SIZE_MB} MB.",
        )

    # Gönderilecek kanal
    chat_id = _get_auth_channel()

    # Güvenli dosya adı oluştur (Telegram'da caption olarak görünür)
    safe_imdb = imdb_id.replace("/", "_").replace("\\", "_")
    season_ep = ""
    if season is not None and episode is not None:
        season_ep = f"_S{season:02d}E{episode:02d}"
    elif season is not None:
        season_ep = f"_S{season:02d}"

    unique_id = uuid.uuid4().hex[:8]
    filename = f"{safe_imdb}{season_ep}_{lang}_{unique_id}{ext}"

    lang_label = LANG_LABELS.get(lang, lang.upper())
    caption = (
        f"🎬 Altyazı\n"
        f"• IMDb: {imdb_id}\n"
        f"• Dil: {lang_label} ({lang})\n"
        f"• Dosya: {filename}"
    )

    # Telegram'a yükle — BytesIO ile bellek üzerinden (diske yazmadan)
    try:
        buf = io.BytesIO(content)
        buf.name = filename  # pyrogram dosya adını buradan alır

        tg_message = await StreamBot.send_document(
            chat_id=chat_id,
            document=buf,
            caption=caption,
            force_document=True,
        )
    except Exception as exc:
        LOGGER.error(f"[Subtitle] Telegram'a yükleme başarısız: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Telegram'a yükleme başarısız: {exc}",
        )

    tg_message_id = tg_message.id
    tg_chat_id = str(chat_id)  # negatif olabilir; string sakla

    # DB'ye kaydet — file_path yok, Telegram referansı var
    doc = {
        "imdb_id": imdb_id,
        "media_type": media_type,
        "lang": lang,
        "lang_label": lang_label,
        "season": season,
        "episode": episode,
        "filename": filename,
        "original_name": original_name,
        "file_size": len(content),
        "tg_chat_id": tg_chat_id,
        "tg_message_id": tg_message_id,
        "uploaded_at": datetime.utcnow(),
    }

    subtitle_id = await db.add_subtitle(doc)
    LOGGER.info(
        f"[Subtitle] Telegram'a yüklendi: {filename} "
        f"(ID={subtitle_id}, chat={tg_chat_id}, msg={tg_message_id})"
    )

    return JSONResponse({
        "success": True,
        "subtitle_id": subtitle_id,
        "filename": filename,
        "lang": lang,
        "lang_label": lang_label,
        "message": "Altyazı başarıyla Telegram kanalına yüklendi.",
    })


# ─── Listeleme ────────────────────────────────────────────────────────────────

@router.get("/api/subtitles/list", dependencies=[Depends(require_auth)])
async def list_subtitles(
    imdb_id: str = Query(...),
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
):
    """Belirli bir içeriğin tüm altyazılarını döndürür."""
    from datetime import datetime as _dt
    subtitles = await db.get_subtitles(imdb_id, season, episode)
    base_url = Telegram.BASE_URL.rstrip("/")

    for s in subtitles:
        s["serve_url"] = f"{base_url}/subtitles/serve/{s['_id']}"
        for key, val in list(s.items()):
            if isinstance(val, _dt):
                s[key] = val.isoformat()

    return JSONResponse({"subtitles": subtitles})


# ─── Silme ───────────────────────────────────────────────────────────────────

@router.delete("/api/subtitles/{subtitle_id}", dependencies=[Depends(require_auth)])
async def delete_subtitle(subtitle_id: str):
    """Altyazıyı hem Telegram'dan hem de veritabanından siler."""
    from Backend.pyrofork.bot import StreamBot

    doc = await db.get_subtitle_by_id(subtitle_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Altyazı bulunamadı.")

    # Telegram mesajını sil
    tg_chat_id = doc.get("tg_chat_id")
    tg_message_id = doc.get("tg_message_id")

    if tg_chat_id and tg_message_id:
        try:
            await StreamBot.delete_messages(
                chat_id=int(tg_chat_id),
                message_ids=int(tg_message_id),
            )
            LOGGER.info(f"[Subtitle] Telegram mesajı silindi: chat={tg_chat_id}, msg={tg_message_id}")
        except Exception as e:
            # Mesaj zaten silinmiş olabilir; sadece uyar, işlemi durdurma
            LOGGER.warning(f"[Subtitle] Telegram mesajı silinemedi: {e}")

    # Eski sistemden kalan disk dosyasını da temizle (varsa)
    file_path_str = doc.get("file_path", "")
    if file_path_str:
        fp = Path(file_path_str)
        if fp.exists():
            try:
                fp.unlink()
            except Exception as e:
                LOGGER.warning(f"[Subtitle] Eski disk dosyası silinemedi: {fp} → {e}")

    # DB'den sil
    deleted = await db.delete_subtitle(subtitle_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Veritabanından silinemedi.")

    # Cache'ten de kaldır
    _cache_remove(subtitle_id)

    LOGGER.info(f"[Subtitle] Silindi: {subtitle_id}")
    return JSONResponse({"success": True, "message": "Altyazı silindi."})


# ─── Bellek Cache (12 saat TTL, son istek üzerinden, max 50 kayıt) ───────────
#
# Yapı: { subtitle_id: {"content": str, "last_access": float} }
# Her serve isteğinde last_access güncellenir.
# 12 saat boyunca hiç istek gelmezse bir sonraki herhangi bir serve isteğinde
# süresi dolmuş tüm kayıtlar temizlenir.
# Cache 50 kayıt sınırına ulaşınca en eski erişilen kayıt silinir (LRU).

import time as _time

_SUBTITLE_CACHE: dict = {}
_CACHE_TTL = 12 * 3600  # 12 saat (saniye)
_CACHE_MAX = 50          # maksimum kayıt sayısı


def _cache_get(subtitle_id: str) -> Optional[str]:
    """Cache'te varsa içeriği döner ve last_access'i günceller; yoksa None."""
    entry = _SUBTITLE_CACHE.get(subtitle_id)
    if entry is None:
        return None
    entry["last_access"] = _time.monotonic()
    return entry["content"]


def _cache_set(subtitle_id: str, content: str) -> None:
    """İçeriği cache'e yazar. 50 kayıt sınırı aşılırsa en eski silinir (LRU)."""
    if subtitle_id not in _SUBTITLE_CACHE and len(_SUBTITLE_CACHE) >= _CACHE_MAX:
        # En eski erişilen kaydı bul ve sil
        oldest_key = min(_SUBTITLE_CACHE, key=lambda k: _SUBTITLE_CACHE[k]["last_access"])
        del _SUBTITLE_CACHE[oldest_key]
    _SUBTITLE_CACHE[subtitle_id] = {
        "content": content,
        "last_access": _time.monotonic(),
    }


def _cache_evict() -> None:
    """Son erişimden bu yana 12 saat geçmiş tüm kayıtları temizler."""
    now = _time.monotonic()
    expired = [k for k, v in _SUBTITLE_CACHE.items() if now - v["last_access"] >= _CACHE_TTL]
    for k in expired:
        del _SUBTITLE_CACHE[k]
    if expired:
        LOGGER.info(f"[Subtitle Cache] {len(expired)} suresi dolmus kayit temizlendi.")


def _cache_remove(subtitle_id: str) -> None:
    """Silme isleminde cache kaydini da kaldirir."""
    _SUBTITLE_CACHE.pop(subtitle_id, None)


# ─── Serve (ham dosya) ───────────────────────────────────────────────────────

def _srt_to_vtt(srt_content: str) -> str:
    """SRT icerigini WebVTT formatina cevirir (Stremio web icin gerekli)."""
    import re
    vtt = "WEBVTT\n\n"
    blocks = re.split(r"\n\n+", srt_content.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        lines[0] = lines[0].replace(",", ".")
        vtt += "\n".join(lines) + "\n\n"
    return vtt


@router.get("/subtitles/serve/{subtitle_id}")
async def serve_subtitle(subtitle_id: str, format: str = "vtt"):
    """
    Altyazi dosyasini Stremio'ya veya tarayiciya serve eder.

    Icerik once bellek cache'inden aranir. Cache'te varsa Telegram'a gidilmez;
    son istekten itibaren 24 saat boyunca cache'te tutulur, bu sure her yeni
    istekle sifirlanir. 24 saat istek gelmezse cache'ten duser ve bir sonraki
    istekte Telegram'dan tekrar indirilir.

    ?format=vtt  -> SRT dosyasini aninda VTT'ye cevirip doner (Stremio web icin)
    ?format=srt  -> Ham SRT dosyasini doner (masaustu oynaticiler icin)

    Bu endpoint herkese aciktir -- CORS header'lari Stremio icin gereklidir.
    """
    from fastapi.responses import Response
    from Backend.pyrofork.bot import StreamBot

    # Suresi dolmus cache kayitlarini temizle (her istekte kontrol)
    _cache_evict()

    doc = await db.get_subtitle_by_id(subtitle_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Altyazi bulunamadi.")

    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Cache-Control": "public, max-age=3600",
    }

    filename = doc.get("filename", "subtitle.srt")
    ext = Path(filename).suffix.lower()

    # 1. Cache kontrolu
    raw_content = _cache_get(subtitle_id)

    if raw_content is not None:
        LOGGER.debug(f"[Subtitle Cache] Cache'ten donduruldu: {subtitle_id}")

    else:
        # 2. Telegram'dan indir (yeni sistem)
        tg_chat_id = doc.get("tg_chat_id")
        tg_message_id = doc.get("tg_message_id")

        if tg_chat_id and tg_message_id:
            import tempfile
            tmp_path = None
            try:
                message = await StreamBot.get_messages(
                    chat_id=int(tg_chat_id),
                    message_ids=int(tg_message_id),
                )
                if not message or not message.document:
                    raise ValueError("Telegram mesajinda belge bulunamadi.")

                with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                    tmp_path = tmp.name

                await StreamBot.download_media(message, file_name=tmp_path)
                raw_content = Path(tmp_path).read_text(encoding="utf-8", errors="replace")

                _cache_set(subtitle_id, raw_content)
                LOGGER.info(f"[Subtitle Cache] Telegram'dan indirilip cache'e alindi: {subtitle_id}")

            except Exception as exc:
                LOGGER.error(f"[Subtitle] Telegram'dan indirilemedi (id={subtitle_id}): {exc}")
                raise HTTPException(
                    status_code=502,
                    detail="Altyazi Telegram'dan alinamadi. Lutfen tekrar deneyin.",
                )
            finally:
                if tmp_path and Path(tmp_path).exists():
                    try:
                        Path(tmp_path).unlink()
                    except Exception:
                        pass

        # 3. Eski sistem: disk dosyasi (geriye donuk uyumluluk)
        elif doc.get("file_path"):
            _APP_ROOT = Path(__file__).resolve().parent.parent.parent.parent
            SUBTITLE_DIR = _APP_ROOT / "subtitles"

            file_path = Path(doc["file_path"])
            if not file_path.is_absolute():
                file_path = _APP_ROOT / file_path
            if not file_path.exists():
                fallback = SUBTITLE_DIR / doc.get("filename", "")
                if fallback.exists():
                    file_path = fallback
                else:
                    LOGGER.warning(f"[Subtitle] Eski disk dosyasi bulunamadi: {file_path}")
                    raise HTTPException(status_code=404, detail="Altyazi dosyasi bulunamadi.")

            raw_content = file_path.read_text(encoding="utf-8", errors="replace")
            _cache_set(subtitle_id, raw_content)

        else:
            raise HTTPException(
                status_code=404,
                detail="Altyazi kaynagi bulunamadi (ne Telegram ne disk kaydi var).",
            )

    # SRT -> VTT donusumu (varsayilan: Stremio web VTT ister)
    if ext == ".srt" and format != "srt":
        vtt_content = _srt_to_vtt(raw_content)
        return Response(
            content=vtt_content.encode("utf-8"),
            media_type="text/vtt; charset=utf-8",
            headers=cors_headers,
        )

    media_type = "text/vtt; charset=utf-8" if ext == ".vtt" else "text/plain; charset=utf-8"
    return Response(
        content=raw_content.encode("utf-8"),
        media_type=media_type,
        headers=cors_headers,
    )

