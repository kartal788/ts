"""
cloud_sync.py
=============
rclone / Google Drive'dan silinen dosyaların Stremio kataloğundan da kaldırılması.

webdav_sync.py ile aynı mantık, aynı güvenlik kuralları:

  * `ekle_approved` kayıtları (rclone: `rclone_path`/`remote`, Drive: `file_id`) kaynağa göre
    gruplanır (her rclone remote'u ayrı bir grup, Drive tek grup).
  * rclone: dizin başına TEK `rclone lsjson`; listede olmayan dosya ayrıca tek tek doğrulanır.
    Yalnızca rclone "bulunamadı" (çıkış kodu 3/4) derse dosya eksik sayılır.
  * Drive: her dosya için `files.get`; 404 veya `trashed=True` ise eksik sayılır
    (Drive'da "çöp kutusuna taşı" da silme sayılır).
  * Bağlantı hatası, kimlik hatası, zaman aşımı gibi belirsiz durumlar "silindi" SAYILMAZ.
  * Grubun kökü/Drive erişilemiyorsa o grup tamamen ATLANIR.
  * Bir gruptaki kayıtların büyük bölümü (>= %50 ve >= 10 adet) eksik görünüyorsa
    (remote düştü / token bozuldu olabilir) otomatik silme DURDURULUR; yalnızca `force=True`.

Kullanım:
    report = await sync_cloud(dry_run=True)     # sadece raporla
    report = await sync_cloud()                 # sil
    configure_cloud_sync_interval(6, startup=True)   # periyodik (panelden değiştirilebilir)
"""

import asyncio
import json
import pickle
import posixpath
import shutil
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from Backend import db
from Backend.logger import LOGGER

APPROVED_COLLECTION = "ekle_approved"

SAFETY_MIN_MISSING = 10      # bu sayının altında eksik varsa eşik uygulanmaz
SAFETY_RATIO = 0.5           # eksik / toplam oranı bunu aşarsa otomatik silme durur
_DIR_CONCURRENCY = 3         # aynı anda en fazla kaç rclone dizin listesi
_RCLONE_TIMEOUT = 90         # saniye

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RCLONE_CONF_PATH = _PROJECT_ROOT / "rclone.conf"
GDRIVE_TOKEN_PATH = _PROJECT_ROOT / "gdrive_token.pickle"

_lock = asyncio.Lock()


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


# ── Kayıtları toplama ─────────────────────────────────────────────────────────

def _is_rclone(rec: dict) -> bool:
    return rec.get("source") == "rclone" or bool(rec.get("rclone_path"))


def _rclone_parts(rec: dict) -> Tuple[str, str]:
    """(remote, path) döndürür. Kayıtta `remote` yoksa `rclone_path`'ten ("remote:yol") çıkarır."""
    remote = (rec.get("remote") or "").strip()
    rp = (rec.get("rclone_path") or "").strip()
    if ":" in rp:
        r2, p2 = rp.split(":", 1)
        return (remote or r2.strip()), p2.strip().strip("/")
    return remote, rp.strip("/")


async def _collect_records() -> Dict[str, List[dict]]:
    """ekle_approved kayıtlarını gruba göre toplar: 'gdrive' veya 'rclone:<remote>'."""
    grouped: Dict[str, List[dict]] = {}
    for key, database in db.dbs.items():
        if not key.startswith("storage_"):
            continue
        try:
            docs = await database[APPROVED_COLLECTION].find({}).to_list(length=None)
        except Exception:
            LOGGER.warning("[cloud-sync] %s okunamadı", key, exc_info=True)
            continue
        for d in docs:
            d["_storage"] = key
            if _is_rclone(d):
                remote, _ = _rclone_parts(d)
                grouped.setdefault(f"rclone:{remote}", []).append(d)
            else:
                grouped.setdefault("gdrive", []).append(d)
    return grouped


# ── rclone ────────────────────────────────────────────────────────────────────

def _rclone_bin() -> str:
    found = shutil.which("rclone")
    if found:
        return found
    for c in ("/usr/bin/rclone", "/usr/local/bin/rclone", "/usr/sbin/rclone",
              "/opt/rclone/rclone", "/app/.venv/bin/rclone"):
        if Path(c).is_file():
            return c
    raise RuntimeError("rclone binary bulunamadı")


