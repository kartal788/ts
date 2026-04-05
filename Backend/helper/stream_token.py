"""
stream_token.py
================
Stream URL'leri için token yöneticisi.

Her iki kullanım (Stremio video izleme + üye indirme) aynı sistemle çalışır:
  - Her token üyeye (member_token) + dosyaya (file_id) özgüdür.
  - YENILEME saat sonra geçersiz olur (varsayılan 6 saat).
  - Video: Stremio yeni stream isteği geldiğinde (sayfa yenilenince) token otomatik yenilenir.
  - İndirme: Tekrar indirmek için tekrar İndir butonuna basılması gerekir.

Kullanım:
  from Backend.helper.stream_token import media_token_manager
  tok = media_token_manager.create(member_token, file_id, kind="video")
  tok = media_token_manager.create(member_token, file_id, kind="indir")
  ok  = media_token_manager.verify(tok, member_token, file_id)
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time

logger = logging.getLogger("stream_token")


class MediaTokenManager:
    """
    Üyeye ve dosyaya özgü, süreli token yöneticisi.
    Video izleme ve indirme için aynı TTL kullanılır (YENILEME).

    create(member_token, file_id, kind)  → yeni token döner (kind: "video" | "indir")
    verify(token, member_token, file_id) → token + üye + dosya + TTL doğrular
    configure(ttl_raw)                   → YENILEME config değeriyle TTL ayarlar
    """

    def __init__(self) -> None:
        # { token: {"member_token": str, "file_id": str, "kind": str, "expires_at": float} }
        self._store: dict[str, dict] = {}
        self._ttl_hours: int = 6  # varsayılan

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def configure(self, ttl_raw: str) -> None:
        """config.env'deki YENILEME değeriyle TTL'i ayarla. Boş → 6 saat."""
        raw = (ttl_raw or "").strip()
        self._ttl_hours = int(raw) if raw.isdigit() and int(raw) > 0 else 6
        logger.info("Token TTL %d saat olarak ayarlandı.", self._ttl_hours)

    def create(self, member_token: str, file_id: str, kind: str = "video") -> str:
        """
        Token üret veya mevcut geçerli tokeni döndür.
        Aynı (member_token, file_id, kind) için geçerli token varsa onu döndürür (revoke etmez).
        Süresi dolmuş veya hiç yoksa yeni token üretir.
        """
        # Mevcut geçerli token varsa döndür — Stremio paralel istek sorununu önler
        now = time.monotonic()
        for tok, entry in self._store.items():
            if (
                entry["member_token"] == member_token
                and entry["file_id"] == file_id
                and entry["kind"] == kind
                and entry["expires_at"] > now
            ):
                logger.debug(
                    "Mevcut token yeniden kullanıldı — kind=%s member=%s",
                    kind, member_token[:8],
                )
                return tok

        # Süresi dolmuş eski tokenları temizle
        self._revoke_existing(member_token, file_id, kind)
        token = self._generate()
        self._store[token] = {
            "member_token": member_token,
            "file_id":      file_id,
            "kind":         kind,
            "expires_at":   time.monotonic() + self._ttl_hours * 3600,
        }
        logger.debug(
            "Token oluşturuldu — kind=%s member=%s file_id=%s TTL=%dh",
            kind, member_token[:8], file_id[:16], self._ttl_hours,
        )
        self._purge_expired()
        return token

    def verify(self, token: str, member_token: str, file_id: str) -> bool:
        """Token geçerli mi? (değer + üye eşleşmesi + dosya eşleşmesi + TTL)"""
        entry = self._store.get(token)
        if not entry:
            return False
        if entry["member_token"] != member_token:
            return False
        if entry["file_id"] != file_id:
            return False
        if time.monotonic() > entry["expires_at"]:
            del self._store[token]
            return False
        return True

    def ttl_hours(self) -> int:
        return self._ttl_hours

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _generate() -> str:
        raw = secrets.token_bytes(16)
        return hashlib.blake2s(raw, digest_size=10).hexdigest()  # 20 hex karakter

    def _revoke_existing(self, member_token: str, file_id: str, kind: str) -> None:
        to_del = [
            t for t, e in self._store.items()
            if e["member_token"] == member_token
            and e["file_id"] == file_id
            and e["kind"] == kind
        ]
        for t in to_del:
            del self._store[t]

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [t for t, e in self._store.items() if now > e["expires_at"]]
        for t in expired:
            del self._store[t]


# Singleton
media_token_manager = MediaTokenManager()
