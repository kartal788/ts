"""
csrf.py
=======
Double-Submit Cookie + HMAC imzalı CSRF koruması.

Çalışma prensibi:
  1. Oturum açan her kullanıcı için session'a bir CSRF raw-secret kaydedilir.
  2. Bu secret, SESSION_SECRET_KEY ile HMAC-SHA256'lanarak imzalı token üretilir.
  3. Token /api/csrf-token endpoint'i ile tarayıcıya döner (JS ile okunabilir).
  4. JS, state-değiştiren her fetch isteğine X-CSRF-Token başlığı ekler.
  5. CSRFMiddleware, korunan endpoint'lerde bu başlığı doğrular.

Neden bu yöntem?
  - Tarayıcı Same-Origin Policy sayesinde başka bir domain'deki JS,
    X-CSRF-Token başlığını okuyamaz/ekleyemez.
  - HMAC imzası sayesinde session'a doğrudan erişim olmadan token üretilemez.
  - HttpOnly olmayan (JS'den okunabilir) cookie yerine header tabanlı yaklaşım
    kullanıldığından XSS etkisi minimize edilir (mevcut XSS senaryosunda
    herhangi bir yaklaşım kırılgandır; bu en yaygın kabul görmüş pratiktir).

Korunan scope (CSRF_PROTECTED_PREFIXES):
  - /api/admin/*, /api/media/*, /api/tokens*, /api/sunucu/*, /api/istatistik/*,
    /api/duyuru/*, /api/system/*, /api/link-ekle/*, /api/subtitles/*,
    /api/uye/hatirla, /api/uye/film-hatirla  ← state-değiştiren üye uç noktaları
  - GET ve HEAD istekleri muaf tutulur (idempotent).
  - /login POST muaftır (oturum yokken token alınamaz; captcha + brute-force
    koruması yeterlidir).
  - /uye/giris POST muaftır (aynı gerekçe).
  - Stremio/dl/subtitles public path'leri muaftır.
"""

from __future__ import annotations

import hmac
import hashlib
import secrets
import os
import logging
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_logger = logging.getLogger("csrf")

# ── HMAC anahtarı ─────────────────────────────────────────────────────────────
def _get_hmac_key() -> bytes:
    key = (
        os.getenv("SESSION_SECRET_KEY", "")
        or os.getenv("TOKEN_HMAC_SECRET", "")
    )
    if not key:
        raise RuntimeError("SESSION_SECRET_KEY tanımlı değil — CSRF koruması başlatılamadı.")
    return key.encode()


# ── Token üretimi & doğrulama ─────────────────────────────────────────────────

def generate_csrf_secret() -> str:
    """Oturum başına bir kez üretilen 32-byte hex raw-secret."""
    return secrets.token_hex(32)


def _sign_secret(secret: str) -> str:
    """secret → HMAC-SHA256 imzalı token (64 hex karakter)."""
    return hmac.new(_get_hmac_key(), secret.encode(), hashlib.sha256).hexdigest()


def get_csrf_token(request: Request) -> Optional[str]:
    """
    Session'daki raw-secret'ı HMAC ile imzalayarak istemciye gönderilecek
    token'ı döner.  Session yoksa None.
    """
    secret = request.session.get("csrf_secret")
    if not secret:
        return None
    return _sign_secret(secret)


def ensure_csrf_secret(request: Request) -> str:
    """
    Session'da csrf_secret yoksa üretir ve kaydeder; varsa olduğu gibi bırakır.
    Login sonrasında çağrılmalıdır.
    Dönen değer: imzalı token (istemciye gönderilecek).
    """
    if "csrf_secret" not in request.session:
        request.session["csrf_secret"] = generate_csrf_secret()
    return _sign_secret(request.session["csrf_secret"])


