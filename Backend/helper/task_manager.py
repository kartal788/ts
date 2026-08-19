from asyncio import sleep
from time import monotonic
from pyrogram.errors import FloodWait, RPCError
from Backend.logger import LOGGER
from Backend.pyrofork.bot import Helper

# ----- MESSAGE_DELETE_FORBIDDEN circuit breaker ---------------------------
# Helper istemcisinin bir sohbette silme yetkisi yoksa (ör. o mesajları başka
# bir hesap/bot yüklediyse), her mükerrer video için tekrar tekrar 403 alıp
# logu doldurmak yerine, bir sohbette bu hata bir kez görüldüğünde belirli bir
# süre boyunca o sohbet için silme denemeleri atlanır ve tek bir uyarı basılır.
_FORBIDDEN_CHATS: dict[int, float] = {}
_FORBIDDEN_COOLDOWN = 3600  # saniye — bu süre sonunda tekrar denenir


async def edit_message(chat_id: int, msg_id: int, new_caption: str):
    try:
        await Helper.edit_message_caption(
            chat_id=chat_id,
            message_id=msg_id,
            caption=new_caption
        )
        await sleep(2)
    except FloodWait as e:
        LOGGER.warning(f"FloodWait for {e.value} seconds while editing message {msg_id} in {chat_id}")
        await sleep(e.value)
    except Exception as e:
        LOGGER.error(f"Error while editing message {msg_id} in {chat_id}: {e}")

async def delete_message(chat_id: int, msg_id: int):
    blocked_since = _FORBIDDEN_CHATS.get(chat_id)
    if blocked_since is not None:
        if monotonic() - blocked_since < _FORBIDDEN_COOLDOWN:
            # Bu sohbette silme yetkisi olmadığı zaten biliniyor —
            # boşuna Telegram'a istek atıp logu kirletmeden atla.
            return
        # Cooldown doldu, tekrar denenebilir.
        _FORBIDDEN_CHATS.pop(chat_id, None)

    try:
        await Helper.delete_messages(
            chat_id=chat_id,
            message_ids=msg_id
        )
        await sleep(2)
        LOGGER.info(f"Deleted message {msg_id} in {chat_id}")
    except FloodWait as e:
        LOGGER.warning(f"FloodWait for {e.value} seconds while deleting message {msg_id} in {chat_id}")
        await sleep(e.value)
    except RPCError as e:
        if "MESSAGE_DELETE_FORBIDDEN" in str(e):
            _FORBIDDEN_CHATS[chat_id] = monotonic()
            LOGGER.warning(
                f"Helper istemcisinin {chat_id} sohbetinde mesaj silme yetkisi yok "
                f"(msg {msg_id}). Bu sohbet için silme denemeleri {_FORBIDDEN_COOLDOWN}s "
                f"boyunca atlanacak. Kalıcı çözüm: Helper bot hesabını bu kanalda/grupta "
                f"'Mesajları Sil' yetkisiyle yönetici yapın."
            )
        else:
            LOGGER.error(f"Error while deleting message {msg_id} in {chat_id}: {e}")
    except Exception as e:
        LOGGER.error(f"Error while deleting message {msg_id} in {chat_id}: {e}")
