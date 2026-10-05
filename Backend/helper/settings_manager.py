"""
settings_manager.py
====================
DB'ye kalıcı (canlı) ayarlar sistemi. Panelden (Ayarlar sayfası) değiştirilen
değerler MongoDB'nin "tracking" veritabanındaki "settings" koleksiyonuna
yazılır ve process yeniden başlatılmadan hemen devreye girer.

Mevcut kod tabanının tamamı `Backend.config.Telegram.XXX` üzerinden ayarlara
erişiyor. Yüzlerce dosyayı SettingsManager.current() kullanacak şekilde
değiştirmek yerine (riskli/kapsamlı bir refactor), SettingsManager.update()
çağrıldığında ilgili `Telegram` sınıfı attribute'ları da canlı olarak
güncellenir (bkz. _SETTINGS_TO_TELEGRAM_ATTR). Böylece stream_routes.py,
sunucu_routes.py vb. tüm mevcut kod hiç değişmeden yeni değerleri anında görür.

config.env / ortam değişkenleri hâlâ İLK açılıştaki (seed) değerleri sağlar;
DB'de kayıt yoksa oradan başlatılır. Sonraki her değişiklik DB'de kalıcı olur
ve config.env'in önüne geçer.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from Backend.config import Telegram
from Backend.logger import LOGGER

#----- Ana yönetici (OWNER_ID) yönetici listesinden hiçbir şekilde çıkarılamaz.
#----- Liste her kaydedilişte/yüklenişte normalize edilir: tamsayıya çevrilir,
#----- tekrarlar atılır ve OWNER_ID HER ZAMAN ilk sırada yer alır. Böylece
#----- panelden, API'den, yedek geri yüklemeden ya da DB'den gelen listeden
#----- ana yönetici silinmiş olsa bile onay talepleri ve yetkisi korunur.
def normalize_approver_ids(values) -> List[int]:
    result: List[int] = []
    owner = Telegram.OWNER_ID
    if owner:
        result.append(int(owner))
    for v in (values or []):
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv not in result:
            result.append(iv)
    return result


#----- Abonelik ve dizi/film isteği taleplerini onaylayacak hesap.
#----- Seçilen hesap yönetici listesinde (OWNER_ID veya APPROVER_IDS) değilse ya da
#----- hiç seçilmemişse (0) ana yönetici (OWNER_ID) kullanılır.
def get_approval_account_id() -> int:
    owner = int(Telegram.OWNER_ID or 0)
    try:
        chosen = int(getattr(Telegram, "APPROVAL_ACCOUNT_ID", 0) or 0)
    except (TypeError, ValueError):
        return owner
    if chosen and (chosen == owner or chosen in (Telegram.APPROVER_IDS or [])):
        return chosen
    return owner


def can_review_requests(user_id) -> bool:
    """Bot üzerinden abonelik/istek onay butonlarını kullanabilir mi?
    Seçili onay hesabı + ana yönetici (eski mesajlardaki butonlar için)."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid == get_approval_account_id() or uid == int(Telegram.OWNER_ID or 0)


#----- Yönetici Telegram kullanıcı adı (ör. "kaya89"). Panelde (Ayarlar > Abonelik)
#----- girilir; abonelik talebi bekleme / red mesajlarında "yönetici" yerine gösterilir.
#----- Başındaki "@" ve "https://t.me/" öneki atılır; boş bırakılabilir (boşsa
#----- mesajlar eskisi gibi "yönetici" der).
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def normalize_admin_username(value) -> str:
    name = str(value or "").strip()
    name = re.sub(r"^(?:https?://)?(?:t\.me/|telegram\.me/)", "", name, flags=re.IGNORECASE)
    name = name.lstrip("@").strip().rstrip("/")
    return name


def get_admin_username() -> str:
    try:
        name = normalize_admin_username(SettingsManager.current().admin_username)
    except Exception:
        return ""
    return name if _USERNAME_RE.match(name) else ""


