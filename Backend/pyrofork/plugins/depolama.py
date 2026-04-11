"""
depolama.py — Pyrogram bot eklentisi
/depolama  → sunucudaki disk kullanımını detaylı gösterir
"""

import asyncio
import os
import shutil
from pathlib import Path

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


def _du(path: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return "—"
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return _bytes(total)
    except Exception:
        return "?"


@Client.on_message(
    filters.command("depolama") & filters.private & CustomFilters.owner,
    group=10,
)
async def cmd_depolama(client: Client, message: Message):
    msg = await message.reply_text(
        "🔍 <b>Disk kullanımı analiz ediliyor…</b>",
        parse_mode=enums.ParseMode.HTML,
    )

    lines = []

    # ── 1. Genel disk durumu (/dev/sda1 veya overlay) ────────────────────────
    disk = shutil.disk_usage("/")
    lines.append(
        "💾 <b>Genel Disk</b>\n"
        f"  Toplam : <b>{_bytes(disk.total)}</b>\n"
        f"  Kullanılan: <b>{_bytes(disk.used)}</b> ({disk.used/disk.total*100:.1f}%)\n"
        f"  Boş   : <b>{_bytes(disk.free)}</b>"
    )

    # ── 2. Kök dizin altındaki büyük klasörler ────────────────────────────────
    du_root = await _run(
        "du -sh /* 2>/dev/null | sort -rh | head -15", timeout=30
    )
    lines.append(f"\n📂 <b>Kök dizin kullanımı (büyükten küçüğe)</b>\n<code>{du_root}</code>")

    # ── 3. Docker ─────────────────────────────────────────────────────────────
    docker_df = await _run("docker system df 2>/dev/null", timeout=20)
    if "REPOSITORY" in docker_df or "Images" in docker_df or "Containers" in docker_df:
        lines.append(f"\n🐳 <b>Docker Genel</b>\n<code>{docker_df}</code>")

        docker_vol = await _run(
            "docker system df -v 2>/dev/null | grep -v '^$' | head -40", timeout=20
        )
        if docker_vol:
            lines.append(f"\n🐳 <b>Docker Volume Detayı</b>\n<code>{docker_vol[:1500]}</code>")
    else:
        lines.append("\nℹ️ Docker bulunamadı veya çalışmıyor.")

    # ── 4. Uygulama dizinleri ─────────────────────────────────────────────────
    app_paths = {
        "uploads (SUNUCU_DIR)": os.getenv("SUNUCU_DIR", "./uploads"),
        "/tmp":                 "/tmp",
        "/tmp/zipwork":         "/tmp/zipwork",
        "log.txt":              "log.txt",
    }

    app_lines = []
    for label, path in app_paths.items():
        p = Path(path)
        if p.exists():
            if p.is_file():
                size = _bytes(p.stat().st_size)
            else:
                size = await _run(f"du -sh '{path}' 2>/dev/null | cut -f1", timeout=15)
                size = size or "?"
            app_lines.append(f"  {label}: <b>{size}</b>")
        else:
            app_lines.append(f"  {label}: —")

    lines.append("\n📁 <b>Uygulama Dizinleri</b>\n" + "\n".join(app_lines))

    # ── 5. MongoDB veri dizini (self-hosted ise) ──────────────────────────────
    for mongo_path in ["/var/lib/mongodb", "/var/lib/mongo", "/data/db"]:
        if Path(mongo_path).exists():
            size = await _run(f"du -sh {mongo_path} 2>/dev/null | cut -f1", timeout=15)
            lines.append(f"\n🍃 <b>MongoDB ({mongo_path})</b>: <b>{size}</b>")

    # ── 6. En büyük dosyalar (/tmp ve uploads altında) ────────────────────────
    big_files = await _run(
        "find /tmp ./uploads 2>/dev/null -type f -size +100M "
        "-exec ls -lh {} \\; 2>/dev/null | sort -rh | head -10",
        timeout=20,
    )
    if big_files:
        lines.append(f"\n🔎 <b>100 MB+ Büyük Dosyalar</b>\n<code>{big_files[:1000]}</code>")
    else:
        lines.append("\n✅ 100 MB+ büyük dosya bulunamadı.")

    # ── Gönder ───────────────────────────────────────────────────────────────
    full_text = "\n".join(lines)
    # Telegram 4096 karakter limiti
    chunks = [full_text[i:i+3800] for i in range(0, len(full_text), 3800)]
    try:
        await msg.edit_text(chunks[0], parse_mode=enums.ParseMode.HTML)
    except Exception:
        await message.reply_text(chunks[0], parse_mode=enums.ParseMode.HTML)
    for chunk in chunks[1:]:
        await message.reply_text(chunk, parse_mode=enums.ParseMode.HTML)
