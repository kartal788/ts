"""
sunucuyayukle.py
================
/sunucuyayukle <adet>              — Zip/7z dosyalarını indir, çıkar, veritabanına kaydet.
/sunucuyayukle <URL>               — URL'den zip indir, çıkar, veritabanına kaydet.
/sunucuyayukle <URL> <dosya.mkv>   — URL'den dosyayı belirtilen adla indir.
/sunucudansil                      — Sunucu dosya yöneticisinde gezinip dosya/klasör sil.
/iptal                             — Aktif işlemi iptal et.

Birden fazla /sunucuyayukle isteği kuyruğa alınır ve sırayla işlenir.
Tüm görevler tek bir mesaj üzerinden düzenlenerek takip edilir.
"""

import asyncio
import re
import shutil
import time
import traceback
import zipfile
from pathlib import Path

import aiohttp
import asyncio as _asyncio_gdrive
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from Backend import db
from Backend.helper.custom_filter import CustomFilters
from Backend.helper.metadata import metadata
from Backend.pyrofork.plugins.reciever import _archive_to_video_name
from Backend.helper.pyro import clean_filename, get_readable_file_size, remove_urls
from Backend.logger import LOGGER
from Backend.fastapi.routes.sunucu_routes import SUNUCU_DIR

# Bot'un dosyaları indirip çıkardığı çalışma dizini — FastAPI sunucu rotaları ile aynı
WORK_DIR = SUNUCU_DIR
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Google Drive token.pickle yolu (ayarlar.py üzerinden yüklenir)
GDRIVE_TOKEN_PATH = Path(__file__).parent.parent.parent.parent / "gdrive_token.pickle"

# config.env yolu — kuyruk limitleri için
_CONFIG_PATH = Path("config.env")

def _read_concurrency_limits() -> tuple[int, int]:
    """
    config.env'den MAX_CONCURRENT_DOWNLOADS ve MAX_CONCURRENT_UPLOADS okur.
    Boş veya 0 → sınırsız (999 olarak döner).
    """
    try:
        import re as _re
        text = _CONFIG_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        def _get(key):
            m = _re.search(rf'^{key}\s*=\s*["\']?(\d*)["\']?\s*(?:#.*)?$', text, _re.MULTILINE)
            if m:
                v = m.group(1).strip()
                return int(v) if v and v != "0" else 999
            return 999
        return _get("MAX_CONCURRENT_DOWNLOADS"), _get("MAX_CONCURRENT_UPLOADS")
    except Exception:
        return 999, 999

# Bot başlangıç zamanı — restart/ilk başlatmada sıfırlanır
BOT_START_TIME: float = time.time()

# ── Oturum takibi ─────────────────────────────────────────────────────────────
_SESSIONS: dict = {}
_SESSION_LOCKS: dict = {}
_ACTIVE_TASKS: dict = {}
_PROGRESS_LAST_UPDATE: dict = {}

# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL KUYRUK SİSTEMİ
# Her /sunucuyayukle isteği _QUEUE'ya girer; worker birer birer işler.
# Tüm görevler tek bir durum mesajı üzerinden edit_text ile güncellenir.
# ══════════════════════════════════════════════════════════════════════════════

_QUEUE: asyncio.Queue = None
_QUEUE_WORKER: asyncio.Task = None

# Her chat için tek durum mesajı: chat_id → Message
_STATUS_MSGS: dict = {}

# Görev durumu listesi (sıralı) — her öğe bir dict
_TASKS_STATE: list = []
_TASKS_LOCK: asyncio.Lock = None

# Sayfalama: chat_id → sayfa indexi (0-tabanlı)
_PAGE_STATE: dict = {}
ITEMS_PER_PAGE = 4

# ── Eşzamanlılık semaforları (config'den okunur, worker başlarken güncellenir) ──
_DL_SEMAPHORE: asyncio.Semaphore = None   # indirme limiti
_UL_SEMAPHORE: asyncio.Semaphore = None   # yükleme (DB kayıt) limiti


# ─── Kuyruk başlatma ──────────────────────────────────────────────────────────

def _ensure_queue():
    global _QUEUE, _TASKS_LOCK, _DL_SEMAPHORE, _UL_SEMAPHORE
    if _QUEUE is None:
        _QUEUE = asyncio.Queue()
    if _TASKS_LOCK is None:
        _TASKS_LOCK = asyncio.Lock()
    # Semaforlar henüz oluşturulmamışsa veya limit değişmişse yeniden oluştur
    max_dl, max_ul = _read_concurrency_limits()
    if _DL_SEMAPHORE is None or _DL_SEMAPHORE._value != max_dl:
        _DL_SEMAPHORE = asyncio.Semaphore(max_dl)
        LOGGER.info(f"[yukle] İndirme limiti: {max_dl if max_dl < 999 else 'sınırsız'}")
    if _UL_SEMAPHORE is None or _UL_SEMAPHORE._value != max_ul:
        _UL_SEMAPHORE = asyncio.Semaphore(max_ul)
        LOGGER.info(f"[yukle] Yükleme limiti: {max_ul if max_ul < 999 else 'sınırsız'}")


def _start_worker():
    global _QUEUE_WORKER
    _ensure_queue()
    if _QUEUE_WORKER is None or _QUEUE_WORKER.done():
        _QUEUE_WORKER = asyncio.create_task(_queue_worker())
        LOGGER.info("[yukle] Kuyruk worker başlatıldı.")


# ─── Görev durumu yönetimi ────────────────────────────────────────────────────

async def _task_set(session_id: str, **kwargs):
    """Görev durumunu günceller; yoksa ekler."""
    if _TASKS_LOCK is None:
        return
    async with _TASKS_LOCK:
        for i, t in enumerate(_TASKS_STATE):
            if t["session_id"] == session_id:
                _TASKS_STATE[i].update(kwargs)
                return
        _TASKS_STATE.append({"session_id": session_id, **kwargs})


async def _task_remove(session_id: str):
    if _TASKS_LOCK is None:
        return
    async with _TASKS_LOCK:
        for i, t in enumerate(_TASKS_STATE):
            if t["session_id"] == session_id:
                _TASKS_STATE.pop(i)
                return


# ─── Yardımcı fonksiyonlar ────────────────────────────────────────────────────

def _human(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.2f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f}TB"


def _readable_time(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:    parts.append(f"{days}g")
    if hours:   parts.append(f"{hours}s")
    if minutes: parts.append(f"{minutes}d")
    parts.append(f"{secs}s")
    return "".join(parts)


def _disk_usage_str(path: Path) -> str:
    try:
        total, used, free = shutil.disk_usage(path)
        return f"🖥 Disk: {_human(used)} / {_human(total)} (Boş: {_human(free)})"
    except Exception:
        return "🖥 Disk: bilinmiyor"


def _bar(done: int, total: int, width: int = 14) -> str:
    if total <= 0:
        return "░" * width
    filled = int(width * done / total)
    return "█" * filled + "░" * (width - filled)


_ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".z01", ".z02", ".z03"}
_ARCHIVE_RE = re.compile(r'\.(zip|7z|rar|z\d+)(\.\d+)?$', re.IGNORECASE)
_ARCHIVE_TRUNC_RE = re.compile(r'[._]zip\.\d+$', re.IGNORECASE)
_ARCHIVE_7Z_TRUNC_RE = re.compile(r'[._]7z\.\d+$', re.IGNORECASE)

# Parçalı arşiv tespiti: .zip.001, .7z.001, .z01, .part1.rar, .part01.rar vb.
_MULTIPART_RE = re.compile(
    r'('
    r'\.(zip|7z|rar)\.\d+'          # .zip.001  .7z.001  .rar.001
    r'|\.z\d{2,}'                    # .z01  .z02  …
    r'|\.part\d+\.rar'              # .part1.rar  .part01.rar
    r'|[._](zip|7z)\.\d+'           # _zip.001  .7z.002
    r')',
    re.IGNORECASE,
)


def _is_multipart_archive(name: str) -> bool:
    """
    Dosya adının parçalı bir arşive ait olup olmadığını döner.
    .zip.001, .7z.001, .z01, .part1.rar gibi formatları tanır.
    Parçalı arşivler için indirme limiti her zaman 1 olarak uygulanır.
    """
    return bool(_MULTIPART_RE.search(name))


def _is_archive(name: str) -> bool:
    name_lower = name.lower()
    if any(name_lower.endswith(ext) for ext in _ARCHIVE_EXTS):
        return True
    if _ARCHIVE_RE.search(name_lower):
        return True
    if _ARCHIVE_TRUNC_RE.search(name_lower):
        return True
    if _ARCHIVE_7Z_TRUNC_RE.search(name_lower):
        return True
    return False


def _is_zip_or_7z(doc) -> bool:
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if name.endswith((".zip", ".7z")):
        return True
    if re.search(r'\.(zip|7z)\.\d+$', name):
        return True
    if re.search(r'\.z\d+$', name):
        return True
    if _ARCHIVE_TRUNC_RE.search(name):
        return True
    if _ARCHIVE_7Z_TRUNC_RE.search(name):
        return True
    if mime in ("application/zip", "application/x-7z-compressed",
                "application/x-zip-compressed"):
        return True
    return False


# ─── Durum mesajı render ──────────────────────────────────────────────────────

def _sys_info_lines() -> str:
    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=None)
        mem  = psutil.virtual_memory()
        disk = shutil.disk_usage(WORK_DIR)
        free_gb  = disk.free / 1_073_741_824
        free_pct = disk.free / disk.total * 100

        # Bot başlatma zamanından itibaren uptime (restart'ta sıfırlanır)
        total_sec = int(time.time() - BOT_START_TIME)
        d_up, rem = divmod(total_sec, 86400)
        h, rem    = divmod(rem, 3600)
        m, s      = divmod(rem, 60)
        if d_up:
            up_str = f"{d_up}g {h:02d}sa {m:02d}dk {s:02d}sn"
        else:
            up_str = f"{h}sa {m:02d}dk {s:02d}sn"

        return (
            f"┟ CPU → {cpu:.1f}% | Boş → {free_gb:.2f}GB [{free_pct:.1f}%]\n"
            f"┖ RAM → {mem.percent:.1f}% | Süre → {up_str}"
        )
    except Exception:
        return ""


