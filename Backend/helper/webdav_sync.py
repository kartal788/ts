"""
webdav_sync.py
==============
WebDAV sunucusundan silinen / taşınan dosyaların katalogdan da kaldırılması.

Sorun: WebDAV ile eklenen içerik, WebDAV sunucusunda silinse bile katalogda
(movie/tv koleksiyonları + `webdav_approved`) kalmaya devam ediyordu.

Çözüm: `webdav_approved` kayıtları (server_id + path) dizin bazında gruplanır,
her dizin için TEK bir PROPFIND (Depth:1) yapılır; listede olmayan dosyalar
ayrıca Depth:0 ile doğrulanır. Yalnızca sunucu AÇIKÇA 404/410 dönerse içerik
silinir.

Güvenlik kuralları (yanlışlıkla toplu silmeyi önler):
  * Sunucu kökü (profil root'u) erişilemiyorsa o sunucu tamamen ATLANIR.
  * Bağlantı hatası, 401, 5xx gibi belirsiz durumlar "silindi" sayılmaz.
  * Bir sunucudaki kayıtların büyük bölümü (>= %50 ve >= 10 adet) eksik
    görünüyorsa (NAS düştü / mount kayboldu olabilir) otomatik silme
    DURDURULUR; yalnızca elle `force=True` ile yapılabilir.

Kullanım:
    report = await sync_webdav(dry_run=True)    # sadece raporla
    report = await sync_webdav()                # sil
    configure_sync_interval(6, startup=True)        # periyodik (panelden değiştirilebilir)
"""

import asyncio
import posixpath
from typing import Dict, List, Optional

from Backend import db
from Backend.helper import webdav as dav
from Backend.logger import LOGGER

APPROVED_COLLECTION = "webdav_approved"

SAFETY_MIN_MISSING = 10      # bu sayının altında eksik varsa eşik uygulanmaz
SAFETY_RATIO = 0.5           # eksik / toplam oranı bunu aşarsa otomatik silme durur
_DIR_CONCURRENCY = 4         # aynı anda en fazla kaç dizin listelensin

_lock = asyncio.Lock()


# ── Kayıtları toplama ─────────────────────────────────────────────────────────

async def _collect_records(server_id: Optional[str] = None) -> Dict[str, List[dict]]:
    """webdav_approved kayıtlarını server_id'ye göre gruplar (tüm storage DB'lerinden)."""
    grouped: Dict[str, List[dict]] = {}
    query = {"server_id": server_id} if server_id else {}
    for key, database in db.dbs.items():
        if not key.startswith("storage_"):
            continue
        try:
            docs = await database[APPROVED_COLLECTION].find(query).to_list(length=None)
        except Exception:
            LOGGER.warning("[webdav-sync] %s okunamadı", key, exc_info=True)
            continue
        for d in docs:
            d["_storage"] = key
            grouped.setdefault(d.get("server_id") or "", []).append(d)
    return grouped


# ── Eksik dosyaları bulma ─────────────────────────────────────────────────────

async def _find_missing(server: dict, records: List[dict]) -> tuple:
    """
    (eksik_kayıtlar, belirsiz_listesi) döndürür; belirsiz_listesi = [{"path", "reason"}].
    Eksik = sunucu açıkça "yok" dedi. Belirsiz durumlar eksik SAYILMAZ.
    """
    by_dir: Dict[str, list] = {}
    unknown: List[dict] = []
    for r in records:
        try:
            path = dav._norm_path(r.get("path", ""))
        except dav.WebDAVError:
            unknown.append({"path": r.get("path", ""), "reason": "Geçersiz yol"})
            continue
        if not path:
            unknown.append({"path": r.get("path", ""), "reason": "Boş yol"})
            continue
        by_dir.setdefault(posixpath.dirname(path), []).append((path, r))

    sem = asyncio.Semaphore(_DIR_CONCURRENCY)
    anc_cache: dict = {}   # üst dizin listeleri (bu çalıştırma boyunca)

    async def check_dir(parent: str, items: list):
        async with sem:
            try:
                names = await dav.list_files_in_dir(server, parent)
            except dav.WebDAVNotFound:
                # Üst dizin yok (kök erişilebilir olduğu zaten doğrulandı) → hepsi yok
                return [r for _, r in items], []
            except dav.WebDAVError as e:
                # Sunucu eksik klasöre 404 yerine garip yanıt veriyor olabilir:
                # kökten başlayarak üst dizin listeleriyle doğrula.
                miss, unk = [], []
                for path, r in items:
                    gone = await dav.missing_via_ancestors(server, path, anc_cache)
                    if gone is True:
                        miss.append(r)
                    else:
                        LOGGER.warning("[webdav-sync] '%s' doğrulanamadı: %s", path, e)
                        unk.append({"path": path, "reason": f"Klasör listelenemedi: {e}"})
                return miss, unk

            miss, unk = [], []
            for path, r in items:
                if dav._nfc(path) in names:
                    continue
                # Listede yok → tek tek doğrula (liste eksik/sayfalı olabilir)
                exists, reason = await dav.check_file(server, path)
                if exists is False:
                    miss.append(r)
                elif exists is None:
                    if await dav.missing_via_ancestors(server, path, anc_cache) is True:
                        miss.append(r)
                    else:
                        LOGGER.warning("[webdav-sync] '%s' doğrulanamadı: %s", path, reason)
                        unk.append({"path": path, "reason": reason})
            return miss, unk

    results = await asyncio.gather(*(check_dir(d, it) for d, it in by_dir.items()))
    missing: List[dict] = []
    for miss, unk in results:
        missing.extend(miss)
        unknown.extend(unk)
    return missing, unknown


