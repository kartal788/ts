"""
content_announcer.py
=====================
Veritabanına yeni bir film/dizi bölümü eklendiğinde, ayarlardan açılmışsa,
bir Telegram kanalına (veya bir grubun içindeki belirli bir konuya/topic'e)
otomatik olarak Türkçe bir duyuru mesajı gönderir.

Duyuru sıklığı: aynı başlık (tmdb_id + tür) için en fazla 18 saatte bir
duyuru gönderilir. Örneğin bir diziye art arda birkaç bölüm eklenirse tek
bir duyuru yeterli olur; 24 saat dolduktan sonra o başlığa yeniden içerik
eklenirse tekrar duyurulur.

Hedef konu (topic) kapalıysa (TOPIC_CLOSED), bot konuyu otomatik olarak
geçici açar, duyuruyu gönderir ve ardından kullanıcının tercihini bozmamak
için konuyu tekrar kapatır. Bunun için botun grupta "Konuları Yönet"
(Manage Topics) yetkisine sahip bir admin olması gerekir.

Ayarlar sayfası ("Yeni İçerik Duyuruları" bölümü) şu alanları kullanır:
  announce_new_content   → duyuru sistemi açık/kapalı (bool)
  announcement_channel   → hedef; aşağıdaki biçimlerden biri olabilir:

    -1001234567890                       → kanal/grup ID'si
    @kanaladi                            → kullanıcı adı
    https://t.me/c/1234567890/2          → grup içindeki 2 numaralı KONUYA
                                            (forum topic) gönderir
    https://t.me/c/1234567890/2/15       → aynısı (sondaki 15, konudaki bir
                                            mesaj numarasıdır, sadece link
                                            kolay kopyalanabilsin diye kabul
                                            edilir; gönderim yine 2 numaralı
                                            konuya yapılır)
    https://t.me/kanaladi/2              → yukarıdakiyle aynı, ama kullanıcı
                                            adı olan genel bir grup/kanal için

Bot, hedef kanalda/grupta admin olmalıdır; konu (topic) hedefleniyorsa
grubun "Konular" (Forum) özelliği açık olmalı ve bot o gruba dahil olmalıdır.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from pymongo.errors import DuplicateKeyError
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, TopicClosed
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from Backend import db
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.metadata import _translate_with_retry, TRANSLATE_CACHE
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot

#----- Aynı başlık (tmdb_id + tür) için iki duyuru arasındaki minimum süre.
#----- Bu sürenin geçmesinden sonra o başlığa yeni bir video/bölüm eklenirse
#----- tekrar duyurulur.
ANNOUNCE_COOLDOWN_HOURS = 18

#----- TMDB görsel URL'lerindeki boyut segmentini (w92, w154, w185, w300,
#----- w342, w500, w780 vb.) yakalamak için kullanılır.
_TMDB_IMAGE_URL_RE = re.compile(
    r"^(https?://image\.tmdb\.org/t/p/)(w\d+|original)(/.+)$"
)


def _tmdb_original_size(url: str) -> str:
    """
    poster_tr veritabanında küçük boyutlu (w500) olarak saklanır (bkz.
    metadata.py -> get_lang_images). Duyuru gönderilirken bu görsel TMDB'den
    geliyorsa, indirilip Telegram'a yüklenecek dosyanın kalitesi düşük
    kalmasın diye w500 yerine "original" (tam boyut) URL'i kullanılır.
    TMDB'den gelmeyen (örn. imdb/metahub) linkler dokunulmadan döner.
    """
    if not url:
        return url
    match = _TMDB_IMAGE_URL_RE.match(url)
    if not match:
        return url
    prefix, size, suffix = match.groups()
    if size == "original":
        return url
    return f"{prefix}original{suffix}"

#----- Toplu ekleme (ör. art arda 100 film) sırasında duyurular art arda,
#----- kuyruğa alınarak TEK TEK gönderilir; aralarında bu kadar saniye
#----- beklenir. Telegram'ın kanal/grup flood ve spam korumasına takılmamak
#----- için mesajlar hiçbir zaman eşzamanlı (paralel) gönderilmez.
ANNOUNCE_QUEUE_DELAY_SECONDS = 3

#----- Telegram FloodWait döndürürse, belirtilen süre kadar beklenip yeniden
#----- denenir (mesaj asla sessizce terk edilmez). En fazla bu kadar deneme
#----- yapılır; hepsi başarısız olursa hata loglanır ve o duyuru atlanır.
ANNOUNCE_MAX_FLOODWAIT_RETRIES = 5

#----- metadata() içinde description_tr çevirisi başarısız olup (Google hata
#----- sayfası) orijinal (İngilizce) metne düşerse, duyuru bu haliyle
#----- gönderilirdi. Bunu önlemek için, duyuru gönderilmeden HEMEN ÖNCE
#----- description_tr == description (kaynakla birebir aynı) durumu tespit
#----- edilir ve en fazla ANNOUNCE_TRANSLATE_MAX_ATTEMPTS kez, aralarında
#----- ANNOUNCE_TRANSLATE_RETRY_DELAY_SECONDS saniye beklenerek yeniden
#----- çeviri denenir. Tüm denemeler de başarısız olursa duyuru yine de
#----- (orijinal metinle) gönderilir — sonsuza kadar beklemez.
ANNOUNCE_TRANSLATE_RETRY_DELAY_SECONDS = 20
ANNOUNCE_TRANSLATE_MAX_ATTEMPTS = 3

# t.me/c/<internal_id>/<topic_id>[/<mesaj_id>]  (özel kanal/grup)
_TME_C_RE = re.compile(
    r'^(?:https?://)?t\.me/c/(\d+)(?:/(\d+))?(?:/(\d+))?/?$', re.IGNORECASE
)
# t.me/<kullanici_adi>/<topic_id>[/<mesaj_id>]  (genel kanal/grup)
_TME_USER_RE = re.compile(
    r'^(?:https?://)?t\.me/([A-Za-z0-9_]{5,})(?:/(\d+))?(?:/(\d+))?/?$', re.IGNORECASE
)


#----- "announcement_channel" alanını (chat, message_thread_id) olarak çözer
def _parse_target(value: str) -> Tuple[Optional[object], Optional[int]]:
    value = str(value or "").strip()
    if not value:
        return None, None

    m = _TME_C_RE.match(value)
    if m:
        internal_id, topic_id, _msg_id = m.groups()
        chat_id = int(f"-100{internal_id}")
        thread_id = int(topic_id) if topic_id is not None else None
        return chat_id, thread_id

    m = _TME_USER_RE.match(value)
    if m:
        username, topic_id, _msg_id = m.groups()
        thread_id = int(topic_id) if topic_id is not None else None
        return f"@{username}", thread_id

    if value.startswith("@"):
        return value, None

    try:
        return int(value), None
    except ValueError:
        return value, None


#----- Bir başlığın 18 saat içinde en fazla bir kez duyurulmasını sağlar.
#----- Aynı başlığa (tmdb_id + tür) art arda video eklense bile, son
#----- duyurudan bu yana ANNOUNCE_COOLDOWN_HOURS saat geçmediyse duyuru
#----- gönderilmez. Süre dolduktan sonra yeni içerik eklenirse tekrar
#----- duyurulur.
#-----
#----- NOT: _id eşitliğiyle birlikte $or içeren bir upsert kullanmak MongoDB'de
#----- tehlikelidir — filtre eşleşmezse (yani kayıt zaten var ama süresi
#----- dolmamışsa) Mongo yine de _id alanını filtreden alıp INSERT denemesi
#----- yapar ve DuplicateKeyError (E11000) fırlatır. Bunun yerine iki adımlı,
#----- kesin bir yaklaşım kullanılır: önce "insert" denenir (kayıt hiç yoksa
#----- başarılı olur), varsa DuplicateKeyError yakalanır ve ardından süresi
#----- dolmuş mu diye koşullu (upsert'siz) bir update yapılır.
async def _claim(media_type: str, tmdb_id) -> bool:
    if not tmdb_id:
        return False
    key = f"{media_type}:{tmdb_id}"
    coll = db.dbs["tracking"]["announced_content"]
    now = datetime.utcnow()

    #----- 1) Bu başlık daha önce hiç duyurulmadıysa: kayıt oluşturulur.
    try:
        await coll.insert_one({"_id": key, "at": now})
        return True
    except DuplicateKeyError:
        pass

    #----- 2) Kayıt zaten var: son duyurudan bu yana yeterli süre geçtiyse
    #-----    (upsert olmadan) güncelle, geçmediyse dokunma.
    cutoff = now - timedelta(hours=ANNOUNCE_COOLDOWN_HOURS)
    result = await coll.update_one(
        {"_id": key, "at": {"$lte": cutoff}},
        {"$set": {"at": now}},
    )
    return result.modified_count > 0


#----- Dosya adında "camrips, cam, telesync, ts, 1xbet" gibi kaçak sinema
#----- çekimi (cam/telesync) belirten ifadeler geçiyorsa True döner.
#----- Kelime sınırı (\b) kullanılır; büyük/küçük harf ayırt edilmez.
_CAM_KEYWORDS_RE = re.compile(
    r'\b(camrips?|telesync|1xbet|ts|cam)\b', re.IGNORECASE
)

#----- Film adının kendisi "Cam Sehpa", "Cam Perde", "Cam Ustası" gibi
#----- "cam" (=glass) kelimesini içeren gerçek bir film ismiyse, dosya adında
#----- geçen "cam" ifadesi kaçak sinema çekimi anlamına gelmez; bu yüzden
#----- kalite tespiti film ADINA göre bu ifadelerden biriyse devre dışı
#----- bırakılır. Ayraç olarak boşluk, nokta, alt çizgi veya tire kabul edilir.
_CAM_FALSE_POSITIVE_TITLE_RE = re.compile(
    r'\bcam[\s._-]*(sehpa|perde|ustas\w*)\b', re.IGNORECASE
)

#----- "german" ifadesi dosya adında varsa Almanca, "tr" ifadesi varsa
#----- Türkçe ses etiketi döner. Büyük/küçük harf ayırt edilmez.
_GERMAN_AUDIO_RE = re.compile(r'\bgerman\b', re.IGNORECASE)
_TR_AUDIO_RE = re.compile(r'\btr\b', re.IGNORECASE)


#----- Dosya adı ve film adına bakarak (kalite = "Sinema Çekimi mi?", ses
#----- etiketi = "Almanca"/"Türkçe"/None) bilgisini döndürür.
def _detect_cam_quality_and_audio(info: dict) -> Tuple[bool, Optional[str]]:
    filename = str(info.get("source_filename") or info.get("file_name") or "")
    title = str(info.get("title_tr") or info.get("title") or "")

    if _CAM_FALSE_POSITIVE_TITLE_RE.search(title):
        #----- Film adı "cam sehpa/perde/ustası" gibiyse sinema çekimi değildir.
        return False, None

    is_cam = bool(_CAM_KEYWORDS_RE.search(filename))
    if not is_cam:
        return False, None

    if _GERMAN_AUDIO_RE.search(filename):
        audio = "Almanca"
    elif _TR_AUDIO_RE.search(filename):
        audio = "Türkçe"
    else:
        audio = None

    return True, audio


#----- description_tr, kaynak description ile birebir aynıysa (metadata()
#----- sırasında Google Translate'in geçici hata sayfasına takılıp orijinale
#----- düştüğü anlamına gelir) duyuru gönderilmeden önce bunu en fazla
#----- ANNOUNCE_TRANSLATE_MAX_ATTEMPTS kez daha, aralarında
#----- ANNOUNCE_TRANSLATE_RETRY_DELAY_SECONDS saniye bekleyerek yeniden
#----- dener.
#-----
#----- ÖNEMLİ: translate_text_safe() DEĞİL, doğrudan _translate_with_retry()
#----- çağrılır. Çünkü translate_text_safe, bu metnin az önce (metadata()
#----- sırasında) başarısız olduğunu TRANSLATE_RECENT_FAIL'e 120 saniyeliğine
#----- kaydetmiştir; bu pencere içinde tekrar çağrılırsa Google'a hiç
#----- gitmeden doğrudan orijinal metni geri döner — yani "yeniden deneme"
#----- aslında hiç denemeden başarısız sayılırdı. _translate_with_retry bu
#----- kısayolu atlayıp gerçekten Google'a sorar.
#-----
#----- Başarılı olursa hem info["description_tr"] güncellenir hem de sonuç
#----- TRANSLATE_CACHE'e yazılır (_translate_with_retry içinde), böylece bu
#----- metin için ileride translate_text_safe çağrılırsa da cache'ten doğru
#----- sonuç döner. Tüm denemeler başarısız olursa orijinal metinle duyuru
#----- yine de gönderilir.
async def _ensure_description_translated(info: dict) -> None:
    source = (info.get("description") or "").strip()
    current = (info.get("description_tr") or "").strip()
    if len(source) < 3 or not current or current != source:
        return  # boş / çok kısa / zaten (kaynaktan farklı) bir çeviri var

    for attempt in range(1, ANNOUNCE_TRANSLATE_MAX_ATTEMPTS + 1):
        await asyncio.sleep(ANNOUNCE_TRANSLATE_RETRY_DELAY_SECONDS)
        result = await asyncio.to_thread(
            _translate_with_retry, source, "tr", TRANSLATE_CACHE, "tr",
        )
        if result and result.strip() != source:
            info["description_tr"] = result
            LOGGER.info(
                f"Duyuru öncesi description_tr çevirisi düzeltildi (deneme {attempt}/"
                f"{ANNOUNCE_TRANSLATE_MAX_ATTEMPTS})."
            )
            return

    LOGGER.warning(
        f"Duyuru öncesi description_tr çevirisi {ANNOUNCE_TRANSLATE_MAX_ATTEMPTS} "
        f"denemede de başarısız oldu (Google hata sayfası/erişim sorunu), "
        f"orijinal metinle duyurulacak."
    )


#----- Duyuru metnini Türkçe olarak oluşturur
def _build_caption(info: dict) -> str:
    is_tv = info.get("media_type") == "tv"
    title = info.get("title_tr") or info.get("title") or "Bilinmiyor"
    header = f"{'📺' if is_tv else '🎬'} <b>{title}</b>"
    if info.get("year"):
        header += f" ({info['year']})"

    lines = [header, "", f"🗂 <b>Tür:</b> {'Dizi' if is_tv else 'Film'}"]

    if info.get("rate"):
        try:
            lines.append(f"⭐ <b>Puan:</b> {round(float(info['rate']), 1)}")
        except (TypeError, ValueError):
            pass

    #----- Kategori: veritabanındaki Türkçe tür listesi (genres_tr) kullanılır
    genres_tr = info.get("genres_tr") or info.get("genres") or []
    if genres_tr:
        lines.append(f"🎭 <b>Kategori:</b> {', '.join(genres_tr[:4])}")

    #----- Ses ve Kalite bilgileri normalde duyuru metnine eklenmez; tek
    #----- istisna, dosya adından tespit edilen "Sinema Çekimi" (cam/telesync)
    #----- kalitesidir. Bu durumda kalite ve (varsa) ses etiketi eklenir.
    is_cam, cam_audio = _detect_cam_quality_and_audio(info)
    if is_cam:
        lines.append("🎥 <b>Kalite:</b> Sinema Çekimi")
        if cam_audio:
            lines.append(f"🔊 <b>Ses:</b> {cam_audio}")

    desc = (info.get("description_tr") or info.get("description") or "").strip()
    if desc:
        if len(desc) > 320:
            desc = desc[:317].rstrip() + "..."
        lines += ["", f"<i>{desc}</i>"]

    return "\n".join(lines)


#----- Fotoğraflı (varsa) veya düz metinli duyuruyu tek seferlik gönderir.
#----- posters: öncelik sırasına göre aday görsel listesi (backdrop_tr,
#----- backdrop, backdrop_de, poster_tr, poster, poster_de). Bir alanın dolu
#----- olması, o linkin gerçekten çalıştığı anlamına gelmez (ör. veritabanında
#----- kayıtlı ama artık "Missing image" veren bozuk bir link olabilir); bu
#----- yüzden ilk aday başarısız olursa sıradaki denenir, hiçbiri çalışmazsa
#----- düz metin mesajına düşülür.
async def _send_once(chat, thread_id, posters, caption, markup, display_title="") -> None:
    send_kwargs = {}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id

    for poster in posters:
        if not poster:
            continue
        try:
            await StreamBot.send_photo(
                chat, poster, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=markup,
                **send_kwargs,
            )
            return
        except (FloodWait, TopicClosed):
            raise
        except Exception as e:
            #----- Bu aday gönderilemedi (bozuk/kayıp link vb.) → sıradaki
            #----- adaya geç. Veritabanındaki bozuk linki tespit edebilmek
            #----- için loglanır.
            LOGGER.warning(
                f"Duyuru görseli gönderilemedi '{display_title}' ({poster}): {e}"
            )
            continue

    #----- Hiçbir aday görsel gönderilemedi (veya hiç aday yoktu) → düz metin
    await StreamBot.send_message(
        chat, caption, parse_mode=ParseMode.HTML,
        reply_markup=markup, disable_web_page_preview=True,
        **send_kwargs,
    )


#----- _send_once'ı çağırır; Telegram FloodWait döndürürse mesajı sessizce
#----- terk ETMEZ, istenen süre kadar bekleyip yeniden dener. TopicClosed
#----- olduğu gibi çağırana (üst seviyedeki konu-açma mantığına) bırakılır.
async def _send_with_flood_retry(chat, thread_id, posters, caption, markup, display_title="") -> None:
    attempt = 0
    while True:
        try:
            await _send_once(chat, thread_id, posters, caption, markup, display_title)
            return
        except FloodWait as e:
            attempt += 1
            wait_seconds = int(getattr(e, "value", 0) or 0) + 1
            if attempt >= ANNOUNCE_MAX_FLOODWAIT_RETRIES:
                LOGGER.error(
                    f"Duyuru gönderilemedi '{display_title}': FloodWait limiti aşıldı "
                    f"({attempt} deneme, son bekleme {wait_seconds}sn)."
                )
                raise
            LOGGER.warning(
                f"Duyuru FloodWait ('{display_title}'): {wait_seconds}sn bekleniyor "
                f"(deneme {attempt}/{ANNOUNCE_MAX_FLOODWAIT_RETRIES})."
            )
            await asyncio.sleep(wait_seconds)


#----- Duyuru mesajındaki "Stremio'da Aç" / "Nuvio'da Aç" butonlarını oluşturur.
#----- Buradaki alan adı, projenin gerçek BASE_URL'inden BAĞIMSIZ olarak
#----- ayarlar sayfasındaki "Yönlendirme Alan Adı" (redirect_base_url) alanından
#----- okunur (ör. bir Cloudflare Workers alt alan adı: https://a.arsiv.workers.dev).
#----- Böylece duyuru butonlarında projenin gerçek sunucu adresi ifşa olmaz.
#----- imdb_id yoksa veya alan adı ayarlanmamışsa buton eklenmez.
def _build_open_buttons(info: dict, settings) -> list:
    redirect_base = str(getattr(settings, "redirect_base_url", "") or "").strip().rstrip("/")
    imdb_id = str(info.get("imdb_id") or "").strip()
    if not redirect_base or not imdb_id:
        return []
    stremio_type = "series" if info.get("media_type") == "tv" else "movie"
    return [
        InlineKeyboardButton("▶️ Stremio'da Aç", url=f"{redirect_base}/open/stremio/{stremio_type}/{imdb_id}"),
        InlineKeyboardButton("📱 Nuvio'da Aç", url=f"{redirect_base}/open/nuvio/{stremio_type}/{imdb_id}"),
    ]


async def _announce(info: dict) -> None:
    settings = SettingsManager.current()
    if not settings.announce_new_content:
        return

    #----- Görünürlük "Sadece seçtiğim üye(ler)" (visibility.mode == "selected")
    #----- olarak ayarlanmış içerikler herkese açık değildir; bu yüzden genel
    #----- Telegram kanalına/konusuna duyuru gönderilmez. Sadece "subscribers"
    #----- (tüm aktif aboneler) veya görünürlük belirtilmemiş içerikler duyurulur.
    visibility = info.get("visibility") or {}
    if isinstance(visibility, dict) and visibility.get("mode") == "selected":
        return

    chat, thread_id = _parse_target(getattr(settings, "announcement_channel", ""))
    if chat is None:
        return

    if not await _claim(info.get("media_type"), info.get("tmdb_id")):
        return

    #----- Gönderimden hemen önce: description_tr hâlâ orijinal (çevrilmemiş)
    #----- ise en fazla ANNOUNCE_TRANSLATE_MAX_ATTEMPTS kez daha dener
    #----- (aralarında ANNOUNCE_TRANSLATE_RETRY_DELAY_SECONDS bekleyerek).
    #----- Bu, kuyruktaki tek işçiyi (worker) bu süre boyunca meşgul eder;
    #----- yani bu içerikten sonraki duyurular da bu kadar gecikir — bu,
    #----- Telegram'a yarım çevrilmiş bir duyuru göndermemek için bilinçli
    #----- bir tercihtir.
    await _ensure_description_translated(info)

    caption = _build_caption(info)
    is_cam, _cam_audio = _detect_cam_quality_and_audio(info)
    if is_cam:
        #----- Sinema çekimi (cam/telesync) içerikte resim önceliği farklıdır:
        #----- önce poster_tr, yoksa poster, o da yoksa backdrop_tr, o da
        #----- yoksa backdrop denenir. İlk dolu alan değil, ilk GERÇEKTEN
        #----- GÖNDERİLEBİLEN görsel kullanılır (dolu ama bozuk/kayıp link olabilir).
        posters = [
            _tmdb_original_size(info.get("poster_tr")),
            info.get("poster"),
            info.get("backdrop_tr"),
            info.get("backdrop"),
            info.get("backdrop_de"),
            info.get("poster_de"),
        ]
    else:
        #----- Duyuru resmi adayları: backdrop_tr → backdrop → backdrop_de →
        #----- poster_tr → poster → poster_de sırasıyla denenir. İlk dolu alan
        #----- değil, ilk GERÇEKTEN GÖNDERİLEBİLEN görsel kullanılır (dolu ama
        #----- bozuk/kayıp bir link olabilir).
        posters = [
            info.get("backdrop_tr"),
            info.get("backdrop"),
            info.get("backdrop_de"),
            _tmdb_original_size(info.get("poster_tr")),
            info.get("poster"),
            info.get("poster_de"),
        ]
    display_title = info.get("title_tr") or info.get("title")

    markup = None
    rows = []

    open_buttons = _build_open_buttons(info, settings)
    if open_buttons:
        rows.append(open_buttons)

    bot_username = getattr(StreamBot, "username", None)
    if bot_username:
        app_name = (getattr(settings, "isim", "") or "").strip() or "Bot"
        rows.append([InlineKeyboardButton(f"🤖 {app_name}'e üye ol", url=f"https://t.me/{bot_username}")])

    if rows:
        markup = InlineKeyboardMarkup(rows)

    try:
        await _send_with_flood_retry(chat, thread_id, posters, caption, markup, display_title)
    except TopicClosed:
        # Hedef konu kapalı. Konuyu geçici olarak aç, duyuruyu gönder,
        # ardından kullanıcının tercihini bozmamak için tekrar kapat.
        if thread_id is None:
            LOGGER.error(f"Duyuru gönderilemedi '{display_title}': hedef konu kapalı (TOPIC_CLOSED).")
            return
        try:
            await StreamBot.reopen_forum_topic(chat, thread_id)
        except Exception as e:
            LOGGER.error(
                f"Duyuru gönderilemedi '{display_title}': konu kapalı ve otomatik açılamadı "
                f"(chat={chat}, konu={thread_id}): {e}"
            )
            return
        try:
            await _send_with_flood_retry(chat, thread_id, posters, caption, markup, display_title)
        except FloodWait:
            pass  # _send_with_flood_retry zaten bekleyip yeniden denedi ve hatayı logladı
        except Exception as e:
            LOGGER.error(f"Konu geçici olarak açıldı ama duyuru yine gönderilemedi '{display_title}': {e}")
        finally:
            try:
                await StreamBot.close_forum_topic(chat, thread_id)
            except Exception as e:
                LOGGER.warning(f"Duyuru sonrası konu tekrar kapatılamadı (chat={chat}, konu={thread_id}): {e}")
    except FloodWait:
        pass  # _send_with_flood_retry zaten bekleyip yeniden denedi ve hatayı logladı
    except Exception as e:
        LOGGER.error(f"Duyuru gönderilemedi '{display_title}': {e}")


#----- Duyurular tek bir kuyruktan, TEK BİR arka plan işçisi (worker) tarafından
#----- sırayla işlenir. Bu sayede toplu içerik eklemede (ör. art arda 100 film)
#----- Telegram'a onlarca mesaj eşzamanlı gönderilmez; her duyurudan sonra
#----- ANNOUNCE_QUEUE_DELAY_SECONDS kadar beklenir, bu da flood/spam
#----- korumasına takılma riskini ortadan kaldırır.
_announce_queue: Optional["asyncio.Queue[dict]"] = None
_announce_worker_task = None


#----- Duyuru kuyruğu tek işçili (worker) çalışır; bu değişkenler dışarıya
#----- "şu an bekleyen veya işlenmekte olan bir duyuru var mı?" bilgisini
#----- vermek için kullanılır. Amaç: canlı duyuru gönderimi ile arka planda
#----- çalışabilecek başka Google Translate tüketicilerinin aynı anda istek
#----- göndermesini önlemek — aynı paylaşımlı throttle/rate-limit'e tabi
#----- olduklarından, aynı anda yüklenirlerse birbirlerini (ve normal içerik
#----- ekleme çevirilerini) yavaşlatabilirler.
_announce_busy_count = 0


def has_pending_announcements() -> bool:
    """Kuyrukta bekleyen VEYA şu an gönderilmekte/çevrilmekte olan bir duyuru
    varsa True döner."""
    if _announce_queue is not None and not _announce_queue.empty():
        return True
    return _announce_busy_count > 0


async def _announce_worker() -> None:
    global _announce_busy_count
    while True:
        info = await _announce_queue.get()
        _announce_busy_count += 1
        try:
            await _announce(info)
        except Exception as e:
            LOGGER.error(f"Duyuru kuyruğu işlenirken beklenmeyen hata: {e}")
        finally:
            _announce_busy_count -= 1
            _announce_queue.task_done()
        #----- Bir sonraki duyuruya geçmeden önce sabit bir bekleme uygulanır.
        await asyncio.sleep(ANNOUNCE_QUEUE_DELAY_SECONDS)


def _ensure_announce_worker() -> None:
    global _announce_queue, _announce_worker_task
    if _announce_queue is None:
        _announce_queue = asyncio.Queue()
    if _announce_worker_task is None or _announce_worker_task.done():
        _announce_worker_task = asyncio.create_task(_announce_worker())


#----- Yeni eklenen bir içerik için duyuruyu kuyruğa ekler (fire-and-forget).
#----- Doğrudan görev (task) başlatmaz; tüm duyurular tek işçi tarafından
#----- sırayla ve aralarında bekleme ile gönderilir.
def announce_new_content(info: dict) -> None:
    try:
        _ensure_announce_worker()
        _announce_queue.put_nowait(dict(info))
    except RuntimeError:
        LOGGER.warning("Duyuru atlandı: çalışan bir event loop bulunamadı.")
