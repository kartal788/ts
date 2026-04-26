"""
ayarlar.py
===========
/ayarlar komutu — stats.py mimarisini taklit eder.
Sayfa tabanlı navigasyon, inline callback ile çalışır.

Sayfalar:
  home      → Ana menü
  toggle    → True/False anahtarlar
  stremio   → Stremio eklenti ayarları
  erisim    → Erişim ayarları
  abonelik  → Abonelik ayarları
"""

import re
import pathlib

from pyrogram import filters, Client
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.enums import ParseMode

from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER
from Backend import db

# ── Sabitler ──────────────────────────────────────────────────────────────────

CONFIG_PATH        = pathlib.Path("config.env")
GDRIVE_TOKEN_PATH  = pathlib.Path(__file__).parent.parent.parent.parent / "gdrive_token.pickle"
RCLONE_CONF_PATH   = pathlib.Path(__file__).parent.parent.parent.parent / "rclone.conf"

TOGGLE_KEYS = ["REPLACE_MODE", "HIDE_CATALOG", "SUBSCRIPTION", "WEBSITESI", "Proxy"]

PAGE_TEXT_KEYS = {
    "stremio":  ["ISIM", "EKLENTI_ACIKLAMASI", "EKLENTI_LOGOSU", "BOLUM_RESIMI"],
    "erisim":   ["APPROVER_IDS", "AUTH_CHANNEL"],
    "abonelik": ["SUBSCRIPTION_URL"],
    "sistem":   ["YENILEME", "HIZ_LIMITI", "LIMIT_SIFIRLAMA"],
    "kuyruk":   ["MAX_CONCURRENT_DOWNLOADS", "MAX_CONCURRENT_UPLOADS"],
    "guvenlik": ["BRUTE_WINDOW", "BRUTE_MAX", "BRUTE_BAN"],
    "proxy":    ["ProxyType", "HTTP_Proxy_URL", "PROXY_MODE"],
    "token_limitleri": ["DEFAULT_DEVICE_LIMIT"],
}

KEY_DESCRIPTIONS = {
    "REPLACE_MODE":       "Dosya adı düzenleme modu",
    "HIDE_CATALOG":       "Katalog gizleme",
    "SUBSCRIPTION":       "Abonelik sistemi",
    "WEBSITESI":          "Website açık/kapalı (false → bakım modu, abonelere giriş bilgisi gönderilmez)",
    "Proxy":              "Proxy sistemi aktif/pasif",
    "ProxyType":          "Proxy türü (HTTP veya HTTPS)",
    "HTTP_Proxy_URL":     "Proxy URL'si (örn: https://PROXYURL/?url=)",
    "PROXY_MODE":         "Proxy modu: 1=Sadece normal, 2=Proxy+Normal (ikisi birden), 3=Sadece proxy",
    "ISIM":               "Eklenti / site adı (varsayılan: KARTAL)",
    "EKLENTI_ACIKLAMASI": "Stremio eklenti açıklaması",
    "EKLENTI_LOGOSU":     "Stremio eklenti logo URL'si",
    "BOLUM_RESIMI":       "Bölüm resmi fallback URL'si",
    "APPROVER_IDS":       "Onaylayan admin ID'leri (virgülle ayır)",
    "AUTH_CHANNEL":       "Zorunlu üyelik kanalı",
    "SUBSCRIPTION_URL":   "Abonelik sayfası URL'si",
    "YENILEME":           "Token geçerlilik süresi (saat). Boş = varsayılan 6 saat. Video izleme + indirme için geçerli.",
    "HIZ_LIMITI":         "Global hız limiti (Mbit/s). Boş = sınırsız. Örn: 50 → 50 Mbit/s",
    "LIMIT_SIFIRLAMA":    "Günlük limit sıfırlama saati (Türkiye saati, UTC+3). SS:DD formatında. Boş = gece 00:00 Türkiye saati. Örn: 06:00 → sabah 06:00 Türkiye saatinde sıfırlanır.",
    "MAX_CONCURRENT_DOWNLOADS": "Aynı anda kaç indirme yapılacak (Telegram/URL/GDrive). Boş veya 0 = sınırsız. Örn: 2 → en fazla 2 indirme paralel çalışır.",
    "MAX_CONCURRENT_UPLOADS":   "Aynı anda kaç yükleme (DB kayıt + metadata) yapılacak. Boş veya 0 = sınırsız. Örn: 1 → yüklemeler sırayla işlenir.",
    "BRUTE_WINDOW":             "Kaç saniye içindeki başarısız girişler sayılsın? (varsayılan: 60 sn)",
    "BRUTE_MAX":                "Pencere içinde kaç hata sonrası IP banlansın? (varsayılan: 10)",
    "BRUTE_BAN":                "IP kaç saniye boyunca engellensin? (varsayılan: 300 sn = 5 dk)",
    "DEFAULT_DEVICE_LIMIT":     "Her token için varsayılan maks. eşzamanlı stream (cihaz) sayısı. 0 = sınırsız. Örn: 3 → aynı anda 3 cihaz izleyebilir.",
}

