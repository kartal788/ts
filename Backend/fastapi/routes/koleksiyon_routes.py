"""
koleksiyon_routes.py
====================
Nuvio tarzı "Koleksiyonlar" (bir koleksiyon = ana sayfada bir satır,
satırdaki her klasör = GIF/kapak görselli bir kutucuk, kutucuğa basınca
içerik listesi açılır).

Bu modül şunları sağlar:

  • Admin API  (/api/admin/koleksiyonlar/...)  → oluştur / sil / klasör & içerik yönetimi,
                                                  hazır koleksiyon üreticileri, Nuvio JSON dışa aktarma
  • Public     (/stremio/{token}/{lang}/koleksiyonlar.json) → Nuvio'ya içe aktarılabilir JSON
  • Public     (/stremio/koleksiyon/gif/{ad}.gif)            → hazır koleksiyonlar için üretilen animasyonlu GIF
  • Katalog servisi: stremio_routes.py içindeki manifest ve get_catalog
    fonksiyonları, bu modüldeki manifest_catalogs() ve serve_catalog_page()
    fonksiyonlarını çağırır (klasör id'si:  kol_<klasör_id>-<movie|series>_<lang>).

Veri modeli  (tracking.koleksiyonlar):
  {
    _id, name, description, icon, preset (hazır koleksiyon anahtarı | None),
    active, order, in_manifest, hide_from_home, pin_to_top, backdrop_url,
    folders: [{
        id, title, emoji, cover_url, focus_gif_url, gif_key, tile_shape,
        media: "all" | "movie" | "tv",
        rule:  {type: "platform", platform, sort: "new"|"popular"}
             | {type: "actor",    name, sort}
             | {type: "collection", collection_id}          (TMDB film serisi)
             | {type: "company",  company_key, company_ids}  (TMDB yapım şirketi)
             | {type: "country",  country_key, langs, origin} (orijinal dil / TMDB menşe ülke)
             | {type: "genre",    genre, sort}               (kütüphane türü, örn. "Aile")
             | {type: "director", person_id, name}           (TMDB yönetmenlik kredileri)
             | {type: "tmdb_list", list}                     (trend_day|trend_week|trend_month|airing|now_playing)
             | {type: "manual",   items: [imdb_id, ...]}
             | {type: "titles",   titles: [[ad, yıl], ...], items: [imdb_id, ...]},
        # İsteğe bağlı: klasörün içindeki alt bölümler (Nuvio'da sekme olarak görünür).
        # sections varsa klasörün kendi rule/media alanı kullanılmaz.
        sections: [{id, title, media: "movie" | "tv", rule: {...}}]
    }]
  }

Hazır koleksiyonlar (en üstte sabitlenir):
  • "Dijital Platformlar" → Netflix, Disney+ ... klasörleri (GIF + TMDB logosu);
        her klasörde: Yeni Eklenen <P> Dizileri / Yeni Eklenen <P> Filmleri /
                      Popüler <P> Dizileri / Popüler <P> Filmleri
  • "Yapım Şirketleri"   → Marvel, DC, Paramount ... klasörleri (GIF + TMDB şirket logosu);
        her klasörde: <Şirket> Filmleri / <Şirket> Dizileri (içerik TMDB'den, kütüphaneyle eşleştirilir)
  • "Ülkeler"            → Türkiye (Yerli Filmler/Diziler), Amerika (Amerikan Filmleri/Dizileri), Fransa ... klasörleri
        (dalgalanan bayrak GIF'i; içerik orijinal dile veya TMDB menşe ülkesine göre)
  • "Türler"             → Aile, Aksiyon, Komedi ... klasörleri; her birinde Yeni Eklenen <Tür> Dizileri / Filmleri
                            ve Popüler <Tür> Dizileri / Filmleri (kütüphanedeki genres_tr alanına göre)
  • "Temalar"            → Uzay, Kovboy, Paralel Evren, Uzaylılar, Zaman Yolculuğu, Vampirler ... klasörleri (hareketli GIF);
        her klasörde: <Tema> Filmleri / <Tema> Dizileri (TMDB anahtar kelimeleri + kütüphane türü ile eşleştirilir)
  • "Yıllar"             → 2020'ler, 2010'lar, 2000'ler, 90'lar, 80'ler ... klasörleri;
        her klasörde: Yeni Eklenen <Dönem> Filmleri / Yeni Eklenen <Dönem> Dizileri /
                      Popüler <Dönem> Dizileri / Popüler <Dönem> Filmleri (yapım yılı o dönemde olanlar)
  • "TMDB Trend Listesi" → Günlük / Haftalık / Aylık Trend (Diziler + Filmler), Devam Eden Diziler, Vizyondakiler
        (TMDB'nin güncel listeleri; yalnızca kütüphanenizde bulunanlar, TMDB sırasıyla)
  • "Oyuncular"          → Tom Cruise, Cem Yılmaz ... klasörleri (TMDB fotoğrafı);
        her klasörde: <Oyuncu> Filmleri / <Oyuncu> Dizileri
  • "Yönetmenler"        → Christopher Nolan ... klasörleri (TMDB fotoğrafı); her birinde <Soyad> Filmleri / Dizileri
                            (TMDB yönetmenlik kredileri kütüphaneyle tmdb_id üzerinden eşleştirilir)
  • "Seri Filmler"       → Harry Potter, Hızlı ve Öfkeli, Yüzüklerin Efendisi ... klasörleri
        (TMDB koleksiyonu; ad ve kapak görseli TMDB'den, filmler yıl sırasıyla)
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import time
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, Response
import httpx

from Backend import db
from Backend.config import Telegram
from Backend.fastapi.security.credentials import require_auth
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.platform_catalog import platform_catalog, PLATFORM_LABELS

_logger = logging.getLogger("koleksiyonlar")

admin_router = APIRouter(prefix="/api/admin/koleksiyonlar", tags=["Admin - Koleksiyonlar"])
public_router = APIRouter(prefix="/stremio", tags=["Koleksiyonlar (public)"])

_COLL = "koleksiyonlar"
_SUPPORTED_LANGS = ("tr", "de", "en", "original")

# ════════════════════════════════════════════════════════════════════════════
#  Hazır koleksiyon tanımları
# ════════════════════════════════════════════════════════════════════════════

# Platform renkleri (GIF üretimi için): anahtar -> (renk1, renk2)
_PLATFORM_COLORS: Dict[str, Tuple[str, str]] = {
    "trend-day":      ("#ff5a1f", "#3b0d00"),
    "trend-week":     ("#e11d74", "#350018"),
    "trend-month":    ("#8b3dff", "#1a0742"),
    "trend-airing":   ("#00b894", "#00261d"),
    "trend-playing":  ("#0ea5e9", "#021b2b"),
    "company-marvel":    ("#ec1d24", "#3a0507"),
    "company-dc":        ("#0476f2", "#021733"),
    "company-paramount": ("#1a6bff", "#020f2e"),
    "company-warner":    ("#1b4fd8", "#050c2b"),
    "company-universal": ("#2b5fb4", "#070f24"),
    "company-disney":    ("#1a3fd1", "#060c33"),
    "company-pixar":     ("#2d8ae0", "#0a2140"),
    "company-lucasfilm": ("#1f2937", "#030712"),
    "netflix": ("#E50914", "#1A0003"),
    "disney":  ("#1E4FE0", "#0A1A5C"),
    "amazon":  ("#00A8E1", "#14202B"),
    "hbo":     ("#7A32D6", "#1A0A38"),
    "bein":    ("#7B2CBF", "#240A3A"),
    "exxen":   ("#F5B800", "#3A2A00"),
    "gain":    ("#FF3D6E", "#3A0A1A"),
    "apple":   ("#4A4A52", "#0E0E10"),
    "tabii":   ("#00B894", "#00342A"),
    "tvplus":  ("#0A84D6", "#06203A"),
    "marvel":  ("#ED1D24", "#2B0003"),
    "theme-uzay":            ("#5b3df5", "#05021f"),
    "theme-kovboy":          ("#d97706", "#2b1100"),
    "theme-paralel-evren":   ("#d946ef", "#1a0526"),
    "theme-uzaylilar":       ("#22c55e", "#021a0a"),
    "theme-zaman-yolculugu": ("#06b6d4", "#021a22"),
    "theme-vampirler":       ("#b91c1c", "#1a0000"),
    "theme-zombiler":        ("#65a30d", "#101a02"),
    "theme-robotlar":        ("#64748b", "#0b1220"),
    "theme-super-kahramanlar": ("#ef4444", "#10163a"),
    "theme-hayaletler":      ("#94a3b8", "#0f172a"),
    "theme-distopya":        ("#78716c", "#1c1917"),
    "theme-casusluk":        ("#334155", "#020617"),
    "theme-soygun":          ("#ca8a04", "#1c1400"),
    "theme-mafya":           ("#7f1d1d", "#0c0a09"),
    "theme-korsanlar":       ("#0369a1", "#04121f"),
    "theme-samuray":         ("#dc2626", "#1a0505"),
    "theme-buyuculuk":       ("#7c3aed", "#14072b"),
    "theme-ejderhalar":      ("#ea580c", "#2a0e00"),
    "theme-kurt-adamlar":    ("#57534e", "#0c0a09"),
    "theme-canavarlar":      ("#0f766e", "#01201d"),
    "theme-dedektif":        ("#a16207", "#1a1200"),
    "theme-hapishane":       ("#52525b", "#09090b"),
}

# Marvel Sinematik Evreni — (başlık, yıl). Eşleşme veritabanındaki title / title_tr / title_de
# alanlarına (büyük-küçük harf ve aksan duyarsız, ±1 yıl toleransla) yapılır.
_MCU_MOVIES: List[Tuple[str, int]] = [
    ("Iron Man", 2008), ("The Incredible Hulk", 2008), ("Iron Man 2", 2010),
    ("Thor", 2011), ("Captain America: The First Avenger", 2011), ("The Avengers", 2012),
    ("Iron Man 3", 2013), ("Thor: The Dark World", 2013),
    ("Captain America: The Winter Soldier", 2014), ("Guardians of the Galaxy", 2014),
    ("Avengers: Age of Ultron", 2015), ("Ant-Man", 2015), ("Captain America: Civil War", 2016),
    ("Doctor Strange", 2016), ("Guardians of the Galaxy Vol. 2", 2017),
    ("Spider-Man: Homecoming", 2017), ("Thor: Ragnarok", 2017), ("Black Panther", 2018),
    ("Avengers: Infinity War", 2018), ("Ant-Man and the Wasp", 2018), ("Captain Marvel", 2019),
    ("Avengers: Endgame", 2019), ("Spider-Man: Far From Home", 2019), ("Black Widow", 2021),
    ("Shang-Chi and the Legend of the Ten Rings", 2021), ("Eternals", 2021),
    ("Spider-Man: No Way Home", 2021), ("Doctor Strange in the Multiverse of Madness", 2022),
    ("Thor: Love and Thunder", 2022), ("Black Panther: Wakanda Forever", 2022),
    ("Ant-Man and the Wasp: Quantumania", 2023), ("Guardians of the Galaxy Vol. 3", 2023),
    ("The Marvels", 2023), ("Deadpool & Wolverine", 2024),
    ("Captain America: Brave New World", 2025), ("Thunderbolts*", 2025),
    ("The Fantastic Four: First Steps", 2025),
]
_MCU_SERIES: List[Tuple[str, int]] = [
    ("Agents of S.H.I.E.L.D.", 2013), ("Daredevil", 2015), ("Jessica Jones", 2015),
    ("Luke Cage", 2016), ("Iron Fist", 2017), ("The Punisher", 2017),
    ("WandaVision", 2021), ("The Falcon and the Winter Soldier", 2021), ("Loki", 2021),
    ("What If...?", 2021), ("Hawkeye", 2021), ("Moon Knight", 2022), ("Ms. Marvel", 2022),
    ("She-Hulk: Attorney at Law", 2022), ("Secret Invasion", 2023), ("Echo", 2024),
    ("Agatha All Along", 2024), ("Daredevil: Born Again", 2025), ("Ironheart", 2025),
]

TILE_SHAPES = ("LANDSCAPE", "SQUARE", "POSTER")

# Varsayılan (hazır) klasör görselleri: gif_key -> hareketli GIF adresi.
# Klasöre kendi kapak/GIF adresi girilmemişse bunlar kullanılır; listede olmayan anahtarlar için
# otomatik üretilen GIF devam eder. Platformlar: düz anahtar, şirketler: "company-<anahtar>".
DEFAULT_GIFS: Dict[str, str] = {
    "netflix": "https://64.media.tumblr.com/9f93a9fc2e02fb466eb02a7d2247cb6e/a5b604d3737fc559-49/s500x750/403804744183922f6091d48139021fc3da22f786.gifv",
    "disney": "https://64.media.tumblr.com/ca6dc6d4e8a260c0c5f40c47c7334e57/eb71329b4dc482d5-50/s500x750/e9e82c5bca839be289f3d87863141ebf6d7fca28.gifv",
    "amazon": "https://64.media.tumblr.com/5c5ed8bf948c5b3ca63a11544b94c720/55dbe5d6db4b66a5-bf/s500x750/d6686beaab57aa152f1b92a4562f757a1c522a2f.gifv",
    "hbo": "https://64.media.tumblr.com/cca7a86d443a0bc88536a2ad6ce72aec/b495f88d5c1df470-a2/s640x960/4cb9b614191d17d02c946b4ca59548cd333c06fd.gifv",
    "apple": "https://64.media.tumblr.com/d717319220a7d26bdaa88e72f6f76889/d9a7a808f588d8f4-63/s500x750/959b0ca57f53153b2ca9adaf414859e45e3734e6.gifv",
    "company-mgm": "https://64.media.tumblr.com/9f43b185411e18d71444c9a5a5a79632/5d68ae94e9917470-70/s500x750/0871e87c17045759735ef64c26703c71b585bfb0.gifv",
    "company-marvel": "https://c.tenor.com/8ctGtM0JDmUAAAAC/tenor.gif",
    "company-dc": "https://images.steamusercontent.com/ugc/216563764914212087/0D3CE328F7C181CC1FFFED8411A3BBE10F989663/?imw=5000&imh=5000&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=false",
    "company-warner": "https://i.makeagif.com/media/3-15-2016/Oo51Jl.gif",
    "company-universal": "https://media.tenor.com/hLayucjMNdwAAAAM/universal-intro.gif",
    "exxen": "https://media.tenor.com/8R0Mq9xvUh8AAAAM/exxen.gif",
    "company-paramount": "https://i.makeagif.com/media/4-28-2016/dIiXuy.gif",
    "company-disney": "https://i.makeagif.com/media/5-10-2017/FPwMOC.gif",
    "company-pixar": "https://i.pinimg.com/originals/98/29/af/9829af2048f945057b94de4c156b3d0b.gif",
    "company-fox": "https://media.tenor.com/4B2j48N2jMUAAAAM/20th-century-fox-logo.gif",
    "company-sony": "https://i.makeagif.com/media/9-16-2015/fHWxPA.gif",
    "company-lucasfilm": "https://public-assets.production.noteflight.com/coverArts/000/001/639/001/intro-disney.png",
    "company-newline": "https://i.makeagif.com/media/1-06-2024/26u1zG.gif",
    "company-lionsgate": "https://forums.rpgmakerweb.com/attachments/lions-gate-intro-gif.53687/",
    "company-dreamworks": "https://i.pinimg.com/originals/7b/61/d8/7b61d8df673ce983bfa6e570cc153274.gif",
    "company-illumination": "https://i.makeagif.com/media/4-28-2022/S9FpaI.gif",
    "company-a24": "https://media.tenor.com/6tcG0w9TLtYAAAAM/movies-a24.gif",
    "company-blumhouse": "https://i.makeagif.com/media/4-24-2026/crrlKv.gif",
    "company-hbo": "https://media.tenor.com/veJGMopBqToAAAAM/hbo-max-new-intro.gif",
    "bein": "https://images.samsung.com/is/image/samsung/assets/TOD_logo__.jpg",
    "tvplus": "https://cdnuploads.aa.com.tr/uploads/sirkethaberleri/Contents/2019/03/26/thumbs_b_c_f6b8b21e5f1cdb90693b43ea3d8ec447.jpg",
    "theme-korsanlar": "https://technotoday.com.tr/wp-content/uploads/2022/09/korsan-filmleri.jpg",
    "theme-buyuculuk": "https://images.justwatch.com/backdrop/310571689/s1440/guide.png",
    "theme-ejderhalar": "https://cdn.kayiprihtim.com/wp-content/uploads/2025/05/Dragonslayer-Eski-Ejderha-Filmi.jpg",
    "theme-kurt-adamlar": "https://cdn.kayiprihtim.com/wp-content/uploads/2024/03/Kurt-Adam-Filmleri-662x372.jpg",
    "theme-canavarlar": "https://www.gazetebirlik.com/cropImages/1280x/uploads/haberler/2026/03/MwMP-yesil-canavar-filmi-ne-en-iyi-modern-canavar-filmleri-hangileri.jpg",
    "gain": "https://cdn.webrazzi.com/uploads/2020/12/gain-444.png",
    "tabii": "https://cms-tabii-assets.tabii.com/thumbnails/23963_781e9dee-cbd8-491a-b166-0b1df25b3bd0_720.jpeg",
    "genre-aile": "https://www.kaanintavsiyesi.com/pictures/kesfet/153/20/uygunsuz-sahne-yok-ailece-izleyebileceginiz-tam-19-iyi-aile-filmi-onerisi-780x439.jpg",
    "genre-belgesel": "https://is1-ssl.mzstatic.com/image/thumb/Video126/v4/53/9d/48/539d4895-2681-d18b-a4bb-1580f0c2ff97/pr_source.lsr/1200x675.jpg",
    "genre-animasyon": "https://cdn-i.pr.trt.com.tr/trtportal/en_iyi_animasyon_filmleri_1400x812-21610464-0-11-1399-788.jpeg",
    "genre-aksiyon-ve-macera": "https://img-s1.onedio.com/id-62a0581099353f6018a58a3f/rev-0/w-900/h-506/f-jpg/s-b244f6004b27e2bc9dea85cff56f7d4f02a983e4.jpg",
    "genre-bilim-kurgu": "https://cdn.kayiprihtim.com/wp-content/uploads/2019/03/Edge-of-Tomorrow-devam-filmi.jpg",
    "genre-bilim-kurgu-ve-fantazi": "https://img.chip.com.tr/rcman/Cw820h461q95gm/storage/files/images/2024/02/06/film-8s1e.jpg",
    "genre-biyografi": "https://img.chip.com.tr/rcman/Cw1280h720q95gm/storage/files/images/2022/11/24/en-iyi-biyografik-filmler-imdb-puani-en-iyi-olan-50-filmi-sectik-MXLu.jpg",
    "genre-cocuklar": "https://ares.shiftdelete.net/2022/04/buz-devri-2-768x432.jpg",
    "genre-dram": "https://image.cnnturk.com/i/cnnturk/75/1200x675/600c19dcb57f15211c70ad82.jpg",
    "genre-fantastik": "https://www.cepkolik.com/wp-content/uploads/2021/02/fantastik-1.jpg",
    "genre-gerilim": "https://tr.web.img4.acsta.net/newsv7/21/01/25/15/02/54725080.png",
    "genre-gerceklik": "https://seyler.ekstat.com/img/max/800/6/6VUbBrkY2pnJNupo-636498068884377836.jpg",
    "genre-gizem": "https://media.senscritique.com/media/000019341326/1200/shutter_island.jpg",
    "genre-haberler": "https://i20.haber7.net/resize/1280x720/haber/haber7/photos/trt_habere_6_milyon_dolarlik_studyo13852209780_h1098252.jpg",
    "genre-kara-film": "https://img-s1.onedio.com/id-633fe1685a0c7258625f8239/rev-0/w-1200/h-720/f-jpg/s-27cad616c5cb1cf7daa1e01d7b0e945b70d27f5e.jpg",
    "genre-komedi": "https://cdn-i.pr.trt.com.tr/trt1/leyla-ile-mecnun-ana-resim-15413437-481-0-2879-2160.jpeg",
    "genre-korku": "https://www.ozan.com/wp-content/uploads/2023/05/2022-en-iyi-korku-filmleri-scream.jpg",
    "genre-kisa": "https://cdn.kayiprihtim.com/wp-content/uploads/2020/04/7-kisa-film-liste.jpg",
    "genre-macera": "https://www.cepkolik.com/wp-content/uploads/2021/02/dis-3.jpg",
    "genre-muzik": "https://ortakoltuk.com/wp-content/uploads/2023/01/bursa-bulbulu-8-e1673034988903.jpg",
    "genre-muzikal": "https://turkblogs.com/wp-content/uploads/2024/11/Movie_Review_La_La_Land.jpeg",
    "genre-oyun-gosterisi": "https://wp.saatolog.com.tr/wp-content/uploads/2025/11/2025-tiyatro-oyunlari-1.jpg",
    "genre-pembe-dizi": "https://i.ytimg.com/vi/XzUlvA76tu4/maxresdefault.jpg",
    "genre-romantik": "https://i.teknolojioku.com/2/1280/720/storage/files/images/2019/05/28/10-iB2p_cover.jpg",
    "genre-savas": "https://img-s2.onedio.com/id-62835e3f8eae178b168a62c7/rev-0/w-1200/h-800/f-jpg/s-7d40a3903b93d2966259b7de0855c1ecc520e857.jpg",
    "genre-savas-ve-politika": "https://foto.haberler.com/haber/2025/09/16/dunya-istilasi-los-angeles-savasi-filmi-19054776_8292_amp.jpg",
    "genre-spor": "https://www.ortadogugazetesi.com/uploads/upload-image/2026/4/1776793595327-image.jpg",
    "genre-suc": "https://img.chip.com.tr/storage/files/images/2023/01/05/39-olagan-supheliler-1995-hb5G.jpg",
    "genre-tv-filmi": "https://media.cumhuriyet.com.tr/Archive/4a0b1dbd-57a4-4138-9d02-695903c762dd.png",
    "genre-talk-show": "https://ahaslides.com/wp-content/uploads/2023/09/corden-behind-the-desk_custom-2d062278b16926aa01465fed0142cd07e6cbf853-s1100-c50-1024x681.jpg",
    "genre-tarih": "https://img.paratic.com/dosya/2017/06/tarihi-savas-filmleri-truva.jpg",
    "genre-vahsi-bati": "https://www.slashfilm.com/img/gallery/clint-eastwood-sergio-leone-connected-through-two-words-on-the-dollars-trilogy/a-language-barrier-blasted-down-with-blazing-pistols-1728922453.jpg",
    "trend-playing": "https://image.hurimg.com/i/hurriyet/75/770x0/56b4850f67b0a93b8c3a2a58.jpg",
    "trend-day": "https://i.hizliresim.com/t1tnjh2w.jpg",
    "trend-month": "https://i.hizliresim.com/gzluwnmq.jpg",
    "trend-airing": "https://i.hizliresim.com/wtzttxm0.jpg",
    "trend-week": "https://i.hizliresim.com/s5fvc8or.jpg",
    "years-2020": "https://i.hizliresim.com/o7mboehi.jpg",
    "years-2010": "https://i.hizliresim.com/w64yhutc.jpg",
    "years-2000": "https://i.hizliresim.com/qr6kglyz.jpg",
    "years-40": "https://i.hizliresim.com/x7n4c9up.jpg",
    "years-50": "https://i.hizliresim.com/cj4njdt9.jpg",
    "years-60": "https://i.hizliresim.com/46u2urfj.jpg",
    "years-70": "https://i.hizliresim.com/vsaipw1z.jpg",
    "years-80": "https://i.hizliresim.com/tg2zdm9x.jpg",
    "years-90": "https://i.hizliresim.com/uhsecfii.jpg",
    "genre-aksiyon": "https://www.cepkolik.com/wp-content/uploads/2021/02/aksiyon-filmleri.jpg",
    "theme-uzay": "https://i.pinimg.com/736x/ee/5c/9d/ee5c9d05ff3fc8352caf1a7c998e8c26.jpg",
    "theme-kovboy": "https://images3.alphacoders.com/612/thumb-1920-612633.jpg",
    "theme-paralel-evren": "https://www.tarihiolaylar.com/img/tarihiolaylar/tarihi_olaylar_paralel-evren-jpg_890904099_1430405685.jpg",
    "theme-uzaylilar": "https://4kwallpapers.com/images/walls/thumbs_2t/18262.jpg",
    "theme-zaman-yolculugu": "https://img.goodfon.com/wallpaper/big/0/ee/mashina-vremeni-time-machine-137.webp",
    "theme-vampirler": "https://wallpapercave.com/wp/wp14762208.jpg",
    "theme-zombiler": "https://wallpaperaccess.com/full/5021288.jpg",
    "theme-robotlar": "https://wallpaperaccess.com/full/2083589.jpg",
    "theme-super-kahramanlar": "https://wallpapers.com/images/hd/4k-avengers-fight-stance-xkimrnmv85vgai6m.jpg",
    "theme-hayaletler": "https://wallpaperaccess.com/full/1148791.jpg",
    "theme-distopya": "https://wallpapertag.com/wallpaper/full/6/4/7/889780-top-dystopia-wallpapers-1920x1200-htc.jpg",
    "theme-casusluk": "https://wallpapercave.com/wp/LmO8jh0.jpg",
    "theme-soygun": "https://cdn.milenio.com/uploads/media/2021/12/01/personajes-de-la-casa-de_0_0_1200_747.jpeg",
    "theme-mafya": "https://wallpaperaccess.com/full/1107716.jpg",
    "theme-samuray": "https://wallpapercave.com/wp/wp2843801.jpg",
    "theme-dedektif": "https://wallpapercave.com/wp/wp2329742.jpg",
    "theme-hapishane": "https://wallpapers.com/images/hd/prison-break-major-characters-cover-7sl5hhfclcf6ub0h.jpg",
}


# ════════════════════════════════════════════════════════════════════════════
#  Küçük yardımcılar
# ════════════════════════════════════════════════════════════════════════════

def _coll():
    return db.dbs["tracking"][_COLL]


def _oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(status_code=404, detail="Koleksiyon bulunamadı")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _clean_text(value: Any, max_len: int = 80) -> str:
    return str(value or "").strip()[:max_len]


def _clean_url(value: Any) -> str:
    v = str(value or "").strip()[:600]
    if v and not re.match(r"^https?://", v, re.I):
        raise HTTPException(status_code=400, detail="Görsel adresi http:// veya https:// ile başlamalı")
    return v


def _norm(text: Any) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).lower()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", s.replace("ı", "i"))


def _ts(value: Any) -> float:
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except Exception:
            return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    return 0.0


def _storages():
    """Veritabanı parçalarını yeniden eskiye doğru döndürür."""
    for i in range(db.current_db_index, 0, -1):
        yield db.dbs[f"storage_{i}"]


def _public_doc(doc: dict) -> dict:
    """Mongo dokümanını JSON'a uygun hale getirir."""
    out = dict(doc)
    out["_id"] = str(out.get("_id"))
    for k in ("created_at", "updated_at"):
        if isinstance(out.get(k), datetime):
            out[k] = out[k].isoformat()
    return out


