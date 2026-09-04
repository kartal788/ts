"""
ekle.py — Google Drive Tarayıcı & Onay Sistemi
"""

import asyncio
import time
import traceback
from pathlib import Path

from pyrogram import filters, Client
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
from Backend.helper.pyro import clean_filename, get_readable_file_size, remove_urls
from Backend.logger import LOGGER

GDRIVE_TOKEN_PATH = Path(__file__).parent.parent.parent.parent / "gdrive_token.pickle"
RCLONE_CONF_PATH  = Path(__file__).parent.parent.parent.parent / "rclone.conf"
APPROVED_COLLECTION = "ekle_approved"
PAGE_SIZE = 8

VIDEO_MIMES = {
    "video/mp4", "video/x-matroska", "video/x-msvideo",
    "video/quicktime", "video/x-ms-wmv", "video/mpeg",
    "video/x-flv", "video/webm", "video/3gpp",
    "application/octet-stream",
}
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m4v", ".webm", ".flv", ".mpg", ".mpeg"}


# ─── Başlangıç: orphan temizleme ──────────────────────────────────────────────

async def cleanup_gdrive_orphans():
    """
    Bot başlarken ekle_approved koleksiyonundaki her kaydın
    Stremio DB'de (telegram.id) hâlâ mevcut olup olmadığını kontrol eder.
    Stremio'da artık yoksa ekle_approved kaydını da siler.
    """
    await asyncio.sleep(5)
    try:
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is None:
            LOGGER.warning("[ekle/cleanup] DB storage bulunamadı, orphan temizleme atlandı.")
            return

        col = storage[APPROVED_COLLECTION]
        total = await col.count_documents({})
        if total == 0:
            return

        LOGGER.info(f"[ekle/cleanup] {total} kayıt kontrol ediliyor...")
        removed = 0
        cursor = col.find({})
        async for doc in cursor:
            # Rclone kayıtlarını atla — encoded_string telegram.id ile eşleşmez,
            # yanlışlıkla orphan sayılıp silinmesin.
            if doc.get("source") == "rclone" or doc.get("rclone_path"):
                continue

            stream_id = doc.get("db_id", "")
            if not stream_id:
                await col.delete_one({"_id": doc["_id"]})
                removed += 1
                continue

            found = False
            try:
                for i in range(1, db.current_db_index + 1):
                    sdb = db.dbs.get(f"storage_{i}")
                    if sdb is None:
                        continue
                    if await sdb["movie"].find_one({"telegram.id": stream_id}):
                        found = True
                        break
                    if await sdb["tv"].find_one({"seasons.episodes.telegram.id": stream_id}):
                        found = True
                        break
            except Exception as e:
                LOGGER.warning(f"[ekle/cleanup] Kontrol hatası (doc={doc['_id']}): {e}")
                continue

            if not found:
                await col.delete_one({"_id": doc["_id"]})
                removed += 1
                LOGGER.info(f"[ekle/cleanup] Orphan silindi: {doc.get('file_name', '?')}")

        LOGGER.info(f"[ekle/cleanup] Tamamlandı. {removed}/{total} orphan kayıt temizlendi.")
    except Exception as e:
        LOGGER.error(f"[ekle/cleanup] Hata: {e}\n{traceback.format_exc()}")


# ─── Google Drive yardımcıları ────────────────────────────────────────────────

def _ensure_gdrive_packages():
    import importlib, subprocess, sys, shutil as _shutil
    pkgs = {
        "googleapiclient": "google-api-python-client",
        "google.auth":     "google-auth",
    }
    for module, pip_name in pkgs.items():
        try:
            importlib.import_module(module)
        except ImportError:
            LOGGER.info(f"[ekle/gdrive] {pip_name} yükleniyor...")
            uv_bin = _shutil.which("uv") or "/app/.venv/bin/uv"
            import os as _os
            if _shutil.which("uv") or _os.path.exists(uv_bin):
                cmd = [uv_bin, "pip", "install", pip_name]
            else:
                cmd = [sys.executable, "-m", "pip", "install",
                       "--break-system-packages", "--quiet", pip_name]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"{pip_name} yüklenemedi: {result.stderr[:200]}")
            LOGGER.info(f"[ekle/gdrive] {pip_name} yüklendi.")
            if module in sys.modules:
                del sys.modules[module]