#----- Türkçe yönelme (-e/-a) eki: kullanıcı adının OKUNUŞUNA göre seçilir.
#----- Sayılar okunuşlarıyla değerlendirilir (89 -> "dokuz" -> 'a, 86 -> "altı" -> 'ya).
_DIGIT_SUFFIX = {
    "0": "a", "1": "e", "2": "ye", "3": "e", "4": "e",
    "5": "e", "6": "ya", "7": "ye", "8": "e", "9": "a",
}
_BACK_VOWELS = set("aıouAIOU")
_FRONT_VOWELS = set("eiöüEİÖÜ")
_ALL_VOWELS = _BACK_VOWELS | _FRONT_VOWELS | set("Iı")


def _dative_suffix(name: str) -> str:
    last = name[-1]
    if last in _DIGIT_SUFFIX:
        return _DIGIT_SUFFIX[last]
    if last == "_":
        return _dative_suffix(name[:-1]) if len(name) > 1 else "e"
    last_vowel = next((c for c in reversed(name) if c in _ALL_VOWELS), None)
    harmony = "a" if (last_vowel is None or last_vowel in _BACK_VOWELS) else "e"
    return ("y" + harmony) if last in _ALL_VOWELS else harmony


#----- "Talebiniz {yöneticiye / @kullanici'ya} iletildi."
def admin_forwarded_to() -> str:
    name = get_admin_username()
    return f"@{name}'{_dative_suffix(name)}" if name else "yöneticiye"


#----- "Daha fazla bilgi için {yönetici / @kullanici} ile iletişime geçin."
def admin_contact_ref() -> str:
    name = get_admin_username()
    return f"@{name}" if name else "yönetici"


