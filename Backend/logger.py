import pytz
from logging import getLogger, StreamHandler, INFO, ERROR, Formatter, basicConfig
from logging.handlers import RotatingFileHandler
from datetime import datetime

IST = pytz.timezone("Europe/Istanbul")

class ISTFormatter(Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        return dt.strftime(datefmt or "%d-%b-%y %I:%M:%S %p")

# mode="w" → bot her yeniden başladığında log.txt sıfırlanır
# maxBytes=3MB, backupCount=0 → tek dosya, 3MB dolunca en eski loglar silinir
file_handler = RotatingFileHandler("log.txt", mode="w", maxBytes=3 * 1024 * 1024, backupCount=0)
stream_handler = StreamHandler()
formatter = ISTFormatter("[%(asctime)s] [%(levelname)s] - %(message)s", "%d-%b-%y %I:%M:%S %p")
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

basicConfig(
    handlers=[file_handler, stream_handler],
    level=INFO
)

getLogger("httpx").setLevel(ERROR)
getLogger("pyrogram").setLevel(ERROR)
getLogger("fastapi").setLevel(ERROR)


LOGGER = getLogger(__name__)
LOGGER.setLevel(INFO)

LOGGER.info("Logger initialized with IST timezone.")
