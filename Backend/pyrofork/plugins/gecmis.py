"""
/gecmis komutu — Üye botta "/gecmis" yazınca kendi izleme geçmişini indirir.
Her satırda: tarih, saat, video adı ve o izlemede kullanılan veri miktarı
(GB) yer alır. Dosyanın sonunda ayrıca gün gün kullanım özeti, toplam
kullanım, günlük ortalama kullanım ve video bazlı (tarih + video adı +
o gün o video için kullanılan toplam veri) özet bilgisi eklenir; sonuç
bir .txt dosyası olarak üyeye gönderilir.
"""
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pymongo import DESCENDING

from Backend import db

_TZ = ZoneInfo("Europe/Istanbul")


def _fmt_gb(v: float) -> str:
    if v is None:
        return "0 MB"
    if v < 0.001:
        return "0 MB"
    if v < 1:
        return f"{v * 1024:.0f} MB"
    if v >= 1000:
        return f"{v / 1024:.2f} TB"
    return f"{v:.2f} GB"


@Client.on_message(filters.command("gecmis") & filters.private)
async def gecmis_command(client: Client, message: Message):
    """Üyenin izleme geçmişini (tarih, saat, video, kullanım) .txt olarak gönderir."""
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    # ── Aktif abonelik kontrolü ────────────────────────────────────────────
    user = await db.get_user(user_id)
    if not user or user.get("subscription_status") != "active":
        await message.reply_text(
            "❌ <b>Aktif aboneliğiniz bulunmuyor.</b>\n"
            "İzleme geçmişinizi görüntüleyebilmek için aktif aboneliğiniz olması gerekir.",
            parse_mode=enums.ParseMode.HTML,
            quote=True,
        )
        return

    wait_msg = await message.reply_text(
        "⏳ İzleme geçmişiniz hazırlanıyor…",
        quote=True,
    )

    try:
        # Telegram user_id → API token eşleştirmesi (member_routes.py ile aynı mantık)
        all_tokens = await db.get_all_api_tokens()
        token_doc  = next((t for t in all_tokens if t.get("user_id") == user_id), None)
        user_token = token_doc.get("token") if token_doc else None

        if not user_token:
            await wait_msg.edit_text("ℹ️ Henüz herhangi bir izleme kaydınız bulunmuyor.")
            return

        col = db.dbs["tracking"]["stream_analytics"]
        cursor = col.find(
            {"user_token": user_token},
            {"_id": 0, "title": 1, "imdb_id": 1, "total_bytes": 1, "logged_at": 1},
        ).sort("logged_at", DESCENDING).limit(2000)
        rows = await cursor.to_list(None)

        if not rows:
            await wait_msg.edit_text("ℹ️ Henüz herhangi bir izleme kaydınız bulunmuyor.")
            return

        # ── .txt içeriğini oluştur ──────────────────────────────────────────
        display_name = user.get("first_name") or user.get("username") or str(user_id)
        lines = [
            f"İzleme Geçmişi — {display_name} (ID: {user_id})",
            f"Oluşturulma: {datetime.now(_TZ).strftime('%d.%m.%Y %H:%M:%S')}",
            f"Toplam Kayıt: {len(rows)}",
            "-" * 60,
        ]

        total_bytes = 0
        daily_bytes: dict = {}          # {"dd.mm.yyyy": bytes_sum}
        video_daily_bytes: dict = {}    # {("dd.mm.yyyy", title): bytes_sum}
        for r in rows:
            logged = r.get("logged_at")
            if isinstance(logged, datetime):
                if logged.tzinfo is None:
                    logged = logged.replace(tzinfo=timezone.utc)
                logged_local = logged.astimezone(_TZ)
                tarih = logged_local.strftime("%d.%m.%Y")
                saat  = logged_local.strftime("%H:%M:%S")
            else:
                tarih, saat = "—", "—"

            title = r.get("title") or r.get("imdb_id") or "İsimsiz İçerik"
            b     = r.get("total_bytes", 0) or 0
            total_bytes += b
            if tarih != "—":
                daily_bytes[tarih] = daily_bytes.get(tarih, 0) + b
                key = (tarih, title)
                video_daily_bytes[key] = video_daily_bytes.get(key, 0) + b
            gb = b / (1024 ** 3)
            lines.append(f"[{tarih} {saat}]  {title}  —  {_fmt_gb(gb)}")

        # ── Gün gün kullanım özeti ────────────────────────────────────────────
        lines.append("-" * 60)
        lines.append("Günlük Kullanım:")
        # Tarihleri en yeniden en eskiye sırala (dd.mm.yyyy -> sıralanabilir tarih)
        sorted_days = sorted(
            daily_bytes.keys(),
            key=lambda d: datetime.strptime(d, "%d.%m.%Y"),
            reverse=True,
        )
        for day in sorted_days:
            day_gb = daily_bytes[day] / (1024 ** 3)
            lines.append(f"{day}  —  {_fmt_gb(day_gb)}")

        lines.append("-" * 60)
        lines.append(f"Toplam Kullanım: {_fmt_gb(total_bytes / (1024 ** 3))}")
        gun_sayisi = len(daily_bytes) or 1
        ortalama_gb = (total_bytes / (1024 ** 3)) / gun_sayisi
        lines.append(f"Günlük Ortalama Kullanım: {_fmt_gb(ortalama_gb)}")

        # ── Video bazlı kullanım özeti (tarih + video + gün toplamı) ─────────
        lines.append("-" * 60)
        lines.append("Video Bazlı Kullanım:")
        sorted_video_keys = sorted(
            video_daily_bytes.keys(),
            key=lambda k: (datetime.strptime(k[0], "%d.%m.%Y"), video_daily_bytes[k]),
            reverse=True,
        )
        for (day, title) in sorted_video_keys:
            vb = video_daily_bytes[(day, title)] / (1024 ** 3)
            lines.append(f"{day} {title} {_fmt_gb(vb)}")

        content  = "\n".join(lines)
        txt_file = io.BytesIO(content.encode("utf-8"))
        filename = f"izleme_gecmisi_{user_id}.txt"
        txt_file.name = filename

        await wait_msg.delete()
        await message.reply_document(
            document=txt_file,
            file_name=filename,
            caption=(
                "📄 <b>İzleme Geçmişiniz</b>\n"
                "Tarih, saat ve kullanım miktarına göre listelenmiştir."
            ),
            parse_mode=enums.ParseMode.HTML,
            quote=True,
        )

    except Exception as e:
        try:
            await wait_msg.edit_text(f"⚠️ Bir hata oluştu: <code>{str(e)[:200]}</code>", parse_mode=enums.ParseMode.HTML)
        except Exception:
            await message.reply_text(f"⚠️ Bir hata oluştu: <code>{str(e)[:200]}</code>", parse_mode=enums.ParseMode.HTML)