def _render_queue_msg(chat_id: int) -> str:
    tasks = [t for t in _TASKS_STATE if t.get("chat_id") == chat_id]
    if not tasks:
        return "✅ <b>Kuyruk boş.</b>"

    total_tasks = len(tasks)
    page        = _PAGE_STATE.get(chat_id, 0)
    total_pages = max(1, -(-total_tasks // ITEMS_PER_PAGE))
    page        = max(0, min(page, total_pages - 1))
    _PAGE_STATE[chat_id] = page

    page_tasks = tasks[page * ITEMS_PER_PAGE: (page + 1) * ITEMS_PER_PAGE]
    lines = []

    for i, t in enumerate(page_tasks, start=page * ITEMS_PER_PAGE + 1):
        fname      = t.get("fname", "?")
        fname_s    = (fname[:52] + "…") if len(fname) > 52 else fname
        status     = t.get("status", "Bekliyor")
        pct        = float(t.get("pct", 0))
        processed  = t.get("processed", 0)
        total_sz   = t.get("total_size", 0)
        speed      = t.get("speed", 0)
        eta        = t.get("eta", "-")
        elapsed    = t.get("elapsed", "-")
        engine     = t.get("engine", "Sistem")
        mode_in    = t.get("mode_in", "#ZipDosya")
        session_id = t.get("session_id", "")
        cancel_cmd = f"/c_{session_id[:10]}" if session_id else "/iptal"

        # İlerleme çubuğu (12 hane)
        bar_width = 12
        filled = int(bar_width * pct / 100) if pct > 0 else 0
        bar = "⬢" * filled + "⬡" * (bar_width - filled)

        proc_str  = _human(processed)
        total_str = _human(total_sz) if total_sz else "?"
        speed_str = f"{_human(speed)}/s" if speed > 0 else "0B/s"

        if status == "Kuyrukta":
            q_pos    = t.get("queue_pos", "?")
            time_str = f"- / -  (sıra: {q_pos})"
        else:
            time_str = f"{elapsed} / {eta}"

        lines.append(f"<b>{i}. {fname_s}</b>")
        lines.append(f"┟ [{bar}] {pct:.2f}%")
        lines.append(f"┠ İşlendi → {proc_str} / {total_str}")
        lines.append(f"┠ Durum   → {status}")
        lines.append(f"┠ Hız     → {speed_str}")
        lines.append(f"┠ Süre    → {time_str}")
        lines.append(f"┠ Motor   → {engine}")
        lines.append(f"┠ Mod     → {mode_in}")
        lines.append(f"┖ Durdur  → {cancel_cmd}")
        lines.append("")

    _waiting_statuses = {"Kuyrukta", "Dosya Bekleniyor", "Yükleme Bekleniyor"}
    running = sum(1 for t in tasks if t.get("status") not in _waiting_statuses)
    waiting = sum(1 for t in tasks if t.get("status") in _waiting_statuses)
    max_dl, max_ul = _read_concurrency_limits()
    # Parçalı arşiv içeren aktif görev varsa indirme limiti göstergesi 1'e düşer
    has_multipart_active = any(
        t.get("is_multipart") and t.get("status") not in {"Kuyrukta", "Dosya Bekleniyor", "Yükleme Bekleniyor"}
        for t in tasks
    )
    dl_effective = 1 if has_multipart_active else max_dl
    dl_str = str(dl_effective) if dl_effective < 999 else "∞"
    ul_str = str(max_ul) if max_ul < 999 else "∞"
    lines.append(f"⌬ Bot Durumu: Sayfa {page+1}/{total_pages}")
    lines.append(
        f"| Görev: {total_tasks} | Aktif: {running} | Bekleyen: {waiting} | ⬇️{dl_str} ⬆️{ul_str}"
    )
    
    sys_line = _sys_info_lines()
    if sys_line:
        lines.append(sys_line)

    return "\n".join(lines)


def _page_kb(chat_id: int):
    tasks = [t for t in _TASKS_STATE if t.get("chat_id") == chat_id]
    total = len(tasks)
    if total <= ITEMS_PER_PAGE:
        return None
    page        = _PAGE_STATE.get(chat_id, 0)
    total_pages = -(-total // ITEMS_PER_PAGE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Önceki", callback_data=f"yukleq:prev:{chat_id}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="yukleq:noop:0"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Sonraki ▶", callback_data=f"yukleq:next:{chat_id}"))
    return InlineKeyboardMarkup([nav]) if nav else None


_PUSH_LAST: dict = {}  # chat_id → last push timestamp

async def _replace_status_msg(chat_id: int, client, text: str, kb):
    """Eski durum mesajını siler, yeni mesajı bota gönderir ve _STATUS_MSGS'i günceller."""
    old_msg = _STATUS_MSGS.pop(chat_id, None)
    if old_msg:
        try:
            await old_msg.delete()
        except Exception:
            pass
    try:
        new_msg = await client.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb
        )
        _STATUS_MSGS[chat_id] = new_msg
        _PUSH_LAST[chat_id] = time.time()
    except Exception as e:
        LOGGER.warning(f"[yukle] Durum mesajı gönderilemedi: {e}")


async def _push_status(chat_id: int, client=None, force: bool = False):
    """Durum mesajını düzenler; mesaj yoksa yeni gönderir. 15 saniyede bir edit yapar."""
    now = time.time()
    # 15 saniye throttle — force=True olsa da bu kuralı koru
    # (sadece yeni mesaj gönderme durumunda force dikkate alınır)
    elapsed = now - _PUSH_LAST.get(chat_id, 0)
    if elapsed < 15 and not force:
        return
    # force=True ama 3 saniye bile geçmemişse yine de atla (spam önleme)
    if elapsed < 3:
        return
    _PUSH_LAST[chat_id] = now

    # Kuyrukta görev yoksa mevcut mesajı güncelleme, yeni mesaj da gönderme
    tasks_exist = any(t.get("chat_id") == chat_id for t in _TASKS_STATE)
    if not tasks_exist:
        return

    text = _render_queue_msg(chat_id)
    kb   = _page_kb(chat_id)
    msg  = _STATUS_MSGS.get(chat_id)

    if msg:
        try:
            await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except Exception:
            _STATUS_MSGS.pop(chat_id, None)  # Geçersiz mesajı temizle

    if client:
        try:
            new_msg = await client.send_message(
                chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb
            )
            _STATUS_MSGS[chat_id] = new_msg
        except Exception as e:
            LOGGER.warning(f"[yukle] Durum mesajı gönderilemedi: {e}")


# ─── Sayfalama callback ───────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^yukleq:"))
async def cb_yukleq(client: Client, query: CallbackQuery):
    parts  = query.data.split(":")
    action = parts[1] if len(parts) > 1 else "noop"
    try:
        chat_id = int(parts[2]) if len(parts) > 2 else query.message.chat.id
    except Exception:
        chat_id = query.message.chat.id

    if action == "prev":
        _PAGE_STATE[chat_id] = max(0, _PAGE_STATE.get(chat_id, 0) - 1)
    elif action == "next":
        total    = sum(1 for t in _TASKS_STATE if t.get("chat_id") == chat_id)
        max_page = max(0, -(-total // ITEMS_PER_PAGE) - 1)
        _PAGE_STATE[chat_id] = min(max_page, _PAGE_STATE.get(chat_id, 0) + 1)

    try:
        await query.message.edit_text(
            _render_queue_msg(chat_id),
            parse_mode=ParseMode.HTML,
            reply_markup=_page_kb(chat_id),
        )
    except Exception:
        pass
    await query.answer()


# ─── Kuyruk worker ────────────────────────────────────────────────────────────

async def _run_one_task(item: dict):
    """
    Tek bir görevi indirme + yükleme semaforlarıyla kontrollü olarak çalıştırır.
    Bu fonksiyon _queue_worker tarafından asyncio.create_task() ile paralel başlatılır.
    """
    client       = item["client"]
    uid          = item["uid"]
    session      = item["session"]
    orig_message = item["orig_message"]
    session_id   = session["session_id"]
    chat_id      = session["chat_id"]

    _ensure_queue()

    # Kuyruktan çıkmadan önce iptal edilmiş olabilir (kuyruk iptali)
    if session.get("cancelled"):
        await _task_remove(session_id)
        return

    # Parçalı arşiv (zip.001, 7z.001, z01 vb.) ise indirme limiti her zaman 1.
    # Bu dosyalar birbiriyle bağlantılı olduğundan paralel indirme anlamsız.
    is_multipart = session.get("is_multipart", False)
    dl_sem = asyncio.Semaphore(1) if is_multipart else _DL_SEMAPHORE

    # İndirme semaforunu al (indirme + çıkarma aşaması)
    async with dl_sem:
        await _task_set(session_id, status="İndiriliyor")
        # Kuyrukta bekleyenlerin sırasını güncelle
        pos = 1
        for t in _TASKS_STATE:
            if t.get("chat_id") == chat_id and t.get("status") == "Kuyrukta":
                await _task_set(t["session_id"], queue_pos=pos)
                pos += 1
        await _push_status(chat_id, client, force=True)

        try:
            task = asyncio.create_task(
                _process_session_download(client, uid, session, orig_message)
            )
            _ACTIVE_TASKS[session_id] = task
            try:
                await task
            except asyncio.CancelledError:
                LOGGER.info(f"[yukle:{session_id}] İndirme task iptal edildi.")
                return
            finally:
                _ACTIVE_TASKS.pop(session_id, None)
        except Exception as e:
            LOGGER.error(f"[yukle:{session_id}] İndirme hatası: {e}")
            await _task_set(session_id, status="❌ İndirme Hatası")
            await _push_status(chat_id, client, force=True)
            return

    # İptal kontrolü — indirme bitti, yükleme başlamadan önce
    if session.get("cancelled"):
        await _cleanup_task(session_id, uid, session, chat_id, client)
        return

    # Yükleme semaforunu al (metadata + DB kayıt aşaması)
    async with _UL_SEMAPHORE:
        # Semafor bekleme sırasında da iptal olmuş olabilir
        if session.get("cancelled"):
            await _cleanup_task(session_id, uid, session, chat_id, client)
            return

        await _task_set(session_id, status="Yükleme Sırasında")
        await _push_status(chat_id, client, force=True)

        try:
            task = asyncio.create_task(
                _process_session_upload(client, uid, session, orig_message)
            )
            _ACTIVE_TASKS[session_id] = task
            try:
                await task
            except asyncio.CancelledError:
                LOGGER.info(f"[yukle:{session_id}] Yükleme task iptal edildi.")
                await _cleanup_task(session_id, uid, session, chat_id, client)
                return
            finally:
                _ACTIVE_TASKS.pop(session_id, None)
        except Exception as e:
            LOGGER.error(f"[yukle:{session_id}] Yükleme hatası: {e}")
            await _task_set(session_id, status="❌ Yükleme Hatası")
            await _push_status(chat_id, client, force=True)

    await asyncio.sleep(5)   # Final durumu görünsün
    await _task_remove(session_id)
    tasks_left = [t for t in _TASKS_STATE if t.get("chat_id") == chat_id]
    if not tasks_left:
        status_msg = _STATUS_MSGS.pop(chat_id, None)
        _PAGE_STATE.pop(chat_id, None)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
    else:
        await _push_status(chat_id, client, force=True)


async def _cleanup_task(session_id: str, uid: int, session: dict,
                        chat_id: int, client):
    """İptal/hata sonrası temizlik."""
    shutil.rmtree(session.get("session_dir", Path("/tmp/__none__")), ignore_errors=True)
    _SESSIONS.pop(uid, None)
    _SESSION_LOCKS.pop(session_id, None)
    await _task_remove(session_id)
    tasks_left = [t for t in _TASKS_STATE if t.get("chat_id") == chat_id]
    if not tasks_left:
        status_msg = _STATUS_MSGS.pop(chat_id, None)
        _PAGE_STATE.pop(chat_id, None)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
    else:
        await _push_status(chat_id, client, force=True)


async def _queue_worker():
    """
    Kuyruktan görevleri alır ve her birini _run_one_task ile paralel başlatır.
    Semaforlar indirme ve yükleme paralelliğini ayrı ayrı kontrol eder.
    Örn: MAX_CONCURRENT_DOWNLOADS=2, MAX_CONCURRENT_UPLOADS=1
      → Aynı anda 2 indirme yapılabilir, ama DB'ye kayıt sırayla gider.
    """
    _ensure_queue()
    while True:
        try:
            item = await _QUEUE.get()
            # Her görevi paralel başlat — semaforlar içeride beklemeyi yönetir
            asyncio.create_task(_run_one_task(item))
            _QUEUE.task_done()

            # Kuyrukta bekleyen görevlerin sıra numaralarını güncelle
            chat_id = item["session"]["chat_id"]
            pos = 1
            for t in _TASKS_STATE:
                if t.get("chat_id") == chat_id and t.get("status") == "Kuyrukta":
                    await _task_set(t["session_id"], queue_pos=pos)
                    pos += 1

        except asyncio.CancelledError:
            break
        except Exception as e:
            LOGGER.error(f"[yukle] Worker döngüsü hatası: {e}")


# ─── İlerleme callback (Telegram indirme) ─────────────────────────────────────

class _ZipCancelled(Exception):
    pass


async def _progress_cb(current: int, total: int, session_id: str,
                       fname: str, start: float, client, chat_id: int):
    for sess in _SESSIONS.values():
        if sess.get("session_id") == session_id and sess.get("cancelled"):
            raise _ZipCancelled("İptal edildi")

    now = time.time()
    key  = f"{session_id}_dl"
    last = _PROGRESS_LAST_UPDATE.get(key, 0)
    if now - last < 15 and current != total:
        return
    _PROGRESS_LAST_UPDATE[key] = now

    elapsed_sec = max(0.001, now - start)
    speed       = int(current / elapsed_sec)
    pct         = current / total * 100 if total else 0
    elapsed_str = _readable_time(int(elapsed_sec))
    eta_str     = _readable_time(int((total - current) / speed) if speed > 0 else 0)

    await _task_set(
        session_id,
        pct=pct, processed=current, total_size=total,
        speed=speed, eta=eta_str, elapsed=elapsed_str,
        status="İndiriliyor", engine="Pyrogram", mode_in="#TgDosya",
    )
    await _push_status(chat_id, client)


# ─── Google Drive yardımcıları ────────────────────────────────────────────────

def _gdrive_file_id(url: str) -> str | None:
    """
    Çeşitli Drive URL formatlarından file_id çıkarır.
    Desteklenen formatlar:
      https://drive.google.com/file/d/<ID>/view
      https://drive.google.com/open?id=<ID>
      https://drive.google.com/uc?id=<ID>
    """
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]{25,})",
        r"[?&]id=([a-zA-Z0-9_-]{25,})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _is_gdrive_url(url: str) -> bool:
    return "drive.google.com" in url


def _ensure_gdrive_packages():
    """google-api-python-client ve google-auth paketlerini gerekirse yukler."""
    import importlib, subprocess, sys, shutil
    pkgs = {
        "googleapiclient": "google-api-python-client",
        "google.auth":     "google-auth",
    }
    for module, pip_name in pkgs.items():
        try:
            importlib.import_module(module)
        except ImportError:
            LOGGER.info(f"[gdrive] {pip_name} yukleniyor...")
            # uv venv icinde "uv pip install" kullan; yoksa pip ile dene
            uv_bin = shutil.which("uv") or "/app/.venv/bin/uv"
            if shutil.which("uv") or __import__("os").path.exists(uv_bin):
                cmd = [uv_bin, "pip", "install", pip_name]
            else:
                cmd = [sys.executable, "-m", "pip", "install",
                       "--break-system-packages", "--quiet", pip_name]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"{pip_name} yuklenemedi.\n"
                    f"Cmd: {' '.join(cmd)}\n"
                    f"Stderr: {result.stderr[:300]}"
                )
            LOGGER.info(f"[gdrive] {pip_name} yuklendi.")
            # Yeni yuklenen modulu importlib cache'den temizle
            if module in sys.modules:
                del sys.modules[module]


def _get_gdrive_service():
    """
    token.pickle dan kimlik dogrulanmis Google Drive servisi doner.
    google-auth ve google-api-python-client paketleri otomatik yuklenir.
    """
    import pickle
    _ensure_gdrive_packages()
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request

    if not GDRIVE_TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"token.pickle bulunamadı: {GDRIVE_TOKEN_PATH}\n"
            "/ayarlar → 📁 Dosya Ekle → token.pickle Yükle"
        )

    with open(GDRIVE_TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)

    # Token süresi dolmuşsa yenile
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GDRIVE_TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)

    return build("drive", "v3", credentials=creds, cache_discovery=False)


