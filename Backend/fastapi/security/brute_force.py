"""
brute_force.py
==============
IP tabanlı brute-force (kaba kuvvet) saldırı koruması.

Kural:
  - _LOGIN_WINDOW saniye içinde _LOGIN_MAX kez başarısız giriş yapılırsa,
    o IP _BAN_DURATION saniye boyunca engellenir.
  - Engellenen IP adresi ve zamanı log dosyasına VE MongoDB'ye yazılır.
  - Bot yeniden başlatılsa bile ban kayıtları kalıcıdır (DB tabanlı).
  - Bellek tabanlı cache, her istekte DB sorgusu yapmayı önler.
  - _LOGIN_WINDOW, _LOGIN_MAX ve _BAN_DURATION değerleri config.env üzerinden
    ayarlanabilir:
      BRUTE_WINDOW     = saniye cinsinden pencere süresi     (varsayılan: 60)
      BRUTE_MAX        = maksimum başarısız deneme sayısı    (varsayılan: 10)
      BRUTE_BAN        = saniye cinsinden ban süresi         (varsayılan: 600)
"""

from __future__ import annotations

import asyncio
import logging
import time
import ipaddress
from collections import defaultdict
from os import getenv
from pathlib import Path

# ── Ayarlar (config.env'den okunur, yoksa varsayılan) ────────────────────────
_LOGIN_WINDOW: int = int(getenv("BRUTE_WINDOW", "60"))
_LOGIN_MAX:    int = int(getenv("BRUTE_MAX",    "10"))
_BAN_DURATION: int = int(getenv("BRUTE_BAN",   "600"))

# ── Log dosyası ───────────────────────────────────────────────────────────────
_LOG_PATH = Path("logs/brute_force.log")
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

_logger = logging.getLogger("brute_force")
if not _logger.handlers:
    _handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.WARNING)

# ── Bellek tabanlı durum tabloları (hızlı erişim için cache) ─────────────────
_attempts: dict[str, list[float]] = defaultdict(list)
_bans:     dict[str, float]       = {}          # ip → ban_bitiş unix timestamp


def _db():
    """DB nesnesini lazy import et (circular import önleme)."""
    from Backend import db as _backend_db
    return _backend_db


# ─────────────────────────────────────────────────────────────────────────────

async def is_banned_async(ip: str) -> bool:
    """
    Async context için tam DB destekli ban kontrolü.
    member_routes ve template_routes bu versiyonu çağırmalıdır.
    """
    now = time.time()

    # 1. Bellek cache
    ban_until = _bans.get(ip)
    if ban_until is not None:
        if now < ban_until:
            return True
        del _bans[ip]
        _attempts.pop(ip, None)
        try:
            await _db().delete_ip_ban(ip)
        except Exception:
            pass
        return False

    # 2. DB'den kontrol
    try:
        db_ban_until = await _db().get_ip_ban(ip)
        if db_ban_until and now < db_ban_until:
            _bans[ip] = db_ban_until   # cache'e yükle
            return True
        elif db_ban_until:
            await _db().delete_ip_ban(ip)
    except Exception as e:
        _logger.warning("DB ban kontrolü başarısız (bellek cache kullanılıyor): %s", e)

    return False


def is_banned(ip: str) -> bool:
    """
    Sync uyumluluk wrapper'ı — sadece bellek cache'ini kontrol eder.
    Async context'te is_banned_async kullanılmalıdır.
    """
    now = time.time()
    ban_until = _bans.get(ip)
    if ban_until is None:
        return False
    if now < ban_until:
        return True
    del _bans[ip]
    _attempts.pop(ip, None)
    return False


def ban_remaining(ip: str) -> int:
    """Kalan ban süresi (saniye). Banlı değilse 0 döner."""
    ban_until = _bans.get(ip)
    if ban_until is None:
        return 0
    remaining = int(ban_until - time.time())
    return max(remaining, 0)