async def _rclone(*args: str) -> Tuple[int, str, str]:
    """rclone çalıştırır → (returncode, stdout, stderr). Zaman aşımında returncode = -1."""
    proc = await asyncio.create_subprocess_exec(
        _rclone_bin(), *args, "--config", str(RCLONE_CONF_PATH),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_RCLONE_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return -1, "", "zaman aşımı"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace").strip()


# rclone çıkış kodları: 3 = dizin bulunamadı, 4 = dosya bulunamadı
_NOT_FOUND_CODES = (3, 4)


async def _rclone_file_exists(remote: str, path: str) -> Tuple[Optional[bool], str]:
    """True = var, False = rclone açıkça 'yok' dedi, None = belirsiz."""
    rc, out, err = await _rclone("lsjson", f"{remote}:{path}", "--no-modtime")
    if rc == 0:
        try:
            return (True, "") if json.loads(out or "[]") else (False, "")
        except ValueError:
            return None, "rclone çıktısı okunamadı"
    if rc in _NOT_FOUND_CODES:
        return False, ""
    return None, err[:200] or f"rclone çıkış kodu {rc}"


async def _find_missing_rclone(remote: str, records: List[dict]) -> Tuple[List[dict], List[dict]]:
    by_dir: Dict[str, list] = {}
    unknown: List[dict] = []
    for r in records:
        _, path = _rclone_parts(r)
        if not path:
            unknown.append({"path": r.get("rclone_path", ""), "reason": "Boş yol"})
            continue
        by_dir.setdefault(posixpath.dirname(path), []).append((path, r))

    sem = asyncio.Semaphore(_DIR_CONCURRENCY)

    async def check_dir(parent: str, items: list):
        async with sem:
            rc, out, err = await _rclone("lsjson", f"{remote}:{parent}", "--files-only", "--no-modtime")
            if rc in _NOT_FOUND_CODES:
                # Üst dizin yok (remote kökü zaten erişilebilir doğrulandı) → hepsi yok
                return [r for _, r in items], []
            if rc != 0:
                reason = f"Klasör listelenemedi: {err[:200] or rc}"
                return [], [{"path": p, "reason": reason} for p, _ in items]
            try:
                names = {_nfc(i.get("Name", "")) for i in json.loads(out or "[]")}
            except ValueError:
                return [], [{"path": p, "reason": "rclone çıktısı okunamadı"} for p, _ in items]

            miss, unk = [], []
            for path, r in items:
                if _nfc(posixpath.basename(path)) in names:
                    continue
                exists, reason = await _rclone_file_exists(remote, path)
                if exists is False:
                    miss.append(r)
                elif exists is None:
                    unk.append({"path": f"{remote}:{path}", "reason": reason})
            return miss, unk

    results = await asyncio.gather(*(check_dir(d, it) for d, it in by_dir.items()))
    missing: List[dict] = []
    for miss, unk in results:
        missing.extend(miss)
        unknown.extend(unk)
    return missing, unknown


async def _rclone_root_ok(remote: str) -> Optional[str]:
    """None = erişilebilir, aksi halde sebep metni."""
    if not remote:
        return "Kayıtta remote adı yok"
    if not RCLONE_CONF_PATH.exists():
        return "rclone.conf bulunamadı"
    try:
        rc, _, err = await _rclone("lsd", f"{remote}:")
    except RuntimeError as e:
        return str(e)
    return None if rc == 0 else (err[:200] or f"rclone çıkış kodu {rc}")


# ── Google Drive ──────────────────────────────────────────────────────────────

def _gdrive_service():
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request as GRequest
    if not GDRIVE_TOKEN_PATH.exists():
        raise FileNotFoundError("gdrive_token.pickle bulunamadı")
    with open(GDRIVE_TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        creds.refresh(GRequest())
        with open(GDRIVE_TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _gdrive_check_sync(records: List[dict]) -> Tuple[List[dict], List[dict], str]:
    """
    (eksik_kayıtlar, belirsiz_listesi, atlama_sebebi) döndürür.
    Drive'a erişilemiyorsa atlama_sebebi dolu gelir ve hiçbir kayıt eksik sayılmaz.
    """
    from googleapiclient.errors import HttpError
    try:
        svc = _gdrive_service()
        svc.files().get(fileId="root", fields="id").execute()   # kimlik/erişim testi
    except Exception as e:
        return [], [], f"Google Drive erişilemedi: {str(e)[:200]}"

    missing, unknown = [], []
    for r in records:
        fid = (r.get("file_id") or "").strip()
        if not fid:
            unknown.append({"path": r.get("file_name", ""), "reason": "file_id yok"})
            continue
        try:
            meta = svc.files().get(
                fileId=fid, fields="id,trashed", supportsAllDrives=True
            ).execute()
            if meta.get("trashed"):
                missing.append(r)
        except HttpError as e:
            if getattr(e.resp, "status", None) == 404:
                missing.append(r)
            else:
                unknown.append({"path": r.get("file_name", fid),
                                "reason": f"Drive HTTP {getattr(e.resp, 'status', '?')}"})
        except Exception as e:
            unknown.append({"path": r.get("file_name", fid), "reason": str(e)[:200]})
    return missing, unknown, ""


# ── Silme ─────────────────────────────────────────────────────────────────────

async def _remove_record(rec: dict) -> bool:
    """Katalog girdisini ve ekle_approved kaydını kaldırır."""
    if rec.get("db_id"):
        try:
            # Son kalite/bölüm silinirse bölüm → sezon → dizi/film de temizlenir.
            await db.delete_media_by_stream_id(rec["db_id"])
        except Exception:
            LOGGER.warning("[cloud-sync] katalog girdisi silinemedi: %s",
                           rec.get("file_name"), exc_info=True)
            return False
    try:
        await db.dbs[rec["_storage"]][APPROVED_COLLECTION].delete_one({"_id": rec["_id"]})
    except Exception:
        LOGGER.warning("[cloud-sync] approved kaydı silinemedi: %s",
                       rec.get("file_name"), exc_info=True)
        return False
    return True


def _brief(rec: dict) -> dict:
    if _is_rclone(rec):
        remote, path = _rclone_parts(rec)
        shown = f"{remote}:{path}"
    else:
        shown = rec.get("file_name") or rec.get("file_id", "")
    return {
        "doc_id": str(rec.get("_id")),
        "title": rec.get("title") or rec.get("file_name") or "",
        "path": shown,
        "source": "rclone" if _is_rclone(rec) else "gdrive",
    }


# ── Ana işlev ─────────────────────────────────────────────────────────────────

async def sync_cloud(source: Optional[str] = None, dry_run: bool = False,
                     force: bool = False) -> dict:
    """
    rclone / Google Drive'dan silinmiş dosyaların katalogdaki kayıtlarını temizler.

    source   : None = hepsi, "rclone" veya "gdrive" ile kısıtlanabilir.
    dry_run  : True → hiçbir şey silmez, ne silineceğini raporlar.
    force    : True → toplu silme güvenlik eşiğini yok sayar.
    """
    if _lock.locked():
        return {"error": "Senkronizasyon zaten çalışıyor, bitmesini bekleyin"}

    async with _lock:
        report = {
            "dry_run": dry_run, "groups": [],
            "checked": 0, "missing": 0, "unknown": 0, "removed": 0, "failed": 0, "blocked": 0,
        }
        grouped = await _collect_records()

        for group, records in grouped.items():
            kind = "gdrive" if group == "gdrive" else "rclone"
            if source and source != kind:
                continue

            entry = {
                "group": group, "source": kind, "checked": len(records),
                "missing": [], "unknown": [], "removed": 0, "failed": 0,
                "blocked": False, "skipped": "",
            }
            report["groups"].append(entry)
            report["checked"] += len(records)

            try:
                if kind == "gdrive":
                    loop = asyncio.get_running_loop()
                    missing, unknown, skip = await loop.run_in_executor(
                        None, lambda recs=records: _gdrive_check_sync(recs))
                    if skip:
                        entry["skipped"] = skip
                        LOGGER.warning("[cloud-sync] gdrive atlandı: %s", skip)
                        continue
                else:
                    remote = group.split(":", 1)[1]
                    why = await _rclone_root_ok(remote)
                    if why:
                        entry["skipped"] = f"Remote erişilemedi: {why}"
                        LOGGER.warning("[cloud-sync] %s atlandı: %s", group, why)
                        continue
                    missing, unknown = await _find_missing_rclone(remote, records)
            except Exception:
                LOGGER.error("[cloud-sync] %s kontrol hatası", group, exc_info=True)
                entry["skipped"] = "Kontrol sırasında beklenmeyen hata"
                continue

            entry["unknown"] = unknown
            report["unknown"] += len(unknown)
            entry["missing"] = [_brief(r) for r in missing]
            report["missing"] += len(missing)

            if not missing:
                continue

            # Toplu silme koruması
            if (not force and len(missing) >= SAFETY_MIN_MISSING
                    and len(missing) / max(len(records), 1) >= SAFETY_RATIO):
                entry["blocked"] = True
                report["blocked"] += len(missing)
                LOGGER.warning(
                    "[cloud-sync] %s: %d/%d kayıt eksik görünüyor — güvenlik eşiği nedeniyle "
                    "otomatik silme durduruldu (remote/token bağlantısını kontrol edin).",
                    group, len(missing), len(records),
                )
                continue

            if dry_run:
                continue

            for rec in missing:
                if await _remove_record(rec):
                    entry["removed"] += 1
                    LOGGER.info("[cloud-sync] kaldırıldı: %s", _brief(rec)["path"])
                else:
                    entry["failed"] += 1
            report["removed"] += entry["removed"]
            report["failed"] += entry["failed"]

        if report["removed"]:
            try:
                from Backend.helper.platform_catalog import platform_catalog
                platform_catalog.schedule_refresh()
            except Exception as e:
                LOGGER.warning("[cloud-sync] Katalog yenileme planlanamadı: %s", e)

        LOGGER.info(
            "[cloud-sync] Tamamlandı%s — kontrol: %d, eksik: %d, silinen: %d, hata: %d, eşik nedeniyle bekleyen: %d",
            " (dry-run)" if dry_run else "", report["checked"], report["missing"],
            report["removed"], report["failed"], report["blocked"],
        )
        return report


# ── Periyodik çalıştırıcı ─────────────────────────────────────────────────────

class CloudSyncChecker:
    """WebDAVSyncChecker ile aynı desen."""

    def __init__(self, interval_hours: float = 6, first_delay_seconds: int = 180):
        self.interval = max(float(interval_hours), 0.05) * 3600
        self.first_delay = first_delay_seconds
        self.is_running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        LOGGER.info("[cloud-sync] Arka plan senkronizasyonu başladı (aralık: %.2f saat)",
                    self.interval / 3600)
        self._task = asyncio.create_task(self._run_loop())

    def stop(self):
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run_loop(self):
        await asyncio.sleep(self.first_delay)
        while self.is_running:
            try:
                await sync_cloud()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.error("[cloud-sync] döngü hatası", exc_info=True)
            await asyncio.sleep(self.interval)


_checker: Optional[CloudSyncChecker] = None


def configure_cloud_sync_interval(hours, startup: bool = False) -> str:
    """
    Arka plan senkronizasyonunu verilen aralığa (saat) göre başlatır / yeniden kurar / kapatır.
    hours <= 0 → kapalı. Çalışan bir event loop içinde çağrılmalıdır.
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
        LOGGER.info("[cloud-sync] Otomatik senkronizasyon kapalı (aralık: 0).")
        return "rclone/Drive otomatik senkronizasyonu kapatıldı"

    first_delay = 180 if startup else int(hours * 3600)
    _checker = CloudSyncChecker(hours, first_delay_seconds=first_delay)
    asyncio.get_running_loop().create_task(_checker.start())
    return f"rclone/Drive senkronizasyonu her {hours:g} saatte bir çalışacak"
