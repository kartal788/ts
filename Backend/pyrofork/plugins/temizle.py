"""
temizle.py — Pyrogram bot eklentisi
/temizle   → sistem temizliği (cache, RAM, tmp, docker, journald)
/ramraporu → process bazında RAM kullanımı
"""

import asyncio
import ctypes
import gc
import os
import shutil
import sys
from datetime import datetime, timedelta

import psutil
from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER


def _bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def _disk_info() -> str:
    d = psutil.disk_usage("/")
    return f"💾 <b>Disk:</b> {_bytes(d.used)} / {_bytes(d.total)} ({d.percent:.1f}% dolu)"

def _ram_info() -> str:
    r = psutil.virtual_memory()
    return f"🧠 <b>RAM:</b> {_bytes(r.used)} / {_bytes(r.total)} ({r.percent:.1f}% dolu)"

async def _run(cmd: str, timeout: int = 30) -> tuple:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, (out or b"").decode(errors="replace").strip()
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        return -1, f"Zaman aşımı ({timeout}s)"
    except Exception as e:
        return -1, str(e)


# ── /ramraporu ────────────────────────────────────────────────────────────────

def _get_ram_report() -> str:
    lines = []

    # 1. Sistem geneli
    vm = psutil.virtual_memory()
    lines.append(
        f"<b>🖥 Sistem RAM</b>\n"
        f"  Toplam : <b>{_bytes(vm.total)}</b>\n"
        f"  Kullanılan: <b>{_bytes(vm.used)}</b> ({vm.percent:.1f}%)\n"
        f"  Boş    : <b>{_bytes(vm.available)}</b>\n"
        f"  Tampon/Cache: <b>{_bytes(vm.buffers + vm.cached if hasattr(vm,'cached') else vm.buffers)}</b>"
    )

    # 2. Process bazında sıralama (RSS = gerçek fiziksel RAM)
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline', 'memory_info']):
        try:
            mi = p.info['memory_info']
            if mi and mi.rss > 10 * 1024 * 1024:  # 10 MB üzeri göster
                cmd = " ".join(p.info['cmdline'] or [])[:50] if p.info['cmdline'] else p.info['name']
                procs.append((mi.rss, p.info['pid'], p.info['name'], cmd))
        except Exception:
            pass

    procs.sort(reverse=True)
    lines.append(f"\n<b>📋 Process Bazında RAM (RSS) — İlk 15</b>")
    for rss, pid, name, cmd in procs[:15]:
        lines.append(f"  [{pid:>6}] <b>{_bytes(rss):>10}</b>  {name[:15]:<15}  <i>{cmd[:40]}</i>")

    # 3. Bu bot process'inin kendi detayları
    me = psutil.Process(os.getpid())
    mi = me.memory_info()
    lines.append(
        f"\n<b>🤖 Bu Bot Process</b>\n"
        f"  PID : {me.pid}\n"
        f"  RSS (fiziksel) : <b>{_bytes(mi.rss)}</b>\n"
        f"  VMS (sanal)    : <b>{_bytes(mi.vms)}</b>"
    )

    return "\n".join(lines)


# ── /temizle ──────────────────────────────────────────────────────────────────

async def _step_mongodb(lines: list):
    try:
        from Backend import db
        tracking_db = db.dbs.get("tracking", None)
        if tracking_db is None:
            lines.append("ℹ️ MongoDB: tracking DB bulunamadı")
            return
        try:
            cutoff = datetime.utcnow() - timedelta(days=30)
            result = await db.dbs["tracking"]["stream_analytics"].delete_many(
                {"logged_at": {"$lt": cutoff}}
            )
            lines.append(f"✅ stream_analytics — <b>{result.deleted_count}</b> eski kayıt silindi")
        except Exception as e:
            lines.append(f"⚠️ stream_analytics: {e}")
    except Exception as e:
        lines.append(f"⚠️ MongoDB: {e}")