async def record_failure_async(ip: str, endpoint: str = "") -> bool:
    """
    Başarısız girişi kaydet (async — DB destekli).
    member_routes bu versiyonu çağırmalıdır.
    """
    now = time.monotonic()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < _LOGIN_WINDOW]
    _attempts[ip].append(now)

    if len(_attempts[ip]) >= _LOGIN_MAX:
        ban_until_wall = time.time() + _BAN_DURATION
        _bans[ip] = ban_until_wall
        _attempts.pop(ip, None)
        _logger.warning(
            "BAN  ip=%-20s  endpoint=%-30s  ban_duration=%ds",
            ip, endpoint or "-", _BAN_DURATION
        )
        # DB'ye kalıcı olarak yaz
        try:
            await _db().set_ip_ban(ip, ban_until_wall)
        except Exception as e:
            _logger.warning("DB ban kaydı yazılamadı (bellek ban aktif): %s", e)
        return True

    return False


def record_failure(ip: str, endpoint: str = "") -> bool:
    """
    Sync uyumluluk wrapper'ı — bellek tabanlı.
    Async context'te record_failure_async kullanılmalıdır.
    """
    now = time.monotonic()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < _LOGIN_WINDOW]
    _attempts[ip].append(now)

    if len(_attempts[ip]) >= _LOGIN_MAX:
        ban_until_wall = time.time() + _BAN_DURATION
        _bans[ip] = ban_until_wall
        _attempts.pop(ip, None)
        _logger.warning(
            "BAN  ip=%-20s  endpoint=%-30s  ban_duration=%ds",
            ip, endpoint or "-", _BAN_DURATION
        )
        # DB'ye fire-and-forget ile yaz
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_persist_ban(ip, ban_until_wall))
        except RuntimeError:
            pass
        return True

    return False


async def _persist_ban(ip: str, ban_until: float) -> None:
    """Ban kaydını DB'ye yaz (arka plan görevi)."""
    try:
        await _db().set_ip_ban(ip, ban_until)
    except Exception as e:
        _logger.warning("DB ban kaydı yazılamadı: %s", e)


def record_success(ip: str) -> None:
    """Başarılı girişte o IP'nin başarısız sayacını sıfırla."""
    _attempts.pop(ip, None)


async def cleanup_expired_bans() -> None:
    """Periyodik görev: süresi dolmuş DB ban kayıtlarını temizle."""
    try:
        count = await _db().cleanup_expired_ip_bans()
        if count:
            _logger.info("Süresi dolmuş %d IP ban kaydı temizlendi.", count)
    except Exception as e:
        _logger.warning("IP ban cleanup başarısız: %s", e)


# ── Güvenilir proxy IP aralıkları ────────────────────────────────────────────
_raw_trusted = getenv("TRUSTED_PROXY_CIDRS", "").strip()
_TRUSTED_PROXIES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
if _raw_trusted:
    for _cidr in _raw_trusted.split(","):
        _cidr = _cidr.strip()
        if _cidr:
            try:
                _TRUSTED_PROXIES.append(ipaddress.ip_network(_cidr, strict=False))
            except ValueError:
                logging.getLogger("brute_force").warning(
                    f"[brute_force] Geçersiz TRUSTED_PROXY_CIDRS değeri: {_cidr!r} — atlandı"
                )


def _is_trusted_proxy(ip: str) -> bool:
    if not _TRUSTED_PROXIES:
        return False
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _TRUSTED_PROXIES)
    except ValueError:
        return False


def get_client_ip(request) -> str:
    """
    FastAPI Request nesnesinden gerçek IP'yi güvenli şekilde okur.
    X-Forwarded-For yalnızca güvenilir proxy'lerden gelen isteklerde
    dikkate alınır; saldırgan sahte header yazarak ban'ı atlayamaz.
    """
    direct_ip = request.client.host if request.client else "unknown"

    if _is_trusted_proxy(direct_ip):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
            try:
                ipaddress.ip_address(client_ip)
                return client_ip
            except ValueError:
                pass

    return direct_ip
