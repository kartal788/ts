#!/bin/bash
# ============================================================
# db_backup.sh  –  15 dakikada bir MongoDB yedeği al
# ============================================================
# Kullanım: Docker içinde cron veya entrypoint üzerinden çalışır.
# Ortam değişkeni:  MONGO_URI  (DATABASE config'deki ilk URI)
# ============================================================

set -euo pipefail

BACKUP_DIR="/tmp/mongo_backup"      # Geçici yedek klasörü
LOCK_FILE="/tmp/db_backup.lock"     # Çakışmayı önler

# --- Kilit kontrolü ---
if [ -f "$LOCK_FILE" ]; then
    echo "[backup] Önceki yedek hâlâ çalışıyor, atlanıyor."
    exit 0
fi
touch "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

MONGO_URI="${MONGO_URI:-}"
if [ -z "$MONGO_URI" ]; then
    echo "[backup] HATA: MONGO_URI ortam değişkeni tanımlı değil."
    exit 1
fi

echo "[backup] $(date '+%Y-%m-%d %H:%M:%S')  –  Yedekleme başladı."

# Eski yedeği sil
rm -rf "$BACKUP_DIR"

# Yeni yedeği al  (dbFyvio veritabanı, tüm koleksiyonlar)
mongodump \
    --uri="$MONGO_URI" \
    --db=dbFyvio \
    --out="$BACKUP_DIR" \
    --quiet

echo "[backup] Yedekleme tamamlandı → $BACKUP_DIR"

# Platform kataloğunu yenile (FastAPI servisine sinyal gönder)
# BASE_URL ortam değişkeni tanımlıysa HTTP hook'u tetikle
if [ -n "${BASE_URL:-}" ]; then
    curl -sf "${BASE_URL}/internal/platform-catalog/refresh" \
         -o /dev/null \
         --max-time 10 \
         || echo "[backup] Platform kataloğu yenileme isteği başarısız (servis henüz hazır olmayabilir)."
fi

echo "[backup] Bitti."
