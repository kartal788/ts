"""
encrypt.py
==========
Stream ID'leri için güvenli encode/decode.

Versiyon 2: HMAC-SHA256 imzası eklendi.
  - Eski format (imzasız base62): geriye dönük uyumluluk için decode edilebilir,
    ancak local_path içeriyorsa reddedilir.
  - Yeni format: <imza_16hex>.<base62_payload>
    İmza TOKEN_HMAC_SECRET (yoksa SESSION_SECRET_KEY) env değişkeninden türetilir.
    Her iki değişken de tanımlı değilse uygulama başlangıcında RuntimeError fırlatılır.

Kullanım:
  from Backend.helper.encrypt import encode_string, decode_string
"""

import os
import zlib
import json
import hmac
import hashlib
import asyncio
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor()

# ── HMAC anahtarı ─────────────────────────────────────────────────────────────
# TOKEN_HMAC_SECRET zorunludur; yoksa uygulama başlangıcında RuntimeError fırlatılır
# (main.py'deki SESSION_SECRET_KEY kontrolüyle aynı desen).
def _get_hmac_key() -> bytes:
    key = (
        os.getenv("TOKEN_HMAC_SECRET", "")
        or os.getenv("SESSION_SECRET_KEY", "")
    )
    if not key:
        raise RuntimeError(
            "\n\n"
            "KRİTİK GÜVENLİK HATASI — BOT DURDU\n"
            "TOKEN_HMAC_SECRET config.env'de tanımlı değil!\n\n"
            "Bu key olmadan stream token'ları imzasız (güvensiz) çalışır;\n"
            "token manipülasyonu tespit edilemez.\n\n"
            "Çözüm — config.env dosyasına şu satırı ekle:\n"
            "  TOKEN_HMAC_SECRET=\"<güçlü-rastgele-değer>\"\n\n"
            "Güvenli bir key üretmek için terminalde şunu çalıştır:\n"
            "  python3 -c \"import secrets; print(secrets.token_hex(32))\"\n"
        )
    return key.encode()

_SEPARATOR = "."   # imza.payload ayracı — base62 alfabesinde yok


# ── Düşük seviye işlevler ─────────────────────────────────────────────────────

def compress_data(data: str) -> bytes:
    return zlib.compress(data.encode(), level=zlib.Z_BEST_COMPRESSION)

def decompress_data(data: bytes) -> str:
    return zlib.decompress(data).decode()

def base62_encode(data: bytes) -> str:
    BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    num = int.from_bytes(data, "big")
    result = []
    while num:
        num, rem = divmod(num, 62)
        result.append(BASE62[rem])
    return "".join(reversed(result)) or "0"

def base62_decode(data: str) -> bytes:
    BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    num = 0
    for char in data:
        num = num * 62 + BASE62.index(char)
    return num.to_bytes((num.bit_length() + 7) // 8, "big") or b"\x00"


def _sign(payload: str) -> str:
    """16 hex karakterlik HMAC-SHA256 imzası döner."""
    return hmac.new(_get_hmac_key(), payload.encode(), hashlib.sha256).hexdigest()[:16]

def _verify_sig(sig: str, payload: str) -> bool:
    """Sabit zamanlı karşılaştırma — timing attack'e karşı."""
    expected = _sign(payload)
    return hmac.compare_digest(sig, expected)


# ── Senkron encode/decode ──────────────────────────────────────────────────────

def _encode_sync(data: dict) -> str:
    """dict → imzalı token string."""
    json_data    = json.dumps(data, separators=(",", ":"))
    compressed   = compress_data(json_data)
    payload      = base62_encode(compressed)
    sig          = _sign(payload)
    return f"{sig}{_SEPARATOR}{payload}"


def _decode_sync(encoded: str) -> dict:
    """
    İmzalı veya imzasız (eski) token → dict.

    Eski format (nokta içermiyor): imzasız, geriye uyumluluk için decode edilir
    ANCAK local_path içeriyorsa güvenlik gerekçesiyle reddedilir.

    Yeni format (<sig>.<payload>): imza doğrulanır, başarısızsa ValueError.
    """
    if _SEPARATOR in encoded:
        # ── Yeni format: imzalı ───────────────────────────────────────────
        sig, payload = encoded.split(_SEPARATOR, 1)
        if not _verify_sig(sig, payload):
            raise ValueError("Geçersiz token imzası — manipülasyon tespit edildi.")
        compressed = base62_decode(payload)
    else:
        # ── Eski format: imzasız (geriye uyumluluk) ───────────────────────
        compressed = base62_decode(encoded)
        data = json.loads(decompress_data(compressed))
        # İmzasız token'larda local_path kesinlikle kabul edilmez
        if "local_path" in data:
            raise ValueError(
                "İmzasız token local_path içeriyor — güvenlik gerekçesiyle reddedildi."
            )
        return data

    return json.loads(decompress_data(compressed))


# ── Async yardımcılar (geriye uyumluluk) ──────────────────────────────────────

async def async_compress_data(data: str) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, compress_data, data)

async def async_decompress_data(data: bytes) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, decompress_data, data)

async def async_base62_encode(data: bytes) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, base62_encode, data)

async def async_base62_decode(data: str) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, base62_decode, data)


# ── Ana API ───────────────────────────────────────────────────────────────────

async def encode_string(data: dict) -> str:
    """dict → imzalı token. Async executor'da çalışır."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _encode_sync, data)


async def decode_string(encoded: str) -> dict:
    """İmzalı veya eski imzasız token → dict. Async executor'da çalışır."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _decode_sync, encoded)