def _step_ram(lines: list):
    ram_before = psutil.virtual_memory().used
    total_cleared = 0

    # ── eskiverileriyenile _tr_cache ──
    try:
        import Backend.pyrofork.plugins.eskiverileriyenile as eski
        count = len(eski._tr_cache)
        eski._tr_cache.clear()
        total_cleared += count
        lines.append(f"✅ Çeviri cache (eskiverileriyenile) — <b>{count}</b> giriş temizlendi")
    except Exception as e:
        lines.append(f"ℹ️ Çeviri cache: {e}")

    # ── ACTIVE_STREAMS ──
    try:
        from Backend.helper.custom_dl import ACTIVE_STREAMS
        count = len(ACTIVE_STREAMS)
        ACTIVE_STREAMS.clear()
        total_cleared += count
        lines.append(f"✅ ACTIVE_STREAMS — <b>{count}</b> kayıt temizlendi")
    except Exception as e:
        lines.append(f"ℹ️ ACTIVE_STREAMS: {e}")

    # ── ByteStreamer._file_id_cache (tüm instance'lar) ──
    try:
        from Backend.helper.custom_dl import ByteStreamer
        file_id_count = 0
        streamer_count = 0
        for idx, streamer in ByteStreamer._instances.items():
            cnt = len(streamer._file_id_cache)
            streamer._file_id_cache.clear()
            file_id_count += cnt
            streamer_count += 1
        total_cleared += file_id_count
        lines.append(f"✅ ByteStreamer file_id cache — <b>{file_id_count}</b> giriş, {streamer_count} instance temizlendi")
    except Exception as e:
        lines.append(f"ℹ️ ByteStreamer cache: {e}")

    # ── metadata.py cache dict'leri ──
    try:
        import Backend.helper.metadata as meta
        meta_map = {
            "IMDB_CACHE": getattr(meta, "IMDB_CACHE", None),
            "TMDB_SEARCH_CACHE": getattr(meta, "TMDB_SEARCH_CACHE", None),
            "TMDB_DETAILS_CACHE": getattr(meta, "TMDB_DETAILS_CACHE", None),
            "EPISODE_CACHE": getattr(meta, "EPISODE_CACHE", None),
            "TRANSLATE_CACHE": getattr(meta, "TRANSLATE_CACHE", None),
            "TRANSLATE_DE_CACHE": getattr(meta, "TRANSLATE_DE_CACHE", None),
        }
        meta_total = 0
        meta_names = []
        for name, cache in meta_map.items():
            if isinstance(cache, dict) and len(cache) > 0:
                cnt = len(cache)
                cache.clear()
                meta_total += cnt
                meta_names.append(f"{name}({cnt})")
                total_cleared += cnt
        if meta_total > 0:
            lines.append(f"✅ Metadata cache — <b>{meta_total}</b> giriş: {', '.join(meta_names)}")
        else:
            lines.append("ℹ️ Metadata cache: zaten boş")
    except Exception as e:
        lines.append(f"ℹ️ Metadata cache: {e}")

    # ── tmdb_catalog modül düzeyindeki cache'ler ──
    try:
        import Backend.helper.tmdb_catalog as tmdb
        tmdb_attrs = [a for a in dir(tmdb) if "cache" in a.lower() or "Cache" in a]
        tmdb_total = 0
        for attr in tmdb_attrs:
            cache = getattr(tmdb, attr, None)
            if isinstance(cache, dict) and len(cache) > 0:
                cnt = len(cache)
                cache.clear()
                tmdb_total += cnt
                total_cleared += cnt
        if tmdb_total > 0:
            lines.append(f"✅ TMDB catalog cache — <b>{tmdb_total}</b> giriş temizlendi")
    except Exception as e:
        lines.append(f"ℹ️ TMDB catalog cache: {e}")

    # ── platform_catalog modül düzeyindeki cache'ler ──
    try:
        import Backend.helper.platform_catalog as plat
        plat_attrs = [a for a in dir(plat) if "cache" in a.lower() or "Cache" in a]
        plat_total = 0
        for attr in plat_attrs:
            cache = getattr(plat, attr, None)
            if isinstance(cache, dict) and len(cache) > 0:
                cnt = len(cache)
                cache.clear()
                plat_total += cnt
                total_cleared += cnt
        if plat_total > 0:
            lines.append(f"✅ Platform catalog cache — <b>{plat_total}</b> giriş temizlendi")
    except Exception as e:
        lines.append(f"ℹ️ Platform catalog cache: {e}")

    # ── Python GC ──
    gc.collect(0)
    gc.collect(1)
    collected = gc.collect(2)
    lines.append(f"✅ Python GC — <b>{collected}</b> nesne temizlendi")

    # ── stream_token MediaTokenManager._store ──
    try:
        from Backend.helper.stream_token import media_token_manager
        import time as _time
        now = _time.monotonic()
        # Önce süresi dolmuşları temizle
        expired_keys = [k for k, v in media_token_manager._store.items() if v.get("expires_at", 0) <= now]
        for k in expired_keys:
            del media_token_manager._store[k]
        remaining = len(media_token_manager._store)
        total_cleared += len(expired_keys)
        lines.append(f"✅ stream_token store — <b>{len(expired_keys)}</b> süresi dolmuş token silindi, {remaining} aktif kaldı")
    except Exception as e:
        lines.append(f"ℹ️ stream_token: {e}")

    # ── RECENT_STREAMS deque ──
    try:
        from Backend.helper.custom_dl import RECENT_STREAMS
        count = len(RECENT_STREAMS)
        RECENT_STREAMS.clear()
        total_cleared += count
        lines.append(f"✅ RECENT_STREAMS — <b>{count}</b> kayıt temizlendi")
    except Exception as e:
        lines.append(f"ℹ️ RECENT_STREAMS: {e}")

    # ── malloc_trim: OS'a fiziksel belleği iade et ──
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        lines.append("✅ malloc_trim — OS'a bellek iade edildi")
    except Exception:
        lines.append("ℹ️ malloc_trim: atlandı")

    ram_after = psutil.virtual_memory().used
    freed = max(0, ram_before - ram_after)
    lines.append(
        f"📉 RAM değişimi: <b>-{_bytes(freed)}</b> "
        f"({_bytes(ram_before)} → {_bytes(ram_after)}) | "
        f"Toplam <b>{total_cleared}</b> cache girişi silindi"
    )


