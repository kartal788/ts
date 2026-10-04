"""
deepl_keys.py
=============
Birden fazla DeepL API anahtarını yöneten yardımcı modül.

  * Anahtarlar sırayla denenir (listedeki sıra = öncelik).
  * Bir anahtarın karakter kotası dolarsa (HTTP 456) ya da anahtar geçersizse (HTTP 403)
    o anahtar bir süre devre dışı bırakılır ve AYNI istekte sıradaki anahtara geçilir.
  * Kotası dolan anahtar belirli aralıkla (EXHAUSTED_RETRY) tekrar denenir; kota
    yenilenmişse (ay başı) kendiliğinden yeniden devreye girer. Panelden kullanım
    yenilendiğinde de kalan karakter > 0 ise hemen serbest bırakılır.
  * Tüm anahtarlar kullanılamazsa DeepL adımı atlanır → Google/MyMemory zincirine düşer.

Çeviri thread'lerde (senkron), kullanım sorgusu async çalışır; durum thread-safe tutulur.
"""

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from Backend.config import Telegram
from Backend.logger import LOGGER

LOW_CHARS_THRESHOLD = 5000        # kalan karakter bu değerin altındaysa "azaldı" uyarısı
EXHAUSTED_RETRY = 30 * 60         # kotası dolan anahtar kaç sn sonra tekrar denensin
INVALID_RETRY = 6 * 60 * 60       # geçersiz anahtar kaç sn sonra tekrar denensin
_USAGE_CACHE_TTL = 20             # sn; panel/dashboard sık yenilemesinde DeepL'e yüklenmemek için

_LOCK = threading.Lock()
# key -> {"blocked_until": float, "kind": "exhausted"|"invalid"|"", "reason": str}
_STATE: Dict[str, Dict[str, Any]] = {}
# key -> (ts, usage_dict)
_USAGE_CACHE: Dict[str, tuple] = {}


# ── Anahtar listesi ───────────────────────────────────────────────────────────

