"""
depolama.py — Pyrogram bot eklentisi
/depolama  → TS yazılımı + sistem + Docker disk/RAM kullanımını gösterir
"""

import asyncio
import os
import shutil
import resource
import psutil
from pathlib import Path

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER


# ── Yardımcı fonksiyonlar ──────────────────────────────────────────────────────

def _bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _pct_bar(used: int, total: int, width: int = 10) -> str:
    """Basit ASCII doluluk çubuğu: ████░░░░░░ %80"""
    if total == 0:
        return "N/A"
    pct = used / total
    filled = round(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {pct*100:.1f}%"


async def _run(cmd: str, timeout: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (out or b"").decode(errors="replace").strip()
    except asyncio.TimeoutError:
        return f"⏱ Zaman aşımı ({timeout}s)"
    except Exception as e:
        return f"Hata: {e}"


def _dir_size(path: str) -> tuple[int, str]:
    """Dizin veya dosyanın byte boyutunu ve formatlanmış stringini döner."""
    try:
        p = Path(path)
        if not p.exists():
            return 0, "—"
        if p.is_file():
            s = p.stat().st_size
            return s, _bytes(s)
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return total, _bytes(total)
    except Exception:
        return 0, "?"


# ── Ana komut ─────────────────────────────────────────────────────────────────

@Client.on_message(
    filters.command("depolama") & filters.private & CustomFilters.owner,
    group=10,
)
async def cmd_depolama(client: Client, message: Message):
    msg = await message.reply_text(
        "🔍 <b>Analiz ediliyor…</b>",
        parse_mode=enums.ParseMode.HTML,
    )

    lines = []

    # ══════════════════════════════════════════════════════════════════════════
    # BÖLÜM 1 — RAM
    # ══════════════════════════════════════════════════════════════════════════
    try:
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # Bu process'in RAM kullanımı
        proc = psutil.Process(os.getpid())
        proc_rss = proc.memory_info().rss  # resident set size

        lines.append(
            "🧠 <b>RAM Durumu</b>\n"
            f"  Toplam : <b>{_bytes(vm.total)}</b>\n"
            f"  Kullanılan: <b>{_bytes(vm.used)}</b>  {_pct_bar(vm.used, vm.total)}\n"
            f"  Boş (available): <b>{_bytes(vm.available)}</b>\n"
            f"  Buffer/Cache: <b>{_bytes(getattr(vm, 'buffers', 0) + getattr(vm, 'cached', 0))}</b>\n"
            f"\n  📌 <b>TS Süreci RAM</b>: <b>{_bytes(proc_rss)}</b>"
        )
        if swap.total > 0:
            lines[-1] += (
                f"\n\n  🔄 <b>Swap</b>: {_bytes(swap.used)} / {_bytes(swap.total)}"
                f"  {_pct_bar(swap.used, swap.total)}"
            )
    except Exception as e:
        lines.append(f"🧠 RAM: Hata — {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # BÖLÜM 2 — GENEL DİSK
    # ══════════════════════════════════════════════════════════════════════════
    disk = shutil.disk_usage("/")
    lines.append(
        "\n💾 <b>Genel Disk (/)</b>\n"
        f"  Toplam : <b>{_bytes(disk.total)}</b>\n"
        f"  Kullanılan: <b>{_bytes(disk.used)}</b>  {_pct_bar(disk.used, disk.total)}\n"
        f"  Boş   : <b>{_bytes(disk.free)}</b>"
    )
    if disk.free < 2 * 1024**3:
        lines[-1] += "\n  ⚠️ <b>KRİTİK: Disk neredeyse dolu!</b>"

    # ══════════════════════════════════════════════════════════════════════════
    # BÖLÜM 3 — TS YAZILIMININ YAZDIĞI YERLER
    # ══════════════════════════════════════════════════════════════════════════
    sunucu_dir = os.getenv("SUNUCU_DIR", "./uploads")

    ts_paths = {
        "📂 SUNUCU_DIR (uploads)": sunucu_dir,
        "📂 log.txt": "log.txt",
        "📂 bot.session (StreamBot)": "bot.session",
        "📂 helper.session (Helper)": "helper.session",
        "📂 config.env": "config.env",
        "📂 gdrive_token.pickle": "gdrive_token.pickle",
        "📂 /tmp (geçici indirmeler)": "/tmp",
    }

    # SUNUCU_DIR altındaki alt dizinler varsa ekle
    try:
        sd = Path(sunucu_dir)
        if sd.exists():
            for child in sorted(sd.iterdir()):
                if child.is_dir():
                    ts_paths[f"   └─ {child.name}/"] = str(child)
    except Exception:
        pass

    ts_lines = []
    ts_total = 0
    for label, path in ts_paths.items():
        sz, sz_str = _dir_size(path)
        ts_total += sz
        p = Path(path)
        kind = "📄" if p.is_file() else ("📁" if p.is_dir() else "✗ ")
        exists = p.exists()
        ts_lines.append(
            f"  {kind} {label}: <b>{sz_str if exists else '—'}</b>"
        )

    lines.append(
        "\n🗂 <b>TS Yazılımının Yazdığı Konumlar</b>\n"
        + "\n".join(ts_lines)
        + f"\n\n  📊 TS Toplam (tahmini): <b>{_bytes(ts_total)}</b>"
    )

    # ══════════════════════════════════════════════════════════════════════════
    # BÖLÜM 4 — DOCKER DEPOLAMA
    # ══════════════════════════════════════════════════════════════════════════
    docker_check = await _run("docker info 2>/dev/null | head -1", timeout=10)
    if "error" in docker_check.lower() or not docker_check:
        lines.append("\n🐳 <b>Docker</b>: Bulunamadı veya çalışmıyor.")
    else:
        # docker system df
        docker_df = await _run("docker system df 2>/dev/null", timeout=20)
        lines.append(f"\n🐳 <b>Docker — Genel Kullanım</b>\n<code>{docker_df}</code>")

        # Çalışan container'lar ve memory
        docker_ps = await _run(
            "docker stats --no-stream --format "
            "'{{.Name}}|{{.MemUsage}}|{{.MemPerc}}|{{.BlockIO}}' 2>/dev/null",
            timeout=20,
        )
        if docker_ps:
            ps_lines = []
            for row in docker_ps.splitlines():
                parts = row.split("|")
                if len(parts) == 4:
                    name, mem, mem_pct, blk = parts
                    ps_lines.append(
                        f"  🔹 <b>{name.strip()}</b>\n"
                        f"     RAM: {mem.strip()} ({mem_pct.strip()})\n"
                        f"     Disk I/O: {blk.strip()}"
                    )
            if ps_lines:
                lines.append("\n🐳 <b>Container RAM &amp; Disk I/O</b>\n" + "\n".join(ps_lines))

        # Volume detayları
        docker_vols = await _run(
            "docker system df -v 2>/dev/null | awk '/^VOLUME NAME/,/^$/' | head -20",
            timeout=20,
        )
        if docker_vols and "VOLUME" in docker_vols:
            lines.append(f"\n🐳 <b>Docker Volume'ler</b>\n<code>{docker_vols}</code>")

        # overlay2 boyutu (host diskini etkileyen en büyük Docker kalemi)
        overlay_size = await _run(
            "du -sh /var/lib/docker/overlay2 2>/dev/null | cut -f1", timeout=30
        )
        if overlay_size and overlay_size != "?":
            lines.append(
                f"\n🐳 <b>Docker Image Katmanları</b> (/var/lib/docker/overlay2): "
                f"<b>{overlay_size}</b>\n"
                "  💡 Temizlemek: <code>docker system prune -af --volumes</code> (DİKKATLİ!)"
            )

    # ══════════════════════════════════════════════════════════════════════════
    # BÖLÜM 5 — SİLİNMİŞ AMA AÇIK DOSYALAR (df/du FARKI)
    # ══════════════════════════════════════════════════════════════════════════
    deleted = await _run(
        "lsof 2>/dev/null | grep '(deleted)' | "
        "awk '{print $1,$7,$NF}' | sort -k2 -rn | head -10",
        timeout=20,
    )
    if deleted and "(deleted)" in deleted:
        lines.append(
            f"\n⚠️ <b>Silinmiş ama Açık Dosyalar</b> (disk boşalmıyor!)\n"
            f"<code>{deleted[:1000]}</code>\n"
            "  💡 Düzelt: Botu veya ilgili servisi yeniden başlat."
        )
    else:
        lines.append("\n✅ Silinmiş-ama-açık dosya yok. (df/du farkı başka kaynaktan)")

    # ══════════════════════════════════════════════════════════════════════════
    # BÖLÜM 6 — 100MB+ BÜYÜK DOSYALAR (tüm sistem)
    # ══════════════════════════════════════════════════════════════════════════
    big_files = await _run(
        "find / -xdev -type f -size +100M "
        "-exec ls -lh {} \\; 2>/dev/null | sort -rh | head -10",
        timeout=45,
    )
    if big_files:
        lines.append(
            f"\n🔎 <b>100MB+ Büyük Dosyalar</b>\n<code>{big_files[:1200]}</code>"
        )
    else:
        lines.append("\n✅ 100MB+ büyük dosya bulunamadı.")

    # ── Gönder ───────────────────────────────────────────────────────────────
    full_text = "\n".join(lines)
    chunks = [full_text[i:i+3800] for i in range(0, len(full_text), 3800)]
    try:
        await msg.edit_text(chunks[0], parse_mode=enums.ParseMode.HTML)
    except Exception:
        await message.reply_text(chunks[0], parse_mode=enums.ParseMode.HTML)
    for chunk in chunks[1:]:
        await message.reply_text(chunk, parse_mode=enums.ParseMode.HTML)