# ── Silme ─────────────────────────────────────────────────────────────────────

async def _remove_record(rec: dict) -> bool:
    """Katalog girdisini ve webdav_approved kaydını kaldırır."""
    if rec.get("db_id"):
        try:
            # Son kalite/bölüm silinirse bölüm → sezon → dizi/film de temizlenir.
            # Katalogda zaten yoksa False döner; approved kaydı yine de silinir.
            await db.delete_media_by_stream_id(rec["db_id"])
        except Exception:
            LOGGER.warning("[webdav-sync] katalog girdisi silinemedi: %s",
                           rec.get("path"), exc_info=True)
            return False
    try:
        await db.dbs[rec["_storage"]][APPROVED_COLLECTION].delete_one({"_id": rec["_id"]})
    except Exception:
        LOGGER.warning("[webdav-sync] approved kaydı silinemedi: %s",
                       rec.get("path"), exc_info=True)
        return False
    return True


def _brief(rec: dict) -> dict:
    return {
        "doc_id": str(rec.get("_id")),
        "title": rec.get("title") or rec.get("file_name") or "",
        "path": rec.get("path", ""),
        "media_type": rec.get("media_type", "movie"),
    }


# ── Ana işlev ─────────────────────────────────────────────────────────────────

async def sync_webdav(server_id: Optional[str] = None, dry_run: bool = False,
                      force: bool = False) -> dict:
    """
    WebDAV'dan silinmiş dosyaların kataloğdaki kayıtlarını temizler.

    dry_run=True  → hiçbir şey silmez, ne silineceğini raporlar.
    force=True    → güvenlik eşiğini (toplu silme koruması) yok sayar.
    """
    if _lock.locked():
        return {"error": "Senkronizasyon zaten çalışıyor, bitmesini bekleyin"}

    async with _lock:
        report = {
            "dry_run": dry_run, "servers": [],
            "checked": 0, "missing": 0, "unknown": 0, "removed": 0, "failed": 0, "blocked": 0,
        }
        grouped = await _collect_records(server_id)

        for sid, records in grouped.items():
            entry = {
                "server_id": sid, "server_name": "", "checked": len(records),
                "missing": [], "unknown": [], "removed": 0, "failed": 0,
                "blocked": False, "skipped": "",
            }
            report["servers"].append(entry)
            report["checked"] += len(records)

            server = await dav.get_server(sid) if sid else None
            if server:
                entry["server_name"] = server.get("name", "")
                # Kök erişilebilir mi? Değilse bu sunucuda HİÇBİR ŞEY silme.
                try:
                    await dav._propfind(server, "", 0)
                except dav.WebDAVError as e:
                    entry["skipped"] = f"Sunucu/kök klasör erişilemedi: {e}"
                    LOGGER.warning("[webdav-sync] %s atlandı: %s", entry["server_name"] or sid, e)
                    continue
                try:
                    missing, unknown = await _find_missing(server, records)
                except Exception:
                    LOGGER.error("[webdav-sync] %s kontrol hatası", sid, exc_info=True)
                    entry["skipped"] = "Kontrol sırasında beklenmeyen hata"
                    continue
                entry["unknown"] = unknown
            else:
                # Profil silinmiş: bu kayıtlar akış için kullanılamaz (yetim)
                entry["server_name"] = "(silinmiş profil)"
                missing = list(records)

            report["unknown"] += len(entry["unknown"])
            entry["missing"] = [_brief(r) for r in missing]
            report["missing"] += len(missing)

            if not missing:
                continue

            # Toplu silme koruması (profil yoksa eşik uygulanmaz: kayıtlar zaten yetim)
            if (server and not force and len(missing) >= SAFETY_MIN_MISSING
                    and len(missing) / max(len(records), 1) >= SAFETY_RATIO):
                entry["blocked"] = True
                report["blocked"] += len(missing)
                LOGGER.warning(
                    "[webdav-sync] %s: %d/%d kayıt eksik görünüyor — güvenlik eşiği nedeniyle "
                    "otomatik silme durduruldu (NAS bağlantısını kontrol edin).",
                    entry["server_name"] or sid, len(missing), len(records),
                )
                continue

            if dry_run:
                continue

            for rec in missing:
                if await _remove_record(rec):
                    entry["removed"] += 1
                    LOGGER.info("[webdav-sync] kaldırıldı: %s:%s", entry["server_name"] or sid, rec.get("path"))
                else:
                    entry["failed"] += 1
            report["removed"] += entry["removed"]
            report["failed"] += entry["failed"]

        if report["removed"]:
            try:
                from Backend.helper.platform_catalog import platform_catalog
                platform_catalog.schedule_refresh()
            except Exception as e:
                LOGGER.warning("[webdav-sync] Katalog yenileme planlanamadı: %s", e)

        LOGGER.info(
            "[webdav-sync] Tamamlandı%s — kontrol: %d, eksik: %d, silinen: %d, hata: %d, eşik nedeniyle bekleyen: %d",
            " (dry-run)" if dry_run else "", report["checked"], report["missing"],
            report["removed"], report["failed"], report["blocked"],
        )
        return report