#----- Panelden yönetilebilir ayarların varsayılan değerleri
_DEFAULTS: Dict[str, Any] = {
    "replace_mode": True,
    "hide_catalog": False,
    "auth_channels": [],
    "tmdb_api": "",
    #----- DeepL çeviri API anahtarı (bkz. Backend.helper.metadata çeviri zinciri).
    #----- Boş bırakılırsa DeepL adımı atlanır, Google -> MyMemory zincirine düşülür.
    "deepl_api": "",
    #----- Birden fazla DeepL anahtari (oncelik sirasi = liste sirasi). "deepl_api" her zaman
    #----- bu listenin ilk elemanina esitlenir (eski kodlarla uyumluluk icin).
    "deepl_api_keys": [],
    #----- DeepL API kendi abonelik/faturalama dönemini (başlangıç-bitiş)
    #----- DÖNMÜYOR, sadece karakter kullanımı/kotasını veriyor (bkz.
    #----- Backend.helper.metadata.get_deepl_usage). Bu yüzden dönem
    #----- tarihleri panelde kullanıcı tarafından elle girilir; "kalan gün"
    #----- deepl_renewal_date'ten istemci tarafında hesaplanır.
    "deepl_billing_start": "",
    "deepl_renewal_date": "",
    "base_url": "",
    #----- Duyurulardaki Stremio/Nuvio "aç" butonlarında kullanılan alan adı.
    #----- Kasıtlı olarak base_url'den AYRI tutulur: bu genellikle gerçek
    #----- sunucu adresini gizlemek için ayrı barındırılan (ör. bir Cloudflare
    #----- Workers alt alan adı) bir yönlendirme sayfasının adresidir.
    "redirect_base_url": "",
    "upstream_repo": "",
    "upstream_branch": "",
    "isim": "KARTAL",
    "eklenti_aciklamasi": "Dizi ve film arşivi.",
    "eklenti_logosu": "",
    "bolum_resimi": "",
    "max_concurrent_downloads": "",
    "max_concurrent_uploads": "1",
    "proxy": False,
    "proxy_type": "HTTPS",
    "http_proxy_url": "",
    "proxy_mode": 1,
    #----- Proxy hangi üyelere uygulanacak: "subscribers" (tüm aktif aboneler,
    #----- varsayılan) veya "selected" (yalnızca proxy_scope_member_ids'teki
    #----- üyeler). Kapsam dışı üyeler PROXY_MODE'dan bağımsız olarak her
    #----- zaman doğrudan (proxy'siz) link alır.
    "proxy_scope_mode": "subscribers",
    "proxy_scope_member_ids": [],
    "default_device_limit": 0,
    "member_bot_limit": 3,
    "credential_rotate_days": 7,
    "yenileme": "",
    "hiz_limiti": "",
    "limit_sifirlama": "",
    "subscription": False,
    "subscription_group_id": 0,
    "subscription_url": "https://t.me/",
    "approver_ids": [],
    #----- Abonelik/istek onay mesajlarının gideceği hesap. 0 = ana yönetici (OWNER_ID).
    "approval_account_id": 0,
    #----- Abonelik mesajlarında "yönetici" yerine gösterilen Telegram kullanıcı adı
    #----- (bkz. admin_forwarded_to / admin_contact_ref). Boş = "yönetici".
    "admin_username": "",
    "websitesi": False,
    "brute_window": 60,
    "brute_max": 5,
    "brute_ban": 1800,
    #----- Stream chunk indirme davranışı (Backend.helper.custom_dl.prefetch_stream)
    #----- "parallel" -> Telegram.PARALLEL -> aslında prefetch KUYRUK derinliği
    #----- "pre_fetch" -> Telegram.PRE_FETCH -> aslında Telegram'a giden EŞZAMANLI istek sayısı
    #----- (İsimler config.env'deki eski adlandırmayla ters eşleşiyor, bkz. stream_routes.py)
    "parallel": 4,
    "pre_fetch": 3,
    #----- Brute-force korumasının X-Forwarded-For'a güvendiği proxy CIDR'ları.
    #----- Boş = header'a hiç güvenilmez (bkz. Backend.fastapi.security.brute_force).
    "trusted_proxy_cidrs": "",
    #----- WebDAV senkronizasyon aralığı (saat). 0 = kapalı. Varsayılan config.env'den gelir.
    "webdav_sync_interval_hours": Telegram.WEBDAV_SYNC_INTERVAL_HOURS,
    "cloud_sync_interval_hours": Telegram.CLOUD_SYNC_INTERVAL_HOURS,
    "extra_databases": [],
    "multi_tokens": [],
    "announce_new_content": False,
    "announcement_channel": "",
    #----- Açıksa Stremio "poster" alanları (poster/poster_tr/poster_de) için
    #----- veritabanı yerine btttr.cc üzerinden imdb_id'ye göre üretilen
    #----- "Better Poster" linkleri denenir; bu link çalışmazsa (veya imdb_id
    #----- yoksa) veritabanındaki eski posterlere geri düşülür. Varsayılan
    #----- kapalı — kapalıyken eski davranış (doğrudan veritabanı posteri) aynen çalışır.
    "better_poster_url": False,
    #----- /start komutuna aktif aboneliği olmayan kullanıcılara gösterilen
    #----- mesaj (satın alınabilir planlar listelenmeden önceki üst metin).
    #----- İçinde geçen {isim} ifadesi gönderim anında Telegram.ISIM ile
    #----- değiştirilir.
    "uye_olmayan_mesaji": (
        "<b>{isim} ile sinema keyfine hazır mısın?</b>\n\n"
        "Stremio üzerinden sunduğumuz özel içeriklere erişebilmen için aktif "
        "bir aboneliğin olması gerekiyor. Merak etme, senin için en avantajlı "
        "planları aşağıda listeledik.\n\n"
        "🚀 Hemen başlamak için bir plan seç:"
    ),
}

