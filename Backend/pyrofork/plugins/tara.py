"""
tara.py — Pyrogram bot eklentisi
=================================
Komutlar (sadece OWNER, özel mesaj):

    /tara             — Önce tüm DB'yi sil, sonra baştan tara (nükleer seçenek)
    /tara db          — AUTH_CHANNEL kanallarını tara, DB'deki mevcut kayıtları atla
    /tara_durum       — Devam eden taramanın istatistiklerini göster
    /tara_iptal       — Devam eden taramayı durdur

Neden get_messages?
  Botlar get_chat_history ve search_messages kullanamaz (BOT_METHOD_INVALID).
  get_messages(chat_id, [id1, id2, ...]) ise botlarla çalışır.
  Strateji: ID 1'den başlayıp 200'lük batch'lerle ileri gidilir.
  5 ardışık boş batch → tüm geçmiş tarandı, dur.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import time

from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import FloodWait, ChannelPrivate, ChatAdminRequired

from Backend.config import Telegram
from Backend.helper.custom_filter import CustomFilters
from Backend.helper.metadata import metadata, extract_default_id
from Backend.helper.pyro import clean_filename, get_readable_file_size, remove_urls
from Backend.helper.encrypt import encode_string, decode_string
from Backend.logger import LOGGER
from Backend import db


# ─────────────────────────────────────────────────────────────
# Tarama durumu (aynı anda tek iş)
# ─────────────────────────────────────────────────────────────
class _TaraState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.running        = False
        self.cancelled      = False
        self.channel_id     = None
        self.channel_name   = ""
        self.total_found    = 0
        self.processed      = 0
        self.indexed        = 0
        self.skipped_dup    = 0
        self.skipped_meta   = 0
        self.skipped_nonvid = 0
        self.errors         = 0
        self.started_at     = 0.0
        self.status_msg: Message | None = None
        # Atlanan / hata veren mesajların ayrıntılı kaydı
        self.skip_log: list[str] = []

    @property
    def elapsed(self) -> str:
        s = int(time.time() - self.started_at) if self.started_at else 0
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}s {m}d {s}sn" if h else (f"{m}d {s}sn" if m else f"{s}sn")


state = _TaraState()

PROGRESS_EVERY   = 20   # kaçta bir durum mesajı güncellenir
RATE_LIMIT_DELAY = 0.3  # batch'ler arası bekleme (sn)
BATCH_SIZE       = 200  # tek seferde çekilecek mesaj ID sayısı
MAX_EMPTY        = 5    # ardışık boş batch limiti → dur
MAX_ID_CAP       = 500_000  # sonsuz döngü önlemi


# ─────────────────────────────────────────────────────────────
# Yardımcılar
# ─────────────────────────────────────────────────────────────
async def _already_indexed(channel_int: int, msg_id: int) -> bool:
    """Bu mesaj zaten DB'de var mı?"""
    try:
        h = await encode_string({"chat_id": channel_int, "msg_id": msg_id})
    except Exception:
        return False
    for i in range(1, db.current_db_index + 1):
        storage = db.dbs.get(f"storage_{i}")
        if storage is None:
            continue
        if await storage["movie"].find_one({"telegram.id": h}):
            return True
        if await storage["tv"].find_one({"seasons.episodes.telegram.id": h}):
            return True
    return False