def _get_gdrive_service():
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
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GDRIVE_TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _list_drive_items(folder_id: str = "root", page_token=None):
    svc = _get_gdrive_service()
    query = (
        f"'{folder_id}' in parents and trashed = false and ("
        "mimeType = 'application/vnd.google-apps.folder' or "
        + " or ".join(f"mimeType = '{m}'" for m in VIDEO_MIMES) +
        ")"
    )
    params = dict(
        q=query,
        fields="nextPageToken, files(id, name, mimeType, size)",
        orderBy="folder,name",
        pageSize=PAGE_SIZE,
    )
    if page_token:
        params["pageToken"] = page_token

    resp = svc.files().list(**params).execute()
    items = resp.get("files", [])
    next_tok = resp.get("nextPageToken")

    filtered = []
    for it in items:
        if it["mimeType"] == "application/vnd.google-apps.folder":
            filtered.append(it)
        elif it["mimeType"] in VIDEO_MIMES:
            ext = Path(it["name"]).suffix.lower()
            if it["mimeType"] != "application/octet-stream" or ext in VIDEO_EXTS:
                filtered.append(it)
        else:
            if Path(it["name"]).suffix.lower() in VIDEO_EXTS:
                filtered.append(it)

    return filtered, next_tok


# ─── Rclone yardımcıları ──────────────────────────────────────────────────────

