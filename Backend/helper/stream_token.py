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

  # FastAPI startup/shutdown'da çağır:
  await media_token_manager.start_cleanup_task()
  await media_token_manager.stop_cleanup_task()
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time

logger = logging.getLogger("stream_token")

# Periyodik temizlik aralığı (saniye). TTL'in yarısı makul bir değer —
# varsayılan 6 saatlik TTL ile store her 3 saatte bir tam temizlenir.
_CLEANUP_INTERVAL_SEC = int(3 * 3600)

# Store'un alabileceği maksimum kayıt sayısı. Bu eşiğe ulaşıldığında
# create() çağrısından önce bir zorla temizlik tetiklenir.
# 50 000 kayıt ≈ ~20 MB bellek (her entry ~400 byte).
_MAX_STORE_SIZE = 50_000


class MediaTokenManager:
    """
    Üyeye ve dosyaya özgü, süreli token yöneticisi.
    Video izleme ve indirme için aynı TTL kullanılır (YENILEME).

    create(member_token, file_id, kind)  → yeni token döner (kind: "video" | "indir")
    verify(token, member_token, file_id) → token + üye + dosya + TTL doğrular
    configure(ttl_raw)                   → YENILEME config değeriyle TTL ayarlar
    start_cleanup_task()                 → periyodik arka plan temizliğini başlatır
    stop_cleanup_task()                  → arka plan görevini durdurur
    """

    def __init__(self) -> None:
        # { token: {"member_token": str, "file_id": str, "kind": str, "expires_at": float} }
        self._store: dict[str, dict] = {}
        self._ttl_hours: int = 6  # varsayılan
        self._cleanup_task: asyncio.Task | None = None

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
        # Store boyutu sınırına ulaşıldıysa yeni kayıt eklemeden önce zorla temizle.
        if len(self._store) >= _MAX_STORE_SIZE:
            before = len(self._store)
            self._purge_expired()
            logger.warning(
                "Store boyut sınırına ulaşıldı (%d/%d) — zorla temizlik: %d kayıt silindi.",
                before, _MAX_STORE_SIZE, before - len(self._store),
            )

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

    def store_size(self) -> int:
        """Mevcut store boyutunu döner (izleme/debug için)."""
        return len(self._store)

    # ------------------------------------------------------------------
    # Periyodik arka plan temizliği
    # ------------------------------------------------------------------

    async def start_cleanup_task(self) -> None:
        """
        Periyodik cleanup task'ını başlatır.
        FastAPI startup event'inden çağrılmalıdır:

            @app.on_event("startup")
            async def startup():
                await media_token_manager.start_cleanup_task()
        """
        if self._cleanup_task and not self._cleanup_task.done():
            logger.debug("Cleanup task zaten çalışıyor, yeni görev başlatılmadı.")
            return

        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="stream_token_cleanup"
        )
        logger.info(
            "Stream token cleanup task başlatıldı (aralık: %d dk).",
            _CLEANUP_INTERVAL_SEC // 60,
        )

    async def stop_cleanup_task(self) -> None:
        """
        Arka plan görevini durdurur.
        FastAPI shutdown event'inden çağrılmalıdır:

            @app.on_event("shutdown")
            async def shutdown():
                await media_token_manager.stop_cleanup_task()
        """
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Stream token cleanup task durduruldu.")

    async def _cleanup_loop(self) -> None:
        """_CLEANUP_INTERVAL_SEC saniyede bir _purge_expired() çalıştırır."""
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL_SEC)
            try:
                before = len(self._store)
                self._purge_expired()
                removed = before - len(self._store)
                if removed:
                    logger.info(
                        "Periyodik temizlik: %d süresi dolmuş token silindi, "
                        "store boyutu: %d → %d.",
                        removed, before, len(self._store),
                    )
                else:
                    logger.debug(
                        "Periyodik temizlik: silinecek token yok, store boyutu: %d.",
                        len(self._store),
                    )
            except Exception:
                logger.exception("Periyodik token temizliğinde beklenmeyen hata.")

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