async def _push_progress(force: bool = False):
    s = state
    if not s.status_msg:
        return
    if not force and s.processed % PROGRESS_EVERY != 0:
        return
    try:
        text = (
            f"<blockquote>📡 <b>Taranıyor:</b> {s.channel_name}</blockquote>\n\n"
            f"⏱ Geçen süre    : <code>{s.elapsed}</code>\n"
            f"📨 İşlenen       : <code>{s.processed}</code>\n"
            f"✅ Eklenen       : <code>{s.indexed}</code>\n"
            f"⏭ Atlandı (DB)  : <code>{s.skipped_dup}</code>\n"
            f"⚠️ Atlandı (meta): <code>{s.skipped_meta}</code>\n"
            f"📎 Atlandı (tip) : <code>{s.skipped_nonvid}</code>\n"
            f"❌ Hata          : <code>{s.errors}</code>\n\n"
            f"📌 Durdurmak için /tara_iptal"
        )
        await s.status_msg.edit_text(text, parse_mode=enums.ParseMode.HTML)
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Tek kanal tarama
# ─────────────────────────────────────────────────────────────
async def _scan_channel(client: Client, chat_id: int):
    """
    ID 1'den başlayarak 200'lük batch'lerle get_messages çağırır.
    Botlarla çalışan tek yöntem budur.
    """
    s = state

    # Kanal adını çek
    try:
        chat = await client.get_chat(chat_id)
        s.channel_name = getattr(chat, "title", str(chat_id))
    except Exception as e:
        s.channel_name = str(chat_id)
        LOGGER.warning(f"[/tara] Kanal adı alınamadı {chat_id}: {e}")

    s.channel_id = chat_id
    channel_str = str(chat_id).replace("-100", "")
    channel_int = int(channel_str)

    LOGGER.info(f"[/tara] Tarama başlıyor: {s.channel_name} ({chat_id})")

    empty_streak = 0
    current      = 1

    while empty_streak < MAX_EMPTY and current < MAX_ID_CAP:
        if s.cancelled:
            LOGGER.info("[/tara] Kullanıcı tarafından iptal edildi.")
            break

        batch_ids = list(range(current, min(current + BATCH_SIZE, MAX_ID_CAP)))

        # get_messages — botlarla uyumlu
        try:
            messages = await client.get_messages(chat_id, batch_ids)
        except FloodWait as e:
            LOGGER.info(f"[/tara] FloodWait {e.value}s, bekleniyor…")
            await asyncio.sleep(e.value)
            try:
                messages = await client.get_messages(chat_id, batch_ids)
            except Exception as ex:
                LOGGER.error(f"[/tara] Yeniden deneme başarısız (ID {current}): {ex}")
                state.skip_log.append(
                    f"[HATA-BATCH] msg_id_aralığı={current}-{current+BATCH_SIZE-1} "
                    f"| hata={ex}"
                )
                s.errors += 1
                current += BATCH_SIZE
                empty_streak += 1
                continue
        except Exception as e:
            LOGGER.error(f"[/tara] Batch hatası (ID {current}): {e}")
            state.skip_log.append(
                f"[HATA-BATCH] msg_id_aralığı={current}-{current+BATCH_SIZE-1} "
                f"| hata={e}"
            )
            s.errors += 1
            current += BATCH_SIZE
            empty_streak += 1
            continue

        if not isinstance(messages, list):
            messages = [messages]

        batch_had_content = False

        for message in messages:
            if s.cancelled:
                break

            if message.empty:
                continue

            batch_had_content = True
            s.total_found += 1

            # Arşiv uzantısı/MIME kontrolü
            _ARCHIVE_MIMES = {
                "application/zip",
                "application/x-zip-compressed",
                "application/x-7z-compressed",
                "application/x-rar-compressed",
                "application/vnd.rar",
            }
            # Parçalı arşiv uzantıları: .zip.001, .z01, .7z.001, .7z.002 ...
            _ARCHIVE_EXT_RE = re.compile(
                r"\.(zip|7z|rar|z\d+)(\.\d+)?$", re.IGNORECASE
            )

            # Video kontrolü
            is_video     = bool(message.video)
            is_video_doc = False
            is_archive   = False
            if message.document and not is_video:
                mime     = getattr(message.document, "mime_type", "") or ""
                fname    = getattr(message.document, "file_name", "") or ""
                is_video_doc = mime.startswith("video/")
                if not is_video_doc:
                    is_archive = (
                        mime in _ARCHIVE_MIMES
                        or bool(_ARCHIVE_EXT_RE.search(fname))
                    )

            if not (is_video or is_video_doc or is_archive):
                mime_log  = getattr(message.document, "mime_type", "—") if message.document else "—"
                fname_log = getattr(message.document, "file_name", "—") if message.document else "—"
                tip_log   = (
                    "fotoğraf"   if message.photo   else
                    "ses/müzik"  if (message.audio or message.voice) else
                    "sticker"    if message.sticker  else
                    "animasyon"  if message.animation else
                    "metin"      if (not message.document) else
                    f"document ({mime_log})"
                )
                state.skip_log.append(
                    f"[TİP-DIŞI] msg_id={message.id} | neden={tip_log} "
                    f"| mime={mime_log} | dosya={fname_log}"
                )
                s.skipped_nonvid += 1
                s.processed      += 1
                await _push_progress()
                continue

            file     = message.video or message.document
            title    = message.caption or (file.file_name if file else None)
            msg_id   = message.id
            size     = get_readable_file_size(file.file_size) if file and file.file_size else "?"

            if not title:
                state.skip_log.append(
                    f"[META-BAŞLIK YOK] msg_id={message.id} "
                    f"| dosya={getattr(file, 'file_name', '—') if file else '—'} "
                    f"| boyut={size}"
                )
                s.skipped_meta += 1
                s.processed    += 1
                await _push_progress()
                continue

            # Mükerrer kontrol
            try:
                if await _already_indexed(channel_int, msg_id):
                    s.skipped_dup += 1
                    s.processed   += 1
                    await _push_progress()
                    continue
            except Exception as e:
                LOGGER.warning(f"[/tara] Mükerrer kontrol hatası msg {msg_id}: {e}")

            # Başlık veya dosya adındaki TMDB/IMDB linkini çıkar → override_id
            override_id = None
            try:
                _url_match = re.search(
                    r'https?://(?:www\.)?(?:themoviedb\.org|imdb\.com)/\S+',
                    title,
                    re.IGNORECASE,
                )
                if _url_match:
                    _oid, _ = extract_default_id(_url_match.group(0))
                    if _oid:
                        override_id = _url_match.group(0)
                        LOGGER.info(
                            f"[/tara] msg {msg_id}: URL bulundu → override_id={override_id!r}"
                        )
            except Exception as _oe:
                LOGGER.warning(f"[/tara] override_id çıkarma hatası msg {msg_id}: {_oe}")

            # Metadata
            try:
                metadata_info = await metadata(
                    clean_filename(title), channel_int, msg_id,
                    override_id=override_id,
                )
            except Exception as e:
                LOGGER.warning(f"[/tara] Metadata istisnası msg {msg_id}: {e}")
                metadata_info = None

            if metadata_info is None:
                state.skip_log.append(
                    f"[META-ÇÖZÜMSÜZ] msg_id={message.id} | başlık={title!r} "
                    f"| boyut={size}"
                )
                s.skipped_meta += 1
                s.processed    += 1
                await _push_progress()
                continue

            # Arşiv bayrağını metadata_info'ya taşı (insert_media bunu _is_archive ile okur)
            if is_archive:
                metadata_info["_is_archive"] = True

            title_clean = remove_urls(title)
            if is_archive:
                # Arşiv dosyasının orijinal uzantısını koru
                if not any(title_clean.lower().endswith(ext) for ext in
                           (".zip", ".7z", ".rar", ".z01")):
                    title_clean += ".zip"
            elif not title_clean.endswith(('.mkv', '.mp4')):
                title_clean += '.mkv'

            # DB'ye ekle
            try:
                updated_id = await db.insert_media(
                    metadata_info,
                    channel=channel_int,
                    msg_id=msg_id,
                    size=size,
                    name=title_clean,
                )
                if updated_id:
                    s.indexed += 1
                    LOGGER.info(f"[/tara] Eklendi msg {msg_id}: {title_clean}")
                else:
                    state.skip_log.append(
                        f"[META-DB-RED] msg_id={msg_id} | başlık={title_clean!r} "
                        f"| boyut={size} | insert_media False/None döndü"
                    )
                    s.skipped_meta += 1
            except Exception as e:
                LOGGER.error(f"[/tara] DB ekleme hatası msg {msg_id}: {e}")
                state.skip_log.append(
                    f"[HATA-DB] msg_id={msg_id} | başlık={title_clean!r} "
                    f"| boyut={size} | hata={e}"
                )
                s.errors += 1

            s.processed += 1
            await _push_progress()

        if batch_had_content:
            empty_streak = 0
        else:
            empty_streak += 1

        current += BATCH_SIZE
        await asyncio.sleep(RATE_LIMIT_DELAY)

    LOGGER.info(
        f"[/tara] Bitti {s.channel_name}: ID {current}'e kadar tarandı, "
        f"{s.total_found} mesaj bulundu, {s.indexed} eklendi"
    )


