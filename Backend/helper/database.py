import secrets
import string
from asyncio import create_task
from bson import ObjectId
import motor.motor_asyncio
from datetime import datetime, timezone, timedelta as _td
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python 3.8
_TZ_IST = ZoneInfo("Europe/Istanbul")

def _daily_key() -> str:
    """
    Gunluk sifirlamanin hangi 'sanal gun' icinde oldugunu hesaplar.
    LIMIT_SIFIRLAMA=HH:MM (Turkiye saati) alinir; o saat gecmisse
    bir sonraki gune ait tarih 'bugunun anahtari' sayilir.
    Ornek: 21:35 — saat 21:34 ise bugunun tarihi, 21:36 ise yarinki tarih.
    """
    from Backend.config import Telegram as _Cfg
    _raw = (_Cfg.LIMIT_SIFIRLAMA or "").strip()
    try:
        _rh, _rm = (int(x) for x in _raw.split(":"))
    except Exception:
        _rh, _rm = 0, 0  # varsayilan: gece 00:00

    now = datetime.now(_TZ_IST)
    threshold = now.replace(hour=_rh, minute=_rm, second=0, microsecond=0)
    if now >= threshold:
        # Sifirlanma saati gecmis → bir sonraki "gun" basladi, yarinki tarihi kullan
        return (now + _td(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")
from pydantic import ValidationError
from pymongo import ASCENDING, DESCENDING, TEXT
from typing import Dict, List, Optional, Tuple, Any

from Backend.logger import LOGGER
from Backend.config import Telegram
import re
from Backend.helper.encrypt import decode_string
from Backend.helper.modal import Episode, MovieSchema, QualityDetail, QualityPart, Season, TVShowSchema
from Backend.helper.task_manager import delete_message


def is_proxy_scope_member(user_id) -> bool:
    """
    Ayarlar sayfasındaki "Proxy Kimlere Uygulansın?" seçimine göre, verilen
    üyenin (user_id) proxy kapsamında olup olmadığını belirler.

    Backend.config.Telegram.PROXY_SCOPE_MODE / PROXY_SCOPE_MEMBER_IDS
    SettingsManager tarafından panelden canlı olarak güncellenir (bkz.
    Backend/helper/settings_manager.py). mode="subscribers" (varsayılan)
    ise tüm üyeler kapsamdadır; mode="selected" ise yalnızca
    PROXY_SCOPE_MEMBER_IDS'teki üyeler kapsamdadır — kapsam dışı üyeler
    Proxy Modu ne olursa olsun her zaman doğrudan (proxy'siz) link alır.
    """
    from Backend.config import Telegram
    if getattr(Telegram, "PROXY_SCOPE_MODE", "subscribers") != "selected":
        return True
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    try:
        allowed_ids = {int(m) for m in (getattr(Telegram, "PROXY_SCOPE_MEMBER_IDS", None) or [])}
    except (TypeError, ValueError):
        allowed_ids = set()
    return uid in allowed_ids


def is_media_visible_to_member(media_doc: Optional[Dict[str, Any]], user_id) -> bool:
    """
    Bir içerik dokümanının 'visibility' alanına göre, verilen üyenin (user_id)
    bu içeriği görüp göremeyeceğini / erişip erişemeyeceğini belirler.

    visibility şeması:
      {"mode": "subscribers", "member_ids": []}          → varsayılan: aktif
                                                              aboneliği olan tüm
                                                              üyelere açık
      {"mode": "selected",    "member_ids": [123, 456]}  → yalnızca listedeki
                                                              üye ID'lerine açık

    'visibility' alanı hiç tanımlı değilse (eski kayıtlar) → herkese açık kabul edilir.
    Abonelik aktifliği kontrolü bu fonksiyonun kapsamı dışındadır; çağıran taraf
    (_check_subscription vb.) ayrıca kontrol etmelidir.
    """
    vis = (media_doc or {}).get("visibility") or {}
    if vis.get("mode") != "selected":
        return True
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    try:
        allowed_ids = {int(m) for m in (vis.get("member_ids") or [])}
    except (TypeError, ValueError):
        allowed_ids = set()
    return uid in allowed_ids


def convert_objectid_to_str(document: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in document.items():
        if isinstance(value, ObjectId):
            document[key] = str(value)
        elif isinstance(value, list):
            document[key] = [convert_objectid_to_str(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            document[key] = convert_objectid_to_str(value)
    return document



# ── Güvenli şifre hash yardımcıları ──────────────────────────────────────────
# scrypt (RFC 7914) — salt'lı, GPU-dirençli, hashlib built-in (Python 3.6+)
# Format: "scrypt$<salt_hex>$<hash_hex>"
# Eski format: düz sha256 hexdigest (32 byte) — geriye uyumluluk için verify'da tanınır

import hashlib as _pw_hashlib
import secrets as _pw_secrets

_SCRYPT_N = 2**14   # CPU/bellek maliyeti (üretim için 2**16 önerilir; OTP için 2**14 yeterli)
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32  # 256-bit çıktı


def _hash_password(password: str) -> str:
    """Yeni kayıt için scrypt hash üretir. Dönen format: 'scrypt$<salt>$<hash>'."""
    salt = _pw_secrets.token_hex(16)           # 128-bit rastgele salt
    dk = _pw_hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt),
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${salt}${dk.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    """
    Sabit zamanlı şifre doğrulama.
    Hem yeni 'scrypt$...' formatını hem eski SHA-256 formatını destekler.
    """
    if stored_hash.startswith("scrypt$"):
        parts = stored_hash.split("$")
        if len(parts) != 3:
            return False
        _, salt_hex, expected_hex = parts
        try:
            dk = _pw_hashlib.scrypt(
                password.encode(),
                salt=bytes.fromhex(salt_hex),
                n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                dklen=_SCRYPT_DKLEN,
            )
            return _pw_secrets.compare_digest(dk.hex(), expected_hex)
        except Exception:
            return False
    else:
        # Eski format: düz SHA-256 — geriye uyumluluk
        legacy = _pw_hashlib.sha256(password.encode()).hexdigest()
        return _pw_secrets.compare_digest(legacy, stored_hash)



class Database:
    def __init__(self, db_name: str = "dbFyvio"):
        self.db_uris = Telegram.DATABASE
        self.db_name = db_name

        if len(self.db_uris) < 2:
            raise ValueError("At least 2 database URIs are required (1 for tracking + 1 for storage).")

        self.clients: Dict[str, motor.motor_asyncio.AsyncIOMotorClient] = {}
        self.dbs: Dict[str, motor.motor_asyncio.AsyncIOMotorDatabase] = {}

        self.current_db_index = 1

    async def connect(self):
        try:
            for index, uri in enumerate(self.db_uris):
                client = motor.motor_asyncio.AsyncIOMotorClient(uri, maxPoolSize=10, minPoolSize=1)
                db_key = "tracking" if index == 0 else f"storage_{index}"
                self.clients[db_key] = client
                self.dbs[db_key] = client[self.db_name]
                db_type = "Tracking" if index == 0 else f"Storage {index}"

                masked_uri = re.sub(r"://(.*?):.*?@", r"://\1:*****@", uri)
                masked_uri = masked_uri.split('?')[0]
                
                LOGGER.info(f"{db_type} Database connected successfully: {masked_uri}")

            state = await self.dbs["tracking"]["state"].find_one({"_id": "db_index"})
            if not state:
                await self.dbs["tracking"]["state"].insert_one({"_id": "db_index", "current_index": 1})
                self.current_db_index = 1
            else:
                self.current_db_index = state["current_index"]

            LOGGER.info(f"Active storage DB: storage_{self.current_db_index}")

            # member_sessions koleksiyonu için unique index
            try:
                await self.dbs["tracking"]["member_sessions"].create_index(
                    "otp_username", unique=True, background=True
                )
                await self.dbs["tracking"]["member_sessions"].create_index(
                    "user_id", unique=True, background=True
                )
                # 72 saatte otomatik TTL temizliği (session_expires null olanlar etkilenmez)
                await self.dbs["tracking"]["member_sessions"].create_index(
                    "session_expires",
                    expireAfterSeconds=0,
                    sparse=True,
                    background=True
                )
            except Exception as idx_err:
                LOGGER.warning(f"member_sessions index: {idx_err}")

            # admin_sessions koleksiyonu için unique index (singleton _id="admin")
            try:
                await self.dbs["tracking"]["admin_sessions"].create_index(
                    "otp_username", unique=True, sparse=True, background=True
                )
            except Exception as idx_err:
                LOGGER.warning(f"admin_sessions index: {idx_err}")

            # ip_bans koleksiyonu: ban_until alanına TTL index (MongoDB otomatik temizler)
            try:
                await self.dbs["tracking"]["ip_bans"].create_index(
                    "ban_until",
                    expireAfterSeconds=0,
                    background=True,
                )
                await self.dbs["tracking"]["ip_bans"].create_index(
                    "ip", unique=True, background=True
                )
            except Exception as idx_err:
                LOGGER.warning(f"ip_bans index: {idx_err}")

            # stream_analytics: TTL'yi 30 güne ayarla, mevcut eski index'i yeniden oluştur
            # (Temizlik işlemi artık sadece db_scheduler.py üzerinden yapılır)
            try:
                col_analytics = self.dbs["tracking"]["stream_analytics"]
                # Eski TTL index'ini sil (farklı expireAfterSeconds ile yeniden oluşturmak için)
                try:
                    await col_analytics.drop_index("logged_at_1")
                except Exception:
                    pass
                await col_analytics.create_index(
                    "logged_at",
                    expireAfterSeconds=30 * 24 * 3600,  # 30 gün
                    background=True,
                )
            except Exception as idx_err:
                LOGGER.warning(f"stream_analytics TTL index: {idx_err}")

            # tv/movie arama text index'leri — her storage DB için ayrı ayrı
            for index in range(1, len(self.db_uris)):
                db_key = f"storage_{index}"
                if db_key in self.dbs:
                    await self._ensure_search_indexes(self.dbs[db_key])

        except Exception as e:
            LOGGER.error(f"Database connection error: {e}")

    async def _ensure_search_indexes(self, db) -> None:
        """
        tv ve movie koleksiyonlarına arama (search_documents) için text index
        oluşturur. default_language="none" ile stemming/stop-word filtresi
        kapatılır — çok dilli (tr/de/en) başlıklarda yanlış kelime kökü
        eşleştirmesi yapılmaması için. Index zaten varsa create_index no-op'tur.
        """
        try:
            await db["tv"].create_index(
                [
                    ("title", TEXT),
                    ("title_tr", TEXT),
                    ("title_de", TEXT),
                    ("cast", TEXT),
                    ("seasons.episodes.telegram.name", TEXT),
                ],
                name="search_text_idx",
                weights={
                    "title": 10,
                    "title_tr": 10,
                    "title_de": 8,
                    "cast": 3,
                    "seasons.episodes.telegram.name": 1,
                },
                default_language="none",
                background=True,
            )
        except Exception as idx_err:
            LOGGER.warning(f"tv search_text_idx: {idx_err}")

        try:
            await db["movie"].create_index(
                [
                    ("title", TEXT),
                    ("title_tr", TEXT),
                    ("title_de", TEXT),
                    ("cast", TEXT),
                    ("telegram.name", TEXT),
                ],
                name="search_text_idx",
                weights={
                    "title": 10,
                    "title_tr": 10,
                    "title_de": 8,
                    "cast": 3,
                    "telegram.name": 1,
                },
                default_language="none",
                background=True,
            )
        except Exception as idx_err:
            LOGGER.warning(f"movie search_text_idx: {idx_err}")

        # ── updated_on index (varsayılan sıralama için) ──────────────────────
        # _get_sort_dict() arama yokken {"updated_on": DESCENDING} kullanıyor.
        # Index olmadan bu sıralama tamamen bellekte yapılır ve büyük
        # koleksiyonlarda (özellikle nested seasons/episodes içeren "tv"
        # koleksiyonunda) MongoDB'nin 32MB in-memory sort limitini aşarak
        # "Sort exceeded memory limit" hatasına -> 500 Internal Server Error'a
        # yol açar. Bu genelde en dolu/eski storage DB'sine denk gelen son
        # sayfalarda görülür (bkz. _paginate_collection).
        try:
            await db["tv"].create_index(
                [("updated_on", DESCENDING)],
                name="updated_on_idx",
                background=True,
            )
        except Exception as idx_err:
            LOGGER.warning(f"tv updated_on_idx: {idx_err}")

        try:
            await db["movie"].create_index(
                [("updated_on", DESCENDING)],
                name="updated_on_idx",
                background=True,
            )
        except Exception as idx_err:
            LOGGER.warning(f"movie updated_on_idx: {idx_err}")

    async def disconnect(self):
        for client in self.clients.values():
            client.close()
        LOGGER.info("All database connections closed.")

    async def update_current_db_index(self):
        await self.dbs["tracking"]["state"].update_one(
            {"_id": "db_index"},
            {"$set": {"current_index": self.current_db_index}},
            upsert=True
        )

    #-----
    #----- Ayarlar (SettingsManager tarafından kullanılır)
    #-----
    async def get_settings(self) -> dict:
        try:
            doc = await self.dbs["tracking"]["settings"].find_one({"_id": "app_settings"})
            return doc or {}
        except Exception as e:
            LOGGER.error(f"Database.get_settings hatası: {e}")
            return {}

    async def save_settings(self, settings: dict) -> bool:
        try:
            clean = {k: v for k, v in settings.items() if k != "_id"}
            await self.dbs["tracking"]["settings"].update_one(
                {"_id": "app_settings"},
                {"$set": clean},
                upsert=True,
            )
            return True
        except Exception as e:
            LOGGER.error(f"Database.save_settings hatası: {e}")
            return False

    #-----
    #----- Ek veritabanı bağlantı yönetimi (Ayarlar → Veritabanları bölümü)
    #-----
    def get_database_list(self) -> List[Dict[str, Any]]:
        result = []
        for index, uri in enumerate(self.db_uris):
            masked = re.sub(r"://(.*?):.*?@", r"://\1:*****@", uri).split('?')[0]
            db_key = "tracking" if index == 0 else f"storage_{index}"
            entry = {
                "index": index,
                "uri_masked": masked,
                "locked": index <= 1,  # ilk iki DB (tracking + storage_1) config.env'den gelir, silinemez
                "type": "tracking" if index == 0 else "storage",
                "connected": db_key in self.clients,
            }
            if index > 1:
                entry["full_uri"] = uri
            result.append(entry)
        return result

    async def connect_storage_db(self, uri: str, index: int) -> bool:
        try:
            client = motor.motor_asyncio.AsyncIOMotorClient(uri, maxPoolSize=10, minPoolSize=1)
            await client.admin.command("ping")

            db_key = "tracking" if index == 0 else f"storage_{index}"
            self.clients[db_key] = client
            self.dbs[db_key] = client[self.db_name]

            db_type = "Tracking" if index == 0 else f"Storage {index}"
            masked_uri = re.sub(r"://(.*?):.*?@", r"://\1:*****@", uri).split('?')[0]
            LOGGER.info(f"{db_type} veritabanı bağlandı: {masked_uri}")

            if index != 0:
                await self._ensure_search_indexes(self.dbs[db_key])

            return True
        except Exception as e:
            LOGGER.error(f"connect_storage_db hatası (index {index}): {e}")
            return False

    async def disconnect_storage_db(self, index: int) -> None:
        db_key = f"storage_{index}"
        client = self.clients.pop(db_key, None)
        self.dbs.pop(db_key, None)
        if client:
            client.close()
            LOGGER.info(f"{db_key} bağlantısı kapatıldı.")

    async def reload_extra_databases(self, new_extra_uris: List[str]) -> Dict[str, Any]:
        """Ayarlar sayfasından 2. sıradan sonraki (ek) veritabanlarını günceller.
        Var olan storage_2, storage_3... konumlarındaki URI'ler DEĞİŞTİRİLEMEZ
        (mevcut medya kayıtları bu index'lere göre saklanır); sadece sona
        ekleme veya en sondakini kaldırma desteklenir."""
        old_extra = self.db_uris[2:]
        new_extra = [u.strip() for u in (new_extra_uris or []) if u and u.strip()]

        common_len = min(len(old_extra), len(new_extra))
        for i in range(common_len):
            if old_extra[i] != new_extra[i]:
                raise ValueError(
                    f"storage_{i + 2} konumundaki veritabanı yerinde değiştirilemez — "
                    f"mevcut medya kayıtları bu index'e göre saklanıyor. "
                    f"Sadece sona ekleme veya en sonuncuları kaldırma desteklenir."
                )

        added = 0
        removed = 0

        if len(new_extra) > len(old_extra):
            for offset, uri in enumerate(new_extra[len(old_extra):]):
                index = len(old_extra) + 2 + offset
                ok = await self.connect_storage_db(uri, index)
                if not ok:
                    raise ValueError(
                        f"storage_{index} bağlantısı kurulamadı. URI'yi kontrol edin — "
                        f"hiçbir değişiklik kaydedilmedi."
                    )
                added += 1
        elif len(new_extra) < len(old_extra):
            for index in range(len(old_extra) + 1, len(new_extra) + 1, -1):
                await self.disconnect_storage_db(index)
                removed += 1

        self.db_uris = self.db_uris[:2] + new_extra

        message = f"{added} veritabanı eklendi, {removed} veritabanı kaldırıldı."
        LOGGER.info(f"reload_extra_databases: {message}")
        return {"added": added, "removed": removed, "message": message}

    # -------------------------------
    # User Subscription Management
    # -------------------------------
    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self.dbs["tracking"]["users"].find_one({"_id": user_id})

    async def update_user_interaction(self, user_id: int, first_name: str, username: str):
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": {"first_name": first_name, "username": username, "last_interaction": datetime.utcnow()}},
            upsert=True
        )

    async def set_pending_payment(self, user_id: int, plan_duration: int, msg_id: int, price=0, currency: str = "TRY", label: str = "", admin_messages: list = None, plan_id: str = ""):
        update_data = {
            "pending_payment": {
                "duration": plan_duration,
                "price": price,
                "currency": currency,
                "label": label,
                "plan_id": plan_id,
                "msg_id": msg_id,
                "date": datetime.utcnow(),
            }
        }
        if admin_messages is not None:
            update_data["pending_payment"]["admin_messages"] = admin_messages
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": update_data},
            upsert=True
        )

    async def approve_payment(self, user_id: int) -> Optional[dict]:
        user = await self.get_user(user_id)
        if not user or "pending_payment" not in user:
            return None

        duration = user["pending_payment"]["duration"]
        
        # Calculate new expiry
        current_expiry = user.get("subscription_expiry")
        now = datetime.utcnow()
        if current_expiry and current_expiry > now:
            from datetime import timedelta
            new_expiry = current_expiry + timedelta(days=duration)
        else:
            from datetime import timedelta
            new_expiry = now + timedelta(days=duration)

        plan_id_str = user["pending_payment"].get("plan_id", "")
        set_fields: dict = {"subscription_expiry": new_expiry, "subscription_status": "active"}
        if plan_id_str:
            set_fields["plan_id"] = plan_id_str
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {
                "$set": set_fields,
                "$unset": {"pending_payment": "", "reminder_sent": "", "expiry_notified": ""}
            }
        )

        # Abonelik/finansal geçmiş kaydı
        try:
            await self.log_subscription_event(user_id, {
                "type":        "payment_approved",
                "label":       user["pending_payment"].get("label", ""),
                "price":       user["pending_payment"].get("price", 0),
                "currency":    user["pending_payment"].get("currency", "TRY"),
                "duration":    duration,
                "new_expiry":  new_expiry,
            })
        except Exception:
            pass

        # Plan limitlerini bul — plan_id ile eşleştir (etiket boş olabilir)
        plan_id_str = user["pending_payment"].get("plan_id", "")
        plan_label  = user["pending_payment"].get("label", "")
        plan_doc = None
        if plan_id_str:
            try:
                plan_doc = await self.dbs["tracking"]["sub_plans"].find_one({"_id": ObjectId(plan_id_str)})
            except Exception:
                pass
        if plan_doc is None and plan_label:
            plan_doc = await self.dbs["tracking"]["sub_plans"].find_one({"label": plan_label})

        plan_daily_gb      = 0.0
        plan_monthly_gb    = 0.0
        plan_speed_mbps    = 0.0
        plan_request_limit = 0
        if plan_doc is not None:
            plan_daily_gb      = float(plan_doc.get("daily_limit_gb",  0) or 0)
            plan_monthly_gb    = float(plan_doc.get("monthly_limit_gb", 0) or 0)
            plan_speed_mbps    = float(plan_doc.get("speed_limit_mbps", 0) or 0)
            plan_request_limit = int(plan_doc.get("monthly_request_limit", 0) or 0)
            # Token zaten varsa anında güncelle (hem str hem int user_id için)
            await self.dbs["tracking"]["api_tokens"].update_many(
                {"$or": [{"user_id": str(user_id)}, {"user_id": int(user_id)}]},
                {"$set": {
                    "limits.daily_limit_gb":        plan_daily_gb,
                    "limits.monthly_limit_gb":      plan_monthly_gb,
                    "limits.speed_limit_mbps":      plan_speed_mbps,
                    "limits.monthly_request_limit": plan_request_limit,
                }}
            )

        user_data = await self.get_user(user_id)
        if user_data is not None:
            # Plan limitlerini çağıran koda ilet — add_api_token bu değerleri kullanacak
            user_data["_plan_daily_gb"]      = plan_daily_gb
            user_data["_plan_monthly_gb"]    = plan_monthly_gb
            user_data["_plan_speed_mbps"]    = plan_speed_mbps
            user_data["_plan_request_limit"] = plan_request_limit
        return user_data

    async def reject_payment(self, user_id: int) -> bool:
        result = await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$unset": {"pending_payment": ""}}
        )
        return result.modified_count > 0

    # ── Abonelik / Finansal Geçmiş ───────────────────────────────────────────

    async def log_subscription_event(self, user_id: int, event: dict) -> None:
        """Bir üyenin abonelik/ödeme geçmişine tek bir olay kaydı ekler.
        event içine ekstra alanlar (label, price, days, new_expiry vb.) konabilir.
        """
        doc = {"user_id": user_id, "date": datetime.utcnow()}
        doc.update(event or {})
        await self.dbs["tracking"]["subscription_history"].insert_one(doc)

    async def get_subscription_history(self, user_id: int, limit: int = 50) -> List[dict]:
        """Bir üyenin abonelik/ödeme geçmişini en yeniden en eskiye döner."""
        cursor = self.dbs["tracking"]["subscription_history"].find(
            {"user_id": user_id}
        ).sort("date", DESCENDING).limit(limit)
        docs = await cursor.to_list(None)
        return [convert_objectid_to_str(d) for d in docs]

    async def ban_user(self, user_id: int) -> bool:
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {
                "$set": {"banned": True},
                "$unset": {"pending_payment": ""}
            },
            upsert=True
        )
        return True

    async def unban_user(self, user_id: int) -> bool:
        result = await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$unset": {"banned": ""}}
        )
        return result.modified_count > 0

    async def is_user_banned(self, user_id: int) -> bool:
        user = await self.get_user(user_id)
        return bool(user and user.get("banned"))

    async def get_expired_users(self) -> List[dict]:
        cursor = self.dbs["tracking"]["users"].find({
            "subscription_expiry": {"$lt": datetime.utcnow()},
            "subscription_status": "active"
        })
        return await cursor.to_list(None)

    async def mark_user_expired(self, user_id: int):
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": {"subscription_status": "expired"}}
        )

    async def get_expired_today_unnotified(self) -> List[dict]:
        """Aboneliği sona ermiş ve henüz sona erme bildirimi gönderilmemiş
        kullanıcıları döndürür. expiry-notify zamanlayıcısı tarafından
        günlük olarak (UTC+3 00:05) çağrılır."""
        cursor = self.dbs["tracking"]["users"].find({
            "subscription_expiry": {"$lt": datetime.utcnow()},
            "subscription_status": "active",
            "expiry_notified": {"$ne": True},
        })
        return await cursor.to_list(None)

    async def mark_expiry_notified(self, user_id: int):
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": {"expiry_notified": True, "subscription_status": "expired"}}
        )

    async def get_expiring_users(self, hours: int = 24) -> List[dict]:
        from datetime import timedelta
        now = datetime.utcnow()
        target_time = now + timedelta(hours=hours)
        cursor = self.dbs["tracking"]["users"].find({
            "subscription_expiry": {"$gt": now, "$lte": target_time},
            "reminder_sent": {"$ne": True},
            "subscription_status": "active"
        })
        return await cursor.to_list(None)
        
    async def mark_reminder_sent(self, user_id: int):
         await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": {"reminder_sent": True}}
        )

    async def reset_reminder_sent(self, user_id: int):
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$unset": {"reminder_sent": ""}}
        )

    # -------------------------------
    # Admin Subscription Management
    # -------------------------------
    async def get_subscription_plans(self) -> List[dict]:
        cursor = self.dbs["tracking"]["sub_plans"].find().sort("days", ASCENDING)
        plans = await cursor.to_list(None)
        return [convert_objectid_to_str(plan) for plan in plans]

    async def add_subscription_plan(self, days: int, price: float, label: str = "", currency: str = "USD", is_unlimited: bool = False, daily_limit_gb: float = 0, monthly_limit_gb: float = 0, speed_limit_mbps: float = 0, monthly_request_limit: int = 0) -> Optional[str]:
        result = await self.dbs["tracking"]["sub_plans"].insert_one({
            "days": days,
            "price": price,
            "label": label,
            "currency": currency,
            "is_unlimited": is_unlimited,
            "daily_limit_gb": daily_limit_gb,
            "monthly_limit_gb": monthly_limit_gb,
            "speed_limit_mbps": speed_limit_mbps,
            "monthly_request_limit": monthly_request_limit,
            "created_at": datetime.utcnow()
        })
        return str(result.inserted_id)

    async def update_subscription_plan(self, plan_id: str, days: int, price: float, label: str = "", currency: str = "USD", is_unlimited: bool = False, daily_limit_gb: float = 0, monthly_limit_gb: float = 0, speed_limit_mbps: float = 0, monthly_request_limit: int = 0) -> bool:
        try:
            result = await self.dbs["tracking"]["sub_plans"].update_one(
                {"_id": ObjectId(plan_id)},
                {"$set": {
                    "days": days,
                    "price": price,
                    "label": label,
                    "currency": currency,
                    "is_unlimited": is_unlimited,
                    "daily_limit_gb": daily_limit_gb,
                    "monthly_limit_gb": monthly_limit_gb,
                    "speed_limit_mbps": speed_limit_mbps,
                    "monthly_request_limit": monthly_request_limit,
                    "updated_at": datetime.utcnow()
                }}
            )
            return result.modified_count > 0
        except Exception:
            return False

    async def delete_subscription_plan(self, plan_id: str) -> bool:
        try:
            result = await self.dbs["tracking"]["sub_plans"].delete_one({"_id": ObjectId(plan_id)})
            return result.deleted_count > 0
        except Exception:
            return False

    async def get_all_subscribers(self) -> List[dict]:
        cursor = self.dbs["tracking"]["users"].find({
            "subscription_status": {"$in": ["active", "expired"]}
        }).sort("subscription_expiry", DESCENDING)
        users = await cursor.to_list(None)
        return [convert_objectid_to_str(u) for u in users]

    async def get_active_subscribers(self) -> List[dict]:
        """Yalnızca aktif aboneleri döndürür (subscription_status == active)."""
        cursor = self.dbs["tracking"]["users"].find({
            "subscription_status": "active"
        }).sort("subscription_expiry", DESCENDING)
        users = await cursor.to_list(None)
        return [convert_objectid_to_str(u) for u in users]

    async def get_all_users(self) -> List[dict]:
        """Bota /start yapmış tüm kullanıcıları döndürür (abone olsun olmasın)."""
        cursor = self.dbs["tracking"]["users"].find({})
        users = await cursor.to_list(None)
        return [convert_objectid_to_str(u) for u in users]

    async def get_non_active_users(self) -> List[dict]:
        """Aboneliği bitmiş, hiç başlamamış veya banned olan kullanıcıları döndürür."""
        cursor = self.dbs["tracking"]["users"].find({
            "subscription_status": {"$not": {"$eq": "active"}}
        })
        users = await cursor.to_list(None)
        return [convert_objectid_to_str(u) for u in users]

    async def manage_subscriber(self, user_id: int, action: str, days: int = 0) -> bool:
        user = await self.get_user(user_id)
        if not user:
            return False
            
        now = datetime.utcnow()
        if action == "extend" or action == "reduce":
            from datetime import timedelta
            current_expiry = user.get("subscription_expiry")
            
            if action == "extend":
                if current_expiry and current_expiry > now:
                    new_expiry = current_expiry + timedelta(days=days)
                else:
                    new_expiry = now + timedelta(days=days)
            else: # reduce
                if current_expiry:
                    new_expiry = current_expiry - timedelta(days=days)
                    if new_expiry < now:
                        new_expiry = now # Just expire them
                else:
                    new_expiry = now # Already expired or none
            
            status = "active" if new_expiry > now else "expired"
            
            result = await self.dbs["tracking"]["users"].update_one(
                {"_id": user_id},
                {"$set": {"subscription_expiry": new_expiry, "subscription_status": status}}
            )
            if result.modified_count > 0:
                try:
                    await self.log_subscription_event(user_id, {
                        "type":       f"admin_{action}",
                        "days":       days,
                        "new_expiry": new_expiry,
                    })
                except Exception:
                    pass
            return result.modified_count > 0
            
        elif action == "delete":
            # Üyeliği TAMAMEN sil — kullanıcı hiç abone olmamış gibi olsun.
            # Aşağıdaki her adım o kullanıcıya ait ilgili tüm kayıtları temizler:
            # API token'ı (ve dolayısıyla eklentileri/limitleri), izleme geçmişi,
            # hatırlatmalar, içerik istekleri, abonelik geçmişi ve web paneli oturumu.

            # 1) Kullanıcının API token kaydını bul (varsa) — izleme geçmişini
            #    silmek ve token'ı kaldırmak için gerekli.
            token_doc = None
            try:
                token_doc = await self.dbs["tracking"]["api_tokens"].find_one(
                    {"$or": [{"user_id": user_id}, {"user_id": str(user_id)}]}
                )
            except Exception as e:
                print(f"manage_subscriber delete: token lookup error: {e}")

            # 2) İzleme geçmişini (stream_analytics) sil
            if token_doc and token_doc.get("token"):
                try:
                    await self.purge_stream_analytics_for_token(token_doc["token"])
                except Exception as e:
                    print(f"manage_subscriber delete: stream analytics purge error: {e}")

            # 3) API token'ını (limitler, eklentiler, portal bilgileri dahil) tamamen sil
            try:
                await self.dbs["tracking"]["api_tokens"].delete_many(
                    {"$or": [{"user_id": user_id}, {"user_id": str(user_id)}]}
                )
            except Exception as e:
                print(f"manage_subscriber delete: token delete error: {e}")

            # 4) Hatırlatmaları (tv/movie reminders) sil
            try:
                await self.delete_user_reminders(user_id)
            except Exception as e:
                print(f"manage_subscriber delete: reminders delete error: {e}")

            # 5) İçerik isteklerini sil
            try:
                await self.delete_user_content_requests(user_id)
            except Exception as e:
                print(f"manage_subscriber delete: content requests delete error: {e}")

            # 6) Abonelik geçmişini (subscription_history) sil
            try:
                await self.dbs["tracking"]["subscription_history"].delete_many({"user_id": user_id})
            except Exception as e:
                print(f"manage_subscriber delete: subscription history delete error: {e}")

            # 7) Web paneli (üye) oturumunu geçersiz kıl
            try:
                await self.invalidate_member_session(user_id)
            except Exception as e:
                print(f"manage_subscriber delete: member session invalidate error: {e}")

            # 8) Kullanıcı kaydındaki tüm abonelik/eklenti/plan alanlarını temizle
            try:
                await self.dbs["tracking"]["users"].update_one(
                    {"_id": user_id},
                    {"$unset": {
                        "subscription_expiry": "",
                        "subscription_status": "",
                        "plan_id": "",
                        "pending_payment": "",
                        "pending_addon": "",
                        "addon_extra_daily_gb": "",
                        "addon_extra_monthly_gb": "",
                        "addon_extra_speed_mbps": "",
                        "addon_extra_requests": "",
                        "reminder_sent": "",
                        "expiry_notified": "",
                    }}
                )
            except Exception as e:
                print(f"manage_subscriber delete: user fields unset error: {e}")

            # Kullanıcı işlem başında bulunmuştu; tüm temizlik adımları denendi.
            return True

        return False

    async def delete_user_reminders(self, user_id: int) -> dict:
        """Üyeye ait tüm hatırlatma kayıtlarını siler.
        tv_reminders ve movie_reminders koleksiyonlarındaki user_ids array'inden
        user_id'yi çıkarır; artık hiç abone kalmayan kayıtları tamamen siler.
        """
        tv_col    = self.dbs["tracking"]["tv_reminders"]
        movie_col = self.dbs["tracking"]["movie_reminders"]

        # user_ids array'inden çıkar
        await tv_col.update_many(
            {"user_ids": user_id},
            {"$pull": {"user_ids": user_id}},
        )
        await movie_col.update_many(
            {"user_ids": user_id},
            {"$pull": {"user_ids": user_id}},
        )

        # Artık hiç abone kalmayan kayıtları sil
        tv_del    = await tv_col.delete_many({"user_ids": {"$size": 0}})
        movie_del = await movie_col.delete_many({"user_ids": {"$size": 0}})

        return {
            "tv_removed":    tv_del.deleted_count,
            "movie_removed": movie_del.deleted_count,
        }

    async def assign_subscription(self, user_id: int, days: int, force_expiry=None) -> dict:
        """Upsert a subscription for any user_id, creating a record if it doesn't exist.
        force_expiry: datetime — if given, use directly as new_expiry (ignores days).
        """
        from datetime import timedelta
        now = datetime.utcnow()

        if force_expiry is not None:
            new_expiry = force_expiry
        else:
            user = await self.get_user(user_id)
            if user:
                current_expiry = user.get("subscription_expiry")
                if current_expiry and current_expiry > now:
                    new_expiry = current_expiry + timedelta(days=days)
                else:
                    new_expiry = now + timedelta(days=days)
            else:
                new_expiry = now + timedelta(days=days)

        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {
                "$set": {
                    "subscription_expiry": new_expiry,
                    "subscription_status": "active",
                },
                "$unset": {"reminder_sent": "", "expiry_notified": ""},
                "$setOnInsert": {
                    "_id": user_id,
                    "first_name": f"User {user_id}",
                    "username": None,
                    "created_at": now,
                }
            },
            upsert=True
        )
        return {
            "user_id": user_id,
            "subscription_expiry": new_expiry.isoformat(),
            "subscription_status": "active",
            "days_assigned": days,
        }


    # -------------------------------
    # Helper Methods for Repeated Logic
    # -------------------------------
    def _get_sort_dict(self, sort_params: List[Tuple[str, str]]) -> Dict[str, int]:
        if sort_params:
            sort_field, sort_direction = sort_params[0]
            return {sort_field: DESCENDING if sort_direction.lower() == "desc" else ASCENDING}
        return {"updated_on": DESCENDING}

    async def _paginate_collection(
        self,
        collection_name: str,
        sort_dict: Dict[str, int],
        page: int,
        page_size: int,
        filter_dict: Optional[dict] = None
    ):
        filter_dict = filter_dict or {}
        skip = (page - 1) * page_size
        results = []
        dbs_checked = []
        total_count = 0

        db_counts = []
        for i in range(1, self.current_db_index + 1):
            db_key = f"storage_{i}"
            db = self.dbs[db_key]
            count = await db[collection_name].count_documents(filter_dict)
            db_counts.append((i, count))
            total_count += count

        start_db_index = None
        for db_index, count in reversed(db_counts):
            if skip < count:
                start_db_index = db_index
                break
            skip -= count

        if not start_db_index:
            return [], [], total_count

        for db_index, count in reversed(db_counts):
            if db_index < start_db_index:
                continue

            db_key = f"storage_{db_index}"
            db = self.dbs[db_key]
            dbs_checked.append(db_index)

            cursor = (
                db[collection_name]
                .find(filter_dict)
                .sort(sort_dict)
                .skip(skip if db_index == start_db_index else 0)
                .limit(page_size - len(results))
            )

            docs = await cursor.to_list(None)
            results.extend(docs)

            if len(results) >= page_size:
                break

        return results, dbs_checked, total_count

    async def _move_document(
        self, collection_name: str, document: dict, old_db_index: int
    ) -> bool:
        current_db_key = f"storage_{self.current_db_index}"
        old_db_key = f"storage_{old_db_index}"
        document["db_index"] = self.current_db_index
        try:
            await self.dbs[current_db_key][collection_name].insert_one(document)
            await self.dbs[old_db_key][collection_name].delete_one({"_id": document["_id"]})
            LOGGER.info(f"✅ Moved document {document.get('tmdb_id')} from {old_db_key} to {current_db_key}")
            return True
        except Exception as e:
            LOGGER.error(f"Error moving document to {current_db_key}: {e}")
            return False

    async def _handle_storage_error(self, func, *args, total_storage_dbs: int) -> Optional[Any]:
        next_db_index = (self.current_db_index % total_storage_dbs) + 1
        if next_db_index == 1:
            LOGGER.warning("⚠️ All storage databases are full! Add more.")
            return None
        self.current_db_index = next_db_index
        await self.update_current_db_index()
        LOGGER.info(f"Switched to storage_{self.current_db_index}")
        return await func(*args)

    # -------------------------------
    # Multi Database Method for insert/update/delete/list
    # -------------------------------

    async def insert_media(
        self, metadata_info: dict,
        channel: int, msg_id: int, size: str, name: str, size_bytes: int = 0
    ) -> Optional[ObjectId]:
        result = await self._insert_media_internal(metadata_info, channel, msg_id, size, name, size_bytes)
        if result is not None:
            try:
                from Backend.helper.tmdb_catalog import notify_new_content
                notify_new_content()
            except Exception:
                pass
            try:
                await self.auto_assign_custom_catalogs(
                    file_name=name,
                    imdb_id=metadata_info.get("imdb_id"),
                    media_type=metadata_info.get("media_type"),
                    title=metadata_info.get("title_tr") or metadata_info.get("title"),
                    poster=metadata_info.get("poster_tr") or metadata_info.get("poster"),
                )
            except Exception as e:
                LOGGER.warning(f"[custom_catalogs] Dosya adına göre otomatik ekleme hatası: {e}")
        return result

    async def auto_assign_custom_catalogs(
        self,
        file_name: str,
        imdb_id: Optional[str],
        media_type: Optional[str],
        title: str = "",
        poster: str = "",
    ) -> list:
        """Dosya adında, bir özel katalog için tanımlı anahtar kelimelerden biri geçiyorsa
        ilgili içeriği (film/dizi) otomatik olarak o kataloğa ekler.

        - Bir kataloğun "keywords" alanı boşsa (eskisi gibi) hiçbir otomatik ekleme yapılmaz;
          o kataloğa içerik sadece admin panelinden elle eklenir.
        - Bir katalogda kelime tanımlıysa, dosya adında (büyük/küçük harf duyarsız) bu
          kelimelerden herhangi biri geçtiğinde içerik o kataloğa eklenir.
        - Katalogun media_type kısıtı ("movie"/"series") varsa, uyuşmayan içerikler atlanır.
        """
        if not imdb_id or not file_name:
            return []

        norm_media_type = "tv" if media_type in ("tv", "tv_show", "series") else "movie"
        catalog_media_type = "series" if norm_media_type == "tv" else "movie"

        catalogs = await self.get_custom_catalogs(active_only=True)
        haystack = file_name.casefold()

        matched_ids = []
        for cat in catalogs:
            keywords = [k for k in (cat.get("keywords") or []) if k and k.strip()]
            if not keywords:
                continue  # kelime tanımlı değilse eskisi gibi davran, otomatik ekleme yapma

            cat_media_type = cat.get("media_type", "mixed")
            if cat_media_type != "mixed" and cat_media_type != catalog_media_type:
                continue

            if not any(kw.strip().casefold() in haystack for kw in keywords):
                continue

            item = {
                "imdb_id": imdb_id,
                "media_type": norm_media_type,
                "title": title or "",
                "poster": poster or "",
            }
            added = await self.add_custom_catalog_item(cat["_id"], item)
            if added:
                matched_ids.append(cat["_id"])
                LOGGER.info(
                    f"[custom_catalogs] '{file_name}' dosya adı eşleşti → '{cat.get('name')}' kataloğuna eklendi ({imdb_id})"
                )

        return matched_ids

    async def rescan_custom_catalog_by_keywords(self, catalog_id: str, progress_cb=None) -> dict:
        """Var olan (bu katalog oluşturulmadan/kelime eklenmeden ÖNCE zaten kütüphaneye
        eklenmiş) film ve dizileri, kataloğun anahtar kelimelerine göre geriye dönük tarar.

        "Otomatik ekleme" (auto_assign_custom_catalogs) sadece BUNDAN SONRA eklenecek
        yeni dosyalar için tetiklenir; hâlihazırda kütüphanede olan içerikler için bu
        fonksiyon aynı eşleştirme mantığını mevcut kayıtlar üzerinde çalıştırır.

        progress_cb: verilirse, tarama ilerledikçe periyodik olarak
        {"checked", "matched", "total", "collection"} sözlüğüyle awaitlenir.
        Bu sayede çağıran taraf (örn. arka plan görevi) ilerlemeyi kullanıcıya
        gösterebilir; büyük dizi koleksiyonlarının taranması uzun sürebildiği için
        bu geri bildirim olmadan işlem "askıda/başarısız" gibi görünebiliyordu.

        Not: dizi kayıtları (seasons → episodes → telegram) film kayıtlarına göre çok
        daha büyük/derin dokümanlar olabildiğinden, sadece ihtiyaç duyulan alanlar
        projeksiyonla çekilir ve her koleksiyon/doküman kendi try/except'i içinde
        işlenir — böylece tek bir bozuk/ağır kayıt, o ana kadar bulunan eşleşmeleri
        (örn. filmler) kaybettirmeden taramayı durdurmaz.
        """
        catalog = await self.get_custom_catalog(catalog_id)
        if not catalog:
            return {"checked": 0, "matched": 0, "error": "Katalog bulunamadı"}

        keywords = [k.strip().casefold() for k in (catalog.get("keywords") or []) if k and k.strip()]
        if not keywords:
            return {"checked": 0, "matched": 0, "error": "Bu katalog için kelime tanımlı değil"}

        cat_media_type = catalog.get("media_type", "mixed")
        collections = []
        if cat_media_type in ("movie", "mixed"):
            collections.append("movie")
        if cat_media_type in ("series", "mixed"):
            collections.append("tv")

        movie_projection = {
            "imdb_id": 1, "title": 1, "title_tr": 1,
            "poster": 1, "poster_tr": 1, "telegram": 1,
        }
        tv_projection = {
            "imdb_id": 1, "title": 1, "title_tr": 1,
            "poster": 1, "poster_tr": 1,
            "seasons.episodes.telegram": 1,
        }

        total_storage_dbs = len(self.dbs) - 1

        async def _report(collection_label: str):
            if progress_cb is None:
                return
            try:
                await progress_cb({
                    "checked": checked,
                    "matched": matched,
                    "total": total_docs,
                    "collection": collection_label,
                })
            except Exception:
                pass  # ilerleme bildirimi taramayı asla durdurmamalı

        # ── Önce toplam kayıt sayısını hesapla (ilerleme yüzdesi için) ──────
        total_docs = 0
        for db_index in range(1, total_storage_dbs + 1):
            storage = self.dbs.get(f"storage_{db_index}")
            if storage is None:
                continue
            for coll_name in collections:
                try:
                    total_docs += await storage[coll_name].count_documents({})
                except Exception:
                    pass

        checked = 0
        matched = 0
        collection_errors = []
        await _report("başlıyor")

        for db_index in range(1, total_storage_dbs + 1):
            storage = self.dbs.get(f"storage_{db_index}")
            if storage is None:
                continue

            for coll_name in collections:
                collection_label = "Filmler" if coll_name == "movie" else "Diziler"
                projection = movie_projection if coll_name == "movie" else tv_projection
                try:
                    cursor = storage[coll_name].find({}, projection)
                    async for doc in cursor:
                        checked += 1
                        try:
                            imdb_id = doc.get("imdb_id")
                            if not imdb_id:
                                continue

                            names = []
                            if coll_name == "movie":
                                for q in (doc.get("telegram") or []):
                                    if isinstance(q, dict) and q.get("name"):
                                        names.append(q["name"])
                            else:  # tv
                                for season in (doc.get("seasons") or []):
                                    if not isinstance(season, dict):
                                        continue
                                    for ep in (season.get("episodes") or []):
                                        if not isinstance(ep, dict):
                                            continue
                                        for q in (ep.get("telegram") or []):
                                            if isinstance(q, dict) and q.get("name"):
                                                names.append(q["name"])

                            if not names:
                                continue

                            haystack = " | ".join(names).casefold()
                            if not any(kw in haystack for kw in keywords):
                                continue

                            item = {
                                "imdb_id": imdb_id,
                                "media_type": "tv" if coll_name == "tv" else "movie",
                                "title": doc.get("title_tr") or doc.get("title", ""),
                                "poster": doc.get("poster_tr") or doc.get("poster", ""),
                            }
                            added = await self.add_custom_catalog_item(catalog_id, item)
                            if added:
                                matched += 1
                        except Exception as doc_err:
                            LOGGER.warning(
                                f"[custom_catalogs] Tarama sırasında kayıt atlandı "
                                f"(storage_{db_index}.{coll_name}, _id={doc.get('_id')}): {doc_err}"
                            )
                            continue
                        finally:
                            if checked % 20 == 0:
                                await _report(collection_label)
                except Exception as coll_err:
                    LOGGER.warning(
                        f"[custom_catalogs] Tarama hatası (storage_{db_index}.{coll_name}): {coll_err}"
                    )
                    collection_errors.append(f"storage_{db_index}.{coll_name}")
                    continue

                await _report(collection_label)

        result = {"checked": checked, "matched": matched}
        if collection_errors:
            result["partial_error"] = (
                "Bazı koleksiyonlar taranırken hata oluştu, sonuçlar eksik olabilir: "
                + ", ".join(collection_errors)
            )
        return result

    async def _insert_media_internal(
        self, metadata_info: dict,
        channel: int, msg_id: int, size: str, name: str, size_bytes: int = 0
    ) -> Optional[ObjectId]:

        if metadata_info['media_type'] == "movie":
            media = MovieSchema(
                tmdb_id=metadata_info['tmdb_id'],
                imdb_id=metadata_info['imdb_id'],
                db_index=self.current_db_index,
                title=metadata_info['title'],
                title_tr=metadata_info.get('title_tr', ''),
                title_de=metadata_info.get('title_de', ''),
                genres=metadata_info.get('genres', []),
                genres_tr=metadata_info.get('genres_tr', []),
                genres_de=metadata_info.get('genres_de', []),
                description=metadata_info['description'],
                description_tr=metadata_info.get('description_tr', ''),
                description_de=metadata_info.get('description_de', ''),
                rating=metadata_info['rate'],
                release_year=metadata_info['year'],
                poster=metadata_info['poster'],
                backdrop=metadata_info['backdrop'],
                logo=metadata_info['logo'],
                poster_tr=metadata_info.get('poster_tr', ''),
                backdrop_tr=metadata_info.get('backdrop_tr', ''),
                logo_tr=metadata_info.get('logo_tr', ''),
                poster_de=metadata_info.get('poster_de', ''),
                backdrop_de=metadata_info.get('backdrop_de', ''),
                logo_de=metadata_info.get('logo_de', ''),
                cast=metadata_info['cast'],
                runtime=metadata_info['runtime'],
                original_language=metadata_info.get('original_language'),
                media_type=metadata_info['media_type'],
                collection_id=metadata_info.get('collection_id'),
                certification_tr=metadata_info.get('certification_tr'),
                certification_de=metadata_info.get('certification_de'),
                certification_us=metadata_info.get('certification_us'),
                visibility=metadata_info.get('visibility'),
                telegram=[QualityDetail(
                    quality=metadata_info['quality'],
                    id=metadata_info['encoded_string'],
                    name=name,
                    size=size,
                    is_archive=bool(metadata_info.get('_is_archive', False)),
                    group_key=metadata_info.get('group_key'),
                    parts=[QualityPart(
                        part_number=metadata_info['part_number'],
                        chat_id=channel,
                        msg_id=msg_id,
                        size_bytes=size_bytes,
                    )] if metadata_info.get('group_key') and metadata_info.get('part_number') else None,
                )]
            )
            return await self.update_movie(media)
        elif metadata_info["media_type"] in ("tv", "tv_show"):
            tv_show = TVShowSchema(
                tmdb_id=metadata_info['tmdb_id'],
                imdb_id=metadata_info['imdb_id'],
                db_index=self.current_db_index,
                title=metadata_info['title'],
                title_tr=metadata_info.get('title_tr', ''),
                title_de=metadata_info.get('title_de', ''),
                genres=metadata_info.get('genres', []),
                genres_tr=metadata_info.get('genres_tr', []),
                genres_de=metadata_info.get('genres_de', []),
                description=metadata_info['description'],
                description_tr=metadata_info.get('description_tr', ''),
                description_de=metadata_info.get('description_de', ''),
                rating=metadata_info['rate'],
                release_year=metadata_info['year'],
                poster=metadata_info['poster'],
                backdrop=metadata_info['backdrop'],
                logo=metadata_info['logo'],
                poster_tr=metadata_info.get('poster_tr', ''),
                backdrop_tr=metadata_info.get('backdrop_tr', ''),
                logo_tr=metadata_info.get('logo_tr', ''),
                poster_de=metadata_info.get('poster_de', ''),
                backdrop_de=metadata_info.get('backdrop_de', ''),
                logo_de=metadata_info.get('logo_de', ''),
                cast=metadata_info['cast'],
                runtime=metadata_info['runtime'],
                original_language=metadata_info.get('original_language'),
                media_type=metadata_info['media_type'],
                status=metadata_info.get('status'),
                certification_tr=metadata_info.get('certification_tr'),
                certification_de=metadata_info.get('certification_de'),
                certification_us=metadata_info.get('certification_us'),
                visibility=metadata_info.get('visibility'),
                seasons=[Season(
                    season_number=metadata_info['season_number'],
                    episodes=[Episode(
                        episode_number=metadata_info['episode_number'],
                        title=metadata_info['episode_title'],
                        title_tr=metadata_info['episode_title_tr'],
                        title_de=metadata_info['episode_title_de'],
                        episode_backdrop=metadata_info['episode_backdrop'],
                        overview=metadata_info['episode_overview'],
                        overview_tr=metadata_info['episode_overview_tr'],
                        overview_de=metadata_info['episode_overview_de'],
                        released=metadata_info['episode_released'],
                        telegram=[QualityDetail(
                            quality=metadata_info['quality'],
                            id=metadata_info['encoded_string'],
                            name=name,
                            size=size,
                            is_archive=bool(metadata_info.get('_is_archive', False)),
                            group_key=metadata_info.get('group_key'),
                            parts=[QualityPart(
                                part_number=metadata_info['part_number'],
                                chat_id=channel,
                                msg_id=msg_id,
                                size_bytes=size_bytes,
                            )] if metadata_info.get('group_key') and metadata_info.get('part_number') else None,
                        )]
                    )]
                )]
            )
            return await self.update_tv_show(tv_show)

    async def _dedupe_same_name_size(self, qualities: list, incoming: dict) -> list:
        """Gelen video ile aynı isim + boyuta sahip mevcut bir kayıt varsa onu
        kaldırır (Telegram'daki eski mesajı silmeyi de dener). Böylece kanala
        mükerrer (isim + boyut eşleşen) bir video iletildiğinde, eski kayıt
        yeni gelenle otomatik güncellenmiş olur — aynı isim + boyutta iki
        ayrı kayıt oluşmaz. Split dosya parçaları (group_key'li) bu kontrolün
        dışında tutulur, onlar zaten kendi grup mantığıyla birleştirilir."""
        incoming_name = (incoming.get("name") or "").strip().casefold()
        incoming_size = (incoming.get("size") or "").strip().casefold()
        if not incoming_name or not incoming_size:
            return qualities

        kept = []
        for q in qualities:
            if q.get("group_key"):
                kept.append(q)
                continue
            same_name = (q.get("name") or "").strip().casefold() == incoming_name
            same_size = (q.get("size") or "").strip().casefold() == incoming_size
            if same_name and same_size:
                try:
                    old_id = q.get("id")
                    if old_id:
                        decoded = await decode_string(old_id)
                        chat_id = int(f"-100{decoded['chat_id']}")
                        msg_id = int(decoded['msg_id'])
                        create_task(delete_message(chat_id, msg_id))
                except Exception as e:
                    LOGGER.error(f"[dedupe] Mükerrer kaydın eski Telegram mesajı silinemedi: {e}")
                LOGGER.info(f"[dedupe] Mükerrer video (isim+boyut eşleşti) güncellendi: {q.get('name')} ({q.get('size')})")
                continue  # eski kaydı at, yenisi eklenecek
            kept.append(q)
        return kept

    async def update_movie(self, movie_data: MovieSchema) -> Optional[ObjectId]:
        try:
            movie_dict = movie_data.dict()
        except ValidationError as e:
            LOGGER.error(f"Validation error: {e}")
            return None

        imdb_id = movie_dict["imdb_id"]
        tmdb_id = movie_dict["tmdb_id"]
        title = movie_dict["title"]
        release_year = movie_dict["release_year"]

        quality_to_update = movie_dict["telegram"][0]
        target_quality = quality_to_update["quality"]

        current_db_key = f"storage_{self.current_db_index}"
        total_storage_dbs = len(self.dbs) - 1

        existing_movie = None
        existing_db_key = None
        existing_db_index = None

        for db_index in range(1, total_storage_dbs + 1):
            db_key = f"storage_{db_index}"
            movie = None

            if imdb_id:
                movie = await self.dbs[db_key]["movie"].find_one({"imdb_id": imdb_id})
            if not movie and tmdb_id:
                movie = await self.dbs[db_key]["movie"].find_one({"tmdb_id": tmdb_id})
            if not movie and title and release_year:
                movie = await self.dbs[db_key]["movie"].find_one({
                    "title": title,
                    "release_year": release_year
                })

            if movie:
                existing_movie = movie
                existing_db_key = db_key
                existing_db_index = db_index
                break

        # ---------------- INSERT NEW MOVIE ----------------
        if not existing_movie:
            try:
                movie_dict["db_index"] = self.current_db_index
                result = await self.dbs[current_db_key]["movie"].insert_one(movie_dict)
                return result.inserted_id
            except Exception as e:
                LOGGER.error(f"Insertion failed in {current_db_key}: {e}")
                if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                    return await self._handle_storage_error(self.update_movie, movie_data, total_storage_dbs=total_storage_dbs)
                return None

        # ---------------- UPDATE MOVIE ----------------
        movie_id = existing_movie["_id"]
        existing_qualities = existing_movie.get("telegram", [])

        incoming_group_key = quality_to_update.get("group_key")

        # ── Mükerrer (isim + boyut) kontrolü — modu ne olursa olsun uygulanır ──
        if not incoming_group_key:
            existing_qualities = await self._dedupe_same_name_size(existing_qualities, quality_to_update)

        if incoming_group_key:
            # ── Split dosya: aynı group_key'e ait kaliteye parça olarak ekle ──
            group_entry = next(
                (q for q in existing_qualities if q.get("group_key") == incoming_group_key),
                None
            )
            if group_entry is None:
                # Grup yok → ilk parça, yeni giriş oluştur
                existing_qualities.append(quality_to_update)
            else:
                # Gruba yeni parça ekle/güncelle
                parts = group_entry.setdefault("parts", [])
                incoming_parts = quality_to_update.get("parts") or []
                for new_part in incoming_parts:
                    pn = new_part.get("part_number")
                    existing_part = next((p for p in parts if p.get("part_number") == pn), None)
                    if existing_part:
                        existing_part.update(new_part)
                    else:
                        parts.append(new_part)
                parts.sort(key=lambda p: p.get("part_number", 0))
                # Toplam boyutu yeniden hesapla
                total = sum(p.get("size_bytes", 0) for p in parts)
                group_entry["size"] = f"{round(total / (1024**3), 2)} GB" if total > 1024**3 else f"{round(total / (1024**2), 0):.0f} MB"

        elif Telegram.REPLACE_MODE:
            to_delete = [q for q in existing_qualities if q.get("quality") == target_quality and not q.get("group_key")]

            for q in to_delete:
                try:
                    old_id = q.get("id")
                    if old_id:
                        decoded = await decode_string(old_id)
                        chat_id = int(f"-100{decoded['chat_id']}")
                        msg_id = int(decoded['msg_id'])
                        create_task(delete_message(chat_id, msg_id))
                except Exception as e:
                    LOGGER.error(f"Failed to delete old quality: {e}")

            existing_qualities = [
                q for q in existing_qualities if not (q.get("quality") == target_quality and not q.get("group_key"))
            ]
            existing_qualities.append(quality_to_update)

        else:
            # allow duplicate qualities
            existing_qualities.append(quality_to_update)

        existing_movie["telegram"] = existing_qualities
        existing_movie["updated_on"] = datetime.utcnow()

        # Yeni veriden TR/DE alanlarını mevcut kayda yaz (boşsa doldur, doluysa güncelle)
        for field in ["title_tr", "title_de", "description_tr", "description_de", "genres_tr", "genres_de", "poster_tr", "backdrop_tr", "logo_tr", "poster_de", "backdrop_de", "logo_de", "certification_tr", "certification_de", "certification_us", "status"]:
            new_val = movie_dict.get(field)
            if new_val:
                existing_movie[field] = new_val

        if existing_db_index != self.current_db_index:
            try:
                if await self._move_document("movie", existing_movie, existing_db_index):
                    return movie_id
            except Exception as e:
                LOGGER.error(f"Error moving movie to {current_db_key}: {e}")
                if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                    return await self._handle_storage_error(self.update_movie, movie_data, total_storage_dbs=total_storage_dbs)

        try:
            await self.dbs[existing_db_key]["movie"].replace_one({"_id": movie_id}, existing_movie)
            return movie_id
        except Exception as e:
            LOGGER.error(f"Failed to update movie {tmdb_id} in {existing_db_key}: {e}")
            if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                return await self._handle_storage_error(self.update_movie, movie_data, total_storage_dbs=total_storage_dbs)

    async def update_tv_show(self, tv_show_data: TVShowSchema) -> Optional[ObjectId]:
        try:
            tv_show_dict = tv_show_data.dict()
        except ValidationError as e:
            LOGGER.error(f"Validation error: {e}")
            return None

        imdb_id = tv_show_dict.get("imdb_id")
        tmdb_id = tv_show_dict.get("tmdb_id")
        title = tv_show_dict["title"]
        release_year = tv_show_dict["release_year"]

        current_db_key = f"storage_{self.current_db_index}"
        total_storage_dbs = len(self.dbs) - 1

        existing_tv = None
        existing_db_key = None
        existing_db_index = None

        for db_index in range(1, total_storage_dbs + 1):
            db_key = f"storage_{db_index}"
            tv = None

            if imdb_id:
                tv = await self.dbs[db_key]["tv"].find_one({"imdb_id": imdb_id})
            if not tv and tmdb_id:
                tv = await self.dbs[db_key]["tv"].find_one({"tmdb_id": tmdb_id})
            if not tv and title and release_year:
                tv = await self.dbs[db_key]["tv"].find_one({
                    "title": title,
                    "release_year": release_year
                })

            if tv:
                existing_tv = tv
                existing_db_key = db_key
                existing_db_index = db_index
                break

        # ---------------- INSERT NEW TV ----------------
        if not existing_tv:
            try:
                tv_show_dict["db_index"] = self.current_db_index
                result = await self.dbs[current_db_key]["tv"].insert_one(tv_show_dict)
                return result.inserted_id
            except Exception as e:
                LOGGER.error(f"Insertion failed in {current_db_key}: {e}")
                if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                    return await self._handle_storage_error(self.update_tv_show, tv_show_data, total_storage_dbs=total_storage_dbs)
                return None

        # ---------------- UPDATE TV ----------------
        tv_id = existing_tv["_id"]

        for season in tv_show_dict["seasons"]:
            existing_season = next(
                (s for s in existing_tv["seasons"]
                if s["season_number"] == season["season_number"]),
                None
            )

            if not existing_season:
                existing_tv["seasons"].append(season)
                continue

            for episode in season["episodes"]:
                existing_episode = next(
                    (e for e in existing_season["episodes"]
                    if e["episode_number"] == episode["episode_number"]),
                    None
                )

                if not existing_episode:
                    existing_season["episodes"].append(episode)
                    continue

                existing_episode.setdefault("telegram", [])

                for quality in episode["telegram"]:
                    target_quality = quality.get("quality")
                    incoming_group_key = quality.get("group_key")

                    # ── Mükerrer (isim + boyut) kontrolü — modu ne olursa olsun uygulanır ──
                    if not incoming_group_key:
                        existing_episode["telegram"] = await self._dedupe_same_name_size(
                            existing_episode["telegram"], quality
                        )

                    if incoming_group_key:
                        # ── Split dosya: aynı group_key'e parça olarak ekle ──
                        group_entry = next(
                            (q for q in existing_episode["telegram"] if q.get("group_key") == incoming_group_key),
                            None
                        )
                        if group_entry is None:
                            existing_episode["telegram"].append(quality)
                        else:
                            parts = group_entry.setdefault("parts", [])
                            incoming_parts = quality.get("parts") or []
                            for new_part in incoming_parts:
                                pn = new_part.get("part_number")
                                existing_part = next((p for p in parts if p.get("part_number") == pn), None)
                                if existing_part:
                                    existing_part.update(new_part)
                                else:
                                    parts.append(new_part)
                            parts.sort(key=lambda p: p.get("part_number", 0))
                            total = sum(p.get("size_bytes", 0) for p in parts)
                            group_entry["size"] = f"{round(total / (1024**3), 2)} GB" if total > 1024**3 else f"{round(total / (1024**2), 0):.0f} MB"

                    elif Telegram.REPLACE_MODE:
                        to_delete = [
                            q for q in existing_episode["telegram"]
                            if q.get("quality") == target_quality and not q.get("group_key")
                        ]

                        for q in to_delete:
                            try:
                                old_id = q.get("id")
                                if old_id:
                                    decoded = await decode_string(old_id)
                                    chat_id = int(f"-100{decoded['chat_id']}")
                                    msg_id = int(decoded['msg_id'])
                                    create_task(delete_message(chat_id, msg_id))
                            except Exception as e:
                                LOGGER.error(f"Failed to delete old quality: {e}")

                        existing_episode["telegram"] = [
                            q for q in existing_episode["telegram"]
                            if not (q.get("quality") == target_quality and not q.get("group_key"))
                        ]
                        existing_episode["telegram"].append(quality)

                    else:
                        existing_episode["telegram"].append(quality)

        existing_tv["updated_on"] = datetime.utcnow()

        # Yeni veriden TR/DE alanlarını mevcut kayda yaz (boşsa doldur, doluysa güncelle)
        for field in ["title_tr", "title_de", "description_tr", "description_de", "genres_tr", "genres_de", "poster_tr", "backdrop_tr", "logo_tr", "poster_de", "backdrop_de", "logo_de", "certification_tr", "certification_de", "certification_us", "status"]:
            new_val = tv_show_dict.get(field)
            if new_val:
                existing_tv[field] = new_val

        # ---------------- MOVE DB IF NEEDED ----------------
        if existing_db_index != self.current_db_index:
            try:
                if await self._move_document("tv", existing_tv, existing_db_index):
                    return tv_id
            except Exception as e:
                LOGGER.error(f"Error moving TV show to {current_db_key}: {e}")
                if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                    return await self._handle_storage_error(self.update_tv_show, tv_show_data, total_storage_dbs=total_storage_dbs)
            return tv_id

        try:
            await self.dbs[existing_db_key]["tv"].replace_one({"_id": tv_id}, existing_tv)
            return tv_id
        except Exception as e:
            LOGGER.error(f"Failed to update TV show {tmdb_id} in {existing_db_key}: {e}")
            if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                return await self._handle_storage_error(self.update_tv_show, tv_show_data, total_storage_dbs=total_storage_dbs)
    
    async def sort_movies(self, sort_params, page, page_size, genre_filter=None, lang="tr", exclude_collection=False, extra_filter=None):
        sort_dict = self._get_sort_dict(sort_params)
        genre_field = "genres_de" if lang == "de" else ("genres" if lang == "original" else "genres_tr")
        filter_dict = {genre_field: {"$in": [genre_filter]}} if genre_filter else {}
        if exclude_collection:
            filter_dict["collection_id"] = {"$in": [None, 0, ""]}
        # Ek filtreler (yıl, oyuncu, vb.)
        if extra_filter:
            filter_dict.update(extra_filter)
        results, dbs_checked, total_count = await self._paginate_collection(
            "movie", sort_dict, page, page_size, filter_dict=filter_dict
        )
        total_pages = (total_count + page_size - 1) // page_size
        return {
            "total_count": total_count,
            "total_pages": total_pages,
            "databases_checked": dbs_checked,
            "current_page": page,
            "movies": [convert_objectid_to_str(result) for result in results],
        }

    async def sort_tv_shows(self, sort_params, page, page_size, genre_filter=None, lang="tr", extra_filter=None):
        sort_dict = self._get_sort_dict(sort_params)
        genre_field = "genres_de" if lang == "de" else ("genres" if lang == "original" else "genres_tr")
        filter_dict = {genre_field: {"$in": [genre_filter]}} if genre_filter else {}
        # Ek filtreler (yıl, oyuncu, vb.)
        if extra_filter:
            filter_dict.update(extra_filter)
        results, dbs_checked, total_count = await self._paginate_collection(
            "tv", sort_dict, page, page_size, filter_dict=filter_dict
        )
        total_pages = (total_count + page_size - 1) // page_size
        return {
            "total_count": total_count,
            "total_pages": total_pages,
            "databases_checked": dbs_checked,
            "current_page": page,
            "tv_shows": [convert_objectid_to_str(result) for result in results],
        }

    def _search_words_all_present(self, doc: dict, words: List[str]) -> bool:
        """
        $text sorgusu kelimeleri OR mantığıyla eşleştirir (herhangi biri
        geçerse belge döner). Orijinal davranışı (tüm kelimeler geçmeli,
        sıra önemsiz) korumak için, $text index'inin ön elediği küçük aday
        kümesi üzerinde bu ek AND kontrolü uygulanır — artık tüm koleksiyon
        değil, sadece indexten dönen adaylar taranıyor.
        """
        parts = [
            str(doc.get("title") or ""),
            str(doc.get("title_tr") or ""),
            str(doc.get("title_de") or ""),
        ]
        cast = doc.get("cast") or []
        if isinstance(cast, list):
            parts.extend(str(c) for c in cast)
        else:
            parts.append(str(cast))

        parts.extend(self._telegram_names(doc.get("telegram")))

        for season in (doc.get("seasons") or []):
            if not isinstance(season, dict):
                continue
            for ep in (season.get("episodes") or []):
                if not isinstance(ep, dict):
                    continue
                parts.extend(self._telegram_names(ep.get("telegram")))

        haystack = " ".join(parts).lower()
        return all(w in haystack for w in words)

    @staticmethod
    def _telegram_names(telegram) -> List[str]:
        """
        `telegram` alanı hem film hem bölüm belgelerinde bir kalite listesi
        (`[{"name": ..., "quality": ..., ...}, ...]`) olarak saklanır — tek
        bir dict değil. Eski arama kodu bunu yanlışlıkla dict sanıyordu ve
        bölüm belgelerinde 'list' object has no attribute 'get' hatasına
        yol açıyordu. Burada hem liste (doğru/güncel şema) hem de olası
        eski/tekil dict biçimi güvenle işlenir.
        """
        if not telegram:
            return []
        entries = telegram if isinstance(telegram, list) else [telegram]
        names = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(str(entry["name"]))
        return names

    async def search_documents(
            self,
            query: str,
            page: int,
            page_size: int
        ) -> dict:

            skip = (page - 1) * page_size

            _MAX_QUERY_LEN  = 100   # toplam girdi karakter sınırı
            _MAX_WORD_LEN   = 40    # tek kelime karakter sınırı
            _MAX_WORD_COUNT = 10    # maksimum kelime sayısı

            query = query.strip()[:_MAX_QUERY_LEN]
            if not query:
                return {"total_count": 0, "results": []}

            words = [
                w[:_MAX_WORD_LEN].lower()
                for w in query.split()
                if w.strip()
            ][:_MAX_WORD_COUNT]

            if not words:
                return {"total_count": 0, "results": []}

            # $text $search söz diziminde çift tırnak ifade eşleşmesi
            # (phrase match) ve "-" hariç tutma anlamı taşır; kullanıcı
            # girdisinde bunlar özel anlam kazanmasın diye temizlenir.
            text_search = " ".join(
                w.replace('"', '').replace("-", " ") for w in words
            ).strip()

            if not text_search:
                return {"total_count": 0, "results": []}

            # NOT: {"$sort": {"score": {"$meta": "textScore"}}} bazı MongoDB
            # sürümlerinde "FieldPath field names may not start with '$',
            # given '$computed0'" hatasına yol açıyor (sort optimizer'ın
            # meta alanını materialize etmeden sıralamaya çalışmasından
            # kaynaklanıyor). Skoru önce gerçek bir alan olarak $addFields
            # ile üretip öyle sıralamak bu hatayı ortadan kaldırıyor.
            base_stage = [
                {"$match": {"$text": {"$search": text_search}}},
                {"$addFields": {"score": {"$meta": "textScore"}}},
                {"$sort": {"score": -1}},
            ]

            tv_pipeline = base_stage + [
                {"$project": {
                    "_id": 1, "tmdb_id": 1, "title": 1, "title_tr": 1, "title_de": 1,
                    "genres": 1, "genres_tr": 1, "genres_de": 1, "rating": 1, "imdb_id": 1,
                    "release_year": 1, "poster": 1, "backdrop": 1,
                    "description": 1, "description_tr": 1, "description_de": 1, "logo": 1,
                    "poster_tr": 1, "backdrop_tr": 1, "logo_tr": 1,
                    "poster_de": 1, "backdrop_de": 1, "logo_de": 1,
                    "media_type": 1, "db_index": 1,
                    "cast": 1, "language": 1,
                    "certification_tr": 1, "certification_de": 1, "certification_us": 1,
                    "seasons": 1
                }}
            ]

            movie_pipeline = base_stage + [
                {"$project": {
                    "_id": 1, "tmdb_id": 1, "title": 1, "title_tr": 1, "title_de": 1,
                    "genres": 1, "genres_tr": 1, "genres_de": 1, "rating": 1,
                    "release_year": 1, "poster": 1, "backdrop": 1,
                    "description": 1, "description_tr": 1, "description_de": 1,
                    "media_type": 1, "db_index": 1, "imdb_id": 1, "logo": 1,
                    "poster_tr": 1, "backdrop_tr": 1, "logo_tr": 1,
                    "poster_de": 1, "backdrop_de": 1, "logo_de": 1,
                    "cast": 1, "language": 1,
                    "certification_tr": 1, "certification_de": 1, "certification_us": 1,
                    "telegram": 1
                }}
            ]

            try:
                results: List[dict] = []
                dbs_checked = []

                active_db_key = f"storage_{self.current_db_index}"
                active_db = self.dbs[active_db_key]
                dbs_checked.append(self.current_db_index)

                tv_results = await active_db["tv"].aggregate(tv_pipeline).to_list(None)
                movie_results = await active_db["movie"].aggregate(movie_pipeline).to_list(None)
                results.extend(
                    r for r in (tv_results + movie_results)
                    if self._search_words_all_present(r, words)
                )

                if len(results) < page_size:
                    previous_db_index = self.current_db_index - 1
                    while previous_db_index > 0 and len(results) < page_size:
                        prev_db_key = f"storage_{previous_db_index}"
                        prev_db = self.dbs[prev_db_key]
                        tv_results_prev = await prev_db["tv"].aggregate(tv_pipeline).to_list(None)
                        movie_results_prev = await prev_db["movie"].aggregate(movie_pipeline).to_list(None)
                        results.extend(
                            r for r in (tv_results_prev + movie_results_prev)
                            if self._search_words_all_present(r, words)
                        )
                        dbs_checked.append(previous_db_index)
                        previous_db_index -= 1

                # total_count sadece taranan (dbs_checked) veritabanlarını
                # kapsar — orijinal davranışla aynı sınırlama.
                total_count = len(results)
                paged_results = results[skip:skip + page_size]

                return {
                    "total_count": total_count,
                    "results": [convert_objectid_to_str(doc) for doc in paged_results]
                }

            except Exception as text_err:
                # Text index henüz oluşturulmadıysa (ör. arka planda build
                # sürüyorsa) veya $text kullanılamıyorsa eski regex tabanlı
                # aramaya düş — böylece arama hiçbir zaman tamamen kesilmez.
                LOGGER.warning(f"search_documents $text hatası, regex'e düşülüyor: {text_err}")
                return await self._search_documents_regex_fallback(query, page, page_size)

    async def _search_documents_regex_fallback(
            self,
            query: str,
            page: int,
            page_size: int
        ) -> dict:
            """$text index kullanılamadığında devreye giren eski regex tabanlı arama."""

            skip = (page - 1) * page_size

            import re as _re

            _MAX_QUERY_LEN  = 100
            _MAX_WORD_LEN   = 40
            _MAX_WORD_COUNT = 10

            query = query.strip()[:_MAX_QUERY_LEN]
            if not query:
                return {"total_count": 0, "results": []}

            words = [
                _re.escape(w[:_MAX_WORD_LEN])
                for w in query.split()
                if w.strip()
            ][:_MAX_WORD_COUNT]

            if not words:
                return {"total_count": 0, "results": []}

            pattern = "".join(f"(?=.*{w})" for w in words)
            regex_query = {'$regex': pattern, '$options': 'i'}

            tv_pipeline = [
                {"$match": {"$or": [
                    {"title": regex_query},
                    {"title_de": regex_query},
                    {"title_tr": regex_query},
                    {"cast": regex_query},
                    {"seasons.episodes.telegram.name": regex_query}
                ]}},
                {"$project": {
                    "_id": 1, "tmdb_id": 1, "title": 1, "title_tr": 1, "title_de": 1,
                    "genres": 1, "genres_tr": 1, "genres_de": 1, "rating": 1, "imdb_id": 1,
                    "release_year": 1, "poster": 1, "backdrop": 1,
                    "description": 1, "description_tr": 1, "description_de": 1, "logo": 1,
                    "poster_tr": 1, "backdrop_tr": 1, "logo_tr": 1,
                    "poster_de": 1, "backdrop_de": 1, "logo_de": 1,
                    "media_type": 1, "db_index": 1,
                    "cast": 1, "language": 1,
                    "certification_tr": 1, "certification_de": 1, "certification_us": 1,
                    "seasons": 1
                }}
            ]

            movie_pipeline = [
                {"$match": {"$or": [
                    {"title": regex_query},
                    {"title_de": regex_query},
                    {"title_tr": regex_query},
                    {"cast": regex_query},
                    {"telegram.name": regex_query}
                ]}},
                {"$project": {
                    "_id": 1, "tmdb_id": 1, "title": 1, "title_tr": 1, "title_de": 1,
                    "genres": 1, "genres_tr": 1, "genres_de": 1, "rating": 1,
                    "release_year": 1, "poster": 1, "backdrop": 1,
                    "description": 1, "description_tr": 1, "description_de": 1,
                    "media_type": 1, "db_index": 1, "imdb_id": 1, "logo": 1,
                    "poster_tr": 1, "backdrop_tr": 1, "logo_tr": 1,
                    "poster_de": 1, "backdrop_de": 1, "logo_de": 1,
                    "cast": 1, "language": 1,
                    "certification_tr": 1, "certification_de": 1, "certification_us": 1,
                    "telegram": 1
                }}
            ]

            results = []
            dbs_checked = []

            active_db_key = f"storage_{self.current_db_index}"
            active_db = self.dbs[active_db_key]
            dbs_checked.append(self.current_db_index)

            tv_results = await active_db["tv"].aggregate(tv_pipeline).to_list(None)
            movie_results = await active_db["movie"].aggregate(movie_pipeline).to_list(None)
            results.extend(tv_results + movie_results)

            if len(results) < page_size:
                previous_db_index = self.current_db_index - 1
                while previous_db_index > 0 and len(results) < page_size:
                    prev_db_key = f"storage_{previous_db_index}"
                    prev_db = self.dbs[prev_db_key]
                    tv_results_prev = await prev_db["tv"].aggregate(tv_pipeline).to_list(None)
                    movie_results_prev = await prev_db["movie"].aggregate(movie_pipeline).to_list(None)
                    results.extend(tv_results_prev + movie_results_prev)
                    dbs_checked.append(previous_db_index)
                    previous_db_index -= 1

            total_count = 0
            for db_index in dbs_checked:
                key = f"storage_{db_index}"
                db = self.dbs[key]
                tv_count = await db["tv"].count_documents({
                    "$or": [
                        {"title": regex_query},
                        {"title_de": regex_query},
                        {"title_tr": regex_query},
                        {"cast": regex_query},
                        {"seasons.episodes.telegram.name": regex_query}
                    ]
                })
                movie_count = await db["movie"].count_documents({
                    "$or": [
                        {"title": regex_query},
                        {"title_de": regex_query},
                        {"title_tr": regex_query},
                        {"cast": regex_query},
                        {"telegram.name": regex_query}
                    ]
                })
                total_count += (tv_count + movie_count)

            paged_results = results[skip:skip + page_size]

            return {
                "total_count": total_count,
                "results": [convert_objectid_to_str(doc) for doc in paged_results]
            }


    async def get_media_details(
        self, 
        imdb_id: str,
        season_number: Optional[int] = None, 
        episode_number: Optional[int] = None
    ) -> Optional[dict]:

        for db_idx in range(self.current_db_index, 0, -1):
            db_key = f"storage_{db_idx}"
            
            if episode_number is not None and season_number is not None:
                tv_show = await self.dbs[db_key]["tv"].find_one({"imdb_id": imdb_id})
                if tv_show:
                    for season in tv_show.get("seasons", []):
                        if season.get("season_number") == season_number:
                            for episode in season.get("episodes", []):
                                if episode.get("episode_number") == episode_number:
                                    details = convert_objectid_to_str(episode)
                                    details.update({
                                        "imdb_id": imdb_id,
                                        "type": "tv",
                                        "season_number": season_number,
                                        "episode_number": episode_number,
                                        "backdrop": episode.get("episode_backdrop"),
                                        "db_index": db_idx,
                                        "certification_tr": tv_show.get("certification_tr"),
                                        "certification_de": tv_show.get("certification_de"),
                                        "certification_us": tv_show.get("certification_us"),
                                        "runtime": tv_show.get("runtime"),
                                    })
                                    return details
            
            elif season_number is not None:
                tv_show = await self.dbs[db_key]["tv"].find_one({"imdb_id": imdb_id})
                if tv_show:
                    for season in tv_show.get("seasons", []):
                        if season.get("season_number") == season_number:
                            details = convert_objectid_to_str(season)
                            details.update({
                                "imdb_id": imdb_id,
                                "type": "tv",
                                "season_number": season_number,
                                "db_index": db_idx
                            })
                            return details
            
            else:
                tv_doc = await self.dbs[db_key]["tv"].find_one({"imdb_id": imdb_id})
                if tv_doc:
                    tv_doc = convert_objectid_to_str(tv_doc)
                    tv_doc["type"] = "tv"
                    tv_doc["db_index"] = db_idx
                    return tv_doc
                
                movie_doc = await self.dbs[db_key]["movie"].find_one({"imdb_id": imdb_id})
                if movie_doc:
                    movie_doc = convert_objectid_to_str(movie_doc)
                    movie_doc["type"] = "movie"
                    movie_doc["db_index"] = db_idx
                    return movie_doc
        
        return None

    # -------------------------------
    # DB Method for Edit Post
    # -------------------------------

    async def get_document(self, media_type: str, tmdb_id: int, db_index: int) -> Optional[Dict[str, Any]]:
        db_key = f"storage_{db_index}"
        if media_type.lower() in ["tv", "series"]:
            collection_name = "tv"
        else:
            collection_name = "movie"
        document = await self.dbs[db_key][collection_name].find_one({"tmdb_id": int(tmdb_id)})
        return convert_objectid_to_str(document) if document else None

    async def update_document(
        self, media_type: str, tmdb_id: int, db_index: int, update_data: Dict[str, Any]
    ):
        update_data.pop('_id', None)
        db_key = f"storage_{db_index}"
        if media_type.lower() in ["tv", "series"]:
            collection_name = "tv"
        else:
            collection_name = "movie"
        collection = self.dbs[db_key][collection_name]

        try:
            result = await collection.update_one({"tmdb_id": int(tmdb_id)}, {"$set": update_data})

            return result.modified_count > 0

        except Exception as e:
            err_str = str(e).lower()
            LOGGER.error(f"Error updating document in {db_key}: {e}")
            if "storage" in err_str or "quota" in err_str:
                total_storage_dbs = len(self.dbs) - 1
                db_index_int = int(db_index)
                next_db_index = (db_index_int % total_storage_dbs) + 1
                if next_db_index == 1:
                    LOGGER.warning("⚠️ All storage databases are full! Add more.")
                    return False

                new_db_key = f"storage_{next_db_index}"
                LOGGER.info(f"Switching from {db_key} to {new_db_key} due to storage error.")

                try:
                    old_doc = await self.dbs[db_key][collection_name].find_one({"tmdb_id": int(tmdb_id)})
                    if not old_doc:
                        LOGGER.error(f"Document with tmdb_id {tmdb_id} not found in {db_key} during migration.")
                        return False

                    old_doc.update(update_data)
                    old_doc["db_index"] = next_db_index
                    old_doc.pop("_id", None)
                    insert_result = await self.dbs[new_db_key][collection_name].insert_one(old_doc)
                    LOGGER.info(f"Inserted document {insert_result.inserted_id} into {new_db_key}")
                    await self.dbs[db_key][collection_name].delete_one({"tmdb_id": int(tmdb_id)})
                    LOGGER.info(f"Deleted document tmdb_id {tmdb_id} from {db_key}")
                    self.current_db_index = next_db_index
                    await self.update_current_db_index()
                    LOGGER.info(f"Switched to {new_db_key} and document migrated successfully.")
                    return True

                except Exception as migrate_error:
                    LOGGER.error(f"Error migrating document tmdb_id {tmdb_id} to {new_db_key}: {migrate_error}")
                    return False
            raise

    async def get_media_visibility(self, media_type: str, tmdb_id: int, db_index: int) -> Dict[str, Any]:
        """
        İçeriğin görünürlük ayarını döner.
        Dönen format: {"mode": "subscribers"|"selected", "member_ids": [int, ...]}
          - "subscribers" (varsayılan): aktif aboneliği olan tüm üyeler görebilir/erişebilir.
          - "selected": yalnızca member_ids içindeki üyeler görebilir/erişebilir.
        """
        doc = await self.get_document(media_type, tmdb_id, db_index)
        vis = (doc or {}).get("visibility") or {}
        mode = vis.get("mode") if vis.get("mode") in ("subscribers", "selected") else "subscribers"
        try:
            member_ids = sorted({int(m) for m in (vis.get("member_ids") or [])})
        except (TypeError, ValueError):
            member_ids = []
        return {"mode": mode, "member_ids": member_ids}

    async def save_media_visibility(
        self, media_type: str, tmdb_id: int, db_index: int, mode: str, member_ids: List[Any]
    ) -> bool:
        """
        İçeriğin görünürlük ayarını kaydeder.
          mode="subscribers" → member_ids yok sayılır, boş liste olarak kaydedilir.
          mode="selected"    → yalnızca member_ids'teki üyelere açık.
        """
        if mode not in ("subscribers", "selected"):
            raise ValueError("Geçersiz görünürlük modu (mode 'subscribers' veya 'selected' olmalı)")

        clean_ids: List[int] = []
        if mode == "selected":
            for m in (member_ids or []):
                try:
                    clean_ids.append(int(m))
                except (TypeError, ValueError):
                    continue
            clean_ids = sorted(set(clean_ids))

        return await self.update_document(media_type, tmdb_id, db_index, {
            "visibility": {"mode": mode, "member_ids": clean_ids},
        })

    async def get_visibility_map(self, imdb_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Verilen imdb_id listesi için, tüm shard'lar (storage_1..N) ve her iki
        koleksiyon (movie/tv) taranarak {imdb_id: {"mode":..., "member_ids":...}}
        şeklinde bir görünürlük haritası döner.

        Harici kaynaklardan (TMDB trendleri, platform koleksiyon önbelleği gibi)
        gelen ve DB dokümanının 'visibility' alanını taşımayan katalog öğelerini
        filtrelemeden önce zenginleştirmek için kullanılır.
        Bulunamayan / visibility alanı olmayan imdb_id'ler haritada yer almaz —
        bu durumda çağıran taraf içeriği herkese açık kabul etmelidir.
        """
        imdb_ids = [i for i in set(imdb_ids or []) if i]
        if not imdb_ids:
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        for db_idx in range(self.current_db_index, 0, -1):
            db_key = f"storage_{db_idx}"
            for coll_name in ("movie", "tv"):
                cursor = self.dbs[db_key][coll_name].find(
                    {"imdb_id": {"$in": imdb_ids}},
                    {"imdb_id": 1, "visibility": 1},
                )
                async for doc in cursor:
                    imdb_id = doc.get("imdb_id")
                    if not imdb_id or imdb_id in result:
                        continue
                    vis = doc.get("visibility") or {}
                    mode = vis.get("mode") if vis.get("mode") in ("subscribers", "selected") else "subscribers"
                    try:
                        member_ids = sorted({int(m) for m in (vis.get("member_ids") or [])})
                    except (TypeError, ValueError):
                        member_ids = []
                    result[imdb_id] = {"mode": mode, "member_ids": member_ids}

        return result

    async def delete_document(self, media_type: str, tmdb_id: int, db_index: int) -> bool:
        db_key = f"storage_{db_index}"

        if media_type == "Movie":
            doc = await self.dbs[db_key]["movie"].find_one({"tmdb_id": tmdb_id})
            if doc and "telegram" in doc:
                for quality in doc["telegram"]:
                    #----- Parçalı (split) dosyalarda her parçanın kendi chat_id/msg_id'si
                    #----- "parts" listesinde tutulur; sadece quality["id"]'yi (1. parça)
                    #----- silmek diğer parçaları (.002, .003, ...) Telegram'da bırakır.
                    parts = quality.get("parts") or []
                    if parts:
                        for part in parts:
                            try:
                                part_chat_id = part.get("chat_id")
                                part_msg_id = part.get("msg_id")
                                if part_chat_id and part_msg_id:
                                    chat_id = int(f"-100{part_chat_id}")
                                    create_task(delete_message(chat_id, int(part_msg_id)))
                            except Exception as e:
                                LOGGER.error(f"Failed to queue split part for deletion: {e}")
                        continue
                    try:
                        old_id = quality.get("id")
                        if old_id:
                            decoded_data = await decode_string(old_id)
                            chat_id = int(f"-100{decoded_data['chat_id']}")
                            msg_id = int(decoded_data['msg_id'])
                            create_task(delete_message(chat_id, msg_id))
                    except Exception as e:
                        LOGGER.error(f"Failed to queue file for deletion: {e}")
            
            result = await self.dbs[db_key]["movie"].delete_one({"tmdb_id": tmdb_id})
        else:
            doc = await self.dbs[db_key]["tv"].find_one({"tmdb_id": tmdb_id})
            if doc and "seasons" in doc:
                for season in doc["seasons"]:
                    for episode in season.get("episodes", []):
                        for quality in episode.get("telegram", []):
                            #----- Parçalı (split) dosyalarda her parçanın kendi chat_id/msg_id'si
                            #----- "parts" listesinde tutulur; sadece quality["id"]'yi (1. parça)
                            #----- silmek diğer parçaları (.002, .003, ...) Telegram'da bırakır.
                            parts = quality.get("parts") or []
                            if parts:
                                for part in parts:
                                    try:
                                        part_chat_id = part.get("chat_id")
                                        part_msg_id = part.get("msg_id")
                                        if part_chat_id and part_msg_id:
                                            chat_id = int(f"-100{part_chat_id}")
                                            create_task(delete_message(chat_id, int(part_msg_id)))
                                    except Exception as e:
                                        LOGGER.error(f"Failed to queue split part for deletion: {e}")
                                continue
                            try:
                                old_id = quality.get("id")
                                if old_id:
                                    decoded_data = await decode_string(old_id)
                                    chat_id = int(f"-100{decoded_data['chat_id']}")
                                    msg_id = int(decoded_data['msg_id'])
                                    create_task(delete_message(chat_id, msg_id))
                            except Exception as e:
                                LOGGER.error(f"Failed to queue file for deletion: {e}")
            
            result = await self.dbs[db_key]["tv"].delete_one({"tmdb_id": tmdb_id})
        
        if result.deleted_count > 0:
            LOGGER.info(f"{media_type} with tmdb_id {tmdb_id} deleted successfully.")
            #----- İçerik tamamen silindi: duyuru bekleme (cooldown) kaydını da temizle.
            #----- Böylece bu tmdb_id'ye yeniden video eklenirse, sanki hiç eklenmemiş
            #----- gibi 24 saat beklemeden tekrar duyurulabilir.
            try:
                announce_media_type = "movie" if media_type == "Movie" else "tv"
                await self.dbs["tracking"]["announced_content"].delete_one(
                    {"_id": f"{announce_media_type}:{tmdb_id}"}
                )
            except Exception as e:
                LOGGER.warning(f"Duyuru bekleme kaydı temizlenemedi (tmdb_id={tmdb_id}): {e}")
            return True
        LOGGER.info(f"No document found with tmdb_id {tmdb_id}.")
        return False

    async def get_title_by_stream_id(self, stream_id_hash: str) -> Optional[str]:
        """Look up the original media title across all storage DBs using the telegram file ID hash.
        For TV shows, it includes the Season and Episode number in the title."""
        for i in range(1, self.current_db_index + 1):
            db = self.dbs[f"storage_{i}"]
            
            # Check Movies
            movie = await db["movie"].find_one({"telegram.id": stream_id_hash})
            if movie and "telegram" in movie:
                for t in movie["telegram"]:
                    if t.get("id") == stream_id_hash:
                        return movie.get("title")

            # Check TV Shows
            tv = await db["tv"].find_one({"seasons.episodes.telegram.id": stream_id_hash})
            if tv and "seasons" in tv:
                title = tv.get("title", "Unknown Series")
                for season in tv.get("seasons", []):
                    for episode in season.get("episodes", []):
                        for t in episode.get("telegram", []):
                            if t.get("id") == stream_id_hash:
                                s_num = season.get("season_number", 0)
                                e_num = episode.get("episode_number", 0)
                                return f"{title} S{s_num:02d}E{e_num:02d}"

        return None


    async def get_document_by_stream_id(self, stream_id_hash: str) -> dict | None:
        """Verilen stream ID hash'ine sahip film/dizi kaydını döner. Yoksa None."""
        for i in range(1, self.current_db_index + 1):
            db = self.dbs[f"storage_{i}"]
            movie = await db["movie"].find_one({"telegram.id": stream_id_hash})
            if movie:
                return movie
            tv = await db["tv"].find_one({"seasons.episodes.telegram.id": stream_id_hash})
            if tv:
                return tv
        return None

    async def delete_media_by_stream_id(self, stream_id_hash: str) -> bool:
        """Finds and removes a specific stream quality by its hash across all DBs. 
        If it's the last quality, it cleans up the movie or episode/season/show."""
        for i in range(1, self.current_db_index + 1):
            db = self.dbs[f"storage_{i}"]
            
            # Check Movies
            movie = await db["movie"].find_one({"telegram.id": stream_id_hash})
            if movie:
                movie["telegram"] = [q for q in movie.get("telegram", []) if q.get("id") != stream_id_hash]
                if len(movie["telegram"]) == 0:
                    await db["movie"].delete_one({"_id": movie["_id"]})
                else:
                    movie['updated_on'] = datetime.utcnow()
                    await db["movie"].replace_one({"_id": movie["_id"]}, movie)
                return True

            # Check TV Shows
            tv = await db["tv"].find_one({"seasons.episodes.telegram.id": stream_id_hash})
            if tv:
                for season in tv.get("seasons", []):
                    for episode in season.get("episodes", []):
                        for q in episode.get("telegram", []):
                            if q.get("id") == stream_id_hash:
                                episode["telegram"] = [t for t in episode.get("telegram", []) if t.get("id") != stream_id_hash]
                                if len(episode["telegram"]) == 0:
                                    season["episodes"] = [e for e in season.get("episodes", []) if e.get("episode_number") != episode.get("episode_number")]
                                    if len(season["episodes"]) == 0:
                                        tv["seasons"] = [s for s in tv.get("seasons", []) if s.get("season_number") != season.get("season_number")]
                                        if len(tv["seasons"]) == 0:
                                            await db["tv"].delete_one({"_id": tv["_id"]})
                                            return True
                                tv['updated_on'] = datetime.utcnow()
                                await db["tv"].replace_one({"_id": tv["_id"]}, tv)
                                return True
        return False

    async def delete_movie_quality(self, tmdb_id: int, db_index: int, id: str) -> bool:
        db_key = f"storage_{db_index}"
        movie = await self.dbs[db_key]["movie"].find_one({"tmdb_id": tmdb_id})
        
        if not movie or "telegram" not in movie:
            return False

        for q in movie["telegram"]:
            if q.get("id") == id:
                # Ana mesajı sil (parts yoksa veya parts listesi boşsa)
                parts = q.get("parts") or []
                if parts:
                    # Tüm parçaları (split dosyaları) Telegram'dan sil
                    for part in parts:
                        try:
                            part_chat_id = part.get("chat_id")
                            part_msg_id = part.get("msg_id")
                            if part_chat_id and part_msg_id:
                                chat_id = int(f"-100{part_chat_id}")
                                create_task(delete_message(chat_id, int(part_msg_id)))
                        except Exception as e:
                            LOGGER.error(f"Failed to queue split part for deletion: {e}")
                else:
                    # Parts yoksa ana id'den decode et ve sil
                    try:
                        old_id = q.get("id")
                        if old_id:
                            decoded_data = await decode_string(old_id)
                            chat_id = int(f"-100{decoded_data['chat_id']}")
                            msg_id = int(decoded_data['msg_id'])
                            create_task(delete_message(chat_id, msg_id))
                    except Exception as e:
                        LOGGER.error(f"Failed to queue file for deletion: {e}")
                break
        
        original_len = len(movie["telegram"])
        movie["telegram"] = [q for q in movie["telegram"] if q.get("id") != id]
        
        if len(movie["telegram"]) == original_len:
            return False
        
        movie['updated_on'] = datetime.utcnow()
        result = await self.dbs[db_key]["movie"].replace_one({"tmdb_id": tmdb_id}, movie)
        return result.modified_count > 0

    async def delete_tv_episode(self, tmdb_id: int, db_index: int, season_number: int, episode_number: int) -> bool:
        db_key = f"storage_{db_index}"
        tv = await self.dbs[db_key]["tv"].find_one({"tmdb_id": tmdb_id})
        
        if not tv or "seasons" not in tv:
            return False
        
        found = False
        for season in tv["seasons"]:
            if season.get("season_number") == season_number:
                for ep in season["episodes"]:
                    if ep.get("episode_number") == episode_number:
                        for quality in ep.get("telegram", []):
                            parts = quality.get("parts") or []
                            if parts:
                                for part in parts:
                                    try:
                                        part_chat_id = part.get("chat_id")
                                        part_msg_id = part.get("msg_id")
                                        if part_chat_id and part_msg_id:
                                            chat_id = int(f"-100{part_chat_id}")
                                            create_task(delete_message(chat_id, int(part_msg_id)))
                                    except Exception as e:
                                        LOGGER.error(f"Failed to queue split part for deletion: {e}")
                            else:
                                try:
                                    old_id = quality.get("id")
                                    if old_id:
                                        decoded_data = await decode_string(old_id)
                                        chat_id = int(f"-100{decoded_data['chat_id']}")
                                        msg_id = int(decoded_data['msg_id'])
                                        create_task(delete_message(chat_id, msg_id))
                                except Exception as e:
                                    LOGGER.error(f"Failed to queue file for deletion: {e}")
                        break
                
                original_len = len(season["episodes"])
                season["episodes"] = [ep for ep in season["episodes"] if ep.get("episode_number") != episode_number]
                found = original_len > len(season["episodes"])
                break
        
        if not found:
            return False
        
        tv['updated_on'] = datetime.utcnow()
        result = await self.dbs[db_key]["tv"].replace_one({"tmdb_id": tmdb_id}, tv)
        return result.modified_count > 0

    async def delete_tv_season(self, tmdb_id: int, db_index: int, season_number: int) -> bool:
        db_key = f"storage_{db_index}"
        tv = await self.dbs[db_key]["tv"].find_one({"tmdb_id": tmdb_id})
        
        if not tv or "seasons" not in tv:
            return False
        
        for season in tv["seasons"]:
            if season.get("season_number") == season_number:
                for episode in season.get("episodes", []):
                    for quality in episode.get("telegram", []):
                        parts = quality.get("parts") or []
                        if parts:
                            for part in parts:
                                try:
                                    part_chat_id = part.get("chat_id")
                                    part_msg_id = part.get("msg_id")
                                    if part_chat_id and part_msg_id:
                                        chat_id = int(f"-100{part_chat_id}")
                                        create_task(delete_message(chat_id, int(part_msg_id)))
                                except Exception as e:
                                    LOGGER.error(f"Failed to queue split part for deletion: {e}")
                        else:
                            try:
                                old_id = quality.get("id")
                                if old_id:
                                    decoded_data = await decode_string(old_id)
                                    chat_id = int(f"-100{decoded_data['chat_id']}")
                                    msg_id = int(decoded_data['msg_id'])
                                    create_task(delete_message(chat_id, msg_id))
                            except Exception as e:
                                LOGGER.error(f"Failed to queue file for deletion: {e}")
                break
        
        original_len = len(tv["seasons"])
        tv["seasons"] = [s for s in tv["seasons"] if s.get("season_number") != season_number]
        
        if len(tv["seasons"]) == original_len:
            return False
        
        tv['updated_on'] = datetime.utcnow()
        result = await self.dbs[db_key]["tv"].replace_one({"tmdb_id": tmdb_id}, tv)
        return result.modified_count > 0

    async def delete_tv_quality(self, tmdb_id: int, db_index: int, season_number: int, episode_number: int, id: str) -> bool:
        db_key = f"storage_{db_index}"
        tv = await self.dbs[db_key]["tv"].find_one({"tmdb_id": tmdb_id})
        
        if not tv or "seasons" not in tv:
            return False
        
        found = False
        for season in tv["seasons"]:
            if season.get("season_number") == season_number:
                for episode in season["episodes"]:
                    if episode.get("episode_number") == episode_number and "telegram" in episode:
                        for q in episode["telegram"]:
                            if q.get("id") == id:
                                parts = q.get("parts") or []
                                if parts:
                                    # Tüm parçaları (split dosyaları) Telegram'dan sil
                                    for part in parts:
                                        try:
                                            part_chat_id = part.get("chat_id")
                                            part_msg_id = part.get("msg_id")
                                            if part_chat_id and part_msg_id:
                                                chat_id = int(f"-100{part_chat_id}")
                                                create_task(delete_message(chat_id, int(part_msg_id)))
                                        except Exception as e:
                                            LOGGER.error(f"Failed to queue split part for deletion: {e}")
                                else:
                                    try:
                                        old_id = q.get("id")
                                        if old_id:
                                            decoded_data = await decode_string(old_id)
                                            chat_id = int(f"-100{decoded_data['chat_id']}")
                                            msg_id = int(decoded_data['msg_id'])
                                            create_task(delete_message(chat_id, msg_id))
                                    except Exception as e:
                                        LOGGER.error(f"Failed to queue file for deletion: {e}")
                                break
                        
                        original_len = len(episode["telegram"])
                        episode["telegram"] = [q for q in episode["telegram"] if q.get("id") != id]
                        found = original_len > len(episode["telegram"])
                        break
        
        if not found:
            return False
        tv['updated_on'] = datetime.utcnow()
        result = await self.dbs[db_key]["tv"].replace_one({"tmdb_id": tmdb_id}, tv)
        return result.modified_count > 0


    # ─────────────────────────────────────────────────────────────────
    # Canlı Yayın Kataloğu  (tracking DB'deki "live" koleksiyonu)
    # ─────────────────────────────────────────────────────────────────

    async def get_live_channels(self, scheduled_only: bool = False) -> list:
        """Tüm canlı yayın kanallarını döndürür.

        scheduled_only=True ise Türkiye saatine (UTC+3) göre şu an aktif olan
        zamanlama aralığına sahip kanallar döndürülür.
        Zamanlama listesi boş olan kanallar her zaman dahil edilir.
        """
        cursor = self.dbs["tracking"]["live"].find({}).sort("order", 1)
        docs = await cursor.to_list(None)
        channels = [convert_objectid_to_str(d) for d in docs]

        if not scheduled_only:
            return channels

        # Türkiye saati (UTC+3)
        from datetime import timezone, timedelta
        tr_tz = timezone(timedelta(hours=3))
        now_tr = datetime.now(tr_tz).replace(tzinfo=None)  # naive, aynı ofset

        def _is_visible(ch: dict) -> bool:
            schedule = ch.get("schedule") or []
            if not schedule:
                return True  # zamanlama yok → her zaman görünür
            for slot in schedule:
                start_str = slot.get("start")
                end_str   = slot.get("end")
                # datetime-local formatı: "YYYY-MM-DDTHH:MM"
                try:
                    start_dt = datetime.fromisoformat(start_str) if start_str else None
                    end_dt   = datetime.fromisoformat(end_str)   if end_str   else None
                except (ValueError, TypeError):
                    start_dt = end_dt = None
                after_start = (start_dt is None) or (now_tr >= start_dt)
                before_end  = (end_dt   is None) or (now_tr <  end_dt)
                if after_start and before_end:
                    return True
            return False

        return [ch for ch in channels if _is_visible(ch)]

    async def get_live_channel(self, channel_id: str) -> Optional[dict]:
        """Tek bir kanalı id'ye göre getirir."""
        from bson import ObjectId as _OID
        doc = await self.dbs["tracking"]["live"].find_one({"_id": _OID(channel_id)})
        return convert_objectid_to_str(doc) if doc else None

    async def add_live_channel(self, data: dict) -> dict:
        """Yeni bir canlı yayın kanalı ekler."""
        data["created_at"] = datetime.utcnow()
        data["updated_at"] = datetime.utcnow()
        data.setdefault("order", 0)
        data.setdefault("links", [])
        # catalog_ids: kanalın ekleneceği canlı yayın katalogları (bkz. "Katalog
        # Yönetimi" bölümü). Boş/eksikse varsayılan "Canlı Yayın" kataloğuna düşer.
        if not data.get("catalog_ids"):
            data["catalog_ids"] = ["default"]
        result = await self.dbs["tracking"]["live"].insert_one(data)
        data["_id"] = str(result.inserted_id)
        return convert_objectid_to_str(data)

    async def update_live_channel(self, channel_id: str, data: dict) -> bool:
        """Mevcut bir kanalı günceller."""
        from bson import ObjectId as _OID
        data["updated_at"] = datetime.utcnow()
        if "catalog_ids" in data and not data["catalog_ids"]:
            data["catalog_ids"] = ["default"]
        result = await self.dbs["tracking"]["live"].update_one(
            {"_id": _OID(channel_id)}, {"$set": data}
        )
        return result.modified_count > 0

    async def delete_live_channel(self, channel_id: str) -> bool:
        """Bir kanalı siler."""
        from bson import ObjectId as _OID
        result = await self.dbs["tracking"]["live"].delete_one({"_id": _OID(channel_id)})
        return result.deleted_count > 0

    # ─── Canlı Yayın – Ek Katalog Yönetimi ─────────────────────────────────────
    # Varsayılan olarak tüm kanal/yayınlar tek bir "Canlı Yayın" kataloğunda
    # (Stremio'da "live_{lang}") toplanır. Bu bölüm, admin'in bunun yanına
    # istediği sayıda ek/isimli katalog oluşturup her kanal/yayını istediği
    # katalog(lar)a atayabilmesini sağlar. "default" özel anahtarı, her zaman
    # var olan yerleşik "Canlı Yayın" kataloğunu temsil eder (silinemez, ama
    # adı değiştirilebilir — bkz. get/set_live_default_catalog_name).

    async def get_live_catalogs(self) -> list:
        """Admin'in oluşturduğu ek canlı yayın kataloglarını sıraya göre döndürür."""
        cursor = self.dbs["tracking"]["live_catalogs"].find({}).sort("order", 1)
        docs = await cursor.to_list(None)
        return [convert_objectid_to_str(d) for d in docs]

    async def get_live_catalog(self, catalog_id: str) -> Optional[dict]:
        """Tek bir ek canlı yayın kataloğunu id'ye göre getirir."""
        from bson import ObjectId as _OID
        try:
            oid = _OID(catalog_id)
        except Exception:
            return None
        doc = await self.dbs["tracking"]["live_catalogs"].find_one({"_id": oid})
        return convert_objectid_to_str(doc) if doc else None

    async def add_live_catalog(self, name: str) -> dict:
        """Yeni, isimlendirilmiş bir canlı yayın kataloğu oluşturur."""
        count = await self.dbs["tracking"]["live_catalogs"].count_documents({})
        data = {
            "name": name,
            "order": count,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = await self.dbs["tracking"]["live_catalogs"].insert_one(data)
        data["_id"] = str(result.inserted_id)
        return convert_objectid_to_str(data)

    async def update_live_catalog(self, catalog_id: str, data: dict) -> bool:
        """Bir canlı yayın kataloğunun adını ve/veya sırasını günceller."""
        from bson import ObjectId as _OID
        try:
            oid = _OID(catalog_id)
        except Exception:
            return False
        data["updated_at"] = datetime.utcnow()
        result = await self.dbs["tracking"]["live_catalogs"].update_one(
            {"_id": oid}, {"$set": data}
        )
        return result.matched_count > 0

    async def delete_live_catalog(self, catalog_id: str) -> bool:
        """Bir canlı yayın kataloğunu siler. Bu kataloğa atanmış kanal ve
        yayınlardan referansı temizler; başka kataloğu kalmayanlar otomatik
        olarak varsayılan 'Canlı Yayın' kataloğuna geri düşer."""
        from bson import ObjectId as _OID
        try:
            oid = _OID(catalog_id)
        except Exception:
            return False
        result = await self.dbs["tracking"]["live_catalogs"].delete_one({"_id": oid})
        if result.deleted_count:
            await self.dbs["tracking"]["live"].update_many(
                {"catalog_ids": catalog_id}, {"$pull": {"catalog_ids": catalog_id}}
            )
            await self.dbs["tracking"]["broadcasts"].update_many(
                {"catalog_ids": catalog_id}, {"$pull": {"catalog_ids": catalog_id}}
            )
            await self.dbs["tracking"]["live"].update_many(
                {"catalog_ids": {"$size": 0}}, {"$set": {"catalog_ids": ["default"]}}
            )
            await self.dbs["tracking"]["broadcasts"].update_many(
                {"catalog_ids": {"$size": 0}}, {"$set": {"catalog_ids": ["default"]}}
            )
        return bool(result.deleted_count)

    async def get_live_default_catalog_name(self) -> str:
        """Varsayılan 'Canlı Yayın' kataloğu yeniden adlandırılmışsa o adı, aksi
        halde boş string döndürür (boşsa çağıran taraf yerleşik etiketi kullanır)."""
        doc = await self.dbs["tracking"]["catalog_settings"].find_one({"_id": "global"})
        if doc and doc.get("live_default_name"):
            return doc["live_default_name"]
        return ""

    async def set_live_default_catalog_name(self, name: str) -> bool:
        """Varsayılan 'Canlı Yayın' kataloğunun görünen adını değiştirir.
        Boş string gönderilirse yerleşik varsayılan isme geri döner."""
        result = await self.dbs["tracking"]["catalog_settings"].update_one(
            {"_id": "global"},
            {"$set": {"live_default_name": name}},
            upsert=True,
        )
        return bool(result.acknowledged)

    # ─── Yayın (Broadcast) CRUD ───────────────────────────────────────────────────

    async def get_broadcasts(self) -> list:
        """Tüm yayınları döndürür."""
        cursor = self.dbs["tracking"]["broadcasts"].find({}).sort("order", 1)
        docs = await cursor.to_list(None)
        return [convert_objectid_to_str(d) for d in docs]

    async def get_broadcast(self, broadcast_id: str) -> Optional[dict]:
        """Tek bir yayını id'ye göre getirir."""
        from bson import ObjectId as _OID
        doc = await self.dbs["tracking"]["broadcasts"].find_one({"_id": _OID(broadcast_id)})
        return convert_objectid_to_str(doc) if doc else None

    async def add_broadcast(self, data: dict) -> dict:
        """Yeni yayın ekler."""
        data["created_at"] = datetime.utcnow()
        data["updated_at"] = datetime.utcnow()
        data.setdefault("order", 0)
        data.setdefault("active", False)
        data.setdefault("buffer_seconds", 15)
        if not data.get("catalog_ids"):
            data["catalog_ids"] = ["default"]
        result = await self.dbs["tracking"]["broadcasts"].insert_one(data)
        data["_id"] = str(result.inserted_id)
        return convert_objectid_to_str(data)

    async def update_broadcast(self, broadcast_id: str, data: dict) -> bool:
        """Mevcut yayını günceller."""
        from bson import ObjectId as _OID
        data["updated_at"] = datetime.utcnow()
        if "catalog_ids" in data and not data["catalog_ids"]:
            data["catalog_ids"] = ["default"]
        result = await self.dbs["tracking"]["broadcasts"].update_one(
            {"_id": _OID(broadcast_id)}, {"$set": data}
        )
        return result.modified_count > 0

    async def delete_broadcast(self, broadcast_id: str) -> bool:
        """Yayını siler."""
        from bson import ObjectId as _OID
        result = await self.dbs["tracking"]["broadcasts"].delete_one({"_id": _OID(broadcast_id)})
        return result.deleted_count > 0

    async def get_active_broadcasts(self) -> list:
        """Sadece aktif (yayında olan) yayınları döndürür — Stremio kataloğu için."""
        cursor = self.dbs["tracking"]["broadcasts"].find({"active": True}).sort("order", 1)
        docs = await cursor.to_list(None)
        return [convert_objectid_to_str(d) for d in docs]

    # ─── Katalog Yönetimi (Admin: platform/trend/öneri açıp-kapama + özel katalog CRUD) ──

    async def get_catalog_global_settings(self) -> dict:
        """Global olarak kapatılmış (disabled) hazır katalog ID'lerini ve admin'in
        belirlediği varsayılan katalog sırasını (order) döndürür.
        Örn: {'disabled': ['tmdb_trending'], 'order': ['similar', 'tmdb_trending', ...]}"""
        doc = await self.dbs["tracking"]["catalog_settings"].find_one({"_id": "global"})
        if not doc:
            return {"disabled": [], "order": []}
        return {"disabled": doc.get("disabled", []), "order": doc.get("order", [])}

    async def save_catalog_global_order(self, order: list) -> bool:
        """Admin panelinden belirlenen, TÜM üyeler için geçerli olacak varsayılan
        katalog sırasını kaydeder. Bir üye kendi Stremio ayarlar sayfasından
        kendi sırasını belirlerse, bu üye için o kişisel sıra bu varsayılanın
        önüne geçer."""
        result = await self.dbs["tracking"]["catalog_settings"].update_one(
            {"_id": "global"},
            {"$set": {"order": order}},
            upsert=True,
        )
        return bool(result.acknowledged)

    async def set_builtin_catalog_enabled(self, catalog_id: str, enabled: bool) -> bool:
        """Hazır (built-in) bir kataloğu global olarak açar/kapatır."""
        if enabled:
            result = await self.dbs["tracking"]["catalog_settings"].update_one(
                {"_id": "global"},
                {"$pull": {"disabled": catalog_id}},
                upsert=True,
            )
        else:
            result = await self.dbs["tracking"]["catalog_settings"].update_one(
                {"_id": "global"},
                {"$addToSet": {"disabled": catalog_id}},
                upsert=True,
            )
        return bool(result.acknowledged)

    async def get_custom_catalogs(self, active_only: bool = False) -> list:
        """Tüm özel katalogları (veya sadece aktif olanları) sıra numarasına göre döndürür."""
        query = {"active": True} if active_only else {}
        cursor = self.dbs["tracking"]["custom_catalogs"].find(query).sort("order", 1)
        docs = await cursor.to_list(None)
        return [convert_objectid_to_str(d) for d in docs]

    async def get_custom_catalog(self, catalog_id: str) -> Optional[dict]:
        """Tek bir özel kataloğu id'ye göre getirir."""
        from bson import ObjectId as _OID
        try:
            oid = _OID(catalog_id)
        except Exception:
            return None
        doc = await self.dbs["tracking"]["custom_catalogs"].find_one({"_id": oid})
        return convert_objectid_to_str(doc) if doc else None

    async def add_custom_catalog(self, data: dict) -> dict:
        """Yeni özel katalog oluşturur."""
        data.setdefault("active", True)
        data.setdefault("items", [])
        data.setdefault("keywords", [])
        if "order" not in data:
            count = await self.dbs["tracking"]["custom_catalogs"].count_documents({})
            data["order"] = count
        data["created_at"] = datetime.utcnow()
        data["updated_at"] = datetime.utcnow()
        result = await self.dbs["tracking"]["custom_catalogs"].insert_one(data)
        data["_id"] = str(result.inserted_id)
        return convert_objectid_to_str(data)

    async def update_custom_catalog(self, catalog_id: str, data: dict) -> bool:
        """Özel katalog meta bilgilerini (ad, tür, aktiflik, sıra) günceller."""
        from bson import ObjectId as _OID
        try:
            oid = _OID(catalog_id)
        except Exception:
            return False
        data["updated_at"] = datetime.utcnow()
        result = await self.dbs["tracking"]["custom_catalogs"].update_one(
            {"_id": oid}, {"$set": data}
        )
        return result.matched_count > 0

    async def delete_custom_catalog(self, catalog_id: str) -> bool:
        """Özel kataloğu tamamen siler."""
        from bson import ObjectId as _OID
        try:
            oid = _OID(catalog_id)
        except Exception:
            return False
        result = await self.dbs["tracking"]["custom_catalogs"].delete_one({"_id": oid})
        return result.deleted_count > 0

    async def add_custom_catalog_item(self, catalog_id: str, item: dict) -> bool:
        """Özel kataloğa bir film/dizi ekler (aynı imdb_id zaten varsa eklemez)."""
        from bson import ObjectId as _OID
        try:
            oid = _OID(catalog_id)
        except Exception:
            return False
        existing = await self.dbs["tracking"]["custom_catalogs"].find_one(
            {"_id": oid, "items.imdb_id": item.get("imdb_id")}
        )
        if existing:
            return False  # zaten ekli
        item["added_at"] = datetime.utcnow()
        result = await self.dbs["tracking"]["custom_catalogs"].update_one(
            {"_id": oid},
            {"$push": {"items": item}, "$set": {"updated_at": datetime.utcnow()}},
        )
        return result.matched_count > 0

    async def remove_custom_catalog_item(self, catalog_id: str, imdb_id: str) -> bool:
        """Özel katalogdan bir film/diziyi çıkarır."""
        from bson import ObjectId as _OID
        try:
            oid = _OID(catalog_id)
        except Exception:
            return False
        result = await self.dbs["tracking"]["custom_catalogs"].update_one(
            {"_id": oid},
            {"$pull": {"items": {"imdb_id": imdb_id}}, "$set": {"updated_at": datetime.utcnow()}},
        )
        return result.modified_count > 0

    async def get_media_by_imdb(self, imdb_id: str) -> Optional[dict]:
        """imdb_id'ye göre film ya da dizi dokümanını (media_type alanıyla birlikte) döndürür.
        Katalog önizleme/serve aşamasında kullanılır."""
        for db_idx in range(self.current_db_index, 0, -1):
            db_key = f"storage_{db_idx}"
            movie_doc = await self.dbs[db_key]["movie"].find_one({"imdb_id": imdb_id})
            if movie_doc:
                movie_doc = convert_objectid_to_str(movie_doc)
                movie_doc.setdefault("media_type", "movie")
                return movie_doc
            tv_doc = await self.dbs[db_key]["tv"].find_one({"imdb_id": imdb_id})
            if tv_doc:
                tv_doc = convert_objectid_to_str(tv_doc)
                tv_doc.setdefault("media_type", "tv")
                return tv_doc
        return None

    # Get per-DB statistics (movies, tv shows, used size, etc.)
    async def get_database_stats(self):
        stats = []
        for key in self.dbs.keys():
            if key.startswith("storage_"):
                db = self.dbs[key]
                movie_count = await db["movie"].count_documents({})
                tv_count = await db["tv"].count_documents({})
                db_stats = await db.command("dbstats")
                stats.append({
                    "db_name": key,
                    "movie_count": movie_count,
                    "tv_count": tv_count,
                    "storageSize": db_stats.get("storageSize", 0),
                    "dataSize": db_stats.get("dataSize", 0)
                })
        return stats

    async def get_content_sizes(self) -> dict:
        """
        Film ve dizi kayıtlarındaki telegram[].size alanlarını ("2.02GB", "700MB" vb.)
        parse ederek toplam boyutu byte cinsinden döner.
        """
        import re as _re

        def _parse_size(s: str) -> int:
            if not s or not isinstance(s, str):
                return 0
            s = s.strip().upper().replace(",", ".")
            m = _re.match(r"([\d.]+)\s*(TB|GB|MB|KB|B)?", s)
            if not m:
                return 0
            val = float(m.group(1))
            unit = m.group(2) or "B"
            return int(val * {"TB": 1099511627776, "GB": 1073741824, "MB": 1048576, "KB": 1024, "B": 1}[unit])

        movies_bytes = 0
        tv_bytes = 0

        for key, db in self.dbs.items():
            if not key.startswith("storage_"):
                continue
            # Filmler — telegram[].size
            try:
                async for doc in db["movie"].find({}, {"telegram.size": 1}):
                    for q in (doc.get("telegram") or []):
                        movies_bytes += _parse_size(q.get("size", ""))
            except Exception:
                pass
            # Diziler — seasons[].episodes[].telegram[].size
            try:
                async for doc in db["tv"].find({}, {"seasons.episodes.telegram.size": 1}):
                    for season in (doc.get("seasons") or []):
                        for ep in (season.get("episodes") or []):
                            for q in (ep.get("telegram") or []):
                                tv_bytes += _parse_size(q.get("size", ""))
            except Exception:
                pass

        return {
            "movies_bytes": movies_bytes,
            "tv_bytes":     tv_bytes,
            "total_bytes":  movies_bytes + tv_bytes,
        }



    # -------------------------------
    # API Token Methods
    # -------------------------------

    async def add_api_token(self, name: str, daily_limit_gb: float = None, monthly_limit_gb: float = None, speed_limit_mbps: float = None, portal_username: str = None, portal_password: str = None, user_id: int = None, expires_at=None, validity_days: int = None, ip_limit: int = None, device_limit: int = None) -> dict:
        # If a user_id is provided, return existing token if already created
        if user_id:
            existing = await self.dbs["tracking"]["api_tokens"].find_one({"user_id": user_id})
            if existing:
                # Limit parametresi geçildiyse mevcut token'ı da güncelle
                if daily_limit_gb is not None or monthly_limit_gb is not None or speed_limit_mbps is not None or ip_limit is not None or device_limit is not None:
                    new_daily   = float(daily_limit_gb)    if daily_limit_gb    else 0.0
                    new_monthly = float(monthly_limit_gb)  if monthly_limit_gb  else 0.0
                    new_speed   = float(speed_limit_mbps)  if speed_limit_mbps  else 0.0
                    new_ip      = int(ip_limit)     if ip_limit     is not None else existing.get("limits", {}).get("ip_limit", 0)
                    new_device  = int(device_limit) if device_limit is not None else existing.get("limits", {}).get("device_limit", 0)
                    update_set = {
                        "limits.daily_limit_gb":   new_daily,
                        "limits.monthly_limit_gb": new_monthly,
                        "limits.speed_limit_mbps": new_speed,
                        "limits.ip_limit":         new_ip,
                        "limits.device_limit":     new_device,
                    }
                    if expires_at is not None:
                        update_set["expires_at"] = expires_at
                    if validity_days is not None:
                        update_set["validity_days"] = validity_days
                    await self.dbs["tracking"]["api_tokens"].update_one(
                        {"_id": existing["_id"]},
                        {"$set": update_set}
                    )
                    existing.setdefault("limits", {})
                    existing["limits"]["daily_limit_gb"]   = new_daily
                    existing["limits"]["monthly_limit_gb"] = new_monthly
                    existing["limits"]["speed_limit_mbps"] = new_speed
                    existing["limits"]["ip_limit"]         = new_ip
                    existing["limits"]["device_limit"]     = new_device
                return convert_objectid_to_str(existing)

        alphabet = string.ascii_letters + string.digits
        token = ''.join(secrets.choice(alphabet) for _ in range(32))
        
        from Backend.config import Telegram as _Cfg
        _default_device = int(_Cfg.DEFAULT_DEVICE_LIMIT) if hasattr(_Cfg, "DEFAULT_DEVICE_LIMIT") else 0

        token_doc = {
            "name": name,
            "token": token,
            "user_id": user_id,
            "created_at": datetime.utcnow(),
            "expires_at": expires_at,
            "validity_days": validity_days,
            "portal_username": portal_username or None,
            "portal_password": portal_password or None,
            "limits": {
                "daily_limit_gb": daily_limit_gb if daily_limit_gb else 0,
                "monthly_limit_gb": monthly_limit_gb if monthly_limit_gb else 0,
                "speed_limit_mbps": speed_limit_mbps if speed_limit_mbps else 0,
                "ip_limit":     int(ip_limit)     if ip_limit     is not None else 0,
                "device_limit": int(device_limit) if device_limit is not None else _default_device,
            },
            "usage": {
                "total_bytes": 0,
                "daily": {"date": datetime.now(_TZ_IST).strftime("%Y-%m-%d"), "bytes": 0},
                "monthly": {"month": datetime.now(_TZ_IST).strftime("%Y-%m"), "bytes": 0}
            },
            "active_devices": [],   # Aktif stream session listesi
            "daily_limit_warned":   False,  # Günlük %80 uyarısı gönderildi mi
            "daily_limit_finished": False,  # Günlük %100 bitti bildirimi gönderildi mi
        }
        
        await self.dbs["tracking"]["api_tokens"].insert_one(token_doc)
        return convert_objectid_to_str(token_doc)

    async def get_api_token(self, token: str) -> Optional[dict]:
        doc = await self.dbs["tracking"]["api_tokens"].find_one({"token": token})
        return convert_objectid_to_str(doc) if doc else None

    async def regenerate_api_token(self, old_token: str) -> Optional[dict]:
        """Mevcut token kaydını (limitler, kullanım geçmişi, kullanıcı bağlantısı vb.
        her şeyi koruyarak) yeni rastgele bir token string'i ile değiştirir.
        Eski token anında geçersiz hale gelir; aktif cihaz oturumları temizlenir."""
        existing = await self.dbs["tracking"]["api_tokens"].find_one({"token": old_token})
        if not existing:
            return None

        alphabet = string.ascii_letters + string.digits
        new_token = ''.join(secrets.choice(alphabet) for _ in range(32))

        await self.dbs["tracking"]["api_tokens"].update_one(
            {"_id": existing["_id"]},
            {"$set": {"token": new_token, "active_devices": []}}
        )
        existing["token"] = new_token
        existing["active_devices"] = []
        return convert_objectid_to_str(existing)

    async def get_all_api_tokens(self) -> List[dict]:
        cursor = self.dbs["tracking"]["api_tokens"].find().sort("created_at", DESCENDING)
        tokens = await cursor.to_list(None)
        now = datetime.utcnow()
        result = []
        for token in tokens:
            # expires_at kontrolü: süresi dolmuş mu?
            exp = token.get("expires_at")
            if exp:
                token["is_expired"] = exp < now
            else:
                token["is_expired"] = False
            result.append(convert_objectid_to_str(token))
        return result

    async def revoke_api_token(self, token: str) -> bool:
        result = await self.dbs["tracking"]["api_tokens"].delete_one({"token": token})
        return result.deleted_count > 0

    async def purge_stream_analytics_for_token(self, token: str) -> int:
        """
        Bir token silindiğinde (abonelik iptali vb.) o token'a ait
        stream_analytics kayıtlarını da temizler. Aksi halde token
        api_tokens tablosundan silinmiş olsa bile stream_analytics'te
        "bugüne" ait kayıtlar kalmaya devam eder ve dashboard'daki
        "Uyarılar" kartında artık var olmayan üye için sahte bir
        "GB Tutarsızlığı" (yetim token) uyarısı olarak görünür.
        """
        if not token:
            return 0
        try:
            result = await self.dbs["tracking"]["stream_analytics"].delete_many({"user_token": token})
            return result.deleted_count
        except Exception as e:
            LOGGER.warning(f"purge_stream_analytics_for_token error: {e}")
            return 0

    async def link_token_user(self, token: str, user_id: int) -> bool:
        """Link an existing token to a Telegram user_id."""
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {"user_id": user_id}}
        )
        return result.modified_count > 0

    async def update_token_usage(self, token: str, bytes_delta: int):
        today_str = _daily_key()
        month_str = datetime.now(_TZ_IST).strftime("%Y-%m")

        # Tek bir atomic aggregation pipeline ile hem sıfırlama hem artırma yapılır.
        # Bu sayede gün/ay değişiminde race condition oluşmaz ve limit kontrolü
        # sıfırlanmış değerler üzerinden doğru çalışır.
        await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            [
                {"$set": {
                    "usage.daily": {
                        "$cond": [
                            {"$ne": [{"$ifNull": ["$usage.daily.date", ""]}, today_str]},
                            {"date": today_str, "bytes": bytes_delta},
                            {
                                "date": "$usage.daily.date",
                                "bytes": {"$add": [{"$ifNull": ["$usage.daily.bytes", 0]}, bytes_delta]}
                            }
                        ]
                    },
                    "usage.monthly": {
                        "$cond": [
                            {"$ne": [{"$ifNull": ["$usage.monthly.month", ""]}, month_str]},
                            {"month": month_str, "bytes": bytes_delta},
                            {
                                "month": "$usage.monthly.month",
                                "bytes": {"$add": [{"$ifNull": ["$usage.monthly.bytes", 0]}, bytes_delta]}
                            }
                        ]
                    },
                    "usage.total_bytes": {"$add": [{"$ifNull": ["$usage.total_bytes", 0]}, bytes_delta]}
                }}
            ]
        )

    async def reset_token_usage_if_needed(self, token: str):
        """
        Günlük/aylık kullanımı Türkiye saatiyle sıfırlar — stream olmadan da
        (ör. sayfa açılışında) tetiklenebilmesi için update_token_usage'dan ayrıldı.
        bytes_delta=0 ile çağrıldığında sadece tarih kontrolü yapıp sıfırlar.
        """
        today_str = _daily_key()
        month_str = datetime.now(_TZ_IST).strftime("%Y-%m")
        await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            [
                {"$set": {
                    "usage.daily": {
                        "$cond": [
                            {"$ne": [{"$ifNull": ["$usage.daily.date", ""]}, today_str]},
                            {"date": today_str, "bytes": 0},
                            "$usage.daily"
                        ]
                    },
                    "usage.monthly": {
                        "$cond": [
                            {"$ne": [{"$ifNull": ["$usage.monthly.month", ""]}, month_str]},
                            {"month": month_str, "bytes": 0},
                            "$usage.monthly"
                        ]
                    },
                }}
            ]
        )

    async def reset_all_daily_usage(self) -> int:
        """
        Tüm tokenların günlük kullanımını sıfırlar.
        db_scheduler tarafından UTC+3 gece 00:00'da çağrılır.
        Döndürür: sıfırlanan token sayısı.
        """
        today_str = _daily_key()
        month_str = datetime.now(_TZ_IST).strftime("%Y-%m")
        result = await self.dbs["tracking"]["api_tokens"].update_many(
            {},
            [
                {"$set": {
                    "usage.daily": {
                        "$cond": [
                            {"$ne": [{"$ifNull": ["$usage.daily.date", ""]}, today_str]},
                            {"date": today_str, "bytes": 0},
                            "$usage.daily"
                        ]
                    },
                    "usage.monthly": {
                        "$cond": [
                            {"$ne": [{"$ifNull": ["$usage.monthly.month", ""]}, month_str]},
                            {"month": month_str, "bytes": 0},
                            "$usage.monthly"
                        ]
                    },
                    # Yeni güne geçince %80 ve %100 uyarı bayraklarını sıfırla
                    "daily_limit_warned":   False,
                    "daily_limit_finished": False,
                    "daily_limit_disabled": False,
                }}
            ]
        )
        return result.modified_count

    async def get_token_daily_limit_warned(self, token: str) -> bool:
        """Token için bugün %80 uyarısı gönderildi mi?"""
        doc = await self.dbs["tracking"]["api_tokens"].find_one(
            {"token": token}, {"daily_limit_warned": 1}
        )
        return bool(doc and doc.get("daily_limit_warned"))

    async def mark_token_daily_limit_warned(self, token: str) -> None:
        """Token için %80 uyarısı gönderildi olarak işaretle."""
        await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {"daily_limit_warned": True}}
        )

    async def get_token_daily_limit_finished(self, token: str) -> bool:
        """Token için bugün %100 bitti bildirimi gönderildi mi?"""
        doc = await self.dbs["tracking"]["api_tokens"].find_one(
            {"token": token}, {"daily_limit_finished": 1}
        )
        return bool(doc and doc.get("daily_limit_finished"))

    async def mark_token_daily_limit_finished(self, token: str) -> None:
        """Token için %100 bitti bildirimi gönderildi olarak işaretle ve token'ı devre dışı bırak."""
        await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {"daily_limit_finished": True, "daily_limit_warned": True, "daily_limit_disabled": True}}
        )

    async def update_api_token_limits(self, token: str, daily_limit_gb: float, monthly_limit_gb: float,
                                       speed_limit_mbps: float = None,
                                       portal_username: str = None, portal_password: str = None,
                                       expires_at=None, clear_expiry: bool = False,
                                       validity_days: int = None,
                                       telegram_user_id: int = None,
                                       ip_limit: int = None, device_limit: int = None,
                                       monthly_request_limit: int = None) -> bool:
        update_fields = {
            "limits": {
                "daily_limit_gb": daily_limit_gb if daily_limit_gb else 0,
                "monthly_limit_gb": monthly_limit_gb if monthly_limit_gb else 0,
                "speed_limit_mbps": float(speed_limit_mbps) if speed_limit_mbps else 0,
                "ip_limit":     int(ip_limit)     if ip_limit     is not None else 0,
                "device_limit": int(device_limit) if device_limit is not None else 0,
                "monthly_request_limit": int(monthly_request_limit) if monthly_request_limit is not None else 0,
            }
        }
        if portal_username is not None:
            update_fields["portal_username"] = portal_username.strip() if portal_username.strip() else None
        if portal_password is not None:
            update_fields["portal_password"] = portal_password.strip() if portal_password.strip() else None

        # Geçerlilik süresi güncelleme
        if clear_expiry:
            # 0 gün = sınırsız: expires_at ve validity_days sıfırla
            update_fields["expires_at"] = None
            update_fields["validity_days"] = 0
        elif expires_at is not None:
            update_fields["expires_at"] = expires_at
            if validity_days is not None:
                update_fields["validity_days"] = validity_days

        # Telegram User ID güncelleme
        if telegram_user_id is not None:
            update_fields["user_id"] = telegram_user_id

        # Günlük limit artırıldıysa (daily_limit_gb > 0) daily_limit_disabled / warned / finished flag'larını sıfırla
        # Böylece önceki kota doldu uyarısı temizlenir ve stremio erişimi yeniden açılır.
        if daily_limit_gb and float(daily_limit_gb) > 0:
            update_fields["daily_limit_disabled"] = False
            update_fields["daily_limit_finished"] = False
            update_fields["daily_limit_warned"]   = False

        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": update_fields}
        )
        return result.modified_count > 0

    # ── Cihaz Takip Metodları ──────────────────────────────────────────────────

    async def add_device_session(self, token: str, session_id: str) -> bool:
        """
        Aktif stream session ekle (addToSet).
        Döner: True (yeni eklendi) / False (zaten var ya da hata)
        """
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$addToSet": {"active_devices": session_id}}
        )
        return result.modified_count > 0

    async def remove_device_session(self, token: str, session_id: str) -> bool:
        """Stream bitince aktif session'ı sil."""
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$pull": {"active_devices": session_id}}
        )
        return result.modified_count > 0

    async def get_active_device_count(self, token: str) -> int:
        """Token'ın anlık aktif stream (cihaz) sayısını döner.
        
        DB yerine ACTIVE_STREAMS RAM dict'ini kullanır — stremio_routes.py ile
        tutarlı olması için (stream'ler DB'ye değil RAM'e kaydediliyor).
        Sadece gerçekten veri transfer etmiş (total_bytes > 0) stream'leri sayar;
        video player'ın probe/range request'leri 0 byte ile kayıt açar ve bunlar
        sayıma dahil edilmez.
        """
        try:
            from Backend.helper.custom_dl import ACTIVE_STREAMS
            return sum(
                1 for s in ACTIVE_STREAMS.values()
                if s.get("status") == "active"
                and s.get("meta", {}).get("user_token") == token
                and (s.get("total_bytes") or 0) > 0
            )
        except Exception:
            # Fallback: DB'deki active_devices listesine bak
            doc = await self.dbs["tracking"]["api_tokens"].find_one(
                {"token": token}, {"active_devices": 1}
            )
            return len(doc.get("active_devices", [])) if doc else 0

    async def clear_device_sessions(self, token: str) -> bool:
        """Token'ın tüm aktif session'larını temizler (admin sıfırlama)."""
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {"active_devices": []}}
        )
        return result.modified_count > 0


    async def get_catalog_prefs(self, token: str) -> list:
        """Return list of hidden catalog IDs for this token (lang-agnostic base IDs)."""
        doc = await self.dbs["tracking"]["api_tokens"].find_one({"token": token})
        if not doc:
            return []
        return doc.get("hidden_catalogs", [])

    async def get_catalog_prefs_full(self, token: str) -> dict:
        """Return hidden_catalogs and catalog_order for this token."""
        doc = await self.dbs["tracking"]["api_tokens"].find_one({"token": token})
        if not doc:
            return {"hidden_catalogs": [], "catalog_order": []}
        return {
            "hidden_catalogs": doc.get("hidden_catalogs", []),
            "catalog_order":   doc.get("catalog_order", []),
        }

    async def get_channel_order(self, token: str) -> list:
        """Return user-specific live channel ordering for this token."""
        doc = await self.dbs["tracking"]["api_tokens"].find_one({"token": token})
        return doc.get("channel_order", []) if doc else []

    async def save_channel_order(self, token: str, channel_order: list) -> bool:
        """Persist user-specific live channel ordering for this token."""
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {"channel_order": channel_order}}
        )
        return result.modified_count > 0

    async def save_catalog_prefs(self, token: str, hidden_catalogs: list) -> bool:
        """Persist hidden catalog IDs for this token."""
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {"hidden_catalogs": hidden_catalogs}}
        )
        return result.modified_count > 0

    async def save_catalog_prefs_full(self, token: str, hidden_catalogs: list, catalog_order: list) -> bool:
        """Persist hidden catalog IDs and custom ordering for this token."""
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {"hidden_catalogs": hidden_catalogs, "catalog_order": catalog_order}}
        )
        return result.modified_count > 0

    # ── Üye erişim kısıtlamaları (admin → uye_detay.html) ────────────────────
    # Admin tarafından tek tek üyelere atanan: görebileceği kataloglar,
    # en fazla görebileceği içerik yaş sınırı (sertifika) ve sertifika
    # sınırından bağımsız her zaman erişebileceği video whitelist'i.
    # api_tokens dokümanı üzerinde "access_restrictions" alanı altında saklanır.

    async def get_member_access_restrictions(self, token: str) -> dict:
        """Bir üyenin (token) admin tarafından tanımlanmış erişim kısıtlamalarını döner.
        allowed_catalogs: None → kısıtlama yok (tüm kataloglar), list → sadece bu id'ler
        certification_max_age: None → sınır yok, int → izin verilen en yüksek yaş sınırı
        allowed_videos: [{"imdb_id","title","media_type"}] → sertifika sınırından muaf videolar
        only_selected_videos: bool → True ise üye SADECE selected_videos listesindeki
            içerikleri görebilir (katalog/sertifika kısıtlamalarının hepsinin önüne geçer)
        selected_videos: [{"imdb_id","title","media_type"}] → only_selected_videos=True
            iken üyenin görebileceği tek içerik kümesi
        include_live_collection: bool → only_selected_videos=True iken, film/dizi
            whitelist'ine ek olarak üyenin Canlı Yayın kataloğunu (tüm kanallar) da
            görebilmesini sağlar.
        """
        if not token:
            return {
                "allowed_catalogs":        None,
                "certification_max_age":   None,
                "allowed_videos":          [],
                "only_selected_videos":    False,
                "selected_videos":         [],
                "include_live_collection": False,
            }
        doc = await self.dbs["tracking"]["api_tokens"].find_one({"token": token})
        restr = (doc or {}).get("access_restrictions") or {}
        return {
            "allowed_catalogs":        restr.get("allowed_catalogs"),
            "certification_max_age":   restr.get("certification_max_age"),
            "allowed_videos":          restr.get("allowed_videos", []),
            "only_selected_videos":    bool(restr.get("only_selected_videos", False)),
            "selected_videos":         restr.get("selected_videos", []),
            "include_live_collection": bool(restr.get("include_live_collection", False)),
        }

    async def save_member_access_restrictions(
        self,
        token: str,
        allowed_catalogs: Optional[list],
        certification_max_age: Optional[int],
        allowed_videos: list,
        only_selected_videos: bool = False,
        selected_videos: Optional[list] = None,
        include_live_collection: bool = False,
    ) -> bool:
        """Bir üyenin erişim kısıtlamalarını kaydeder/günceller."""
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {"access_restrictions": {
                "allowed_catalogs":        allowed_catalogs,
                "certification_max_age":   certification_max_age,
                "allowed_videos":          allowed_videos,
                "only_selected_videos":    bool(only_selected_videos),
                "selected_videos":         selected_videos or [],
                "include_live_collection": bool(include_live_collection),
            }}}
        )
        return result.modified_count > 0

    # -------------------------------
    # Admin / Link Checker Methods
    # -------------------------------
    async def flag_dead_link(self, media_type: str, tmdb_id: int, db_index: int, quality_id: str) -> bool:
        """
        Flags a specific telegram quality entry as 'is_dead: True'.
        """
        db_key = f"storage_{db_index}"
        
        if media_type == "movie":
            # Direct update in the telegram array for movies
            result = await self.dbs[db_key]["movie"].update_one(
                {"tmdb_id": tmdb_id, "telegram.id": quality_id},
                {"$set": {"telegram.$.is_dead": True, "updated_on": datetime.utcnow()}}
            )
            return result.modified_count > 0
            
        elif media_type == "tv":
            # Nested update for TV (arrayFilters needed since we don't know the exact indices)
            # Find the TV show docs
            tv = await self.dbs[db_key]["tv"].find_one({"tmdb_id": tmdb_id})
            if not tv or "seasons" not in tv:
                return False
                
            found = False
            for s_idx, season in enumerate(tv["seasons"]):
                for e_idx, episode in enumerate(season.get("episodes", [])):
                    for q_idx, quality in enumerate(episode.get("telegram", [])):
                        if quality.get("id") == quality_id:
                            tv["seasons"][s_idx]["episodes"][e_idx]["telegram"][q_idx]["is_dead"] = True
                            found = True
                            break
                    if found: break
                if found: break
                
            if found:
                tv["updated_on"] = datetime.utcnow()
                result = await self.dbs[db_key]["tv"].replace_one({"tmdb_id": tmdb_id}, tv)
                return result.modified_count > 0
                
        return False

    async def get_all_dead_links(self) -> List[dict]:
        """
        Scans all active storage databases for both movies and TV shows, returning a
        flattened list of dead links with their metadata for the Admin UI.
        """
        dead_links = []
        
        for i in range(1, self.current_db_index + 1):
            db_key = f"storage_{i}"
            db = self.dbs[db_key]
            
            # --- Scan Movies ---
            # Match any movie where at least one telegram entry has is_dead=True
            movie_cursor = db["movie"].find({"telegram.is_dead": True})
            async for movie in movie_cursor:
                for quality in movie.get("telegram", []):
                    if quality.get("is_dead"):
                        dead_links.append({
                            "type": "movie",
                            "tmdb_id": movie.get("tmdb_id"),
                            "db_index": movie.get("db_index", i),
                            "title": movie.get("title"),
                            "year": movie.get("year"),
                            "poster": movie.get("poster"),
                            "quality_id": quality.get("id"),
                            "quality": quality.get("quality"),
                            "size": quality.get("size"),
                            "date_added": quality.get("date_added")
                        })
                        
            # --- Scan TV Shows ---
            # Match any TV where seasons.episodes.telegram.is_dead=True
            tv_cursor = db["tv"].find({"seasons.episodes.telegram.is_dead": True})
            async for tv in tv_cursor:
                title = tv.get("title")
                year = tv.get("year")
                poster = tv.get("poster")
                for season in tv.get("seasons", []):
                    s_num = season.get("season_number")
                    for ep in season.get("episodes", []):
                        e_num = ep.get("episode_number")
                        for quality in ep.get("telegram", []):
                            if quality.get("is_dead"):
                                dead_links.append({
                                    "type": "tv",
                                    "tmdb_id": tv.get("tmdb_id"),
                                    "db_index": tv.get("db_index", i),
                                    "title": f"{title} (S{s_num:02d}E{e_num:02d})",
                                    "year": year,
                                    "poster": poster,
                                    "season": s_num,
                                    "episode": e_num,
                                    "quality_id": quality.get("id"),
                                    "quality": quality.get("quality"),
                                    "size": quality.get("size"),
                                    "date_added": quality.get("date_added")
                                })
                                
        return dead_links

    # -------------------------------
    # Stream Analytics
    # -------------------------------

    async def log_stream_stats(self, stats: dict) -> None:
        """Persist a finished-stream record to the tracking DB for analytics."""
        try:
            record = {
                "stream_id":   stats.get("stream_id"),
                "msg_id":      stats.get("msg_id"),
                "chat_id":     stats.get("chat_id"),
                "dc_id":       stats.get("dc_id"),
                "title":              stats.get("meta", {}).get("title"),
                "imdb_id":            stats.get("meta", {}).get("imdb_id"),
                "certification_tr":   stats.get("meta", {}).get("certification_tr"),
                "certification_de":   stats.get("meta", {}).get("certification_de"),
                "certification_us":   stats.get("meta", {}).get("certification_us"),
                "user_token":         stats.get("meta", {}).get("user_token"),
                "client_index": stats.get("client_index"),
                "total_bytes": stats.get("total_bytes", 0),
                "duration_sec": round(stats.get("duration", 0.0), 2),
                "avg_mbps":    round(stats.get("avg_mbps", 0.0), 3),
                "peak_mbps":   round(stats.get("peak_mbps", 0.0), 3),
                "status":      stats.get("status", "finished"),
                "parallelism": stats.get("parallelism"),
                "chunk_size":  stats.get("chunk_size"),
                "logged_at":   datetime.utcnow(),
            }
            col = self.dbs["tracking"]["stream_analytics"]
            await col.insert_one(record)

            # Kayıt sayısı sınırı yok — sadece 30 günden eski kayıtlar
            # (yaş bazlı retention, TTL index ile de destekleniyor) silinir
            age_cutoff = datetime.utcnow() - _td(days=30)
            await col.delete_many({"logged_at": {"$lt": age_cutoff}})
        except Exception as e:
            LOGGER.warning(f"Stream analytics log failed: {e}")

    async def get_user_watch_history(self, token: str, limit: int = 60) -> list:
        """
        Kullanıcının izlediği içeriklerin imdb_id listesini döner (en son izlenenler başta).
        Sadece imdb_id alanı dolu olan kayıtlar dahil edilir.
        """
        try:
            col = self.dbs["tracking"]["stream_analytics"]
            pipeline = [
                {"$match": {
                    "user_token": token,
                    "imdb_id": {"$ne": None, "$exists": True},
                }},
                {"$sort": {"logged_at": -1}},
                {"$group": {
                    "_id": "$imdb_id",
                    "last_watched": {"$first": "$logged_at"},
                }},
                {"$sort": {"last_watched": -1}},
                {"$limit": limit},
                {"$project": {"_id": 0, "imdb_id": "$_id"}},
            ]
            rows = await col.aggregate(pipeline).to_list(None)
            return [r["imdb_id"] for r in rows]
        except Exception as e:
            LOGGER.warning(f"get_user_watch_history failed: {e}")
            return []

    async def get_watch_history_rich(self, token: str, limit: int = 40) -> list:
        """
        İzleme geçmişini detaylı döner: [{imdb_id, last_watched}, ...]
        En son izlenen başta gelir.
        """
        try:
            col = self.dbs["tracking"]["stream_analytics"]
            pipeline = [
                {"$match": {
                    "user_token": token,
                    "imdb_id": {"$ne": None, "$exists": True},
                }},
                {"$sort": {"logged_at": -1}},
                {"$group": {
                    "_id": "$imdb_id",
                    "last_watched": {"$first": "$logged_at"},
                    "watch_count": {"$sum": 1},
                }},
                {"$sort": {"last_watched": -1}},
                {"$limit": limit},
                {"$project": {"_id": 0, "imdb_id": "$_id",
                               "last_watched": 1, "watch_count": 1}},
            ]
            rows = await col.aggregate(pipeline).to_list(None)
            return rows
        except Exception as e:
            LOGGER.warning(f"get_watch_history_rich failed: {e}")
            return []

    async def get_similar_items(
        self,
        watched_imdb_ids: list,
        page: int = 1,
        page_size: int = 60,
        lang: str = "tr",
        last_watched_id: str = None,
        watch_history_rich: list = None,
    ) -> list:
        """
        "Sana Özel" kataloğu — en son izlenen içeriğe dayalı akıllı öneri algoritması.

        Öncelik sırası (en önde → en sonda):
          1. En son izlenen içeriğin aynı koleksiyonu / serisi (franchise)
          2. En son izlenen içeriğin oyuncuları
          3. En son izlenenle ortak tür (genre) eşleşmesi
          4. En çok izlenen oyuncu eşleşmesi
          5. En çok izlenen yıl aralığı (+/- 4 yıl)
          6. En çok izlenen sertifika (certification)
          7. Rating'e göre sıralı geri kalanlar

        Kurallar:
          - İzlenmiş içerikler listede YOK
          - Film + dizi karışık
          - Tüm storage DB'leri taranır
          - Maksimum 60 içerik döner
        """
        if not watched_imdb_ids:
            return []

        try:
            # ── Dile göre alan adları ────────────────────────────────────────────
            if lang == "de":
                genre_field = "genres_de"
                cert_field  = "certification_de"
            elif lang in ("en", "original"):
                genre_field = "genres"
                cert_field  = "certification_us"
            else:
                genre_field = "genres_tr"
                cert_field  = "certification_tr"

            PROJ = {
                "imdb_id": 1, "tmdb_id": 1,
                "title": 1, "title_tr": 1, "title_de": 1,
                "description": 1, "description_tr": 1, "description_de": 1,
                "genres": 1, "genres_tr": 1, "genres_de": 1,
                "poster": 1, "poster_tr": 1, "poster_de": 1,
                "backdrop": 1, "backdrop_tr": 1, "backdrop_de": 1,
                "logo": 1, "logo_tr": 1, "logo_de": 1,
                "rating": 1, "release_year": 1, "cast": 1,
                "runtime": 1, "media_type": 1,
                "collection_id": 1, "collection_name": 1,
                "certification_tr": 1, "certification_de": 1, "certification_us": 1,
            }

            # ── 1. İzlenmiş içeriklerden profil oluştur ──────────────────────────
            genre_counter: dict   = {}
            cast_counter:  dict   = {}
            year_counter:  dict   = {}
            cert_counter:  dict   = {}
            collection_ids: set   = set()
            watched_docs_by_id: dict = {}

            all_watched_ids = list(watched_imdb_ids)

            for i in range(1, self.current_db_index + 1):
                db_ref = self.dbs[f"storage_{i}"]
                for col_name in ("movie", "tv"):
                    docs = await db_ref[col_name].find(
                        {"imdb_id": {"$in": all_watched_ids}},
                        {
                            "imdb_id": 1,
                            genre_field: 1, "genres": 1, "genres_tr": 1,
                            "cast": 1,
                            "release_year": 1,
                            cert_field: 1, "certification_us": 1,
                            "certification_tr": 1, "certification_de": 1,
                            "collection_id": 1,
                            "_id": 0,
                        }
                    ).to_list(None)

                    for doc in docs:
                        iid = doc.get("imdb_id")
                        if iid:
                            doc["_col"] = col_name
                            watched_docs_by_id[iid] = doc

                        genres = (
                            doc.get(genre_field)
                            or doc.get("genres_tr")
                            or doc.get("genres")
                            or []
                        )
                        for g in genres:
                            genre_counter[g] = genre_counter.get(g, 0) + 1

                        for c in (doc.get("cast") or [])[:6]:
                            cast_counter[c] = cast_counter.get(c, 0) + 1

                        yr = doc.get("release_year")
                        if yr:
                            try:
                                year_counter[int(yr)] = year_counter.get(int(yr), 0) + 1
                            except (ValueError, TypeError):
                                pass

                        cert = (
                            doc.get(cert_field)
                            or doc.get("certification_us")
                            or doc.get("certification_tr")
                            or ""
                        )
                        if cert:
                            cert_counter[cert] = cert_counter.get(cert, 0) + 1

                        coll_id = doc.get("collection_id")
                        if coll_id:
                            collection_ids.add(str(coll_id))

            # Fallback: genre bulunamamış
            if not genre_counter and not cast_counter:
                return []

            # ── Profil özetleri ──────────────────────────────────────────────────
            top_genres = sorted(genre_counter, key=lambda g: genre_counter[g], reverse=True)[:6]
            top_casts  = sorted(cast_counter,  key=lambda c: cast_counter[c],  reverse=True)[:15]
            top_certs  = sorted(cert_counter,  key=lambda c: cert_counter[c],  reverse=True)[:3]

            # En çok izlenen yıl ağırlık merkezi
            if year_counter:
                total_w = sum(year_counter.values())
                avg_year = sum(y * w for y, w in year_counter.items()) / total_w
                year_min = int(avg_year) - 4
                year_max = int(avg_year) + 4
            else:
                year_min = year_max = None

            # ── En son izlenen içerik ────────────────────────────────────────────
            last_doc = None
            if last_watched_id and last_watched_id in watched_docs_by_id:
                last_doc = watched_docs_by_id[last_watched_id]
            elif watched_imdb_ids:
                # watch_history sıralı ise ilk eleman en son izlenen
                for iid in watched_imdb_ids:
                    if iid in watched_docs_by_id:
                        last_doc = watched_docs_by_id[iid]
                        break

            last_cast_set: set = set()
            last_collection_id = None
            last_genres: list  = []

            if last_doc:
                last_cast_set = set((last_doc.get("cast") or [])[:8])
                last_collection_id = last_doc.get("collection_id")
                if last_collection_id:
                    last_collection_id = str(last_collection_id)
                last_genres = (
                    last_doc.get(genre_field)
                    or last_doc.get("genres_tr")
                    or last_doc.get("genres")
                    or []
                )

            # ── 2. Aday içerikleri çek (izlenmemişler) ──────────────────────────
            # Tüm storage DB'leri taranır; en son izlenen içeriğin özellikleri
            # öncelikli filtre olarak kullanılır. Limit yoktur — sıralama sonrası
            # ilk 60 döndürülür.

            watched_set = set(watched_imdb_ids)

            # En son izlenen içeriğin genre ve oyuncularını öncelikli filtrele
            priority_clauses = []
            if last_genres:
                priority_clauses += [
                    {genre_field: {"$in": last_genres}},
                    {"genres_tr": {"$in": last_genres}},
                    {"genres":    {"$in": last_genres}},
                ]
            if last_cast_set:
                priority_clauses.append({"cast": {"$in": list(last_cast_set)}})
            if last_collection_id:
                priority_clauses.append({"collection_id": last_collection_id})

            # Genel profil filtreleri (fallback)
            general_clauses = [
                {genre_field: {"$in": top_genres}},
                {"genres_tr": {"$in": top_genres}},
                {"genres":    {"$in": top_genres}},
            ]
            if top_casts:
                general_clauses.append({"cast": {"$in": top_casts}})
            if collection_ids:
                general_clauses.append({"collection_id": {"$in": list(collection_ids)}})

            or_clauses = priority_clauses if priority_clauses else general_clauses

            base_match: dict = {
                "imdb_id": {"$nin": list(watched_set), "$ne": None, "$exists": True},
                "$or": or_clauses,
            }

            all_results: list = []
            seen_imdb: set    = set()

            for i in range(1, self.current_db_index + 1):
                db_ref = self.dbs[f"storage_{i}"]
                for col_name in ("movie", "tv"):
                    docs = await db_ref[col_name].find(
                        base_match, {**PROJ}
                    ).to_list(None)
                    for doc in docs:
                        iid = doc.get("imdb_id")
                        if not iid or iid in seen_imdb:
                            continue
                        seen_imdb.add(iid)
                        if not doc.get("media_type"):
                            doc["media_type"] = col_name
                        all_results.append(convert_objectid_to_str(doc))

            # ── 3. Akıllı sıralama ───────────────────────────────────────────────
            def _score(doc: dict) -> tuple:
                iid = doc.get("imdb_id", "")
                doc_genres = (
                    doc.get(genre_field)
                    or doc.get("genres_tr")
                    or doc.get("genres")
                    or []
                )
                doc_cast = set((doc.get("cast") or []))
                doc_cert = (
                    doc.get(cert_field)
                    or doc.get("certification_us")
                    or doc.get("certification_tr")
                    or ""
                )
                doc_year = None
                try:
                    doc_year = int(doc.get("release_year") or 0) or None
                except (ValueError, TypeError):
                    pass
                doc_coll = doc.get("collection_id")
                if doc_coll:
                    doc_coll = str(doc_coll)
                doc_rating = float(doc.get("rating") or 0)

                # — Öncelik 1: Aynı koleksiyon / seri (en son izlenenle) —
                p1 = 1 if (last_collection_id and doc_coll == last_collection_id) else 0

                # — Öncelik 2: En son izlenenin oyuncusu —
                p2 = len(last_cast_set & doc_cast)

                # — Öncelik 3: En son izlenenle ortak genre —
                p3 = len(set(last_genres) & set(doc_genres))

                # — Öncelik 4: Genel top genre skoru —
                p4 = sum(genre_counter.get(g, 0) for g in doc_genres)

                # — Öncelik 5: Genel top cast skoru —
                p5 = sum(cast_counter.get(c, 0) for c in doc_cast)

                # — Öncelik 6: Yıl aralığı eşleşmesi —
                p6 = 0
                if year_min and doc_year and year_min <= doc_year <= year_max:
                    p6 = 1

                # — Öncelik 7: Sertifika eşleşmesi —
                p7 = 1 if (top_certs and doc_cert in top_certs) else 0

                # — Öncelik 8: Rating —
                p8 = doc_rating

                return (p1, p2, p3, p4, p5, p6, p7, p8)

            all_results.sort(key=_score, reverse=True)

            # ── 4. Sayfalama — sayfa başına 15, toplam max 60 içerik ─────────────
            all_results = all_results[:60]
            skip = (page - 1) * page_size
            return all_results[skip: skip + page_size]

        except Exception as e:
            LOGGER.warning(f"get_similar_items failed: {e}")
            return []


    async def get_stream_analytics(self, limit: int = 20) -> dict:
        """Return summary stats + recent stream records from the tracking DB."""
        try:
            col = self.dbs["tracking"]["stream_analytics"]

            # Aggregate totals
            pipeline = [
                {"$group": {
                    "_id": None,
                    "total_streams":     {"$sum": 1},
                    "total_bytes":       {"$sum": "$total_bytes"},
                    "avg_speed":         {"$avg": "$avg_mbps"},
                    "peak_speed":        {"$max": "$peak_mbps"},
                    "avg_duration":      {"$avg": "$duration_sec"},
                }},
            ]
            agg = await col.aggregate(pipeline).to_list(1)
            summary = agg[0] if agg else {}
            summary.pop("_id", None)

            # Per-client breakdown
            per_client_pipeline = [
                {"$group": {
                    "_id":          "$client_index",
                    "streams":      {"$sum": 1},
                    "avg_mbps":     {"$avg": "$avg_mbps"},
                    "peak_mbps":    {"$max": "$peak_mbps"},
                    "total_bytes":  {"$sum": "$total_bytes"},
                }},
                {"$sort": {"_id": 1}},
            ]
            per_client = await col.aggregate(per_client_pipeline).to_list(None)
            for row in per_client:
                row["client_index"] = row.pop("_id")
                row["avg_mbps"]     = round(row.get("avg_mbps", 0), 3)
                row["peak_mbps"]    = round(row.get("peak_mbps", 0), 3)

            # Son 20 kayıt (en yeni önce)
            recent_cursor = col.find(
                {},
                {"_id": 0, "stream_id": 1, "client_index": 1, "dc_id": 1,
                 "total_bytes": 1, "duration_sec": 1, "avg_mbps": 1,
                 "peak_mbps": 1, "status": 1, "logged_at": 1, "title": 1}
            ).sort("logged_at", DESCENDING).limit(20)
            recent = await recent_cursor.to_list(None)
            for r in recent:
                if "logged_at" in r:
                    r["logged_at"] = r["logged_at"].isoformat()

            return {
                "summary":    summary,
                "per_client": per_client,
                "recent":     recent,
            }
        except Exception as e:
            LOGGER.error(f"get_stream_analytics error: {e}")
            return {"summary": {}, "per_client": [], "recent": []}

    async def get_daily_usage_discrepancies(self, threshold_gb: float = 0.05) -> list:
        """
        Her token için "Bugün" sayacı (usage.daily.bytes) ile izleme geçmişindeki
        (stream_analytics) bugüne ait toplam veriyi karşılaştırır ve aralarında
        anlamlı fark (threshold_gb üzeri) olan üyeleri döner.

        "Bugün", admin panelindeki günlük limit sıfırlama saatiyle aynı "sanal gün"
        mantığı (_daily_key) kullanılarak Europe/Istanbul saat dilimine göre hesaplanır;
        böylece iki taraf da aynı gün tanımını kullanır.
        """
        try:
            from Backend.config import Telegram as _Cfg

            # ── "Bugün" pencerisinin (Europe/Istanbul) UTC başlangıç/bitiş sınırlarını hesapla ──
            _raw = (_Cfg.LIMIT_SIFIRLAMA or "").strip()
            try:
                _rh, _rm = (int(x) for x in _raw.split(":"))
            except Exception:
                _rh, _rm = 0, 0

            now_ist = datetime.now(_TZ_IST)
            today_key = _daily_key()
            threshold_today = now_ist.replace(hour=_rh, minute=_rm, second=0, microsecond=0)
            if now_ist >= threshold_today:
                window_start_ist = threshold_today
            else:
                window_start_ist = threshold_today - _td(days=1)
            window_end_ist = window_start_ist + _td(days=1)

            window_start_utc = window_start_ist.astimezone(timezone.utc).replace(tzinfo=None)
            window_end_utc   = window_end_ist.astimezone(timezone.utc).replace(tzinfo=None)

            # ── İzleme geçmişinden bugüne ait token bazlı toplamlar ──
            col = self.dbs["tracking"]["stream_analytics"]
            pipe = [
                {"$match": {
                    "logged_at":  {"$gte": window_start_utc, "$lt": window_end_utc},
                    "user_token": {"$ne": None},
                }},
                {"$group": {"_id": "$user_token", "bytes": {"$sum": "$total_bytes"}}},
            ]
            history_rows = await col.aggregate(pipe).to_list(None)
            history_by_token = {r["_id"]: r["bytes"] for r in history_rows if r["_id"]}

            # ── Token tablosundan "Bugün" sayaçlarını al ──
            tokens = await self.dbs["tracking"]["api_tokens"].find(
                {}, {"_id": 0, "token": 1, "name": 1, "user_id": 1, "usage": 1}
            ).to_list(None)

            discrepancies = []
            seen_tokens = set()
            for t in tokens:
                token_str = t.get("token")
                if not token_str:
                    continue
                seen_tokens.add(token_str)
                usage = t.get("usage", {}) or {}
                daily = usage.get("daily", {}) or {}
                # "Bugün" sayacı sadece tarihi bugünün anahtarıyla eşleşiyorsa geçerlidir;
                # eşleşmiyorsa henüz sıfırlanmamış eski bir güne ait değer demektir → 0 kabul edilir.
                daily_bytes = daily.get("bytes", 0) if daily.get("date") == today_key else 0
                history_bytes = history_by_token.get(token_str, 0)

                diff_bytes = abs(history_bytes - daily_bytes)
                if diff_bytes / (1024 ** 3) < threshold_gb:
                    continue

                discrepancies.append({
                    "user_id":       t.get("user_id"),
                    "name":          t.get("name") or (f"Kullanıcı {t.get('user_id')}" if t.get("user_id") else f"Token …{token_str[-6:]}"),
                    "token":         token_str,
                    "history_bytes": history_bytes,
                    "daily_bytes":   daily_bytes,
                    "diff_bytes":    history_bytes - daily_bytes,
                })

            # Token'ı olmayan ama stream_analytics'te bugüne ait kaydı olan (yetim) tokenlar
            for tok_str, hbytes in history_by_token.items():
                if tok_str in seen_tokens:
                    continue
                if hbytes / (1024 ** 3) < threshold_gb:
                    continue
                discrepancies.append({
                    "user_id":       None,
                    "name":          f"Token …{tok_str[-6:]}" if tok_str else "Bilinmeyen",
                    "token":         tok_str,
                    "history_bytes": hbytes,
                    "daily_bytes":   0,
                    "diff_bytes":    hbytes,
                })

            discrepancies.sort(key=lambda x: abs(x["diff_bytes"]), reverse=True)
            return discrepancies
        except Exception as e:
            LOGGER.error(f"get_daily_usage_discrepancies error: {e}")
            return []

    async def get_daily_limit_reached_tokens(self) -> list:
        """
        Günlük veri limitine ulaşmış (daily_limit_finished == True) tokenları döner.
        Dashboard'daki "Uyarılar" kartında kullanılır.
        """
        try:
            tokens = await self.dbs["tracking"]["api_tokens"].find(
                {"daily_limit_finished": True},
                {"token": 1, "name": 1, "user_id": 1, "usage": 1, "limits": 1},
            ).to_list(None)

            result = []
            for t in tokens:
                usage = t.get("usage", {}) or {}
                limits = t.get("limits", {}) or {}
                daily_bytes = usage.get("daily", {}).get("bytes", 0)
                result.append({
                    "user_id":        t.get("user_id"),
                    "name":           t.get("name") or (f"Kullanıcı {t.get('user_id')}" if t.get("user_id") else None),
                    "token":          t.get("token"),
                    "daily_used_bytes":  daily_bytes,
                    "daily_limit_gb":    limits.get("daily_limit_gb", 0),
                })
            return result
        except Exception as e:
            LOGGER.error(f"get_daily_limit_reached_tokens error: {e}")
            return []

    async def get_expiring_soon_alerts(self, hours: int = 24) -> list:
        """
        Aboneliği önümüzdeki `hours` saat içinde sona erecek, hâlâ "active"
        durumdaki üyeleri döner. Dashboard'daki "Uyarılar" kartında kullanılır.
        (Telegram hatırlatma bildirimi ayrıca subscription_checker.py üzerinden
        gönderilir; bu fonksiyon yalnızca admin panelinde görünürlük sağlar.)
        """
        try:
            from datetime import timedelta
            now = datetime.utcnow()
            target_time = now + timedelta(hours=hours)
            cursor = self.dbs["tracking"]["users"].find(
                {
                    "subscription_expiry": {"$gt": now, "$lte": target_time},
                    "subscription_status": "active",
                },
                {"_id": 1, "first_name": 1, "username": 1, "subscription_expiry": 1},
            ).sort("subscription_expiry", ASCENDING)

            result = []
            async for u in cursor:
                uid = u.get("_id")
                name = u.get("first_name") or u.get("username") or (f"Kullanıcı {uid}" if uid else "Bilinmeyen üye")
                expiry = u.get("subscription_expiry")
                remaining = None
                if isinstance(expiry, datetime):
                    remaining = max(0, int((expiry - now).total_seconds() // 3600))
                result.append({
                    "user_id":         uid,
                    "name":            name,
                    "expires_at":      expiry.isoformat() if isinstance(expiry, datetime) else expiry,
                    "hours_remaining": remaining,
                })
            return result
        except Exception as e:
            LOGGER.error(f"get_expiring_soon_alerts error: {e}")
            return []

    async def get_expired_but_active_alerts(self) -> list:
        """
        Aboneliği zaten sona ermiş (subscription_expiry geçmiş) ama durumu hâlâ
        "active" olarak işaretli üyeleri döner — normalde günlük sıfırlama/expiry
        job'ı bunları "expired"e çevirir; burada görünmesi senkronizasyon veya
        gecikme kaynaklı bir tutarsızlığa işaret eder. Dashboard'daki "Uyarılar"
        kartında kullanılır.
        """
        try:
            now = datetime.utcnow()
            cursor = self.dbs["tracking"]["users"].find(
                {
                    "subscription_expiry": {"$lt": now},
                    "subscription_status": "active",
                },
                {"_id": 1, "first_name": 1, "username": 1, "subscription_expiry": 1},
            ).sort("subscription_expiry", ASCENDING)

            result = []
            async for u in cursor:
                uid = u.get("_id")
                name = u.get("first_name") or u.get("username") or (f"Kullanıcı {uid}" if uid else "Bilinmeyen üye")
                expiry = u.get("subscription_expiry")
                overdue_hours = None
                if isinstance(expiry, datetime):
                    overdue_hours = max(0, int((now - expiry).total_seconds() // 3600))
                result.append({
                    "user_id":       uid,
                    "name":          name,
                    "expired_at":    expiry.isoformat() if isinstance(expiry, datetime) else expiry,
                    "overdue_hours": overdue_hours,
                })
            return result
        except Exception as e:
            LOGGER.error(f"get_expired_but_active_alerts error: {e}")
            return []

    async def get_pending_content_request_members(self) -> list:
        """
        En az bir "pending" (onay bekleyen) içerik talebi olan üyeleri,
        talep sayısı ve en güncel talep bilgisiyle birlikte döner.
        Dashboard'daki "Uyarılar" kartında kullanılır.
        """
        try:
            col = self.dbs["tracking"]["content_requests"]
            pipe = [
                {"$match": {"status": "pending"}},
                {"$sort": {"created_at": -1}},
                {"$group": {
                    "_id":            "$user_id",
                    "count":          {"$sum": 1},
                    "last_title":     {"$first": "$title"},
                    "last_link":      {"$first": "$link"},
                    "last_media_type": {"$first": "$media_type"},
                    "first_requested_at": {"$min": "$created_at"},
                    "last_requested_at":  {"$max": "$created_at"},
                }},
                {"$sort": {"last_requested_at": -1}},
            ]
            rows = await col.aggregate(pipe).to_list(None)

            user_ids = [r["_id"] for r in rows if r.get("_id")]
            users_map: dict = {}
            if user_ids:
                ucursor = self.dbs["tracking"]["users"].find(
                    {"_id": {"$in": user_ids}}, {"_id": 1, "first_name": 1, "username": 1}
                )
                async for u in ucursor:
                    users_map[u["_id"]] = u

            result = []
            for r in rows:
                uid = r.get("_id")
                u = users_map.get(uid, {})
                name = u.get("first_name") or u.get("username") or (f"Kullanıcı {uid}" if uid else "Bilinmeyen üye")
                result.append({
                    "user_id":            uid,
                    "name":               name,
                    "pending_count":      r.get("count", 0),
                    "last_title":         r.get("last_title") or r.get("last_link") or "",
                    "last_media_type":    r.get("last_media_type"),
                    "first_requested_at": r.get("first_requested_at").isoformat() if r.get("first_requested_at") else None,
                    "last_requested_at":  r.get("last_requested_at").isoformat() if r.get("last_requested_at") else None,
                })
            return result
        except Exception as e:
            LOGGER.error(f"get_pending_content_request_members error: {e}")
            return []

    async def get_pending_subscription_payments(self) -> list:
        """
        Bir abonelik planı seçip ödeme/aboneliği henüz onaylanmamış (pending_payment
        alanı dolu olan) üyeleri döner. Dashboard'daki "Uyarılar" kartında kullanılır.
        """
        try:
            cursor = self.dbs["tracking"]["users"].find(
                {"pending_payment": {"$exists": True, "$ne": None}},
                {"_id": 1, "first_name": 1, "username": 1, "pending_payment": 1},
            ).sort("pending_payment.date", DESCENDING)
            users = await cursor.to_list(None)

            result = []
            for u in users:
                pp = u.get("pending_payment") or {}
                if not pp:
                    continue
                name = u.get("first_name") or u.get("username") or f"Kullanıcı {u.get('_id')}"
                requested_at = pp.get("date")
                result.append({
                    "user_id":      u.get("_id"),
                    "name":         name,
                    "plan_label":   pp.get("label") or "",
                    "duration_days": pp.get("duration", 0),
                    "price":        pp.get("price", 0),
                    "currency":     pp.get("currency", "TRY"),
                    "requested_at": requested_at.isoformat() if isinstance(requested_at, datetime) else requested_at,
                })
            return result
        except Exception as e:
            LOGGER.error(f"get_pending_subscription_payments error: {e}")
            return []

    async def get_bandwidth_stats(self) -> dict:
        """Kapsamlı bant genişliği istatistiklerini döndürür."""
        try:
            col = self.dbs["tracking"]["stream_analytics"]
            from datetime import timedelta

            now = datetime.utcnow()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            hour_start  = now.replace(minute=0, second=0, microsecond=0)
            week_start  = today_start - timedelta(days=6)

            # --- Anlık aktif stream hızı (sunucudan dışarı çıkan MB/s toplamı) ---
            try:
                from Backend.helper.custom_dl import ACTIVE_STREAMS
                active_streams = [v for v in ACTIVE_STREAMS.values() if v.get("status") == "active"]
                # instant_mbps: kullanıcıya gönderilen anlık hız (MB/s) → byte/s'e çevir
                instant_bytes = int(sum(v.get("instant_mbps", 0.0) for v in active_streams) * 1024 * 1024)
                instant_count = len(active_streams)
            except Exception:
                instant_bytes = 0
                instant_count = 0

            async def _sum_bytes(match):
                pipe = [{"$match": match}, {"$group": {"_id": None, "total": {"$sum": "$total_bytes"}}}]
                r = await col.aggregate(pipe).to_list(1)
                return r[0]["total"] if r else 0

            daily_bytes   = await _sum_bytes({"logged_at": {"$gte": today_start}})
            monthly_bytes = await _sum_bytes({"logged_at": {"$gte": month_start}})
            hourly_bytes  = await _sum_bytes({"logged_at": {"$gte": hour_start}})
            total_bytes   = await _sum_bytes({})

            # --- Son 7 gün günlük breakdown ---
            weekly_pipe = [
                {"$match": {"logged_at": {"$gte": week_start}}},
                {"$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$logged_at"}},
                    "bytes":        {"$sum": "$total_bytes"},
                    "streams":      {"$sum": 1},
                    "unique_users": {"$addToSet": "$user_token"},
                }},
                {"$sort": {"_id": 1}}
            ]
            weekly_raw = await col.aggregate(weekly_pipe).to_list(None)
            weekly_days = {}
            for i in range(7):
                d = (week_start + timedelta(days=i)).strftime("%Y-%m-%d")
                weekly_days[d] = {"date": d, "bytes": 0, "streams": 0, "unique_users": 0}
            for row in weekly_raw:
                if row["_id"] in weekly_days:
                    weekly_days[row["_id"]]["bytes"]        = row["bytes"]
                    weekly_days[row["_id"]]["streams"]      = row["streams"]
                    weekly_days[row["_id"]]["unique_users"] = len([u for u in row["unique_users"] if u])
            weekly_list = list(weekly_days.values())

            # --- Saatlik son 24 saat ---
            h24_start = now - timedelta(hours=23)
            hourly_pipe = [
                {"$match": {"logged_at": {"$gte": h24_start}}},
                {"$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%dT%H:00", "date": "$logged_at"}},
                    "bytes": {"$sum": "$total_bytes"},
                    "streams": {"$sum": 1}
                }},
                {"$sort": {"_id": 1}}
            ]
            hourly_raw  = await col.aggregate(hourly_pipe).to_list(None)
            hourly_map  = {r["_id"]: {"bytes": r["bytes"], "streams": r["streams"]} for r in hourly_raw}
            hourly_list = []
            for i in range(24):
                h = (h24_start + timedelta(hours=i)).replace(minute=0, second=0, microsecond=0)
                key = h.strftime("%Y-%m-%dT%H:00")
                hourly_list.append({"hour": key, **hourly_map.get(key, {"bytes": 0, "streams": 0})})

            # --- En çok veri çeken kullanıcılar (token bazlı) ---
            tokens = await self.dbs["tracking"]["api_tokens"].find(
                {}, {"_id": 0, "token": 1, "name": 1, "user_id": 1, "usage": 1}
            ).to_list(None)
            top_users = []
            for t in tokens:
                usage = t.get("usage", {})
                total = usage.get("total_bytes", 0)
                if total > 0:
                    top_users.append({
                        "name": t.get("name") or f"User {t.get('user_id', '?')}",
                        "user_id": t.get("user_id"),
                        "token": t.get("token", "")[:8] + "...",
                        "total_bytes": total,
                        "daily_bytes": usage.get("daily", {}).get("bytes", 0),
                        "monthly_bytes": usage.get("monthly", {}).get("bytes", 0),
                    })
            top_users.sort(key=lambda x: x["total_bytes"], reverse=True)
            top_users = top_users[:20]

            # --- En çok veri çeken içerikler (tüm zamanlar) ---
            top_content_pipe = [
                {"$match": {"title": {"$ne": None, "$exists": True}}},
                {"$group": {
                    "_id": "$title",
                    "bytes": {"$sum": "$total_bytes"},
                    "streams": {"$sum": 1},
                    "last_watched": {"$max": "$logged_at"}
                }},
                {"$sort": {"bytes": -1}},
                {"$limit": 200}
            ]
            top_content_raw = await col.aggregate(top_content_pipe).to_list(None)
            top_content_list = []
            for r in top_content_raw:
                lw = r.get("last_watched")
                top_content_list.append({
                    "title": r["_id"],
                    "bytes": r["bytes"],
                    "streams": r["streams"],
                    "last_watched": lw.isoformat() if lw else None
                })

            # --- Son 7 günde en çok veri çeken içerikler ---
            top_content_week_pipe = [
                {"$match": {"title": {"$ne": None, "$exists": True}, "logged_at": {"$gte": week_start}}},
                {"$group": {
                    "_id": "$title",
                    "bytes":        {"$sum": "$total_bytes"},
                    "streams":      {"$sum": 1},
                    "unique_users": {"$addToSet": "$user_token"},
                }},
                {"$sort": {"bytes": -1}},
                {"$limit": 10}
            ]
            top_content_week_raw = await col.aggregate(top_content_week_pipe).to_list(None)
            top_content_week = [
                {
                    "title":        r["_id"],
                    "bytes":        r["bytes"],
                    "streams":      r["streams"],
                    "unique_users": len([u for u in r["unique_users"] if u]),
                }
                for r in top_content_week_raw
            ]

            return {
                "instant": {"bytes": instant_bytes, "active_streams": instant_count},
                "hourly_bytes": hourly_bytes,
                "daily_bytes": daily_bytes,
                "monthly_bytes": monthly_bytes,
                "total_bytes": total_bytes,
                "weekly": weekly_list,
                "hourly_24h": hourly_list,
                "top_users": top_users,
                "top_content": top_content_list,
                "top_content_week": top_content_week,
            }
        except Exception as e:
            LOGGER.error(f"get_bandwidth_stats error: {e}")
            return {
                "instant": {"bytes": 0, "active_streams": 0},
                "hourly_bytes": 0, "daily_bytes": 0, "monthly_bytes": 0, "total_bytes": 0,
                "weekly": [], "hourly_24h": [], "top_users": [], "top_content": [], "top_content_week": []
            }

    # ──────────────────────────────────────────────────────────
    # Abone (member) tek kullanımlık oturum yönetimi
    # ──────────────────────────────────────────────────────────

    async def create_member_otp(self, user_id: int, username: str,
                                photo_url: str = "") -> dict:
        """
        Tek kullanımlık kullanıcı adı + şifre üretir ve veritabanına yazar.
        Her /start çağrısında ESKİ oturumu siler, yeni üretir.
        photo_url: Telegram profil fotoğrafının sunucu URL'si (opsiyonel).
        """
        import secrets as _secrets
        import string as _string

        # Rastgele kısa kullanıcı adı (okunabilir)
        adjectives = ["hızlı","cesur","zeki","güçlü","sakin","kızıl","derin","şen","sessiz","gümüş","asil"]
        nouns      = ["aslan","kartal","tilki","kurt","yıldız","pars","kaplan","şahin","güneş","fırtına","panter"]
        adj  = _secrets.choice(adjectives)
        noun = _secrets.choice(nouns)
        rand = _secrets.randbelow(9000) + 1000        # 4 haneli
        otp_username = f"{adj}{noun}{rand}"           # ör: hızlıkartal5821

        alphabet  = _string.ascii_letters + _string.digits
        otp_pass  = "".join(_secrets.choice(alphabet) for _ in range(12))

        pass_hash = _hash_password(otp_pass)

        import uuid as _uuid
        now = datetime.utcnow()
        new_session_id = str(_uuid.uuid4())  # /start her cagrildiginda degisir → eski cookie gecersiz kalir
        await self.dbs["tracking"]["member_sessions"].update_one(
            {"user_id": user_id},
            {"$set": {
                "user_id":      user_id,
                "display_name": username,
                "photo_url":    photo_url,
                "otp_username": otp_username,
                "pass_hash":    pass_hash,
                "session_id":   new_session_id,
                "used":         False,
                "created_at":   now,
                "session_expires": None,   # login sonrası 72 saat uzatılır
            }},
            upsert=True
        )
        return {"username": otp_username, "password": otp_pass}

    async def verify_member_otp(self, username: str, password: str) -> Optional[dict]:
        """
        Kullanıcı adı + şifre doğrular. Başarılıysa session kaydını döner
        ve 'used' bayrağını kaldırır (artık kalıcı oturum gibi davranır).
        """
        from datetime import timedelta

        # Hash'i DB'den kullanıcı adına göre çek, sonra güvenli karşılaştır
        doc = await self.dbs["tracking"]["member_sessions"].find_one({
            "otp_username": username,
        })
        if not doc or not _verify_password(password, doc.get("pass_hash", "")):
            doc = None
        if doc is None:
            return None
        # Eski SHA-256 kaydını scrypt'e yükselt (otomatik migration)
        if not doc.get("pass_hash", "").startswith("scrypt$"):
            await self.dbs["tracking"]["member_sessions"].update_one(
                {"otp_username": username},
                {"$set": {"pass_hash": _hash_password(password)}},
            )
        user_id = doc["user_id"]
        user    = await self.get_user(user_id)

        # Ban kontrolü
        if user and user.get("subscription_status") == "banned":
            return None

        # Abonelik kontrolü
        now = datetime.utcnow()
        if user:
            expiry = user.get("subscription_expiry")
            if expiry and expiry < now:
                return None
            if user.get("subscription_status") not in ("active",):
                return None

        # Oturumu 72 saat uzat
        from datetime import timedelta as _timedelta
        new_expiry = now + _timedelta(hours=72)
        await self.dbs["tracking"]["member_sessions"].update_one(
            {"_id": doc["_id"]},
            {"$set": {"used": True, "session_expires": new_expiry}}
        )
        doc["user_id"]         = user_id
        doc["session_expires"] = new_expiry.isoformat()

        # Token bul
        all_tokens = await self.get_all_api_tokens()
        token_doc  = next((t for t in all_tokens if t.get("user_id") == user_id), None)
        doc["token"]      = token_doc["token"] if token_doc else None
        doc["token_doc"]  = token_doc
        # session_id: /start ile uretilmis olan deger (eski cookie gecersizlestirme icin)
        doc["session_id"] = doc.get("session_id", "")
        return doc

    async def get_member_session_by_token(self, token: str) -> Optional[dict]:
        """Oturum cookie'sindeki token'ı çöz."""
        doc = await self.dbs["tracking"]["member_sessions"].find_one({"token": token})
        return doc

    async def get_member_session_id(self, user_id: int) -> str:
        """DB'deki gecerli session_id'yi doner. Cookie kontrolu icin kullanilir."""
        doc = await self.dbs["tracking"]["member_sessions"].find_one({"user_id": user_id})
        return doc.get("session_id", "") if doc else ""

    async def invalidate_member_session(self, user_id: int):
        """Cikis islemi — session'i sil."""
        await self.dbs["tracking"]["member_sessions"].delete_one({"user_id": user_id})

    # ──────────────────────────────────────────────────────────────────
    # Admin OTP — yönetici paneli giriş bilgileri
    # ──────────────────────────────────────────────────────────────────

    async def create_admin_otp(self, photo_url: str = "",
                               display_name: str = "Yönetici") -> dict:
        """
        OWNER'ın /start komutuyla tetiklenen yönetici giriş bilgileri.
        Her çağrıda önceki kayıt silinir (invalidate_admin_session ile),
        ardından yeni kullanıcı adı + şifre üretilir ve DB'ye yazılır.
        photo_url:    Telegram profil fotoğrafının sunucu URL'si (opsiyonel).
        display_name: Panelde gösterilecek Telegram adı.
        Dönen dict: {"username": ..., "password": ...}
        """
        import secrets as _secrets
        import string as _string
        adjectives = ["hızlı","cesur","zeki","güçlü","sakin","kızıl","derin","şen","sessiz","gümüş","asil"]
        nouns      = ["aslan","kartal","tilki","kurt","yıldız","pars","kaplan","şahin","güneş","fırtına","panter"]
        adj        = _secrets.choice(adjectives)
        noun       = _secrets.choice(nouns)
        rand       = _secrets.randbelow(9000) + 1000
        otp_username = f"{adj}{noun}{rand}"

        alphabet  = _string.ascii_letters + _string.digits + "!@#$%"
        otp_pass  = "".join(_secrets.choice(alphabet) for _ in range(14))
        pass_hash = _hash_password(otp_pass)

        now = datetime.utcnow()
        # Mevcut session_version'ı koru (invalidate_admin_session artırmış olabilir)
        existing = await self.dbs["tracking"]["admin_sessions"].find_one({"_id": "admin"})
        current_version = existing.get("session_version", 0) if existing else 0
        await self.dbs["tracking"]["admin_sessions"].update_one(
            {"_id": "admin"},
            {"$set": {
                "_id":             "admin",
                "otp_username":    otp_username,
                "pass_hash":       pass_hash,
                "photo_url":       photo_url,
                "display_name":    display_name,
                "used":            False,
                "created_at":      now,
                "session_version": current_version,
            }},
            upsert=True
        )
        return {"username": otp_username, "password": otp_pass}

    async def invalidate_admin_session(self):
        """
        Eski yönetici oturumunu geçersiz kılar:
        - OTP/kimlik bilgilerini siler
        - session_version'ı artırır (aktif tarayıcı cookie'lerini otomatik geçersiz kılar)
        Bot yeniden başladığında veya /start çağrıldığında tetiklenir.
        """
        # Mevcut session_version'ı oku, +1 artır
        doc = await self.dbs["tracking"]["admin_sessions"].find_one({"_id": "admin"})
        new_version = (doc.get("session_version", 0) + 1) if doc else 1
        await self.dbs["tracking"]["admin_sessions"].update_one(
            {"_id": "admin"},
            {"$set": {
                "session_version": new_version,
                "otp_username":    None,
                "pass_hash":       None,
                "used":            False,
                "created_at":      None,
            }},
            upsert=True,
        )

    async def get_admin_session_version(self) -> int:
        """
        Geçerli session_version değerini döner.
        require_auth tarafından cookie'nin hâlâ geçerli olup olmadığını
        kontrol etmek için kullanılır.
        """
        doc = await self.dbs["tracking"]["admin_sessions"].find_one({"_id": "admin"})
        return doc.get("session_version", 0) if doc else 0

    async def verify_admin_credentials(self, username: str, password: str) -> Optional[dict]:
        """
        Yönetici paneli giriş doğrulaması.
        DB'deki admin_sessions kaydıyla karşılaştırır.
        Başarılıysa kaydı döner (truthy — photo_url ve display_name dahil);
        başarısızsa None döner.
        """
        if not username or not password:
            return None

        # Kullanıcı adına göre kaydı çek, hash'i güvenli karşılaştır
        doc = await self.dbs["tracking"]["admin_sessions"].find_one({
            "_id":          "admin",
            "otp_username": username,
        })
        if not doc or not _verify_password(password, doc.get("pass_hash", "")):
            return None
        # Eski SHA-256 kaydını scrypt'e yükselt (otomatik migration)
        if not doc.get("pass_hash", "").startswith("scrypt$"):
            await self.dbs["tracking"]["admin_sessions"].update_one(
                {"_id": "admin", "otp_username": username},
                {"$set": {"pass_hash": _hash_password(password)}},
            )
        return doc

    async def rename_movie_quality(self, tmdb_id: int, db_index: int, quality_id: str, new_name: str) -> bool:
        """Film kalitesinin 'name' alanını güncelle."""
        db_key = f"storage_{db_index}"
        movie = await self.dbs[db_key]["movie"].find_one({"tmdb_id": tmdb_id})
        if not movie or "telegram" not in movie:
            return False
        found = False
        for q in movie["telegram"]:
            if q.get("id") == quality_id:
                q["name"] = new_name
                found = True
                break
        if not found:
            return False
        movie["updated_on"] = datetime.utcnow()
        result = await self.dbs[db_key]["movie"].replace_one({"tmdb_id": tmdb_id}, movie)
        return result.modified_count > 0

    async def rename_tv_quality(self, tmdb_id: int, db_index: int, season_number: int, episode_number: int, quality_id: str, new_name: str) -> bool:
        """Dizi bölümü kalitesinin 'name' alanını güncelle."""
        db_key = f"storage_{db_index}"
        tv = await self.dbs[db_key]["tv"].find_one({"tmdb_id": tmdb_id})
        if not tv or "seasons" not in tv:
            return False
        found = False
        for season in tv["seasons"]:
            if season.get("season_number") == season_number:
                for episode in season.get("episodes", []):
                    if episode.get("episode_number") == episode_number:
                        for q in episode.get("telegram", []):
                            if q.get("id") == quality_id:
                                q["name"] = new_name
                                found = True
                                break
                    if found:
                        break
            if found:
                break
        if not found:
            return False
        tv["updated_on"] = datetime.utcnow()
        result = await self.dbs[db_key]["tv"].replace_one({"tmdb_id": tmdb_id}, tv)
        return result.modified_count > 0

    # ── IP Ban yönetimi (brute_force.py tarafından kullanılır) ───────────────

    async def set_ip_ban(self, ip: str, ban_until: float) -> None:
        """
        Bir IP adresini ban_until (unix timestamp) zamanına kadar banla.
        Kayıt zaten varsa günceller (upsert). MongoDB TTL index'i ban_until
        geçtikten sonra kaydı otomatik siler.
        """
        await self.dbs["tracking"]["ip_bans"].update_one(
            {"ip": ip},
            {"$set": {
                "ip": ip,
                # MongoDB TTL index datetime alanı bekler — float'ı dönüştür
                "ban_until": datetime.utcfromtimestamp(ban_until),
            }},
            upsert=True,
        )

    async def get_ip_ban(self, ip: str) -> Optional[float]:
        """
        IP adresinin ban bitiş zamanını unix timestamp olarak döndürür.
        Kayıt yoksa veya süresi dolmuşsa None döner.
        """
        doc = await self.dbs["tracking"]["ip_bans"].find_one({"ip": ip})
        if not doc:
            return None
        ban_until_dt: datetime = doc.get("ban_until")
        if ban_until_dt is None:
            return None
        return ban_until_dt.replace(tzinfo=None).timestamp()

    async def delete_ip_ban(self, ip: str) -> None:
        """Süresi dolmuş veya kaldırılmış bir IP ban kaydını sil."""
        await self.dbs["tracking"]["ip_bans"].delete_one({"ip": ip})

    # ── Altyazı Yönetimi ────────────────────────────────────────────────────

    async def add_subtitle(self, subtitle_data: dict) -> str:
        """
        Yeni bir altyazı kaydı ekle.
        subtitle_data: {imdb_id, media_type, lang, lang_label, season, episode, filename, file_path, file_size, uploaded_at}
        Döner: eklenen belgenin _id string hali
        """
        from bson import ObjectId
        doc = dict(subtitle_data)
        doc.setdefault("uploaded_at", datetime.utcnow())
        result = await self.dbs["tracking"]["subtitles"].insert_one(doc)
        return str(result.inserted_id)

    async def get_subtitles(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> list:
        """
        Belirli bir içeriğe ait altyazıları listele.
        Film için season/episode=None, dizi bölümü için değer ver.
        """
        from Backend.helper.database import convert_objectid_to_str
        query: dict = {"imdb_id": imdb_id}
        if season is not None:
            query["season"] = season
        if episode is not None:
            query["episode"] = episode
        cursor = self.dbs["tracking"]["subtitles"].find(query).sort("uploaded_at", -1)
        docs = await cursor.to_list(length=200)
        return [convert_objectid_to_str(d) for d in docs]

    async def delete_subtitle(self, subtitle_id: str) -> bool:
        """_id'ye göre altyazı kaydını sil. True → silindi."""
        from bson import ObjectId
        try:
            oid = ObjectId(subtitle_id)
        except Exception:
            return False
        result = await self.dbs["tracking"]["subtitles"].delete_one({"_id": oid})
        return result.deleted_count > 0

    async def get_subtitle_by_id(self, subtitle_id: str) -> Optional[dict]:
        """Tek bir altyazı belgesini _id ile getir."""
        from bson import ObjectId
        from Backend.helper.database import convert_objectid_to_str
        try:
            oid = ObjectId(subtitle_id)
        except Exception:
            return None
        doc = await self.dbs["tracking"]["subtitles"].find_one({"_id": oid})
        return convert_objectid_to_str(doc) if doc else None

    # ------------------------------------------------------------------ #
    #  İçerik Talep (İstek) Metodları                                     #
    # ------------------------------------------------------------------ #

    async def get_user_request_limit(self, user_id: int) -> int:
        """
        Kullanıcının aylık istek limitini döndürür.
        Önce kullanıcıya bağlı token'ın limits.monthly_request_limit alanına bakar,
        sonra abonelik planına bakar. 0 → sınırsız.
        """
        # 1. Kullanıcıya bağlı token'dan bak (ek paket eklenince burası güncellenir)
        try:
            token_doc = await self.dbs["tracking"]["api_tokens"].find_one(
                {"$or": [{"user_id": user_id}, {"user_id": str(user_id)}]},
                {"limits.monthly_request_limit": 1}
            )
            if token_doc:
                token_limit = int((token_doc.get("limits") or {}).get("monthly_request_limit") or 0)
                if token_limit > 0:
                    return token_limit
        except Exception:
            pass

        # 2. Kullanıcının plan_id'sine göre plan koleksiyonuna bak
        user = await self.get_user(user_id)
        if not user:
            return 0

        plan_limit = 0
        plan_id = user.get("plan_id") or user.get("pending_payment", {}).get("plan_id")
        if plan_id:
            try:
                plan = await self.dbs["tracking"]["sub_plans"].find_one({"_id": ObjectId(plan_id)})
                if plan:
                    plan_limit = int(plan.get("monthly_request_limit") or 0)
            except Exception:
                pass

        # 3. Ek paket ile eklenen ekstra istek hakkını da ekle
        addon_extra = int(user.get("addon_extra_requests") or 0)

        total = plan_limit + addon_extra
        return total

    async def count_user_requests_this_month(self, user_id: int) -> int:
        """
        Kullanıcının bu takvim ayında yaptığı içerik talebi sayısını döndürür.
        """
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        count = await self.dbs["tracking"]["content_requests"].count_documents({
            "user_id": user_id,
            "created_at": {"$gte": month_start}
        })
        return count

    async def add_content_request(
        self,
        user_id: int,
        link: str,
        media_type: str,
        tmdb_id: int = 0,
        title: str = "",
        poster: str = "",
        source: str = "bot",
    ) -> str:
        """
        Yeni bir içerik talebi kaydeder ve oluşturulan belge _id'sini string olarak döndürür.
        title/poster verilirse (ör. bot /istek komutunda TMDB/IMDB'den çözüldüyse)
        web akışıyla (submit_content_request) aynı şekilde kaydedilir; böylece
        hatirlatmalar.html'deki "İçerik İstekleri" listesinde poster ve isim de görünür.
        """
        result = await self.dbs["tracking"]["content_requests"].insert_one({
            "user_id": user_id,
            "link": link,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "title": title,
            "poster": poster,
            "status": "pending",
            "source": source,
            "created_at": datetime.utcnow(),
        })
        return str(result.inserted_id)

    async def set_content_request_admin_messages(self, request_id: str, admin_messages: list) -> bool:
        """
        İçerik talebinin yönetici(ler)e gönderilen Telegram bildirim mesajlarının
        (chat_id/message_id) listesini kaydeder. Bu sayede talep herhangi bir yerden
        (bot butonu veya web panelinden) onaylanıp/reddedildiğinde, tüm yöneticilerin
        gördüğü bot mesajları senkron biçimde güncellenebilir.
        """
        try:
            result = await self.dbs["tracking"]["content_requests"].update_one(
                {"_id": ObjectId(request_id)},
                {"$set": {"admin_messages": admin_messages}}
            )
            return result.modified_count > 0
        except Exception:
            return False

    async def get_content_request(self, request_id: str) -> Optional[dict]:
        """
        Belirtilen _id'ye sahip içerik talebini döndürür.
        """
        try:
            doc = await self.dbs["tracking"]["content_requests"].find_one({"_id": ObjectId(request_id)})
            return doc
        except Exception:
            return None

    async def update_content_request_status(self, request_id: str, status: str) -> bool:
        """
        İçerik talebinin durumunu günceller (pending → approved / rejected).
        """
        try:
            result = await self.dbs["tracking"]["content_requests"].update_one(
                {"_id": ObjectId(request_id)},
                {"$set": {"status": status, "updated_at": datetime.utcnow()}}
            )
            return result.modified_count > 0
        except Exception:
            return False

    async def delete_user_content_requests(self, user_id: int) -> int:
        """
        Kullanıcıya ait tüm içerik taleplerini siler.
        Silinen kayıt sayısını döndürür.
        """
        result = await self.dbs["tracking"]["content_requests"].delete_many({"user_id": user_id})
        return result.deleted_count

    # ─────────────────────────────────────────────────────────────────────
    # İstekler sayacı + Web Push (yönetici tarayıcı bildirimleri)
    # ─────────────────────────────────────────────────────────────────────
    # base.html'deki "İstekler" sidebar linkinin yanında gösterilen sayı
    # (bekleyen içerik talebi + bekleyen abonelik talebi) ve yöneticinin
    # tarayıcısına gönderilen Web Push bildirimleri için kullanılan yardımcılar.

    async def get_istekler_pending_count(self) -> dict:
        """
        İstekler sidebar rozeti için bekleyen talep sayılarını döner.
        content_pending      → bekleyen içerik (film/dizi) talebi sayısı
        subscription_pending → bekleyen abonelik (ödeme) talebi sayısı
        total                → ikisinin toplamı (rozette gösterilen sayı)
        """
        try:
            content_pending = await self.dbs["tracking"]["content_requests"].count_documents(
                {"status": "pending"}
            )
        except Exception:
            content_pending = 0
        try:
            subscription_pending = await self.dbs["tracking"]["users"].count_documents(
                {"pending_payment": {"$exists": True, "$ne": None}}
            )
        except Exception:
            subscription_pending = 0
        return {
            "content_pending": content_pending,
            "subscription_pending": subscription_pending,
            "total": content_pending + subscription_pending,
        }

    async def get_or_create_vapid_keys(self) -> dict:
        """
        Web Push bildirimleri için VAPID anahtar çiftini döner. İlk çağrıda
        henüz anahtar yoksa üretir ve tracking.settings koleksiyonuna kaydeder
        (sunucu her yeniden başladığında aynı anahtarlar kullanılır, aksi
        halde tarayıcıdaki eski abonelikler geçersiz kalırdı).
        """
        col = self.dbs["tracking"]["settings"]
        doc = await col.find_one({"_id": "vapid_keys"})
        if doc and doc.get("public_key") and doc.get("private_key"):
            return {"public_key": doc["public_key"], "private_key": doc["private_key"]}

        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        import base64

        priv = ec.generate_private_key(ec.SECP256R1())
        pub_bytes = priv.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        priv_bytes = priv.private_numbers().private_value.to_bytes(32, "big")

        def _b64u(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

        keys = {"public_key": _b64u(pub_bytes), "private_key": _b64u(priv_bytes)}
        await col.update_one({"_id": "vapid_keys"}, {"$set": keys}, upsert=True)
        return keys

    async def add_push_subscription(self, subscription: dict, user_agent: str = "") -> None:
        """Yöneticinin tarayıcısından gelen Push aboneliğini kaydeder/günceller."""
        endpoint = (subscription or {}).get("endpoint")
        if not endpoint:
            return
        await self.dbs["tracking"]["push_subscriptions"].update_one(
            {"_id": endpoint},
            {"$set": {
                "endpoint":    endpoint,
                "subscription": subscription,
                "user_agent":  (user_agent or "")[:300],
                "updated_at":  datetime.utcnow(),
            }},
            upsert=True,
        )

    async def remove_push_subscription(self, endpoint: str) -> None:
        if not endpoint:
            return
        await self.dbs["tracking"]["push_subscriptions"].delete_one({"_id": endpoint})

    async def list_push_subscriptions(self) -> list:
        cursor = self.dbs["tracking"]["push_subscriptions"].find({})
        return await cursor.to_list(length=1000)

    async def cleanup_expired_ip_bans(self) -> int:
        """
        Süresi dolmuş tüm IP ban kayıtlarını sil.
        MongoDB TTL index bunu otomatik yapar ancak bu metot anında
        temizlik için periyodik görevden (brute_force.cleanup_expired_bans)
        çağrılır. Silinen kayıt sayısını döndürür.
        """
        result = await self.dbs["tracking"]["ip_bans"].delete_many(
            {"ban_until": {"$lt": datetime.utcnow()}}
        )
        return result.deleted_count

    async def get_plan_image(self) -> Optional[str]:
        """
        Abonelik planları mesajında kullanılacak resmin Telegram file_id'sini döndürür.
        Resim ayarlanmamışsa None döner.
        """
        doc = await self.dbs["tracking"]["bot_settings"].find_one({"_id": "plan_image"})
        if doc:
            return doc.get("file_id")
        return None

    async def set_plan_image(self, file_id: str) -> bool:
        """
        Abonelik planları mesajında kullanılacak resmin Telegram file_id'sini kaydeder.
        Bot yeniden başlasa bile MongoDB'de kalır.
        """
        try:
            await self.dbs["tracking"]["bot_settings"].update_one(
                {"_id": "plan_image"},
                {"$set": {"file_id": file_id, "updated_at": datetime.utcnow()}},
                upsert=True
            )
            return True
        except Exception as e:
            print(f"set_plan_image error: {e}")
            return False

    async def delete_plan_image(self) -> bool:
        """
        Kayıtlı plan resmini siler; bundan sonra mesaj olarak gönderilir.
        """
        try:
            await self.dbs["tracking"]["bot_settings"].delete_one({"_id": "plan_image"})
            return True
        except Exception as e:
            print(f"delete_plan_image error: {e}")
            return False

    # ------------------------------------------------------------------
    # Yükselt (/yukselt) komutunda gösterilecek resim — /plan2 ile ayarlanır
    # ------------------------------------------------------------------
    async def get_upgrade_image(self) -> Optional[str]:
        """
        /yukselt komutunda gösterilecek resmin Telegram file_id'sini döndürür.
        Resim ayarlanmamışsa None döner.
        """
        doc = await self.dbs["tracking"]["bot_settings"].find_one({"_id": "upgrade_image"})
        if doc:
            return doc.get("file_id")
        return None

    async def set_upgrade_image(self, file_id: str) -> bool:
        try:
            await self.dbs["tracking"]["bot_settings"].update_one(
                {"_id": "upgrade_image"},
                {"$set": {"file_id": file_id, "updated_at": datetime.utcnow()}},
                upsert=True
            )
            return True
        except Exception as e:
            print(f"set_upgrade_image error: {e}")
            return False

    async def delete_upgrade_image(self) -> bool:
        try:
            await self.dbs["tracking"]["bot_settings"].delete_one({"_id": "upgrade_image"})
            return True
        except Exception as e:
            print(f"delete_upgrade_image error: {e}")
            return False

    # ------------------------------------------------------------------
    # Ek Paketler — sadece /yukselt komutunda görünür
    # ------------------------------------------------------------------
    async def get_addon_packages(self) -> List[dict]:
        cursor = self.dbs["tracking"]["addon_packages"].find().sort("price", ASCENDING)
        packages = await cursor.to_list(None)
        return [convert_objectid_to_str(p) for p in packages]

    async def add_addon_package(
        self,
        label: str,
        price: float,
        extra_days: int = 0,
        extra_daily_gb: float = 0,
        extra_monthly_gb: float = 0,
        extra_speed_mbps: float = 0,
        extra_requests: int = 0,
    ) -> Optional[str]:
        result = await self.dbs["tracking"]["addon_packages"].insert_one({
            "label": label,
            "price": price,
            "extra_days": extra_days,
            "extra_daily_gb": extra_daily_gb,
            "extra_monthly_gb": extra_monthly_gb,
            "extra_speed_mbps": extra_speed_mbps,
            "extra_requests": extra_requests,
            "created_at": datetime.utcnow(),
        })
        return str(result.inserted_id)

    async def update_addon_package(
        self,
        pkg_id: str,
        label: str,
        price: float,
        extra_days: int = 0,
        extra_daily_gb: float = 0,
        extra_monthly_gb: float = 0,
        extra_speed_mbps: float = 0,
        extra_requests: int = 0,
    ) -> bool:
        try:
            result = await self.dbs["tracking"]["addon_packages"].update_one(
                {"_id": ObjectId(pkg_id)},
                {"$set": {
                    "label": label,
                    "price": price,
                    "extra_days": extra_days,
                    "extra_daily_gb": extra_daily_gb,
                    "extra_monthly_gb": extra_monthly_gb,
                    "extra_speed_mbps": extra_speed_mbps,
                    "extra_requests": extra_requests,
                    "updated_at": datetime.utcnow(),
                }}
            )
            return result.modified_count > 0
        except Exception:
            return False

    async def delete_addon_package(self, pkg_id: str) -> bool:
        try:
            result = await self.dbs["tracking"]["addon_packages"].delete_one({"_id": ObjectId(pkg_id)})
            return result.deleted_count > 0
        except Exception:
            return False

    async def set_pending_addon(
        self,
        user_id: int,
        pkg_id: str,
        label: str,
        price: float,
        extra_days: int,
        extra_daily_gb: float,
        extra_monthly_gb: float,
        extra_speed_mbps: float,
        extra_requests: int,
        admin_messages: list = None,
    ):
        data = {
            "pending_addon": {
                "pkg_id": pkg_id,
                "label": label,
                "price": price,
                "extra_days": extra_days,
                "extra_daily_gb": extra_daily_gb,
                "extra_monthly_gb": extra_monthly_gb,
                "extra_speed_mbps": extra_speed_mbps,
                "extra_requests": extra_requests,
                "date": datetime.utcnow(),
            }
        }
        if admin_messages is not None:
            data["pending_addon"]["admin_messages"] = admin_messages
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": data},
            upsert=True
        )

    async def approve_addon(self, user_id: int) -> Optional[dict]:
        """
        Bekleyen ek paketi onaylar; kullanıcının limitlerini ve abonelik süresini artırır.
        """
        user = await self.get_user(user_id)
        if not user or "pending_addon" not in user:
            return None

        addon = user["pending_addon"]
        extra_days      = int(addon.get("extra_days", 0))
        extra_daily_gb  = float(addon.get("extra_daily_gb", 0))
        extra_monthly_gb = float(addon.get("extra_monthly_gb", 0))
        extra_speed_mbps = float(addon.get("extra_speed_mbps", 0))
        extra_requests  = int(addon.get("extra_requests", 0))

        now = datetime.utcnow()

        # Abonelik süresini uzat
        set_fields = {}
        if extra_days > 0:
            current_expiry = user.get("subscription_expiry")
            if current_expiry and current_expiry > now:
                from datetime import timedelta
                new_expiry = current_expiry + timedelta(days=extra_days)
            else:
                from datetime import timedelta
                new_expiry = now + timedelta(days=extra_days)
            set_fields["subscription_expiry"] = new_expiry
            set_fields["subscription_status"] = "active"

        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {
                "$set": set_fields,
                "$unset": {"pending_addon": ""},
                "$inc": {
                    "addon_extra_daily_gb": extra_daily_gb,
                    "addon_extra_monthly_gb": extra_monthly_gb,
                    "addon_extra_speed_mbps": extra_speed_mbps,
                    "addon_extra_requests": extra_requests,
                }
            }
        )

        # API token limitlerini güncelle — mevcut plan + ek paket toplamı
        try:
            token_doc = await self.dbs["tracking"]["api_tokens"].find_one(
                {"$or": [{"user_id": str(user_id)}, {"user_id": int(user_id)}]}
            )
            if token_doc:
                limits = token_doc.get("limits", {})

                # Token'daki request limiti 0 ise planın kendi limitini fallback olarak al
                token_req = int(limits.get("monthly_request_limit", 0) or 0)
                if token_req == 0 and extra_requests > 0:
                    plan_req = 0
                    plan_id_str = user.get("plan_id", "")
                    if plan_id_str:
                        try:
                            plan_doc = await self.dbs["tracking"]["sub_plans"].find_one({"_id": ObjectId(plan_id_str)})
                            if plan_doc:
                                plan_req = int(plan_doc.get("monthly_request_limit", 0) or 0)
                        except Exception:
                            pass
                    token_req = plan_req

                new_daily    = (limits.get("daily_limit_gb",    0) or 0) + extra_daily_gb
                new_monthly  = (limits.get("monthly_limit_gb",  0) or 0) + extra_monthly_gb
                new_speed    = (limits.get("speed_limit_mbps",  0) or 0) + extra_speed_mbps
                new_requests = token_req + extra_requests
                await self.dbs["tracking"]["api_tokens"].update_many(
                    {"$or": [{"user_id": str(user_id)}, {"user_id": int(user_id)}]},
                    {"$set": {
                        "limits.daily_limit_gb":        new_daily,
                        "limits.monthly_limit_gb":      new_monthly,
                        "limits.speed_limit_mbps":      new_speed,
                        "limits.monthly_request_limit": new_requests,
                    }}
                )
        except Exception as e:
            print(f"approve_addon token update error: {e}")

        return await self.get_user(user_id)

    async def reject_addon(self, user_id: int) -> bool:
        result = await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$unset": {"pending_addon": ""}}
        )
        return result.modified_count > 0
