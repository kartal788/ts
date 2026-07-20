from asyncio import create_task, sleep as asleep, Queue, Lock, Semaphore
import Backend
from Backend.helper.task_manager import edit_message
from Backend.logger import LOGGER
from Backend import db
from Backend.config import Telegram
from Backend.helper.pyro import clean_filename, get_readable_file_size, remove_urls
from Backend.helper.metadata import metadata
from Backend.helper.split_files import parse_split_info, strip_part_suffix
from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from pyrogram.enums.parse_mode import ParseMode


file_queue = Queue()
db_lock = Lock()

# ─── Eş zamanlı metadata() çağrısı sınırı ────────────────────────────────────
# 100 video aynı anda geldiğinde tüm isteklerin aynı anda API'ye çarpmasını
# önler. En fazla 5 eş zamanlı metadata araması yapılır; geri kalanlar sıraya girer.
METADATA_SEMAPHORE = Semaphore(5)

async def process_file():
    while True:
        metadata_info, channel, msg_id, size, title, size_bytes = await file_queue.get()
        try:
            async with db_lock:
                updated_id = await db.insert_media(metadata_info, channel=channel, msg_id=msg_id, size=size, name=title, size_bytes=size_bytes)
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
                    # ── Yeni içerik duyurusu (Telegram kanalı / konusu) ──────────
                    try:
                        from Backend.helper.content_announcer import announce_new_content
                        _announce_info = dict(metadata_info)
                        _announce_info["source_filename"] = title
                        announce_new_content(_announce_info)
                    except Exception as _announce_err:
                        LOGGER.warning(f"Duyuru tetiklenemedi: {_announce_err}")
                else:
                    LOGGER.info("Update failed due to validation errors.")
        except Exception as e:
            LOGGER.error(f"process_file: DB işlemi sırasında beklenmeyen hata (msg_id={msg_id}): {e}")
        finally:
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
    if name.endswith(ARCHIVE_EXTENSIONS):
        return True
    if _re_archive.search(r'\.(zip|7z|rar|z)\.(\d+)$', name):
        return True
    # .mkv.001, .mp4.002 gibi video split dosyaları
    if _re_archive.search(r'\.(mkv|mp4|avi|mov|ts)\.(\d+)$', name):
        return True
    if _re_archive.search(r'\.part\d+\.rar$', name):
        return True
    if mime in ("application/zip", "application/x-7z-compressed",
                "application/x-zip-compressed", "application/x-rar-compressed",
                "application/vnd.rar"):
        return True
    return False