async def _download_gdrive(file_id: str, dest: Path, session_id: str,
                           client, chat_id: int) -> tuple:
    """
    Google Drive dosyasını dest konumuna indirir.
    İlerlemeyi _task_set / _push_status ile günceller.
    (ok, hata_mesajı) döner.
    """
    try:
        import io
        _ensure_gdrive_packages()
        from googleapiclient.http import MediaIoBaseDownload

        loop = _asyncio_gdrive.get_event_loop()

        # Servis ve metadata'yı thread pool'da çalıştır
        def _get_meta():
            svc = _get_gdrive_service()
            meta = svc.files().get(fileId=file_id, fields="name,size").execute()
            return svc, meta

        svc, meta = await loop.run_in_executor(None, _get_meta)
        fname  = meta.get("name", dest.name)
        total  = int(meta.get("size", 0))

        # dest'i dosya adıyla güncelle (eğer custom_fname yoksa)
        if dest.is_dir() or not dest.suffix:
            dest = dest.parent / fname

        await _task_set(session_id,
                        fname=fname, total_size=total,
                        status="İndiriliyor", engine="GDrive", mode_in="#GDrive",
                        pct=0, processed=0, speed=0, eta="-", elapsed="-")
        await _push_status(chat_id, client, force=True)

        start      = time.time()
        done_bytes = 0

        # Thread ile async köprüsü: ilerleme queue üzerinden taşınır
        progress_q: asyncio.Queue = asyncio.Queue()

        def _run_download():
            nonlocal done_bytes
            request    = svc.files().get_media(fileId=file_id)
            last_chunk = time.time()
            try:
                with open(dest, "wb") as fout:
                    downloader = MediaIoBaseDownload(fout, request, chunksize=8 * 1024 * 1024)
                    finished   = False
                    while not finished:
                        sess = next(
                            (s for s in _SESSIONS.values()
                             if s.get("session_id") == session_id), None
                        )
                        if sess and sess.get("cancelled"):
                            # İptal sinyali: progress_q'yu None ile kapat, sessizce çık
                            loop.call_soon_threadsafe(progress_q.put_nowait, None)
                            return

                        dl_status, finished = downloader.next_chunk()
                        done_bytes = int(dl_status.resumable_progress) if total else done_bytes
                        now        = time.time()
                        if now - last_chunk >= 15:
                            last_chunk = now
                            elapsed    = max(0.001, now - start)
                            speed      = int(done_bytes / elapsed)
                            pct        = done_bytes / total * 100 if total else 0
                            eta_secs   = int((total - done_bytes) / speed) if speed > 0 and total else 0
                            loop.call_soon_threadsafe(
                                progress_q.put_nowait,
                                dict(
                                    pct=pct, processed=done_bytes, total_size=total,
                                    speed=speed,
                                    eta=_readable_time(eta_secs),
                                    elapsed=_readable_time(int(elapsed)),
                                    status="Indiriliyor", engine="GDrive", mode_in="#GDrive",
                                )
                            )
            except Exception as _thread_err:
                # Thread içi hata — progress_q'yu None ile kapat, hatayı ilet
                loop.call_soon_threadsafe(progress_q.put_nowait, None)
                raise _thread_err
            # Bitis sinyali
            loop.call_soon_threadsafe(progress_q.put_nowait, None)

        # Indirme thread'ini baslat
        dl_future = loop.run_in_executor(None, _run_download)

        # Ana event loop'ta ilerlemeyi isle
        while True:
            item = await progress_q.get()
            if item is None:
                break
            await _task_set(session_id, **item)
            await _push_status(chat_id, client, force=True)

        # Future'ı her zaman await et — exception varsa yukarı taşır,
        # iptal durumunda ise sessizce None döner (thread zaten çıktı).
        try:
            await dl_future
        except Exception as _fut_err:
            # Thread'den gelen gerçek hata (iptal değil)
            return False, f"{type(_fut_err).__name__}: {_fut_err}"

        return True, ""
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ─── URL'den indirme ──────────────────────────────────────────────────────────

async def _download_url(url: str, dest: Path, session_id: str,
                        client, chat_id: int) -> tuple:
    """URL'den dosyayı dest'e indirir. (ok, hata_mesajı) döner."""
    try:
        timeout = aiohttp.ClientTimeout(total=3600, connect=30)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(url) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}: {url}"
                total     = int(resp.headers.get("Content-Length", 0))
                done      = 0
                start     = time.time()
                last_push = 0.0
                with dest.open("wb") as fout:
                    async for chunk in resp.content.iter_chunked(512 * 1024):
                        sess = next(
                            (s for s in _SESSIONS.values()
                             if s.get("session_id") == session_id), None
                        )
                        if sess and sess.get("cancelled"):
                            raise asyncio.CancelledError("İptal edildi")
                        fout.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last_push >= 15:
                            last_push   = now
                            elapsed     = max(0.001, now - start)
                            speed       = int(done / elapsed)
                            pct         = done / total * 100 if total else 0
                            elapsed_str = _readable_time(int(elapsed))
                            eta_str     = _readable_time(
                                int((total - done) / speed) if speed > 0 and total else 0
                            )
                            await _task_set(
                                session_id,
                                pct=pct, processed=done, total_size=total,
                                speed=speed, eta=eta_str, elapsed=elapsed_str,
                                status="İndiriliyor", engine="HTTP", mode_in="#URL",
                            )
                            await _push_status(chat_id, client)
        return True, ""
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ─── Arşiv çıkarma ────────────────────────────────────────────────────────────

async def _extract_archive(session_dir: Path, files: list,
                           session_id: str, session: dict,
                           client=None, chat_id: int = 0):
    """Returns (video_path | None, error_str | None)"""
    video_exts   = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m4v"}
    files_sorted = sorted(files, key=lambda p: p.name.lower())

    if not files_sorted:
        return None, "Hiç arşiv dosyası bulunamadı."

    first = files_sorted[0].name.lower()
    loop  = asyncio.get_event_loop()

    def _cancelled():
        return (session is not None and session.get("cancelled")) or \
               next((s.get("cancelled") for s in _SESSIONS.values()
                     if s.get("session_id") == session_id), False)

    async def run_blocking(func, *args):
        return await loop.run_in_executor(None, func, *args)

    async def merge_parts(parts: list, dest: Path):
        CHUNK = 4 * 1024 * 1024
        total   = sum(p.stat().st_size for p in parts)
        written = [0]

        def _do_merge():
            with dest.open("wb") as out:
                for part in parts:
                    with part.open("rb") as src:
                        while True:
                            buf = src.read(CHUNK)
                            if not buf:
                                break
                            out.write(buf)
                            written[0] += len(buf)

        merge_task = asyncio.ensure_future(loop.run_in_executor(None, _do_merge))
        while not merge_task.done():
            await asyncio.sleep(3)
            if _cancelled():
                merge_task.cancel()
                raise asyncio.CancelledError("İptal edildi")
            pct = int(written[0] / total * 100) if total else 0
            await _task_set(session_id, status="Birleştiriliyor", pct=pct,
                            processed=written[0], total_size=total,
                            engine="Sistem", mode_in="#Arşiv")
            await _push_status(chat_id, client)
        await merge_task

    try:
        # ── ZIP ───────────────────────────────────────────────────────────────
        _is_zip_first = (
            re.search(r'\.(zip|z\d+)(\.\d+)?$', first)
            or first.endswith(".zip")
            or _ARCHIVE_TRUNC_RE.search(first)
        )
        if _is_zip_first:
            if len(files_sorted) > 1 or re.search(r'\.(zip|z)[._]\d+$', first) \
                    or _ARCHIVE_TRUNC_RE.search(first):
                merged = session_dir / "merged.zip"
                LOGGER.info(f"[yukle:{session_id}] {len(files_sorted)} parça birleştiriliyor")
                await merge_parts(files_sorted, merged)
                zip_path = merged
            else:
                zip_path = files_sorted[0]

            def _get_members():
                with zipfile.ZipFile(zip_path, "r") as zf:
                    return zf.infolist()

            try:
                members = await run_blocking(_get_members)
            except zipfile.BadZipFile as e:
                return None, f"Bozuk ZIP dosyası: {e}"

            total_size = sum(m.file_size for m in members) or 1
            last_pct   = -1

            await _task_set(session_id, status="ZIP Çıkarılıyor", pct=0,
                            engine="zipfile", mode_in="#Arşiv")
            await _push_status(chat_id, client)

            try:
                extracted = 0
                _session_dir_resolved = session_dir.resolve()
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for i, member in enumerate(members, 1):
                        if _cancelled():
                            raise asyncio.CancelledError("İptal edildi")
                        # ZIP Slip koruması: hedef yolu session_dir dışına çıkamaz
                        _safe_name = member.filename.replace("\\", "/")
                        _target = (_session_dir_resolved / _safe_name).resolve()
                        if not str(_target).startswith(str(_session_dir_resolved) + "/") and _target != _session_dir_resolved:
                            LOGGER.warning(f"[yukle:{session_id}] ZIP Slip engellendi: {member.filename!r}")
                            continue
                        await run_blocking(zf.extract, member, session_dir)
                        extracted += member.file_size
                        pct = int(extracted / total_size * 100)
                        if pct - last_pct >= 5 or i == len(members):
                            last_pct = pct
                            await _task_set(session_id, status="ZIP Çıkarılıyor",
                                            pct=pct, processed=extracted,
                                            total_size=total_size,
                                            engine="zipfile", mode_in="#Arşiv")
                            await _push_status(chat_id, client)
            except zipfile.BadZipFile as e:
                return None, f"Bozuk ZIP dosyası (çıkarma): {e}"

            LOGGER.info(f"[yukle:{session_id}] ZIP çıkarma tamamlandı ({len(members)} dosya)")

            # Birleştirilmiş geçici merged.zip dosyasını sil (orijinal parçalar değil)
            if zip_path.name == "merged.zip" and zip_path.exists():
                try:
                    zip_path.unlink()
                    LOGGER.info(f"[yukle:{session_id}] merged.zip silindi.")
                except Exception as _del_err:
                    LOGGER.warning(f"[yukle:{session_id}] merged.zip silinemedi: {_del_err}")

        # ── 7Z ────────────────────────────────────────────────────────────────
        elif re.search(r'\.7z(\.\d+)?$', first):
            try:
                import py7zr

                def _get_7z_names():
                    with py7zr.SevenZipFile(files_sorted[0], mode="r") as z:
                        return z.getnames()

                all_files   = await run_blocking(_get_7z_names)
                total_files = len(all_files)

                await _task_set(session_id, status="7Z Çıkarılıyor", pct=0,
                                engine="py7zr", mode_in="#Arşiv")
                await _push_status(chat_id, client)

                def _do_7z():
                    with py7zr.SevenZipFile(files_sorted[0], mode="r") as z:
                        z.extractall(path=session_dir)

                extract_task = asyncio.ensure_future(loop.run_in_executor(None, _do_7z))
                elapsed = 0
                while not extract_task.done():
                    await asyncio.sleep(5)
                    elapsed += 5
                    if _cancelled():
                        extract_task.cancel()
                        raise asyncio.CancelledError("İptal edildi")
                    await _task_set(session_id, status="7Z Çıkarılıyor",
                                    elapsed=_readable_time(elapsed),
                                    engine="py7zr", mode_in="#Arşiv")
                    await _push_status(chat_id, client)
                await extract_task

                await _task_set(session_id, status="7Z Tamamlandı", pct=100)
                await _push_status(chat_id, client)
                LOGGER.info(f"[yukle:{session_id}] 7Z çıkarma tamamlandı ({total_files} dosya)")

            except ImportError:
                return None, "py7zr kurulu değil. `pip install py7zr` ile kurun."
            except Exception as e:
                return None, f"7Z çıkarma hatası: {type(e).__name__}: {e}"

        # ── RAR ───────────────────────────────────────────────────────────────
        elif re.search(r'\.rar$', first):
            try:
                import rarfile

                def _get_rar_names():
                    with rarfile.RarFile(files_sorted[0], "r") as rf:
                        return rf.namelist()

                all_files   = await run_blocking(_get_rar_names)
                total_files = len(all_files)

                await _task_set(session_id, status="RAR Çıkarılıyor", pct=0,
                                engine="rarfile", mode_in="#Arşiv")
                await _push_status(chat_id, client)

                def _do_rar():
                    with rarfile.RarFile(files_sorted[0], "r") as rf:
                        rf.extractall(path=session_dir)

                extract_task = asyncio.ensure_future(loop.run_in_executor(None, _do_rar))
                elapsed = 0
                while not extract_task.done():
                    await asyncio.sleep(5)
                    elapsed += 5
                    if _cancelled():
                        extract_task.cancel()
                        raise asyncio.CancelledError("İptal edildi")
                    await _task_set(session_id, status="RAR Çıkarılıyor",
                                    elapsed=_readable_time(elapsed),
                                    engine="rarfile", mode_in="#Arşiv")
                    await _push_status(chat_id, client)
                await extract_task

                await _task_set(session_id, status="RAR Tamamlandı", pct=100)
                await _push_status(chat_id, client)
                LOGGER.info(f"[yukle:{session_id}] RAR çıkarma tamamlandı ({total_files} dosya)")

            except ImportError:
                return None, "rarfile kurulu değil. `pip install rarfile` ile kurun."
            except Exception as e:
                return None, f"RAR çıkarma hatası: {type(e).__name__}: {e}"

        else:
            return None, f"Desteklenmeyen arşiv formatı: {first}"

    except asyncio.CancelledError:
        raise
    except Exception as e:
        return None, f"Çıkarma hatası: {type(e).__name__}: {e}"

    found = [p for p in session_dir.rglob("*")
             if p.is_file() and p.suffix.lower() in video_exts]
    if not found:
        return None, "Çıkarılan arşivde video dosyası bulunamadı."

    LOGGER.info(f"[yukle:{session_id}] Video bulundu: {found[0]}")
    return found[0], None