def verify_csrf_token(request: Request) -> bool:
    """
    İstekteki X-CSRF-Token başlığını session'daki secret ile doğrular.
    Sabit-zamanlı karşılaştırma (timing-safe).
    """
    secret = request.session.get("csrf_secret")
    if not secret:
        return False
    header_token = request.headers.get("X-CSRF-Token", "")
    if not header_token:
        return False
    expected = _sign_secret(secret)
    return hmac.compare_digest(header_token, expected)


# ── Korunan path prefix'leri ──────────────────────────────────────────────────

# Bu prefix'lerle başlayan ve state-değiştiren (POST/PUT/DELETE/PATCH)
# istekler CSRF doğrulamasına tabi tutulur.
CSRF_PROTECTED_PREFIXES = (
    "/api/admin/",
    "/api/media/",
    "/api/tokens",   # /api/tokens ve /api/tokens/...
    "/api/sunucu/",
    "/api/istatistik/",
    "/api/duyuru/",
    "/api/system/",
    "/api/link-ekle/",
    "/api/subtitles/",
    "/api/uye/hatirla",       # /api/uye/hatirla ve /api/uye/hatirla/...
    "/api/uye/film-hatirla",  # /api/uye/film-hatirla ve /api/uye/film-hatirla/...
    "/set-theme",
    # DÜZELTME: yayin_routes.py'deki /api/yayin (POST), /api/yayin/{id}
    # (PUT/DELETE), /api/yayin/{id}/start ve /api/yayin/{id}/stop
    # session-cookie tabanlı require_auth ile korunuyordu ama bu prefix
    # listesinde YOKTU — yani CSRF middleware'i bu path'leri "korunmuyor"
    # sayıp doğrudan geçiriyordu. start/stop endpoint'leri body gerektirmediği
    # için basit bir <form method="POST"> ile CORS preflight'a takılmadan
    # tetiklenebiliyordu (test ile doğrulandı). "/api/yayin" hem tam eşleşen
    # hem "/api/yayin/..." alt path'lerini de startswith ile kapsar.
    "/api/yayin",
)

# Tamamen muaf tutulan path'ler (CSRF token henüz mevcut değil veya public)
CSRF_EXEMPT_PATHS = {
    "/login",        # captcha + brute-force koruması var; token henüz yok
    "/uye/giris",    # aynı gerekçe
    "/uye/cikis",    # GET — zaten muaf
    "/logout",       # GET — zaten muaf
}

# Public prefix'ler — CSRF kontrolü hiç uygulanmaz
CSRF_PUBLIC_PREFIXES = ("/stremio/", "/dl/", "/subtitles/", "/stream/")

# Muaf HTTP metodları (idempotent)
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


# ── Middleware ────────────────────────────────────────────────────────────────

class CSRFMiddleware(BaseHTTPMiddleware):
    """
    State-değiştiren admin/API endpoint'lerinde X-CSRF-Token başlığını doğrular.
    Başlık eksik veya geçersizse 403 döner.
    """

    async def dispatch(self, request: Request, call_next):
        path   = request.url.path
        method = request.method

        # 1. Idempotent metodlar → geç
        if method in CSRF_SAFE_METHODS:
            return await call_next(request)

        # 2. Public path'ler → geç
        if any(path.startswith(p) for p in CSRF_PUBLIC_PREFIXES):
            return await call_next(request)

        # 3. Muaf path'ler → geç
        if path in CSRF_EXEMPT_PATHS:
            return await call_next(request)

        # 4. Korunan prefix mi?
        is_protected = any(path.startswith(p) for p in CSRF_PROTECTED_PREFIXES)
        if not is_protected:
            return await call_next(request)

        # 5. Oturum yoksa CSRF'i atla (401 zaten auth katmanından gelir)
        if not request.session.get("authenticated") and not request.session.get("member"):
            return await call_next(request)

        # 6. Token doğrula
        if not verify_csrf_token(request):
            _logger.warning(
                "CSRF doğrulama başarısız — method=%s path=%s ip=%s",
                method, path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token geçersiz veya eksik. Sayfayı yenileyin."},
            )

        return await call_next(request)