def _archive_to_video_name(title: str) -> str:
    """Arşiv dosya adını metadata araması için .mkv uzantılı isime çevirir."""
    import os
    name = title
    while True:
        base, ext = os.path.splitext(name)
        ext_lower = ext.lower()
        if _re_archive.match(r'^\.\d+$', ext_lower):
            name = base
            continue
        if ext_lower in (".zip", ".7z", ".rar", ".z"):
            name = base
            continue
        break
    for trunc_suffix in ("_zip", ".zip", "_7z", ".7z"):
        if name.lower().endswith(trunc_suffix):
            name = name[: -len(trunc_suffix)]
            break
    _video_exts = (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts")
    if name.lower().endswith(_video_exts):
        return name
    return name + ".mkv"


async def _consume_manual_season_episode(mode_snapshot: dict) -> tuple:
    """Panel/komut üzerinden açılan 'tv' modundaki bir sonraki sezon/bölüm
    numarasını döndürür ve global sayacı bir sonraki dosya için artırır.
    Aynı anda birden çok dosya işlenirken sayaç yarışına (race condition)
    girmemek için Backend.MANUAL_MODE_LOCK kullanılır.

    Not: mode_snapshot çağrı anında yakalanan referanstır; kilit altında
    Backend.MANUAL_MODE hâlâ aynı obje mi (mod bu sırada kapatılmadı mı)
    kontrol edilip güvenle güncellenir.
    """
    async with Backend.MANUAL_MODE_LOCK:
        current_mode = Backend.MANUAL_MODE
        if current_mode is not mode_snapshot or current_mode is None:
            # Mod bu esnada kapatıldı/değiştirildi — panelden ayarlanan son
            # değerleri kullan (sayaç ilerletmeden).
            season = (mode_snapshot or {}).get("season")
            episode = (mode_snapshot or {}).get("next_episode")
            return season, episode
        season = current_mode.get("season")
        episode = current_mode.get("next_episode")
        if episode is not None:
            current_mode["next_episode"] = episode + 1
        return season, episode


async def _consume_attach_season_episode(mode_snapshot: dict) -> tuple:
    """/media/edit sayfasındaki 'İçerik Ekle' modunda bir sonraki sezon/bölüm
    numarasını döndürür ve global sayacı bir sonraki dosya için artırır.
    _consume_manual_season_episode ile aynı mantık; Backend.ATTACH_MODE_LOCK
    kullanır (Backend.MANUAL_MODE_LOCK ile karışmaması için ayrı kilit)."""
    async with Backend.ATTACH_MODE_LOCK:
        current_mode = Backend.ATTACH_MODE
        if current_mode is not mode_snapshot or current_mode is None:
            season = (mode_snapshot or {}).get("season")
            episode = (mode_snapshot or {}).get("next_episode")
            return season, episode
        season = current_mode.get("season")
        episode = current_mode.get("next_episode")
        if episode is not None:
            current_mode["next_episode"] = episode + 1
        return season, episode


async def _handle_video_message(client: Client, message: Message):
    """
    Tek bir video/arşiv mesajını işler.
    METADATA_SEMAPHORE ile eş zamanlı istek sayısı sınırlandırılır;
    bu sayede 100 video aynı anda gelse bile bot çökmez.
    """
    doc = message.document
    is_archive = _is_archive_file(doc)

    try:
        if message.video or (doc and doc.mime_type and doc.mime_type.startswith("video/")):
            file = message.video or doc
            title = message.caption or file.file_name
            msg_id = message.id
            size = get_readable_file_size(file.file_size)
            channel = str(message.chat.id).replace("-100", "")

            if Backend.ATTACH_MODE:
                from Backend.helper.metadata import build_manual_metadata
                _attach_snapshot = Backend.ATTACH_MODE
                _attach_media_type = _attach_snapshot.get("media_type", "movie")
                _a_season, _a_episode = (None, None)
                if _attach_media_type == "tv":
                    _a_season, _a_episode = await _consume_attach_season_episode(_attach_snapshot)
                async with METADATA_SEMAPHORE:
                    metadata_info = await build_manual_metadata(
                        clean_filename(title), int(channel), msg_id,
                        title=_attach_snapshot.get("title"),
                        poster=_attach_snapshot.get("poster"),
                        media_type=_attach_media_type,
                        season_number=_a_season,
                        episode_number=_a_episode,
                        tmdb_id=_attach_snapshot.get("tmdb_id"),
                        imdb_id=_attach_snapshot.get("imdb_id"),
                    )
            elif Backend.MANUAL_MODE:
                from Backend.helper.metadata import build_manual_metadata
                _mode_snapshot = Backend.MANUAL_MODE
                _mode_media_type = _mode_snapshot.get("media_type", "movie")
                _season, _episode = (None, None)
                if _mode_media_type == "tv":
                    _season, _episode = await _consume_manual_season_episode(_mode_snapshot)
                async with METADATA_SEMAPHORE:
                    metadata_info = await build_manual_metadata(
                        clean_filename(title), int(channel), msg_id,
                        title=_mode_snapshot.get("title"),
                        poster=_mode_snapshot.get("poster"),
                        description=_mode_snapshot.get("description"),
                        media_type=_mode_media_type,
                        season_number=_season,
                        episode_number=_episode,
                        year=_mode_snapshot.get("year"),
                        rating=_mode_snapshot.get("rating"),
                        genres=_mode_snapshot.get("genres"),
                    )
            else:
                from Backend.helper.metadata import extract_default_id
                override_id, _id_media_type = extract_default_id(title) if title else (None, None)

                async with METADATA_SEMAPHORE:
                    metadata_info = await metadata(clean_filename(title), int(channel), msg_id, override_id=override_id)

            if metadata_info is None:
                LOGGER.warning(f"Metadata failed for file: {title} (ID: {msg_id})")
                return

            title = remove_urls(title)
            # Split dosya ise görünen adı temizle (.mkv.001 → .mkv)
            if metadata_info.get("group_key"):
                title = strip_part_suffix(title)
            if not title.endswith(('.mkv', '.mp4')):
                title += '.mkv'

            if Backend.USE_DEFAULT_ID:
                new_caption = (message.caption + "\n\n" + Backend.USE_DEFAULT_ID) if message.caption else Backend.USE_DEFAULT_ID
                create_task(edit_message(
                    chat_id=message.chat.id,
                    msg_id=message.id,
                    new_caption=new_caption
                ))

            await file_queue.put((metadata_info, int(channel), msg_id, size, title, file.file_size))

        elif is_archive:
            file = doc
            raw_title = message.caption or file.file_name or ""
            msg_id = message.id
            size = get_readable_file_size(file.file_size)
            channel = str(message.chat.id).replace("-100", "")

            from Backend.helper.metadata import extract_default_id
            override_id, _id_media_type = extract_default_id(raw_title) if raw_title else (None, None)
            _manual_mode_snapshot = Backend.MANUAL_MODE

            # .mkv.001 gibi video split dosyalarında group_key/part_number ve
            # görünen ad CAPTION'dan çıkarılmalı (varsa) — leech botları dosyayı
            # kısaltılmış adla yükleyip orijinal/uzun adı caption'a yazabiliyor.
            # file.file_name'i raw_title'a göre önceliklendirmek bu durumda
            # kısaltılmış adın veritabanına yazılmasına yol açıyordu.
            split_source = raw_title or file.file_name
            split_info_raw = parse_split_info(split_source)

            video_name = _archive_to_video_name(split_source)
            clean_name = clean_filename(video_name)

            _attach_mode_snapshot = Backend.ATTACH_MODE

            if _attach_mode_snapshot:
                from Backend.helper.metadata import build_manual_metadata
                _attach_media_type = _attach_mode_snapshot.get("media_type", "movie")
                _a_season, _a_episode = (None, None)
                if _attach_media_type == "tv":
                    _a_season, _a_episode = await _consume_attach_season_episode(_attach_mode_snapshot)
                async with METADATA_SEMAPHORE:
                    metadata_info = await build_manual_metadata(
                        clean_name, int(channel), msg_id,
                        title=_attach_mode_snapshot.get("title"),
                        poster=_attach_mode_snapshot.get("poster"),
                        media_type=_attach_media_type,
                        season_number=_a_season,
                        episode_number=_a_episode,
                        tmdb_id=_attach_mode_snapshot.get("tmdb_id"),
                        imdb_id=_attach_mode_snapshot.get("imdb_id"),
                    )
            elif _manual_mode_snapshot:
                from Backend.helper.metadata import build_manual_metadata
                _mode_media_type = _manual_mode_snapshot.get("media_type", "movie")
                _season, _episode = (None, None)
                if _mode_media_type == "tv":
                    _season, _episode = await _consume_manual_season_episode(_manual_mode_snapshot)
                async with METADATA_SEMAPHORE:
                    metadata_info = await build_manual_metadata(
                        clean_name, int(channel), msg_id,
                        title=_manual_mode_snapshot.get("title"),
                        poster=_manual_mode_snapshot.get("poster"),
                        description=_manual_mode_snapshot.get("description"),
                        media_type=_mode_media_type,
                        season_number=_season,
                        episode_number=_episode,
                        year=_manual_mode_snapshot.get("year"),
                        rating=_manual_mode_snapshot.get("rating"),
                        genres=_manual_mode_snapshot.get("genres"),
                    )
            else:
                async with METADATA_SEMAPHORE:
                    metadata_info = await metadata(clean_name, int(channel), msg_id, override_id=override_id)

            if metadata_info is None:
                LOGGER.warning(f"Metadata failed for archive: {raw_title} (ID: {msg_id})")
                return

            # split dosya ise group_key ve part_number'ı metadata'ya enjekte et
            if split_info_raw:
                quality = metadata_info.get("quality", "")
                metadata_info["group_key"] = f"{channel}:{quality}:{split_info_raw[0]}"
                metadata_info["part_number"] = split_info_raw[1]
                LOGGER.info(f"Split dosya tespit edildi: part={split_info_raw[1]} group_key={metadata_info['group_key']}")

            # Görünen ad da aynı önceliği izlemeli: caption (uzun/orijinal ad)
            # varsa o kullanılır, yoksa Telegram'daki (kısaltılmış olabilen)
            # dosya adına düşülür.
            archive_display_name = remove_urls(split_source)
            # .mkv.001 gibi split video dosyaları arşiv DEĞİL — sadece gerçek arşivler (zip/7z/rar) işaretlenir
            if not split_info_raw:
                metadata_info["_is_archive"] = True

            LOGGER.info(f"Archive file processed as media: {archive_display_name} -> {metadata_info.get('title')}")
            await file_queue.put((metadata_info, int(channel), msg_id, size, archive_display_name, file.file_size))

        else:
            try:
                await message.reply_text("> Not supported")
            except Exception:
                LOGGER.warning(f"Could not reply to unsupported file message {message.id}")

    except FloodWait as e:
        # Kanal mesajlarında reply_text çağrısı crash'e yol açıyordu — kaldırıldı.
        # Bekleme sonrası mesajı yeniden kuyruğa al.
        LOGGER.warning(f"FloodWait {e.value}s for msg {message.id}, bekleniyor ve yeniden deneniyor…")
        await asleep(e.value)
        create_task(_handle_video_message(client, message))
    except Exception as e:
        LOGGER.error(f"_handle_video_message: msg_id={message.id} işlenirken hata: {e}")


@Client.on_message(filters.channel & (filters.document | filters.video))
async def file_receive_handler(client: Client, message: Message):
    if str(message.chat.id) in Telegram.AUTH_CHANNEL:
        # Her mesaj için ayrı task — handler anında döner, bot bloke olmaz.
        # Eş zamanlılık METADATA_SEMAPHORE ile kontrol edilir.
        create_task(_handle_video_message(client, message))
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
            if message.video or (_doc and _doc.mime_type and _doc.mime_type.startswith("video/")) or _is_archive_file(_doc):
                file = message.video or _doc
                if file is None:
                    LOGGER.warning(f"file_edited_handler: file is None for msg {message.id}, skipping")
                    return
                title = message.caption or file.file_name
                msg_id = message.id
                size = get_readable_file_size(file.file_size)
                channel = str(message.chat.id).replace("-100", "")

                from Backend.helper.metadata import extract_default_id
                override_id, _id_media_type = extract_default_id(message.caption) if message.caption else (None, None)

                if override_id:
                    LOGGER.info(f"Detected override ID '{override_id}' in edited message {msg_id}")
                    
                    from Backend.helper.encrypt import encode_string
                    stream_id_hash = await encode_string({"chat_id": int(channel), "msg_id": msg_id})
                    
                    await db.delete_media_by_stream_id(stream_id_hash)

                    async with METADATA_SEMAPHORE:
                        metadata_info = await metadata(clean_filename(title), int(channel), msg_id, override_id=override_id)

                    if metadata_info is None:
                        LOGGER.warning(f"Metadata failed for edited file: {title} (ID: {msg_id})")
                        return

                    title = remove_urls(title)
                    if not title.endswith(('.mkv', '.mp4')):
                        title += '.mkv'

                    await file_queue.put((metadata_info, int(channel), msg_id, size, title, file.file_size))
            else:
                pass
        except FloodWait as e:
            LOGGER.warning(f"FloodWait {e.value}s during edited message {message.id}, bekleniyor…")
            await asleep(e.value)
        except Exception as e:
            LOGGER.error(f"Error handling edited generic file {message.id}: {e}")

@Client.on_deleted_messages(filters.channel)
async def file_deleted_handler(client: Client, messages: list[Message]):
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