# ── Periyodik çalıştırıcı ─────────────────────────────────────────────────────

class WebDAVSyncChecker:
    """DeadLinkChecker ile aynı desen: start() bir arka plan döngüsü başlatır.
    stop() ile durdurulabilir (panelden aralık değiştirilince görev yeniden kurulur)."""

    def __init__(self, interval_hours: float = 6, first_delay_seconds: int = 120):
        self.interval = max(float(interval_hours), 0.05) * 3600
        self.first_delay = first_delay_seconds
        self.is_running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        LOGGER.info("[webdav-sync] Arka plan senkronizasyonu başladı (aralık: %.2f saat)",
                    self.interval / 3600)
        self._task = asyncio.create_task(self._run_loop())

    def stop(self):
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run_loop(self):
        await asyncio.sleep(self.first_delay)  # bot/DB tam açılsın
        while self.is_running:
            try:
                await sync_webdav()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.error("[webdav-sync] döngü hatası", exc_info=True)
            await asyncio.sleep(self.interval)


# Tek (singleton) çalışan kontrolcü — panelden aralık değişince yeniden kurulur.
_checker: Optional[WebDAVSyncChecker] = None


def configure_sync_interval(hours, startup: bool = False) -> str:
    """
    Arka plan senkronizasyonunu verilen aralığa (saat) göre başlatır / yeniden kurar / kapatır.
    hours <= 0 → kapalı. Çalışan bir event loop içinde çağrılmalıdır.

    startup=True  → ilk açılış: ilk kontrol 2 dk sonra.
    startup=False → panelden değişiklik: ilk kontrol yeni aralık dolunca (ani silme tetiklenmez).
    Kullanıcıya gösterilecek kısa bir durum metni döndürür.
    """
    global _checker
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        hours = 6.0

    if _checker is not None:
        _checker.stop()
        _checker = None

    if hours <= 0:
        LOGGER.info("[webdav-sync] Otomatik senkronizasyon kapalı (aralık: 0).")
        return "WebDAV otomatik senkronizasyonu kapatıldı"

    first_delay = 120 if startup else int(hours * 3600)
    _checker = WebDAVSyncChecker(hours, first_delay_seconds=first_delay)
    asyncio.get_running_loop().create_task(_checker.start())
    label = f"{hours:g}"
    return f"WebDAV senkronizasyonu her {label} saatte bir çalışacak"
