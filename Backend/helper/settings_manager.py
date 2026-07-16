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
from typing import Any, Dict, List

from Backend.config import Telegram
from Backend.logger import LOGGER

#----- Panelden yönetilebilir ayarların varsayılan değerleri
_DEFAULTS: Dict[str, Any] = {
    "replace_mode": True,
    "hide_catalog": False,
    "auth_channels": [],
    "tmdb_api": "",
    "base_url": "",
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
    "default_device_limit": 0,
    "yenileme": "",
    "hiz_limiti": "",
    "limit_sifirlama": "",
    "subscription": False,
    "subscription_group_id": 0,
    "subscription_url": "https://t.me/",
    "approver_ids": [],
    "websitesi": False,
    "brute_window": 60,
    "brute_max": 5,
    "brute_ban": 1800,
    "extra_databases": [],
    "multi_tokens": [],
    "announce_new_content": False,
    "announcement_channel": "",
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
    "default_device_limit": "DEFAULT_DEVICE_LIMIT",
    "yenileme": "YENILEME",
    "hiz_limiti": "HIZ_LIMITI",
    "limit_sifirlama": "LIMIT_SIFIRLAMA",
    "subscription": "SUBSCRIPTION",
    "subscription_group_id": "SUBSCRIPTION_GROUP_ID",
    "subscription_url": "SUBSCRIPTION_URL",
    "approver_ids": "APPROVER_IDS",
    "websitesi": "WEBSITESI",
    "brute_window": "BRUTE_WINDOW",
    "brute_max": "BRUTE_MAX",
    "brute_ban": "BRUTE_BAN",
}


#----- İlk açılışta config.env / ortam değişkenlerinden tohumlama
def _seed_from_env() -> Dict[str, Any]:
    seed = dict(_DEFAULTS)
    seed.update({
        "replace_mode":         Telegram.REPLACE_MODE,
        "hide_catalog":         Telegram.HIDE_CATALOG,
        "auth_channels":        list(Telegram.AUTH_CHANNEL),
        "tmdb_api":             Telegram.TMDB_API,
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
        "default_device_limit": Telegram.DEFAULT_DEVICE_LIMIT,
        "yenileme":             Telegram.YENILEME,
        "hiz_limiti":           Telegram.HIZ_LIMITI,
        "limit_sifirlama":      Telegram.LIMIT_SIFIRLAMA,
        "subscription":         Telegram.SUBSCRIPTION,
        "subscription_group_id": Telegram.SUBSCRIPTION_GROUP_ID,
        "subscription_url":     Telegram.SUBSCRIPTION_URL,
        "approver_ids":         list(Telegram.APPROVER_IDS),
        "websitesi":            Telegram.WEBSITESI,
        "brute_window":         Telegram.BRUTE_WINDOW,
        "brute_max":            Telegram.BRUTE_MAX,
        "brute_ban":            Telegram.BRUTE_BAN,
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

        results: Dict[str, str] = {}

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
                setattr(Telegram, attr, data[key])

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

        return results
