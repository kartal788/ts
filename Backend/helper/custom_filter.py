from pyrogram.filters import create
from Backend.config import Telegram


def is_admin_id(uid) -> bool:
    """OWNER_ID veya panelden eklenen yönetici (APPROVER_IDS) mi?
    Telegram.APPROVER_IDS Ayarlar sayfasından canlı güncellendiği için her çağrıda yeniden okunur."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return False
    return uid == Telegram.OWNER_ID or uid in (Telegram.APPROVER_IDS or [])


class CustomFilters:

    @staticmethod
    async def owner_filter(client, message):
        user = message.from_user or message.sender_chat
        if user is None:
            return False
        return is_admin_id(user.id)

    # Not: adı "owner" olarak kaldı (68 yerde kullanılıyor); artık OWNER + yöneticileri kapsar.
    owner = create(owner_filter)