_LIGHT_PROJ = {
    "_id": 0, "imdb_id": 1, "tmdb_id": 1, "media_type": 1, "title": 1, "title_tr": 1,
    "poster": 1, "poster_tr": 1, "backdrop": 1, "backdrop_tr": 1,
    "release_year": 1, "rating": 1, "updated_on": 1,
}


def _light(doc: dict) -> dict:
    """Önizleme/sıralama için hafif içerik özeti."""
    return {
        "imdb_id":      doc.get("imdb_id") or "",
        "media_type":   doc.get("media_type") or "movie",
        "title":        doc.get("title_tr") or doc.get("title") or "",
        "poster":       doc.get("poster_tr") or doc.get("poster") or "",
        "backdrop":     doc.get("backdrop_tr") or doc.get("backdrop") or "",
        "release_year": doc.get("release_year"),
        "rating":       doc.get("rating") or 0,
        "updated_on":   _ts(doc.get("updated_on")),
    }


def _rating(item: dict) -> float:
    try:
        return float(item.get("rating") or 0)
    except (TypeError, ValueError):
        return 0.0


def _year(item: dict) -> int:
    try:
        return int(item.get("release_year") or 0)
    except (TypeError, ValueError):
        return 0


# ════════════════════════════════════════════════════════════════════════════
#  Klasör kurallarını içeriğe çevirme
# ════════════════════════════════════════════════════════════════════════════

def _mt_ok(folder_media: str, item_media: str) -> bool:
    return folder_media in ("all", "", None) or folder_media == item_media


async def _by_imdb_ids(imdb_ids: List[str]) -> List[dict]:
    """imdb_id listesini (sırayı koruyarak) hafif içerik özetlerine çevirir."""
    wanted = [i for i in dict.fromkeys(imdb_ids) if i]
    found: Dict[str, dict] = {}
    for st in _storages():
        missing = [i for i in wanted if i not in found]
        if not missing:
            break
        for coll_name in ("movie", "tv"):
            cursor = st[coll_name].find({"imdb_id": {"$in": missing}}, _LIGHT_PROJ)
            for d in await cursor.to_list(None):
                d.setdefault("media_type", coll_name)
                found.setdefault(d["imdb_id"], _light(d))
    return [found[i] for i in wanted if i in found]


async def _actor_items(name: str) -> List[dict]:
    rx = {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}
    items: Dict[str, dict] = {}
    for st in _storages():
        for coll_name in ("movie", "tv"):
            cursor = st[coll_name].find({"cast": rx}, _LIGHT_PROJ).limit(500)
            for d in await cursor.to_list(None):
                d.setdefault("media_type", coll_name)
                if d.get("imdb_id") and d["imdb_id"] not in items:
                    items[d["imdb_id"]] = _light(d)
    return list(items.values())


def _meta_item(meta: dict, default_mt: str = "tv") -> dict:
    return {
        "imdb_id":      meta["imdb_id"],
        "media_type":   meta.get("media_type") or default_mt,
        "title":        meta.get("title_tr") or meta.get("title") or "",
        "poster":       meta.get("poster_tr") or meta.get("poster") or "",
        "backdrop":     meta.get("backdrop_tr") or meta.get("backdrop") or "",
        "release_year": meta.get("release_year"),
        "rating":       meta.get("rating") or 0,
        "updated_on":   _ts(meta.get("updated_on")),
    }


def _platform_items(key: str) -> List[dict]:
    if key not in PLATFORM_LABELS or not platform_catalog.is_loaded():
        return []
    return [_meta_item(m) for m in platform_catalog.get(key) if m.get("imdb_id")]


def _collection_groups() -> Dict[str, List[dict]]:
    """{tmdb_koleksiyon_id: [film özetleri]} — bellekteki katalogdan (seri filmler)."""
    groups: Dict[str, List[dict]] = {}
    if not platform_catalog.is_loaded():
        return groups
    for m in platform_catalog.get_collection_movies():
        if m.get("imdb_id") and m.get("collection_id"):
            groups.setdefault(str(m["collection_id"]), []).append(_meta_item(m, "movie"))
    return groups


async def _collection_items(collection_id: Any) -> List[dict]:
    cid = str(collection_id or "").strip()
    if not cid:
        return []
    if platform_catalog.is_loaded():
        return list(_collection_groups().get(cid, []))
    # Katalog henüz yüklenmediyse doğrudan veritabanından
    vals: List[Any] = [cid] + ([int(cid)] if cid.isdigit() else [])
    items: Dict[str, dict] = {}
    for st in _storages():
        for d in await st["movie"].find({"collection_id": {"$in": vals}}, _LIGHT_PROJ).to_list(None):
            d.setdefault("media_type", "movie")
            if d.get("imdb_id"):
                items.setdefault(d["imdb_id"], _light(d))
    return list(items.values())


def _sections_of(folder: dict) -> List[dict]:
    """Klasörün içerik kaynaklarını döndürür.

    sections tanımlıysa onlar; değilse klasörün kendisi tek bir kaynak gibi davranır
    (eski klasörlerle geriye dönük uyumluluk, id=\"\").
    """
    secs = [s for s in (folder.get("sections") or []) if s.get("id")]
    if secs:
        return secs
    return [{"id": "", "title": folder.get("title", ""), "media": folder.get("media", "all"),
             "rule": folder.get("rule") or {}}]


def _source_key(folder: dict, view: dict) -> str:
    """Katalog id'sinde kullanılan anahtar: <klasör_id> veya <klasör_id>.<bölüm_id>."""
    return f"{folder['id']}.{view['id']}" if view.get("id") else folder["id"]


async def resolve_folder(folder: dict) -> List[dict]:
    """Bir klasörün kuralına göre sıralı içerik özetlerini döndürür (medya türü süzgeci dahil)."""
    rule = folder.get("rule") or {}
    rtype = rule.get("type")
    sort = rule.get("sort") or ("new" if rtype == "platform" else "year")

    if rtype == "platform":
        items = _platform_items(rule.get("platform", ""))
    elif rtype == "actor":
        items = await _actor_items(rule.get("name", ""))
    elif rtype == "collection":
        items = await _collection_items(rule.get("collection_id"))
        sort = rule.get("sort") or "oldest"
    elif rtype == "company":
        items = await _company_items(rule, folder.get("media") or "all")
        sort = rule.get("sort") or "year"
    elif rtype == "country":
        items = await _country_items(rule, folder.get("media") or "all")
        sort = rule.get("sort") or "year"
    elif rtype == "director":
        items = await _director_items(rule, folder.get("media") or "all")
        sort = rule.get("sort") or "year"
    elif rtype == "genre":
        items = await _genre_items(rule, folder.get("media") or "all")
        sort = rule.get("sort") or "new"
    elif rtype == "theme":
        items = await _theme_items(rule, folder.get("media") or "all")
        sort = rule.get("sort") or "popular"
    elif rtype == "years":
        items = await _years_items(rule, folder.get("media") or "all")
        sort = rule.get("sort") or "new"
    elif rtype == "tmdb_list":
        items = await _tmdb_list_items(rule, folder.get("media") or "all")
        sort = "keep"  # TMDB sırası
    elif rtype in ("manual", "titles"):
        items = await _by_imdb_ids(rule.get("items") or [])
        sort = "keep"
    else:
        items = []

    items = [i for i in items if _mt_ok(folder.get("media", "all"), i.get("media_type"))]

    if sort == "new":
        items.sort(key=lambda i: (i["updated_on"], _year(i)), reverse=True)
    elif sort == "popular":
        items.sort(key=lambda i: (_rating(i), _year(i)), reverse=True)
    elif sort == "year":
        items.sort(key=lambda i: (_year(i), _rating(i)), reverse=True)
    elif sort == "oldest":  # film serileri: ilk film başta
        items.sort(key=lambda i: (_year(i) or 9999, -_rating(i)))
    return items


# Katalog isteklerinde aynı klasörün tekrar tekrar hesaplanmaması için kısa süreli önbellek
_RESOLVE_CACHE: Dict[str, Tuple[float, List[dict]]] = {}
_RESOLVE_TTL = 60.0


async def _resolve_cached(folder: dict) -> List[dict]:
    rule_sig = repr(sorted((folder.get("rule") or {}).items(), key=lambda kv: kv[0]))
    key = f"{folder.get('id')}|{folder.get('media')}|{rule_sig}"
    now = time.monotonic()
    hit = _RESOLVE_CACHE.get(key)
    if hit and now - hit[0] < _RESOLVE_TTL:
        return hit[1]
    items = await resolve_folder(folder)
    if len(_RESOLVE_CACHE) > 300:
        _RESOLVE_CACHE.clear()
    _RESOLVE_CACHE[key] = (now, items)
    return items


# ════════════════════════════════════════════════════════════════════════════
#  Stremio / Nuvio katalog entegrasyonu (stremio_routes.py tarafından çağrılır)
# ════════════════════════════════════════════════════════════════════════════

_MT_SUFFIX = {"movie": "movie", "tv": "series"}


def _folder_types(folder: dict) -> List[str]:
    media = folder.get("media", "all")
    if media == "movie":
        return ["movie"]
    if media == "tv":
        return ["series"]
    return ["movie", "series"]


def folder_catalog_id(folder_id: str, stremio_type: str, lang: str) -> str:
    return f"kol_{folder_id}-{stremio_type}_{lang}"