#----- settings key -> Backend.config.Telegram attribute adı
#----- (update() sırasında bu attribute'lar canlı olarak yamalanır)
_SETTINGS_TO_TELEGRAM_ATTR: Dict[str, str] = {
    "replace_mode": "REPLACE_MODE",
    "hide_catalog": "HIDE_CATALOG",
    "auth_channels": "AUTH_CHANNEL",
    "tmdb_api": "TMDB_API",
    "deepl_api": "DEEPL_API",
    "deepl_api_keys": "DEEPL_API_KEYS",
    "base_url": "BASE_URL",
    "upstream_repo": "UPSTREAM_REPO",
    "upstream_branch": "UPSTREAM_BRANCH",
    "isim": "ISIM",
    "eklenti_aciklamasi": "EKLENTI_ACIKLAMASI",
    "eklenti_logosu": "EKLENTI_LOGOSU",
    "bolum_resimi": "BOLUM_RESIMI",
    "max_concurrent_downloads": "MAX_CONCURRENT_DOWNLOADS",
    "max_concurrent_uploads": "MAX_CONCURRENT_UPLOADS",
    "proxy": "PROXY",
    "proxy_type": "PROXY_TYPE",
    "http_proxy_url": "HTTP_PROXY_URL",
    "proxy_mode": "PROXY_MODE",
    "proxy_scope_mode": "PROXY_SCOPE_MODE",
    "proxy_scope_member_ids": "PROXY_SCOPE_MEMBER_IDS",
    "default_device_limit": "DEFAULT_DEVICE_LIMIT",
    "member_bot_limit": "MEMBER_BOT_LIMIT",
    "credential_rotate_days": "CREDENTIAL_ROTATE_DAYS",
    "yenileme": "YENILEME",
    "hiz_limiti": "HIZ_LIMITI",
    "limit_sifirlama": "LIMIT_SIFIRLAMA",
    "subscription": "SUBSCRIPTION",
    "subscription_group_id": "SUBSCRIPTION_GROUP_ID",
    "subscription_url": "SUBSCRIPTION_URL",
    "approver_ids": "APPROVER_IDS",
    "approval_account_id": "APPROVAL_ACCOUNT_ID",
    "websitesi": "WEBSITESI",
    "brute_window": "BRUTE_WINDOW",
    "brute_max": "BRUTE_MAX",
    "brute_ban": "BRUTE_BAN",
    "parallel": "PARALLEL",
    "pre_fetch": "PRE_FETCH",
    "trusted_proxy_cidrs": "TRUSTED_PROXY_CIDRS",
    "webdav_sync_interval_hours": "WEBDAV_SYNC_INTERVAL_HOURS",
    "cloud_sync_interval_hours": "CLOUD_SYNC_INTERVAL_HOURS",
}