# ─────────────────────────────────────────────────────────────
# DB temizleme (rescan için)
# ─────────────────────────────────────────────────────────────
async def _purge_all_media() -> int:
    """Tüm storage DB'lerindeki movie ve tv koleksiyonlarını siler."""
    total = 0
    for i in range(1, db.current_db_index + 1):
        db_key = f"storage_{i}"
        storage = db.dbs.get(db_key)
        if storage is None:
            continue
        m = await storage["movie"].delete_many({})
        t = await storage["tv"].delete_many({})
        total += m.deleted_count + t.deleted_count
        LOGGER.info(
            f"[/tara] storage_{i} temizlendi: "
            f"{m.deleted_count} film + {t.deleted_count} dizi"
        )
    return total


# ─────────────────────────────────────────────────────────────
# Ortak tarama akışı
# ─────────────────────────────────────────────────────────────
async def _run_scan(client: Client, message: Message, channels: list[str]):
    state.reset()
    state.running    = True
    state.started_at = time.time()

    state.status_msg = await message.reply_text(
        "📡 <b>Tarama başlıyor…</b>",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        for ch_str in channels:
            if state.cancelled:
                break
            try:
                ch_id = int(ch_str)
            except ValueError:
                LOGGER.warning(f"[/tara] Geçersiz kanal ID: {ch_str}")
                continue
            await _scan_channel(client, ch_id)

        # Son özet
        s      = state
        durum  = "🛑 İptal Edildi" if s.cancelled else "✅ Tamamlandı"
        ozet   = (
            f"<blockquote>📡 <b>Tarama {durum}</b></blockquote>\n\n"
            f"⏱ Süre            : <code>{s.elapsed}</code>\n"
            f"📨 Toplam mesaj    : <code>{s.total_found}</code>\n"
            f"✅ Eklenen         : <code>{s.indexed}</code>\n"
            f"⏭ Atlandı (DB)    : <code>{s.skipped_dup}</code>\n"
            f"⚠️ Atlandı (meta)  : <code>{s.skipped_meta}</code>\n"
            f"📎 Atlandı (tip)   : <code>{s.skipped_nonvid}</code>\n"
            f"❌ Hata            : <code>{s.errors}</code>"
        )
        try:
            await s.status_msg.edit_text(ozet, parse_mode=enums.ParseMode.HTML)
        except Exception:
            await message.reply_text(ozet, parse_mode=enums.ParseMode.HTML)

        if s.processed > 20:
            bildirim = (
                f"{'🛑 Tarama iptal' if s.cancelled else '✅ Tarama bitti'} — "
                f"{s.indexed} eklendi, {s.skipped_dup} atlandı, "
                f"{s.errors} hata ({s.elapsed})"
            )
            await message.reply_text(bildirim)

        # ── Atlanan/hata raporu → .txt olarak gönder ──────────────────────
        if s.skip_log:
            satirlar = [
                "═══════════════════════════════════════════════════════════",
                f"  TARAMA RAPORU — ATLANAN & HATA VERENLERİN AYRINTISI",
                "═══════════════════════════════════════════════════════════",
                f"  Kanal     : {s.channel_name}",
                f"  Süre      : {s.elapsed}",
                f"  Toplam    : {s.total_found}  |  Eklenen: {s.indexed}",
                f"  Atl.(DB)  : {s.skipped_dup}  |  Atl.(meta): {s.skipped_meta}"
                f"  |  Atl.(tip): {s.skipped_nonvid}  |  Hata: {s.errors}",
                "───────────────────────────────────────────────────────────",
                "",
                "ETIKET AÇIKLAMALARI:",
                "  [TİP-DIŞI]    → Video veya arşiv olmayan mesaj (foto/ses/pdf vb.)",
                "  [META-BAŞLIK] → Caption ve dosya adı yok; indekslenemez",
                "  [META-ÇÖZÜMSÜZ]→ metadata() fonksiyonu None döndürdü",
                "  [META-DB-RED] → insert_media() False/None döndürdü",
                "  [HATA-DB]     → MongoDB yazma istisnası",
                "  [HATA-BATCH]  → get_messages() başarısız (ağ/flood hatası)",
                "",
                "───────────────────────────────────────────────────────────",
                f"  Toplam kayıt : {len(s.skip_log)}",
                "───────────────────────────────────────────────────────────",
                "",
            ]
            for i, satir in enumerate(s.skip_log, 1):
                satirlar.append(f"{i:>4}. {satir}")
            satirlar += ["", "═══════════════════════════════════════════════════════════"]

            icerik   = "\n".join(satirlar)
            buf      = io.BytesIO(icerik.encode("utf-8"))
            buf.name = "tarama_raporu.txt"
            try:
                await message.reply_document(
                    document=buf,
                    caption=(
                        f"📋 <b>Tarama Raporu</b>\n"
                        f"Atlanan: <b>{s.skipped_nonvid + s.skipped_meta + s.skipped_dup}</b> "
                        f"| Hata: <b>{s.errors}</b> | Toplam kayıt: <b>{len(s.skip_log)}</b>"
                    ),
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception as exc:
                LOGGER.error(f"[/tara] Rapor gönderilemedi: {exc}")

    except (ChannelPrivate, ChatAdminRequired) as e:
        await message.reply_text(
            f"❌ <b>Kanala erişim reddedildi.</b>\n\n"
            f"Botun kanalda yönetici olduğundan emin olun.\n"
            f"<code>{e}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        LOGGER.error(f"[/tara] Beklenmeyen hata: {e}")
        await message.reply_text(
            f"❌ Tarama başarısız: <code>{e}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    finally:
        state.running = False


# ─────────────────────────────────────────────────────────────
# Komut handler'ları
# ─────────────────────────────────────────────────────────────
@Client.on_message(
    filters.command("tara") & filters.private & CustomFilters.owner,
    group=10,
)
async def cmd_tara(client: Client, message: Message):
    """
    /tara     → DB'yi tamamen sil, baştan tara
    /tara db  → Mevcut kayıtları koru, sadece yenileri ekle
    """
    if state.running:
        await message.reply_text(
            "⚠️ Zaten bir tarama çalışıyor.\n"
            "Durdurmak için /tara_iptal gönderin.",
            quote=True,
        )
        return

    args     = message.text.split()
    channels = list(Telegram.AUTH_CHANNEL)

    if not channels:
        await message.reply_text("❌ AUTH_CHANNEL yapılandırılmamış.", quote=True)
        return

    # /tara db → mevcut kayıtları koru, sadece yenileri ekle
    if len(args) > 1 and args[1].lower() == "db":
        asyncio.create_task(_run_scan(client, message, channels))
        return

    # /tara (varsayılan) → DB'yi sil, baştan tara
    purge_msg = await message.reply_text(
        "🗑 <b>Veritabanı temizleniyor…</b>",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        silinen = await _purge_all_media()
    except Exception as e:
        await purge_msg.edit_text(
            f"❌ Temizleme hatası:\n<code>{e}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    await purge_msg.edit_text(
        f"🗑 <b>{silinen}</b> kayıt silindi. Tarama başlatılıyor…",
        parse_mode=enums.ParseMode.HTML,
    )
    await asyncio.sleep(1)

    asyncio.create_task(_run_scan(client, message, channels))


@Client.on_message(
    filters.command("tara_durum") & filters.private & CustomFilters.owner,
    group=10,
)
async def cmd_tara_durum(client: Client, message: Message):
    """Devam eden taramanın anlık durumunu göster."""
    s = state
    if not s.running:
        await message.reply_text("ℹ️ Şu an çalışan bir tarama yok.", quote=True)
        return

    text = (
        f"<blockquote>📡 <b>Tarama devam ediyor:</b> {s.channel_name}</blockquote>\n\n"
        f"⏱ Geçen süre    : <code>{s.elapsed}</code>\n"
        f"📨 İşlenen       : <code>{s.processed}</code>\n"
        f"✅ Eklenen       : <code>{s.indexed}</code>\n"
        f"⏭ Atlandı (DB)  : <code>{s.skipped_dup}</code>\n"
        f"⚠️ Atlandı (meta): <code>{s.skipped_meta}</code>\n"
        f"📎 Atlandı (tip) : <code>{s.skipped_nonvid}</code>\n"
        f"❌ Hata          : <code>{s.errors}</code>"
    )
    await message.reply_text(text, quote=True, parse_mode=enums.ParseMode.HTML)


@Client.on_message(
    filters.command("tara_iptal") & filters.private & CustomFilters.owner,
    group=10,
)
async def cmd_tara_iptal(client: Client, message: Message):
    """Devam eden taramayı durdur."""
    if not state.running:
        await message.reply_text("ℹ️ Şu an çalışan bir tarama yok.", quote=True)
        return

    state.cancelled = True
    await message.reply_text(
        "🛑 <b>İptal isteği alındı.</b>\n"
        "Mevcut batch tamamlandıktan sonra durulacak…",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )
