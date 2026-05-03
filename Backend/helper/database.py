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
from pymongo import ASCENDING, DESCENDING
from typing import Dict, List, Optional, Tuple, Any

from Backend.logger import LOGGER
from Backend.config import Telegram
import re
from Backend.helper.encrypt import decode_string
from Backend.helper.modal import Episode, MovieSchema, QualityDetail, Season, TVShowSchema
from Backend.helper.task_manager import delete_message


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

            # stream_analytics: TTL'yi 10 güne düşür, mevcut eski index'i yeniden oluştur
            try:
                col_analytics = self.dbs["tracking"]["stream_analytics"]
                # Eski TTL index'ini sil (farklı expireAfterSeconds ile yeniden oluşturmak için)
                try:
                    await col_analytics.drop_index("logged_at_1")
                except Exception:
                    pass
                await col_analytics.create_index(
                    "logged_at",
                    expireAfterSeconds=10 * 24 * 3600,  # 10 gün
                    background=True,
                )
                # Mevcut 10 günden eski kayıtları hemen temizle
                cutoff = datetime.utcnow() - _td(days=10)
                deleted = await col_analytics.delete_many({"logged_at": {"$lt": cutoff}})
                if deleted.deleted_count:
                    LOGGER.info(f"stream_analytics: {deleted.deleted_count} eski kayıt temizlendi (>10 gün)")
            except Exception as idx_err:
                LOGGER.warning(f"stream_analytics TTL index: {idx_err}")

        except Exception as e:
            LOGGER.error(f"Database connection error: {e}")

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

        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {
                "$set": {"subscription_expiry": new_expiry, "subscription_status": "active"},
                "$unset": {"pending_payment": "", "reminder_sent": ""}
            }
        )

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

        plan_daily_gb   = 0.0
        plan_monthly_gb = 0.0
        plan_speed_mbps = 0.0
        if plan_doc is not None:
            plan_daily_gb   = float(plan_doc.get("daily_limit_gb",  0) or 0)
            plan_monthly_gb = float(plan_doc.get("monthly_limit_gb", 0) or 0)
            plan_speed_mbps = float(plan_doc.get("speed_limit_mbps", 0) or 0)
            # Token zaten varsa anında güncelle (hem str hem int user_id için)
            await self.dbs["tracking"]["api_tokens"].update_many(
                {"$or": [{"user_id": str(user_id)}, {"user_id": int(user_id)}]},
                {"$set": {
                    "limits.daily_limit_gb":   plan_daily_gb,
                    "limits.monthly_limit_gb": plan_monthly_gb,
                    "limits.speed_limit_mbps": plan_speed_mbps,
                }}
            )

        user_data = await self.get_user(user_id)
        if user_data is not None:
            # Plan limitlerini çağıran koda ilet — add_api_token bu değerleri kullanacak
            user_data["_plan_daily_gb"]   = plan_daily_gb
            user_data["_plan_monthly_gb"] = plan_monthly_gb
            user_data["_plan_speed_mbps"] = plan_speed_mbps
        return user_data

    async def reject_payment(self, user_id: int) -> bool:
        result = await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$unset": {"pending_payment": ""}}
        )
        return result.modified_count > 0

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

    async def add_subscription_plan(self, days: int, price: float, label: str = "", currency: str = "USD", is_unlimited: bool = False, daily_limit_gb: float = 0, monthly_limit_gb: float = 0, speed_limit_mbps: float = 0) -> Optional[str]:
        result = await self.dbs["tracking"]["sub_plans"].insert_one({
            "days": days,
            "price": price,
            "label": label,
            "currency": currency,
            "is_unlimited": is_unlimited,
            "daily_limit_gb": daily_limit_gb,
            "monthly_limit_gb": monthly_limit_gb,
            "speed_limit_mbps": speed_limit_mbps,
            "created_at": datetime.utcnow()
        })
        return str(result.inserted_id)

    async def update_subscription_plan(self, plan_id: str, days: int, price: float, label: str = "", currency: str = "USD", is_unlimited: bool = False, daily_limit_gb: float = 0, monthly_limit_gb: float = 0, speed_limit_mbps: float = 0) -> bool:
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
            return result.modified_count > 0
            
        elif action == "delete":
            result = await self.dbs["tracking"]["users"].update_one(
                {"_id": user_id},
                {"$unset": {"subscription_expiry": "", "subscription_status": ""}}
            )
            return result.modified_count > 0
            
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
        channel: int, msg_id: int, size: str, name: str
    ) -> Optional[ObjectId]:
        result = await self._insert_media_internal(metadata_info, channel, msg_id, size, name)
        if result is not None:
            try:
                from Backend.helper.tmdb_catalog import notify_new_content
                notify_new_content()
            except Exception:
                pass
        return result

    async def _insert_media_internal(
        self, metadata_info: dict,
        channel: int, msg_id: int, size: str, name: str
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
                telegram=[QualityDetail(
                    quality=metadata_info['quality'],
                    id=metadata_info['encoded_string'],
                    name=name,
                    size=size,
                    is_archive=bool(metadata_info.get('_is_archive', False))
                )]
            )
            return await self.update_movie(media)
        else:
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
                            is_archive=bool(metadata_info.get('_is_archive', False))
                        )]
                    )]
                )]
            )
            return await self.update_tv_show(tv_show)

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

        if Telegram.REPLACE_MODE:
            to_delete = [q for q in existing_qualities if q.get("quality") == target_quality]

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
                q for q in existing_qualities if q.get("quality") != target_quality
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

                    if Telegram.REPLACE_MODE:
                        to_delete = [
                            q for q in existing_episode["telegram"]
                            if q.get("quality") == target_quality
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
                            if q.get("quality") != target_quality
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

    async def search_documents(
            self, 
            query: str, 
            page: int, 
            page_size: int
        ) -> dict:

            skip = (page - 1) * page_size

            # ── ReDoS & Regex Injection koruması ─────────────────────────────
            # Kullanıcı girdisindeki tüm regex özel karakterleri escape edilir
            # (re.escape → ".", "*", "+", "(", ")", "[" vb. → "\.", "\*" ...).
            # Boş/sadece-boşluk girdi reddedilir; kelime başına uzunluk sınırı
            # uygulanarak aşırı uzun token'larla tetiklenen ReDoS önlenir.
            import re as _re

            _MAX_QUERY_LEN  = 100   # toplam girdi karakter sınırı
            _MAX_WORD_LEN   = 40    # tek kelime karakter sınırı
            _MAX_WORD_COUNT = 10    # maksimum kelime sayısı

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

            # Her kelime bağımsız lookahead ile eşleştirilir:
            # (?=.*kelime1)(?=.*kelime2)... → kelime sırası önemli değil,
            # hepsi metinde geçmeli. Tek .* yerine lookahead kullanmak
            # MongoDB regex motorundaki backtracking yükünü önemli ölçüde azaltır.
            pattern = "".join(f"(?=.*{w})" for w in words)
            regex_query = {
                '$regex': pattern,
                '$options': 'i'
            }
            
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
            combined = tv_results + movie_results
            results.extend(combined)
            
            if len(results) < page_size:
                previous_db_index = self.current_db_index - 1
                while previous_db_index > 0 and len(results) < page_size:
                    prev_db_key = f"storage_{previous_db_index}"
                    prev_db = self.dbs[prev_db_key]
                    tv_results_prev = await prev_db["tv"].aggregate(tv_pipeline).to_list(None)
                    movie_results_prev = await prev_db["movie"].aggregate(movie_pipeline).to_list(None)
                    combined_prev = tv_results_prev + movie_results_prev
                    results.extend(combined_prev)
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
                        {"seasons.episodes.telegram.name": regex_query}
                    ]
                })
                movie_count = await db["movie"].count_documents({
                    "$or": [
                        {"title": regex_query},
                        {"title_de": regex_query},
                    {"title_tr": regex_query},
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

    async def delete_document(self, media_type: str, tmdb_id: int, db_index: int) -> bool:
        db_key = f"storage_{db_index}"

        if media_type == "Movie":
            doc = await self.dbs[db_key]["movie"].find_one({"tmdb_id": tmdb_id})
            if doc and "telegram" in doc:
                for quality in doc["telegram"]:
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
        result = await self.dbs["tracking"]["live"].insert_one(data)
        data["_id"] = str(result.inserted_id)
        return convert_objectid_to_str(data)

    async def update_live_channel(self, channel_id: str, data: dict) -> bool:
        """Mevcut bir kanalı günceller."""
        from bson import ObjectId as _OID
        data["updated_at"] = datetime.utcnow()
        result = await self.dbs["tracking"]["live"].update_one(
            {"_id": _OID(channel_id)}, {"$set": data}
        )
        return result.modified_count > 0

    async def delete_live_channel(self, channel_id: str) -> bool:
        """Bir kanalı siler."""
        from bson import ObjectId as _OID
        result = await self.dbs["tracking"]["live"].delete_one({"_id": _OID(channel_id)})
        return result.deleted_count > 0

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
        result = await self.dbs["tracking"]["broadcasts"].insert_one(data)
        data["_id"] = str(result.inserted_id)
        return convert_objectid_to_str(data)

    async def update_broadcast(self, broadcast_id: str, data: dict) -> bool:
        """Mevcut yayını günceller."""
        from bson import ObjectId as _OID
        data["updated_at"] = datetime.utcnow()
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
                                       ip_limit: int = None, device_limit: int = None) -> bool:
        update_fields = {
            "limits": {
                "daily_limit_gb": daily_limit_gb if daily_limit_gb else 0,
                "monthly_limit_gb": monthly_limit_gb if monthly_limit_gb else 0,
                "speed_limit_mbps": float(speed_limit_mbps) if speed_limit_mbps else 0,
                "ip_limit":     int(ip_limit)     if ip_limit     is not None else 0,
                "device_limit": int(device_limit) if device_limit is not None else 0,
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

            # Kullanıcı başına maksimum 10 kayıt — eskiden fazlası silinir
            user_token = record.get("user_token")
            if user_token:
                count = await col.count_documents({"user_token": user_token})
                if count > 10:
                    oldest = await col.find(
                        {"user_token": user_token},
                        {"_id": 1}
                    ).sort("logged_at", 1).limit(count - 10).to_list(None)
                    old_ids = [d["_id"] for d in oldest]
                    await col.delete_many({"_id": {"$in": old_ids}})
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