#----- İlk açılışta config.env / ortam değişkenlerinden tohumlama
def _seed_from_env() -> Dict[str, Any]:
    seed = dict(_DEFAULTS)
    seed.update({
        "replace_mode":         Telegram.REPLACE_MODE,
        "hide_catalog":         Telegram.HIDE_CATALOG,
        "auth_channels":        list(Telegram.AUTH_CHANNEL),
        "tmdb_api":             Telegram.TMDB_API,
        "deepl_api":            Telegram.DEEPL_API,
        "deepl_api_keys":       list(Telegram.DEEPL_API_KEYS),
        "base_url":             Telegram.BASE_URL,
        "upstream_repo":        Telegram.UPSTREAM_REPO,
        "upstream_branch":      Telegram.UPSTREAM_BRANCH,
        "isim":                 Telegram.ISIM,
        "eklenti_aciklamasi":   Telegram.EKLENTI_ACIKLAMASI,
        "eklenti_logosu":       Telegram.EKLENTI_LOGOSU,
        "bolum_resimi":         Telegram.BOLUM_RESIMI,
        "max_concurrent_downloads": Telegram.MAX_CONCURRENT_DOWNLOADS,
        "max_concurrent_uploads":   Telegram.MAX_CONCURRENT_UPLOADS,
        "proxy":                Telegram.PROXY,
        "proxy_type":           Telegram.PROXY_TYPE,
        "http_proxy_url":       Telegram.HTTP_PROXY_URL,
        "proxy_mode":           Telegram.PROXY_MODE,
        "proxy_scope_mode":       Telegram.PROXY_SCOPE_MODE,
        "proxy_scope_member_ids": list(Telegram.PROXY_SCOPE_MEMBER_IDS),
        "default_device_limit": Telegram.DEFAULT_DEVICE_LIMIT,
        "member_bot_limit":     Telegram.MEMBER_BOT_LIMIT,
        "credential_rotate_days": Telegram.CREDENTIAL_ROTATE_DAYS,
        "yenileme":             Telegram.YENILEME,
        "hiz_limiti":           Telegram.HIZ_LIMITI,
        "limit_sifirlama":      Telegram.LIMIT_SIFIRLAMA,
        "subscription":         Telegram.SUBSCRIPTION,
        "subscription_group_id": Telegram.SUBSCRIPTION_GROUP_ID,
        "subscription_url":     Telegram.SUBSCRIPTION_URL,
        "approver_ids":         list(Telegram.APPROVER_IDS),
        "approval_account_id":  Telegram.APPROVAL_ACCOUNT_ID,
        "websitesi":            Telegram.WEBSITESI,
        "brute_window":         Telegram.BRUTE_WINDOW,
        "brute_max":            Telegram.BRUTE_MAX,
        "brute_ban":            Telegram.BRUTE_BAN,
        "parallel":             Telegram.PARALLEL,
        "pre_fetch":            Telegram.PRE_FETCH,
        "trusted_proxy_cidrs":  Telegram.TRUSTED_PROXY_CIDRS,
        "webdav_sync_interval_hours": Telegram.WEBDAV_SYNC_INTERVAL_HOURS,
        "cloud_sync_interval_hours": Telegram.CLOUD_SYNC_INTERVAL_HOURS,
        "extra_databases":      list(Telegram.DATABASE[2:]) if len(Telegram.DATABASE) > 2 else [],
        "multi_tokens":         [],
    })
    return seed


#----- Bot token'ını panelde göstermek için maskeler: 123456:ABCDEF -> 123456:AB••••EF
def mask_bot_token(token: str) -> str:
    token = (token or "").strip()
    if ":" not in token:
        return "•" * len(token)
    bot_id, _, secret = token.partition(":")
    if len(secret) <= 4:
        return f"{bot_id}:{'•' * len(secret)}"
    return f"{bot_id}:{secret[:2]}{'•' * (len(secret) - 4)}{secret[-2:]}"


#----- config.env'de tanımlı MULTI_TOKEN_x değişkenlerini (maskelenmiş) listeler.
#----- Bunlar ayarlar sayfasından eklenip çıkarılamaz (salt okunur bilgi amaçlıdır),
#----- gerçek istemci başlatma mantığı hâlâ Backend.pyrofork.clients.TokenParser'da.
def get_env_multi_tokens() -> List[Dict[str, str]]:
    try:
        env_tokens = sorted(
            (name, value) for name, value in os.environ.items()
            if name.startswith("MULTI_TOKEN") and value.strip()
        )
        return [{"name": name, "masked": mask_bot_token(value)} for name, value in env_tokens]
    except Exception:
        return []


#----- Değişmez ayar anlık görüntüsü (snapshot)
class Settings:
    __slots__ = ("_d",)

    def __init__(self, data: Dict[str, Any]) -> None:
        merged = dict(_DEFAULTS)
        merged.update({k: v for k, v in data.items() if k != "_id"})
        self._d = merged

    def __getattr__(self, item: str) -> Any:
        try:
            return self._d[item]
        except KeyError:
            raise AttributeError(item)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._d)


