from asyncio import gather, create_task
from pyrogram import Client
from Backend.logger import LOGGER
from Backend.config import Telegram
from Backend.pyrofork.bot import multi_clients, work_loads, StreamBot, client_dc_map
from os import environ

class TokenParser:
    @staticmethod
    def parse_from_env():
        env_tokens = [
            t for _, t in sorted(
                filter(
                    lambda n: n[0].startswith("MULTI_TOKEN") and bool(n[1].strip()),
                    environ.items()
                )
            )
        ]

        #----- Ayarlar sayfasından eklenen ek token'lar (SettingsManager)
        try:
            from Backend.helper.settings_manager import SettingsManager
            settings_tokens = [
                t.strip() for t in (SettingsManager.current().multi_tokens or [])
                if t and t.strip()
            ]
        except Exception:
            settings_tokens = []

        all_tokens = env_tokens + [t for t in settings_tokens if t not in env_tokens]
        return {c + 1: t for c, t in enumerate(all_tokens)}

async def start_client(client_id, token):
    try:
        LOGGER.info(f"Starting - Bot Client {client_id}")
        client = await Client(
            name=str(client_id),
            api_id=Telegram.API_ID,
            api_hash=Telegram.API_HASH,
            bot_token=token,
            sleep_threshold=100,
            no_updates=True,
            in_memory=True
        ).start()
        
        try:
            client_dc = await client.storage.dc_id()
            client_dc_map[client_id] = client_dc
            LOGGER.info(f"Client {client_id} connected to DC {client_dc}")
        except Exception as e:
            LOGGER.warning(f"Could not get DC for Client {client_id}: {e}")
            client_dc_map[client_id] = None
        
        work_loads[client_id] = 0
        return client_id, client
    except Exception as e:
        LOGGER.error(f"Failed to start Client - {client_id} Error: {e}", exc_info=True)
        return None

async def initialize_clients():
    multi_clients[0], work_loads[0] = StreamBot, 0
    
    try:
        main_dc = await StreamBot.storage.dc_id()
        client_dc_map[0] = main_dc
        LOGGER.info(f"Main StreamBot connected to DC {main_dc}")
    except Exception as e:
        LOGGER.warning(f"Could not get DC for StreamBot: {e}")
        client_dc_map[0] = None
    
    all_tokens = TokenParser.parse_from_env()
    if not all_tokens:
        LOGGER.info("No additional Bot Clients found, Using default client")
        return

    tasks = [create_task(start_client(i, token)) for i, token in all_tokens.items()]
    clients = await gather(*tasks)
    clients = {client_id: client for result in clients if result is not None for client_id, client in [result]}
    multi_clients.update(clients)

    if len(multi_clients) != 1:
        LOGGER.info(f"Multi-Client Mode Enabled with {len(multi_clients)} clients")
        LOGGER.info(f"DC Distribution: {client_dc_map}")
    else:
        LOGGER.info("No additional clients were initialized, using default client")

async def stop_client(client_id: int) -> None:
    """Tek bir ek bot istemcisini durdurur ve kayıtlarını temizler."""
    client = multi_clients.pop(client_id, None)
    work_loads.pop(client_id, None)
    client_dc_map.pop(client_id, None)
    if client:
        try:
            await client.stop()
            LOGGER.info(f"Stopped Bot Client {client_id}")
        except Exception as e:
            LOGGER.warning(f"Error stopping Client {client_id}: {e}")


async def reload_multi_token_clients() -> dict:
    """Ayarlar sayfasından çoklu token listesi değiştiğinde çağrılır:
    artık listede olmayan istemcileri durdurur, yeni eklenenleri başlatır."""
    desired_tokens = TokenParser.parse_from_env()  # env + ayarlar birleşik liste

    # Şu an çalışan ek istemciler (0 = ana StreamBot, o hariç tutulur)
    current_ids = [cid for cid in multi_clients.keys() if cid != 0]

    stopped = 0
    started = 0

    # Artık istenmeyen (id aralığı dışında kalan) istemcileri durdur
    max_desired_id = max(desired_tokens.keys()) if desired_tokens else 0
    for cid in list(current_ids):
        if cid > max_desired_id:
            await stop_client(cid)
            stopped += 1

    # Eksik olan istemcileri başlat
    tasks = []
    for cid, token in desired_tokens.items():
        if cid not in multi_clients:
            tasks.append(create_task(start_client(cid, token)))

    if tasks:
        results = await gather(*tasks)
        for result in results:
            if result is not None:
                client_id, client = result
                multi_clients[client_id] = client
                started += 1

    return {
        "started": started,
        "stopped": stopped,
        "total_clients": len(multi_clients),
    }
