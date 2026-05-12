from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from Backend.config import Telegram
from Backend import db
from datetime import datetime, timedelta
import pathlib, re as _re

from bson.objectid import ObjectId

_CONFIG_PATH = pathlib.Path("config.env")

def _get_websitesi() -> bool:
    """config.env'den WEBSITESI değerini runtime'da okur (bot restart gerekmez)."""
    try:
        text = _CONFIG_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        m = _re.search(r'^WEBSITESI\s*=\s*["\']?(.*?)["\']?\s*(?:#.*)?$', text, _re.MULTILINE)
        if m:
            return m.group(1).strip().lower() == "true"
    except Exception:
        pass
    return True  # Bulunamazsa varsayılan: açık


@Client.on_callback_query(filters.regex(r"^plan_([a-fA-F0-9]{24})$"))
async def plan_selection(client: Client, callback_query: CallbackQuery):
    if not Telegram.SUBSCRIPTION:
        return await callback_query.answer("Abonelikler etkinleştirilmedi.", show_alert=True)

    plan_id = callback_query.matches[0].group(1)

    plans = await db.get_subscription_plans()
    plan = next((p for p in plans if p["_id"] == plan_id), None)

    if not plan:
        return await callback_query.answer("Geçersiz plan.", show_alert=True)

    user_id = callback_query.from_user.id if callback_query.from_user else callback_query.message.chat.id
    first_name = callback_query.from_user.first_name if callback_query.from_user else callback_query.message.chat.title
    username = callback_query.from_user.username if callback_query.from_user else callback_query.message.chat.username
    user_mention = callback_query.from_user.mention if callback_query.from_user else f"User {user_id}"
    username_str = f"@{callback_query.from_user.username}" if (callback_query.from_user and callback_query.from_user.username) else "N/A"

    # Callback'i hemen kapat — her şeyden önce
    await callback_query.answer(
        f"✅ {plan['days']} günlük plan seçildi. Yönetici onayı bekleniyor.",
        show_alert=True
    )

    await db.update_user_interaction(user_id, first_name, username)

    # Tahmini bitiş tarihi
    user = await db.get_user(user_id)
    now = datetime.utcnow()
    current_expiry = user.get("subscription_expiry") if user else None

    if current_expiry and current_expiry > now:
        new_expiry = current_expiry + timedelta(days=int(plan["days"]))
    else:
        new_expiry = now + timedelta(days=int(plan["days"]))

    expiry_str = new_expiry.strftime("%d.%m.%Y")

    # Pending kaydı oluştur
    await db.set_pending_payment(user_id, int(plan["days"]), 0, price=plan.get("price", 0), plan_id=plan.get("_id", ""))

    # Kullanıcıya bekleme mesajı — inline butonun üzerine düzenle, DM gönderme
    try:
        await callback_query.message.edit_text(
            f"⏳ <b>Plan Talebiniz Alındı</b>\n\n"
            f"📦 <b>Plan:</b> {plan['days']} gün — {plan['price']} TL\n"
            f"📅 <b>Tahmini son kullanma tarihi:</b> {expiry_str}\n\n"
            f"Talebiniz yöneticiye iletildi. Onaylandığında eklenti linkiniz buraya gönderilecektir.",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        print(f"Could not edit message for user {user_id}: {e}")
        try:
            await callback_query.message.reply_text(
                f"⏳ <b>Plan Talebiniz Alındı</b>\n\n"
                f"📦 <b>Plan:</b> {plan['days']} gün — {plan['price']} TL\n"
                f"📅 <b>Tahmini son kullanma tarihi:</b> {expiry_str}\n\n"
                f"Talebiniz yöneticiye iletildi. Onaylandığında eklenti linkiniz buraya gönderilecektir.",
                parse_mode=enums.ParseMode.HTML
            )
        except Exception as e2:
            print(f"Could not reply to user {user_id}: {e2}")

    # Admin bildirimi
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Onayla", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("❌ Reddet", callback_data=f"reject_{user_id}"),
            InlineKeyboardButton("🚫 Banla", callback_data=f"ban_{user_id}"),
        ]
    ])

    admin_text = (
        f"<b>📩 Yeni Abonelik Talebi</b>\n\n"
        f"<b>👤 Kullanıcı:</b> {user_mention}\n"
        f"<b>🆔 Kullanıcı ID:</b> <code>{user_id}</code>\n"
        f"<b>🔗 Kullanıcı Adı:</b> {username_str}\n\n"
        f"<b>📦 Plan:</b> {plan['days']} gün — {plan['price']} TL\n"
        f"<b>📅 Tahmini bitiş:</b> {expiry_str}\n\n"
        f"Lütfen talebi onaylayın veya reddedin."
    )

    approver_ids = Telegram.APPROVER_IDS if Telegram.APPROVER_IDS else [Telegram.OWNER_ID]
    print(f"DEBUG: Sending admin notification to: {approver_ids}")
    admin_messages = []
    for approver_id in approver_ids:
        try:
            sent = await client.send_message(approver_id, admin_text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)
            admin_messages.append({"chat_id": approver_id, "message_id": sent.id})
            print(f"DEBUG: Admin notified: {approver_id}")
        except Exception as e:
            print(f"Failed to notify approver {approver_id}: {e}")

    await db.set_pending_payment(user_id, int(plan["days"]), 0, price=plan.get("price", 0),
                                  admin_messages=admin_messages, plan_id=plan.get("_id", ""))