def split_keys(raw) -> List[str]:
    """str (virgül/satır ile ayrılmış) veya liste → temiz, tekrarsız, sıralı liste."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace("\n", ",").replace(";", ",").split(",")
    else:
        parts = []
        for item in raw:
            parts.extend(str(item or "").replace("\n", ",").replace(";", ",").split(","))
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def get_keys() -> List[str]:
    """Etkin anahtar listesi. Liste boşsa eski tekil DEEPL_API ayarına düşer."""
    keys = split_keys(getattr(Telegram, "DEEPL_API_KEYS", None))
    if not keys:
        keys = split_keys(getattr(Telegram, "DEEPL_API", ""))
    return keys


def mask(key: str) -> str:
    suffix = ":fx" if key.endswith(":fx") else ""
    core = key[:-3] if suffix else key
    if len(core) <= 8:
        return "••••" + suffix
    return f"{core[:4]}••••{core[-4:]}{suffix}"


def _endpoint(key: str, path: str) -> str:
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    return f"https://{host}/v2/{path}"


# ── Durum ─────────────────────────────────────────────────────────────────────

def _block(key: str, kind: str, reason: str, seconds: int) -> None:
    with _LOCK:
        prev = _STATE.get(key) or {}
        _STATE[key] = {"blocked_until": time.time() + seconds, "kind": kind, "reason": reason}
    if prev.get("kind") != kind:   # aynı durumu tekrar tekrar loglama
        LOGGER.warning("[deepl-keys] %s devre dışı: %s — sıradaki anahtara geçiliyor.", mask(key), reason)


def _unblock(key: str) -> None:
    with _LOCK:
        had = _STATE.pop(key, None)
    if had and had.get("kind"):
        LOGGER.info("[deepl-keys] %s yeniden kullanılabilir.", mask(key))


def _blocked(key: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    now = now or time.time()
    with _LOCK:
        st = _STATE.get(key)
        if st and st.get("blocked_until", 0) > now:
            return dict(st)
    return None


def prune(valid_keys: List[str]) -> None:
    """Listeden çıkarılan anahtarların durumunu unut (tekrar eklenirse temiz başlasın)."""
    keep = set(valid_keys)
    with _LOCK:
        for k in list(_STATE):
            if k not in keep:
                del _STATE[k]
        for k in list(_USAGE_CACHE):
            if k not in keep:
                del _USAGE_CACHE[k]


def active_key() -> Optional[str]:
    """Şu an çeviri için kullanılacak (engelli olmayan ilk) anahtar."""
    now = time.time()
    for k in get_keys():
        if not _blocked(k, now):
            return k
    return None


def has_usable_keys() -> bool:
    return active_key() is not None


# ── Çeviri (senkron, thread'ten çağrılır) ─────────────────────────────────────

def translate(text: str, target: str) -> Optional[str]:
    """Sıradaki kullanılabilir anahtarla çevirir; kota/yetki hatasında sonrakine geçer."""
    now = time.time()
    for key in get_keys():
        if _blocked(key, now):
            continue
        try:
            resp = httpx.post(
                _endpoint(key, "translate"),
                headers={"Authorization": f"DeepL-Auth-Key {key}"},
                data={"text": text, "target_lang": target.upper()},
                timeout=15,
            )
        except Exception:
            return None                      # ağ hatası: tüm anahtarlar için geçerli, zinciri sürdür

        code = resp.status_code
        if code == 200:
            try:
                translated = resp.json()["translations"][0]["text"]
            except Exception:
                return None
            if _blocked(key):                # engelliyken (retry süresi dolmuş) başarılı → serbest bırak
                _unblock(key)
            return translated or None
        if code == 456:                      # kota doldu
            _block(key, "exhausted", "Karakter limiti doldu", EXHAUSTED_RETRY)
            continue
        if code in (401, 403):               # geçersiz anahtar
            _block(key, "invalid", "Geçersiz API anahtarı", INVALID_RETRY)
            continue
        return None                          # 429/5xx/diğer: geçici, anahtarı engelleme
    return None


# ── Kullanım (async) ──────────────────────────────────────────────────────────

async def _fetch_usage(key: str, use_cache: bool = True) -> Dict[str, Any]:
    if use_cache:
        hit = _USAGE_CACHE.get(key)
        if hit and time.time() - hit[0] < _USAGE_CACHE_TTL:
            return hit[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _endpoint(key, "usage"),
                headers={"Authorization": f"DeepL-Auth-Key {key}"},
            )
        if resp.status_code != 200:
            try:
                msg = resp.json().get("message")
            except Exception:
                msg = None
            result = {"error": msg or f"HTTP {resp.status_code}", "http": resp.status_code}
        else:
            data = resp.json()
            result = {
                "character_count": data.get("character_count"),
                "character_limit": data.get("character_limit"),
            }
    except Exception as exc:
        result = {"error": str(exc) or "bağlantı hatası"}
    _USAGE_CACHE[key] = (time.time(), result)
    return result


def _describe(index: int, key: str, usage: Dict[str, Any], active: Optional[str]) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "index": index, "masked": mask(key), "active": key == active,
        "used": None, "limit": None, "remaining": None, "percent_used": None,
        "error": "", "status": "ok",
    }
    blk = _blocked(key)

    if usage.get("error"):
        item["error"] = usage["error"]
        if usage.get("http") in (401, 403):
            _block(key, "invalid", "Geçersiz API anahtarı", INVALID_RETRY)
            item["status"] = "invalid"
        else:
            item["status"] = "exhausted" if (blk and blk.get("kind") == "exhausted") else "error"
        if blk and not item["error"]:
            item["error"] = blk.get("reason", "")
        return item

    used, limit = usage.get("character_count"), usage.get("character_limit")
    item["used"], item["limit"] = used, limit
    if used is not None and limit:
        remaining = max(int(limit) - int(used), 0)
        item["remaining"] = remaining
        item["percent_used"] = round(int(used) / int(limit) * 100, 1)
        if remaining <= 0:
            _block(key, "exhausted", "Karakter limiti doldu", EXHAUSTED_RETRY)
            item["status"] = "exhausted"
            item["error"] = "Karakter limiti doldu"
        else:
            if blk:                              # kota yenilenmiş / limit yükseltilmiş
                _unblock(key)
            item["status"] = "low" if remaining <= LOW_CHARS_THRESHOLD else "ok"
    return item


async def keys_usage(keys: Optional[List[str]] = None, use_cache: bool = True) -> List[Dict[str, Any]]:
    """Her anahtar için durum + kullanım. Sıra, verilen listenin sırasıyla aynıdır."""
    keys = split_keys(keys) if keys is not None else get_keys()
    if not keys:
        return []
    usages = await asyncio.gather(*(_fetch_usage(k, use_cache) for k in keys))
    active = None
    items = []
    # önce durumları güncelle, sonra aktif anahtarı belirle
    for i, (k, u) in enumerate(zip(keys, usages)):
        items.append(_describe(i, k, u, None))
    now = time.time()
    for k in keys:
        if not _blocked(k, now):
            active = k
            break
    for it, k in zip(items, keys):
        it["active"] = (k == active)
    return items