# {user_id: (key, orig_message_id)}
_WAITING: dict = {}

# {user_id: dosya_tipi}  — dosya yükleme bekleniyor
_WAITING_FILE: dict = {}


# ── config.env yardımcıları ───────────────────────────────────────────────────

def _read_env() -> dict:
    """config.env okur; \r\n ve \r satır sonlarını temizler."""
    vals = {}
    if not CONFIG_PATH.exists():
        return vals
    # \r\n → \n normalize et
    text = CONFIG_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Z0-9_]+)\s*=\s*["\']?(.*?)["\']?\s*(?:#.*)?$', line)
        if m:
            vals[m.group(1)] = m.group(2).strip()
    return vals


def _write_env_key(key: str, value: str) -> None:
    """config.env dosyasında tek bir anahtarı günceller; \r\n sorununu önler."""
    if CONFIG_PATH.exists():
        # Önce \r\n ve \r normalize et, ardından işle
        text = CONFIG_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = ""

    new_line = f'{key}="{value}"'
    # \r? ekleyerek olası kalan carriage-return karakterini de yakala
    pattern = re.compile(rf'^{re.escape(key)}\s*=.*\r?$', re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(new_line, text)
    else:
        text = text.rstrip("\n") + f"\n{new_line}\n"

    # Her zaman Unix satır sonuyla yaz
    CONFIG_PATH.write_text(text, encoding="utf-8")
    LOGGER.info("Config güncellendi: %s = %s", key, value)


# ── UI üreticileri ────────────────────────────────────────────────────────────

def _short(vals: dict, key: str) -> str:
    raw = vals.get(key, "").strip()
    v = raw if raw else "(boş)"
    return v if len(v) <= 35 else v[:32] + "…"


def _back_close(user_id: int):
    return [
        InlineKeyboardButton("◀️ Geri", callback_data=f"cfg {user_id} home"),
        InlineKeyboardButton("✖ Kapat", callback_data=f"cfg {user_id} close"),
    ]


def _make_page(user_id: int, page: str, vals: dict):
    """(msg_html, InlineKeyboardMarkup) döner."""

    if page == "home":
        msg = (
            "⌬ <b><i>Bot Ayarları</i></b>\n"
            "│\n"
            "┟ Aşağıdaki kategorilerden birini seçin.\n"
            "┖ Değişiklikler <code>config.env</code> dosyasına kaydedilir."
        )
        kbd = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔧 Stremio", callback_data=f"cfg {user_id} toggle"),
                InlineKeyboardButton("🎬 Tasarım",        callback_data=f"cfg {user_id} stremio"),
            ],
            [
                InlineKeyboardButton("🔑 Erişim",         callback_data=f"cfg {user_id} erisim"),
                InlineKeyboardButton("💳 Abonelik",       callback_data=f"cfg {user_id} abonelik"),
            ],
            [
                InlineKeyboardButton("⚙️ Sistem",         callback_data=f"cfg {user_id} sistem"),
                InlineKeyboardButton("📋 Kuyruk",         callback_data=f"cfg {user_id} kuyruk"),
            ],
            [
                InlineKeyboardButton("🛡️ Güvenlik",       callback_data=f"cfg {user_id} guvenlik"),
                InlineKeyboardButton("🔒 Token Limitleri", callback_data=f"cfg {user_id} token_limitleri"),
            ],
            [
                InlineKeyboardButton("🔄 Yenile",         callback_data=f"cfg {user_id} home"),
                InlineKeyboardButton("📁 Dosya Ekle",     callback_data=f"cfg {user_id} dosya_ekle"),
            ],
            [
                InlineKeyboardButton("✖ Kapat",           callback_data=f"cfg {user_id} close"),
            ],
        ])
        return msg, kbd

    if page == "toggle":
        lines = ["⌬ <b><i>Stremio Ayarları</i></b>\n│"]
        for k in TOGGLE_KEYS:
            v = vals.get(k, "false").lower()
            emoji = "✅" if v == "true" else "❌"
            desc = KEY_DESCRIPTIONS.get(k, k)
            lines.append(f"┠ {emoji} <b>{k}</b> — <i>{desc}</i>")
        lines.append("┖ Değiştirmek için butona bas.")
        msg = "\n".join(lines)

        rows = []
        for k in TOGGLE_KEYS:
            v = vals.get(k, "false").lower()
            emoji = "✅" if v == "true" else "❌"
            rows.append([InlineKeyboardButton(
                f"{emoji} {k}",
                callback_data=f"cfg {user_id} _toggle {k}",
            )])
        rows.append(_back_close(user_id))
        return msg, InlineKeyboardMarkup(rows)

    if page == "dosya_ekle":
        token_status  = "✅ Yüklü" if GDRIVE_TOKEN_PATH.exists() else "❌ Yok"
        rclone_status = "✅ Yüklü" if RCLONE_CONF_PATH.exists() else "❌ Yok"

        # rclone.conf varsa sürücüleri listele
        rclone_remotes = ""
        if RCLONE_CONF_PATH.exists():
            try:
                import configparser
                rcp = configparser.ConfigParser()
                rcp.read(str(RCLONE_CONF_PATH))
                remotes = rcp.sections()
                if remotes:
                    rclone_remotes = "\n┃   Sürücüler: " + ", ".join(f"<code>{r}</code>" for r in remotes[:5])
                    if len(remotes) > 5:
                        rclone_remotes += f" (+{len(remotes)-5} daha)"
            except Exception:
                pass

        msg = (
            "⌬ <b><i>Dosya Ekle</i></b>\n"
            "│\n"
            "┠ <b>Google Drive Token</b>\n"
            f"┃   ↳ <code>gdrive_token.pickle</code> — <i>{token_status}</i>\n"
            "┠ Token'ı yüklemek için aşağıdaki butona bas,\n"
            "┃ ardından <code>.pickle</code> dosyasını gönder.\n"
            "┠ ─────────────────────────────\n"
            "┠ <b>Rclone Yapılandırması</b>\n"
            f"┃   ↳ <code>rclone.conf</code> — <i>{rclone_status}</i>"
            f"{rclone_remotes}\n"
            "┠ Rclone sürücülerinden (Drive, OneDrive vb.) içerik\n"
            "┃ eklemek için <code>rclone.conf</code> dosyanı gönder.\n"
            "┖ Yükledikten sonra <code>/ekle</code> komutuyla\n"
            "   Rclone sürücülerine erişebilirsin."
        )
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 token.pickle Yükle", callback_data=f"cfg {user_id} _upload_pickle")],
            [InlineKeyboardButton("🔧 rclone.conf Yükle",  callback_data=f"cfg {user_id} _upload_rclone")],
            _back_close(user_id),
        ])
        return msg, kbd

    if page in PAGE_TEXT_KEYS:
        titles = {
            "stremio":  "Stremio Eklenti Ayarları",
            "erisim":   "Erişim Ayarları",
            "abonelik": "Abonelik Ayarları",
            "sistem":   "Sistem Ayarları",
            "kuyruk":   "Kuyruk & Eşzamanlılık Ayarları",
            "guvenlik": "Güvenlik — Brute-Force Koruması",
            "proxy":    "Proxy Ayarları",
            "token_limitleri": "Token IP & Cihaz Limitleri",
        }
        lines = [f"⌬ <b><i>{titles[page]}</i></b>\n│"]

        if page == "guvenlik":
            bw_val  = vals.get("BRUTE_WINDOW", "").strip() or "60"
            bm_val  = vals.get("BRUTE_MAX",    "").strip() or "10"
            bb_val  = vals.get("BRUTE_BAN",    "").strip() or "300"
            lines.append(f"┠ <b>BRUTE_WINDOW</b>")
            lines.append(f"┃   ↳ <i>{KEY_DESCRIPTIONS['BRUTE_WINDOW']}</i>")
            lines.append(f"┃   Şu an: <code>{bw_val} sn</code>")
            lines.append(f"┠ <b>BRUTE_MAX</b>")
            lines.append(f"┃   ↳ <i>{KEY_DESCRIPTIONS['BRUTE_MAX']}</i>")
            lines.append(f"┃   Şu an: <code>{bm_val} deneme</code>")
            lines.append(f"┠ <b>BRUTE_BAN</b>")
            lines.append(f"┃   ↳ <i>{KEY_DESCRIPTIONS['BRUTE_BAN']}</i>")
            lines.append(f"┃   Şu an: <code>{bb_val} sn ({int(bb_val)//60} dk)</code>")
            lines.append("┠ ─────────────────────────────")
            lines.append("┠ <i>Örnek: 60 sn içinde 5 yanlış giriş → 10 dk ban</i>")
            lines.append("┃   → BRUTE_WINDOW = 60")
            lines.append("┃   → BRUTE_MAX    = 5")
            lines.append("┃   → BRUTE_BAN    = 600")
            lines.append("┖ Düzenlemek için ilgili butona bas.")

        elif page == "token_limitleri":
            dev_val = vals.get("DEFAULT_DEVICE_LIMIT", "").strip() or "0"
            dev_disp = dev_val if dev_val != "0" else "Sınırsız"
            lines.append("┠ <b>DEFAULT_DEVICE_LIMIT</b>")
            lines.append(f"┃   ↳ <i>{KEY_DESCRIPTIONS['DEFAULT_DEVICE_LIMIT']}</i>")
            lines.append(f"┃   Şu an: <code>{dev_disp}</code>")
            lines.append("┠ ─────────────────────────────")
            lines.append("┠ <i>Tokenın kendi limiti yoksa (0 / tanımsız) bu değer</i>")
            lines.append("┃ <i>anlık olarak tüm tokenlar için geçerli olur.</i>")
            lines.append("┠ <i>Tokenın özel limiti varsa bu ayar o tokena uygulanmaz.</i>")
            lines.append("┠ ─────────────────────────────")
            lines.append("┠ <i>Örnek: 3 cihaz limiti</i>")
            lines.append("┃   → DEFAULT_DEVICE_LIMIT = 3")
            lines.append("┖ Düzenlemek için ilgili butona bas.")

        elif page == "kuyruk":
            dl_val  = vals.get("MAX_CONCURRENT_DOWNLOADS", "").strip()
            ul_val  = vals.get("MAX_CONCURRENT_UPLOADS", "").strip()
            dl_disp = dl_val if dl_val and dl_val != "0" else "Sınırsız"
            ul_disp = ul_val if ul_val and ul_val != "0" else "Sınırsız"
            lines.append(f"┠ <b>MAX_CONCURRENT_DOWNLOADS</b>")
            lines.append(f"┃   ↳ <i>{KEY_DESCRIPTIONS['MAX_CONCURRENT_DOWNLOADS']}</i>")
            lines.append(f"┃   Şu an: <code>{dl_disp}</code>")
            lines.append(f"┠ <b>MAX_CONCURRENT_UPLOADS</b>")
            lines.append(f"┃   ↳ <i>{KEY_DESCRIPTIONS['MAX_CONCURRENT_UPLOADS']}</i>")
            lines.append(f"┃   Şu an: <code>{ul_disp}</code>")
            lines.append("┠ ─────────────────────────────")
            lines.append("┠ <i>Örnek: 2 indirme + 1 yükleme</i>")
            lines.append("┃   → MAX_CONCURRENT_DOWNLOADS = 2")
            lines.append("┃   → MAX_CONCURRENT_UPLOADS   = 1")
            lines.append("┠ <i>Toplam 1 işlem:</i>")
            lines.append("┃   → MAX_CONCURRENT_DOWNLOADS = 1")
            lines.append("┃   → MAX_CONCURRENT_UPLOADS   = 1")
            lines.append("┖ Boş veya 0 = sınırsız (eski davranış)")
        elif page == "proxy":
            proxy_on   = vals.get("Proxy", "false").lower() == "true"
            proxy_type = vals.get("ProxyType", "HTTPS").strip() or "HTTPS"
            proxy_url_val = vals.get("HTTP_Proxy_URL", "").strip() or "(boş)"
            mode_val   = vals.get("PROXY_MODE", "1").strip() or "1"
            mode_labels = {"1": "Sadece normal", "2": "Proxy + Normal (ikisi birden)", "3": "Sadece proxy"}
            mode_disp  = mode_labels.get(mode_val, mode_val)
            proxy_emoji = "✅" if proxy_on else "❌"
            lines.append(f"┠ {proxy_emoji} <b>Proxy</b> — <i>Proxy sistemi aktif/pasif</i>")
            lines.append(f"┠ <b>ProxyType</b>: <code>{proxy_type}</code>")
            lines.append(f"┃   ↳ <i>HTTP veya HTTPS</i>")
            lines.append(f"┠ <b>HTTP_Proxy_URL</b>: <code>{proxy_url_val if len(proxy_url_val) <= 50 else proxy_url_val[:47]+'…'}</code>")
            lines.append(f"┃   ↳ <i>Örn: https://PROXYURL/?url=</i>")
            lines.append(f"┠ <b>PROXY_MODE</b>: <code>{mode_val}</code> — <i>{mode_disp}</i>")
            lines.append(f"┃   ↳ <i>1=Sadece normal  2=Proxy+Normal  3=Sadece proxy</i>")
            lines.append("┖ Düzenlemek için ilgili butona bas.")

        else:
            for k in PAGE_TEXT_KEYS[page]:
                desc = KEY_DESCRIPTIONS.get(k, k)
                val  = _short(vals, k)
                lines.append(f"┠ <b>{k}</b>")
                lines.append(f"┃   ↳ <i>{desc}</i>")
                lines.append(f"┃   <code>{val}</code>")
            lines.append("┖ Düzenlemek için ilgili butona bas.")

        msg = "\n".join(lines)

        rows = []
        if page == "proxy":
            # Proxy açma/kapama toggle butonu
            proxy_on = vals.get("Proxy", "false").lower() == "true"
            proxy_emoji = "✅" if proxy_on else "❌"
            rows.append([InlineKeyboardButton(
                f"{proxy_emoji} Proxy (Aç/Kapat)",
                callback_data=f"cfg {user_id} _toggle Proxy",
            )])
            # ProxyType, HTTP_Proxy_URL, PROXY_MODE metin düzenleme butonları
            for k in ["ProxyType", "HTTP_Proxy_URL", "PROXY_MODE"]:
                rows.append([InlineKeyboardButton(
                    f"✏️ {k}",
                    callback_data=f"cfg {user_id} _text {k}",
                )])
        elif page == "token_limitleri":
            for k in ["DEFAULT_DEVICE_LIMIT"]:
                label = "📱 Cihaz Limiti"
                rows.append([InlineKeyboardButton(
                    f"✏️ {label}",
                    callback_data=f"cfg {user_id} _text {k}",
                )])
        else:
            for k in PAGE_TEXT_KEYS[page]:
                rows.append([InlineKeyboardButton(
                    f"✏️ {k}",
                    callback_data=f"cfg {user_id} _text {k}",
                )])
        rows.append(_back_close(user_id))
        return msg, InlineKeyboardMarkup(rows)

    return _make_page(user_id, "home", vals)


