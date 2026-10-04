"""
scheduled_delete.py
===================
Sunucu (SUNUCU_DIR) dosyaları için ZAMANLANMIŞ SİLME.

sunucu.html → "HTTPS'den Dosya İndir" modalında admin bir tarih/saat seçerse,
metadata onaylanıp veritabanına kaydedildiği anda buraya bir kayıt düşer.
Zamanı gelince:
  1) Veritabanındaki (Stremio kataloğunu besleyen) kayıt kaldırılır,
  2) Dosya sunucudan silinir (boş kalan üst klasör de temizlenir),
  3) Katalog yenilemesi tetiklenir.

Kayıtlar MongoDB'de (tracking DB → "scheduled_deletions") tutulur; bu yüzden bot
yeniden başlasa bile zamanlama kaybolmaz. Süresi kaçırılmış (bot kapalıyken
zamanı geçmiş) kayıtlar açılışta hemen işlenir.

Tüm zamanlar UTC (naive datetime) olarak saklanır; arayüz yerel saati ISO/UTC'ye
çevirip gönderir.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from bson import ObjectId

from Backend.logger import LOGGER

COLLECTION = "scheduled_deletions"
POLL_SECONDS = 30
MIN_LEAD_SECONDS = 30            # geçmişe / "şimdi"ye zamanlamayı engelle
MAX_LEAD_DAYS = 365 * 5          # makul üst sınır (yazım hatası koruması)
STALE_CLAIM_MINUTES = 10         # "running" takılı kalırsa tekrar dene
MAX_ATTEMPTS = 5

_loop_task: Optional[asyncio.Task] = None
_bg_refs: set = set()   # arka plan görevleri çöp toplayıcıya kaptırılmasın


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _col():
    from Backend import db
    return db.dbs["tracking"][COLLECTION]


def parse_delete_at(value) -> datetime:
    """İstemciden gelen ISO-8601 metnini naive UTC datetime'a çevirir.

    Zaman dilimi bilgisi YOKSA UTC kabul edilir (istemci toISOString() gönderir).
    Geçmiş / çok yakın / çok uzak tarihler ValueError fırlatır.
    """
    if not value or not isinstance(value, str):
        raise ValueError("Silinme tarihi/saati geçersiz.")
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError("Silinme tarihi/saati okunamadı.")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    now = _utcnow()
    if dt < now + timedelta(seconds=MIN_LEAD_SECONDS):
        raise ValueError("Silinme zamanı gelecekte olmalı (en az birkaç saniye sonrası).")
    if dt > now + timedelta(days=MAX_LEAD_DAYS):
        raise ValueError("Silinme zamanı çok uzak bir tarih.")
    return dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return (dt.replace(tzinfo=timezone.utc).isoformat() if dt else None)


async def schedule(rel_path: str, abs_path: str, encoded_id: Optional[str],
                   delete_at: datetime, title: str = "") -> str:
    """Bu dosya için zamanlanmış silme oluşturur. Aynı dosyada bekleyen eski
    zamanlama varsa onun yerine yenisi geçer (tek aktif zamanlama)."""
    col = _col()
    await col.delete_many({"rel_path": rel_path, "status": {"$in": ["pending", "failed"]}})
    res = await col.insert_one({
        "rel_path": rel_path,
        "abs_path": abs_path,
        "encoded_id": encoded_id,
        "title": title or Path(rel_path).name,
        "delete_at": delete_at,
        "status": "pending",
        "attempts": 0,
        "created_at": _utcnow(),
    })
    LOGGER.info(f"[zamanli-silme] '{rel_path}' → {delete_at.isoformat()} UTC'de silinecek.")
    return str(res.inserted_id)


async def list_pending(rel_prefix: Optional[str] = None) -> list:
    q: dict = {"status": {"$in": ["pending", "running", "failed"]}}
    out = []
    async for d in _col().find(q).sort("delete_at", 1):
        if rel_prefix and not d.get("rel_path", "").startswith(rel_prefix):
            continue
        out.append({
            "id": str(d["_id"]),
            "path": d.get("rel_path"),
            "title": d.get("title"),
            "delete_at": _iso(d.get("delete_at")),
            "status": d.get("status"),
            "last_error": d.get("last_error"),
        })
    return out


async def pending_by_path() -> dict:
    """{rel_path: {id, delete_at}} — dosya listesinde rozet göstermek için."""
    res = {}
    async for d in _col().find({"status": {"$in": ["pending", "running"]}}):
        res[d.get("rel_path")] = {"id": str(d["_id"]), "delete_at": _iso(d.get("delete_at"))}
    return res


async def pending_by_abs_path() -> dict:
    """{abs_path: delete_at(datetime, naive UTC)} — Stremio akışında gösterim için."""
    res = {}
    async for d in _col().find({"status": {"$in": ["pending", "running"]}}):
        ap = d.get("abs_path")
        if ap and d.get("delete_at"):
            res[ap] = d["delete_at"]
    return res


async def set_for_path(rel_path: str, abs_path: str, encoded_id: Optional[str],
                       delete_at: datetime, title: str = "") -> str:
    """Dosya için silinme zamanını değiştirir. Bekleyen zamanlama varsa yerinde
    günceller (encoded_id/başlık korunur), yoksa yenisini oluşturur."""
    col = _col()
    existing = await col.find_one({"rel_path": rel_path, "status": {"$in": ["pending", "failed", "running"]}})
    if existing:
        await col.update_one(
            {"_id": existing["_id"]},
            {"$set": {"delete_at": delete_at, "status": "pending", "attempts": 0},
             "$unset": {"last_error": "", "claimed_at": ""}},
        )
        LOGGER.info(f"[zamanli-silme] '{rel_path}' zamanı değiştirildi → {delete_at.isoformat()} UTC")
        return str(existing["_id"])
    return await schedule(rel_path, abs_path, encoded_id, delete_at, title)


async def repath(old_abs: str, new_abs: str) -> int:
    """Dosya/klasör yeniden adlandırılınca bekleyen zamanlamaların yolunu günceller.
    (Aksi halde zamanı gelince eski yol bulunamaz ve dosya sunucuda kalırdı.)"""
    import os
    from Backend.helper.encrypt import encode_string, decode_string
    from Backend.fastapi.routes import sunucu_routes as sr

    base = sr.SUNUCU_DIR.resolve()
    n = 0
    async for d in _col().find({"status": {"$in": ["pending", "running", "failed"]}}):
        ap = d.get("abs_path") or ""
        if not ap or not (ap == old_abs or ap.startswith(old_abs + os.sep) or ap.startswith(old_abs + "/")):
            continue
        new_ap = new_abs + ap[len(old_abs):]
        try:
            new_rel = str(Path(new_ap).resolve().relative_to(base))
        except Exception:
            continue
        upd = {"abs_path": new_ap, "rel_path": new_rel}
        old_enc = d.get("encoded_id")
        if old_enc:
            try:
                dec = await decode_string(old_enc)
                if dec.get("local_path"):
                    dec["local_path"] = new_ap
                    upd["encoded_id"] = await encode_string(dec)
            except Exception:
                pass
        await _col().update_one({"_id": d["_id"]}, {"$set": upd})
        n += 1
    if n:
        LOGGER.info(f"[zamanli-silme] {n} zamanlama yeni yola taşındı: {old_abs} → {new_abs}")
    return n


async def cancel(sched_id: str) -> bool:
    try:
        oid = ObjectId(sched_id)
    except Exception:
        return False
    r = await _col().delete_one({"_id": oid, "status": {"$in": ["pending", "failed"]}})
    if r.deleted_count:
        LOGGER.info(f"[zamanli-silme] Zamanlama iptal edildi: {sched_id}")
    return bool(r.deleted_count)


# Arşivden çıkan klasörlerde video silindikten sonra geriye kalan "çöp" dosyalar
_JUNK_EXTS = {
    ".nfo", ".txt", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".srt", ".sub", ".idx",
    ".ass", ".ssa", ".vtt", ".url", ".sfv", ".md5", ".sha1", ".nzb", ".xml", ".html",
    ".htm", ".db", ".ini", ".log", ".md", ".exe", ".lnk", ".par2", ".diz",
}
_VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m4v", ".mpg", ".mpeg", ".webm", ".flv",
}


def _remove_path(target: Path) -> None:
    """Dosya/klasörü gerçekten siler; başarısızsa istisna fırlatır (sessizce geçmez)."""
    import os
    import shutil
    import stat

    def _onerror(func, path, exc_info):
        # Salt-okunur / izin sorunu: yazma izni verip bir kez daha dene
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(path)
        except Exception:
            raise exc_info[1]

    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target, onerror=_onerror)
    else:
        try:
            target.unlink()
        except FileNotFoundError:
            return
        except PermissionError:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            target.unlink()

    if target.exists():
        raise RuntimeError(f"Silme komutu çalıştı ama yol hâlâ duruyor: {target}")


def _cleanup_leftover_dirs(start_dir: Path, base: Path) -> None:
    """Dosya silindikten sonra üst klasörleri temizler.

    - Boş klasör → silinir.
    - İçinde video YOK ve yalnızca çöp dosyaları (nfo, jpg, srt …) varsa → klasör silinir
      (arşivden çıkan klasörlerin geride kalan artıkları).
    - İçinde video / bilinmeyen dosya varsa dururuz.
    SUNUCU_DIR'in kendisine asla dokunulmaz.
    """
    import shutil
    parent = start_dir
    while True:
        try:
            parent_res = parent.resolve()
            if parent_res == base or base not in parent_res.parents:
                break
            if not parent.exists():
                parent = parent.parent
                continue
            files = [p for p in parent.rglob("*") if p.is_file()]
            if not files:
                shutil.rmtree(parent, ignore_errors=True)
            else:
                has_video = any(p.suffix.lower() in _VIDEO_EXTS for p in files)
                only_junk = all(p.suffix.lower() in _JUNK_EXTS for p in files)
                if has_video or not only_junk:
                    break
                shutil.rmtree(parent, ignore_errors=True)
            if parent.exists():
                break
            parent = parent.parent
        except Exception as e:
            LOGGER.warning(f"[zamanli-silme] Klasör temizliği atlandı ({parent}): {e}")
            break


async def _execute(doc: dict) -> None:
    """Tek bir zamanlanmış silmeyi uygular: Stremio/DB kaydı + fiziksel dosya.

    Sıra bilinçli seçildi:
      1) DB (Stremio) kaydı kaldırılır → içerik hemen katalogdan düşer
      2) FİZİKSEL DOSYA silinir (hata olursa istisna → zamanlayıcı tekrar dener)
      3) Artık klasörler temizlenir
      4) Katalog yenilenir
      5) Tüm kütüphaneyi tarayan ağır DB temizliği ARKA PLANDA çalışır; böylece
         büyük kütüphanede dakikalarca sürüp dosya silmeyi geciktirmez/askıda bırakmaz.
    """
    from Backend import db
    from Backend.fastapi.routes import sunucu_routes as sr

    rel = doc.get("rel_path") or ""
    abs_path = doc.get("abs_path") or ""
    encoded_id = doc.get("encoded_id")

    # 0) Önce yolu doğrula: SUNUCU_DIR dışına çıkan kayıt hiçbir şeye dokunmadan reddedilir
    try:
        target = sr._safe_path(rel)
    except ValueError:
        raise RuntimeError("Güvensiz/geçersiz yol; silme atlandı.")

    # 1) Veritabanı kaydı (Stremio kataloğu buradan beslenir)
    if encoded_id:
        try:
            await db.delete_media_by_stream_id(encoded_id)
        except Exception as e:
            LOGGER.warning(f"[zamanli-silme] DB kaydı id ile silinemedi: {e}")

    # 2) Fiziksel dosya (kritik adım — hata fırlatırsa kayıt yeniden denenir)
    base = sr.SUNUCU_DIR.resolve()
    if target.exists():
        await asyncio.to_thread(_remove_path, target)
        LOGGER.info(f"[zamanli-silme] Dosya sunucudan silindi: {target}")
    else:
        LOGGER.info(f"[zamanli-silme] Dosya zaten yok (daha önce silinmiş/taşınmış): {target}")

    # 3) Arşivden çıkarılan klasörün artıkları / boş üst klasörler
    await asyncio.to_thread(_cleanup_leftover_dirs, target.parent, base)

    # 4) Katalog yenile
    try:
        from Backend.helper.platform_catalog import platform_catalog as _pc
        _pc.schedule_refresh()
    except Exception:
        pass

    # 5) Aynı dosyaya işaret eden başka kayıt kaldıysa (yeniden kayıt vb.) arka planda temizle
    async def _bg_cleanup():
        try:
            await sr._db_cleanup_after_delete(list({p for p in (abs_path, str(target)) if p}))
        except Exception as e:
            LOGGER.warning(f"[zamanli-silme] DB tarama temizliği hatası: {e}")
    _t = asyncio.create_task(_bg_cleanup())
    _bg_refs.add(_t)
    _t.add_done_callback(_bg_refs.discard)

    LOGGER.info(f"[zamanli-silme] Silindi (sunucu + veritabanı): {rel}")


async def run_due() -> int:
    """Zamanı gelen kayıtları işler. İşlenen kayıt sayısını döner."""
    col = _col()
    processed = 0
    while True:
        now = _utcnow()
        doc = await col.find_one_and_update(
            {"$or": [
                {"status": "pending", "delete_at": {"$lte": now}},
                {"status": "running", "claimed_at": {"$lte": now - timedelta(minutes=STALE_CLAIM_MINUTES)}},
            ]},
            {"$set": {"status": "running", "claimed_at": now}},
        )
        if not doc:
            break
        try:
            await _execute(doc)
            await col.delete_one({"_id": doc["_id"]})
            processed += 1
        except Exception as e:
            attempts = int(doc.get("attempts", 0)) + 1
            LOGGER.error(f"[zamanli-silme] '{doc.get('rel_path')}' silinemedi (deneme {attempts}): {e}")
            # Vazgeçme yok: dosya sunucuda kalmasın diye kademeli geri çekilmeyle
            # (2 dk, 4 dk … en fazla 60 dk) tekrar denenir.
            backoff_min = min(2 * attempts, 60)
            await col.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "status": "pending",
                    "attempts": attempts,
                    "last_error": str(e)[:300],
                    "delete_at": now + timedelta(minutes=backoff_min),
                }},
            )
    return processed


async def scheduler_loop() -> None:
    LOGGER.info("[zamanli-silme] Zamanlayıcı başladı.")
    while True:
        try:
            await run_due()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOGGER.error(f"[zamanli-silme] Döngü hatası: {e}")
        await asyncio.sleep(POLL_SECONDS)


def start_scheduler() -> None:
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(scheduler_loop())
