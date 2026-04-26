from asyncio import get_event_loop, sleep as asleep
import asyncio
import logging
from traceback import format_exc
from pyrogram import idle
from Backend import __version__, db
from Backend.helper.pinger import ping
from Backend.logger import LOGGER
from Backend.fastapi import server
from Backend.helper.pyro import restart_notification, setup_bot_commands
from Backend.pyrofork.bot import Helper, StreamBot
from Backend.pyrofork.clients import initialize_clients
from Backend.helper.link_checker import DeadLinkChecker
from Backend.fastapi.main import app

loop = get_event_loop()


async def _restore_persistent_files():
    """Bot başlarken MongoDB'deki kalıcı dosyaları diske geri yükler."""
    from pathlib import Path
    files_to_restore = [
        ("rclone_conf",  Path(__file__).parent.parent / "rclone.conf"),
        ("gdrive_pickle", Path(__file__).parent.parent / "gdrive_token.pickle"),
    ]
    for doc_id, dest_path in files_to_restore:
        try:
            doc = await db.dbs["tracking"]["bot_files"].find_one({"_id": doc_id})
            if doc and doc.get("data"):
                dest_path.write_bytes(doc["data"])
                LOGGER.info(f"[startup] {doc_id} → {dest_path} geri yüklendi.")
            else:
                LOGGER.info(f"[startup] {doc_id} MongoDB'de bulunamadı, atlandı.")
        except Exception as e:
            LOGGER.warning(f"[startup] {doc_id} geri yükleme hatası: {e}")


async def start_services():
    try:
        LOGGER.info(f"Initializing Telegram-Stremio v-{__version__}")
        await asleep(1.2)

        # SESSION_SECRET_KEY boşsa otomatik üret ve config.env'e yaz
        from Backend.config import Telegram
        import os, secrets, re as _re
        if not Telegram.SESSION_SECRET_KEY:
            _new_key = secrets.token_hex(32)
            _env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.env")
            if os.path.exists(_env_path):
                _env_text = open(_env_path, "r", encoding="utf-8").read()
                if _re.search(r'^SESSION_SECRET_KEY\s*=', _env_text, _re.MULTILINE):
                    # Satır varsa — değerini güncelle
                    _env_text = _re.sub(
                        r'^(SESSION_SECRET_KEY\s*=\s*).*$',
                        rf'\g<1>"{_new_key}"',
                        _env_text, flags=_re.MULTILINE
                    )
                else:
                    # Satır yoksa — sona ekle
                    _env_text = _env_text.rstrip("\n") + f'\nSESSION_SECRET_KEY="{_new_key}"\n'
                open(_env_path, "w", encoding="utf-8").write(_env_text)
            else:
                # config.env hiç yoksa oluştur
                open(_env_path, "w", encoding="utf-8").write(f'SESSION_SECRET_KEY="{_new_key}"\n')
            # Çalışan process'e de hemen uygula
            os.environ["SESSION_SECRET_KEY"] = _new_key
            Telegram.SESSION_SECRET_KEY = _new_key
            LOGGER.info("SESSION_SECRET_KEY otomatik üretildi ve config.env'e kaydedildi.")

        await db.connect()
        await asleep(1.2)

        # Bot başlarken kalıcı dosyaları MongoDB'den geri yükle
        await _restore_persistent_files()
        
        await StreamBot.start()
        StreamBot.username = StreamBot.me.username
        LOGGER.info(f"Bot Client : [@{StreamBot.username}]")
        await asleep(1.2)

        await Helper.start()
        Helper.username = Helper.me.username
        LOGGER.info(f"Helper Bot Client : [@{Helper.username}]")
        await asleep(1.2)

        LOGGER.info("Initializing Multi Clients...")
        await initialize_clients()
        await asleep(2)

        await setup_bot_commands(StreamBot)
        await asleep(2)

        LOGGER.info('Initializing Telegram-Stremio Web Server...')
        await restart_notification()
        loop.create_task(server.serve())
        loop.create_task(ping())
        
        # Start the background Dead Link Checker
        link_checker_task = DeadLinkChecker(db, app, check_interval_hours=24)
        loop.create_task(link_checker_task.start())

        # Başlangıç: yerel dosya yolu olan ama artık mevcut olmayan DB kayıtlarını temizle
        from Backend.pyrofork.plugins.sunucuyayukle import cleanup_local_path_records
        loop.create_task(cleanup_local_path_records())

        # Başlangıç: ekle komutu ile eklenen ama Stremio DB'de artık olmayan kayıtları temizle
        from Backend.pyrofork.plugins.ekle import cleanup_gdrive_orphans
        loop.create_task(cleanup_gdrive_orphans())

        # Start Subscription Background Task
        from Backend.config import Telegram

        # Token yöneticisini başlat (video + indirme, tek TTL)
        from Backend.helper.stream_token import media_token_manager
        media_token_manager.configure(Telegram.YENILEME)
        LOGGER.info("Stream Token Manager başlatıldı.")

        # stream_token periyodik temizlik (10 dakikada bir süresi dolmuş tokenları sil)
        async def _purge_tokens_loop():
            while True:
                await asleep(10 * 60)
                media_token_manager._purge_expired()
                LOGGER.debug("stream_token: süresi dolmuş tokenlar temizlendi.")
        loop.create_task(_purge_tokens_loop())

        if Telegram.SUBSCRIPTION:
            from Backend.helper.subscription_checker import subscription_checker_loop
            loop.create_task(subscription_checker_loop(StreamBot))
            LOGGER.info("Subscription Checker Task Started.")
        
        LOGGER.info("Telegram-Stremio Started Successfully!")
        await idle()
    except Exception:
        LOGGER.error("Error during startup:\n" + format_exc())

async def stop_services():
    try:
        LOGGER.info("Stopping services...")

        # Uvicorn'u önce düzgünce durdur (lifespan CancelledError'ı önler)
        try:
            server.should_exit = True
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # Pyrogram client'larını düzgünce durdur
        try:
            await StreamBot.stop()
        except Exception:
            pass

        try:
            await Helper.stop()
        except Exception:
            pass

        # Kalan task'ları iptal et (pinger, link_checker, vb.)
        pending_tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        await db.disconnect()

        LOGGER.info("Services stopped successfully.")
    except Exception:
        LOGGER.error("Error during shutdown:\n" + format_exc())

if __name__ == '__main__':
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        LOGGER.info('Service Stopping...')
    except Exception:
        LOGGER.error(format_exc())
    finally:
        loop.run_until_complete(stop_services())
        loop.stop()
        logging.shutdown()  