#----- Ayarların tekil kaynak (singleton) yöneticisi
class SettingsManager:
    _current: "Settings | None" = None

    #----- DB'den yükle; yoksa config.env'den tohumla
    @classmethod
    async def initialize(cls, db) -> None:
        try:
            raw = await db.get_settings()
        except Exception as exc:
            LOGGER.error(f"SettingsManager.initialize: DB okuma hatası: {exc}")
            raw = {}

        if not raw:
            LOGGER.info("SettingsManager: DB'de ayar bulunamadı — config.env'den tohumlanıyor.")
            seed = _seed_from_env()
            try:
                await db.save_settings(seed)
            except Exception as exc:
                LOGGER.error(f"SettingsManager.initialize: DB kayıt hatası: {exc}")
            cls._current = Settings(seed)
        else:
            cls._current = Settings(raw)

        #----- Yüklenen değerleri Telegram sınıfına uygula (mevcut kod tabanı bunları kullanıyor)
        cls._apply_to_telegram(cls._current.to_dict())
        LOGGER.info("SettingsManager: ayarlar başarıyla yüklendi.")

    @classmethod
    async def reload(cls, db) -> None:
        raw = await db.get_settings()
        if raw:
            cls._current = Settings(raw)
            cls._apply_to_telegram(cls._current.to_dict())

    @classmethod
    def current(cls) -> Settings:
        if cls._current is None:
            return Settings({})
        return cls._current

    #----- Yeni değerleri kaydet, snapshot'ı güncelle, bağımlı bileşenleri yeniden başlat
    @classmethod
    async def update(cls, db, new_values: Dict[str, Any]) -> Dict[str, str]:
        old = cls.current().to_dict()
        merged = dict(old)
        merged.update({k: v for k, v in new_values.items() if k in _DEFAULTS})

        #----- DeepL anahtarlari: temizle, tekrarlari at; "deepl_api" = ilk anahtar
        if "deepl_api_keys" in new_values or "deepl_api" in new_values:
            from Backend.helper.deepl_keys import split_keys, prune
            raw = new_values["deepl_api_keys"] if "deepl_api_keys" in new_values else new_values["deepl_api"]
            keys = split_keys(raw)
            merged["deepl_api_keys"] = keys
            merged["deepl_api"] = keys[0] if keys else ""
            prune(keys)   # listeden cikarilan anahtarlarin durumunu unut

        #----- Üye başına bot sayısı: tam sayı, en az 1
        if "member_bot_limit" in new_values:
            try:
                mbl = int(str(new_values["member_bot_limit"]).strip())
            except (TypeError, ValueError):
                raise ValueError("Üye başına bot sayısı tam sayı olmalıdır (en az 1).")
            if mbl < 1 or mbl > 100:
                raise ValueError("Üye başına bot sayısı 1 ile 100 arasında olmalıdır.")
            merged["member_bot_limit"] = mbl

        #----- Şifre geçerlilik süresi (gün): tam sayı, 0 = kapalı
        if "credential_rotate_days" in new_values:
            try:
                crd = int(str(new_values["credential_rotate_days"]).strip())
            except (TypeError, ValueError):
                raise ValueError("Şifre geçerlilik süresi tam sayı olmalıdır (gün; 0 = kapalı).")
            if crd < 0 or crd > 3650:
                raise ValueError("Şifre geçerlilik süresi 0 ile 3650 gün arasında olmalıdır (0 = kapalı).")
            merged["credential_rotate_days"] = crd

        #----- WebDAV senkron aralığı: sayı olmalı, negatif olamaz (0 = kapalı)
        if "webdav_sync_interval_hours" in new_values:
            try:
                hours = float(str(new_values["webdav_sync_interval_hours"]).strip().replace(",", "."))
            except (TypeError, ValueError):
                raise ValueError("WebDAV senkron aralığı sayı olmalıdır (saat; 0 = kapalı).")
            if hours < 0 or hours > 24 * 365:
                raise ValueError("WebDAV senkron aralığı 0 ile 8760 saat arasında olmalıdır (0 = kapalı).")
            merged["webdav_sync_interval_hours"] = int(hours) if hours == int(hours) else hours

        #----- rclone/Drive senkron aralığı: sayı olmalı, negatif olamaz (0 = kapalı)
        if "cloud_sync_interval_hours" in new_values:
            try:
                hours = float(str(new_values["cloud_sync_interval_hours"]).strip().replace(",", "."))
            except (TypeError, ValueError):
                raise ValueError("rclone/Drive senkron aralığı sayı olmalıdır (saat; 0 = kapalı).")
            if hours < 0 or hours > 24 * 365:
                raise ValueError("rclone/Drive senkron aralığı 0 ile 8760 saat arasında olmalıdır (0 = kapalı).")
            merged["cloud_sync_interval_hours"] = int(hours) if hours == int(hours) else hours

        #----- Yönetici kullanıcı adı: "@" atılır, Telegram kuralına uymalı (boş olabilir)
        if "admin_username" in new_values:
            uname = normalize_admin_username(new_values["admin_username"])
            if uname and not _USERNAME_RE.match(uname):
                raise ValueError(
                    "Yönetici kullanıcı adı geçersiz. 5-32 karakter olmalı, harfle başlamalı; "
                    "yalnızca harf, rakam ve alt çizgi içerebilir."
                )
            merged["admin_username"] = uname

        results: Dict[str, str] = {}

        #----- Ana yönetici (OWNER_ID) listeden çıkarılamaz — sunucu tarafında zorlanır
        merged["approver_ids"] = normalize_approver_ids(merged.get("approver_ids"))

        #----- Onay hesabı: yönetici listesinde olmalı; aksi halde 0 (= ana yönetici)
        try:
            chosen = int(str(merged.get("approval_account_id") or 0).strip() or 0)
        except (TypeError, ValueError):
            raise ValueError("Onay hesabı geçerli bir Telegram ID olmalıdır.")
        #----- (seçili kişi yöneticilerden çıkarıldıysa otomatik olarak ana yöneticiye döner)
        if chosen == int(Telegram.OWNER_ID or 0) or chosen not in merged["approver_ids"]:
            chosen = 0
        merged["approval_account_id"] = chosen

        #----- Ek veritabanları değiştiyse önce onları bağla/ayır (başarısızsa kayıt iptal)
        old_extra = old.get("extra_databases") or []
        new_extra = merged.get("extra_databases") or []
        if old_extra != new_extra:
            try:
                result = await db.reload_extra_databases(new_extra)
                results["databases"] = result.get("message", "veritabanları güncellendi")
            except Exception as exc:
                LOGGER.error(f"SettingsManager.update: reload_extra_databases hatası: {exc}")
                results["databases"] = f"hata: {exc}"
                merged["extra_databases"] = old_extra  # kaydetme, eski değere dön

        #----- Kaydet ve anlık görüntüyü güncelle
        await db.save_settings(merged)
        cls._current = Settings(merged)
        cls._apply_to_telegram(merged)

        #----- Bağımlı bileşenleri (varsa) yeniden başlat
        results.update(await cls._reinit_dependent(old, merged))

        return results

    #----- settings dict'ini Backend.config.Telegram attribute'larına yansıt
    @classmethod
    def _apply_to_telegram(cls, data: Dict[str, Any]) -> None:
        for key, attr in _SETTINGS_TO_TELEGRAM_ATTR.items():
            if key in data:
                value = data[key]
                if key == "approver_ids":
                    value = normalize_approver_ids(value)
                setattr(Telegram, attr, value)

    @classmethod
    async def _reinit_dependent(cls, old: dict, new: dict) -> Dict[str, str]:
        results: Dict[str, str] = {}

        #----- Çoklu token istemcileri değiştiyse hot-reload
        old_tokens = old.get("multi_tokens") or []
        new_tokens = new.get("multi_tokens") or []
        if old_tokens != new_tokens:
            try:
                from Backend.pyrofork.clients import reload_multi_token_clients
                result = await reload_multi_token_clients()
                results["multi_tokens"] = (
                    f"{result['started']} başlatıldı, {result['stopped']} durduruldu "
                    f"({result['total_clients']} aktif istemci)"
                )
            except Exception as exc:
                LOGGER.error(f"SettingsManager reinit multi_tokens: {exc}")
                results["multi_tokens"] = f"hata: {exc}"

        #----- Abonelik açıldı/kapandı → arka plan görevini başlat/durdur
        if old.get("subscription") != new.get("subscription"):
            try:
                if new.get("subscription"):
                    from Backend.helper.subscription_checker import subscription_checker_loop
                    from Backend.pyrofork.bot import StreamBot
                    import asyncio
                    asyncio.create_task(subscription_checker_loop(StreamBot))
                    results["subscription"] = "abonelik kontrol görevi başlatıldı"
                else:
                    results["subscription"] = "abonelik kapatıldı (görev bir sonraki döngüde duracak)"
            except Exception as exc:
                LOGGER.error(f"SettingsManager reinit subscription: {exc}")
                results["subscription"] = f"hata: {exc}"

        #----- Proxy ayarları değiştiyse bilgi ver
        proxy_keys = {"proxy", "proxy_type", "http_proxy_url", "proxy_mode"}
        if any(old.get(k) != new.get(k) for k in proxy_keys):
            results["proxy"] = "güncellendi — sonraki isteklerde geçerli olacak"

        #----- WebDAV senkron aralığı değiştiyse arka plan görevini yeniden kur
        if old.get("webdav_sync_interval_hours") != new.get("webdav_sync_interval_hours"):
            try:
                from Backend.helper.webdav_sync import configure_sync_interval
                results["webdav_sync"] = configure_sync_interval(
                    new.get("webdav_sync_interval_hours"), startup=False
                )
            except Exception as exc:
                LOGGER.error(f"SettingsManager reinit webdav_sync: {exc}")
                results["webdav_sync"] = f"hata: {exc}"

        #----- rclone/Drive senkron aralığı değiştiyse arka plan görevini yeniden kur
        if old.get("cloud_sync_interval_hours") != new.get("cloud_sync_interval_hours"):
            try:
                from Backend.helper.cloud_sync import configure_cloud_sync_interval
                results["cloud_sync"] = configure_cloud_sync_interval(
                    new.get("cloud_sync_interval_hours"), startup=False
                )
            except Exception as exc:
                LOGGER.error(f"SettingsManager reinit cloud_sync: {exc}")
                results["cloud_sync"] = f"hata: {exc}"

        #----- TRUSTED_PROXY_CIDRS değiştiyse brute-force modülündeki canlı
        #----- listeyi hemen güncelle (aksi halde process yeniden başlamadan
        #----- eski/boş liste kullanılmaya devam eder ve gerçek IP tespiti
        #----- yine bozuk kalır).
        if old.get("trusted_proxy_cidrs") != new.get("trusted_proxy_cidrs"):
            try:
                from Backend.fastapi.security.brute_force import set_trusted_proxies
                count = set_trusted_proxies(new.get("trusted_proxy_cidrs") or "")
                results["trusted_proxy_cidrs"] = (
                    f"{count} güvenilir proxy aralığı yüklendi" if count
                    else "güvenilir proxy tanımlı değil (X-Forwarded-For'a güvenilmeyecek)"
                )
            except Exception as exc:
                LOGGER.error(f"SettingsManager reinit trusted_proxy_cidrs: {exc}")
                results["trusted_proxy_cidrs"] = f"hata: {exc}"

        return results