# ─── /sunucuyayukle komutu ────────────────────────────────────────────────────

@Client.on_message(
    filters.command(["sunucuyayukle", "s"]) & filters.private & CustomFilters.owner
)
async def cmd_sunucuyayukle(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        max_dl, max_ul = _read_concurrency_limits()
        dl_str = str(max_dl) if max_dl < 999 else "sınırsız"
        ul_str = str(max_ul) if max_ul < 999 else "sınırsız"
        return await message.reply_text(
            f"ℹ️ <b>Nasıl Yükleme Yapılır?</b>\n\n"
            f"<b>📦 Zip/7z Arşiv Modu:</b>\n"
            f"• <code>/s 1</code> ➜ 1 adet zip/7z dosyası bekler.\n"
            f"• <code>/s 2</code> ➜ 2 adet partlı dosya bekler (Zip/7z).\n"
            f"• <code>/s 2 [TMDB/IMDb Link]</code> ➜ 2 dosya + metadata linki ile sorgular.\n\n"
            f"<b>🎬 Video Modu:</b>\n"
            f"• <code>/s v</code> ➜ 1 video dosyası bekler, doğrudan sunucuya yükler.\n"
            f"• <code>/s v2</code> ➜ 2 video dosyası bekler (ikincisi episode/part).\n"
            f"• <code>/s v [TMDB/IMDb Link]</code> ➜ Video + metadata linki ile sorgular.\n\n"
            f"<b>📁 Ham Dosya Modu (metadata yok):</b>\n"
            f"• <code>/s s1</code> ➜ 1 dosyayı metadata olmadan doğrudan sunucuya yükler.\n"
            f"• <code>/s s2</code> ➜ 2 dosyayı metadata olmadan doğrudan sunucuya yükler.\n"
            f"  ↳ Resim, müzik, zip veya herhangi bir dosya türü desteklenir.\n\n"
            f"<b>🌐 URL Modu:</b>\n"
            f"• <code>/s [URL]</code> ➜ Linkten direkt indirir.\n"
            f"• <code>/s [URL] [İsim]</code> ➜ Özel isimle indirir.\n"
            f"• <code>/s [Drive-Link]</code> ➜ Google Drive desteği.\n"
            "  ↳ Drive indirme için önce /ayarlar → 📁 Dosya Ekle → token.pickle yükleyin.\n\n"
            f"<b>🔗 Metadata Linkleri:</b>\n"
            f"• TMDB: <code>https://www.themoviedb.org/movie/12345</code>\n"
            f"• TMDB Dizi: <code>https://www.themoviedb.org/tv/67890</code>\n"
            f"• IMDb: <code>https://www.imdb.com/title/tt1234567</code>\n\n"
            f"⚙️ <b>Sistem Limitleri:</b>\n"
            f"├ ⬇️ İndirme: <b>{dl_str}</b>\n"
            f"└ ⬆️ Yükleme: <b>{ul_str}</b>\n\n"
            f"📂 <i>Değiştirmek için: /ayarlar ➜ 📋 Kuyruk</i>",
            parse_mode=ParseMode.HTML,
        )

    _ensure_queue()
    _start_worker()

    uid         = message.from_user.id if message.from_user else message.chat.id
    session_id  = f"{uid}_{int(time.time())}"
    session_dir = WORK_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    chat_id     = message.chat.id
    raw_arg     = args[1]

    # ── URL modu ──────────────────────────────────────────────────────────────
    if raw_arg.lower().startswith(("http://", "https://")):
        urls         = [a for a in args[1:] if a.lower().startswith(("http://", "https://"))]
        non_url_args = [a for a in args[2:] if not a.lower().startswith(("http://", "https://"))]
        custom_fname = non_url_args[0] if len(non_url_args) == 1 and len(urls) == 1 else None

        # Google Drive URL kontrolü
        is_gdrive = any(_is_gdrive_url(u) for u in urls)
        if is_gdrive:
            gdrive_ids = [_gdrive_file_id(u) for u in urls]
            if any(fid is None for fid in gdrive_ids):
                return await message.reply_text(
                    "❌ Google Drive URL'sinden dosya ID'si çıkarılamadı.\n"
                    "Lütfen paylaşım linkini (<code>/file/d/…</code>) kullanın.",
                    parse_mode=ParseMode.HTML,
                )
            if not GDRIVE_TOKEN_PATH.exists():
                return await message.reply_text(
                    "❌ <b>Google Drive token.pickle bulunamadı.</b>\n\n"
                    "/ayarlar → 📁 Dosya Ekle → 📤 token.pickle Yükle\n"
                    "adımını tamamlayın.",
                    parse_mode=ParseMode.HTML,
                )
        else:
            gdrive_ids = []

        if custom_fname:
            display_fname = custom_fname
        else:
            try:
                from urllib.parse import unquote
                display_fname = unquote(
                    urls[0].split("?")[0].rstrip("/").split("/")[-1]
                ) or "file.bin"
                if is_gdrive and display_fname in ("", "view", "edit", "file.bin"):
                    display_fname = "gdrive_dosya"
            except Exception:
                display_fname = "file.bin"

        engine_label = "GDrive" if is_gdrive else "HTTP"
        mode_label   = "#GDrive" if is_gdrive else "#URL"

        # Parçalı arşiv kontrolü: URL'den çıkarılan dosya adına göre
        url_fnames = []
        for u in urls:
            try:
                from urllib.parse import unquote as _uq
                url_fnames.append(_uq(u.split("?")[0].rstrip("/").split("/")[-1]))
            except Exception:
                url_fnames.append("")
        is_multipart_url = any(_is_multipart_archive(fn) for fn in url_fnames)
        if custom_fname:
            is_multipart_url = is_multipart_url or _is_multipart_archive(custom_fname)

        session = {
            "count": len(urls), "mode": "url", "collected": [],
            "session_dir": session_dir, "session_id": session_id,
            "chat_id": chat_id, "cancelled": False,
            "urls": urls, "custom_fname": custom_fname,
            "is_gdrive": is_gdrive, "gdrive_ids": gdrive_ids,
            "is_multipart": is_multipart_url,
        }
        _SESSIONS[uid] = session
        _SESSION_LOCKS[session_id] = asyncio.Lock()

        q_pos = _QUEUE.qsize() + 1
        await _task_set(
            session_id,
            fname=display_fname, chat_id=chat_id,
            status="Kuyrukta", pct=0, processed=0, total_size=0,
            speed=0, eta="-", elapsed="-",
            engine=engine_label, mode_in=mode_label, queue_pos=q_pos,
            is_multipart=is_multipart_url,
        )

        # Eski durum mesajını sil, yeni mesajı bota at
        text = _render_queue_msg(chat_id)
        kb   = _page_kb(chat_id)
        await _replace_status_msg(chat_id, client, text, kb)

        await _QUEUE.put({"client": client, "uid": uid,
                          "session": session, "orig_message": message})
        return

    # ── Ham Dosya Modu: /s s1, /s s2, … (metadata yok, direkt sunucuya yükle) ─
    raw_lower = raw_arg.lower()

    _raw_mode_match = re.fullmatch(r's(\d+)', raw_lower)
    if _raw_mode_match:
        raw_count = int(_raw_mode_match.group(1))
        if raw_count < 1:
            raw_count = 1

        session = {
            "count": raw_count, "mode": "raw", "collected": [],
            "session_dir": session_dir, "session_id": session_id,
            "chat_id": chat_id, "cancelled": False,
        }
        _SESSIONS[uid] = session
        _SESSION_LOCKS[session_id] = asyncio.Lock()

        await message.reply_text(
            f"📁 <b>Ham Dosya Modu Aktif — {raw_count} dosya bekleniyor</b>\n"
            "Metadata işlemi uygulanmayacak; dosya doğrudan sunucuya yüklenecek.\n"
            "Resim, müzik, zip veya herhangi bir dosya türü gönderin.\n"
            "<i>İptal: /iptal</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Video modu: /s v, /s v2, /s v [link] ─────────────────────────────────
    raw_lower = raw_arg.lower()

    # "v" veya "v<N>" formatını kontrol et: v, v1, v2, v3 ...
    _video_mode_match = re.fullmatch(r'v(\d*)', raw_lower)
    if _video_mode_match:
        video_count_str = _video_mode_match.group(1)
        video_count = int(video_count_str) if video_count_str else 1
        if video_count < 1:
            video_count = 1

        # Metadata link argümanı var mı? (/s v tmdblink veya /s v2 tmdblink)
        _remaining_args = args[2:]
        override_link = None
        for _a in _remaining_args:
            if _a.lower().startswith(("http://", "https://")):
                override_link = _a
                break

        session = {
            "count": video_count, "mode": "video", "collected": [],
            "session_dir": session_dir, "session_id": session_id,
            "chat_id": chat_id, "cancelled": False,
            "override_link": override_link,
        }
        _SESSIONS[uid] = session
        _SESSION_LOCKS[session_id] = asyncio.Lock()

        link_info = ""
        if override_link:
            link_info = f"\n🔗 <b>Metadata:</b> <code>{override_link[:60]}{'…' if len(override_link) > 60 else ''}</code>"

        await message.reply_text(
            f"🎬 <b>Video Modu Aktif — {video_count} video bekleniyor</b>{link_info}\n"
            "Video dosyasını gönderin (mkv, mp4, avi, …).\n"
            "<i>İptal: /iptal</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Arşiv dosya modu: /s <N> veya /s <N> [link] ──────────────────────────
    # Metadata link argümanı var mı? (/s 2 tmdblink veya /s 2 imdblink)
    _meta_link_from_args = None
    for _a in args[2:]:
        if _a.lower().startswith(("http://", "https://")):
            _meta_link_from_args = _a
            break

    if not (raw_lower.isdigit() and int(raw_lower) >= 1):
        return await message.reply_text(
            "❌ Geçersiz parametre.\n"
            "Kullanım: <code>/s 2</code>, <code>/s v</code> veya <code>/s https://…</code>",
            parse_mode=ParseMode.HTML,
        )

    count   = int(raw_lower)
    session = {
        "count": count, "mode": "multi", "collected": [],
        "session_dir": session_dir, "session_id": session_id,
        "chat_id": chat_id, "cancelled": False,
        "override_link": _meta_link_from_args,
    }
    _SESSIONS[uid] = session
    _SESSION_LOCKS[session_id] = asyncio.Lock()

    link_info = ""
    if _meta_link_from_args:
        link_info = f"\n🔗 <b>Metadata:</b> <code>{_meta_link_from_args[:60]}{'…' if len(_meta_link_from_args) > 60 else ''}</code>"

    # Dosya bekleniyor — sadece bilgi mesajı gönder, görev satırı ekleme.
    # Görev satırı her dosya gelince zip_file_collector tarafından eklenir.
    await message.reply_text(
        f"✅ <b>Yükleme Modu Aktif — {count} dosya bekleniyor</b>{link_info}\n"
        "Dosyaları sırasıyla gönderin.\n"
        "<i>İptal: /iptal</i>",
        parse_mode=ParseMode.HTML,
    )


# ─── /iptal komutu ────────────────────────────────────────────────────────────

@Client.on_message(
    filters.command(["iptal"]) & filters.private & CustomFilters.owner
)
async def cmd_iptal(client: Client, message: Message):
    chat_id = message.chat.id
    found   = False
    for uid, sess in list(_SESSIONS.items()):
        if sess["chat_id"] == chat_id:
            sid = sess.get("session_id", "")
            sess["cancelled"] = True
            task = _ACTIVE_TASKS.get(sid)
            if task and not task.done():
                task.cancel()
                # Task'ın gerçekten iptal olmasını bekle (shield KULLANILMAZ)
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            # Her durumda manuel temizle
            shutil.rmtree(sess["session_dir"], ignore_errors=True)
            _SESSIONS.pop(uid, None)
            _SESSION_LOCKS.pop(sid, None)
            _ACTIVE_TASKS.pop(sid, None)
            await _task_remove(sid)
            found = True
            LOGGER.info(f"[yukle] /iptal → {sid} iptal edildi.")
            break

    if found:
        tasks_left = [t for t in _TASKS_STATE if t.get("chat_id") == chat_id]
        if tasks_left:
            await _push_status(chat_id, client, force=True)
        else:
            status_msg = _STATUS_MSGS.pop(chat_id, None)
            _PAGE_STATE.pop(chat_id, None)
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

    # Varsa parent "multi" session'ını da temizle
    for _uid2, sess2 in list(_SESSIONS.items()):
        if sess2.get("chat_id") == chat_id and sess2.get("mode") == "multi":
            sess2["cancelled"] = True
            sid2 = sess2.get("session_id", "")
            task2 = _ACTIVE_TASKS.get(sid2)
            if task2 and not task2.done():
                task2.cancel()
                try:
                    await asyncio.wait_for(task2, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            shutil.rmtree(sess2.get("session_dir", Path("/tmp/__none__")), ignore_errors=True)
            _SESSIONS.pop(_uid2, None)
            _SESSION_LOCKS.pop(sid2, None)
            _ACTIVE_TASKS.pop(sid2, None)
            await _task_remove(sid2)
            break

    await message.reply_text("⛔ İşlem iptal edildi." if found else "⚠️ Aktif işlem yok.")


# ─── /c_XXXXXXXXXX komutu (belirli session'ı iptal et) ───────────────────────

@Client.on_message(
    filters.regex(r"^/c_[a-zA-Z0-9_]+") & filters.private & CustomFilters.owner
)
async def cmd_cancel_session(client: Client, message: Message):
    chat_id  = message.chat.id
    text     = message.text or ""
    # /c_XXXXXXXXXX → ilk 10 karakter prefix
    prefix   = text.strip().lstrip("/c_") if text.startswith("/c_") else ""
    # Tam prefix: text = "/c_6763021546" → prefix = "6763021546"
    try:
        cancel_prefix = text.strip()[3:]  # "/c_" sonrası tümü
    except Exception:
        cancel_prefix = ""

    found = False
    for uid, sess in list(_SESSIONS.items()):
        sid = sess.get("session_id", "")
        # session_id'nin ilk 10 karakteri ile eşleştir
        if sid[:10] == cancel_prefix[:10] and sess["chat_id"] == chat_id:
            sess["cancelled"] = True
            task = _ACTIVE_TASKS.get(sid)
            if task and not task.done():
                task.cancel()
                # Task'ın gerçekten iptal olmasını bekle (shield KULLANILMAZ)
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            # Her durumda manuel temizle
            shutil.rmtree(sess["session_dir"], ignore_errors=True)
            _SESSIONS.pop(uid, None)
            _SESSION_LOCKS.pop(sid, None)
            _ACTIVE_TASKS.pop(sid, None)
            await _task_remove(sid)
            found = True
            LOGGER.info(f"[yukle] /c_ komutuyla iptal → {sid}")
            break

    # Kuyrukta bekleyenler için de kontrol et
    if not found:
        for t in list(_TASKS_STATE):
            sid = t.get("session_id", "")
            if sid[:10] == cancel_prefix[:10] and t.get("chat_id") == chat_id:
                # Kuyruktan da iptal et — cancelled flag'ini set et ve _SESSIONS'tan temizle
                for uid2, sess2 in list(_SESSIONS.items()):
                    if sess2.get("session_id") == sid:
                        sess2["cancelled"] = True
                        # Aktif task varsa iptal et (kuyruktan çıkıp işleme geçmiş olabilir)
                        task2 = _ACTIVE_TASKS.get(sid)
                        if task2 and not task2.done():
                            task2.cancel()
                            try:
                                await asyncio.wait_for(task2, timeout=5.0)
                            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                                pass
                        shutil.rmtree(sess2["session_dir"], ignore_errors=True)
                        _SESSIONS.pop(uid2, None)
                        _SESSION_LOCKS.pop(sid, None)
                        _ACTIVE_TASKS.pop(sid, None)
                        break
                await _task_remove(sid)
                found = True
                LOGGER.info(f"[yukle] /c_ kuyruk iptali → {sid}")
                break

    if found:
        tasks_left = [t for t in _TASKS_STATE if t.get("chat_id") == chat_id]
        if tasks_left:
            await _push_status(chat_id, client, force=True)
        else:
            status_msg = _STATUS_MSGS.pop(chat_id, None)
            _PAGE_STATE.pop(chat_id, None)
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

    await message.reply_text("⛔ İşlem iptal edildi." if found else "⚠️ Eşleşen aktif işlem bulunamadı.")


# ─── Dosya toplayıcı ──────────────────────────────────────────────────────────

@Client.on_message(
    filters.private & (filters.document | filters.video | filters.photo | filters.audio) & CustomFilters.owner,
    group=5,
)
async def zip_file_collector(client: Client, message: Message):
    """
    Gelen arsiv, video veya resim dosyalarini sirayla ayni session'a toplar.
    "multi" modunda arsiv dosyalari, "video" modunda video dosyalari beklenir.
    "raw" modunda her turlu dosya (resim dahil) kabul edilir.
    Tum parcalar toplandiktan sonra kuyruga isleme gorevi eklenir.
    """
    chat_id = message.chat.id

    # Bu chat icin aktif bir "multi", "video" veya "raw" session'i ara
    session = None
    uid     = None
    for _uid, sess in _SESSIONS.items():
        if sess["chat_id"] == chat_id and sess.get("mode") in ("multi", "video", "raw"):
            session = sess
            uid     = _uid
            break

    if session is None or session.get("cancelled"):
        return

    doc = message.document
    # Telegram bazen video dosyalarini `video` tipinde gonderir (document degil).
    # Bu durumda message.video uzerinden dosya bilgisini al.
    _tg_video = None
    if doc is None and message.video:
        _tg_video = message.video
        # video nesnesini document gibi kullanabilmek icin bir sarmalayici olustur
        class _VideoAsDoc:
            file_id        = _tg_video.file_id
            file_unique_id = _tg_video.file_unique_id
            file_size      = _tg_video.file_size
            file_name      = getattr(_tg_video, "file_name", None) or f"video_{_tg_video.file_unique_id}.mp4"
        doc = _VideoAsDoc()

    # Telegram fotograflari `photo` tipinde gelir (document degil).
    # raw modunda fotograflari da kabul et.
    if doc is None and message.photo:
        _tg_photo = message.photo
        # En yuksek cozunurluklu boyutu al (Pyrogram'da list, son eleman en buyuk)
        _photo_obj = _tg_photo if not isinstance(_tg_photo, list) else _tg_photo[-1]
        class _PhotoAsDoc:
            file_id        = _photo_obj.file_id
            file_unique_id = _photo_obj.file_unique_id
            file_size      = getattr(_photo_obj, "file_size", 0) or 0
            file_name      = f"photo_{_photo_obj.file_unique_id}.jpg"
        doc = _PhotoAsDoc()

    # Telegram müzik dosyalarını `audio` tipinde gönderir (document değil).
    # raw modunda audio dosyalarını da kabul et.
    if doc is None and message.audio:
        _tg_audio = message.audio
        class _AudioAsDoc:
            file_id        = _tg_audio.file_id
            file_unique_id = _tg_audio.file_unique_id
            file_size      = getattr(_tg_audio, "file_size", 0) or 0
            file_name      = (
                getattr(_tg_audio, "file_name", None)
                or f"{getattr(_tg_audio, 'title', None) or 'audio'}_{_tg_audio.file_unique_id}.mp3"
            )
        doc = _AudioAsDoc()

    _video_exts = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m4v", ".webm", ".flv", ".mpg", ".mpeg"}
    _is_video_doc = bool(doc and (doc.file_name or "").lower().endswith(tuple(_video_exts)))

    mode = session.get("mode", "multi")

    # Mod kontrolü
    if mode == "raw":
        pass  # Ham dosya modunda her türlü dosya kabul edilir
    elif mode == "video":
        if not _is_video_doc:
            return  # Video modunda arşiv kabul etme
    else:
        if not _is_zip_or_7z(doc) and not _is_video_doc:
            return

    session_id = session["session_id"]
    lock       = _SESSION_LOCKS.get(session_id)
    if lock is None:
        return

    async with lock:
        if session.get("cancelled"):
            return

        count   = session["count"]
        idx     = len(session["collected"]) + 1
        if idx > count:
            return  # Fazla dosya, yoksay

        session_dir: Path = session["session_dir"]
        tg_fname = doc.file_name or f"file_{doc.file_unique_id}.{'mkv' if _is_video_doc else ('bin' if mode == 'raw' else 'zip')}"
        dest     = session_dir / tg_fname

        # İlk dosya geldiğinde parçalı arşiv tespiti yap (sadece multi modunda)
        if mode == "multi" and "is_multipart" not in session:
            session["is_multipart"] = _is_multipart_archive(tg_fname)
            if session["is_multipart"]:
                LOGGER.info(f"[yukle:{session_id}] Parçalı arşiv tespit edildi: {tg_fname} — indirme limiti 1")

        LOGGER.info(f"[yukle:{session_id}] Dosya alındı ({idx}/{count}): {tg_fname} [mod={mode}]")

        mode_label = "#VideoMod" if mode == "video" else ("#HamDosya" if mode == "raw" else "#TgDosya")

        # Bu dosyanın indirme görevini TASKS'a ekle / güncelle
        await _task_set(
            session_id,
            fname=tg_fname, chat_id=chat_id,
            status="İndiriliyor", pct=0,
            processed=0, total_size=doc.file_size or 0,
            speed=0, eta="-", elapsed="0s",
            engine="Pyrogram", mode_in=mode_label,
            is_multipart=session.get("is_multipart", False),
        )

        # Eski durum mesajını sil, yeni mesajı bota at
        text = _render_queue_msg(chat_id)
        kb   = _page_kb(chat_id)
        await _replace_status_msg(chat_id, client, text, kb)

        # Dosyayı indir
        start = time.time()
        try:
            await client.download_media(
                message,
                file_name=str(dest),
                progress=_progress_cb,
                progress_args=(session_id, tg_fname, start, client, chat_id),
            )
        except (asyncio.CancelledError, _ZipCancelled):
            LOGGER.info(f"[yukle:{session_id}] İndirme iptal: {tg_fname}")
            session["cancelled"] = True
            shutil.rmtree(session_dir, ignore_errors=True)
            _SESSIONS.pop(uid, None)
            _SESSION_LOCKS.pop(session_id, None)
            await _task_remove(session_id)
            status_msg = _STATUS_MSGS.pop(chat_id, None)
            _PAGE_STATE.pop(chat_id, None)
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            return
        except FileNotFoundError as e:
            if session.get("cancelled"):
                return
            err = f"FileNotFoundError: {e}"
            LOGGER.error(f"[yukle:{session_id}] İndirme hatası: {err}\n{traceback.format_exc()}")
            await _task_set(session_id, status=f"❌ {err[:50]}")
            await _push_status(chat_id, client, force=True)
            session["cancelled"] = True
            shutil.rmtree(session_dir, ignore_errors=True)
            _SESSIONS.pop(uid, None)
            _SESSION_LOCKS.pop(session_id, None)
            return
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            LOGGER.error(f"[yukle:{session_id}] İndirme hatası: {err}\n{traceback.format_exc()}")
            await _task_set(session_id, status=f"❌ {err[:50]}")
            await _push_status(chat_id, client, force=True)
            session["cancelled"] = True
            shutil.rmtree(session_dir, ignore_errors=True)
            _SESSIONS.pop(uid, None)
            _SESSION_LOCKS.pop(session_id, None)
            return

        if session.get("cancelled"):
            return

        LOGGER.info(f"[yukle:{session_id}] İndirme OK: {tg_fname}")
        session["collected"].append(dest)

        remaining = count - len(session["collected"])
        if remaining > 0:
            # Sonraki dosyayı bekle — durum güncelle
            await _task_set(
                session_id,
                fname=f"{len(session['collected'])}/{count} alındı — {remaining} {'video' if mode == 'video' else 'dosya'} bekleniyor",
                status="Dosya Bekleniyor", pct=0,
                processed=0, total_size=0, speed=0, eta="-", elapsed="-",
            )
            await _push_status(chat_id, client, force=True)
        else:
            # Tüm dosyalar toplandı
            if mode == "raw":
                # Ham dosya modu — kuyruğa atmadan doğrudan işle
                _SESSIONS.pop(uid, None)
                _SESSION_LOCKS.pop(session_id, None)
                asyncio.create_task(
                    _process_session_upload_raw(client, uid, session, message)
                )
            else:
                # Normal mod — kuyruğa işleme görevi ekle
                _ensure_queue()
                _start_worker()
                q_pos = _QUEUE.qsize() + 1
                await _task_set(session_id, status="Kuyrukta", queue_pos=q_pos,
                                fname=session["collected"][0].name if session["collected"] else tg_fname)
                await _push_status(chat_id, client, force=True)
                await _QUEUE.put({
                    "client": client, "uid": uid,
                    "session": session, "orig_message": message,
                })
                _SESSION_LOCKS.pop(session_id, None)

# ─── Oturum işleme ────────────────────────────────────────────────────────────

async def _process_session(client: Client, uid: int, session: dict,
                            orig_message: Message):
    """Eski entry point — geriye uyumluluk için korundu. Artık kullanılmıyor."""
    await _process_session_download(client, uid, session, orig_message)
    if not session.get("cancelled") and session.get("_video_path"):
        await _process_session_upload(client, uid, session, orig_message)


async def _process_session_download(client: Client, uid: int, session: dict,
                                     orig_message: Message):
    """
    İndirme + arşiv çıkarma aşaması.
    Başarılıysa session['_video_path'] ve session['_orig_message'] set edilir.
    """
    if session.get("cancelled"):
        _SESSIONS.pop(uid, None)
        return

    session_id  = session["session_id"]
    session_dir: Path = session["session_dir"]
    chat_id     = session["chat_id"]

    try:
        # URL modunda henüz indirme yapılmadıysa yap
        if session.get("mode") == "url":
            urls         = session.get("urls", [])
            gdrive_ids   = session.get("gdrive_ids", [])
            is_gdrive    = session.get("is_gdrive", False)
            custom_fname = session.get("custom_fname")

            for i, url in enumerate(urls, 1):
                if session.get("cancelled"):
                    break

                if is_gdrive:
                    file_id = gdrive_ids[i - 1] if i - 1 < len(gdrive_ids) else None
                    if not file_id:
                        await _task_set(session_id, status="❌ Drive ID alınamadı")
                        await _push_status(chat_id, client, force=True)
                        _SESSIONS.pop(uid, None)
                        shutil.rmtree(session_dir, ignore_errors=True)
                        return
                    dest = session_dir / (custom_fname if custom_fname and len(urls) == 1 else f"gdrive_{i}")
                    ok, err = await _download_gdrive(file_id, dest, session_id, client, chat_id)
                    if ok:
                        candidates = list(session_dir.iterdir()) if session_dir.exists() else []
                        candidates = [p for p in candidates if p not in session["collected"] and p.is_file()]
                        if candidates:
                            dest = max(candidates, key=lambda p: p.stat().st_mtime)
                else:
                    if custom_fname and len(urls) == 1:
                        fname = custom_fname
                    else:
                        try:
                            from urllib.parse import unquote
                            fname = unquote(url.split("?")[0].rstrip("/").split("/")[-1]) or f"file_{i}.bin"
                        except Exception:
                            fname = f"file_{i}.bin"

                    dest = session_dir / fname
                    await _task_set(session_id, fname=fname, status="İndiriliyor", pct=0,
                                    processed=0, total_size=0, speed=0, eta="-", elapsed="-",
                                    engine="HTTP", mode_in="#URL")
                    await _push_status(chat_id, client, force=True)
                    ok, err = await _download_url(url, dest, session_id, client, chat_id)

                if not ok:
                    LOGGER.error(f"[yukle:{session_id}] İndirme hatası: {err}")
                    await _task_set(session_id, status=f"❌ {err[:60]}")
                    await _push_status(chat_id, client, force=True)
                    _SESSIONS.pop(uid, None)
                    shutil.rmtree(session_dir, ignore_errors=True)
                    return
                session["collected"].append(dest)

        if session.get("cancelled"):
            _SESSIONS.pop(uid, None)
            return

        parts             = sorted(session["collected"], key=lambda p: p.name.lower())
        archive_parts     = [p for p in parts if _is_archive(p.name)]
        non_archive_parts = [p for p in parts if not _is_archive(p.name)]
        video_exts        = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m4v"}
        mode              = session.get("mode", "multi")

        # Video modunda arşiv çıkarmayı atla, direkt video yolunu kullan
        if mode == "video" or (non_archive_parts and not archive_parts):
            video_parts = [p for p in parts if p.suffix.lower() in video_exts]
            video_path  = video_parts[0] if video_parts else (non_archive_parts[0] if non_archive_parts else parts[0] if parts else None)
            LOGGER.info(f"[yukle:{session_id}] Direkt dosya: {video_path.name if video_path else '?'} [mod={mode}]")
            await _task_set(session_id, status="Yükleme Bekleniyor",
                            fname=video_path.name if video_path else "?", pct=100)
            await _push_status(chat_id, client, force=True)
            extract_err = None
        else:
            mode_in = "#URL" if session.get("mode") == "url" else "#TgDosya"
            fname_d = parts[0].name if parts else "?"
            await _task_set(session_id, fname=fname_d, status="Çıkarılıyor",
                            pct=0, engine="Arşiv", mode_in=mode_in)
            await _push_status(chat_id, client, force=True)
            video_path, extract_err = await _extract_archive(
                session_dir, archive_parts or parts,
                session_id, session, client, chat_id
            )

        if session.get("cancelled"):
            _SESSIONS.pop(uid, None)
            return

        if extract_err or not video_path:
            err_msg = extract_err or "Dosyadan video çıkarılamadı."
            LOGGER.error(f"[yukle:{session_id}] Başarısız: {err_msg}")
            await _task_set(session_id, status=f"❌ {err_msg[:50]}")
            await _push_status(chat_id, client, force=True)
            _SESSIONS.pop(uid, None)
            return

        for part in archive_parts:
            try:
                part.unlink()
            except Exception as e:
                LOGGER.warning(f"[yukle:{session_id}] Parça silinemedi {part.name}: {e}")

        # Yükleme aşamasına video yolunu aktar
        session["_video_path"]    = video_path
        session["_orig_message"]  = orig_message
        session["_archive_parts"] = archive_parts

    except asyncio.CancelledError:
        LOGGER.info(f"[yukle:{session_id}] İndirme iptal edildi.")
        await asyncio.sleep(1)
        shutil.rmtree(session_dir, ignore_errors=True)
        _SESSIONS.pop(uid, None)
        _SESSION_LOCKS.pop(session_id, None)
        raise
    except Exception as e:
        LOGGER.error(f"[yukle:{session_id}] İndirme iç hata: {e}\n{traceback.format_exc()}")
        await _task_set(session_id, status=f"❌ {str(e)[:50]}")
        await _push_status(chat_id, client, force=True)
        _SESSIONS.pop(uid, None)


async def _process_session_upload(client: Client, uid: int, session: dict,
                                   orig_message: Message):
    """
    Metadata arama + DB kayıt aşaması.
    session['_video_path'] set edilmiş olmalıdır (_process_session_download sonrası).
    """
    session_id  = session["session_id"]
    session_dir: Path = session["session_dir"]
    chat_id     = session["chat_id"]
    video_path  = session.get("_video_path")

    if not video_path or session.get("cancelled"):
        _SESSIONS.pop(uid, None)
        return

    try:
        await _task_set(session_id, status="Metadata Aranıyor",
                        fname=video_path.name, pct=100)
        await _push_status(chat_id, client, force=True)

        from Backend.helper.metadata import extract_default_id
        raw_caption = orig_message.caption or ""
        override_id, _ = extract_default_id(raw_caption) if raw_caption else (None, None)

        # Eğer caption'da link yoksa session'daki override_link'i kullan
        # (/s 2 tmdblink veya /s v imdblink ile gönderilen link)
        if not override_id:
            override_link = session.get("override_link")
            if override_link:
                _oid, _ = extract_default_id(override_link)
                if _oid:
                    override_id = override_link  # metadata() fonksiyonu extract_default_id çağırır
                    LOGGER.info(f"[yukle:{session_id}] override_link kullanılıyor: {override_link}")

        video_name_for_meta = _archive_to_video_name(video_path.name)
        clean_name = clean_filename(video_name_for_meta)
        channel    = str(orig_message.chat.id).replace("-100", "")
        msg_id     = orig_message.id

        LOGGER.info(f"[yukle:{session_id}] Metadata: '{clean_name}'")

        try:
            metadata_info = await metadata(clean_name, int(channel), msg_id,
                                           override_id=override_id)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            LOGGER.error(f"[yukle:{session_id}] Metadata hatası: {err}\n{traceback.format_exc()}")
            await _task_set(session_id, status=f"❌ Metadata: {err[:40]}")
            await _push_status(chat_id, client, force=True)
            _SESSIONS.pop(uid, None)
            return

        if metadata_info is None:
            LOGGER.warning(f"[yukle:{session_id}] Metadata bulunamadı: {clean_name}")
            await _task_set(session_id, status="⚠️ Metadata Bulunamadı")
            await _push_status(chat_id, client, force=True)
            _SESSIONS.pop(uid, None)
            return

        media_title = metadata_info.get("title", clean_name)
        LOGGER.info(f"[yukle:{session_id}] Metadata OK: {media_title}")

        await _task_set(session_id, status="DB'ye Kaydediliyor", fname=media_title)
        await _push_status(chat_id, client, force=True)

        display_name = remove_urls(video_name_for_meta)
        if not display_name.lower().endswith((".mkv", ".mp4")):
            display_name += ".mkv"
        size_str = get_readable_file_size(video_path.stat().st_size)

        from Backend.helper.encrypt import encode_string as _encode_string
        try:
            local_encoded = await _encode_string({"local_path": str(video_path)})
            metadata_info = dict(metadata_info)
            metadata_info["encoded_string"] = local_encoded
        except Exception as e:
            LOGGER.warning(f"[yukle:{session_id}] local encoded_string oluşturulamadı: {e}")

        try:
            updated_id = await db.insert_media(
                metadata_info,
                channel=int(channel),
                msg_id=msg_id,
                size=size_str,
                name=display_name,
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            LOGGER.error(f"[yukle:{session_id}] DB hatası: {err}\n{traceback.format_exc()}")
            await _task_set(session_id, status=f"❌ DB: {err[:40]}")
            await _push_status(chat_id, client, force=True)
            _SESSIONS.pop(uid, None)
            return

        if updated_id:
            LOGGER.info(f"[yukle:{session_id}] DB OK: ID={updated_id} | {media_title}")
            await _task_set(
                session_id,
                status="✅ Tamamlandı",
                fname=f"{media_title} ({size_str})",
                pct=100,
            )

            try:
                import psutil
                disk = shutil.disk_usage(WORK_DIR)
                used_gb  = disk.used / 1_073_741_824
                total_gb = disk.total / 1_073_741_824
                free_gb  = disk.free / 1_073_741_824
                media_type = metadata_info.get("type", "MOVIE").upper()
                disk_str = f"{used_gb:.1f} GB / {total_gb:.1f} GB (Boş: {free_gb:.1f} GB)"
            except Exception:
                disk_str   = "bilinmiyor"
                media_type = metadata_info.get("type", "MOVIE").upper()

            done_text = (
                f"✅ <b>İşlem Tamamlandı!</b>\n"
                f"🎬 <b>Başlık:</b> {media_title}\n"
                f"📦 <b>Dosya:</b> {display_name}\n"
                f"💾 <b>Boyut:</b> {size_str}\n"
                f"🆔 <b>DB ID:</b> {updated_id}\n"
                f"📁 <b>Tür:</b> {media_type}\n"
                f"🖥 <b>Disk:</b> {disk_str}"
            )
            try:
                await client.send_message(chat_id, done_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                LOGGER.warning(f"[yukle:{session_id}] Tamamlandı mesajı gönderilemedi: {e}")
        else:
            LOGGER.warning(f"[yukle:{session_id}] DB kaydı başarısız: {media_title}")
            await _task_set(session_id, status="⚠️ DB Kaydı Başarısız")

        await _push_status(chat_id, client, force=True)
        _SESSIONS.pop(uid, None)

    except asyncio.CancelledError:
        LOGGER.info(f"[yukle:{session_id}] Yükleme iptal edildi.")
        _SESSIONS.pop(uid, None)
        raise
    except Exception as e:
        LOGGER.error(f"[yukle:{session_id}] Yükleme iç hata: {e}\n{traceback.format_exc()}")
        await _task_set(session_id, status=f"❌ {str(e)[:50]}")
        await _push_status(chat_id, client, force=True)
        _SESSIONS.pop(uid, None)




# ─── Ham Dosya Modu Yükleme ──────────────────────────────────────────────────

async def _process_session_upload_raw(client: Client, uid: int, session: dict,
                                       orig_message: Message):
    """
    Ham dosya modu (/s s1, /s s2, …) için yükleme aşaması.
    Metadata işlemi uygulanmaz; dosyalar olduğu gibi WORK_DIR'e taşınır
    ve DB'ye yerel path + ham dosya adı ile kaydedilir.
    """
    session_id  = session["session_id"]
    session_dir: Path = session["session_dir"]
    chat_id     = session["chat_id"]
    collected: list[Path] = session.get("collected", [])

    if not collected or session.get("cancelled"):
        _SESSIONS.pop(uid, None)
        return

    try:
        results = []
        for src_path in collected:
            if session.get("cancelled"):
                break

            fname = src_path.name
            dest  = WORK_DIR / fname

            # Aynı isimde dosya varsa sonuna _1, _2, … ekle
            counter = 1
            stem    = src_path.stem
            suffix  = src_path.suffix
            while dest.exists():
                dest = WORK_DIR / f"{stem}_{counter}{suffix}"
                counter += 1

            LOGGER.info(f"[yukle_raw:{session_id}] Dosya taşınıyor: {src_path} → {dest}")
            shutil.move(str(src_path), str(dest))

            size_str = get_readable_file_size(dest.stat().st_size)

            LOGGER.info(f"[yukle_raw:{session_id}] Yükleme OK: {dest} ({size_str})")
            results.append(
                f"✅ <b>{fname}</b>\n"
                f"   📂 Yol: <code>{dest}</code>\n"
                f"   💾 Boyut: {size_str}"
            )

        # Oturum dizinini temizle (artık dosyalar WORK_DIR'e taşındı)
        shutil.rmtree(session_dir, ignore_errors=True)

        # Disk bilgisi
        try:
            disk = shutil.disk_usage(WORK_DIR)
            used_gb  = disk.used  / 1_073_741_824
            total_gb = disk.total / 1_073_741_824
            free_gb  = disk.free  / 1_073_741_824
            disk_str = f"{used_gb:.1f} GB / {total_gb:.1f} GB (Boş: {free_gb:.1f} GB)"
        except Exception:
            disk_str = "bilinmiyor"

        summary = "\n".join(results) if results else "⚠️ Hiç dosya işlenemedi."
        done_text = (
            f"📁 <b>Ham Dosya Yükleme Tamamlandı</b>\n\n"
            f"{summary}\n\n"
            f"🖥 <b>Disk:</b> {disk_str}"
        )
        # İlerleme mesajını sil
        await _task_remove(session_id)
        status_msg = _STATUS_MSGS.pop(chat_id, None)
        _PAGE_STATE.pop(chat_id, None)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        try:
            await client.send_message(chat_id, done_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            LOGGER.warning(f"[yukle_raw:{session_id}] Tamamlandı mesajı gönderilemedi: {e}")

    except asyncio.CancelledError:
        LOGGER.info(f"[yukle_raw:{session_id}] Ham yükleme iptal edildi.")
        _SESSIONS.pop(uid, None)
        raise
    except Exception as e:
        LOGGER.error(f"[yukle_raw:{session_id}] İç hata: {e}\n{traceback.format_exc()}")
        await _task_remove(session_id)
        status_msg = _STATUS_MSGS.pop(chat_id, None)
        _PAGE_STATE.pop(chat_id, None)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        try:
            await client.send_message(
                chat_id,
                f"❌ <b>Ham yükleme hatası:</b> <code>{str(e)[:100]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        _SESSIONS.pop(uid, None)


# ─── Başlangıç temizliği ──────────────────────────────────────────────────────

async def cleanup_local_path_records():
    """
    Bot başlarken artık mevcut olmayan yerel dosya DB kayıtlarını temizler.
    """
    from Backend.helper.encrypt import decode_string as _decode_string
    removed = 0
    try:
        for i in range(1, db.current_db_index + 1):
            storage = db.dbs[f"storage_{i}"]
            for col in ("movie", "tv"):
                async for doc in storage[col].find({}):
                    if col == "movie":
                        bad_ids = []
                        for q in doc.get("telegram", []):
                            qid = q.get("id", "")
                            try:
                                decoded = await _decode_string(qid)
                                if decoded.get("local_path"):
                                    if not Path(decoded["local_path"]).exists():
                                        bad_ids.append(qid)
                            except Exception:
                                pass
                        for bad in bad_ids:
                            await db.delete_media_by_stream_id(bad)
                            removed += 1
                    else:
                        bad_ids = []
                        for season in doc.get("seasons", []):
                            for episode in season.get("episodes", []):
                                for q in episode.get("telegram", []):
                                    qid = q.get("id", "")
                                    try:
                                        decoded = await _decode_string(qid)
                                        if decoded.get("local_path"):
                                            if not Path(decoded["local_path"]).exists():
                                                bad_ids.append(qid)
                                    except Exception:
                                        pass
                        for bad in bad_ids:
                            await db.delete_media_by_stream_id(bad)
                            removed += 1
    except Exception as e:
        LOGGER.warning(f"[yukle] startup cleanup hatası: {e}")
    LOGGER.info(
        f"[yukle] Başlangıç temizliği: {removed} yerel dosya kaydı DB'den silindi."
        if removed else "[yukle] Başlangıç temizliği: yerel dosya kaydı yok."
    )


# ══════════════════════════════════════════════════════════════════════════════
# /sunucudansil — Sunucu dosya yöneticisi (sayfalı)
# ══════════════════════════════════════════════════════════════════════════════

_SIL_PAGE_SIZE = 8
_PATH_REGISTRY: dict = {}
_PATH_REGISTRY_CTR: list = [0]


def _reg_path(p: Path) -> int:
    _PATH_REGISTRY_CTR[0] += 1
    idx = _PATH_REGISTRY_CTR[0]
    _PATH_REGISTRY[idx] = p
    return idx


def _get_path(idx: int):
    return _PATH_REGISTRY.get(idx)


def _clear_registry():
    _PATH_REGISTRY.clear()
    _PATH_REGISTRY_CTR[0] = 0


def _sil_keyboard(current_path: Path, page: int = 0):
    """
    Sayfalı dosya/klasör gezgini.
    Klasörler → cd, Dosyalar → dokunmak = sil.
    Ayrı 🗑 butonu yok.
    """
    buttons = []
    try:
        all_items = sorted(current_path.iterdir(),
                           key=lambda p: (p.is_file(), p.name.lower()))
    except Exception as e:
        LOGGER.error(f"[sunucudansil] Klasör okunamadı {current_path}: {e}")
        return [], None

    if current_path != WORK_DIR:
        parent_idx = _reg_path(current_path.parent)
        buttons.append([InlineKeyboardButton(
            "⬆️ Üst Dizin", callback_data=f"sds:cd:{page}:{parent_idx}"
        )])

    total       = len(all_items)
    total_pages = max(1, (total + _SIL_PAGE_SIZE - 1) // _SIL_PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    page_items  = all_items[page * _SIL_PAGE_SIZE: (page + 1) * _SIL_PAGE_SIZE]

    for item in page_items:
        item_idx = _reg_path(item)
        if item.is_dir():
            try:
                child_count = sum(1 for _ in item.iterdir())
                label = f"📁 {item.name} ({child_count})"
            except Exception:
                label = f"📁 {item.name}"
            # Klasöre tıklamak = içine gir
            buttons.append([
                InlineKeyboardButton(label, callback_data=f"sds:cd:0:{item_idx}"),
            ])
        else:
            try:
                sz = _human(item.stat().st_size)
            except Exception:
                sz = "?"
            name_short = (item.name[:38] + "…") if len(item.name) > 38 else item.name
            # Dosyaya tıklamak = direkt sil
            buttons.append([
                InlineKeyboardButton(f"🗑 {name_short} ({sz})", callback_data=f"sds:delfile:{page}:{item_idx}"),
            ])

    if total_pages > 1:
        cur_idx = _reg_path(current_path)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                f"◀️ {page}", callback_data=f"sds:page:{page-1}:{cur_idx}"
            ))
        nav.append(InlineKeyboardButton(
            f"📄 {page+1}/{total_pages}", callback_data="sds:noop:0:0"
        ))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                f"{page+2} ▶️", callback_data=f"sds:page:{page+1}:{cur_idx}"
            ))
        buttons.append(nav)

    if all_items:
        cur_idx = _reg_path(current_path)
        buttons.append([InlineKeyboardButton(
            "🔥 Bu Klasörü Tamamen Temizle", callback_data=f"sds:delall:0:{cur_idx}"
        )])
    buttons.append([InlineKeyboardButton("❌ Kapat", callback_data="sds:close:0:0")])

    return all_items, InlineKeyboardMarkup(buttons) if buttons else None


def _sil_text(current_path: Path, page: int = 0, total_pages: int = 1) -> str:
    try:
        rel = "/" + str(current_path.relative_to(WORK_DIR)) \
              if current_path != WORK_DIR else "/"
    except Exception:
        rel = "/"
    try:
        all_items  = list(current_path.iterdir())
        dirs       = sum(1 for i in all_items if i.is_dir())
        files      = sum(1 for i in all_items if i.is_file())
        total_size = sum(f.stat().st_size for f in current_path.rglob("*") if f.is_file())
        info = f"{dirs} klasör, {files} dosya · {_human(total_size)}"
    except Exception:
        info = "İçerik okunamadı"
    page_info = f" — Sayfa {page+1}/{total_pages}" if total_pages > 1 else ""
    return (
        f"📂 <b>{WORK_DIR}{rel}</b>{page_info}\n"
        f"<i>{info}</i>\n\n"
        f"{_disk_usage_str(WORK_DIR)}"
    )


async def _delete_file_and_db(target: Path) -> str:
    from Backend.helper.encrypt import decode_string as _decode_string
    fname = target.name
    try:
        target.unlink()
        LOGGER.info(f"[sds] Dosya silindi: {target}")
    except Exception as e:
        return f"❌ Dosya silinemedi: {e}"

    db_removed = 0
    try:
        lp_str = str(target)
        for i in range(1, db.current_db_index + 1):
            storage = db.dbs[f"storage_{i}"]
            for col in ("movie", "tv"):
                async for doc in storage[col].find({}):
                    tg_list = []
                    if col == "movie":
                        tg_list = doc.get("telegram", [])
                    else:
                        for s in doc.get("seasons", []):
                            for ep in s.get("episodes", []):
                                tg_list += ep.get("telegram", [])
                    for q in tg_list:
                        qid = q.get("id", "")
                        try:
                            decoded = await _decode_string(qid)
                            if decoded.get("local_path") == lp_str:
                                await db.delete_media_by_stream_id(qid)
                                db_removed += 1
                        except Exception:
                            pass
    except Exception as e:
        LOGGER.warning(f"[sds] DB temizlik hatası: {e}")

    if db_removed:
        return f"✅ Silindi: {fname}\n🗄 DB'den de kaldırıldı ({db_removed} kayıt)"
    return f"✅ Silindi: {fname}\n⚠️ DB'de eşleşen kayıt bulunamadı"


async def _delete_dir_and_db(target: Path) -> tuple:
    """(dname, db_removed, err) döner."""
    from Backend.helper.encrypt import decode_string as _decode_string
    dname      = target.name
    db_removed = 0
    try:
        for f in target.rglob("*"):
            if not f.is_file():
                continue
            lp_str = str(f)
            for i in range(1, db.current_db_index + 1):
                storage = db.dbs[f"storage_{i}"]
                for col in ("movie", "tv"):
                    async for doc in storage[col].find({}):
                        tg_list = []
                        if col == "movie":
                            tg_list = doc.get("telegram", [])
                        else:
                            for s in doc.get("seasons", []):
                                for ep in s.get("episodes", []):
                                    tg_list += ep.get("telegram", [])
                        for q in tg_list:
                            qid = q.get("id", "")
                            try:
                                decoded = await _decode_string(qid)
                                if decoded.get("local_path") == lp_str:
                                    await db.delete_media_by_stream_id(qid)
                                    db_removed += 1
                            except Exception:
                                pass
    except Exception as e:
        LOGGER.warning(f"[sds] Klasör DB temizlik hatası: {e}")

    try:
        shutil.rmtree(target)
        LOGGER.info(f"[sds] Klasör silindi: {target}")
        return dname, db_removed, None
    except Exception as e:
        return dname, db_removed, str(e)


@Client.on_message(
    filters.command("sunucudansil") & filters.private & CustomFilters.owner
)
async def cmd_sunucudansil(client: Client, message: Message):
    _clear_registry()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        items = list(WORK_DIR.iterdir())
    except Exception as e:
        return await message.reply_text(f"❌ Klasör okunamadı: {e}")

    if not items:
        return await message.reply_text(
            f"📂 <b>{WORK_DIR} boş</b>\n{_disk_usage_str(WORK_DIR)}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Kapat", callback_data="sds:close:0:0")
            ]])
        )
    all_items, kb = _sil_keyboard(WORK_DIR, page=0)
    total_pages   = max(1, (len(all_items) + _SIL_PAGE_SIZE - 1) // _SIL_PAGE_SIZE)
    await message.reply_text(
        _sil_text(WORK_DIR, page=0, total_pages=total_pages),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


def _parse_sds_cb(data: str):
    parts  = data.split(":", 3)
    action = parts[1] if len(parts) > 1 else "close"
    try:
        page = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        page = 0
    try:
        idx = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, TypeError):
        idx = 0
    target = _get_path(idx) if idx else WORK_DIR
    if target is None:
        target = WORK_DIR
    return action, page, idx, target


@Client.on_callback_query(filters.regex(r"^sds:"))
async def cb_sunucudansil(client: Client, query: CallbackQuery):
    action, page, idx, target = _parse_sds_cb(query.data)

    if action == "noop":
        return await query.answer()

    if action == "close":
        _clear_registry()
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await query.answer()
        return

    try:
        target.resolve().relative_to(WORK_DIR.resolve())
    except (ValueError, AttributeError):
        return await query.answer("⛔ Güvenlik ihlali!", show_alert=True)

    if action == "page":
        if not target.exists() or not target.is_dir():
            return await query.answer("❌ Klasör bulunamadı.", show_alert=True)
        all_items, kb = _sil_keyboard(target, page=page)
        total_pages   = max(1, (len(all_items) + _SIL_PAGE_SIZE - 1) // _SIL_PAGE_SIZE)
        try:
            await query.message.edit_text(
                _sil_text(target, page=page, total_pages=total_pages),
                parse_mode=ParseMode.HTML, reply_markup=kb
            )
        except Exception:
            pass
        await query.answer()

    elif action == "cd":
        if not target.exists() or not target.is_dir():
            return await query.answer("❌ Klasör bulunamadı.", show_alert=True)
        all_items, kb = _sil_keyboard(target, page=page)
        total_pages   = max(1, (len(all_items) + _SIL_PAGE_SIZE - 1) // _SIL_PAGE_SIZE)
        try:
            await query.message.edit_text(
                _sil_text(target, page=page, total_pages=total_pages),
                parse_mode=ParseMode.HTML, reply_markup=kb
            )
        except Exception:
            pass
        await query.answer()

    elif action == "delfile":
        if not target.exists() or not target.is_file():
            return await query.answer("❌ Dosya bulunamadı.", show_alert=True)
        parent     = target.parent
        result_msg = await _delete_file_and_db(target)
        await query.answer(result_msg, show_alert=True)
        back        = parent if parent.exists() and str(parent).startswith(str(WORK_DIR)) else WORK_DIR
        _clear_registry()
        all_items, kb = _sil_keyboard(back, page=page)
        total_pages = max(1, (len(all_items) + _SIL_PAGE_SIZE - 1) // _SIL_PAGE_SIZE)
        _page = min(page, total_pages - 1)
        # Sayfa taşması: son sayfadan sonra önceki sayfaya git
        if _page != page:
            _clear_registry()
            all_items, kb = _sil_keyboard(back, page=_page)
            total_pages = max(1, (len(all_items) + _SIL_PAGE_SIZE - 1) // _SIL_PAGE_SIZE)
        if not all_items:
            try:
                await query.message.edit_text(
                    f"📂 <b>{WORK_DIR} boş</b>\n{_disk_usage_str(WORK_DIR)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ Kapat", callback_data="sds:close:0:0")
                    ]])
                )
            except Exception:
                pass
        else:
            try:
                await query.message.edit_text(
                    _sil_text(back, page=_page, total_pages=total_pages),
                    parse_mode=ParseMode.HTML, reply_markup=kb
                )
            except Exception:
                pass

    elif action == "deldir":
        if not target.exists() or not target.is_dir():
            return await query.answer("❌ Klasör bulunamadı.", show_alert=True)
        dname, db_removed, err = await _delete_dir_and_db(target)
        if err:
            return await query.answer(f"❌ Hata: {err}", show_alert=True)
        msg = f"✅ Silindi: {dname}"
        if db_removed:
            msg += f"\n🗄 DB'den {db_removed} kayıt kaldırıldı"
        await query.answer(msg, show_alert=True)
        _clear_registry()
        all_items, kb = _sil_keyboard(WORK_DIR, page=0)
        total_pages   = max(1, (len(all_items) + _SIL_PAGE_SIZE - 1) // _SIL_PAGE_SIZE)
        try:
            await query.message.edit_text(
                _sil_text(WORK_DIR, page=0, total_pages=total_pages),
                parse_mode=ParseMode.HTML, reply_markup=kb
            )
        except Exception:
            pass

    elif action == "delall":
        if not target.exists():
            return await query.answer("❌ Hedef bulunamadı.", show_alert=True)
        tname = target.name if target != WORK_DIR else "zipwork (tümü)"

        # Önce DB'den temizle
        from Backend.helper.encrypt import decode_string as _decode_string
        db_removed   = 0
        rglob_target = target if target != WORK_DIR else WORK_DIR
        try:
            for f in rglob_target.rglob("*"):
                if not f.is_file():
                    continue
                lp_str = str(f)
                for i in range(1, db.current_db_index + 1):
                    storage = db.dbs[f"storage_{i}"]
                    for col in ("movie", "tv"):
                        async for doc in storage[col].find({}):
                            tg_list = []
                            if col == "movie":
                                tg_list = doc.get("telegram", [])
                            else:
                                for s in doc.get("seasons", []):
                                    for ep in s.get("episodes", []):
                                        tg_list += ep.get("telegram", [])
                            for q in tg_list:
                                qid = q.get("id", "")
                                try:
                                    decoded = await _decode_string(qid)
                                    if decoded.get("local_path") == lp_str:
                                        await db.delete_media_by_stream_id(qid)
                                        db_removed += 1
                                except Exception:
                                    pass
        except Exception as e:
            LOGGER.warning(f"[sds] Toplu DB temizlik hatası: {e}")

        try:
            if target == WORK_DIR:
                for child in list(target.iterdir()):
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
                LOGGER.info(f"[sds] {WORK_DIR} tamamen temizlendi.")
            else:
                shutil.rmtree(target)
                LOGGER.info(f"[sds] Tamamen silindi: {target}")
            msg = f"✅ Temizlendi: {tname}"
            if db_removed:
                msg += f"\n🗄 DB'den {db_removed} kayıt kaldırıldı"
            await query.answer(msg, show_alert=True)
        except Exception as e:
            LOGGER.error(f"[sds] Toplu silme hatası {target}: {e}")
            return await query.answer(f"❌ Hata: {e}", show_alert=True)

        _clear_registry()
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        all_items, kb = _sil_keyboard(WORK_DIR, page=0)
        total_pages   = max(1, (len(all_items) + _SIL_PAGE_SIZE - 1) // _SIL_PAGE_SIZE)
        try:
            if not list(WORK_DIR.iterdir()):
                await query.message.edit_text(
                    f"📂 <b>{WORK_DIR} boş</b>\n{_disk_usage_str(WORK_DIR)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ Kapat", callback_data="sds:close:0:0")
                    ]])
                )
            else:
                await query.message.edit_text(
                    _sil_text(WORK_DIR, page=0, total_pages=total_pages),
                    parse_mode=ParseMode.HTML, reply_markup=kb
                )
        except Exception:
            pass

    else:
        await query.answer("Bilinmeyen işlem.", show_alert=True)