# ── /ayarlar komutu ───────────────────────────────────────────────────────────

@Client.on_message(filters.command("ayarlar") & filters.private & CustomFilters.owner)
async def cmd_ayarlar(client: Client, message: Message):
    vals = _read_env()
    msg, kbd = _make_page(message.from_user.id, "home", vals)
    await message.reply_text(msg, reply_markup=kbd, parse_mode=ParseMode.HTML, quote=True)


# ── Callback router ───────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^cfg "))
async def cfg_callback(client: Client, query: CallbackQuery):
    parts = query.data.split()
    if len(parts) < 3:
        await query.answer()
        return

    user_id = int(parts[1])
    action  = parts[2]
    extra   = parts[3] if len(parts) > 3 else None

    if query.from_user.id != user_id:
        await query.answer("Bu menü sana ait değil!", show_alert=True)
        return

    if action == "close":
        await query.answer()
        await query.message.delete()
        return

    vals = _read_env()

    if action == "_toggle" and extra in TOGGLE_KEYS:
        current = vals.get(extra, "false").lower()
        new_val = "false" if current == "true" else "true"
        _write_env_key(extra, new_val)
        vals[extra] = new_val
        await query.answer(f"✔️ {extra} → {new_val}")
        # Proxy toggle ise proxy sayfasına dön, aksi halde toggle sayfasına
        back_page = "proxy" if extra == "Proxy" else "toggle"
        msg, kbd = _make_page(user_id, back_page, vals)
        try:
            await query.message.edit_text(msg, reply_markup=kbd, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    # Google Drive token.pickle yükleme
    if action == "_upload_pickle":
        await query.answer()
        prompt = await query.message.reply_text(
            "📤 <b>token.pickle yükle</b>\n\n"
            "Google Drive <code>token.pickle</code> dosyasını şimdi gönder.\n"
            "<i>İptal için /iptal yaz.</i>",
            parse_mode=ParseMode.HTML,
        )
        _WAITING_FILE[user_id] = ("gdrive_pickle", prompt)
        LOGGER.info(f"[ayarlar] _WAITING_FILE güncellendi — uid={user_id}, tür=gdrive_pickle")
        return

    # Rclone conf yükleme
    if action == "_upload_rclone":
        await query.answer()
        prompt = await query.message.reply_text(
            "🔧 <b>rclone.conf yükle</b>\n\n"
            "Rclone yapılandırma dosyasını (<code>rclone.conf</code>) şimdi gönder.\n"
            "Sürücü adlarını kontrol etmek için: <code>rclone listremotes</code>\n"
            "<i>İptal için /iptal yaz.</i>",
            parse_mode=ParseMode.HTML,
        )
        _WAITING_FILE[user_id] = ("rclone_conf", prompt)
        LOGGER.info(f"[ayarlar] _WAITING_FILE güncellendi — uid={user_id}, tür=rclone_conf")
        return

    # Metin düzenleme
    if action == "_text" and extra:
        desc = KEY_DESCRIPTIONS.get(extra, extra)
        hint = ""
        if extra == "APPROVER_IDS":
            hint = "\n\n💡 <i>Birden fazla ID: <code>123456,789012</code></i>"
        await query.answer()
        prompt = await query.message.reply_text(
            f"✏️ <b>{extra}</b> için yeni değeri girin.\n"
            f"<i>{desc}</i>{hint}\n\n"
            f"<i>Boş bırakmak için <code>-</code> gönderin. İptal: /iptal</i>",
            parse_mode=ParseMode.HTML,
        )
        _WAITING[user_id] = (extra, prompt.id, query)
        return

    # Sayfa navigasyonu
    await query.answer()
    msg, kbd = _make_page(user_id, action, vals)
    try:
        await query.message.edit_text(msg, reply_markup=kbd, parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ── Metin girişi yakalayıcı ───────────────────────────────────────────────────

@Client.on_message(
    filters.private & CustomFilters.owner & filters.text & ~filters.command(""),
    group=1,
)
async def catch_text_input(client: Client, message: Message):
    uid = message.from_user.id
    if uid not in _WAITING:
        return

    key, prompt_msg_id, orig_query = _WAITING.pop(uid)

    # Kullanıcının metin mesajını sil
    try:
        await message.delete()
    except Exception:
        pass
    # Bot'un "değer girin" prompt mesajını sil
    try:
        await client.delete_messages(message.chat.id, prompt_msg_id)
    except Exception:
        pass

    if message.text.strip().lower() in ("/iptal", "iptal"):
        await orig_query.answer("❌ İptal edildi.", show_alert=True)
        return

    value = "" if message.text.strip() == "-" else message.text.strip()
    _write_env_key(key, value)

    # Eski ayarlar menüsü mesajını sil
    try:
        await orig_query.message.delete()
    except Exception:
        pass

    # Hangi sayfaya ait olduğunu bul
    back_page = "home"
    for page, keys in PAGE_TEXT_KEYS.items():
        if key in keys:
            back_page = page
            break

    vals = _read_env()
    page_msg, kbd = _make_page(uid, back_page, vals)

    await client.send_message(
        chat_id=message.chat.id,
        text=(
            f"✅ <b>{key}</b> güncellendi: <code>{value or '(boş)'}</code>\n"
            f"⚠️ <i>Değişikliğin etkili olması için botu yeniden başlatın.</i>\n\n"
            f"{page_msg}"
        ),
        reply_markup=kbd,
        parse_mode=ParseMode.HTML,
    )


# ── Dosya yükleme yakalayıcı (token.pickle vb.) ──────────────────────────────

@Client.on_message(
    filters.private & CustomFilters.owner & filters.document,
    group=1,
)
async def catch_file_upload(client: Client, message: Message):
    uid = message.from_user.id if message.from_user else None
    LOGGER.info(f"[ayarlar] catch_file_upload tetiklendi — uid={uid}, _WAITING_FILE keys={list(_WAITING_FILE.keys())}")

    if uid is None or uid not in _WAITING_FILE:
        LOGGER.info(f"[ayarlar] catch_file_upload — uid={uid} bekleme listesinde değil, atlanıyor.")
        return

    waiting_val = _WAITING_FILE.pop(uid)
    if isinstance(waiting_val, tuple):
        file_type, prompt_msg = waiting_val
    else:
        file_type, prompt_msg = waiting_val, None

    chat_id = message.chat.id

    # ── Debug: gelen mesaj tipini logla ──────────────────────────────────────
    doc_name = message.document.file_name if message.document else None
    doc_mime = message.document.mime_type if message.document else None
    LOGGER.info(
        f"[ayarlar] dosya detay — file_type={file_type}, "
        f"document={message.document is not None}, "
        f"file_name='{doc_name}', mime='{doc_mime}'"
    )

    # ── Prompt mesajını sil (kullanıcıya "şimdi dosya gönder" diyen mesaj) ──
    if prompt_msg:
        try:
            await prompt_msg.delete()
        except Exception:
            pass

    # ── token.pickle ────────────────────────────────────────────────────────
    if file_type == "gdrive_pickle":
        doc = message.document
        if doc is None:
            LOGGER.error("[ayarlar] token.pickle — message.document None geldi!")
            await client.send_message(chat_id, "❌ Dosya alınamadı (document=None). Lütfen dosyayı doğrudan dosya olarak gönderin.", parse_mode=ParseMode.HTML)
            return
        fname = (doc.file_name or "").lower()
        # Telegram bazen uzantıyı düşürür veya farklı isim verir; "pickle" geçiyorsa kabul et
        if ".pickle" not in fname:
            LOGGER.warning(f"[ayarlar] token.pickle red — gelen dosya adı: '{doc.file_name}'")
            try:
                await message.delete()
            except Exception:
                pass
            await client.send_message(
                chat_id,
                "❌ Yalnızca <code>.pickle</code> uzantılı dosya kabul edilir.\n"
                f"<i>Gelen dosya adı: <code>{doc.file_name}</code></i>",
                parse_mode=ParseMode.HTML,
            )
            return

        prog_msg = await client.send_message(chat_id, "⏳ İndiriliyor…", parse_mode=ParseMode.HTML)
        try:
            LOGGER.info(f"[ayarlar] token.pickle indiriliyor → {GDRIVE_TOKEN_PATH}")
            await client.download_media(message, file_name=str(GDRIVE_TOKEN_PATH))
            LOGGER.info(f"[ayarlar] token.pickle indirildi ✅")
        except Exception as e:
            LOGGER.error(f"[ayarlar] token.pickle indirme hatası: {e}")
            await prog_msg.edit_text(f"❌ İndirme hatası: <code>{e}</code>", parse_mode=ParseMode.HTML)
            return

        # İndirme başarılı — dosya ve progress mesajını sil
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await prog_msg.delete()
        except Exception:
            pass

        # MongoDB'ye kaydet (yeniden başlatmada kaybolmasın)
        try:
            pickle_bytes = GDRIVE_TOKEN_PATH.read_bytes()
            await db.dbs["tracking"]["bot_files"].update_one(
                {"_id": "gdrive_pickle"},
                {"$set": {"data": pickle_bytes, "file_name": "gdrive_token.pickle"}},
                upsert=True,
            )
            LOGGER.info("[ayarlar] gdrive_token.pickle MongoDB'ye kaydedildi.")
        except Exception as _e:
            LOGGER.warning(f"[ayarlar] gdrive_token.pickle MongoDB kaydı başarısız: {_e}")

        vals = _read_env()
        page_msg, kbd = _make_page(uid, "dosya_ekle", vals)
        await client.send_message(
            chat_id,
            f"✅ <b>token.pickle</b> kaydedildi.\n\n"
            f"Artık <code>/sunucuyayukle https://drive.google.com/…</code> "
            f"komutuyla Google Drive içerikleri indirilebilir.\n\n"
            f"{page_msg}",
            reply_markup=kbd,
            parse_mode=ParseMode.HTML,
        )

    # ── rclone.conf ─────────────────────────────────────────────────────────
    elif file_type == "rclone_conf":
        doc = message.document
        if doc is None:
            LOGGER.error("[ayarlar] rclone.conf — message.document None geldi!")
            await client.send_message(chat_id, "❌ Dosya alınamadı (document=None). Lütfen dosyayı doğrudan dosya olarak gönderin.", parse_mode=ParseMode.HTML)
            return
        fname = (doc.file_name or "").lower()
        # "rclone.conf", ".conf" uzantısı veya adında "rclone" geçiyorsa kabul et
        if not (fname == "rclone.conf" or fname.endswith(".conf") or "rclone" in fname):
            LOGGER.warning(f"[ayarlar] rclone.conf red — gelen dosya adı: '{doc.file_name}'")
            try:
                await message.delete()
            except Exception:
                pass
            await client.send_message(
                chat_id,
                "❌ Yalnızca <code>rclone.conf</code> dosyası kabul edilir.\n"
                f"<i>Gelen dosya adı: <code>{doc.file_name}</code></i>",
                parse_mode=ParseMode.HTML,
            )
            return

        prog_msg = await client.send_message(chat_id, "⏳ İndiriliyor…", parse_mode=ParseMode.HTML)
        try:
            LOGGER.info(f"[ayarlar] rclone.conf indiriliyor → {RCLONE_CONF_PATH}")
            await client.download_media(message, file_name=str(RCLONE_CONF_PATH))
            LOGGER.info(f"[ayarlar] rclone.conf indirildi ✅")
        except Exception as e:
            LOGGER.error(f"[ayarlar] rclone.conf indirme hatası: {e}")
            await prog_msg.edit_text(f"❌ İndirme hatası: <code>{e}</code>", parse_mode=ParseMode.HTML)
            return

        # MongoDB'ye kaydet (kalıcılık için)
        try:
            conf_bytes = RCLONE_CONF_PATH.read_bytes()
            await db.dbs["tracking"]["bot_files"].update_one(
                {"_id": "rclone_conf"},
                {"$set": {"data": conf_bytes, "file_name": "rclone.conf"}},
                upsert=True,
            )
            LOGGER.info("[ayarlar] rclone.conf MongoDB'ye kaydedildi.")
        except Exception as _e:
            LOGGER.warning(f"[ayarlar] rclone.conf MongoDB kaydı başarısız: {_e}")

        # Sürücü listesini oku
        remotes_text = ""
        try:
            import configparser
            rcp = configparser.ConfigParser()
            rcp.read(str(RCLONE_CONF_PATH))
            remotes = rcp.sections()
            if remotes:
                remotes_text = "\n\nBulunan sürücüler:\n" + "\n".join(f"• <code>{r}</code>" for r in remotes)
        except Exception:
            pass

        # Dosya ve progress mesajını sil
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await prog_msg.delete()
        except Exception:
            pass

        vals = _read_env()
        page_msg, kbd = _make_page(uid, "dosya_ekle", vals)
        await client.send_message(
            chat_id,
            f"✅ <b>rclone.conf</b> kaydedildi.{remotes_text}\n\n"
            f"Artık <code>/ekle</code> komutuyla Rclone sürücülerine erişebilirsin.\n\n"
            f"{page_msg}",
            reply_markup=kbd,
            parse_mode=ParseMode.HTML,
        )

