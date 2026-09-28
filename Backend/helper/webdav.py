"""
webdav.py
=========
WebDAV istemcisi (rclone binary'sine ihtiyaç duymaz).

- Sunucu profilleri MongoDB `tracking.webdav_servers` koleksiyonunda saklanır.
- Kimlik bilgileri yalnızca sunucu tarafında kalır; Stremio'ya / istemciye giden
  URL'ler imzalı `/dl/...` linkleridir (WebDAV şifresi asla dışarı çıkmaz).
- Medya kaydının encoded_string'i: {"webdav_id": <profil id>, "webdav_path": <göreli yol>}
"""

import asyncio
import time
import uuid
import xml.etree.ElementTree as ET
from typing import AsyncIterator, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

import httpx

from Backend import db
from Backend.logger import LOGGER

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m4v",
    ".webm", ".flv", ".mpg", ".mpeg",
}

_DAV = "{DAV:}"
_COLLECTION = "webdav_servers"

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    "</d:prop></d:propfind>"
)


class WebDAVError(Exception):
    pass


# ── Profil yönetimi ───────────────────────────────────────────────────────────

def _col():
    return db.dbs["tracking"][_COLLECTION]


def _public(doc: dict) -> dict:
    """İstemciye şifre döndürme."""
    return {
        "id": doc["_id"],
        "name": doc.get("name", ""),
        "url": doc.get("url", ""),
        "username": doc.get("username", ""),
        "has_password": bool(doc.get("password")),
        "auth_type": doc.get("auth_type", "basic"),
        "verify_ssl": doc.get("verify_ssl", True),
        "root": doc.get("root", ""),
    }


async def list_servers() -> List[dict]:
    return [_public(d) async for d in _col().find({}).sort("name", 1)]


async def get_server(server_id: str) -> Optional[dict]:
    """Şifre dahil ham profil (yalnızca sunucu içinde kullanılır)."""
    return await _col().find_one({"_id": server_id})


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise WebDAVError("URL http:// veya https:// ile başlamalı")
    return url.rstrip("/")


def _clean_root(root: str) -> str:
    parts = [s for s in (root or "").replace("\\", "/").split("/") if s]
    if any(s in (".", "..") for s in parts):
        raise WebDAVError("Geçersiz kök klasör")
    return "/".join(parts)


async def save_server(data: dict) -> dict:
    """Ekle veya güncelle. Güncellemede boş şifre = mevcut şifreyi koru."""
    sid = (data.get("id") or "").strip()
    name = (data.get("name") or "").strip()
    if not name:
        raise WebDAVError("Ad gerekli")
    auth_type = data.get("auth_type", "basic")
    if auth_type not in ("basic", "digest", "none"):
        raise WebDAVError("Geçersiz kimlik doğrulama türü")

    doc = {
        "name": name,
        "url": _validate_url(data.get("url", "")),
        "username": (data.get("username") or "").strip(),
        "auth_type": auth_type,
        "verify_ssl": bool(data.get("verify_ssl", True)),
        "root": _clean_root(data.get("root", "")),
    }
    password = data.get("password") or ""

    if sid:
        existing = await get_server(sid)
        if not existing:
            raise WebDAVError("Sunucu bulunamadı")
        doc["password"] = password or existing.get("password", "")
        await _col().update_one({"_id": sid}, {"$set": doc})
    else:
        sid = uuid.uuid4().hex[:12]
        doc["password"] = password
        doc["created_at"] = int(time.time())
        await _col().insert_one({"_id": sid, **doc})

    _clients.pop(sid, None)
    _stat_cache.clear()
    return _public({"_id": sid, **doc})


async def delete_server(server_id: str) -> bool:
    res = await _col().delete_one({"_id": server_id})
    _clients.pop(server_id, None)
    _stat_cache.clear()
    return res.deleted_count > 0


# ── HTTP istemcisi ────────────────────────────────────────────────────────────

