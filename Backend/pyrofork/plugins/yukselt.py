"""
/yukselt komutu — Yalnızca aktif aboneler görebilir.
Kullanıcı ek paket seçer → yöneticiye bildirim gider → onaylanırsa
günlük limit, aylık limit, hız limiti, istek limiti ve abonelik süresi artar.
"""

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from Backend.config import Telegram
from Backend import db
from datetime import datetime, timedelta

print("DEBUG: yukselt.py PLUGIN LOADED SUCCESSFULLY!")


# ─── Yardımcı: onaylayan ID listesi ──────────────────────────────────────────
def _approver_ids():
    return Telegram.APPROVER_IDS if Telegram.APPROVER_IDS else [Telegram.OWNER_ID]


# ─── /yukselt komutu ─────────────────────────────────────────────────────────
@Client.on_message(filters.command("yukselt") & filters.private)
async def yukselt_command(client: Client, message: Message):
    if not Telegram.SUBSCRIPTION:
        return

    user_id = (
        (message.from_user.id if message.from_user else None)
        or (message.sender_chat.id if message.sender_chat else None)
        or message.chat.id
    )

    # Ban kontrolü
    if await db.is_user_banned(user_id):
        return await message.reply_text(
            "🚫 <b>Hesabınız engellenmiştir.</b>",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )

    # Sadece aktif aboneler görebilir
    user = await db.get_user(user_id)
    now = datetime.utcnow()
    is_active = False
    if user and user.get("subscription_status") == "active":
        if user.get("subscription_expiry") and user["subscription_expiry"] > now:
            is_active = True
        else:
            await db.mark_user_expired(user_id)

    if not is_active:
        return await message.reply_text(
            "❌ <b>Bu komut yalnızca aktif aboneler içindir.</b>\n\n"
            "Abonelik satın almak için /start yazabilirsiniz.",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )

    # Ek paketleri çek
    packages = await db.get_addon_packages()
    if not packages:
        return await message.reply_text(
            "ℹ️ Şu anda tanımlı bir ek paket bulunmamaktadır. "
            "Daha sonra tekrar deneyin.",
            quote=True,
        )

    # Klavye oluştur
    keyboard_buttons = []
    for pkg in packages:
        label = pkg.get("label") or f"Ek Paket — {pkg.get('price', 0)} TL"
        price = pkg.get("price", 0)
        keyboard_buttons.append(
            [InlineKeyboardButton(f"{label} — {price} TL", callback_data=f"addon_{pkg['_id']}")]
        )

    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    caption = (
        f"<b>🚀 Aboneliğinizi Yükseltin</b>\n\n"
        "Aşağıdaki ek paketlerden birini seçerek günlük limit, aylık limit, "
        "hız limiti, istek limiti veya abonelik sürenizi artırabilirsiniz.\n\n"
        "💡 <i>Ek paket seçin:</i>"
    )

    upgrade_image_id = await db.get_upgrade_image()
    if upgrade_image_id:
        await message.reply_photo(
            photo=upgrade_image_id,
            caption=caption,
            reply_markup=keyboard,
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await message.reply_text(
            caption,
            reply_markup=keyboard,
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )


# ─── Callback: ek paket seçimi ────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^addon_([a-fA-F0-9]{24})$"))
async def addon_selection(client: Client, callback_query: CallbackQuery):
    if not Telegram.SUBSCRIPTION:
        return await callback_query.answer("Abonelikler etkinleştirilmedi.", show_alert=True)

    pkg_id = callback_query.matches[0].group(1)
    packages = await db.get_addon_packages()
    pkg = next((p for p in packages if p["_id"] == pkg_id), None)

    if not pkg:
        return await callback_query.answer("Geçersiz paket.", show_alert=True)

    user_id = callback_query.from_user.id if callback_query.from_user else callback_query.message.chat.id
    first_name = callback_query.from_user.first_name if callback_query.from_user else ""
    username = callback_query.from_user.username if callback_query.from_user else ""
    user_mention = callback_query.from_user.mention if callback_query.from_user else f"User {user_id}"
    username_str = f"@{username}" if username else "N/A"

    # Aktif abonelik kontrolü
    user = await db.get_user(user_id)
    now = datetime.utcnow()
    is_active = (
        user
        and user.get("subscription_status") == "active"
        and user.get("subscription_expiry")
        and user["subscription_expiry"] > now
    )
    if not is_active:
        return await callback_query.answer(
            "Bu özelliği kullanmak için aktif aboneliğiniz olmalıdır.",
            show_alert=True,
        )

    # Callback'i kapat
    await callback_query.answer(
        f"✅ '{pkg.get('label')}' seçildi. Yönetici onayı bekleniyor.",
        show_alert=True,
    )

    await db.update_user_interaction(user_id, first_name, username)

    # Pending addon kaydı oluştur (admin_messages henüz boş)
    await db.set_pending_addon(
        user_id=user_id,
        pkg_id=pkg_id,
        label=pkg.get("label", ""),
        price=pkg.get("price", 0),
        extra_days=pkg.get("extra_days", 0),
        extra_daily_gb=pkg.get("extra_daily_gb", 0),
        extra_monthly_gb=pkg.get("extra_monthly_gb", 0),
        extra_speed_mbps=pkg.get("extra_speed_mbps", 0),
        extra_requests=pkg.get("extra_requests", 0),
    )

    # Kullanıcıya bekleme mesajı
    detail_lines = []
    if pkg.get("extra_days"):
        detail_lines.append(f"📅 +{pkg['extra_days']} gün abonelik süresi")
    if pkg.get("extra_daily_gb"):
        detail_lines.append(f"📊 +{pkg['extra_daily_gb']} GB günlük limit")
    if pkg.get("extra_monthly_gb"):
        detail_lines.append(f"📦 +{pkg['extra_monthly_gb']} GB aylık limit")
    if pkg.get("extra_speed_mbps"):
        detail_lines.append(f"⚡ +{pkg['extra_speed_mbps']} Mbps hız")
    if pkg.get("extra_requests"):
        detail_lines.append(f"📩 +{pkg['extra_requests']} aylık istek hakkı")
    details_text = "\n".join(detail_lines) if detail_lines else ""

    wait_text = (
        f"⏳ <b>Ek Paket Talebiniz Alındı</b>\n\n"
        f"📦 <b>Paket:</b> {pkg.get('label')} — {pkg.get('price', 0)} TL\n"
        f"{details_text}\n\n"
        "Talebiniz yöneticiye iletildi. Onaylandığında bilgilendirileceksiniz."
    )

    try:
        await callback_query.message.edit_caption(
            wait_text,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        try:
            await callback_query.message.edit_text(
                wait_text,
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            try:
                await callback_query.message.reply_text(
                    wait_text,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception as e:
                print(f"yukselt: could not send wait message to {user_id}: {e}")

    # Admin bildirimi
    admin_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Onayla", callback_data=f"addon_approve_{user_id}"),
            InlineKeyboardButton("❌ Reddet", callback_data=f"addon_reject_{user_id}"),
        ]
    ])

    admin_text = (
        f"<b>📦 Yeni Ek Paket Talebi</b>\n\n"
        f"<b>👤 Kullanıcı:</b> {user_mention}\n"
        f"<b>🆔 Kullanıcı ID:</b> <code>{user_id}</code>\n"
        f"<b>🔗 Kullanıcı Adı:</b> {username_str}\n\n"
        f"<b>📦 Paket:</b> {pkg.get('label')} — {pkg.get('price', 0)} TL\n"
        f"{details_text}\n\n"
        "Lütfen talebi onaylayın veya reddedin."
    )

    admin_messages = []
    for approver_id in _approver_ids():
        try:
            sent = await client.send_message(
                approver_id,
                admin_text,
                reply_markup=admin_keyboard,
                parse_mode=enums.ParseMode.HTML,
            )
            admin_messages.append({"chat_id": approver_id, "message_id": sent.id})
        except Exception as e:
            print(f"yukselt: failed to notify approver {approver_id}: {e}")

    # Admin mesaj ID'lerini kaydet
    await db.set_pending_addon(
        user_id=user_id,
        pkg_id=pkg_id,
        label=pkg.get("label", ""),
        price=pkg.get("price", 0),
        extra_days=pkg.get("extra_days", 0),
        extra_daily_gb=pkg.get("extra_daily_gb", 0),
        extra_monthly_gb=pkg.get("extra_monthly_gb", 0),
        extra_speed_mbps=pkg.get("extra_speed_mbps", 0),
        extra_requests=pkg.get("extra_requests", 0),
        admin_messages=admin_messages,
    )


# ─── Callback: admin onay / red ───────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^addon_(approve|reject)_(\d+)$"))
async def addon_admin_review(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id not in _approver_ids():
        return await callback_query.answer("Bu işlemi yapmaya yetkiniz yok.", show_alert=True)

    action = callback_query.matches[0].group(1)
    target_user_id = int(callback_query.matches[0].group(2))
    admin_name = callback_query.from_user.first_name or callback_query.from_user.username or f"Admin {callback_query.from_user.id}"

    user_pre = await db.get_user(target_user_id)
    if not user_pre or "pending_addon" not in user_pre:
        return await callback_query.answer("Bu talep zaten işleme alınmış.", show_alert=True)

    addon = user_pre["pending_addon"]
    admin_messages = addon.get("admin_messages", [])

    try:
        target_user = await client.get_users(target_user_id)
        user_mention = target_user.mention
        username_str = f"@{target_user.username}" if target_user.username else "N/A"
    except Exception:
        user_mention = f"User {target_user_id}"
        username_str = "N/A"

    label = addon.get("label", "?")
    price = addon.get("price", "?")

    detail_lines = []
    if addon.get("extra_days"):
        detail_lines.append(f"📅 +{addon['extra_days']} gün")
    if addon.get("extra_daily_gb"):
        detail_lines.append(f"📊 +{addon['extra_daily_gb']} GB/gün")
    if addon.get("extra_monthly_gb"):
        detail_lines.append(f"📦 +{addon['extra_monthly_gb']} GB/ay")
    if addon.get("extra_speed_mbps"):
        detail_lines.append(f"⚡ +{addon['extra_speed_mbps']} Mbps")
    if addon.get("extra_requests"):
        detail_lines.append(f"📩 +{addon['extra_requests']} istek/ay")
    details_text = "\n".join(detail_lines)

    info_text = (
        f"👤 <b>Kullanıcı:</b> {user_mention}\n"
        f"🆔 <b>Kullanıcı ID:</b> <code>{target_user_id}</code>\n"
        f"🔗 <b>Kullanıcı Adı:</b> {username_str}\n\n"
        f"📦 <b>Paket:</b> {label} ({price} TL)\n"
        f"{details_text}"
    )

    if action == "approve":
        user_data = await db.approve_addon(target_user_id)
        if user_data:
            # Kullanıcıya onay mesajı
            expiry = user_data.get("subscription_expiry")
            expiry_str = (expiry + timedelta(hours=3)).strftime("%d.%m.%Y") if expiry else "—"

            success_text = (
                f"✅ <b>Ek Paketiniz Aktif Edildi!</b>\n\n"
                f"📦 <b>Paket:</b> {label}\n"
                f"{details_text}\n\n"
                f"📅 <b>Abonelik bitiş tarihi:</b> {expiry_str}\n\n"
                "Limitler hesabınıza yansıtılmıştır."
            )
            try:
                await client.send_message(target_user_id, success_text, parse_mode=enums.ParseMode.HTML)
            except Exception as e:
                print(f"yukselt approve: could not message user {target_user_id}: {e}")

            status_caption = f"✅ <b>{admin_name} tarafından onaylandı</b>\n\n{info_text}"
            try:
                await callback_query.message.edit_text(status_caption, parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass

            acting_msg_id = callback_query.message.id
            for am in admin_messages:
                if am["message_id"] == acting_msg_id:
                    continue
                try:
                    await client.edit_message_text(
                        chat_id=am["chat_id"],
                        message_id=am["message_id"],
                        text=status_caption,
                        parse_mode=enums.ParseMode.HTML,
                    )
                except Exception:
                    pass
        else:
            await callback_query.answer("Onaylanamadı — bekleyen talep bulunamadı.", show_alert=True)

    elif action == "reject":
        success = await db.reject_addon(target_user_id)
        if success:
            try:
                await client.send_message(
                    target_user_id,
                    "❌ <b>Ek Paket Talebiniz Reddedildi</b>\n\n"
                    "Talebiniz yönetici tarafından reddedildi. "
                    "Farklı bir paket seçmek için /yukselt yazabilirsiniz.",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception as e:
                print(f"yukselt reject: could not message user {target_user_id}: {e}")

            status_caption = f"❌ <b>{admin_name} tarafından reddedildi</b>\n\n{info_text}"
            try:
                await callback_query.message.edit_text(status_caption, parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass

            acting_msg_id = callback_query.message.id
            for am in admin_messages:
                if am["message_id"] == acting_msg_id:
                    continue
                try:
                    await client.edit_message_text(
                        chat_id=am["chat_id"],
                        message_id=am["message_id"],
                        text=status_caption,
                        parse_mode=enums.ParseMode.HTML,
                    )
                except Exception:
                    pass
        else:
            await callback_query.answer("Reddedilemedi — bekleyen talep bulunamadı.", show_alert=True)
