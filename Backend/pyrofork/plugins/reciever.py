from asyncio import create_task, sleep as asleep, Queue, Lock
import Backend
from Backend.helper.task_manager import edit_message
from Backend.logger import LOGGER
from Backend import db
from Backend.config import Telegram
from Backend.helper.pyro import clean_filename, get_readable_file_size, remove_urls
from Backend.helper.metadata import metadata
from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from pyrogram.enums.parse_mode import ParseMode


file_queue = Queue()
db_lock = Lock()

async def process_file():
    while True:
        metadata_info, channel, msg_id, size, title = await file_queue.get()
        async with db_lock:
            updated_id = await db.insert_media(metadata_info, channel=channel, msg_id=msg_id, size=size, name=title)
            if updated_id:
                LOGGER.info(f"{metadata_info['media_type']} updated with ID: {updated_id}")
                # ── TV dizisi ise hatırlatma bildirimlerini tetikle ──────────
                if metadata_info.get("media_type") == "tv":
                    try:
                        from Backend.fastapi.routes.notification_routes import (
                            send_tv_reminder_notifications,
                        )
                        tmdb_id  = metadata_info.get("tmdb_id")
                        db_index = db.current_db_index
                        notif_title = (
                            metadata_info.get("title_tr")
                            or metadata_info.get("title")
                            or title
                        )
                        poster  = metadata_info.get("poster", "")
                        season  = metadata_info.get("season_number")
                        episode = metadata_info.get("episode_number")

                        if tmdb_id is not None:
                            LOGGER.info(
                                f"TV hatirlatma tampona aliniyor: tmdb_id={tmdb_id} "
                                f"s={season} e={episode}"
                            )
                            create_task(
                                send_tv_reminder_notifications(
                                    tmdb_id=int(tmdb_id),
                                    db_index=int(db_index),
                                    title=notif_title,
                                    poster=poster,
                                    new_season=season,
                                    new_episode=episode,
                                )
                            )
                        else:
                            LOGGER.warning(
                                f"Hatirlatma atlandi: tmdb_id={tmdb_id} eksik"
                            )
                    except Exception as _notif_err:
                        LOGGER.warning(f"Hatirlatma bildirimi baslatılamadi: {_notif_err}")
                # ── Film ise hatırlatma bildirimlerini tetikle ───────────────
                elif metadata_info.get("media_type") == "movie":
                    try:
                        from Backend.fastapi.routes.notification_routes import (
                            send_movie_reminder_notifications,
                        )
                        tmdb_id  = metadata_info.get("tmdb_id")
                        db_index = db.current_db_index
                        notif_title = (
                            metadata_info.get("title_tr")
                            or metadata_info.get("title")
                            or title
                        )
                        poster        = metadata_info.get("poster", "")
                        quality_label = metadata_info.get("quality", "")

                        # Dosya adında "german" geçiyorsa kalite etiketine ekle
                        _raw_title = (title or "").lower()
                        _has_german = bool(__import__("re").search(r'\bgerman\b', _raw_title))
                        _has_camrip = bool(__import__("re").search(r'\bcam[-_]?rip\b|\bcamrip\b|\bcam\b', _raw_title))
                        if _has_german and _has_camrip:
                            quality_label = "GermanCamRip"
                        elif _has_german:
                            quality_label = f"German:{quality_label}" if quality_label else "German"

                        if tmdb_id is not None:
                            LOGGER.info(
                                f"Film hatirlatma tampona aliniyor: tmdb_id={tmdb_id} "
                                f"kalite={quality_label!r}"
                            )
                            create_task(
                                send_movie_reminder_notifications(
                                    tmdb_id=int(tmdb_id),
                                    db_index=int(db_index),
                                    title=notif_title,
                                    poster=poster,
                                    quality_label=quality_label,
                                )
                            )
                        else:
                            LOGGER.warning(
                                f"Film hatirlatma atlandi: tmdb_id={tmdb_id} eksik"
                            )
                    except Exception as _notif_err:
                        LOGGER.warning(f"Film hatirlatma bildirimi baslatılamadi: {_notif_err}")
                # ── Katalog yenilemesini debounce ile tetikle ────────────────
                try:
                    from Backend.helper.platform_catalog import platform_catalog
                    platform_catalog.schedule_refresh()
                except Exception as _cat_err:
                    LOGGER.warning(f"Katalog yenileme planlanamadı: {_cat_err}")
                # ────────────────────────────────────────────────────────────
            else:
                LOGGER.info("Update failed due to validation errors.")
        file_queue.task_done()

