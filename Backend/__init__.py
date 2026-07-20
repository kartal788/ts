from Backend.helper.database import Database
from time import time
from datetime import datetime
import asyncio
import pytz

timezone = pytz.timezone("Europe/Istanbul")
now = datetime.now(timezone)
StartTime = time()


USE_DEFAULT_ID: str = None

# /media/manage sayfasındaki "İçerik Ekle" paneli ile açılan
# "manuel içerik ekleme" modu.
# None ise kapalı; dict ise:
#   {
#       "title": str,
#       "poster": str|None,
#       "description": str|None,
#       "media_type": "movie" | "tv",
#       "year": int|None,            # opsiyonel çıkış yılı
#       "season": int|None,          # yalnızca media_type == "tv" iken kullanılır
#       "next_episode": int|None,    # bir sonraki dosyaya otomatik verilecek bölüm no
#   }
MANUAL_MODE: dict = None

# MANUAL_MODE["next_episode"] sayacını, aynı anda gelen birden çok dosya
# (paralel task'lar) arasında yarış durumu (race condition) olmadan güvenle
# artırmak için kullanılan kilit.
MANUAL_MODE_LOCK = asyncio.Lock()

# /media/edit sayfasındaki "İçerik Ekle" butonu ile açılan "var olan içeriğe
# ekleme" modu. MANUAL_MODE'dan farkı: yeni bir kart oluşturmaz, kanala
# iletilen dosyaları doğrudan hedeflenen (tmdb_id/imdb_id'si belli) film ya
# da dizinin altına (film ise yeni kalite, dizi ise yeni bölüm olarak) ekler.
# None ise kapalı; dict ise:
#   {
#       "tmdb_id": int,
#       "imdb_id": str|None,
#       "title": str,
#       "poster": str|None,
#       "media_type": "movie" | "tv",
#       "season": int|None,          # yalnızca media_type == "tv" iken kullanılır
#       "next_episode": int|None,    # bir sonraki dosyaya otomatik verilecek bölüm no
#   }
ATTACH_MODE: dict = None

# ATTACH_MODE["next_episode"] sayacı için MANUAL_MODE_LOCK ile aynı amaçla
# kullanılan ayrı bir kilit.
ATTACH_MODE_LOCK = asyncio.Lock()

db = Database()  

__version__ = "4.4.8"
