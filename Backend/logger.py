import pytz
from logging import getLogger, StreamHandler, INFO, WARNING, ERROR, Formatter, basicConfig
from logging.handlers import RotatingFileHandler
from datetime import datetime

IST = pytz.timezone("Europe/Istanbul")

class ISTFormatter(Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        return dt.strftime(datefmt or "%d-%b-%y %I:%M:%S %p")

# mode="w" → bot her yeniden başladığında log.txt sıfırlanır
# maxBytes=15MB, backupCount=0 → tek dosya, 15MB dolunca en eski loglar silinir
file_handler = RotatingFileHandler("log.txt", mode="w", maxBytes=15 * 1024 * 1024, backupCount=0)
stream_handler = StreamHandler()
formatter = ISTFormatter("[%(asctime)s] [%(levelname)s] - %(message)s", "%d-%b-%y %I:%M:%S %p")
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

basicConfig(
    handlers=[file_handler, stream_handler],
    level=INFO
)

getLogger("httpx").setLevel(ERROR)
#----- pyrogram'ın kendi bağlantı/oturum logları önceden tamamen susturuluyordu
#----- (ERROR). Bu, gerçek "bağlantı koptu / yeniden bağlanılıyor" gibi TG
#----- sorunlarının log.txt'e hiç düşmemesine sebep oluyordu. WARNING'e
#----- çekilerek bu tip olaylar görünür kalırken, kütüphanenin gürültülü
#----- INFO/DEBUG mesajları hâlâ bastırılıyor.
getLogger("pyrogram").setLevel(WARNING)
getLogger("pyrogram.session").setLevel(WARNING)
getLogger("pyrogram.connection").setLevel(WARNING)
getLogger("pyrogram.connection.transport").setLevel(WARNING)
getLogger("pyrogram.session.session").setLevel(WARNING)
getLogger("fastapi").setLevel(ERROR)


LOGGER = getLogger(__name__)
LOGGER.setLevel(INFO)

LOGGER.info("Logger initialized with IST timezone.")