# ════════════════════════════════════════════════════════════════════════════
#  Dil desteği: koleksiyon / klasör / bölüm adları (de, en). tr ve original → Türkçe (kayıtlı ad).
# ════════════════════════════════════════════════════════════════════════════
# (tr, en, de) — yalnızca çevrilmesi gereken adlar; platform/şirket/kişi adları olduğu gibi kalır.
_L10N_ROWS: List[Tuple[str, str, str]] = [
    # Koleksiyon adları
    ("Dijital Platformlar", "Digital Platforms", "Digitale Plattformen"),
    ("Yapım Şirketleri", "Production Companies", "Produktionsfirmen"),
    ("Ülkeler", "Countries", "Länder"),
    ("Türler", "Genres", "Genres"),
    ("Temalar", "Themes", "Themen"),
    ("TMDB Trend Listesi", "TMDB Trending", "TMDB Trends"),
    ("Oyuncular", "Actors", "Schauspieler"),
    ("Yönetmenler", "Directors", "Regisseure"),
    ("Seri Filmler", "Movie Series", "Filmreihen"),
    # Yıllar (dönemler)
    ("Yıllar", "Years", "Jahre"),
    ("2020'ler", "2020s", "2020er"), ("2010'lar", "2010s", "2010er"), ("2000'ler", "2000s", "2000er"),
    ("90'lar", "90s", "90er"), ("80'ler", "80s", "80er"), ("70'ler", "70s", "70er"),
    ("60'lar", "60s", "60er"), ("50'ler", "50s", "50er"),
    ("40'lar ve Öncesi", "1940s & Earlier", "40er und früher"),
    # Trend klasörleri
    ("Günlük Trend", "Trending Today", "Heute im Trend"),
    ("Haftalık Trend", "Trending This Week", "Diese Woche im Trend"),
    ("Aylık Trend", "Trending This Month", "Diesen Monat im Trend"),
    ("Devam Eden Diziler", "Ongoing Series", "Laufende Serien"),
    ("Vizyondakiler", "In Theaters", "Im Kino"),
    # Türler
    ("Aile", "Family", "Familie"), ("Aksiyon", "Action", "Action"),
    ("Aksiyon ve Macera", "Action & Adventure", "Action & Abenteuer"),
    ("Animasyon", "Animation", "Animation"), ("Belgesel", "Documentary", "Dokumentation"),
    ("Bilim Kurgu", "Science Fiction", "Science-Fiction"),
    ("Bilim Kurgu ve Fantazi", "Sci-Fi & Fantasy", "Sci-Fi & Fantasy"),
    ("Biyografi", "Biography", "Biografie"), ("Çocuklar", "Kids", "Kinder"), ("Dram", "Drama", "Drama"),
    ("Fantastik", "Fantasy", "Fantasy"), ("Gerilim", "Thriller", "Thriller"),
    ("Gerçeklik", "Reality", "Reality"), ("Gizem", "Mystery", "Mystery"), ("Haberler", "News", "Nachrichten"),
    ("Kara Film", "Film Noir", "Film Noir"), ("Komedi", "Comedy", "Komödie"), ("Korku", "Horror", "Horror"),
    ("Kısa", "Short", "Kurzfilm"), ("Macera", "Adventure", "Abenteuer"), ("Müzik", "Music", "Musik"),
    ("Müzikal", "Musical", "Musical"), ("Oyun Gösterisi", "Game Show", "Spielshow"),
    ("Pembe Dizi", "Soap Opera", "Seifenoper"), ("Romantik", "Romance", "Romantik"), ("Savaş", "War", "Krieg"),
    ("Savaş ve Politika", "War & Politics", "Krieg & Politik"), ("Spor", "Sports", "Sport"),
    ("Suç", "Crime", "Kriminalität"), ("TV Filmi", "TV Movie", "TV-Film"), ("Talk-Show", "Talk Show", "Talkshow"),
    ("Tarih", "History", "Geschichte"), ("Vahşi Batı", "Western", "Western"),
    # Ülkeler (klasör adı)
    ("Türkiye", "Turkey", "Türkei"), ("Amerika", "United States", "USA"), ("İngiltere", "United Kingdom", "Großbritannien"),
    ("Fransa", "France", "Frankreich"), ("Almanya", "Germany", "Deutschland"), ("İtalya", "Italy", "Italien"),
    ("İspanya", "Spain", "Spanien"), ("Japonya", "Japan", "Japan"), ("Güney Kore", "South Korea", "Südkorea"),
    ("Hindistan", "India", "Indien"), ("Çin", "China", "China"), ("Rusya", "Russia", "Russland"),
    ("İsveç", "Sweden", "Schweden"), ("Danimarka", "Denmark", "Dänemark"), ("Norveç", "Norway", "Norwegen"),
    ("Polonya", "Poland", "Polen"), ("Hollanda", "Netherlands", "Niederlande"), ("Yunanistan", "Greece", "Griechenland"),
    ("İran", "Iran", "Iran"), ("Tayland", "Thailand", "Thailand"), ("Brezilya", "Brazil", "Brasilien"),
    ("Meksika", "Mexico", "Mexiko"), ("Kanada", "Canada", "Kanada"), ("Avustralya", "Australia", "Australien"),
    # Temalar (klasör adı)
    ("Uzay", "Space", "Weltraum"), ("Kovboy", "Cowboy", "Cowboy"), ("Paralel Evren", "Parallel Universe", "Paralleluniversum"),
    ("Uzaylılar", "Aliens", "Außerirdische"), ("Zaman Yolculuğu", "Time Travel", "Zeitreisen"),
    ("Vampirler", "Vampires", "Vampire"), ("Zombiler", "Zombies", "Zombies"),
    ("Robotlar ve Yapay Zeka", "Robots & AI", "Roboter & KI"), ("Süper Kahramanlar", "Superheroes", "Superhelden"),
    ("Hayaletler", "Ghosts", "Geister"), ("Distopya ve Kıyamet", "Dystopia & Apocalypse", "Dystopie & Apokalypse"),
    ("Casusluk", "Espionage", "Spionage"), ("Soygun", "Heist", "Raubüberfall"), ("Mafya", "Mafia", "Mafia"),
    ("Korsanlar", "Pirates", "Piraten"), ("Samuray ve Ninja", "Samurai & Ninja", "Samurai & Ninja"),
    ("Büyücülük", "Witchcraft", "Hexerei"), ("Ejderhalar", "Dragons", "Drachen"),
    ("Kurt Adamlar", "Werewolves", "Werwölfe"), ("Canavarlar", "Monsters", "Monster"),
    ("Dedektif", "Detective", "Detektiv"), ("Hapishane", "Prison", "Gefängnis"),
    # Bölüm adı çekirdekleri (tema etiketleri, ülke sıfatları, tek sözcük)
    ("Uzaylı", "Alien", "Außerirdische"), ("Vampir", "Vampire", "Vampir"), ("Zombi", "Zombie", "Zombie"),
    ("Robot ve Yapay Zeka", "Robot & AI", "Roboter & KI"), ("Süper Kahraman", "Superhero", "Superhelden"),
    ("Hayalet", "Ghost", "Geister"), ("Distopya", "Dystopian", "Dystopie"), ("Korsan", "Pirate", "Piraten"),
    ("Ejderha", "Dragon", "Drachen"), ("Kurt Adam", "Werewolf", "Werwolf"), ("Canavar", "Monster", "Monster"),
    ("Yerli", "Turkish", "Türkische"), ("Amerikan", "American", "Amerikanische"), ("İngiliz", "British", "Britische"),
    ("Fransız", "French", "Französische"), ("Alman", "German", "Deutsche"), ("İtalyan", "Italian", "Italienische"),
    ("İspanyol", "Spanish", "Spanische"), ("Japon", "Japanese", "Japanische"), ("Hint", "Indian", "Indische"),
    ("Rus", "Russian", "Russische"), ("Yunan", "Greek", "Griechische"),
]
_L10N: Dict[str, Dict[str, str]] = {}
for _tr, _en, _de in _L10N_ROWS:
    _L10N.setdefault(_tr, {"en": _en, "de": _de})
# Ülke sıfatları: klasör adı ile aynı olanlar (Çin, Güney Kore ...) için çekirdek karşılıkları
_L10N_ADJ: Dict[str, Dict[str, str]] = {
    "Güney Kore": {"en": "South Korean", "de": "Südkoreanische"}, "Çin": {"en": "Chinese", "de": "Chinesische"},
    "İsveç": {"en": "Swedish", "de": "Schwedische"}, "Danimarka": {"en": "Danish", "de": "Dänische"},
    "Norveç": {"en": "Norwegian", "de": "Norwegische"}, "Polonya": {"en": "Polish", "de": "Polnische"},
    "Hollanda": {"en": "Dutch", "de": "Niederländische"}, "İran": {"en": "Iranian", "de": "Iranische"},
    "Tayland": {"en": "Thai", "de": "Thailändische"}, "Brezilya": {"en": "Brazilian", "de": "Brasilianische"},
    "Meksika": {"en": "Mexican", "de": "Mexikanische"}, "Kanada": {"en": "Canadian", "de": "Kanadische"},
    "Avustralya": {"en": "Australian", "de": "Australische"},
}
_L10N_NOUN = {"Filmleri": ("Movies", "Filme"), "Filmler": ("Movies", "Filme"),
              "Dizileri": ("Series", "Serien"), "Diziler": ("Series", "Serien")}
_L10N_PREFIX = {"Yeni Eklenen": ("New", "Neu hinzugefügt:"), "Popüler": ("Popular", "Beliebt:")}
_SECTION_RX = re.compile(r"^(Yeni Eklenen |Popüler )?(.+?) (Filmleri|Dizileri|Filmler|Diziler)$")
_PAREN_SUFFIX = re.compile(r"\s*\((?:film|filmler|dizi|diziler|movies|series|filme|serien)\)\s*$", re.I)


def _l10n(text: str, lang: str) -> str:
    """Koleksiyon/klasör/bölüm adını istenen dile çevirir (bilinmeyen adlar olduğu gibi kalır).
    Sonundaki '(Filmler)' / '(Diziler)' gibi parantezli açıklamalar her zaman atılır."""
    text = _PAREN_SUFFIX.sub("", text or "").strip()
    if lang not in ("en", "de") or not text:
        return text
    hit = _L10N.get(text)
    if hit:
        return hit[lang]
    m = _SECTION_RX.match(text)
    if not m:
        return text
    pre, core, noun = m.groups()
    noun_t = _L10N_NOUN[noun][0 if lang == "en" else 1]
    core_t = (_L10N_ADJ.get(core) or _L10N.get(core) or {}).get(lang, core)
    if pre:
        pre_t = _L10N_PREFIX[pre.strip()][0 if lang == "en" else 1]
        return f"{pre_t} {core_t} {noun_t}"
    return f"{core_t} {noun_t}"


async def manifest_catalogs(lang: str) -> List[dict]:
    """Manifest'e eklenecek klasör katalogları (yalnızca aktif ve in_manifest=True koleksiyonlar).

    hide_from_home=True olan koleksiyonların katalogları, Stremio uyumlu istemcilerin ana ekranda
    satır olarak göstermemesi için "zorunlu ekstra" ile işaretlenir; Nuvio koleksiyon klasörleri
    bu kataloglara yine de doğrudan erişir.
    """
    out: List[dict] = []
    cursor = _coll().find({"active": True, "in_manifest": True}).sort("order", 1)
    for col in await cursor.to_list(None):
        hide = bool(col.get("hide_from_home", False))
        for folder in col.get("folders", []):
            for view in _sections_of(folder):
                for stype in _folder_types(view):
                    if view.get("id"):
                        vt, ft = view.get("title", ""), folder.get("title", "")
                        # Ülkeler/Temalar: yalnızca bölüm adı ("Rus Filmleri", "Kovboy Dizileri")
                        if col.get("preset") in (PRESET_COUNTRIES, PRESET_THEMES, PRESET_YEARS) or _norm(ft) in _norm(vt):
                            name = _l10n(vt, lang)
                        else:
                            name = f"{_l10n(ft, lang)} · {_l10n(vt, lang)}"
                    else:
                        name = f"{_l10n(col.get('name', ''), lang)} · {_l10n(folder.get('title', ''), lang)}"
                    extra = [{"name": "skip"}]
                    if hide:
                        extra.append({"name": "koleksiyon", "isRequired": True, "options": ["klasor"]})
                    out.append({
                        "type": stype,
                        "id": folder_catalog_id(_source_key(folder, view), stype, lang),
                        "name": name,
                        "extra": extra,
                        "extraSupported": ["skip"] + (["koleksiyon"] if hide else []),
                    })
    return out


async def serve_catalog_page(cat_id: str, lang: str, skip: int, size: int) -> List[dict]:
    """kol_<klasör>-<tür>_<lang> kataloğunun ilgili sayfasını TAM doküman olarak döndürür."""
    raw = cat_id[len("kol_"):]
    for sfx in ("_tr", "_de", "_en", "_original"):
        if raw.endswith(sfx):
            raw = raw[: -len(sfx)]
            break
    source_key, _, stype = raw.partition("-")
    if not source_key or stype not in ("movie", "series"):
        return []
    folder_id, _, section_id = source_key.partition(".")

    col = await _coll().find_one({"folders.id": folder_id, "active": True})
    if not col:
        return []
    folder = next((f for f in col["folders"] if f["id"] == folder_id), None)
    if not folder:
        return []
    view = next((v for v in _sections_of(folder) if (v.get("id") or "") == section_id), None)
    if not view:
        return []

    wanted_mt = "movie" if stype == "movie" else "tv"
    items = [i for i in await _resolve_cached(view) if i.get("media_type") == wanted_mt]
    page = items[skip: skip + size]

    docs = await asyncio.gather(*(db.get_media_by_imdb(i["imdb_id"]) for i in page))
    return [d for d in docs if d]


# ════════════════════════════════════════════════════════════════════════════
#  TMDB yardımcıları (oyuncu fotoğrafı + platform logosu)
# ════════════════════════════════════════════════════════════════════════════

_TMDB_API = "https://api.themoviedb.org/3"
_TMDB_IMG = "https://image.tmdb.org/t/p"

# Platform anahtarı -> TMDB "watch provider" adının (normalize edilmiş) kabul edilen biçimleri.
# Yalnızca TAM eşleşme aranır ("max" gibi kısa adların başka platformlarla karışmaması için).
_TMDB_PROVIDER_NAMES: Dict[str, Tuple[str, ...]] = {
    "netflix": ("netflix",),
    "disney":  ("disneyplus", "disney"),
    "amazon":  ("amazonprimevideo", "primevideo", "amazonvideo"),
    "hbo":     ("hbomax", "max"),
    "bein":    ("beinconnect", "tod", "beinsports"),
    "exxen":   ("exxen",),
    "gain":    ("gain", "gaintv"),
    "apple":   ("appletvplus", "appletv"),
    "tabii":   ("tabii",),
    "tvplus":  ("turkcelltv", "tvplus"),
}

_TMDB_CACHE_TTL = 24 * 3600.0
_provider_cache: Dict[str, Any] = {"ts": 0.0, "logos": {}}
_logo_bytes_cache: Dict[str, Optional[bytes]] = {}
_person_cache: Dict[str, Tuple[float, Optional[dict]]] = {}


async def _tmdb_get(path: str, **params) -> Optional[dict]:
    api_key = Telegram.TMDB_API
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(f"{_TMDB_API}/{path}", params={**params, "api_key": api_key})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        _logger.warning("TMDB isteği başarısız (%s): %s", path, e)
        return None


async def tmdb_person(name: str, department: str = "") -> Optional[dict]:
    """Kişiyi TMDB'de arar; {id, name, profile_url} döner (fotoğrafı yoksa None).
    department="Directing" verilirse yönetmenler öne alınır."""
    key = _norm(name) + "|" + department
    hit = _person_cache.get(key)
    if hit and time.monotonic() - hit[0] < _TMDB_CACHE_TTL:
        return hit[1]
    result: Optional[dict] = None
    data = await _tmdb_get("search/person", query=name, language="tr-TR", include_adult="false")
    cands = [p for p in (data or {}).get("results", []) if p.get("profile_path")]
    if cands:
        exact = [p for p in cands if _norm(p.get("name")) == _norm(name)]
        pool = exact or cands
        if department:
            pool = [p for p in pool if p.get("known_for_department") == department] or pool
        best = max(pool, key=lambda p: float(p.get("popularity") or 0))
        result = {"id": best["id"], "name": best.get("name") or name,
                  "profile_url": f"{_TMDB_IMG}/w500{best['profile_path']}"}
    if data is not None:  # ağ hatasını önbelleğe alma
        _person_cache[key] = (time.monotonic(), result)
    return result


# Yapım şirketleri: (anahtar, görünen ad, TMDB şirket numarası ipuçları, ad önekleri (doğrulama), arama sözcüğü)
# Numaralar yalnızca ipucudur: TMDB'den adı doğrulanır, tutmazsa ada göre aranır.
COMPANY_SPECS: Dict[str, dict] = {c["key"]: c for c in [
    {"key": "marvel",     "label": "Marvel",             "ids": [420, 38679, 7505], "tokens": ("marvel",), "search": "Marvel"},
    {"key": "dc",         "label": "DC",                 "ids": [9993, 128064, 429], "tokens": ("dc",), "search": "DC Entertainment"},
    {"key": "paramount",  "label": "Paramount",          "ids": [4],        "tokens": ("paramount",), "search": "Paramount Pictures"},
    {"key": "warner",     "label": "Warner Bros.",       "ids": [174],      "tokens": ("warnerbros",), "search": "Warner Bros. Pictures"},
    {"key": "universal",  "label": "Universal",          "ids": [33],       "tokens": ("universalpictures",), "search": "Universal Pictures"},
    {"key": "disney",     "label": "Walt Disney",        "ids": [2],        "tokens": ("waltdisney",), "search": "Walt Disney Pictures"},
    {"key": "pixar",      "label": "Pixar",              "ids": [3],        "tokens": ("pixar",), "search": "Pixar"},
    {"key": "fox",        "label": "20th Century",       "ids": [25],       "tokens": ("20thcentury",), "search": "20th Century Studios"},
    {"key": "sony",       "label": "Sony / Columbia",    "ids": [34, 5],    "tokens": ("sonypictures", "columbiapictures"), "search": "Sony Pictures"},
    {"key": "lucasfilm",  "label": "Lucasfilm",          "ids": [1],        "tokens": ("lucasfilm",), "search": "Lucasfilm"},
    {"key": "newline",    "label": "New Line Cinema",    "ids": [12],       "tokens": ("newline",), "search": "New Line Cinema"},
    {"key": "legendary",  "label": "Legendary",          "ids": [923],      "tokens": ("legendary",), "search": "Legendary Pictures"},
    {"key": "lionsgate",  "label": "Lionsgate",          "ids": [1632],     "tokens": ("lionsgate",), "search": "Lionsgate"},
    {"key": "dreamworks", "label": "DreamWorks",         "ids": [521],      "tokens": ("dreamworks",), "search": "DreamWorks Animation"},
    {"key": "illumination", "label": "Illumination",     "ids": [6704],     "tokens": ("illumination",), "search": "Illumination"},
    {"key": "mgm",        "label": "MGM",                "ids": [21, 8411], "tokens": ("metrogoldwyn", "mgm"), "search": "Metro-Goldwyn-Mayer"},
    {"key": "a24",        "label": "A24",                "ids": [41077],    "tokens": ("a24",), "search": "A24"},
    {"key": "blumhouse",  "label": "Blumhouse",          "ids": [3172],     "tokens": ("blumhouse",), "search": "Blumhouse Productions"},
    {"key": "hbo",        "label": "HBO",                "ids": [3268],     "tokens": ("hbo",), "search": "HBO"},
    {"key": "amazon",     "label": "Amazon MGM Studios", "ids": [20580],    "tokens": ("amazon",), "search": "Amazon Studios"},
]}

# Ülkeler (sıra = klasör sırası; Türkiye en başta).
#   langs  : kütüphanedeki original_language değerleri ile eşleşir (Türkiye'de projedeki "Yerli" mantığı)
#   origin : İngilizce gibi birden çok ülkenin ortak dili olduğunda TMDB menşe ülkesi (discover) kullanılır
#   pages  : origin için TMDB discover'dan alınacak en fazla sayfa (sayfa başı 20 içerik, popülerliğe göre)
COUNTRY_SPECS: Dict[str, dict] = {c["code"]: c for c in [
    {"code": "tr", "title": "Türkiye",        "adj": "Yerli",           "langs": ["tr"], "rx": "^tur"},
    {"code": "us", "title": "Amerika",        "adj": "Amerikan",        "origin": "US", "pages": 40},
    {"code": "gb", "title": "İngiltere",      "adj": "İngiliz",         "origin": "GB", "pages": 20},
    {"code": "fr", "title": "Fransa",         "adj": "Fransız",         "langs": ["fr"], "rx": "^fre|^fra"},
    {"code": "de", "title": "Almanya",        "adj": "Alman",           "langs": ["de"], "rx": "^ger|^deu"},
    {"code": "it", "title": "İtalya",         "adj": "İtalyan",         "langs": ["it"], "rx": "^ita"},
    {"code": "es", "title": "İspanya",        "adj": "İspanyol",        "langs": ["es"], "rx": "^spa"},
    {"code": "jp", "title": "Japonya",        "adj": "Japon",           "langs": ["ja"], "rx": "^jap"},
    {"code": "kr", "title": "Güney Kore",     "adj": "Güney Kore",      "langs": ["ko"], "rx": "^kor"},
    {"code": "in", "title": "Hindistan",      "adj": "Hint",            "langs": ["hi", "ta", "te", "ml", "kn", "bn", "mr", "pa"], "rx": "^hin"},
    {"code": "cn", "title": "Çin",            "adj": "Çin",             "langs": ["zh", "cn"], "rx": "^chi|^man|^can"},
    {"code": "ru", "title": "Rusya",          "adj": "Rus",             "langs": ["ru"], "rx": "^rus"},
    {"code": "se", "title": "İsveç",          "adj": "İsveç",           "langs": ["sv"], "rx": "^swe"},
    {"code": "dk", "title": "Danimarka",      "adj": "Danimarka",       "langs": ["da"], "rx": "^dan"},
    {"code": "no", "title": "Norveç",         "adj": "Norveç",          "langs": ["no", "nb", "nn"], "rx": "^nor"},
    {"code": "pl", "title": "Polonya",        "adj": "Polonya",         "langs": ["pl"], "rx": "^pol"},
    {"code": "nl", "title": "Hollanda",       "adj": "Hollanda",        "langs": ["nl"], "rx": "^dut|^nld"},
    {"code": "gr", "title": "Yunanistan",     "adj": "Yunan",           "langs": ["el"], "rx": "^gre|^ell"},
    {"code": "ir", "title": "İran",           "adj": "İran",            "langs": ["fa"], "rx": "^per|^fas"},
    {"code": "th", "title": "Tayland",        "adj": "Tayland",         "langs": ["th"], "rx": "^tha"},
    {"code": "br", "title": "Brezilya",       "adj": "Brezilya",        "origin": "BR", "pages": 10},
    {"code": "mx", "title": "Meksika",        "adj": "Meksika",         "origin": "MX", "pages": 10},
    {"code": "ca", "title": "Kanada",         "adj": "Kanada",          "origin": "CA", "pages": 15},
    {"code": "au", "title": "Avustralya",     "adj": "Avustralya",      "origin": "AU", "pages": 15},
]}

_flag_cache: Dict[str, Optional[bytes]] = {}


async def flag_bytes(code: str) -> Optional[bytes]:
    """Bayrak görseli (flagcdn.com). Alınamazsa None → düz renkli GIF'e düşülür."""
    code = code.lower()
    if code in _flag_cache:
        return _flag_cache[code]
    data: Optional[bytes] = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://flagcdn.com/w640/{code}.png")
            r.raise_for_status()
            data = r.content
        _flag_cache[code] = data
    except Exception as e:
        _logger.warning("Bayrak indirilemedi (%s): %s", code, e)
    return data


async def _discover_origin(origin: str, kind: str, pages: int) -> List[int]:
    """TMDB discover: menşe ülkesine göre içerik numaraları (popülerliğe göre)."""
    ck = f"origin|{kind}|{origin}|{pages}"
    hit = _discover_cache.get(ck)
    if hit and time.monotonic() - hit[0] < 6 * 3600:
        return hit[1]
    out: List[int] = []
    ok = False
    for page in range(1, max(1, pages) + 1):
        d = await _tmdb_get(f"discover/{kind}", with_origin_country=origin, sort_by="popularity.desc",
                            page=page, include_adult="false")
        if d is None:
            break
        ok = True
        out += [r["id"] for r in d.get("results", []) if r.get("id")]
        if page >= int(d.get("total_pages") or 1):
            break
    if ok:
        _discover_cache[ck] = (time.monotonic(), out)
    return out


async def _country_items(rule: dict, media: str) -> List[dict]:
    spec = COUNTRY_SPECS.get(str(rule.get("country_key") or ""))
    langs = list(rule.get("langs") or (spec or {}).get("langs") or [])
    origin = rule.get("origin") or (spec or {}).get("origin")
    rx = (spec or {}).get("rx")
    items: Dict[str, dict] = {}
    for kind in ("movie", "tv"):
        if media not in ("all", "", None, kind):
            continue
        for st in _storages():
            if origin:
                ids = await _discover_origin(origin, kind, int((spec or {}).get("pages") or 10))
                vals: List[Any] = list(ids) + [str(i) for i in ids]
                docs: List[dict] = []
                for n in range(0, len(vals), 800):
                    docs += await st[kind].find({"tmdb_id": {"$in": vals[n:n + 800]}}, _LIGHT_PROJ).to_list(None)
            else:
                conds: List[dict] = [{"original_language": {"$in": langs}}]
                if rx:
                    conds.append({"original_language": {"$regex": rx, "$options": "i"}})
                docs = await st[kind].find({"$or": conds}, _LIGHT_PROJ).limit(3000).to_list(None)
            for d in docs:
                d.setdefault("media_type", kind)
                if d.get("imdb_id"):
                    items.setdefault(d["imdb_id"], _light(d))
    return list(items.values())


TREND_MONTH_DAYS = 30  # "Aylık Trend": son bu kadar günde çıkan yapımlar, popülerliğe göre

_list_cache: Dict[str, Tuple[float, List[int]]] = {}


async def _tmdb_list_ids(list_key: str, kind: str) -> List[int]:
    """TMDB listesindeki içerik numaraları, TMDB sırasıyla.

    trend_day / trend_week : trending/{kind}/day|week
    trend_month            : TMDB'de aylık trend uç noktası yok → son 30 günde çıkanlar, popülerliğe göre
    airing   (yalnız dizi) : devam eden (Returning Series) ve son 30 günde bölümü yayınlanan diziler
    now_playing (yalnız film): Türkiye vizyonundaki filmler
    """
    from datetime import date, timedelta
    ttl = 3600.0 if list_key in ("trend_day", "now_playing") else 6 * 3600.0
    ck = f"{list_key}|{kind}"
    hit = _list_cache.get(ck)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]

    today = date.today()
    since = (today - timedelta(days=TREND_MONTH_DAYS)).isoformat()
    path, params, pages = "", {"language": "tr-TR"}, 10
    if list_key == "trend_day":
        path = f"trending/{kind}/day"
    elif list_key == "trend_week":
        path = f"trending/{kind}/week"
    elif list_key == "trend_month":
        path, pages = f"discover/{kind}", 10
        date_field = "primary_release_date" if kind == "movie" else "first_air_date"
        params.update({"sort_by": "popularity.desc", f"{date_field}.gte": since,
                       f"{date_field}.lte": today.isoformat(), "include_adult": "false"})
    elif list_key == "airing" and kind == "tv":
        path = "discover/tv"
        params.update({"sort_by": "popularity.desc", "with_status": "0",
                       "air_date.gte": since, "include_adult": "false"})
    elif list_key == "now_playing" and kind == "movie":
        path, pages = "movie/now_playing", 5
        params.update({"region": "TR"})
    else:
        return []

    out: List[int] = []
    ok = False
    for page in range(1, pages + 1):
        d = await _tmdb_get(path, page=page, **params)
        if d is None:
            break
        ok = True
        out += [r["id"] for r in d.get("results", []) if r.get("id") and r["id"] not in out]
        if page >= int(d.get("total_pages") or 1):
            break
    if ok:
        _list_cache[ck] = (time.monotonic(), out)
    return out


async def _tmdb_list_items(rule: dict, media: str) -> List[dict]:
    """TMDB listesinin yalnızca kütüphanenizde bulunan öğeleri (TMDB sırası korunur)."""
    list_key = str(rule.get("list") or "")
    items: List[dict] = []
    for kind in ("tv", "movie"):
        if media not in ("all", "", None, kind):
            continue
        ids = await _tmdb_list_ids(list_key, kind)
        if not ids:
            continue
        rank = {i: n for n, i in enumerate(ids)}
        vals: List[Any] = list(ids) + [str(i) for i in ids]
        found: Dict[str, Tuple[int, dict]] = {}
        for st in _storages():
            for d in await st[kind].find({"tmdb_id": {"$in": vals}}, _LIGHT_PROJ).to_list(None):
                try:
                    r = rank.get(int(d.get("tmdb_id")), 10 ** 6)
                except (TypeError, ValueError):
                    continue
                d.setdefault("media_type", kind)
                if d.get("imdb_id"):
                    found.setdefault(d["imdb_id"], (r, _light(d)))
        items += [it for _r, it in sorted(found.values(), key=lambda x: x[0])]
    return items


_credits_cache: Dict[int, Tuple[float, Dict[str, List[int]]]] = {}


async def _director_credit_ids(person_id: int) -> Dict[str, List[int]]:
    """{'movie': [tmdb_id], 'tv': [tmdb_id]} — kişinin yönetmen olarak yer aldığı yapımlar."""
    hit = _credits_cache.get(person_id)
    if hit and time.monotonic() - hit[0] < 6 * 3600:
        return hit[1]
    data = await _tmdb_get(f"person/{person_id}/combined_credits", language="tr-TR")
    out: Dict[str, List[int]] = {"movie": [], "tv": []}
    for c in (data or {}).get("crew", []):
        if (c.get("job") or "").lower() == "director" and c.get("media_type") in out and c.get("id"):
            if c["id"] not in out[c["media_type"]]:
                out[c["media_type"]].append(c["id"])
    if data is not None:
        _credits_cache[person_id] = (time.monotonic(), out)
    return out


async def _director_items(rule: dict, media: str) -> List[dict]:
    try:
        pid = int(rule.get("person_id"))
    except (TypeError, ValueError):
        return []
    credits = await _director_credit_ids(pid)
    items: Dict[str, dict] = {}
    for kind in ("movie", "tv"):
        if media not in ("all", "", None, kind) or not credits.get(kind):
            continue
        vals: List[Any] = list(credits[kind]) + [str(i) for i in credits[kind]]
        for st in _storages():
            for d in await st[kind].find({"tmdb_id": {"$in": vals}}, _LIGHT_PROJ).to_list(None):
                d.setdefault("media_type", kind)
                if d.get("imdb_id"):
                    items.setdefault(d["imdb_id"], _light(d))
    return list(items.values())


async def _genre_items(rule: dict, media: str) -> List[dict]:
    genre = str(rule.get("genre") or "").strip()
    if not genre:
        return []
    field = "rating" if rule.get("sort") == "popular" else "updated_on"
    items: Dict[str, dict] = {}
    for kind in ("movie", "tv"):
        if media not in ("all", "", None, kind):
            continue
        for st in _storages():
            try:
                docs = await st[kind].find({"genres_tr": genre}, _LIGHT_PROJ).sort(field, -1).limit(600).to_list(None)
            except Exception as e:  # sıralama için indeks/bellek yetmezse sırasız dene
                _logger.warning("Tür sorgusu sıralanamadı (%s): %s", genre, e)
                docs = await st[kind].find({"genres_tr": genre}, _LIGHT_PROJ).limit(2000).to_list(None)
            for d in docs:
                d.setdefault("media_type", kind)
                if d.get("imdb_id"):
                    items.setdefault(d["imdb_id"], _light(d))
    return list(items.values())


_company_cache: Dict[str, Tuple[float, Optional[dict]]] = {}
_company_logo_cache: Dict[str, Optional[bytes]] = {}
_discover_cache: Dict[str, Tuple[float, List[int]]] = {}


async def resolve_company(key: str) -> Optional[dict]:
    """{ids, logo_path} — TMDB'de adı doğrulanmış şirket numaraları."""
    spec = COMPANY_SPECS.get(key)
    if not spec:
        return None
    hit = _company_cache.get(key)
    if hit and time.monotonic() - hit[0] < _TMDB_CACHE_TTL:
        return hit[1]
    ids: List[int] = []
    logo = ""
    ok = lambda name: any(_norm(name).startswith(t) for t in spec["tokens"])  # noqa: E731
    reached = False
    for cid in spec["ids"]:
        d = await _tmdb_get(f"company/{cid}")
        if d is not None:
            reached = True
        if d and ok(d.get("name")):
            ids.append(cid)
            logo = logo or d.get("logo_path") or ""
    if not ids:
        d = await _tmdb_get("search/company", query=spec["search"])
        if d is not None:
            reached = True
        for r in (d or {}).get("results", [])[:10]:
            if ok(r.get("name")) and len(ids) < 3:
                ids.append(r["id"])
                logo = logo or r.get("logo_path") or ""
    result = {"ids": ids, "logo_path": logo} if ids else None
    if reached:  # ağ hatası önbelleğe alınmaz
        _company_cache[key] = (time.monotonic(), result)
    return result


async def company_logo_bytes(key: str) -> Optional[bytes]:
    if key in _company_logo_cache:
        return _company_logo_cache[key]
    info = await resolve_company(key)
    data: Optional[bytes] = None
    if info and info.get("logo_path"):
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.get(f"{_TMDB_IMG}/w300{info['logo_path']}")
                r.raise_for_status()
                data = r.content
        except Exception as e:
            _logger.warning("Şirket logosu indirilemedi (%s): %s", key, e)
    if data is not None or not Telegram.TMDB_API:
        _company_logo_cache[key] = data
    return data


async def _discover_ids(company_ids: List[int], kind: str) -> List[int]:
    """TMDB discover: şirketlerin film/dizi numaraları (popülerliğe göre, en fazla 8 sayfa)."""
    ck = f"{kind}|{'|'.join(map(str, company_ids))}"
    hit = _discover_cache.get(ck)
    if hit and time.monotonic() - hit[0] < 6 * 3600:
        return hit[1]
    out: List[int] = []
    ok = False
    for page in range(1, 9):
        d = await _tmdb_get(f"discover/{kind}", with_companies="|".join(map(str, company_ids)),
                            sort_by="popularity.desc", page=page, include_adult="false")
        if d is None:
            break
        ok = True
        out += [r["id"] for r in d.get("results", []) if r.get("id")]
        if page >= int(d.get("total_pages") or 1):
            break
    if ok:
        _discover_cache[ck] = (time.monotonic(), out)
    return out


async def _company_items(rule: dict, media: str) -> List[dict]:
    ids = [int(i) for i in (rule.get("company_ids") or []) if str(i).isdigit()]
    if not ids:
        info = await resolve_company(rule.get("company_key") or "")
        ids = (info or {}).get("ids") or []
    if not ids:
        return []
    items: Dict[str, dict] = {}
    for kind, coll_name in (("movie", "movie"), ("tv", "tv")):
        if media not in ("all", "", None, coll_name):
            continue
        tmdb_ids = await _discover_ids(ids, kind)
        if not tmdb_ids:
            continue
        vals: List[Any] = list(tmdb_ids) + [str(i) for i in tmdb_ids]
        for st in _storages():
            for d in await st[coll_name].find({"tmdb_id": {"$in": vals}}, _LIGHT_PROJ).to_list(None):
                d.setdefault("media_type", coll_name)
                if d.get("imdb_id"):
                    items.setdefault(d["imdb_id"], _light(d))
    return list(items.values())


_collection_info_cache: Dict[str, Optional[dict]] = {}
_SERIES_SUFFIX = re.compile(r"\s*[-–:]?\s*(koleksiyonu|koleksiyon|serisi|seri|filmleri|collection|saga|reihe|filmreihe)\s*$", re.I)


async def tmdb_collection(collection_id: Any) -> Optional[dict]:
    """TMDB film serisi: {name, backdrop_url, poster_url}. Ad sondaki 'Koleksiyonu/Collection' ekinden arındırılır."""
    cid = str(collection_id)
    if cid in _collection_info_cache:
        return _collection_info_cache[cid]
    data = await _tmdb_get(f"collection/{cid}", language="tr-TR")
    info: Optional[dict] = None
    if data and data.get("name"):
        name = _SERIES_SUFFIX.sub("", data["name"]).strip() or data["name"]
        info = {
            "name": name,
            "backdrop_url": f"{_TMDB_IMG}/w780{data['backdrop_path']}" if data.get("backdrop_path") else "",
            "poster_url": f"{_TMDB_IMG}/w500{data['poster_path']}" if data.get("poster_path") else "",
        }
    if data is not None:
        _collection_info_cache[cid] = info
    return info


async def _provider_logos() -> Dict[str, str]:
    """{platform_anahtarı: logo_url} — TMDB watch-provider listesinden."""
    if _provider_cache["logos"] and time.monotonic() - _provider_cache["ts"] < _TMDB_CACHE_TTL:
        return _provider_cache["logos"]
    by_name: Dict[str, str] = {}
    for kind in ("tv", "movie"):
        for region in ("TR", "DE", "US"):
            data = await _tmdb_get(f"watch/providers/{kind}", watch_region=region, language="en-US")
            for p in (data or {}).get("results", []):
                if p.get("logo_path"):
                    by_name.setdefault(_norm(p.get("provider_name")), p["logo_path"])
    logos: Dict[str, str] = {}
    for key, names in _TMDB_PROVIDER_NAMES.items():
        for n in names:
            if n in by_name:
                logos[key] = f"{_TMDB_IMG}/w154{by_name[n]}"
                break
    if logos:
        _provider_cache.update(ts=time.monotonic(), logos=logos)
    return logos


async def platform_logo_bytes(key: str) -> Optional[bytes]:
    if key in _logo_bytes_cache:
        return _logo_bytes_cache[key]
    url = (await _provider_logos()).get(key)
    data: Optional[bytes] = None
    if url:
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.content
        except Exception as e:
            _logger.warning("Platform logosu indirilemedi (%s): %s", key, e)
    if data is not None or not Telegram.TMDB_API:
        _logo_bytes_cache[key] = data
    return data


# ════════════════════════════════════════════════════════════════════════════
#  Animasyonlu GIF üretici (hazır koleksiyon kutucukları)
# ════════════════════════════════════════════════════════════════════════════

_GIF_CACHE: Dict[str, bytes] = {}


def _hex(c: str) -> Tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _auto_colors(key: str) -> Tuple[str, str]:
    if key in _PLATFORM_COLORS:
        return _PLATFORM_COLORS[key]
    h = int(hashlib.md5(key.encode()).hexdigest()[:6], 16)
    r, g, b = (h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF
    # Parlak ama aşırı açık olmayan bir ton + koyu eşi
    c1 = f"#{max(r, 70):02x}{max(g, 70):02x}{max(b, 70):02x}"
    c2 = f"#{r // 5:02x}{g // 5:02x}{b // 5:02x}"
    return c1, c2


_TR_ASCII = str.maketrans("İıŞşĞğÜüÖöÇç", "IiSsGgUuOoCc")


def _load_font(size: int):
    """(font, unicode_tam_destekli_mi) döner.

    Türkçe karakterler için TrueType font gerekir (Dockerfile'da fonts-dejavu-core kurulur).
    Bulunamazsa Pillow'un yerleşik fontuna düşülür; bu font İ/Ş/Ğ/ü/ı gibi harfleri
    çizemediğinden make_gif() metni ASCII'ye çevirir.
    """
    from PIL import ImageFont
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size), True
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size), False
    except TypeError:
        return ImageFont.load_default(), False


def make_gif(text: str, key: str, width: int = 480, height: int = 270, frames: int = 14,
             logo: Optional[bytes] = None, plate: bool = False, sparkle: bool = False) -> bytes:
    """Renkli, parlak şeritli animasyonlu GIF. logo verilirse (TMDB platform logosu) yazının üstüne yerleştirilir."""
    from PIL import Image, ImageDraw

    c1, c2 = _auto_colors(key)
    a, b = _hex(c1), _hex(c2)

    base = Image.new("RGB", (width, height))
    px = ImageDraw.Draw(base)
    for x in range(width):
        t = x / max(1, width - 1)
        col = tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))
        px.line([(x, 0), (x, height)], fill=col)

    # Logo (varsa) — yuvarlatılmış köşeli, ortada, üst bölümde
    logo_h = 0
    logo_img = None
    if logo:
        try:
            logo_img = Image.open(io.BytesIO(logo)).convert("RGBA")
            if plate:
                # Şirket logoları genelde geniş ve tek renklidir (siyah/beyaz): beyaz yuvarlatılmış zemin üzerine koy
                box_w, box_h, pad = 300, 112, 14
                scale = min((box_w - 2 * pad) / logo_img.width, (box_h - 2 * pad) / logo_img.height)
                lw, lh = max(1, int(logo_img.width * scale)), max(1, int(logo_img.height * scale))
                logo_small = logo_img.resize((lw, lh), Image.LANCZOS)
                card = Image.new("RGBA", (lw + 2 * pad, lh + 2 * pad), (0, 0, 0, 0))
                ImageDraw.Draw(card).rounded_rectangle([0, 0, card.width - 1, card.height - 1],
                                                       radius=20, fill=(255, 255, 255, 240))
                card.alpha_composite(logo_small, (pad, pad))
                logo_img, logo_h = card, card.height
            else:
                logo_h = 118
                ratio = logo_h / logo_img.height
                logo_img = logo_img.resize((max(1, int(logo_img.width * ratio)), logo_h), Image.LANCZOS)
                mask = Image.new("L", logo_img.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, logo_img.width - 1, logo_img.height - 1],
                                                       radius=22, fill=255)
                alpha = logo_img.split()[3]
                logo_img.putalpha(Image.composite(alpha, Image.new("L", logo_img.size, 0), mask))
        except Exception as e:
            _logger.warning("Logo işlenemedi: %s", e)
            logo_img, logo_h = None, 0
    if logo_img is not None:
        base.paste(logo_img, ((width - logo_img.width) // 2, 26), logo_img)

    # Metni sığdır
    text = (text or "").strip()[:28] or "Koleksiyon"
    size = 56 if logo_img is None else 36
    font, full_unicode = _load_font(size)
    if not full_unicode:
        text = text.translate(_TR_ASCII)
    probe = ImageDraw.Draw(base)
    while size > 18:
        w = probe.textlength(text, font=font)
        if w <= width - 48:
            break
        size -= 4
        font, _ = _load_font(size)
    tw = probe.textlength(text, font=font)
    tx = (width - tw) / 2
    ty = (height - size) / 2 - 4 if logo_img is None else 26 + logo_h + 22

    # Tema GIF'leri: yanıp sönen yıldız/parçacıklar (anahtardan türetilen sabit konumlar)
    stars: List[Tuple[int, int, int, float]] = []
    if sparkle:
        import random
        rnd = random.Random(key)
        stars = [(rnd.randrange(8, width - 8), rnd.randrange(8, height - 8), rnd.choice((2, 2, 3, 4)),
                  rnd.random() * 6.2832) for _ in range(46)]

    out_frames = []
    band = 90
    for n in range(frames):
        frame = base.copy()
        if stars:
            import math
            sd = ImageDraw.Draw(frame)
            for sx, sy, sr, ph in stars:
                k = 0.5 + 0.5 * math.sin(ph + n / frames * 6.2832)
                v = int(90 + 165 * k)
                r = max(1, int(sr * (0.5 + k * 0.7)))
                sd.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(v, v, v))
        # Soldan sağa kayan parlak şerit
        shift = int((n / frames) * (width + 2 * band)) - band
        mask = Image.new("L", (width, height), 0)
        md = ImageDraw.Draw(mask)
        md.polygon([(shift, 0), (shift + band, 0), (shift + band - 60, height), (shift - 60, height)], fill=70)
        frame = Image.composite(Image.new("RGB", (width, height), (255, 255, 255)), frame, mask)

        d = ImageDraw.Draw(frame)
        d.text((tx + 2, ty + 3), text, font=font, fill=(0, 0, 0))
        d.text((tx, ty), text, font=font, fill=(255, 255, 255))
        out_frames.append(frame.convert("P", palette=Image.ADAPTIVE, colors=128))

    buf = io.BytesIO()
    out_frames[0].save(
        buf, format="GIF", save_all=True, append_images=out_frames[1:],
        duration=90, loop=0, disposal=2,
    )
    return buf.getvalue()


def make_flag_gif(text: str, flag_png: bytes, width: int = 480, height: int = 270, frames: int = 18) -> bytes:
    """Dalgalanan bayrak animasyonu (alt kısımda ülke adı)."""
    import math
    from PIL import Image, ImageDraw

    amp, strip = 11, 4
    flag = Image.open(io.BytesIO(flag_png)).convert("RGB")
    # Bayrağı kenarlarda boşluk kalmayacak şekilde (dalga payıyla) kutuya sığdır
    tw, th = width, height + 2 * amp
    scale = max(tw / flag.width, th / flag.height)
    nw, nh = max(tw, int(flag.width * scale)), max(th, int(flag.height * scale))
    flag = flag.resize((nw, nh), Image.LANCZOS)
    src = flag.crop(((nw - tw) // 2, (nh - th) // 2, (nw - tw) // 2 + tw, (nh - th) // 2 + th))

    text = (text or "").strip()[:28]
    size = 40
    font, full_unicode = _load_font(size)
    if not full_unicode:
        text = text.translate(_TR_ASCII)
    probe = ImageDraw.Draw(src)
    while size > 18 and probe.textlength(text, font=font) > width - 48:
        size -= 4
        font, _ = _load_font(size)
    tx = (width - probe.textlength(text, font=font)) / 2
    ty = height - size - 24

    out = []
    for n in range(frames):
        frame = Image.new("RGB", (width, height))
        shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        for x in range(0, width, strip):
            phase = 2 * math.pi * (1.6 * x / width - n / frames)
            dy = int(round(amp * math.sin(phase)))
            frame.paste(src.crop((x, amp + dy, x + strip, amp + dy + height)), (x, 0))
            c = math.cos(phase)
            sd.rectangle([x, 0, x + strip, height],
                         fill=(0, 0, 0, int(55 * -c)) if c < 0 else (255, 255, 255, int(45 * c)))
        frame = Image.alpha_composite(frame.convert("RGBA"), shade)
        bar = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bar)
        for y in range(height - 100, height):  # alttan yukarı koyulaşan şerit: yazı okunabilsin
            bd.line([(0, y), (width, y)], fill=(0, 0, 0, int(190 * (y - (height - 100)) / 100)))
        frame = Image.alpha_composite(frame, bar)
        d = ImageDraw.Draw(frame)
        d.text((tx + 2, ty + 2), text, font=font, fill=(0, 0, 0, 255))
        d.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))
        out.append(frame.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256))

    buf = io.BytesIO()
    out[0].save(buf, format="GIF", save_all=True, append_images=out[1:], duration=80, loop=0, disposal=2)
    return buf.getvalue()


@public_router.get("/koleksiyon/gif/{key}.gif")
async def koleksiyon_gif(key: str, t: str = Query("", max_length=40)):
    """Hazır koleksiyon kutucukları için animasyonlu GIF (kimlik doğrulaması gerekmez).

    Platform anahtarlarında (netflix, disney, ...) TMDB'den alınan platform logosu GIF'in üstüne eklenir.
    """
    if not re.fullmatch(r"[a-z0-9_\-]{1,40}", key):
        raise HTTPException(status_code=404, detail="Geçersiz ad")
    if key in DEFAULT_GIFS:  # varsayılan hazır GIF'e yönlendir
        return RedirectResponse(DEFAULT_GIFS[key], status_code=302,
                                headers={"Cache-Control": "public, max-age=3600"})
    country = COUNTRY_SPECS.get(key[8:]) if key.startswith("country-") else None
    if country:
        label = t or country["title"]
        flag = await flag_bytes(country["code"])
        cache_key = f"{key}|{label}|{'F' if flag else '-'}"
        data = _GIF_CACHE.get(cache_key)
        if data is None:
            if flag:
                data = await asyncio.to_thread(make_flag_gif, label, flag)
            else:
                data = await asyncio.to_thread(make_gif, label, key)
            if len(_GIF_CACHE) > 200:
                _GIF_CACHE.clear()
            _GIF_CACHE[cache_key] = data
        return Response(content=data, media_type="image/gif",
                        headers={"Cache-Control": "public, max-age=86400" if flag else "public, max-age=300"})
    company = COMPANY_SPECS.get(key[8:]) if key.startswith("company-") else None
    label = t or PLATFORM_LABELS.get(key) or (company or {}).get("label") or key.replace("-", " ").title()
    logo = None
    if key in PLATFORM_LABELS:
        logo = await platform_logo_bytes(key)
    elif company:
        logo = await company_logo_bytes(key[8:])
    cache_key = f"{key}|{label}|{'L' if logo else '-'}"
    data = _GIF_CACHE.get(cache_key)
    if data is None:
        data = await asyncio.to_thread(make_gif, label, key, logo=logo, plate=bool(company),
                                       sparkle=key.startswith("theme-"))
        if len(_GIF_CACHE) > 200:
            _GIF_CACHE.clear()
        _GIF_CACHE[cache_key] = data
    # Logo henüz alınamadıysa (TMDB geçici hata) tarayıcı/Nuvio sonucu uzun süre saklamasın
    wants_logo = key in PLATFORM_LABELS or bool(company)
    cache = "public, max-age=86400" if (logo or not wants_logo or not Telegram.TMDB_API) else "public, max-age=300"
    return Response(content=data, media_type="image/gif", headers={"Cache-Control": cache})


@admin_router.get("/tmdb/foto")
async def tmdb_photo(name: str = "", dept: str = "", _: bool = Depends(require_auth)):
    """Oyuncu adı için TMDB profil fotoğrafı adresini döndürür."""
    name = _clean_text(name)
    if not name:
        raise HTTPException(status_code=400, detail="Oyuncu adı gerekli")
    if not Telegram.TMDB_API:
        raise HTTPException(status_code=409, detail="TMDB_API anahtarı tanımlı değil")
    person = await tmdb_person(name, "Directing" if dept == "Directing" else "")
    if not person:
        raise HTTPException(status_code=404, detail="TMDB'de bu kişi için fotoğraf bulunamadı")
    return person


def _auto_gif_url(folder: dict, collection: dict) -> str:
    key = str(folder.get("gif_key") or collection.get("preset") or "default").lower()
    key = re.sub(r"[^a-z0-9_\-]", "-", key)[:40] or "default"
    if key in DEFAULT_GIFS:  # Nuvio'ya doğrudan adres ver (ekstra yönlendirme olmasın)
        return DEFAULT_GIFS[key]
    from urllib.parse import quote
    label = folder.get("title") or collection.get("name") or ""
    base = Telegram.BASE_URL or ""
    return f"{base}/stremio/koleksiyon/gif/{key}.gif?t={quote(label[:40])}"


# ════════════════════════════════════════════════════════════════════════════
#  Nuvio JSON dışa aktarma
# ════════════════════════════════════════════════════════════════════════════

def build_nuvio_export(collections: List[dict], addon_id: str, lang: str) -> List[dict]:
    result = []
    for col in collections:
        folders = []
        for f in col.get("folders", []):
            gif = f.get("focus_gif_url") or ""
            cover = f.get("cover_url") or gif or _auto_gif_url(f, col)
            focus = gif or (_auto_gif_url(f, col) if not f.get("cover_url") else "")
            sources = [
                {
                    "addonId": addon_id,
                    "type": stype,
                    "catalogId": folder_catalog_id(_source_key(f, view), stype, lang),
                    "genre": None,
                }
                for view in _sections_of(f)
                for stype in _folder_types(view)
            ]
            folders.append({
                "id": f["id"],
                "title": _l10n(f.get("title", ""), lang),
                "coverImageUrl": cover,
                "focusGifUrl": focus or None,
                "focusGifEnabled": bool(focus),
                "coverEmoji": f.get("emoji") or None,
                "tileShape": f.get("tile_shape") or "LANDSCAPE",
                "hideTitle": False,
                "catalogSources": sources,
            })
        result.append({
            "id": str(col["_id"]),
            "title": _l10n(col.get("name", ""), lang),
            "backdropImageUrl": col.get("backdrop_url") or None,
            "pinToTop": bool(col.get("pin_to_top", False)),
            "focusGlowEnabled": True,
            "viewMode": "TABBED_GRID",
            "showAllTab": True,
            "folders": folders,
        })
    return result


async def _active_collections() -> List[dict]:
    cursor = _coll().find({"active": True}).sort("order", 1)
    return await cursor.to_list(None)


@public_router.get("/{token}/{lang}/koleksiyonlar.json")
@public_router.get("/{token}/koleksiyonlar.json")
async def public_collections_export(token: str, lang: str = "tr", token_data: dict = Depends(verify_token)):
    """Üyenin Nuvio'ya içe aktarabileceği koleksiyon JSON'u."""
    lang = lang if lang in _SUPPORTED_LANGS else "tr"
    addon_id = f"telegram.media.{token[:8]}.{lang}"
    return build_nuvio_export(await _active_collections(), addon_id, lang)


@admin_router.get("/export")
async def admin_export(token: str, lang: str = "tr", _: bool = Depends(require_auth)):
    lang = lang if lang in _SUPPORTED_LANGS else "tr"
    if not await db.get_api_token(token):
        raise HTTPException(status_code=404, detail="Token bulunamadı")
    addon_id = f"telegram.media.{token[:8]}.{lang}"
    return build_nuvio_export(await _active_collections(), addon_id, lang)


# ════════════════════════════════════════════════════════════════════════════
#  Admin API — koleksiyon CRUD
# ════════════════════════════════════════════════════════════════════════════

async def _summarize(col: dict) -> dict:
    out = _public_doc(col)
    for f in out.get("folders", []):
        if f.get("sections"):
            total = 0
            for sec in f["sections"]:
                srule = sec.get("rule") or {}
                if srule.get("type") in ("manual", "titles"):
                    sec["item_count"] = len(srule.get("items") or [])
                elif srule.get("type") == "platform":
                    sec["item_count"] = len(await resolve_folder(sec))
                else:
                    sec["item_count"] = None
                total += sec["item_count"] or 0
            f["item_count"] = total if all(x.get("item_count") is not None for x in f["sections"]) else None
            continue
        rule = f.get("rule") or {}
        # Ucuz sayımlar: elle/başlık listesi → doğrudan; platform → bellekten
        if rule.get("type") in ("manual", "titles"):
            f["item_count"] = len(rule.get("items") or [])
        elif rule.get("type") in ("platform", "collection"):
            f["item_count"] = len(await resolve_folder(f))
        else:
            f["item_count"] = None  # oyuncu: önizlemede hesaplanır
    return out


@admin_router.get("")
async def list_collections(_: bool = Depends(require_auth)):
    cols = await _coll().find({}).sort("order", 1).to_list(None)
    return {"collections": [await _summarize(c) for c in cols]}


@admin_router.get("/meta")
async def collections_meta(_: bool = Depends(require_auth)):
    tokens = await db.get_all_api_tokens()
    return {
        "tokens": [
            {"token": t["token"], "name": t.get("name") or t["token"][:8]}
            for t in tokens if t.get("token") and not t.get("is_expired")
        ],
        "base_url": Telegram.BASE_URL or "",
        "platforms": [{"key": k, "label": v} for k, v in PLATFORM_LABELS.items()],
        "platform_loaded": platform_catalog.is_loaded(),
    }