for _ in range(1):
    create_task(process_file())


# Desteklenen arşiv uzantıları (zip/7z/rar ve multipart varyantları)
ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar")
import re as _re_archive

def _is_archive_file(doc) -> bool:
    """Dosyanın arşiv olup olmadığını kontrol eder (multipart .001, .z01 dahil)."""
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    # Doğrudan uzantı eşleşmesi
    if name.endswith(ARCHIVE_EXTENSIONS):
        return True
    # Multipart arşiv: .zip.001, .7z.001, .z01, .z02, .part1.rar vb.
    if _re_archive.search(r'\.(zip|7z|rar|z)\.(\d+)$', name):
        return True
    if _re_archive.search(r'\.part\d+\.rar$', name):
        return True
    # MIME tipi kontrolü
    if mime in ("application/zip", "application/x-7z-compressed",
                "application/x-zip-compressed", "application/x-rar-compressed",
                "application/vnd.rar"):
        return True
    return False

def _archive_to_video_name(title: str) -> str:
    """Arşiv dosya adını metadata araması için .mkv uzantılı isime çevirir.
    Örnek: Film.mkv.zip.001  -> Film.mkv
    Örnek: Film.1080p.zip    -> Film.1080p.mkv
    Örnek: Film_B_zip.001    -> Film_B.mkv   (Telegram 64-kar kesmesi)
    """
    import os
    name = title
    # Tüm arşiv/multipart uzantılarını soy
    while True:
        base, ext = os.path.splitext(name)
        ext_lower = ext.lower()
        # Sayısal bölüm uzantısı (.001, .002...)
        if _re_archive.match(r'^\.\d+$', ext_lower):
            name = base
            continue
        # Arşiv uzantısı
        if ext_lower in (".zip", ".7z", ".rar", ".z"):
            name = base
            continue
        break
    # Telegram 64 karakter kesmesi: "..._zip" veya "..._7z" şeklinde biten adları temizle
    for trunc_suffix in ("_zip", ".zip", "_7z", ".7z"):
        if name.lower().endswith(trunc_suffix):
            name = name[: -len(trunc_suffix)]
            break
    # Zaten .mkv veya video uzantısı ile bitiyorsa olduğu gibi döndür
    _video_exts = (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts")
    if name.lower().endswith(_video_exts):
        return name
    return name + ".mkv"

@Client.on_message(filters.channel & (filters.document | filters.video))
async def file_receive_handler(client: Client, message: Message):
    if str(message.chat.id) in Telegram.AUTH_CHANNEL:
        try:
            doc = message.document
            is_archive = _is_archive_file(doc)

            if message.video or (doc and doc.mime_type.startswith("video/")):
                # Normal video dosyası
                file = message.video or doc
                title = message.caption or file.file_name
                msg_id = message.id
                size = get_readable_file_size(file.file_size)
                channel = str(message.chat.id).replace("-100", "")

                from Backend.helper.metadata import extract_default_id
                override_id, _id_media_type = extract_default_id(title) if title else (None, None)

                metadata_info = await metadata(clean_filename(title), int(channel), msg_id, override_id=override_id)
                if metadata_info is None:
                    LOGGER.warning(f"Metadata failed for file: {title} (ID: {msg_id})")
                    return

                title = remove_urls(title)
                if not title.endswith(('.mkv', '.mp4')):
                    title += '.mkv'

                if Backend.USE_DEFAULT_ID:
                    new_caption = (message.caption + "\n\n" + Backend.USE_DEFAULT_ID) if message.caption else Backend.USE_DEFAULT_ID
                    create_task(edit_message(
                        chat_id=message.chat.id,
                        msg_id=message.id,
                        new_caption=new_caption
                    ))

                await file_queue.put((metadata_info, int(channel), msg_id, size, title))

            elif is_archive:
                # ZIP / 7Z arşiv dosyası: caption'dan metadata al, .mkv gibi işle
                file = doc
                # Caption varsa kullan (TMDb/IMDb URL veya dosya adı içerebilir), yoksa dosya adını kullan
                raw_title = message.caption or file.file_name or ""
                msg_id = message.id
                size = get_readable_file_size(file.file_size)
                channel = str(message.chat.id).replace("-100", "")

                from Backend.helper.metadata import extract_default_id
                override_id, _id_media_type = extract_default_id(raw_title) if raw_title else (None, None)

                # Metadata için dosya adını .mkv'ye çevirerek gönder
                video_name = _archive_to_video_name(file.file_name or raw_title)
                clean_name = clean_filename(video_name)

                metadata_info = await metadata(clean_name, int(channel), msg_id, override_id=override_id)
                if metadata_info is None:
                    LOGGER.warning(f"Metadata failed for archive: {raw_title} (ID: {msg_id})")
                    # reply_text kanal mesajlarında crash verebilir; sadece log'la
                    return

                # DB'ye gerçek arşiv dosya adıyla kaydet (is_archive=True ile işaretle)
                archive_display_name = remove_urls(file.file_name or raw_title)
                # metadata_info'ya arşiv bayrağını ekle (insert_media bunu QualityDetail'e geçirir)
                metadata_info["_is_archive"] = True

                LOGGER.info(f"Archive file processed as media: {archive_display_name} -> {metadata_info.get('title')}")
                await file_queue.put((metadata_info, int(channel), msg_id, size, archive_display_name))

            else:
                try:
                    await message.reply_text("> Not supported")
                except Exception:
                    LOGGER.warning(f"Could not reply to unsupported file message {message.id}")
        except FloodWait as e:
            LOGGER.info(f"Sleeping for {str(e.value)}s")
            await asleep(e.value)
            await message.reply_text(
                text=f"Got Floodwait of {str(e.value)}s",
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN
            )
    else:
        try:
            await message.reply_text("> Channel is not in AUTH_CHANNEL")
        except Exception:
            pass
        

@Client.on_edited_message(filters.channel & (filters.document | filters.video))
async def file_edited_handler(client: Client, message: Message):
    if str(message.chat.id) in Telegram.AUTH_CHANNEL:
        try:
            _doc = message.document
            if message.video or (_doc and _doc.mime_type.startswith("video/")) or _is_archive_file(_doc):
                file = message.video or _doc
                title = message.caption or file.file_name
                msg_id = message.id
                size = get_readable_file_size(file.file_size)
                channel = str(message.chat.id).replace("-100", "")

                from Backend.helper.metadata import extract_default_id
                override_id, _id_media_type = extract_default_id(message.caption) if message.caption else (None, None)

                # Only proceed if we found a valid manual overide ID in the caption update
                if override_id:
                    LOGGER.info(f"Detected override ID '{override_id}' in edited message {msg_id}")
                    
                    from Backend.helper.encrypt import encode_string
                    stream_id_hash = await encode_string({"chat_id": int(channel), "msg_id": msg_id})
                    
                    # Wipe the old streaming quality reference from the old associated media
                    await db.delete_media_by_stream_id(stream_id_hash)

                    # Reprocess metadata completely
                    metadata_info = await metadata(clean_filename(title), int(channel), msg_id, override_id=override_id)
                    if metadata_info is None:
                        LOGGER.warning(f"Metadata failed for edited file: {title} (ID: {msg_id})")
                        return

                    title = remove_urls(title)
                    if not title.endswith(('.mkv', '.mp4')):
                        title += '.mkv'

                    # Add the new quality to the correct DB movie/show
                    await file_queue.put((metadata_info, int(channel), msg_id, size, title))
            else:
                pass # ignore edits on other types
        except Exception as e:
            LOGGER.error(f"Error handling edited generic file {message.id}: {e}")

@Client.on_deleted_messages(filters.channel)
async def file_deleted_handler(client: Client, messages: list[Message]):
    # pyrogram provides a list of deleted messages.
    try:
        from Backend.helper.encrypt import encode_string
        
        for message in messages:
            if message.chat and str(message.chat.id) in Telegram.AUTH_CHANNEL:
                channel = str(message.chat.id).replace("-100", "")
                msg_id = message.id
                
                try:
                    stream_id_hash = await encode_string({"chat_id": int(channel), "msg_id": msg_id})
                    deleted = await db.delete_media_by_stream_id(stream_id_hash)
                    
                    if deleted:
                        LOGGER.info(f"Automatically purged deleted message {msg_id} from database.")
                except Exception as ex:
                    LOGGER.error(f"Failed to scrub deleted message {msg_id}: {ex}")
                    
    except Exception as e:
        LOGGER.error(f"Error handling deleted messages: {e}")
