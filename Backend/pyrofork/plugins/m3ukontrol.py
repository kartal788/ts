"""
m3ukontrol.py — M3U Link Kontrol Sistemi
Kullanım: /m3ukontrol <link1> <link2> ...

Mantık:
- Her linke bağlan, sunucu gerçek bir .m3u dosyası indiriyor mu kontrol et
- Çalışan = HTTP 200 + Content-Disposition'da .m3u dosya adı VEYA
             içerik #EXTM3U ile başlıyor VEYA
             content-type m3u/mpegurl içeriyor
- TXT'ye sadece çalışan linkleri yaz (başka bir şey yazma)
- Telegram'a özet mesaj + txt dosyası gönder
"""

import asyncio
import aiohttp
import time
import io
from datetime import datetime

from pyrogram import filters, Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER

REQUEST_TIMEOUT = 15
CONCURRENT_REQUESTS = 10


async def check_m3u_link(session: aiohttp.ClientSession, url: str) -> bool:
    """
    Linkin gerçek bir .m3u dosyası döndürüp döndürmediğini kontrol eder.
    True = çalışıyor, False = çalışmıyor.

    Çalışan sayılma koşulları (en az biri sağlanmalı):
      1. Content-Disposition header'ında .m3u veya .m3u8 uzantılı dosya adı varsa
         (örn: filename="tv_channels_ayhanuc1276nx_plus.m3u")
      2. Content-Type header'ı mpegurl / m3u içeriyorsa
      3. Dönen verinin ilk bytes'ı #EXTM3U ile başlıyorsa
    """
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return False

            content_type = resp.headers.get("Content-Type", "").lower()
            content_disposition = resp.headers.get("Content-Disposition", "").lower()

            # Koşul 1: Content-Disposition'da .m3u dosya adı
            if ".m3u" in content_disposition or ".m3u8" in content_disposition:
                return True

            # Koşul 2: Content-Type m3u / mpegurl
            if "mpegurl" in content_type or "m3u" in content_type:
                return True

            # Koşul 3: İlk 256 byte'ı oku, #EXTM3U başlığı var mı?
            chunk = await resp.content.read(256)
            if chunk.strip().startswith(b"#EXTM3U"):
                return True

            return False

    except Exception:
        return False


async def check_all_links(urls: list, progress_msg: Message):
    """
    Tüm linkleri kontrol eder.
    Döner: (çalışan_url_listesi, toplam_sayı)
    """
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    working = []
    checked = 0
    total = len(urls)
    last_update = time.time()
    lock = asyncio.Lock()

    connector = aiohttp.TCPConnector(limit=CONCURRENT_REQUESTS, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:

        async def check_one(url):
            nonlocal checked, last_update
            async with semaphore:
                ok = await check_m3u_link(session, url)
                async with lock:
                    checked += 1
                    if ok:
                        working.append(url)
                    now = time.time()
                    if now - last_update >= 3 or checked == total:
                        last_update = now
                        try:
                            await progress_msg.edit_text(
                                f"🔍 <b>M3U Kontrol Ediliyor...</b>\n\n"
                                f"⏳ İşlenen: <b>{checked}/{total}</b>\n"
                                f"✅ Çalışan: <b>{len(working)}</b>\n"
                                f"❌ Çalışmayan: <b>{checked - len(working)}</b>",
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception:
                            pass

        await asyncio.gather(*[check_one(url) for url in urls])

    return working, total


@Client.on_message(
    filters.command("m3ukontrol") & filters.private & CustomFilters.owner
)
async def cmd_m3ukontrol(client: Client, message: Message):
    raw = message.text or message.caption or ""
    parts = raw.split()

    if len(parts) < 2:
        return await message.reply_text(
            "❌ <b>Kullanım:</b>\n"
            "<code>/m3ukontrol link1 link2 ...</code>",
            parse_mode=ParseMode.HTML,
        )

    # http ile başlayan linkleri ayıkla, mükerrerleri kaldır
    seen = set()
    unique_urls = []
    for p in parts[1:]:
        u = p.strip()
        if u.startswith("http") and u not in seen:
            seen.add(u)
            unique_urls.append(u)

    if not unique_urls:
        return await message.reply_text(
            "❌ Geçerli link bulunamadı. Linkler <code>http://</code> ile başlamalıdır.",
            parse_mode=ParseMode.HTML,
        )

    progress_msg = await message.reply_text(
        f"🔍 <b>M3U Kontrol Başlatılıyor...</b>\n\n"
        f"📋 Toplam link: <b>{len(unique_urls)}</b>\n"
        f"⏳ Lütfen bekleyin...",
        parse_mode=ParseMode.HTML,
    )

    LOGGER.info(f"[m3ukontrol] {len(unique_urls)} link kontrol başladı")

    try:
        working, total = await check_all_links(unique_urls, progress_msg)
    except Exception as e:
        LOGGER.error(f"[m3ukontrol] Hata: {e}")
        return await progress_msg.edit_text(
            f"❌ Hata oluştu:\n<code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )

    failed_count = total - len(working)

    # Özet mesajı
    await progress_msg.edit_text(
        f"✅ <b>M3U Kontrol Tamamlandı!</b>\n\n"
        f"📋 <b>Toplam:</b> {total}\n"
        f"✅ <b>Çalışan:</b> {len(working)}\n"
        f"❌ <b>Çalışmayan:</b> {failed_count}",
        parse_mode=ParseMode.HTML,
    )

    # TXT: sadece çalışan linkler, her satıra bir tane, başka hiçbir şey yok
    if working:
        txt_content = "\n".join(working)
        txt_file = io.BytesIO(txt_content.encode("utf-8"))
        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"m3u_calisan_{now_str}.txt"
        txt_file.name = filename

        try:
            await message.reply_document(
                document=txt_file,
                caption=f"✅ Çalışan linkler ({len(working)} adet)",
                file_name=filename,
            )
            LOGGER.info(f"[m3ukontrol] Tamamlandı: {len(working)} çalışan / {total} toplam")
        except Exception as e:
            LOGGER.error(f"[m3ukontrol] Dosya gönderilemedi: {e}")
            await message.reply_text(
                f"⚠️ Dosya gönderilemedi: <code>{str(e)[:100]}</code>",
                parse_mode=ParseMode.HTML,
            )
    else:
        LOGGER.info(f"[m3ukontrol] Çalışan link yok. Toplam: {total}")
