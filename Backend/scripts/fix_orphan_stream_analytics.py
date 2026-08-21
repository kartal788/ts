"""
fix_orphan_stream_analytics.py
===============================
Bir üyenin token'ı / aboneliği silindiğinde, o token'a ait stream_analytics
kayıtları önceden temizlenmiyordu. Bu da dashboard'daki "Uyarılar" kartında
artık var olmayan (silinmiş) üyeler için sahte bir "GB Tutarsızlığı"
(yetim token) uyarısı olarak görünmeye devam etmesine sebep oluyordu
(örn. aboneliği silinen "KARTAL" isimli üye).

Kod düzeltmesi (revoke_token_api artık token silindiğinde ilgili
stream_analytics kayıtlarını da temizliyor) SADECE bundan sonra silinecek
tokenlar için geçerlidir. Bu betik, düzeltmeden ÖNCE silinmiş olan tokenlara
ait "yetim" stream_analytics kayıtlarını tek seferlik olarak temizler.

Çalıştırma:
    python -m Backend.scripts.fix_orphan_stream_analytics          # dry-run (sadece rapor)
    python -m Backend.scripts.fix_orphan_stream_analytics --apply  # gerçekten siler
"""

from __future__ import annotations

import asyncio
import sys

from Backend import db
from Backend.logger import LOGGER


async def _run(apply: bool) -> None:
    tokens = await db.dbs["tracking"]["api_tokens"].find({}, {"token": 1}).to_list(None)
    valid_tokens = {t.get("token") for t in tokens if t.get("token")}

    col = db.dbs["tracking"]["stream_analytics"]
    distinct_tokens = await col.distinct("user_token", {"user_token": {"$ne": None}})

    orphan_tokens = [t for t in distinct_tokens if t and t not in valid_tokens]

    if not orphan_tokens:
        print("Yetim (orphan) stream_analytics kaydı bulunamadı. Temizlenecek bir şey yok.")
        return

    total_deleted = 0
    for tok in orphan_tokens:
        count = await col.count_documents({"user_token": tok})
        print(f"  Token …{tok[-6:]}: {count} kayıt" + (" (siliniyor)" if apply else " (dry-run)"))
        if apply:
            result = await col.delete_many({"user_token": tok})
            total_deleted += result.deleted_count

    if apply:
        print(f"\nToplam {total_deleted} yetim stream_analytics kaydı silindi ({len(orphan_tokens)} token).")
    else:
        print(f"\n{len(orphan_tokens)} yetim token bulundu. Gerçekten silmek için --apply ile çalıştırın.")


if __name__ == "__main__":
    apply_flag = "--apply" in sys.argv
    try:
        asyncio.run(_run(apply_flag))
    except Exception as e:
        LOGGER.error(f"fix_orphan_stream_analytics error: {e}")
        raise