@Client.on_callback_query(filters.regex(r"^(approve|reject|ban|unban)_(\d+)$"))
async def admin_review(client: Client, callback_query: CallbackQuery):
    approver_ids = Telegram.APPROVER_IDS if Telegram.APPROVER_IDS else [Telegram.OWNER_ID]
    if callback_query.from_user.id not in approver_ids:
        return await callback_query.answer("Bu işlemi yapmaya yetkiniz yok.", show_alert=True)

    action = callback_query.matches[0].group(1)
    target_user_id = int(callback_query.matches[0].group(2))
    acting_admin = callback_query.from_user
    admin_name = acting_admin.first_name or acting_admin.username or f"Admin {acting_admin.id}"

    user_pre = await db.get_user(target_user_id)

    # Ban ve unban işlemleri için pending_payment zorunlu değil
    if action not in ("ban", "unban") and (not user_pre or "pending_payment" not in user_pre):
        return await callback_query.answer("Bu talep zaten işleme alınmış.", show_alert=True)

    admin_messages = (user_pre.get("pending_payment", {}).get("admin_messages", [])) if user_pre else []

    try:
        target_user = await client.get_users(target_user_id)
        user_mention = target_user.mention
        username_str = f"@{target_user.username}" if target_user.username else "N/A"
    except Exception:
        user_mention = f"User {target_user_id}"
        username_str = "N/A"

    duration = (user_pre.get("pending_payment", {}).get("duration", "?")) if user_pre else "?"
    price = (user_pre.get("pending_payment", {}).get("price", "?")) if user_pre else "?"

    info_text = (
        f"👤 <b>Kullanıcı:</b> {user_mention}\n"
        f"🆔 <b>Kullanıcı ID:</b> <code>{target_user_id}</code>\n"
        f"🔗 <b>Kullanıcı Adı:</b> {username_str}\n\n"
        f"📦 <b>Plan:</b> {duration} gün ({price} TL)"
    )

    if action == "approve":
        user_data = await db.approve_payment(target_user_id)
        if user_data:
            # Hatırlatma flag'ini sıfırla — yeni abonelik döneminde tekrar mesaj gitsin
            try:
                await db.reset_reminder_sent(target_user_id)
            except Exception:
                pass
            try:
                user_obj = await db.get_user(target_user_id)
                user_name = (user_obj.get("first_name") or user_obj.get("username") or str(target_user_id)) if user_obj else str(target_user_id)
                plan_daily   = user_data.get("_plan_daily_gb",   0) or None
                plan_monthly = user_data.get("_plan_monthly_gb", 0) or None
                plan_speed   = user_data.get("_plan_speed_mbps", 0) or None
                token_doc = await db.add_api_token(
                    name=user_name,
                    user_id=target_user_id,
                    daily_limit_gb=plan_daily,
                    monthly_limit_gb=plan_monthly,
                    speed_limit_mbps=plan_speed
                )
                token_str = token_doc.get("token")
            except Exception:
                token_str = None

            base_url = Telegram.BASE_URL
            expiry = user_data.get("subscription_expiry")
            expiry_str = expiry.strftime("%d.%m.%Y") if expiry else "—"

            # Portal girişi için OTP üret — sadece website açıksa (WEBSITESI=true)
            try:
                if _get_websitesi():
                    user_obj2 = user_obj or await db.get_user(target_user_id)
                    user_name2 = (user_obj2.get("first_name") or user_obj2.get("username") or str(target_user_id)) if user_obj2 else str(target_user_id)
                    otp = await db.create_member_otp(target_user_id, user_name2)
                    portal_url = f"{base_url}/uye/giris"
                    otp_text = (
                        f"\n\n🌐 <b>Dizi ve filmleri indirmek için:</b>\n"
                        f"🔗 {portal_url}\n"
                        f"👤 <b>Kullanıcı Adı:</b> <code>{otp['username']}</code>\n"
                        f"🔑 <b>Şifre:</b> <code>{otp['password']}</code>\n"
                        f"<i>⚠️ Bu bilgiler her /start'ta yenilenir.</i>"
                    )
                else:
                    otp_text = (
                        f"\n\n🔧 <b>{Telegram.ISIM} Websitesi</b> şu an bakım çalışmasındadır.\n"
                        f"<i>Hizmet kısa süre içinde tekrar aktif olacaktır.</i>"
                    )
            except Exception:
                otp_text = ""

            if token_str:
                tr_url = f"{base_url}/stremio/{token_str}/tr/manifest.json"
                de_url = f"{base_url}/stremio/{token_str}/de/manifest.json"
                en_url = f"{base_url}/stremio/{token_str}/en/manifest.json"

                success_text = (
                    f"✅ <b>Aboneliğiniz aktif durumdadır.</b>\n"
                    f"📅 <b>Son kullanma tarihi:</b> {expiry_str}\n\n"
                    f"🔗 <b>Eklenti linkiniz:</b>\n\n"
                    f"🇹🇷 <b>Türkçe:</b>\n"
                    f"<code>{tr_url}</code>\n\n"
                    f"🇩🇪 <b>Deutsch:</b>\n"
                    f"<code>{de_url}</code>\n\n"
                    f"🇬🇧 <b>English:</b>\n"
                    f"<code>{en_url}</code>\n\n"
                    f"Dizi ve filmleri izlemek için yukarıdaki linki kopyalayıp Stremio eklentilerine yapıştırın."
                    f"{otp_text}"
                )
            else:
                success_text = (
                    f"✅ <b>Aboneliğiniz aktif durumdadır.</b>\n"
                    f"📅 <b>Son kullanma tarihi:</b> {expiry_str}\n\n"
                    f"⚠️ Eklenti linkiniz oluşturulurken sorun oluştu. Lütfen yönetici ile iletişime geçin."
                    f"{otp_text}"
                )

            await client.send_message(target_user_id, success_text, parse_mode=enums.ParseMode.HTML)

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
                        parse_mode=enums.ParseMode.HTML
                    )
                except Exception:
                    pass
        else:
            await callback_query.answer("Onaylanamadı — bekleyen talep bulunamadı.", show_alert=True)

    elif action == "reject":
        success = await db.reject_payment(target_user_id)
        if success:
            await client.send_message(
                target_user_id,
                "❌ <b>Talebiniz Reddedildi</b>\n\nAbonelik talebiniz yönetici tarafından reddedildi. "
                "Daha fazla bilgi için yönetici ile iletişime geçin ya da tekrar deneyin.",
                parse_mode=enums.ParseMode.HTML
            )

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
                        parse_mode=enums.ParseMode.HTML
                    )
                except Exception:
                    pass
        else:
            await callback_query.answer("Reddedilemedi — bekleyen talep bulunamadı.", show_alert=True)

    elif action == "ban":
        await db.ban_user(target_user_id)
        try:
            await client.send_message(
                target_user_id,
                "🚫 <b>Hesabınız Engellendi</b>\n\nHesabınız yönetici tarafından engellenmiştir. "
                "Bu botu artık kullanamazsınız.",
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass

        unban_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔓 Banı Kaldır", callback_data=f"unban_{target_user_id}")]
        ])

        status_caption = f"🚫 <b>{admin_name} tarafından banlandı</b>\n\n{info_text}"
        try:
            await callback_query.message.edit_text(status_caption, reply_markup=unban_keyboard, parse_mode=enums.ParseMode.HTML)
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
                    reply_markup=unban_keyboard,
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass

        await callback_query.answer("Kullanıcı banlandı.", show_alert=True)

    elif action == "unban":
        success = await db.unban_user(target_user_id)
        if success:
            try:
                await client.send_message(
                    target_user_id,
                    "🔓 <b>Engeliniz Kaldırıldı</b>\n\nHesabınızdaki engel yönetici tarafından kaldırıldı. "
                    "/start yazarak plan seçebilirsiniz.",
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass

            status_caption = f"🔓 <b>{admin_name} tarafından banı kaldırıldı</b>\n\n{info_text}"
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
                        parse_mode=enums.ParseMode.HTML
                    )
                except Exception:
                    pass

            await callback_query.answer("Kullanıcının banı kaldırıldı.", show_alert=True)
        else:
            await callback_query.answer("Bu kullanıcı zaten banlı değil.", show_alert=True)


@Client.on_message(filters.command("abonelik"))
async def check_status(client: Client, message: Message):
    if not Telegram.SUBSCRIPTION:
        return

    user_id = (message.from_user.id if message.from_user else None) or (message.sender_chat.id if message.sender_chat else None) or message.chat.id

    user = await db.get_user(user_id)
    if not user or user.get("subscription_status") != "active":
        return await message.reply_text("Aktif bir aboneliğiniz bulunmamaktadır.")

    expiry = user.get("subscription_expiry")
    if not expiry:
        return await message.reply_text("Son kullanma tarihi alınırken hata oluştu.")

    now = datetime.utcnow()
    if now > expiry:
        return await message.reply_text("Aboneliğiniz sona ermiştir.")

    remaining = expiry - now
    days = remaining.days
    hours = remaining.seconds // 3600

    expiry_tr = expiry + timedelta(hours=3)

    await message.reply_text(
        f"<b>Üyelik:</b> Aktif ✅\n"
        f"<b>Bitiş:</b> {expiry_tr.strftime('%d.%m.%Y %H:%M')}\n"
        f"<b>Kalan:</b> {days} gün {hours} saat",
        parse_mode=enums.ParseMode.HTML
    )