@admin_router.post("")
async def create_collection(payload: dict, _: bool = Depends(require_auth)):
    name = _clean_text(payload.get("name"))
    if not name:
        raise HTTPException(status_code=400, detail="Koleksiyon adı gerekli")
    count = await _coll().count_documents({})
    doc = {
        "name": name,
        "description": _clean_text(payload.get("description"), 300),
        "preset": None,
        "active": True,
        "in_manifest": bool(payload.get("in_manifest", True)),
        "hide_from_home": bool(payload.get("hide_from_home", False)),
        "pin_to_top": bool(payload.get("pin_to_top", False)),
        "order": count,
        "backdrop_url": _clean_url(payload.get("backdrop_url")),
        "folders": [],
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    res = await _coll().insert_one(doc)
    doc["_id"] = res.inserted_id
    return _public_doc(doc)


@admin_router.put("/{cid}")
async def update_collection(cid: str, payload: dict, _: bool = Depends(require_auth)):
    upd: Dict[str, Any] = {}
    if "name" in payload:
        name = _clean_text(payload.get("name"))
        if not name:
            raise HTTPException(status_code=400, detail="Koleksiyon adı boş olamaz")
        upd["name"] = name
    if "description" in payload:
        upd["description"] = _clean_text(payload.get("description"), 300)
    if "active" in payload:
        upd["active"] = bool(payload["active"])
    if "in_manifest" in payload:
        upd["in_manifest"] = bool(payload["in_manifest"])
    if "backdrop_url" in payload:
        upd["backdrop_url"] = _clean_url(payload.get("backdrop_url"))
    if "hide_from_home" in payload:
        upd["hide_from_home"] = bool(payload["hide_from_home"])
    if "pin_to_top" in payload:
        upd["pin_to_top"] = bool(payload["pin_to_top"])
    if "order" in payload:
        try:
            upd["order"] = int(payload["order"])
        except (TypeError, ValueError):
            pass
    if not upd:
        raise HTTPException(status_code=400, detail="Güncellenecek alan yok")
    upd["updated_at"] = datetime.utcnow()
    res = await _coll().update_one({"_id": _oid(cid)}, {"$set": upd})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Koleksiyon bulunamadı")
    return {"ok": True}


@admin_router.delete("/{cid}")
async def delete_collection(cid: str, _: bool = Depends(require_auth)):
    res = await _coll().delete_one({"_id": _oid(cid)})
    if not res.deleted_count:
        raise HTTPException(status_code=404, detail="Koleksiyon bulunamadı")
    _RESOLVE_CACHE.clear()
    return {"ok": True}


# ── Klasörler ───────────────────────────────────────────────────────────────

def _build_rule(raw: dict) -> dict:
    rtype = (raw or {}).get("type")
    if rtype == "platform":
        key = raw.get("platform")
        if key not in PLATFORM_LABELS:
            raise HTTPException(status_code=400, detail="Geçersiz platform")
        return {"type": "platform", "platform": key,
                "sort": raw.get("sort") if raw.get("sort") in ("new", "popular") else "new"}
    if rtype == "actor":
        name = _clean_text(raw.get("name"), 80)
        if not name:
            raise HTTPException(status_code=400, detail="Oyuncu adı gerekli")
        return {"type": "actor", "name": name,
                "sort": raw.get("sort") if raw.get("sort") in ("year", "popular", "new") else "year"}
    if rtype == "collection":
        cid = str(raw.get("collection_id") or "").strip()
        if not cid.isdigit():
            raise HTTPException(status_code=400, detail="Geçerli bir TMDB koleksiyon numarası gerekli")
        return {"type": "collection", "collection_id": cid, "sort": "oldest"}
    if rtype == "manual":
        items = [str(i).strip() for i in (raw.get("items") or []) if str(i).strip()]
        return {"type": "manual", "items": list(dict.fromkeys(items))}
    raise HTTPException(status_code=400, detail="Geçersiz klasör kuralı")


def _build_folder(payload: dict, existing: Optional[dict] = None) -> dict:
    folder = dict(existing or {})
    folder.setdefault("id", _new_id())
    if "title" in payload or not existing:
        title = _clean_text(payload.get("title"))
        if not title:
            raise HTTPException(status_code=400, detail="Klasör adı gerekli")
        folder["title"] = title
    if "emoji" in payload:
        folder["emoji"] = _clean_text(payload.get("emoji"), 8)
    if "cover_url" in payload:
        folder["cover_url"] = _clean_url(payload.get("cover_url"))
    if "focus_gif_url" in payload:
        folder["focus_gif_url"] = _clean_url(payload.get("focus_gif_url"))
    if "tile_shape" in payload:
        folder["tile_shape"] = payload["tile_shape"] if payload["tile_shape"] in TILE_SHAPES else "LANDSCAPE"
    if "media" in payload or not existing:
        folder["media"] = payload.get("media") if payload.get("media") in ("all", "movie", "tv") else "all"
    if "rule" in payload or not existing:
        old_rule = (existing or {}).get("rule") or {}
        new_rule = _build_rule(payload.get("rule") or {})
        # Elle seçilmiş içerik listesi, kural düzenlenirken kaybolmasın
        if new_rule["type"] == "manual" and not new_rule["items"] and old_rule.get("type") == "manual":
            new_rule["items"] = old_rule.get("items", [])
        folder["rule"] = new_rule
    folder.setdefault("cover_url", "")
    folder.setdefault("focus_gif_url", "")
    folder.setdefault("tile_shape", "LANDSCAPE")
    folder.setdefault("emoji", "")
    return folder


@admin_router.post("/{cid}/folders")
async def add_folder(cid: str, payload: dict, _: bool = Depends(require_auth)):
    folder = _build_folder(payload)
    res = await _coll().update_one(
        {"_id": _oid(cid)},
        {"$push": {"folders": folder}, "$set": {"updated_at": datetime.utcnow()}},
    )
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Koleksiyon bulunamadı")
    return {"ok": True, "folder": folder}


@admin_router.put("/{cid}/folders/{fid}")
async def update_folder(cid: str, fid: str, payload: dict, _: bool = Depends(require_auth)):
    col = await _coll().find_one({"_id": _oid(cid)})
    if not col:
        raise HTTPException(status_code=404, detail="Koleksiyon bulunamadı")
    folders = col.get("folders", [])
    for idx, f in enumerate(folders):
        if f["id"] == fid:
            folders[idx] = _build_folder(payload, existing=f)
            break
    else:
        raise HTTPException(status_code=404, detail="Klasör bulunamadı")
    await _coll().update_one({"_id": col["_id"]},
                             {"$set": {"folders": folders, "updated_at": datetime.utcnow()}})
    _RESOLVE_CACHE.clear()
    return {"ok": True}


@admin_router.delete("/{cid}/folders/{fid}")
async def delete_folder(cid: str, fid: str, _: bool = Depends(require_auth)):
    res = await _coll().update_one(
        {"_id": _oid(cid)},
        {"$pull": {"folders": {"id": fid}}, "$set": {"updated_at": datetime.utcnow()}},
    )
    if not res.modified_count:
        raise HTTPException(status_code=404, detail="Klasör bulunamadı")
    _RESOLVE_CACHE.clear()
    return {"ok": True}


@admin_router.post("/{cid}/folders/{fid}/items")
async def add_folder_item(cid: str, fid: str, payload: dict, _: bool = Depends(require_auth)):
    imdb_id = _clean_text(payload.get("imdb_id"), 20)
    if not imdb_id:
        raise HTTPException(status_code=400, detail="imdb_id gerekli")
    if not await db.get_media_by_imdb(imdb_id):
        raise HTTPException(status_code=404, detail="Bu içerik veritabanında bulunamadı")
    col = await _coll().find_one({"_id": _oid(cid), "folders.id": fid})
    if not col:
        raise HTTPException(status_code=404, detail="Klasör bulunamadı")
    folder = next(f for f in col["folders"] if f["id"] == fid)
    if (folder.get("rule") or {}).get("type") not in ("manual", "titles"):
        raise HTTPException(status_code=400, detail="Bu klasör kural tabanlı; içerik elle eklenemez")
    items = (folder.get("rule") or {}).setdefault("items", [])
    if imdb_id in items:
        raise HTTPException(status_code=409, detail="Bu içerik zaten klasörde ekli")
    items.append(imdb_id)
    await _coll().update_one({"_id": col["_id"]},
                             {"$set": {"folders": col["folders"], "updated_at": datetime.utcnow()}})
    _RESOLVE_CACHE.clear()
    return {"ok": True}


@admin_router.delete("/{cid}/folders/{fid}/items/{imdb_id}")
async def remove_folder_item(cid: str, fid: str, imdb_id: str, _: bool = Depends(require_auth)):
    col = await _coll().find_one({"_id": _oid(cid), "folders.id": fid})
    folder = next((f for f in (col or {}).get("folders", []) if f["id"] == fid), None)
    items = ((folder or {}).get("rule") or {}).get("items") or []
    if imdb_id not in items:
        raise HTTPException(status_code=404, detail="Kayıt bulunamadı")
    folder["rule"]["items"] = [i for i in items if i != imdb_id]
    await _coll().update_one({"_id": col["_id"]},
                             {"$set": {"folders": col["folders"], "updated_at": datetime.utcnow()}})
    _RESOLVE_CACHE.clear()
    return {"ok": True}


@admin_router.get("/{cid}/folders/{fid}/preview")
async def preview_folder(cid: str, fid: str, limit: int = 60, sid: str = "", _: bool = Depends(require_auth)):
    col = await _coll().find_one({"_id": _oid(cid), "folders.id": fid})
    if not col:
        raise HTTPException(status_code=404, detail="Klasör bulunamadı")
    folder = next(f for f in col["folders"] if f["id"] == fid)
    views = _sections_of(folder)
    folder = next((v for v in views if (v.get("id") or "") == sid), views[0])
    items = await resolve_folder(folder)
    limit = max(1, min(limit, 200))
    return {
        "total": len(items),
        "movies": sum(1 for i in items if i["media_type"] == "movie"),
        "series": sum(1 for i in items if i["media_type"] == "tv"),
        "items": [{k: i[k] for k in ("imdb_id", "media_type", "title", "poster", "release_year")}
                  for i in items[:limit]],
        "editable": (folder.get("rule") or {}).get("type") in ("manual", "titles"),
    }


# ── Oyuncu arama (veritabanındaki cast alanından) ───────────────────────────

@admin_router.get("/oyuncu-ara")
async def search_actors(q: str = "", _: bool = Depends(require_auth)):
    q = (q or "").strip()[:60]
    if len(q) < 2:
        return {"results": []}
    rx = {"$regex": re.escape(q), "$options": "i"}
    pipeline = [
        {"$match": {"cast": rx}},
        {"$unwind": "$cast"},
        {"$match": {"cast": rx}},
        {"$group": {"_id": "$cast", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
        {"$limit": 15},
    ]
    counts: Dict[str, int] = {}
    for st in _storages():
        for coll_name in ("movie", "tv"):
            try:
                async for row in st[coll_name].aggregate(pipeline, maxTimeMS=10000):
                    if row["_id"]:
                        counts[row["_id"]] = counts.get(row["_id"], 0) + row["n"]
            except Exception as e:
                _logger.warning("oyuncu-ara hata: %s", e)
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
    return {"results": [{"name": n, "count": c} for n, c in top]}


# ════════════════════════════════════════════════════════════════════════════
#  Hazır koleksiyon üreticileri
# ════════════════════════════════════════════════════════════════════════════

async def _upsert_preset(preset_key: str, doc: dict, force: bool) -> str:
    """'created' | 'updated' | 'skipped' döner."""
    existing = await _coll().find_one({"preset": preset_key})
    if existing and not force:
        return "skipped"
    now = datetime.utcnow()
    doc = {**doc, "preset": preset_key, "updated_at": now}
    if existing:
        # Admin'in aktif/sıra/manifest ayarları korunur
        for keep in ("active", "order", "in_manifest"):
            doc[keep] = existing.get(keep, doc.get(keep))
        await _coll().update_one({"_id": existing["_id"]}, {"$set": doc})
        return "updated"
    doc.setdefault("active", True)
    doc.setdefault("in_manifest", True)
    doc["order"] = await _coll().count_documents({})
    doc["created_at"] = now
    await _coll().insert_one(doc)
    return "created"


PRESET_PLATFORMS = "platforms"
PRESET_ACTORS = "actors"
PRESET_COMPANIES = "companies"
PRESET_COUNTRIES = "countries"
PRESET_GENRES = "genres"
PRESET_TRENDS = "tmdb_trends"
PRESET_DIRECTORS = "directors"


def _platform_section_specs(label: str, media_present: set) -> List[Tuple[str, str, str]]:
    """(başlık, medya, sıralama) — sırayla: Yeni Diziler, Yeni Filmler, Popüler Diziler, Popüler Filmler."""
    specs: List[Tuple[str, str, str]] = []
    for prefix, sort in (("Yeni Eklenen", "new"), ("Popüler", "popular")):
        if "tv" in media_present:
            specs.append((f"{prefix} {label} Dizileri", "tv", sort))
        if "movie" in media_present:
            specs.append((f"{prefix} {label} Filmleri", "movie", sort))
    return specs


def _platform_sections(key: str) -> List[dict]:
    have = {i["media_type"] for i in _platform_items(key)}
    return [{"id": _new_id(), "title": title, "media": media,
             "rule": {"type": "platform", "platform": key, "sort": sort}}
            for title, media, sort in _platform_section_specs(PLATFORM_LABELS[key], have)]


def _platform_folder(key: str) -> Optional[dict]:
    """Bir platform için klasör (içerik yoksa None)."""
    sections = _platform_sections(key)
    if not sections:
        return None
    return {"id": _new_id(), "title": PLATFORM_LABELS[key], "emoji": "", "cover_url": "",
            "focus_gif_url": "", "gif_key": key, "tile_shape": "LANDSCAPE", "media": "all",
            "sections": sections}


def _platforms_collection() -> Tuple[dict, List[str]]:
    folders, empty = [], []
    for key, label in PLATFORM_LABELS.items():
        f = _platform_folder(key)
        (folders if f else empty).append(f or label)
    doc = {"name": "Dijital Platformlar",
           "description": "Netflix, Disney+ gibi platformların popüler ve yeni eklenen film/dizileri",
           "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": folders}
    return doc, empty


async def _merge_platform_folders(existing: dict, wanted: dict) -> List[str]:
    """Var olan 'Dijital Platformlar' koleksiyonunu günceller:
      • henüz olmayan platform klasörlerini ekler,
      • bölüm düzeni değişmişse (örn. kaldırılan 'Tümü' sekmesi) yalnızca bölümleri yeniler.
    Admin'in klasörlerde yaptığı düzenlemelere (kapak, GIF, ad) dokunulmaz."""
    by_key = {f.get("gif_key"): f for f in existing.get("folders", [])}
    notes: List[str] = []
    folders = list(existing.get("folders", []))
    changed = False
    for nf in wanted["folders"]:
        old = by_key.get(nf.get("gif_key"))
        if old is None:
            folders.append(nf)
            notes.append(nf["title"])
            changed = True
        elif [x["title"] for x in old.get("sections", [])] != [x["title"] for x in nf["sections"]]:
            old["sections"] = nf["sections"]
            notes.append(f"{nf['title']} (sekmeler yenilendi)")
            changed = True
    if changed:
        await _coll().update_one({"_id": existing["_id"]},
                                 {"$set": {"folders": folders, "updated_at": datetime.utcnow()}})
    return notes


PRESET_SERIES = "movie_series"


async def _series_folder(cid: str, movies: List[dict]) -> dict:
    """Bir film serisi (Harry Potter, Hızlı ve Öfkeli ...) için klasör; ad/kapak TMDB'den."""
    info = await tmdb_collection(cid) if Telegram.TMDB_API else None
    first = min(movies, key=lambda m: _year(m) or 9999)
    title = (info or {}).get("name") or f"{first.get('title') or 'Seri'} Serisi"
    cover = (info or {}).get("backdrop_url") or first.get("backdrop") or ""
    return {"id": _new_id(), "title": title, "emoji": "", "cover_url": cover,
            "focus_gif_url": "", "gif_key": f"seri-{cid}", "tile_shape": "LANDSCAPE", "media": "movie",
            "rule": {"type": "collection", "collection_id": cid, "sort": "oldest"}}


async def _series_collection(min_movies: int = 2) -> dict:
    groups = {c: m for c, m in _collection_groups().items() if len(m) >= min_movies}
    order = sorted(groups, key=lambda c: (-len(groups[c]), c))[:120]
    sem = asyncio.Semaphore(8)

    async def one(cid: str) -> dict:
        async with sem:
            return await _series_folder(cid, groups[cid])

    folders = await asyncio.gather(*(one(c) for c in order))
    folders.sort(key=lambda f: _norm(f["title"]))
    return {"name": "Seri Filmler",
            "description": "Harry Potter, Hızlı ve Öfkeli, Yüzüklerin Efendisi gibi film serileri",
            "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": list(folders)}


async def _merge_series_folders(existing: dict, wanted: dict) -> List[str]:
    """Kütüphaneye sonradan eklenen film serilerini mevcut koleksiyona ekler; var olanlara dokunmaz."""
    have = {str((f.get("rule") or {}).get("collection_id")) for f in existing.get("folders", [])}
    added = [f for f in wanted["folders"] if f["rule"]["collection_id"] not in have]
    if added:
        await _coll().update_one({"_id": existing["_id"]},
                                 {"$push": {"folders": {"$each": added}},
                                  "$set": {"updated_at": datetime.utcnow()}})
    return [f["title"] for f in added]


async def _company_folder(key: str) -> Optional[dict]:
    """Şirket klasörü: <Şirket> Filmleri / Dizileri. Kütüphanede içeriği yoksa (veya TMDB'ye ulaşılamazsa) None."""
    spec = COMPANY_SPECS[key]
    info = await resolve_company(key)
    if not info:
        return None
    rule = {"type": "company", "company_key": key, "company_ids": info["ids"], "sort": "year"}
    sections = []
    for media, noun in (("movie", "Filmleri"), ("tv", "Dizileri")):
        probe = {"rule": rule, "media": media}
        if await resolve_folder(probe):
            sections.append({"id": _new_id(), "title": f"{spec['label']} {noun}", "media": media,
                             "rule": dict(rule)})
    if not sections:
        return None
    return {"id": _new_id(), "title": spec["label"], "emoji": "", "cover_url": "", "focus_gif_url": "",
            "gif_key": f"company-{key}", "tile_shape": "LANDSCAPE", "media": "all", "sections": sections}


async def _companies_collection() -> Tuple[dict, List[str]]:
    sem = asyncio.Semaphore(4)

    async def one(key: str) -> Optional[dict]:
        async with sem:
            return await _company_folder(key)

    keys = list(COMPANY_SPECS)
    results = await asyncio.gather(*(one(k) for k in keys))
    folders = [f for f in results if f]
    empty = [COMPANY_SPECS[k]["label"] for k, f in zip(keys, results) if not f]
    return ({"name": "Yapım Şirketleri",
             "description": "Marvel, DC, Paramount gibi yapım şirketlerinin film ve dizileri",
             "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": folders}, empty)


async def _merge_company_folders(existing: dict, wanted: dict) -> List[str]:
    """Kütüphaneye sonradan eklenen şirket içeriklerine göre eksik klasörleri ekler; var olanlara dokunmaz."""
    have = {f.get("gif_key") for f in existing.get("folders", [])}
    added = [f for f in wanted["folders"] if f["gif_key"] not in have]
    if added:
        await _coll().update_one({"_id": existing["_id"]},
                                 {"$push": {"folders": {"$each": added}},
                                  "$set": {"updated_at": datetime.utcnow()}})
    return [f["title"] for f in added]


async def _country_folder(code: str) -> Optional[dict]:
    """Ülke klasörü: <Sıfat> Filmleri / <Sıfat> Dizileri (içeriği olmayan bölüm atlanır)."""
    spec = COUNTRY_SPECS[code]
    rule = {"type": "country", "country_key": code, "sort": "year"}
    if spec.get("origin"):
        rule["origin"] = spec["origin"]
    else:
        rule["langs"] = spec["langs"]
    sections = []
    for media, noun in (("movie", "Filmleri" if code != "tr" else "Filmler"),
                        ("tv", "Dizileri" if code != "tr" else "Diziler")):
        if await resolve_folder({"rule": rule, "media": media}):
            sections.append({"id": _new_id(), "title": f"{spec['adj']} {noun}", "media": media, "rule": dict(rule)})
    if not sections:
        return None
    return {"id": _new_id(), "title": spec["title"], "emoji": "", "cover_url": "", "focus_gif_url": "",
            "gif_key": f"country-{code}", "tile_shape": "LANDSCAPE", "media": "all", "sections": sections}


async def _countries_collection() -> Tuple[dict, List[str]]:
    sem = asyncio.Semaphore(3)

    async def one(code: str) -> Optional[dict]:
        async with sem:
            return await _country_folder(code)

    codes = list(COUNTRY_SPECS)  # Türkiye en başta
    results = await asyncio.gather(*(one(c) for c in codes))
    folders = [f for f in results if f]
    empty = [COUNTRY_SPECS[c]["title"] for c, f in zip(codes, results) if not f]
    return ({"name": "Ülkeler", "description": "Türkiye, Amerika, Fransa gibi ülkelerin film ve dizileri",
             "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": folders}, empty)


async def _merge_country_folders(existing: dict, wanted: dict) -> List[str]:
    """Kütüphaneye sonradan eklenen ülke içeriklerine göre eksik klasörleri ekler; var olanlara dokunmaz."""
    have = {f.get("gif_key") for f in existing.get("folders", [])}
    added = [f for f in wanted["folders"] if f["gif_key"] not in have]
    if added:
        folders = list(existing.get("folders", [])) + added
        order = {f"country-{c}": i for i, c in enumerate(COUNTRY_SPECS)}
        folders.sort(key=lambda f: order.get(f.get("gif_key"), 999))
        await _coll().update_one({"_id": existing["_id"]},
                                 {"$set": {"folders": folders, "updated_at": datetime.utcnow()}})
    return [f["title"] for f in added]


# Kütüphanedeki tür adları (genres_tr) — içeriği olmayanlar atlanır
GENRE_LIST: List[str] = [
    "Aile", "Aksiyon", "Aksiyon ve Macera", "Animasyon", "Belgesel", "Bilim Kurgu", "Bilim Kurgu ve Fantazi",
    "Biyografi", "Çocuklar", "Dram", "Fantastik", "Gerilim", "Gerçeklik", "Gizem", "Haberler", "Kara Film",
    "Komedi", "Korku", "Kısa", "Macera", "Müzik", "Müzikal", "Oyun Gösterisi", "Pembe Dizi", "Romantik",
    "Savaş", "Savaş ve Politika", "Spor", "Suç", "TV Filmi", "Talk-Show", "Tarih", "Vahşi Batı",
]

# Hiç oyuncu/yönetmen seçilmediğinde "Önerilen" olarak eklenen yönetmenler
SUGGESTED_DIRECTORS: List[str] = [
    "Christopher Nolan", "Steven Spielberg", "Martin Scorsese", "Quentin Tarantino", "James Cameron",
    "Ridley Scott", "David Fincher", "Denis Villeneuve", "Tim Burton", "Peter Jackson", "Stanley Kubrick",
    "Francis Ford Coppola", "Clint Eastwood", "Wes Anderson", "Guy Ritchie", "Zack Snyder",
    "Nuri Bilge Ceylan", "Ferzan Özpetek", "Yılmaz Erdoğan", "Fatih Akın", "Çağan Irmak",
]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", text).lower()
                  .replace("ı", "i").encode("ascii", "ignore").decode()).strip("-") or "x"


async def _genre_has(genre: str, kind: str) -> bool:
    for st in _storages():
        if await st[kind].find_one({"genres_tr": genre}, {"_id": 1}):
            return True
    return False


async def _genre_folder(genre: str) -> Optional[dict]:
    """Tür klasörü: Yeni Eklenen / Popüler × Diziler / Filmler (içeriği olmayan bölüm atlanır)."""
    have = {k: await _genre_has(genre, k) for k in ("tv", "movie")}
    sections = []
    for prefix, sort in (("Yeni Eklenen", "new"), ("Popüler", "popular")):
        for media, noun in (("tv", "Dizileri"), ("movie", "Filmleri")):
            if have[media]:
                sections.append({"id": _new_id(), "title": f"{prefix} {genre} {noun}", "media": media,
                                 "rule": {"type": "genre", "genre": genre, "sort": sort}})
    if not sections:
        return None
    return {"id": _new_id(), "title": genre, "emoji": "", "cover_url": "", "focus_gif_url": "",
            "gif_key": f"genre-{_slug(genre)}", "tile_shape": "LANDSCAPE", "media": "all", "sections": sections}


async def _genres_collection() -> Tuple[dict, List[str]]:
    results = await asyncio.gather(*(_genre_folder(g) for g in GENRE_LIST))
    folders = [f for f in results if f]
    empty = [g for g, f in zip(GENRE_LIST, results) if not f]
    return ({"name": "Türler", "description": "Aile, Aksiyon, Komedi gibi türlere göre yeni ve popüler içerikler",
             "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": folders}, empty)


# ════════════════════════════════════════════════════════════════════════════
#  Temalar (Uzay, Kovboy, Paralel Evren ...)
# ════════════════════════════════════════════════════════════════════════════
PRESET_THEMES = "themes"
_THEME_PAGES = 5  # TMDB discover: tema/tür başına en fazla sayfa (sayfa başı 20 içerik, popülerliğe göre)

# key: GIF/klasör anahtarı · title: klasör adı · label: bölüm adı öneki ("<label> Filmleri")
# keywords: TMDB anahtar kelimeleri (tam ad eşleşmesiyle aranır) · genres: kütüphane türleri (genres_tr)
THEME_SPECS: List[dict] = [
    {"key": "uzay",            "title": "Uzay",            "label": "Uzay",
     "keywords": ["space", "outer space", "space travel", "space mission", "astronaut"]},
    {"key": "kovboy",          "title": "Kovboy",          "label": "Kovboy",
     "keywords": ["cowboy", "western", "wild west"], "genres": ["Vahşi Batı"]},
    {"key": "paralel-evren",   "title": "Paralel Evren",   "label": "Paralel Evren",
     "keywords": ["parallel universe", "alternate universe", "multiverse", "parallel world", "alternate reality"]},
    {"key": "uzaylilar",       "title": "Uzaylılar",       "label": "Uzaylı",
     "keywords": ["alien", "extraterrestrial", "alien invasion", "alien life-form", "alien contact"]},
    {"key": "zaman-yolculugu", "title": "Zaman Yolculuğu", "label": "Zaman Yolculuğu",
     "keywords": ["time travel", "time loop", "time machine"]},
    {"key": "vampirler",       "title": "Vampirler",       "label": "Vampir",
     "keywords": ["vampire", "vampire hunter"]},
    {"key": "zombiler",        "title": "Zombiler",        "label": "Zombi",
     "keywords": ["zombie", "zombie apocalypse"]},
    {"key": "robotlar",        "title": "Robotlar ve Yapay Zeka", "label": "Robot ve Yapay Zeka",
     "keywords": ["robot", "artificial intelligence", "android", "cyborg"]},
    {"key": "super-kahramanlar", "title": "Süper Kahramanlar", "label": "Süper Kahraman",
     "keywords": ["superhero", "super power"]},
    {"key": "hayaletler",      "title": "Hayaletler",      "label": "Hayalet",
     "keywords": ["ghost", "haunted house", "haunting"]},
    {"key": "distopya",        "title": "Distopya ve Kıyamet", "label": "Distopya",
     "keywords": ["dystopia", "post-apocalyptic future", "apocalypse"]},
    {"key": "casusluk",        "title": "Casusluk",        "label": "Casusluk",
     "keywords": ["spy", "secret agent", "espionage"]},
    {"key": "soygun",          "title": "Soygun",          "label": "Soygun",
     "keywords": ["heist", "robbery", "bank robbery"]},
    {"key": "mafya",           "title": "Mafya",           "label": "Mafya",
     "keywords": ["mafia", "gangster", "organized crime"]},
    {"key": "korsanlar",       "title": "Korsanlar",       "label": "Korsan",
     "keywords": ["pirate", "pirate ship"]},
    {"key": "samuray",         "title": "Samuray ve Ninja", "label": "Samuray ve Ninja",
     "keywords": ["samurai", "ninja"]},
    {"key": "buyuculuk",       "title": "Büyücülük",       "label": "Büyücülük",
     "keywords": ["witch", "wizard", "witchcraft", "sorcerer"]},
    {"key": "ejderhalar",      "title": "Ejderhalar",      "label": "Ejderha",
     "keywords": ["dragon"]},
    {"key": "kurt-adamlar",    "title": "Kurt Adamlar",    "label": "Kurt Adam",
     "keywords": ["werewolf"]},
    {"key": "canavarlar",      "title": "Canavarlar",      "label": "Canavar",
     "keywords": ["monster", "giant monster", "kaiju", "creature"]},
    {"key": "dedektif",        "title": "Dedektif",        "label": "Dedektif",
     "keywords": ["detective", "private detective", "murder mystery"]},
    {"key": "hapishane",       "title": "Hapishane",       "label": "Hapishane",
     "keywords": ["prison", "prison escape", "prisoner"]},
]

_keyword_cache: Dict[str, Tuple[float, Optional[int]]] = {}


async def _keyword_id(term: str) -> Optional[int]:
    """TMDB anahtar kelimesinin numarası (yalnızca tam ad eşleşmesi). Bulunamazsa/ulaşılamazsa None."""
    ck = term.strip().lower()
    hit = _keyword_cache.get(ck)
    if hit and time.monotonic() - hit[0] < _TMDB_CACHE_TTL:
        return hit[1]
    d = await _tmdb_get("search/keyword", query=term)
    if d is None:
        return None  # geçici hata: önbelleğe alma
    found: Optional[int] = None
    for r in d.get("results", []):
        if (r.get("name") or "").strip().lower() == ck and r.get("id"):
            found = int(r["id"])
            break
    _keyword_cache[ck] = (time.monotonic(), found)
    return found


async def _discover_keywords(kw_ids: List[int], kind: str) -> List[int]:
    """TMDB discover: anahtar kelimelerden herhangi birine sahip yapımlar (popülerliğe göre)."""
    ck = f"kw|{kind}|{'|'.join(map(str, sorted(kw_ids)))}"
    hit = _discover_cache.get(ck)
    if hit and time.monotonic() - hit[0] < 6 * 3600:
        return hit[1]
    out: List[int] = []
    ok = False
    for page in range(1, _THEME_PAGES + 1):
        d = await _tmdb_get(f"discover/{kind}", with_keywords="|".join(map(str, kw_ids)),
                            sort_by="popularity.desc", page=page, include_adult="false")
        if d is None:
            break
        ok = True
        out += [r["id"] for r in d.get("results", []) if r.get("id")]
        if page >= int(d.get("total_pages") or 1):
            break
    if ok:
        _discover_cache[ck] = (time.monotonic(), out)
    return out


async def _theme_items(rule: dict, media: str) -> List[dict]:
    spec = next((t for t in THEME_SPECS if t["key"] == rule.get("theme_key")), {})
    terms = list(rule.get("keywords") or spec.get("keywords") or [])
    genres = list(rule.get("genres") or spec.get("genres") or [])
    kw_ids: List[int] = []
    for term in terms:
        kid = await _keyword_id(term)
        if kid and kid not in kw_ids:
            kw_ids.append(kid)
    items: Dict[str, dict] = {}
    for kind in ("movie", "tv"):
        if media not in ("all", "", None, kind):
            continue
        tmdb_ids = await _discover_keywords(kw_ids, kind) if kw_ids else []
        vals: List[Any] = list(tmdb_ids) + [str(i) for i in tmdb_ids]
        for st in _storages():
            docs: List[dict] = []
            for n in range(0, len(vals), 800):
                docs += await st[kind].find({"tmdb_id": {"$in": vals[n:n + 800]}}, _LIGHT_PROJ).to_list(None)
            if genres:
                docs += await st[kind].find({"genres_tr": {"$in": genres}}, _LIGHT_PROJ).limit(2000).to_list(None)
            for d in docs:
                d.setdefault("media_type", kind)
                if d.get("imdb_id"):
                    items.setdefault(d["imdb_id"], _light(d))
    return list(items.values())


async def _theme_folder(spec: dict) -> Optional[dict]:
    """Tema klasörü: <Tema> Filmleri / <Tema> Dizileri (içeriği olmayan bölüm atlanır)."""
    rule = {"type": "theme", "theme_key": spec["key"], "keywords": list(spec["keywords"]),
            "genres": list(spec.get("genres") or []), "sort": "popular"}
    sections = []
    for media, noun in (("movie", "Filmleri"), ("tv", "Dizileri")):
        if await resolve_folder({"rule": rule, "media": media}):
            sections.append({"id": _new_id(), "title": f"{spec['label']} {noun}", "media": media, "rule": dict(rule)})
    if not sections:
        return None
    return {"id": _new_id(), "title": spec["title"], "emoji": "", "cover_url": "", "focus_gif_url": "",
            "gif_key": f"theme-{spec['key']}", "tile_shape": "LANDSCAPE", "media": "all", "sections": sections}


async def _themes_collection() -> Tuple[dict, List[str]]:
    sem = asyncio.Semaphore(3)

    async def one(spec: dict) -> Optional[dict]:
        async with sem:
            return await _theme_folder(spec)

    results = await asyncio.gather(*(one(t) for t in THEME_SPECS))
    folders = [f for f in results if f]
    empty = [t["title"] for t, f in zip(THEME_SPECS, results) if not f]
    return ({"name": "Temalar", "description": "Uzay, Kovboy, Paralel Evren, Uzaylılar gibi temalara göre film ve diziler",
             "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": folders}, empty)


# ════════════════════════════════════════════════════════════════════════════
#  Yıllar (90'lar, 80'ler ...)
# ════════════════════════════════════════════════════════════════════════════
PRESET_YEARS = "years"

# (klasör adı, gif anahtarı, başlangıç yılı, bitiş yılı) — eskiden yeniye (40'lar → 2020'ler)
YEAR_SPECS: List[Tuple[str, str, int, int]] = [
    ("40'lar ve Öncesi", "years-40", 1880, 1949),
    ("50'ler", "years-50", 1950, 1959),
    ("60'lar", "years-60", 1960, 1969),
    ("70'ler", "years-70", 1970, 1979),
    ("80'ler", "years-80", 1980, 1989),
    ("90'lar", "years-90", 1990, 1999),
    ("2000'ler", "years-2000", 2000, 2009),
    ("2010'lar", "years-2010", 2010, 2019),
    ("2020'ler", "years-2020", 2020, 2029),
]


async def _years_items(rule: dict, media: str) -> List[dict]:
    try:
        y1, y2 = int(rule.get("year_from")), int(rule.get("year_to"))
    except (TypeError, ValueError):
        return []
    if y2 < y1:
        return []
    years = list(range(y1, y2 + 1))
    vals: List[Any] = years + [str(y) for y in years]  # yıl sayı ya da metin olarak saklanmış olabilir
    field = "rating" if rule.get("sort") == "popular" else "updated_on"
    items: Dict[str, dict] = {}
    for kind in ("movie", "tv"):
        if media not in ("all", "", None, kind):
            continue
        for st in _storages():
            q = {"release_year": {"$in": vals}}
            try:
                docs = await st[kind].find(q, _LIGHT_PROJ).sort(field, -1).limit(600).to_list(None)
            except Exception as e:  # sıralama için indeks/bellek yetmezse sırasız dene
                _logger.warning("Yıl sorgusu sıralanamadı (%s-%s): %s", y1, y2, e)
                docs = await st[kind].find(q, _LIGHT_PROJ).limit(2000).to_list(None)
            for d in docs:
                d.setdefault("media_type", kind)
                if d.get("imdb_id"):
                    items.setdefault(d["imdb_id"], _light(d))
    return list(items.values())


async def _year_folder(title: str, key: str, y1: int, y2: int) -> Optional[dict]:
    """Dönem klasörü: Yeni Eklenen Filmler / Yeni Eklenen Diziler / Popüler Diziler / Popüler Filmler."""
    sections = []
    for prefix, sort, media, noun in (("Yeni Eklenen", "new", "movie", "Filmleri"),
                                      ("Yeni Eklenen", "new", "tv", "Dizileri"),
                                      ("Popüler", "popular", "tv", "Dizileri"),
                                      ("Popüler", "popular", "movie", "Filmleri")):
        rule = {"type": "years", "year_from": y1, "year_to": y2, "sort": sort}
        if await resolve_folder({"rule": rule, "media": media}):
            sections.append({"id": _new_id(), "title": f"{prefix} {title} {noun}", "media": media, "rule": rule})
    if not sections:
        return None
    return {"id": _new_id(), "title": title, "emoji": "", "cover_url": "", "focus_gif_url": "",
            "gif_key": key, "tile_shape": "LANDSCAPE", "media": "all", "sections": sections}


async def _years_collection() -> Tuple[dict, List[str]]:
    results = await asyncio.gather(*(_year_folder(*spec) for spec in YEAR_SPECS))
    folders = [f for f in results if f]
    empty = [spec[0] for spec, f in zip(YEAR_SPECS, results) if not f]
    return ({"name": "Yıllar", "description": "90'lar, 80'ler gibi dönemlere göre yeni eklenen ve popüler film ve diziler",
             "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": folders}, empty)


async def _sort_years_folders(existing: dict) -> bool:
    """Var olan 'Yıllar' koleksiyonunda klasörleri 40'lar → 2020'ler sırasına dizer (değiştiyse True)."""
    order = {spec[1]: n for n, spec in enumerate(YEAR_SPECS)}
    col = await _coll().find_one({"_id": existing["_id"]})
    folders = list(col.get("folders", []))
    ranked = sorted(folders, key=lambda f: order.get(f.get("gif_key"), len(order)))  # kararlı sıralama
    if [f.get("id") for f in ranked] == [f.get("id") for f in folders]:
        return False
    await _coll().update_one({"_id": col["_id"]}, {"$set": {"folders": ranked, "updated_at": datetime.utcnow()}})
    return True


async def _merge_new_folders(existing: dict, wanted: dict, key: str = "gif_key") -> List[str]:
    """Kütüphaneye sonradan eklenen içeriklere göre eksik klasörleri ekler; var olanlara dokunmaz."""
    have = {f.get(key) for f in existing.get("folders", [])}
    added = [f for f in wanted["folders"] if f.get(key) not in have]
    if added:
        await _coll().update_one({"_id": existing["_id"]},
                                 {"$push": {"folders": {"$each": added}},
                                  "$set": {"updated_at": datetime.utcnow()}})
    return [f["title"] for f in added]


_TREND_FOLDERS: List[Tuple[str, str, str, str]] = [
    # (klasör adı, gif_key, liste, bölümler) — bölümler: "tv+movie" ya da tek tür
    ("Günlük Trend",  "trend-day",     "trend_day",   "tv+movie"),
    ("Haftalık Trend", "trend-week",   "trend_week",  "tv+movie"),
    ("Aylık Trend",   "trend-month",   "trend_month", "tv+movie"),
    ("Devam Eden Diziler", "trend-airing", "airing",  "tv"),
    ("Vizyondakiler", "trend-playing", "now_playing", "movie"),
]


def _trend_collection() -> dict:
    """TMDB Trend Listesi. Yapı sabittir (içerik canlı gelir), bu yüzden bölümler her zaman oluşturulur."""
    folders = []
    for title, gif_key, list_key, kinds in _TREND_FOLDERS:
        sections = []
        for kind in kinds.split("+"):
            if len(kinds.split("+")) == 2:
                sec_title = f"{title} {'Diziler' if kind == 'tv' else 'Filmler'}"
            else:
                sec_title = title
            sections.append({"id": _new_id(), "title": sec_title, "media": kind,
                             "rule": {"type": "tmdb_list", "list": list_key, "sort": "keep"}})
        folders.append({"id": _new_id(), "title": title, "emoji": "", "cover_url": "", "focus_gif_url": "",
                        "gif_key": gif_key, "tile_shape": "LANDSCAPE", "media": "all", "sections": sections})
    return {"name": "TMDB Trend Listesi",
            "description": "TMDB'de günlük/haftalık/aylık trend olan, devam eden ve vizyondaki yapımlar (yalnızca kütüphanenizdekiler)",
            "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": folders}


async def _director_folder(name: str) -> Tuple[Optional[dict], bool]:
    """Yönetmen klasörü: kapak = TMDB fotoğrafı; bölümler '<Soyad> Filmleri' / '<Soyad> Dizileri'.
    (klasör | None, fotoğraf_bulundu_mu) döner; TMDB'de yoksa ya da kütüphanede yapımı yoksa klasör None."""
    person = await tmdb_person(name, "Directing")
    if not person:
        return None, False
    rule = {"type": "director", "person_id": person["id"], "name": person["name"], "sort": "year"}
    short = person["name"].split()[-1]
    sections = []
    for media, noun in (("movie", "Filmleri"), ("tv", "Dizileri")):
        if await resolve_folder({"rule": rule, "media": media}):
            sections.append({"id": _new_id(), "title": f"{short} {noun}", "media": media, "rule": dict(rule)})
    if not sections:
        return None, True
    return ({"id": _new_id(), "title": person["name"], "emoji": "", "cover_url": person.get("profile_url", ""),
             "focus_gif_url": "", "gif_key": f"director-{_slug(person['name'])}", "tile_shape": "POSTER",
             "media": "all", "sections": sections}, True)


async def _remove_legacy_presets(prefix: str) -> List[str]:
    """Eski, dağınık hazır koleksiyonları (platform_netflix, actor_tomcruise, ...) kaldırır."""
    removed = []
    cursor = _coll().find({"preset": {"$regex": f"^{prefix}(_|$)"}})
    for col in await cursor.to_list(None):
        removed.append(col.get("name", ""))
        await _coll().delete_one({"_id": col["_id"]})
    return removed


async def _reorder_top() -> None:
    """Hazır 'Dijital Platformlar' ve 'Oyuncular' koleksiyonları listenin en üstünde yer alır."""
    rank = {PRESET_PLATFORMS: 0, PRESET_SERIES: 1, PRESET_COMPANIES: 2, PRESET_COUNTRIES: 3,
            PRESET_GENRES: 4, PRESET_THEMES: 5, PRESET_YEARS: 6, PRESET_TRENDS: 7, PRESET_ACTORS: 8, PRESET_DIRECTORS: 9}
    cols = await _coll().find({}).to_list(None)
    cols.sort(key=lambda c: (rank.get(c.get("preset"), 10), c.get("order", 0)))
    for idx, c in enumerate(cols):
        if c.get("order") != idx:
            await _coll().update_one({"_id": c["_id"]}, {"$set": {"order": idx}})


async def _resolve_titles(titles: List[Tuple[str, int]], kind: str) -> Tuple[List[str], List[str]]:
    """(başlık, yıl) listesini veritabanındaki imdb_id'lere çevirir. (bulunanlar, bulunamayanlar)"""
    coll_name = "movie" if kind == "movie" else "tv"
    found: List[str] = []
    missing: List[str] = []

    async def one(title: str, year: int) -> Optional[str]:
        want = _norm(title)
        rx = {"$regex": f"^{re.escape(title)}$", "$options": "i"}
        query = {"$or": [{"title": rx}, {"title_tr": rx}, {"title_de": rx}]}
        for st in _storages():
            for d in await st[coll_name].find(query, _LIGHT_PROJ | {"title_de": 1}).limit(10).to_list(None):
                names = {_norm(d.get(k)) for k in ("title", "title_tr", "title_de")}
                try:
                    y = int(d.get("release_year") or 0)
                except (TypeError, ValueError):
                    y = 0
                if want in names and d.get("imdb_id") and (not y or abs(y - year) <= 1):
                    return d["imdb_id"]
        return None

    results = await asyncio.gather(*(one(t, y) for t, y in titles))
    for (t, y), imdb in zip(titles, results):
        (found if imdb else missing).append(imdb or f"{t} ({y})")
    return list(dict.fromkeys(found)), missing


async def _marvel_preset() -> Tuple[dict, List[str]]:
    movie_ids, movie_missing = await _resolve_titles(_MCU_MOVIES, "movie")
    series_ids, series_missing = await _resolve_titles(_MCU_SERIES, "tv")
    folders = []
    if movie_ids:
        folders.append({"id": _new_id(), "title": "Marvel Sinemaları", "emoji": "🦸",
                        "cover_url": "", "focus_gif_url": "", "gif_key": "marvel",
                        "tile_shape": "LANDSCAPE", "media": "movie",
                        "rule": {"type": "titles", "titles": [list(t) for t in _MCU_MOVIES], "items": movie_ids}})
    if series_ids:
        folders.append({"id": _new_id(), "title": "Marvel Dizileri", "emoji": "📺",
                        "cover_url": "", "focus_gif_url": "", "gif_key": "marvel",
                        "tile_shape": "LANDSCAPE", "media": "tv",
                        "rule": {"type": "titles", "titles": [list(t) for t in _MCU_SERIES], "items": series_ids}})
    doc = {"name": "Marvel", "description": "Marvel Sinematik Evreni filmleri ve dizileri",
           "backdrop_url": "", "hide_from_home": True, "folders": folders}
    return doc, movie_missing + series_missing


@admin_router.post("/hazir/olustur")
async def create_presets(payload: dict, _: bool = Depends(require_auth)):
    """Hazır koleksiyonları oluşturur; işlem ortasında hata olsa bile koleksiyon sırası her zaman düzeltilir."""
    try:
        return await _create_presets_impl(payload)
    finally:
        try:
            await _reorder_top()
        except Exception as e:  # sıralama hatası asıl sonucu gölgelemesin
            _logger.warning("Koleksiyon sırası düzeltilemedi: %s", e)
        _RESOLVE_CACHE.clear()


async def _create_presets_impl(payload: dict) -> Dict[str, Any]:
    """Hazır koleksiyonları oluşturur: 'Dijital Platformlar' (tek koleksiyon) ve Marvel.

    payload: {"kinds": ["platform", "series", "companies", "countries", "genres", "themes", "years", "trends"], "force": false}   ("marvel": eski, elle seçilmiş Marvel listesi)
      force=False → zaten var olan koleksiyonlara dokunulmaz (platformlarda yalnızca eksik klasörler eklenir).
      force=True  → mevcut hazır koleksiyonun klasörleri baştan üretilir.
    Eski sürümün ürettiği dağınık koleksiyonlar (Netflix, Disney+ ... ayrı ayrı) otomatik kaldırılır.
    """
    kinds = payload.get("kinds") or ["platform", "series", "companies", "countries", "genres", "themes", "years", "trends"]
    force = bool(payload.get("force", False))
    report: Dict[str, Any] = {"created": [], "updated": [], "skipped": [], "removed": [],
                              "missing_titles": [], "empty": []}

    if "platform" in kinds:
        if not platform_catalog.is_loaded():
            raise HTTPException(status_code=409,
                                detail="Platform kataloğu henüz yüklenmedi; birkaç dakika sonra tekrar deneyin.")
        report["removed"] += await _remove_legacy_presets("platform")
        doc, empty = _platforms_collection()
        report["empty"] += empty
        if not doc["folders"]:
            report["empty"].append("Dijital Platformlar")
        else:
            existing = await _coll().find_one({"preset": PRESET_PLATFORMS})
            if existing and not force:
                added = await _merge_platform_folders(existing, doc)
                if added:
                    report["updated"].append("Dijital Platformlar (+ " + ", ".join(added) + ")")
                else:
                    report["skipped"].append("Dijital Platformlar")
            else:
                status = await _upsert_preset(PRESET_PLATFORMS, doc, force=True)
                report[status].append("Dijital Platformlar")

    if "companies" in kinds:
        if not Telegram.TMDB_API:
            raise HTTPException(status_code=409,
                                detail="Yapım şirketleri TMDB'den alınır; TMDB_API anahtarı tanımlı değil.")
        doc, empty = await _companies_collection()
        report["empty"] += empty
        if not doc["folders"]:
            report["empty"].append("Yapım Şirketleri (TMDB'ye ulaşılamadı veya eşleşen içerik yok)")
        else:
            report["removed"] += await _remove_legacy_presets("marvel")  # eski ayrı Marvel koleksiyonu
            existing = await _coll().find_one({"preset": PRESET_COMPANIES})
            if existing and not force:
                added = await _merge_company_folders(existing, doc)
                if added:
                    report["updated"].append("Yapım Şirketleri (+ " + ", ".join(added) + ")")
                else:
                    report["skipped"].append("Yapım Şirketleri")
            else:
                status = await _upsert_preset(PRESET_COMPANIES, doc, force=True)
                report[status].append(f"Yapım Şirketleri ({len(doc['folders'])} şirket)")

    if "countries" in kinds:
        doc, empty = await _countries_collection()
        report["empty"] += empty
        if not doc["folders"]:
            report["empty"].append("Ülkeler")
        else:
            existing = await _coll().find_one({"preset": PRESET_COUNTRIES})
            if existing and not force:
                added = await _merge_country_folders(existing, doc)
                if added:
                    report["updated"].append("Ülkeler (+ " + ", ".join(added) + ")")
                else:
                    report["skipped"].append("Ülkeler")
            else:
                status = await _upsert_preset(PRESET_COUNTRIES, doc, force=True)
                report[status].append(f"Ülkeler ({len(doc['folders'])} ülke)")

    if "trends" in kinds:
        if not Telegram.TMDB_API:
            raise HTTPException(status_code=409,
                                detail="TMDB Trend Listesi için TMDB_API anahtarı tanımlı olmalı.")
        doc = _trend_collection()
        existing = await _coll().find_one({"preset": PRESET_TRENDS})
        if existing and not force:
            added = await _merge_new_folders(existing, doc)
            if added:
                report["updated"].append("TMDB Trend Listesi (+ " + ", ".join(added) + ")")
            else:
                report["skipped"].append("TMDB Trend Listesi")
        else:
            status = await _upsert_preset(PRESET_TRENDS, doc, force=True)
            report[status].append("TMDB Trend Listesi")
        # Hangi listeler şu an kütüphanenizle eşleşiyor? (bilgi amaçlı)
        for f in doc["folders"]:
            n = 0
            for sec in f["sections"]:
                n += len(await resolve_folder(sec))
            if not n:
                report["empty"].append(f"{f['title']} (şu an kütüphanenizde eşleşen yok; içerik geldikçe dolar)")

    if "genres" in kinds:
        doc, empty = await _genres_collection()
        report["empty"] += empty
        if not doc["folders"]:
            report["empty"].append("Türler")
        else:
            existing = await _coll().find_one({"preset": PRESET_GENRES})
            if existing and not force:
                added = await _merge_new_folders(existing, doc)
                if added:
                    report["updated"].append("Türler (+ " + ", ".join(added) + ")")
                else:
                    report["skipped"].append("Türler")
            else:
                status = await _upsert_preset(PRESET_GENRES, doc, force=True)
                report[status].append(f"Türler ({len(doc['folders'])} tür)")

    if "years" in kinds:
        doc, empty = await _years_collection()
        report["empty"] += empty
        if not doc["folders"]:
            report["empty"].append("Yıllar")
        else:
            existing = await _coll().find_one({"preset": PRESET_YEARS})
            if existing and not force:
                added = await _merge_new_folders(existing, doc)
                resorted = await _sort_years_folders(existing)
                if added or resorted:
                    report["updated"].append("Yıllar" + (" (+ " + ", ".join(added) + ")" if added else " (sıra güncellendi)"))
                else:
                    report["skipped"].append("Yıllar")
            else:
                status = await _upsert_preset(PRESET_YEARS, doc, force=True)
                report[status].append(f"Yıllar ({len(doc['folders'])} dönem)")

    if "themes" in kinds:
        if not Telegram.TMDB_API:
            raise HTTPException(status_code=409,
                                detail="Temalar TMDB anahtar kelimelerinden alınır; TMDB_API anahtarı tanımlı değil.")
        doc, empty = await _themes_collection()
        report["empty"] += empty
        if not doc["folders"]:
            report["empty"].append("Temalar (TMDB'ye ulaşılamadı veya eşleşen içerik yok)")
        else:
            existing = await _coll().find_one({"preset": PRESET_THEMES})
            if existing and not force:
                added = await _merge_new_folders(existing, doc)
                if added:
                    report["updated"].append("Temalar (+ " + ", ".join(added) + ")")
                else:
                    report["skipped"].append("Temalar")
            else:
                status = await _upsert_preset(PRESET_THEMES, doc, force=True)
                report[status].append(f"Temalar ({len(doc['folders'])} tema)")

    if "series" in kinds:
        if not platform_catalog.is_loaded():
            raise HTTPException(status_code=409,
                                detail="Katalog henüz yüklenmedi; birkaç dakika sonra tekrar deneyin.")
        doc = await _series_collection()
        if not doc["folders"]:
            report["empty"].append("Seri Filmler")
        else:
            existing = await _coll().find_one({"preset": PRESET_SERIES})
            if existing and not force:
                added = await _merge_series_folders(existing, doc)
                if added:
                    report["updated"].append("Seri Filmler (+ " + ", ".join(added[:8])
                                             + (f" ve {len(added) - 8} seri daha" if len(added) > 8 else "") + ")")
                else:
                    report["skipped"].append("Seri Filmler")
            else:
                status = await _upsert_preset(PRESET_SERIES, doc, force=True)
                report[status].append(f"Seri Filmler ({len(doc['folders'])} seri)")

    if "marvel" in kinds:
        doc, missing = await _marvel_preset()
        if doc["folders"]:
            status = await _upsert_preset("marvel", doc, force)
            report[status].append("Marvel")
            report["missing_titles"] = missing
        else:
            report["empty"].append("Marvel")

    await _reorder_top()
    _RESOLVE_CACHE.clear()
    return report


async def _top_actors(min_titles: int, limit: int) -> List[Tuple[str, int]]:
    """Başrol kadrosu (ilk 5 oyuncu) içinde en çok geçen oyuncular."""
    pipeline = [
        {"$project": {"cast": {"$slice": [{"$ifNull": ["$cast", []]}, 5]}}},
        {"$unwind": "$cast"},
        {"$group": {"_id": "$cast", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": 1}}},
        {"$sort": {"n": -1}},
        {"$limit": 500},
    ]
    counts: Dict[str, int] = {}
    for st in _storages():
        for coll_name in ("movie", "tv"):
            try:
                async for row in st[coll_name].aggregate(pipeline, maxTimeMS=30000, allowDiskUse=True):
                    if row["_id"] and isinstance(row["_id"], str):
                        counts[row["_id"]] = counts.get(row["_id"], 0) + row["n"]
            except Exception as e:
                _logger.warning("top actors hata: %s", e)
    ranked = [(n, c) for n, c in counts.items() if c >= min_titles]
    ranked.sort(key=lambda kv: kv[1], reverse=True)
    return ranked[:limit]


async def _actor_folder(name: str) -> Tuple[Optional[dict], bool]:
    """Oyuncu klasörü: kapak = TMDB fotoğrafı, bölümler = '<Ad> Filmleri' / '<Ad> Dizileri'.

    (klasör | None, fotoğraf_bulundu_mu) döner. Kütüphanede hiç içeriği yoksa klasör None'dur.
    """
    items = await _actor_items(name)
    movies = sum(1 for i in items if i["media_type"] == "movie")
    series = sum(1 for i in items if i["media_type"] == "tv")
    sections = []
    if movies:
        sections.append({"id": _new_id(), "title": f"{name} Filmleri", "media": "movie",
                         "rule": {"type": "actor", "name": name, "sort": "year"}})
    if series:
        sections.append({"id": _new_id(), "title": f"{name} Dizileri", "media": "tv",
                         "rule": {"type": "actor", "name": name, "sort": "year"}})
    if not sections:
        return None, False
    person = await tmdb_person(name) if Telegram.TMDB_API else None
    key = "actor-" + (re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name).lower()
                              .encode("ascii", "ignore").decode()).strip("-") or "x")
    folder = {"id": _new_id(), "title": name, "emoji": "", "cover_url": (person or {}).get("profile_url", ""),
              "focus_gif_url": "", "gif_key": key, "tile_shape": "POSTER", "media": "all",
              "sections": sections}
    return folder, bool(person)


def _actor_names_in(col: dict) -> set:
    names = set()
    for f in col.get("folders", []):
        for sec in f.get("sections") or []:
            n = (sec.get("rule") or {}).get("name")
            if n:
                names.add(_norm(n))
    return names


@admin_router.post("/hazir/oyuncular")
async def create_actor_collections(payload: dict, _: bool = Depends(require_auth)):
    """'Oyuncular' koleksiyonuna oyuncu klasörleri ekler (koleksiyon yoksa oluşturulur, en üste sabitlenir).

    payload: {"names": ["Tom Cruise", ...]}            → yalnızca bu oyuncular
         veya {"auto": true, "min_titles": 5, "limit": 10} → veritabanında en çok geçen oyuncular
    Fotoğraflar TMDB'den alınır; bulunamayanlar için klasörde kapak görseli elle girilebilir.
    """
    names = [str(n).strip() for n in (payload.get("names") or []) if str(n).strip()]
    if payload.get("auto"):
        min_titles = max(1, min(int(payload.get("min_titles") or 5), 100))
        limit = max(1, min(int(payload.get("limit") or 10), 50))
        names += [n for n, _c in await _top_actors(min_titles, limit) if n not in names]
    if not names:
        raise HTTPException(status_code=400, detail="Oyuncu bulunamadı veya seçilmedi")

    report: Dict[str, List[str]] = {"created": [], "skipped": [], "empty": [], "no_photo": [], "removed": []}
    report["removed"] += await _remove_legacy_presets("actor")

    col = await _coll().find_one({"preset": PRESET_ACTORS})
    if not col:
        res = await _upsert_preset(PRESET_ACTORS, {
            "name": "Oyuncular", "description": "Oyuncuların filmleri ve dizileri",
            "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": [],
        }, force=True)
        col = await _coll().find_one({"preset": PRESET_ACTORS})
    known = _actor_names_in(col)

    new_folders = []
    for name in dict.fromkeys(names[:50]):
        if _norm(name) in known:
            report["skipped"].append(name)
            continue
        folder, has_photo = await _actor_folder(name)
        if not folder:
            report["empty"].append(name)
            continue
        new_folders.append(folder)
        report["created"].append(name)
        known.add(_norm(name))
        if not has_photo:
            report["no_photo"].append(name)

    if new_folders:
        await _coll().update_one({"_id": col["_id"]},
                                 {"$push": {"folders": {"$each": new_folders}},
                                  "$set": {"updated_at": datetime.utcnow()}})
    await _reorder_top()
    _RESOLVE_CACHE.clear()
    return report


@admin_router.post("/hazir/yonetmenler")
async def create_director_folders(payload: dict, _: bool = Depends(require_auth)):
    """'Yönetmenler' koleksiyonuna yönetmen klasörleri ekler (yoksa oluşturulur, sıralamada Oyuncular'ın altındadır).

    payload: {"names": ["Christopher Nolan", ...], "suggested": false}
      suggested=True → ünlü yönetmenlerden oluşan hazır liste de eklenir.
    Fotoğraf ve yönetmenlik kredileri TMDB'den alınır; kütüphanede yapımı olmayan yönetmen atlanır.
    """
    if not Telegram.TMDB_API:
        raise HTTPException(status_code=409, detail="Yönetmenler TMDB'den alınır; TMDB_API anahtarı tanımlı değil.")
    names = [str(n).strip() for n in (payload.get("names") or []) if str(n).strip()]
    if payload.get("suggested"):
        names += [n for n in SUGGESTED_DIRECTORS if n not in names]
    if not names:
        raise HTTPException(status_code=400, detail="Yönetmen seçilmedi")

    report: Dict[str, List[str]] = {"created": [], "skipped": [], "empty": [], "not_found": [], "no_photo": []}
    col = await _coll().find_one({"preset": PRESET_DIRECTORS})
    if not col:
        await _upsert_preset(PRESET_DIRECTORS, {
            "name": "Yönetmenler", "description": "Yönetmenlerin filmleri ve dizileri",
            "backdrop_url": "", "pin_to_top": True, "hide_from_home": True, "folders": [],
        }, force=True)
        col = await _coll().find_one({"preset": PRESET_DIRECTORS})
    known = {f.get("gif_key") for f in col.get("folders", [])}

    sem = asyncio.Semaphore(4)

    async def one(name: str):
        async with sem:
            return await _director_folder(name)

    wanted = list(dict.fromkeys(names[:60]))
    results = await asyncio.gather(*(one(n) for n in wanted))
    new_folders = []
    for name, (folder, found) in zip(wanted, results):
        if folder is None:
            report["empty" if found else "not_found"].append(name)
        elif folder["gif_key"] in known:
            report["skipped"].append(name)
        else:
            known.add(folder["gif_key"])
            new_folders.append(folder)
            report["created"].append(name)
            if not folder.get("cover_url"):
                report["no_photo"].append(name)
    if new_folders:
        await _coll().update_one({"_id": col["_id"]},
                                 {"$push": {"folders": {"$each": new_folders}},
                                  "$set": {"updated_at": datetime.utcnow()}})
    await _reorder_top()
    _RESOLVE_CACHE.clear()
    return report