_clients: Dict[str, httpx.AsyncClient] = {}


def _client_for(server: dict) -> httpx.AsyncClient:
    sid = server["_id"]
    cli = _clients.get(sid)
    if cli is not None and not cli.is_closed:
        return cli

    auth = None
    if server.get("username") and server.get("auth_type", "basic") != "none":
        if server.get("auth_type") == "digest":
            auth = httpx.DigestAuth(server["username"], server.get("password", ""))
        else:
            auth = httpx.BasicAuth(server["username"], server.get("password", ""))

    cli = httpx.AsyncClient(
        auth=auth,
        verify=server.get("verify_ssl", True),
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=15.0, read=60.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
    )
    _clients[sid] = cli
    return cli


def _norm_path(path: str) -> str:
    parts = [s for s in (path or "").replace("\\", "/").split("/") if s]
    if any(s in (".", "..") for s in parts):
        raise WebDAVError("Geçersiz yol")
    return "/".join(parts)


def _full_path(server: dict, path: str) -> str:
    """Profil kökü + göreli yol → sunucu URL köküne göre yol."""
    root = server.get("root", "")
    rel = _norm_path(path)
    return "/".join(p for p in (root, rel) if p)


def _url(server: dict, path: str, *, trailing_slash: bool = False) -> str:
    full = _full_path(server, path)
    url = server["url"].rstrip("/")
    if full:
        url += "/" + "/".join(quote(seg, safe="") for seg in full.split("/"))
    if trailing_slash:
        url += "/"
    return url


def _rel_from_href(server: dict, href: str) -> str:
    """PROPFIND href'ini profil köküne göre göreli yola çevirir."""
    href_path = unquote(urlparse(href).path)
    base_path = unquote(urlparse(server["url"]).path).rstrip("/")
    if base_path and href_path.startswith(base_path):
        href_path = href_path[len(base_path):]
    href_path = href_path.strip("/")
    root = server.get("root", "")
    if root:
        if href_path == root:
            return ""
        if href_path.startswith(root + "/"):
            return href_path[len(root) + 1:]
    return href_path


# ── İşlemler ──────────────────────────────────────────────────────────────────

async def _propfind(server: dict, path: str, depth: int) -> List[dict]:
    cli = _client_for(server)
    try:
        resp = await cli.request(
            "PROPFIND",
            _url(server, path, trailing_slash=True),
            headers={"Depth": str(depth), "Content-Type": "application/xml"},
            content=_PROPFIND_BODY,
        )
    except httpx.HTTPError as e:
        raise WebDAVError(f"Bağlantı hatası: {type(e).__name__}") from e

    if resp.status_code == 401:
        raise WebDAVError("Kimlik doğrulama başarısız (401)")
    if resp.status_code == 404:
        raise WebDAVError("Yol bulunamadı (404)")
    if resp.status_code not in (200, 207):
        raise WebDAVError(f"Sunucu beklenmeyen yanıt verdi ({resp.status_code})")

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise WebDAVError("Sunucu yanıtı çözümlenemedi (WebDAV değil mi?)") from e

    entries = []
    for r in root.findall(f"{_DAV}response"):
        href_el = r.find(f"{_DAV}href")
        if href_el is None or not href_el.text:
            continue
        rel = _rel_from_href(server, href_el.text)

        is_dir, size, modified = False, 0, ""
        for ps in r.findall(f"{_DAV}propstat"):
            status = ps.findtext(f"{_DAV}status") or ""
            if status and " 200" not in status:
                continue  # 404 vb. propstat'leri atla; status yoksa kabul et
            prop = ps.find(f"{_DAV}prop")
            if prop is None:
                continue
            rt = prop.find(f"{_DAV}resourcetype")
            if rt is not None and rt.find(f"{_DAV}collection") is not None:
                is_dir = True
            cl = prop.findtext(f"{_DAV}getcontentlength")
            if cl and cl.strip().isdigit():
                size = int(cl.strip())
            modified = prop.findtext(f"{_DAV}getlastmodified") or modified

        entries.append({
            "name": rel.rsplit("/", 1)[-1],
            "path": rel,
            "is_dir": is_dir,
            "size": size,
            "modified": modified,
        })
    return entries


