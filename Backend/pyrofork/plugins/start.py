from pyrogram import filters, Client, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from Backend.helper.custom_filter import CustomFilters
from Backend.config import Telegram
from Backend.helper.settings_manager import SettingsManager
from Backend import db
from datetime import datetime

print("DEBUG: start.py PLUGIN LOADED SUCCESSFULLY!")

@Client.on_message(filters.command('start'), group=10)
async def send_start_message(client: Client, message: Message):
    try:
        user_id = (message.from_user.id if message.from_user else None) or (message.sender_chat.id if message.sender_chat else None) or message.chat.id
        print(f"DEBUG: Received /start command from {user_id}")

        # ── Admin oturumu yalnızca OWNER /start attığında geçersiz kılınır ──
        # Diğer kullanıcıların /start komutu admin şifresini etkilemez.
        if user_id == Telegram.OWNER_ID:
            try:
                from Backend import db as _db
                await _db.invalidate_admin_session()
            except Exception as _inv_err:
                print(f"DEBUG: invalidate_admin_session error: {_inv_err}")
        # ────────────────────────────────────────────────────────────────────

        # Ban kontrolü
        if await db.is_user_banned(user_id):
            await message.reply_text(
                "🚫 <b>Hesabınız engellenmiştir.</b>\n\n"
                "Bu botu kullanma yetkiniz bulunmamaktadır. "
                "Daha fazla bilgi için yönetici ile iletişime geçin.",
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )
            return

        base_url = Telegram.BASE_URL
        addon_url = f"{base_url}/stremio/manifest.json"

        # If subscriptions are NOT enabled
        if not Telegram.SUBSCRIPTION:
            user_name = (message.from_user.first_name or message.from_user.username or f"User {user_id}") if message.from_user else f"Chat {user_id}"
            try:
                token_doc = await db.add_api_token(name=user_name, user_id=user_id)
                token_str = token_doc.get("token")
                addon_url = f"{base_url}/stremio/{token_str}/manifest.json"
            except Exception as e:
                print(f"DEBUG: Error ensuring token for free user: {e}")

            await message.reply_text(
                f'🎉 <b>{Telegram.ISIM} Medya Sunucusuna Hoş Geldiniz</b>\n\n'
                'Kişisel Nuvio eklenti bağlantınız aşağıdadır:\n\n'
                '🎬 <b>Stremio Eklentisi:</b>\n'
                f'<code>{addon_url}</code>\n\n'
                'Dizi ve filmleri izlemek için yukarıdaki linki kopyalayıp Nuvio eklentilerine yapıştırın.',
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )
            return

        # Subscription logic (When SUBSCRIPTION=True)
        user = await db.get_user(user_id)
        now = datetime.utcnow()

        # Check if user has an active subscription
        is_active = False
        if user and user.get("subscription_status") == "active":
            if user.get("subscription_expiry") and user.get("subscription_expiry") > now:
                is_active = True
            else:
                await db.mark_user_expired(user_id)

        if not is_active:
            plans = await db.get_subscription_plans()
            if not plans:
                # OWNER ise admin paneli giriş bilgilerini de mesaja ekle
                admin_otp_text = ""
                if user_id == Telegram.OWNER_ID:
                    try:
                        photo_url = message.from_user.photo.big_file_id if (message.from_user and message.from_user.photo) else ""
                        admin_otp = await db.create_admin_otp(photo_url=photo_url)
                        admin_url = f"{base_url}/login"
                        admin_otp_text = (
                            f"\n\n🛡️ <b>Yönetici Paneli Girişi:</b>\n"
                            f"🔗 {admin_url}\n"
                            f"👤 <b>Kullanıcı Adı:</b> <code>{admin_otp['username']}</code>\n"
                            f"🔑 <b>Şifre:</b> <code>{admin_otp['password']}</code>\n"
                            f"<i>⚠️ Bu bilgiler her /start'ta yenilenir, yalnızca tek kullanımlıktır.</i>"
                        )
                    except Exception as e:
                        print(f"DEBUG: Admin OTP generation error (no-plan branch): {e}")

                return await message.reply_text(
                    f'<b>{Telegram.ISIM} Özel Grubuna Hoş Geldiniz!</b>\n\n'
                    'Şu anda herhangi bir abonelik planı tanımlanmamıştır. Lütfen yönetici ile iletişime geçin.'
                    f'{admin_otp_text}',
                    quote=True,
                    parse_mode=enums.ParseMode.HTML
                )

            keyboard_buttons = []
            for plan in plans:
                if plan.get('label'):
                    plan_label = plan['label']
                elif plan.get('is_unlimited'):
                    plan_label = 'Sınırsız'
                elif plan.get('days', 0) >= 365:
                    plan_label = f"{round(plan['days'] / 365)} Yıl"
                elif plan.get('days', 0) >= 30:
                    plan_label = f"{round(plan['days'] / 30)} Ay"
                else:
                    plan_label = f"{plan.get('days', 0)} Gün"
                keyboard_buttons.append([InlineKeyboardButton(f"{plan_label} - {plan['price']} TL", callback_data=f"plan_{plan['_id']}")])

            keyboard = InlineKeyboardMarkup(keyboard_buttons)

            #----- Aktif aboneliği olmayan kullanıcıya gösterilen üst metin,
            #----- panelin Ayarlar > Abonelik bölümünden düzenlenebilir.
            #----- İçindeki {isim} ifadesi bot adıyla değiştirilir.
            message_template = SettingsManager.current().uye_olmayan_mesaji or (
                f'<b>{Telegram.ISIM} ile sinema keyfine hazır mısın?</b>\n\n'
                'Nuvio üzerinden sunduğumuz özel içeriklere erişebilmen için aktif bir aboneliğin olması gerekiyor. '
                'Merak etme, senin için en avantajlı planları aşağıda listeledik.\n\n'
                '🚀 Hemen başlamak için bir plan seç:'
            )
            plan_caption = message_template.replace("{isim}", Telegram.ISIM)

            plan_image_id = await db.get_plan_image()
            if plan_image_id:
                return await message.reply_photo(
                    photo=plan_image_id,
                    caption=plan_caption,
                    reply_markup=keyboard,
                    quote=True,
                    parse_mode=enums.ParseMode.HTML
                )
            else:
                return await message.reply_text(
                    plan_caption,
                    reply_markup=keyboard,
                    quote=True,
                    parse_mode=enums.ParseMode.HTML
                )

        # User is active, fetch their token
        all_tokens = await db.get_all_api_tokens()
        token_doc = next((t for t in all_tokens if t.get("user_id") == user_id), None)

        token_str = None
        if token_doc and "token" in token_doc:
            token_str = token_doc["token"]

        expiry = user.get("subscription_expiry")
        expiry_str = expiry.strftime("%d.%m.%Y") if expiry else "—"

        # Tek kullanımlık portal girişi için OTP üret
        user_name = (message.from_user.first_name or message.from_user.username or f"User {user_id}") if message.from_user else f"Chat {user_id}"
        try:
            otp = await db.create_member_otp(user_id, user_name)
            portal_url = f"{base_url}/uye/giris"
            otp_text = (
                f"\n\n🌐 <b>Dizi ve filmleri indirmek için:</b>\n"
                f"🔗 {portal_url}\n"
                f"👤 <b>Kullanıcı Adı:</b> <code>{otp['username']}</code>\n"
                f"🔑 <b>Şifre:</b> <code>{otp['password']}</code>\n"
                f"<i>⚠️ Bu bilgiler her /start'ta yenilenir.</i>"
            )
        except Exception as e:
            print(f"DEBUG: OTP generation error: {e}")
            otp_text = ""

        # ── OWNER ise admin paneli OTP'sini de ekle ─────────────────────────
        if user_id == Telegram.OWNER_ID:
            try:
                photo_url = message.from_user.photo.big_file_id if (message.from_user and message.from_user.photo) else ""
                admin_otp = await db.create_admin_otp(photo_url=photo_url)
                admin_url = f"{base_url}/login"
                otp_text += (
                    f"\n\n🛡️ <b>Yönetici Paneli Girişi:</b>\n"
                    f"🔗 {admin_url}\n"
                    f"👤 <b>Kullanıcı Adı:</b> <code>{admin_otp['username']}</code>\n"
                    f"🔑 <b>Şifre:</b> <code>{admin_otp['password']}</code>\n"
                    f"<i>⚠️ Bu bilgiler her /start'ta yenilenir, yalnızca tek kullanımlıktır.</i>"
                )
            except Exception as e:
                print(f"DEBUG: Admin OTP generation error: {e}")
        # ────────────────────────────────────────────────────────────────────

        if token_str:
            tr_url  = f"{base_url}/stremio/{token_str}/tr/manifest.json"
            de_url  = f"{base_url}/stremio/{token_str}/de/manifest.json"
            en_url  = f"{base_url}/stremio/{token_str}/en/manifest.json"

            await message.reply_text(
                '✅ <b>Aboneliğiniz aktif durumdadır.</b>\n'
                f'📅 <b>Son kullanma tarihi:</b> {expiry_str}\n\n'
                '🔗 <b>Eklenti linkiniz:</b>\n\n'
                '🇹🇷 <b>Türkçe:</b>\n'
                f'<code>{tr_url}</code>\n\n'
                '🇩🇪 <b>Deutsch:</b>\n'
                f'<code>{de_url}</code>\n\n'
                '🇬🇧 <b>English:</b>\n'
                f'<code>{en_url}</code>\n\n'
                'Dizi ve filmleri izlemek için yukarıdaki linki kopyalayıp Nuvio eklentilerine yapıştırın.'
                f'{otp_text}',
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )
        else:
            await message.reply_text(
                '✅ <b>Aboneliğiniz aktif durumdadır.</b>\n'
                f'📅 <b>Son kullanma tarihi:</b> {expiry_str}\n\n'
                '⚠️ Eklenti linkiniz oluşturulurken bir sorun oluştu. Lütfen yönetici ile iletişime geçin.'
                f'{otp_text}',
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )

    except Exception as e:
        await message.reply_text(f"⚠️ Error: {e}")
        print(f"Error in /start handler: {e}")
