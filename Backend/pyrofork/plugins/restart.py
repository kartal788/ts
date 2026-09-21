from pyrogram import filters, Client, enums
from pyrogram.types import Message
from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER
from asyncio import create_subprocess_exec, gather
from aiofiles import open as aiopen
from os import execv as osexecv
import sys

@Client.on_message(filters.command('restart') & filters.private & CustomFilters.owner, group=10)
async def restart(client: Client, message: Message):
    try:
        restart_message = await message.reply_text(
            '<blockquote>⚙️ Bot başlatılıyor. \n\n✨ Lütfen bekleyiniz. </blockquote>',
            quote=True,
            parse_mode=enums.ParseMode.HTML
        )

        proc1 = await create_subprocess_exec('uv', 'run', 'update.py')
        await gather(proc1.wait())

        async with aiopen(".restartmsg", "w") as f:
            await f.write(f"{restart_message.chat.id}\n{restart_message.id}\n")

        LOGGER.info("Restarting the bot (in-place execv, no uv wrapper)...")

        # NOT: 'uv run -m Backend' ile execl YAPMIYORUZ. Bu process zaten
        # uv tarafından fork edilmiş bir çocuk process; onu tekrar 'uv run'
        # ile değiştirmek her restart'ta bir supervisor katmanı daha
        # ekleyip eskisini asla öldürmüyor (leak). Bunun yerine doğrudan
        # aktif venv Python'ını (sys.executable) execv ile üstüne yazıyoruz
        # — aynı PID, ekstra process yok.
        osexecv(sys.executable, [sys.executable, "-m", "Backend"])

    except Exception as e:
        LOGGER.error(f"Error during restart: {e}")
        await message.reply_text("**❌ Failed to restart. Check logs for details.**")
        