async def test_connection(server: dict) -> dict:
    entries = await _propfind(server, "", 1)
    return {"ok": True, "items": len([e for e in entries if e["path"]])}


async def list_dir(server: dict, path: str = "") -> dict:
    path = _norm_path(path)
    entries = await _propfind(server, path, 1)
    folders, files = [], []
    for e in entries:
        if e["path"] == path or not e["name"]:
            continue  # dizinin kendisi
        if e["is_dir"]:
            folders.append({**e, "size": 0})
        elif "." + e["name"].rsplit(".", 1)[-1].lower() in VIDEO_EXTS:
            files.append(e)
    folders.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return {"path": path, "items": folders + files}


async def walk_videos(server: dict, path: str = "", max_depth: int = 6,
                      max_files: int = 5000) -> List[dict]:
    """Klasörü özyinelemeli tarar; yalnızca video dosyalarını döndürür."""
    results: List[dict] = []
    queue = [(_norm_path(path), 0)]
    while queue and len(results) < max_files:
        cur, depth = queue.pop(0)
        listing = await list_dir(server, cur)
        for it in listing["items"]:
            if it["is_dir"]:
                if depth < max_depth:
                    queue.append((it["path"], depth + 1))
            else:
                results.append(it)
                if len(results) >= max_files:
                    break
    results.sort(key=lambda x: x["path"].lower())
    return results


_stat_cache: Dict[str, tuple] = {}
_STAT_TTL = 600


async def stat_size(server: dict, path: str) -> int:
    """Dosya boyutu. Range istekleri sık geldiği için kısa süreli önbellek."""
    key = f"{server['_id']}:{_norm_path(path)}"
    hit = _stat_cache.get(key)
    if hit and time.time() - hit[1] < _STAT_TTL:
        return hit[0]

    size = 0
    try:
        entries = await _propfind(server, path, 0)
        if entries:
            size = entries[0]["size"]
    except WebDAVError:
        pass

    if not size:
        try:
            r = await _client_for(server).head(_url(server, path))
            if r.status_code == 200:
                size = int(r.headers.get("Content-Length", 0) or 0)
        except (httpx.HTTPError, ValueError):
            pass

    if size:
        _stat_cache[key] = (size, time.time())
    return size


async def stream_range(server: dict, path: str, start: int, end: int,
                       chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """[start, end] (dahil) byte aralığını WebDAV sunucusundan akıtır."""
    cli = _client_for(server)
    headers = {"Range": f"bytes={start}-{end}"}
    to_skip = 0
    remaining = end - start + 1

    async with cli.stream("GET", _url(server, path), headers=headers) as resp:
        if resp.status_code == 416:
            raise WebDAVError("İstenen aralık geçersiz (416)")
        if resp.status_code == 200 and start > 0:
            # Sunucu Range'i desteklemiyor: baştan okuyup atla (yavaş ama doğru)
            LOGGER.warning("[webdav] Sunucu Range desteklemiyor: %s", server.get("name"))
            to_skip = start
        elif resp.status_code not in (200, 206):
            raise WebDAVError(f"Sunucu hatası ({resp.status_code})")

        async for chunk in resp.aiter_bytes(chunk_size):
            if to_skip:
                if len(chunk) <= to_skip:
                    to_skip -= len(chunk)
                    continue
                chunk = chunk[to_skip:]
                to_skip = 0
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            remaining -= len(chunk)
            yield chunk
            if remaining <= 0:
                return


async def close_all() -> None:
    for cli in list(_clients.values()):
        try:
            await cli.aclose()
        except Exception:
            pass
    _clients.clear()