def _step_log(lines: list):
    log_path = "log.txt"
    try:
        import logging
        root_logger = logging.getLogger()

        # Tüm handler'ları kapat ve kaldır
        for handler in root_logger.handlers[:]:
            if isinstance(handler, logging.FileHandler) and "log.txt" in handler.baseFilename:
                handler.close()
                root_logger.removeHandler(handler)

        # log.txt + rotate yedekleri (log.txt.1, log.txt.2, …) hepsini sil
        total_size = 0
        deleted = []
        base_dir = os.path.dirname(os.path.abspath(log_path)) or "."
        for fname in os.listdir(base_dir):
            fpath = os.path.join(base_dir, fname)
            if fname == "log.txt" or (fname.startswith("log.txt.") and fname[8:].isdigit()):
                try:
                    total_size += os.path.getsize(fpath)
                    os.remove(fpath)
                    deleted.append(fname)
                except Exception:
                    pass

        # log.txt'yi sıfırdan oluştur ve handler'ı yeniden ekle
        open(log_path, "w").close()
        from Backend.logger import file_handler
        file_handler.stream = open(log_path, "a", encoding="utf-8")
        root_logger.addHandler(file_handler)

        detail = f" ({', '.join(sorted(deleted))})" if deleted else ""
        lines.append(f"✅ Log dosyaları{detail} — <b>{_bytes(total_size)}</b> temizlendi")
    except Exception as e:
        lines.append(f"⚠️ log.txt: {e}")


async def _step_tmp(lines: list):
    try:
        freed = 0
        for entry in os.scandir("/tmp"):
            try:
                if entry.is_dir(follow_symlinks=False):
                    sz = sum(f.stat().st_size for f in os.scandir(entry.path) if f.is_file())
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    sz = entry.stat(follow_symlinks=False).st_size
                    os.remove(entry.path)
                freed += sz
            except Exception:
                pass
        lines.append(f"✅ /tmp — <b>{_bytes(freed)}</b> boşaltıldı")
    except Exception as e:
        lines.append(f"⚠️ /tmp: {e}")