def _rclone_bin() -> str:
    """rclone binary yolunu bulur; bulamazsa RuntimeError fırlatır."""
    import shutil as _sh
    found = _sh.which("rclone")
    if found:
        return found
    candidates = [
        "/usr/bin/rclone", "/usr/local/bin/rclone",
        "/usr/sbin/rclone", "/opt/rclone/rclone",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    raise RuntimeError(
        "rclone binary bulunamadı. "
        "Dockerfile yeniden build edilmeli: 'RUN curl https://rclone.org/install.sh | bash'"
    )


def _rclone_list_remotes() -> list[str]:
    """rclone.conf dosyasından sürücü adlarını okur — binary gerekmez."""
    if not RCLONE_CONF_PATH.exists():
        raise FileNotFoundError(
            f"rclone.conf bulunamadı: {RCLONE_CONF_PATH}\n"
            "/ayarlar → 📁 Dosya Ekle → rclone.conf Yükle"
        )
    import configparser
    rcp = configparser.ConfigParser()
    rcp.read(str(RCLONE_CONF_PATH))
    return rcp.sections()


def _rclone_list_dir(remote: str, path: str = "") -> list[dict]:
    """
    rclone lsjson komutuyla uzak dizini listeler.
    Her eleman: {name, path, is_dir, size, mime_type}
    """
    import subprocess, json as _json
    rclone = _rclone_bin()
    remote_path = f"{remote}:{path}"
    result = subprocess.run(
        [rclone, "lsjson", "--config", str(RCLONE_CONF_PATH), remote_path, "--no-modtime"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        raise RuntimeError(f"rclone lsjson hatası: {err[:300]}")
    items = _json.loads(result.stdout or "[]")
    filtered = []
    for it in items:
        name = it.get("Name", "")
        is_dir = it.get("IsDir", False)
        if is_dir:
            filtered.append({
                "name": name,
                "path": (path + "/" + name).lstrip("/"),
                "is_dir": True,
                "size": 0,
                "mime_type": "inode/directory",
            })
        else:
            ext = Path(name).suffix.lower()
            if ext in VIDEO_EXTS:
                filtered.append({
                    "name": name,
                    "path": (path + "/" + name).lstrip("/"),
                    "is_dir": False,
                    "size": it.get("Size", 0),
                    "mime_type": it.get("MimeType", "video/x-matroska"),
                })
    return filtered


def _rclone_file_meta(remote: str, path: str) -> dict:
    """Tek bir dosyanın boyutunu ve adını alır."""
    import subprocess, json as _json
    rclone  = _rclone_bin()
    parent  = str(Path(path).parent).replace("\\", "/")
    if parent in (".", ""):
        parent = ""
    list_target = f"{remote}:{parent}"
    result = subprocess.run(
        [rclone, "lsjson", "--config", str(RCLONE_CONF_PATH), list_target, "--no-modtime"],
        capture_output=True, text=True, timeout=60
    )
    name = Path(path).name
    size = 0
    if result.returncode == 0:
        try:
            for it in _json.loads(result.stdout or "[]"):
                if it.get("Name") == name:
                    size = it.get("Size", 0)
                    break
        except Exception:
            pass
    return {"name": name, "size": size, "remote": remote, "path": path}
    return {"name": name, "size": size, "remote": remote, "path": path}


def _get_item_meta(file_id: str) -> dict:
    svc = _get_gdrive_service()
    return svc.files().get(fileId=file_id, fields="id,name,size,mimeType").execute()


# ─── Callback-data ────────────────────────────────────────────────────────────

import base64 as _b64

_ID_CACHE: list = []


def _cache_id(value: str) -> int:
    try:
        return _ID_CACHE.index(value)
    except ValueError:
        _ID_CACHE.append(value)
        return len(_ID_CACHE) - 1


def _resolve_id(idx) -> str:
    try:
        return _ID_CACHE[int(idx)]
    except (IndexError, ValueError, TypeError):
        return str(idx)


def _cb_browse(folder_id: str, parent_id: str = "root", page_token: str = "") -> str:
    i_folder = _cache_id(folder_id)
    i_parent = _cache_id(parent_id)
    i_page   = _cache_id(page_token) if page_token else ""
    cb = f"ekle:browse:{i_folder}:{i_parent}:{i_page}"
    assert len(cb.encode()) <= 64, f"browse cb too long: {len(cb.encode())} bytes"
    return cb


def _cb_approve(file_id: str, folder_id: str) -> str:
    i_file   = _cache_id(file_id)
    i_folder = _cache_id(folder_id)
    cb = f"ekle:approve:{i_file}:{i_folder}"
    assert len(cb.encode()) <= 64, f"approve cb too long: {len(cb.encode())} bytes"
    return cb


def _cb_revoke(doc_id: str) -> str:
    return f"ekle:revoke:{doc_id}"


def _cb_approved_page(page: int) -> str:
    return f"ekle:approved_page:{page}"


# ─── DB yardımcıları ──────────────────────────────────────────────────────────

async def _db_save_approved(record: dict) -> str:
    try:
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is None:
            return ""
        result = await storage[APPROVED_COLLECTION].insert_one(record)
        return str(result.inserted_id)
    except Exception as e:
        LOGGER.error(f"[ekle] approved kayıt hatası: {e}")
        return ""


async def _db_list_approved(page: int = 0) -> tuple:
    try:
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is None:
            return [], 0
        col = storage[APPROVED_COLLECTION]
        total = await col.count_documents({})
        cursor = col.find({}).sort("added_at", -1).skip(page * PAGE_SIZE).limit(PAGE_SIZE)
        items = await cursor.to_list(length=PAGE_SIZE)
        return items, total
    except Exception as e:
        LOGGER.error(f"[ekle] approved liste hatası: {e}")
        return [], 0


async def _db_delete_approved(doc_id: str) -> bool:
    """Onaylanmış kaydı ve Stremio DB'den ilgili medyayı siler."""
    try:
        from bson import ObjectId
        storage = db.dbs.get(f"storage_{db.current_db_index}")
        if storage is None:
            return False
        col = storage[APPROVED_COLLECTION]
        doc = await col.find_one({"_id": ObjectId(doc_id)})
        if not doc:
            return False

        # Stremio DB'den sil — db_id = encoded_string (telegram.id)
        stream_id = doc.get("db_id", "")
        if stream_id:
            try:
                result = await db.delete_media_by_stream_id(stream_id)
                if not result:
                    LOGGER.warning(f"[ekle] delete_media_by_stream_id bulamadı: {stream_id}")
            except Exception as e:
                LOGGER.warning(f"[ekle] Stremio medya silme hatası: {e}")

        await col.delete_one({"_id": ObjectId(doc_id)})
        return True
    except Exception as e:
        LOGGER.error(f"[ekle] approved silme hatası: {e}")
        return False


# ─── UI oluşturucular ──────────────────────────────────────────────────────────

def _build_main_menu() -> InlineKeyboardMarkup:
    rclone_ok = RCLONE_CONF_PATH.exists()
    rows = [
        [
            InlineKeyboardButton("📋 Eklenenler",   callback_data="ekle:approved_page:0"),
            InlineKeyboardButton("📂 Google Drive", callback_data="ekle:main_drive:open"),
        ],
    ]
    if rclone_ok:
        rows.append([InlineKeyboardButton("☁️ Rclone Sürücüleri", callback_data="ekle:rclone_remotes:open")])
    rows.append([InlineKeyboardButton("✖ Kapat", callback_data="ekle:noop:close")])
    return InlineKeyboardMarkup(rows)


def _build_browse_keyboard(items, folder_id, parent_id, next_page_token, is_root) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        is_folder = it["mimeType"] == "application/vnd.google-apps.folder"
        if is_folder:
            label = f"📁 {it['name'][:40]}"
            cb    = _cb_browse(it["id"], folder_id)
        else:
            size_str = get_readable_file_size(int(it.get("size", 0)))
            label    = f"🎬 {it['name'][:32]} [{size_str}]"
            cb       = _cb_approve(it["id"], folder_id)
        rows.append([InlineKeyboardButton(label, callback_data=cb)])

    nav = []
    if not is_root:
        nav.append(InlineKeyboardButton("⬆ Üst Klasör", callback_data=_cb_browse(parent_id)))
    if next_page_token:
        nav.append(InlineKeyboardButton(
            "Sonraki ▶",
            callback_data=_cb_browse(folder_id, parent_id, next_page_token)
        ))
    if nav:
        rows.append(nav)

    bottom = []
    if is_root:
        bottom.append(InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0"))
    bottom.append(InlineKeyboardButton("✖ Kapat", callback_data="ekle:noop:close"))
    rows.append(bottom)
    return InlineKeyboardMarkup(rows)


def _build_approved_keyboard(items, page, total) -> InlineKeyboardMarkup:
    """
    Her kayıt tek buton: file_name gösterilir.
    Tıklayınca revoke tetiklenir — ayrı 'Geri Al' butonu yok.
    """
    rows = []
    for it in items:
        doc_id    = str(it["_id"])
        file_name = it.get("file_name", it.get("title", "?"))
        label = f"🗑 {file_name[:50]}"
        rows.append([InlineKeyboardButton(label, callback_data=_cb_revoke(doc_id))])

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Önceki", callback_data=_cb_approved_page(page - 1)))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="ekle:noop:0"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Sonraki ▶", callback_data=_cb_approved_page(page + 1)))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0"),
        InlineKeyboardButton("✖ Kapat",    callback_data="ekle:noop:close"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_rclone_remotes_keyboard(remotes: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for r in remotes:
        rows.append([InlineKeyboardButton(f"☁️ {r}", callback_data=f"ekle:rclone_browse:{r}::")])
    rows.append([
        InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0"),
        InlineKeyboardButton("✖ Kapat",    callback_data="ekle:noop:close"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_rclone_browse_keyboard(items: list[dict], remote: str, cur_path: str, parent_path: str) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        if it["is_dir"]:
            label = f"📁 {it['name'][:40]}"
            # path encode: remote::path (:: ayraç olarak)
            i_remote = _cache_id(remote)
            i_path   = _cache_id(it["path"])
            i_parent = _cache_id(cur_path)
            cb = f"ekle:rclone_browse:{i_remote}:{i_path}:{i_parent}"
        else:
            size_str = get_readable_file_size(it["size"])
            label    = f"🎬 {it['name'][:32]} [{size_str}]"
            i_remote = _cache_id(remote)
            i_path   = _cache_id(it["path"])
            i_folder = _cache_id(cur_path)
            cb = f"ekle:rclone_approve:{i_remote}:{i_path}:{i_folder}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])

    nav = []
    if cur_path:
        i_remote = _cache_id(remote)
        i_parent = _cache_id(parent_path)
        nav.append(InlineKeyboardButton("⬆ Üst Klasör", callback_data=f"ekle:rclone_browse:{i_remote}:{i_parent}:"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0"),
        InlineKeyboardButton("✖ Kapat",    callback_data="ekle:noop:close"),
    ])
    return InlineKeyboardMarkup(rows)


# ─── /ekle komutu ─────────────────────────────────────────────────────────────

@Client.on_message(
    filters.command("ekle") & filters.private & CustomFilters.owner
)
async def cmd_ekle(client: Client, message: Message):
    await message.reply_text(
        "📦 <b>İçerik Yönetimi</b>\n\n"
        "• <b>Eklenenler</b> — Eklenmiş içerikleri görüntüle / geri al\n"
        "• <b>Google Drive</b> — Drive'ı tara ve yeni içerik ekle",
        reply_markup=_build_main_menu(),
        parse_mode=ParseMode.HTML,
    )


# ─── Callback handler ─────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ekle:"))
async def cb_ekle(client: Client, query: CallbackQuery):
    data = query.data
    parts = data.split(":", 2)
    if len(parts) < 3:
        return await query.answer("Geçersiz buton.", show_alert=True)

    _, action, payload = parts

    # ── noop ──
    if action == "noop":
        if payload == "close":
            try:
                await query.message.delete()
            except Exception:
                pass
        return await query.answer()

    # ── menu ──
    if action == "menu":
        await query.answer()
        return await query.message.edit_text(
            "📦 <b>İçerik Yönetimi</b>\n\n"
            "• <b>Eklenenler</b> — Eklenmiş içerikleri görüntüle / geri al\n"
            "• <b>Google Drive</b> — Drive'ı tara ve yeni içerik ekle",
            reply_markup=_build_main_menu(),
            parse_mode=ParseMode.HTML,
        )

    # ── main_drive ──
    if action == "main_drive":
        await query.answer()
        await query.message.edit_text("📂 <b>Google Drive</b> bağlanıyor...", parse_mode=ParseMode.HTML)
        try:
            items, next_tok = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _list_drive_items("root")
            )
        except FileNotFoundError as e:
            return await query.message.edit_text(
                f"❌ {e}\n\n<i>token.pickle yüklü değil.</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            LOGGER.error(f"[ekle] Drive liste hatası: {e}\n{traceback.format_exc()}")
            return await query.message.edit_text(
                f"❌ Drive'a bağlanılamadı:\n<code>{str(e)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )

        if not items:
            return await query.message.edit_text(
                "📂 Drive'da klasör veya video bulunamadı.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0")
                ]]),
            )

        kb = _build_browse_keyboard(items, "root", "root", next_tok, is_root=True)
        return await query.message.edit_text(
            "📂 <b>Google Drive — Ana Klasör</b>\n"
            "📁 klasöre gir  |  🎬 videoya bas → Stremio'ya ekle",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )

    # ── browse ──
    if action == "browse":
        await query.answer()
        try:
            p = payload.split(":", 2)
            folder_id  = _resolve_id(p[0])
            parent_id  = _resolve_id(p[1]) if len(p) > 1 else "root"
            page_token = _resolve_id(p[2]) if len(p) > 2 and p[2] != "" else ""
        except Exception:
            return await query.answer("Payload hatalı.", show_alert=True)

        await query.message.edit_text("📂 Klasör yükleniyor...", parse_mode=ParseMode.HTML)
        try:
            items, next_tok = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _list_drive_items(folder_id, page_token or None)
            )
        except Exception as e:
            return await query.message.edit_text(
                f"❌ Drive hatası: <code>{str(e)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )

        is_root = (folder_id == "root")
        if not items:
            nav = []
            if not is_root:
                nav.append([InlineKeyboardButton("⬆ Üst Klasör", callback_data=_cb_browse(parent_id))])
            nav.append([InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0")])
            return await query.message.edit_text(
                "📂 Bu klasör boş (video veya alt klasör yok).",
                reply_markup=InlineKeyboardMarkup(nav),
                parse_mode=ParseMode.HTML,
            )

        try:
            folder_name = "Ana Klasör" if folder_id == "root" else (
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _get_item_meta(folder_id)
                )
            ).get("name", folder_id)
        except Exception:
            folder_name = folder_id

        kb = _build_browse_keyboard(items, folder_id, parent_id, next_tok, is_root)
        return await query.message.edit_text(
            f"📂 <b>{folder_name}</b>\n"
            "🎬 videoya bas → ekle  |  📁 klasöre gir",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )

    # ── approve ──
    elif action == "approve":
        await query.answer("⏳ İşleniyor...", show_alert=False)
        try:
            p         = payload.split(":", 1)
            file_id   = _resolve_id(p[0])
            folder_id = _resolve_id(p[1]) if len(p) > 1 else "root"
        except Exception:
            return await query.answer("Payload hatalı.", show_alert=True)

        # Daha önce eklendi mi?
        try:
            storage = db.dbs.get(f"storage_{db.current_db_index}")
            if storage is not None:
                existing = await storage[APPROVED_COLLECTION].find_one({"file_id": file_id})
                if existing:
                    return await query.answer(
                        f"⚠️ Bu dosya zaten eklendi:\n{existing.get('file_name', '?')}",
                        show_alert=True
                    )
        except Exception:
            pass

        try:
            drive_meta = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _get_item_meta(file_id)
            )
        except Exception as e:
            return await query.answer(f"❌ Drive meta hatası: {str(e)[:80]}", show_alert=True)

        file_name = drive_meta.get("name", file_id)
        size_str  = get_readable_file_size(int(drive_meta.get("size", 0)))

        await query.message.edit_text(
            f"🔍 <b>Metadata aranıyor...</b>\n📄 <code>{file_name}</code>",
            parse_mode=ParseMode.HTML,
        )

        from Backend.helper.metadata import extract_default_id
        override_id, _ = extract_default_id(file_name)
        clean_name = clean_filename(file_name)

        FAKE_CHANNEL = 0
        FAKE_MSG_ID  = 0

        try:
            meta_info = await metadata(clean_name, FAKE_CHANNEL, FAKE_MSG_ID, override_id=override_id)
        except Exception as e:
            LOGGER.error(f"[ekle] metadata hatası: {e}\n{traceback.format_exc()}")
            return await query.message.edit_text(
                f"❌ Metadata hatası: <code>{str(e)[:200]}</code>\n\n<i>{file_name}</i>",
                parse_mode=ParseMode.HTML,
            )

        if meta_info is None:
            return await query.message.edit_text(
                f"⚠️ <b>Metadata bulunamadı</b>\n<code>{file_name}</code>\n\n"
                "Dosya adı tanınmadı. İçerik TMDb'de kayıtlı olmayabilir.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ Klasöre Dön", callback_data=_cb_browse(folder_id))
                ]]),
                parse_mode=ParseMode.HTML,
            )

        media_title = meta_info.get("title", clean_name)
        await query.message.edit_text(
            f"💾 <b>DB'ye kaydediliyor...</b>\n🎬 <b>{media_title}</b>",
            parse_mode=ParseMode.HTML,
        )

        drive_encoded = ""
        try:
            from Backend.helper.encrypt import encode_string as _encode_string
            drive_encoded = await _encode_string({"gdrive_file_id": file_id})
            meta_info = dict(meta_info)
            meta_info["encoded_string"] = drive_encoded
        except Exception as e:
            LOGGER.warning(f"[ekle] encoded_string hatası: {e}")

        display_name = remove_urls(file_name)
        if Path(display_name).suffix.lower() not in VIDEO_EXTS:
            display_name += ".mkv"

        try:
            updated_id = await db.insert_media(
                meta_info,
                channel=FAKE_CHANNEL,
                msg_id=FAKE_MSG_ID,
                size=size_str,
                name=display_name,
            )
        except Exception as e:
            LOGGER.error(f"[ekle] DB insert hatası: {e}\n{traceback.format_exc()}")
            return await query.message.edit_text(
                f"❌ DB hatası: <code>{str(e)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )

        if not updated_id:
            return await query.message.edit_text(
                f"⚠️ DB kaydı başarısız: <b>{media_title}</b>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ Klasöre Dön", callback_data=_cb_browse(folder_id))
                ]]),
                parse_mode=ParseMode.HTML,
            )

        # db_id = encoded_string → delete_media_by_stream_id ile eşleşir
        record = {
            "file_id":   file_id,
            "file_name": file_name,
            "title":     media_title,
            "db_id":     drive_encoded,
            "size":      size_str,
            "folder_id": folder_id,
            "added_at":  int(time.time()),
        }
        await _db_save_approved(record)
        LOGGER.info(f"[ekle] ✅ Eklendi: {media_title} | file={file_name} | Drive={file_id}")

        await query.message.edit_text(
            f"✅ <b>Eklendi!</b>\n\n"
            f"🎬 <b>Başlık:</b> {media_title}\n"
            f"📄 <b>Dosya:</b> {display_name}\n"
            f"💾 <b>Boyut:</b> {size_str}\n"
            f"📁 <b>Tür:</b> {meta_info.get('media_type', 'movie').upper()}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Klasöre Dön", callback_data=_cb_browse(folder_id))],
                [InlineKeyboardButton("📋 Eklenenler",  callback_data=_cb_approved_page(0))],
            ]),
            parse_mode=ParseMode.HTML,
        )

    # ── revoke ──
    elif action == "revoke":
        doc_id = payload

        try:
            from bson import ObjectId
            storage = db.dbs.get(f"storage_{db.current_db_index}")
            doc = await storage[APPROVED_COLLECTION].find_one({"_id": ObjectId(doc_id)}) if storage else None
        except Exception:
            doc = None

        file_name = doc.get("file_name", doc.get("title", "?")) if doc else "?"

        deleted = await _db_delete_approved(doc_id)
        if deleted:
            await query.answer(f"🗑 '{file_name}' kaldırıldı.", show_alert=True)
            LOGGER.info(f"[ekle] Geri alındı: {file_name} | doc={doc_id}")
        else:
            await query.answer("❌ Kayıt bulunamadı veya silinemedi.", show_alert=True)
            return

        items, total = await _db_list_approved(0)
        if not items:
            try:
                await query.message.edit_text(
                    "📋 Eklenenler listesi boşaldı.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0")
                    ]]),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        try:
            await query.message.edit_text(
                f"📋 <b>Eklenenler</b> — {total} içerik\n"
                "<i>Dosya adına bas → Stremio'dan kaldır</i>",
                reply_markup=_build_approved_keyboard(items, 0, total),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # ── approved_page ──
    elif action == "approved_page":
        await query.answer()
        try:
            page = int(payload)
        except Exception:
            page = 0

        items, total = await _db_list_approved(page)
        if not items:
            return await query.message.edit_text(
                "📋 Henüz eklenmiş içerik yok.\n"
                "Google Drive'dan içerik ekleyebilirsin.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0")
                ]]),
                parse_mode=ParseMode.HTML,
            )

        try:
            await query.message.edit_text(
                f"📋 <b>Eklenenler</b> — {total} içerik\n"
                "<i>Dosya adına bas → Stremio'dan kaldır</i>",
                reply_markup=_build_approved_keyboard(items, page, total),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # ── rclone_remotes ──
    elif action == "rclone_remotes":
        await query.answer()
        try:
            remotes = await asyncio.get_event_loop().run_in_executor(None, _rclone_list_remotes)
        except FileNotFoundError as e:
            return await query.message.edit_text(
                f"❌ {e}\n\n<i>rclone.conf yüklü değil.</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            return await query.message.edit_text(
                f"❌ Rclone hatası: <code>{str(e)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )
        if not remotes:
            return await query.message.edit_text(
                "☁️ rclone.conf'ta hiç sürücü bulunamadı.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0")]]),
            )
        kb = _build_rclone_remotes_keyboard(remotes)
        return await query.message.edit_text(
            f"☁️ <b>Rclone Sürücüleri</b> — {len(remotes)} sürücü\n"
            "Sürücüye bas → içerikleri listele",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )

    # ── rclone_browse ──
    elif action == "rclone_browse":
        await query.answer()
        try:
            p = payload.split(":", 2)
            remote      = _resolve_id(p[0])
            cur_path    = _resolve_id(p[1]) if len(p) > 1 and p[1] else ""
            parent_path = _resolve_id(p[2]) if len(p) > 2 and p[2] else ""
        except Exception:
            return await query.answer("Payload hatalı.", show_alert=True)

        await query.message.edit_text(
            f"☁️ <b>{remote}:{cur_path or '/'}</b> yükleniyor...",
            parse_mode=ParseMode.HTML,
        )
        try:
            items = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _rclone_list_dir(remote, cur_path)
            )
        except Exception as e:
            return await query.message.edit_text(
                f"❌ Rclone liste hatası: <code>{str(e)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )
        if not items:
            i_remote = _cache_id(remote)
            i_parent = _cache_id(parent_path)
            return await query.message.edit_text(
                "📂 Bu klasör boş (video veya alt klasör yok).",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬆ Üst Klasör", callback_data=f"ekle:rclone_browse:{i_remote}:{i_parent}:")],
                    [InlineKeyboardButton("⬅ Ana Menü", callback_data="ekle:menu:0")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        kb = _build_rclone_browse_keyboard(items, remote, cur_path, parent_path)
        folder_label = f"{remote}:{cur_path}" if cur_path else f"{remote}: (Kök)"
        return await query.message.edit_text(
            f"☁️ <b>{folder_label}</b>\n"
            "🎬 videoya bas → ekle  |  📁 klasöre gir",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )

    # ── rclone_approve ──
    elif action == "rclone_approve":
        await query.answer("⏳ İşleniyor...", show_alert=False)
        try:
            p = payload.split(":", 2)
            remote      = _resolve_id(p[0])
            file_path   = _resolve_id(p[1])
            folder_path = _resolve_id(p[2]) if len(p) > 2 else ""
        except Exception:
            return await query.answer("Payload hatalı.", show_alert=True)

        rclone_key = f"{remote}:{file_path}"

        # Daha önce eklendi mi?
        try:
            storage = db.dbs.get(f"storage_{db.current_db_index}")
            if storage is not None:
                existing = await storage[APPROVED_COLLECTION].find_one({"rclone_path": rclone_key})
                if existing:
                    return await query.answer(
                        f"⚠️ Bu dosya zaten eklendi:\n{existing.get('file_name', '?')}",
                        show_alert=True
                    )
        except Exception:
            pass

        try:
            file_meta = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _rclone_file_meta(remote, file_path)
            )
        except Exception as e:
            return await query.answer(f"❌ Rclone meta hatası: {str(e)[:80]}", show_alert=True)

        file_name = file_meta.get("name", Path(file_path).name)
        size_str  = get_readable_file_size(file_meta.get("size", 0))

        await query.message.edit_text(
            f"🔍 <b>Metadata aranıyor...</b>\n📄 <code>{file_name}</code>",
            parse_mode=ParseMode.HTML,
        )

        from Backend.helper.metadata import extract_default_id
        override_id, _ = extract_default_id(file_name)
        clean_name = clean_filename(file_name)
        FAKE_CHANNEL = 0
        FAKE_MSG_ID  = 0

        try:
            meta_info = await metadata(clean_name, FAKE_CHANNEL, FAKE_MSG_ID, override_id=override_id)
        except Exception as e:
            LOGGER.error(f"[ekle/rclone] metadata hatası: {e}\n{traceback.format_exc()}")
            return await query.message.edit_text(
                f"❌ Metadata hatası: <code>{str(e)[:200]}</code>\n\n<i>{file_name}</i>",
                parse_mode=ParseMode.HTML,
            )

        if meta_info is None:
            i_remote = _cache_id(remote)
            i_folder = _cache_id(folder_path)
            return await query.message.edit_text(
                f"⚠️ <b>Metadata bulunamadı</b>\n<code>{file_name}</code>\n\n"
                "Dosya adı tanınmadı. İçerik TMDb'de kayıtlı olmayabilir.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ Klasöre Dön",
                        callback_data=f"ekle:rclone_browse:{i_remote}:{i_folder}:")
                ]]),
                parse_mode=ParseMode.HTML,
            )

        media_title = meta_info.get("title", clean_name)
        await query.message.edit_text(
            f"💾 <b>DB'ye kaydediliyor...</b>\n🎬 <b>{media_title}</b>",
            parse_mode=ParseMode.HTML,
        )

        rclone_encoded = ""
        try:
            from Backend.helper.encrypt import encode_string as _encode_string
            rclone_encoded = await _encode_string({"rclone_remote": remote, "rclone_path": file_path})
            meta_info = dict(meta_info)
            meta_info["encoded_string"] = rclone_encoded
        except Exception as e:
            LOGGER.warning(f"[ekle/rclone] encoded_string hatası: {e}")

        display_name = remove_urls(file_name)
        if Path(display_name).suffix.lower() not in VIDEO_EXTS:
            display_name += ".mkv"

        try:
            updated_id = await db.insert_media(
                meta_info,
                channel=FAKE_CHANNEL,
                msg_id=FAKE_MSG_ID,
                size=size_str,
                name=display_name,
            )
        except Exception as e:
            LOGGER.error(f"[ekle/rclone] DB insert hatası: {e}\n{traceback.format_exc()}")
            return await query.message.edit_text(
                f"❌ DB hatası: <code>{str(e)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )

        if not updated_id:
            i_remote = _cache_id(remote)
            i_folder = _cache_id(folder_path)
            return await query.message.edit_text(
                f"⚠️ DB kaydı başarısız: <b>{media_title}</b>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ Klasöre Dön",
                        callback_data=f"ekle:rclone_browse:{i_remote}:{i_folder}:")
                ]]),
                parse_mode=ParseMode.HTML,
            )

        record = {
            "rclone_path": rclone_key,
            "file_name":   file_name,
            "title":       media_title,
            "db_id":       rclone_encoded,
            "size":        size_str,
            "folder_path": folder_path,
            "remote":      remote,
            "source":      "rclone",
            "added_at":    int(time.time()),
        }
        saved_id = await _db_save_approved(record)
        if not saved_id:
            LOGGER.error(f"[ekle/rclone] ⚠️ ekle_approved kaydı başarısız: {media_title} | {rclone_key}")
        LOGGER.info(f"[ekle/rclone] ✅ Eklendi: {media_title} | {rclone_key} | approved_id={saved_id}")

        i_remote = _cache_id(remote)
        i_folder = _cache_id(folder_path)
        await query.message.edit_text(
            f"✅ <b>Eklendi!</b>\n\n"
            f"🎬 <b>Başlık:</b> {media_title}\n"
            f"📄 <b>Dosya:</b> {display_name}\n"
            f"💾 <b>Boyut:</b> {size_str}\n"
            f"☁️ <b>Kaynak:</b> Rclone ({remote})\n"
            f"📁 <b>Tür:</b> {meta_info.get('media_type', 'movie').upper()}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Klasöre Dön",
                    callback_data=f"ekle:rclone_browse:{i_remote}:{i_folder}:")],
                [InlineKeyboardButton("📋 Eklenenler", callback_data=_cb_approved_page(0))],
            ]),
            parse_mode=ParseMode.HTML,
        )

    else:
        await query.answer("Bilinmeyen işlem.", show_alert=True)