@Client.on_message(
    filters.command("ramraporu") & filters.private & CustomFilters.owner,
    group=10,
)
async def cmd_ramraporu(client: Client, message: Message):
    msg = await message.reply_text("🔍 <b>RAM analizi yapılıyor…</b>", parse_mode=enums.ParseMode.HTML)
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, _get_ram_report)
    try:
        await msg.edit_text(report, parse_mode=enums.ParseMode.HTML)
    except Exception:
        chunks = [report[i:i+3800] for i in range(0, len(report), 3800)]
        await msg.edit_text(chunks[0], parse_mode=enums.ParseMode.HTML)
        for chunk in chunks[1:]:
            await message.reply_text(chunk, parse_mode=enums.ParseMode.HTML)


@Client.on_message(
    filters.command("temizle") & filters.private & CustomFilters.owner,
    group=10,
)
async def cmd_temizle(client: Client, message: Message):
    msg = await message.reply_text("🧹 <b>Temizlik başlatılıyor…</b>", parse_mode=enums.ParseMode.HTML)
    lines: list = []

    await _step_mongodb(lines)
    await msg.edit_text("🧹 <b>(1/5) MongoDB…</b>\n\n" + "\n".join(f"  {l}" for l in lines), parse_mode=enums.ParseMode.HTML)

    _step_ram(lines)
    await msg.edit_text("🧹 <b>(2/5) RAM & Cache…</b>\n\n" + "\n".join(f"  {l}" for l in lines), parse_mode=enums.ParseMode.HTML)

    await _step_tmp(lines)
    rc, _ = await _run("pip cache purge", 20)
    lines.append("✅ pip cache temizlendi" if rc == 0 else "ℹ️ pip cache: atlandı")
    rc, _ = await _run("uv cache clean", 20)
    lines.append("✅ uv cache temizlendi" if rc == 0 else "ℹ️ uv cache: atlandı")
    await msg.edit_text("🧹 <b>(3/5) Dosya cache…</b>\n\n" + "\n".join(f"  {l}" for l in lines), parse_mode=enums.ParseMode.HTML)

    _step_log(lines)
    await msg.edit_text("🧹 <b>(4/5) Log dosyası…</b>\n\n" + "\n".join(f"  {l}" for l in lines), parse_mode=enums.ParseMode.HTML)

    rc, _ = await _run("docker info", 8)
    if rc == 0:
        rc2, out2 = await _run("docker system prune -f", 60)
        fl = next((l for l in out2.splitlines() if "reclaimed" in l.lower()), "") if rc2 == 0 else ""
        lines.append("✅ Docker temizlendi" + (f" — {fl.strip()}" if fl else "") if rc2 == 0 else f"⚠️ Docker: {out2[:60]}")
    else:
        lines.append("ℹ️ Docker: kurulu değil")

    rc, _ = await _run("journalctl --vacuum-size=100M", 20)
    lines.append("✅ journald 100MB sınırlandı" if rc == 0 else "ℹ️ journald: atlandı")

    await msg.edit_text("🧹 <b>(5/5) Docker & Journald…</b>\n\n" + "\n".join(f"  {l}" for l in lines), parse_mode=enums.ParseMode.HTML)

    result = "\n".join(f"  {l}" for l in lines)
    final = (
        "🧹 <b>Temizlik Tamamlandı</b>\n\n"
        f"{result}\n\n"
        "─────────────────────\n"
        f"{_disk_info()}\n"
        f"{_ram_info()}\n\n"
        "💡 <i>Process bazlı RAM için /ramraporu yazın.</i>"
    )
    try:
        await msg.edit_text(final, parse_mode=enums.ParseMode.HTML)
    except Exception:
        await message.reply_text(final, parse_mode=enums.ParseMode.HTML)
    LOGGER.info("Temizlik komutu tamamlandı.")
