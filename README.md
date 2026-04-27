<p align="center">
  <img src="https://iili.io/KhN0ztj.png" alt="Logo" width="400"/>
</p>

<p align="center">
  <b>FastAPI</b>, <b>MongoDB</b> ve <b>PyroFork</b> ile geliştirilmiş; <b>Stremio</b> ile tam entegre, kendi sunucunda çalışan güçlü bir <b>Telegram Stremio Medya Sunucusu</b>.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/UV%20Package%20Manager-2B7A77?logo=uv&logoColor=white" alt="UV" />
  <img src="https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/MongoDB-47A248?logo=mongodb&logoColor=white" alt="MongoDB" />
  <img src="https://img.shields.io/badge/PyroFork-EE3A3A?logo=python&logoColor=white" alt="PyroFork" />
  <img src="https://img.shields.io/badge/Stremio-8D3DAF?logo=stremio&logoColor=white" alt="Stremio" />
  <img src="https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white" alt="Docker" />
</p>

---

## 🧭 İçindekiler

- [🚀 Giriş](#-giriş)
  - [✨ Temel Özellikler](#-temel-özellikler)
  - [🆕 Yeni Özellikler](#-yeni-özellikler)
- [⚙️ Nasıl Çalışır?](#️-nasıl-çalışır)
  - [Genel Bakış](#genel-bakış)
  - [Yükleme Kuralları](#yükleme-kuralları)
  - [Kalite Değiştirme Mantığı](#-kalite-değiştirme-mantığı)
  - [Hatalı Metadata Düzeltme](#️-hatalı-metadata-düzeltme)
  - [Arka Planda Neler Oluyor?](#arka-planda-neler-oluyor)
- [🤖 Bot Komutları](#-bot-komutları)
- [💳 Abonelik Sistemi](#-abonelik-sistemi)
  - [Abonelik Planları](#abonelik-planları)
  - [Bot Ödeme Akışı](#bot-ödeme-akışı)
  - [Erişim Yönetimi](#erişim-yönetimi)
  - [Stremio Eklenti Entegrasyonu](#stremio-eklenti-entegrasyonu)
- [🔧 Yapılandırma Rehberi](#-yapılandırma-rehberi)
  - [🌐 Proxy Ayarları](#-proxy-ayarları)
- [🚀 Kurulum Rehberi](#-kurulum-rehberi)
  - [VPS ile Docker Compose](#-vps-docker-compose-önerilen)
  - [Manuel Docker](#-manuel-docker)
  - [Alan Adı & HTTPS](#-alan-adı--https-zorunlu)
- [📺 Stremio Kurulumu](#-stremio-kurulumu)
  - [Eklenti Ekleme](#-eklentiyi-stremio-ya-ekle)
  - [Cinemeta Kaldırma](#️-cinemeta-kaldırma-isteğe-bağlı)

---

# 🚀 Giriş

Bu proje, **Telegram'daki dosyaları doğrudan Stremio üzerinden yayınlamanı** sağlayan yeni nesil bir medya sunucusudur. Üçüncü taraf bağımlılığı olmadan, dosya süre sınırı olmadan çalışır; hem bireysel hem de topluluk tabanlı medya barındırma için idealdir.

## ✨ Temel Özellikler

- ⚙️ **Çoklu MongoDB Desteği** — Birden fazla veritabanı ile yük dengeleme ve yedeklilik
- 📡 **Çoklu Kanal Desteği** — Birden fazla Telegram kanalından içerik çekme
- ⚡ **Hızlı Akış Deneyimi** — PyroFork tabanlı optimize edilmiş stream motoru
- 🔑 **Çoklu Token Yük Dengeleyici** — Birden fazla bot ile Telegram hız limitini aşma
- 🎬 **IMDb & TMDB Metadata Entegrasyonu** — Otomatik poster, başlık ve açıklama çekme
- ♾️ **Dosya Süresi Yok** — Telegram'da saklanan dosyalar sona ermez
- 🧠 **Yönetici Paneli** — Web tabanlı kontrol paneli ile tam yönetim
- 💳 **Abonelik Yönetimi** — Plan oluşturma, ödeme onayı, otomatik token üretimi ve süre takibi
- 🔐 **Erişim Yönetimi** — Abonelikleri görüntüle, uzat, kısalt, iptal et ve yeniden ata
- 📁 **Google Drive Entegrasyonu** — GDrive'dan içerik tarama ve onay sistemi
- 📢 **Toplu Duyuru** — Tüm aktif abonelere tek komutla mesaj gönderme
- ⚡ **Hız Testi** — Her dosya için tüm botlara hız testi yapılarak en iyi bağlantı seçilir.
- 🔄 **Geliştirilmiş Yük Dengeleyici** — Çoklu token trafiğini daha verimli dağıtan yeni algoritma.
- 🚫 **Başarısız Bot Yönetimi** — Hatalı botlar otomatik olarak beklemede/gölge moda alınır.
- 📊 **Bot Bazlı Analiz** — Yönetici panelinde her bot için ayrı performans istatistikleri.
- 🧹 **Silinen Dosya Tespiti** — Sunucu yeniden başlatıldığında silinen dosyalar otomatik tespit edilir.
- 🆓 **Ücretsiz Mod** — `SUBSCRIPTION=false` yapılarak tüm kullanıcılara otomatik token üretilir.
- 🔄 **Otomatik Stream Temizleme** — Telegram kanalındaki kaynak mesaj silindiğinde tüm stream kayıtları otomatik kaldırılır.
- 🏷️ **Manuel IMDb/TMDb Geçersiz Kılma** — Kanal mesaj başlığına IMDb/TMDB URL'si yapıştırılarak metadata anında güncellenir.
- 🛡️ **Brute-Force Koruması** — Başarısız giriş denemelerini izleyerek IP bazlı otomatik engelleme.
- 🔒 **Stream Token İmzalama** — HMAC ile imzalanmış güvenli stream token sistemi.
- 🌐 **Proxy Desteği** — Güvenilir proxy CIDR'ları ile X-Forwarded-For başlığı doğrulama.

## 🆕 Yeni Özellikler
- ☁️ **Rclone Entegrasyonu** — Rclone desteği ile bulut depolama alanlarına (Google Drive, S3, Dropbox vb.) dosya aktarımı ve senkronizasyonu sağlanır.
- 📡 **Sunucu Üzerinden Canlı Yayın** — Canlı yayınlar artık doğrudan sunucu üzerinden iletilerek daha stabil ve kesintisiz bir izleme deneyimi sunar.
- 🗓️ **Canlı Yayın Planlama** — Canlı yayınlar önceden belirli bir tarih ve saate planlanabilir; yayın otomatik olarak zamanında başlatılır.
- 📢 **Duyuru Canlı Yayını** — Yönetici panelindeki **Duyuru Yayını** modalı üzerinden doğrudan HLS canlı yayın oluşturulur ve Stremio kataloğuna eklenir. Yayına resim (slayt gösterisi) veya video (döngü oynatma) eklenebilir; arka plan müziği de eklenebilir. Dosya, URL veya sunucuda yüklü medyadan içerik seçilebilir. Kanal posteri, logosu ve sırası ayarlanabilir. Yayın başlatıldığında canlı önizleme panelde gösterilir, durdurulduğunda tüm geçici dosyalar otomatik silinir ve katalogdan kaldırılır.
- 🖼️ **Duyuru Medya Desteği** — Duyuru mesajlarına resim, video ve müzik eklenebilir; zengin içerikli bildirimler abone cihazlarına iletilir.
- 🎞️ **Media Edit'ten Altyazı Ekleme** — `media_edit.html` sayfası üzerinden film ve dizilere doğrudan altyazı dosyası yüklenip yönetilebilir.
- 🔔 **Dizi ve Film Bildirimleri** — Yeni bölüm veya film eklendiğinde ilgili abonelere otomatik Telegram bildirimi gönderilir.
- 📡 **Otomatik Dizi Durum Takibi** — Her dizinin yayın durumu (devam ediyor, sona erdi, iptal edildi vb.) her gün UTC+3 05:00'da TMDB'den çekilerek otomatik güncellenir.
- ✅ **M3U Link Kontrol Sistemi** — `/m3ukontrol` komutu ile birden fazla M3U linki aynı anda kontrol edilir; gerçek `.m3u` dosyası döndüren çalışan linkler filtrelenerek `.txt` dosyası olarak Telegram'a gönderilir. HTTP 200 durumu, `Content-Disposition`, `Content-Type` ve `#EXTM3U` başlığı üç ayrı koşulla doğrulanır.
- 📡 **Kanal Tarama Sistemi** — `/tara` komutu ile `AUTH_CHANNEL` kanalları baştan sona taranır; video ve arşiv dosyaları otomatik olarak veritabanına eklenir. `/tara db` ile mevcut kayıtlar korunarak yalnızca yeni içerikler eklenir. Tarama sırasında `/tara_durum` ile anlık istatistikler, `/tara_iptal` ile durdurma imkânı sunulur. Tarama sonunda atlanan ve hata veren kayıtlar `.txt` raporu olarak Telegram'a iletilir.
---

# ⚙️ Nasıl Çalışır?

Bu proje **Telegram depolama**, **FastAPI** ve **Stremio** arasında köprü kurarak film ve dizilerin doğrudan Telegram dosyalarından akışını sağlar.

## Genel Bakış

**AUTH_CHANNEL**'a Telegram dosyaları iletildiğinde bot otomatik olarak:

1. 🗃️ `message_id` ve `chat_id`'yi veritabanına kaydeder.
2. 🧠 Dosya başlığını işleyerek temel metadata'yı (başlık, yıl, kalite vb.) çıkarır.
3. 🌐 **PyroFork** modülü üzerinden **FastAPI** tarafından yönlendirilen bir stream URL'si oluşturur.
4. 🎞️ Stremio Eklenti API'lerini sunar:
   - `/catalog` → Mevcut medyaları listeler
   - `/meta` → Her öğe için ayrıntılı bilgi gösterir
   - `/stream` → Dosyayı Telegram üzerinden doğrudan akışa alır

## Yükleme Kuralları

Stremio ile sorunsuz entegrasyon için yüklenen dosyaların başlıklarında belirli bilgiler bulunmalıdır.

### 🎥 Filmler için

**Örnek Başlık:**
```
Ghosted 2023 720p 10bit WEBRip [Org Hindi AAC 2.0CH + English 6CH] x265 HEVC Msub ~ PSA.mkv
```

**Zorunlu Alanlar:**
- 🎞️ **Ad** — Film adı (örn. _Ghosted_)
- 📅 **Yıl** — Çıkış yılı (örn. _2023_)
- 📺 **Kalite** — Çözünürlük veya kalite etiketi (örn. _720p_, _1080p_, _2160p_)

### 📺 Diziler için

**Örnek Başlık:**
```
Harikatha.Sambhavami.Yuge.Yuge.S01E04.Dark.Hours.1080p.WEB-DL.DUAL.DDP5.1.Atmos.H.264-Spidey.mkv
```

**Zorunlu Alanlar:**
- 🎞️ **Ad** — Dizi adı (örn. _Harikatha Sambhavami Yuge Yuge_)
- 📆 **Sezon Numarası** — `S` ile başlayan iki haneli sayı (örn. `S01`)
- 🎬 **Bölüm Numarası** — `E` ile başlayan iki haneli sayı (örn. `E04`)
- 📺 **Kalite** — Çözünürlük veya kalite etiketi

## 🔁 Kalite Değiştirme Mantığı

Aynı kalite etiketiyle (`720p`, `1080p` gibi) birden fazla dosya yüklendiğinde **en son dosya otomatik olarak eskisinin yerini alır**.

> **Örnek:** Daha önce `Ghosted 2023 720p` yüklediysen ve yeni bir `720p` sürümü iletirsen bot eski dosyayı değiştirerek katalogu temiz tutar.

`REPLACE_MODE=false` yapıldığında aynı kalitede birden fazla kaynak tutulabilir.

## 🏷️ Hatalı Metadata Düzeltme

Eklenti bir filmi veya diziyi yanlış tanımladıysa ya da metadata tamamen eksikse:

1. Film/dizi için doğru **IMDb URL**'sini veya **TMDB URL**'sini kopyala.
2. Telegram **AUTH_CHANNEL**'ındaki mesaj başlığını düzenle ve URL'yi yapıştır.
3. Bot eski hatalı veritabanı kaydını silerek metadata'yı yeni linkten anında çeker.

## Arka Planda Neler Oluyor?

| Bileşen | Rol |
|:---|:---|
| **Telegram Bot** | Yüklemeleri, iletimleri ve dosya takibini yönetir |
| **MongoDB** | Mesaj ID'lerini, kanal ID'lerini ve metadata'yı saklar |
| **PyroFork** | Telegram tabanlı stream URL'leri üretir |
| **FastAPI** | Stream, katalog ve metadata için REST endpoint'leri barındırır |
| **Stremio Eklentisi** | Katalog görüntüleme ve oynatma için FastAPI endpoint'lerini tüketir |

```
Telegram ➜ MongoDB ➜ FastAPI ➜ Stremio ➜ Kullanıcı
```

---

# 🤖 Bot Komutları

## Kullanıcı Komutları

| Komut | Açıklama |
|:---|:---|
| `/start` | Stremio Eklenti URL'sini gösterir / Abonelik menüsünü açar |
| `/abonelik` | Abonelik bitiş tarihini gösterir |
| `/help` | Mevcut komutları listeler |

## Yönetici (Owner) Komutları

| Komut | Açıklama |
|:---|:---|
| `/ayarlar` | Bot ayarlarını Telegram üzerinden yönetir (toggle, stremio, erişim, abonelik, sistem, kuyruk, güvenlik sayfaları) |
| `/set <imdb-url>` | Sonraki yüklenen dosyayı belirtilen IMDb kaydına bağlar |
| `/log` | En son log dosyasını gönderir |
| `/restart` | Botu yeniden başlatır ve upstream repodan güncelleme çeker |
| `/duyuru` | Tüm aktif abonelere toplu mesaj gönderir |
| `/ekle` | Google Drive'dan içerik tarar ve onay sistemini başlatır |
| `/engelkaldir` | Kullanıcının engelini kaldırır |
| `/istatistik` | Veritabanı ve bot istatistiklerini gösterir |
| `/vindir` | Veritabanı koleksiyonlarını JSON olarak indirir |
| `/aynivideolarisil` | Yinelenen video kayıtlarını temizler |
| `/katalogyenile` | Stremio kataloğunu yeniden oluşturur |
| `/linklerisil` | Geçersiz linkleri veritabanından temizler |
| `/calismayanlinklerisil` | Çalışmayan stream linklerini siler |
| `/eskiverileriyenile` | Eski format veritabanı kayıtlarını günceller |
| `/sunucuyayukle` / `/s` | URL veya dosyayı sunucuya yükler |
| `/sunucudansil` | Sunucudan yüklenen içeriği siler |
| `/durdur` | Devam eden bir işlemi durdurur |
| `/iptal` | Kuyruktaki bir görevi iptal eder |
| `/depolama` | TS yazılımı, sistem ve Docker disk/RAM kullanımını detaylı olarak gösterir (RAM durumu, genel disk, TS'nin yazdığı konumlar, Docker depolama, silinmiş-ama-açık dosyalar ve 100MB+ büyük dosyalar) |
| `/temizle` | Sistem temizliği yapar: MongoDB eski stream kayıtları, uygulama RAM cache'leri, /tmp dizini, pip/uv cache, log dosyası, Docker dangling objeleri ve journald logları temizlenir |
| `/ramraporu` | Sistem ve process bazlı RAM kullanımını gösterir; en çok RAM kullanan 15 process ve botun kendi RSS/VMS değerleri raporlanır |
| `/m3ukontrol` | Verilen M3U linklerini eş zamanlı olarak kontrol eder; gerçek `.m3u` dosyası döndüren çalışan linkleri filtreler ve sonuçları `.txt` dosyası olarak gönderir |
| `/tara` | Tüm DB'yi silerek `AUTH_CHANNEL` kanallarını baştan tarar; video ve arşiv dosyalarını veritabanına ekler. `/tara db` ile mevcut kayıtlar korunarak yalnızca yeni içerikler eklenir |
| `/tara_durum` | Devam eden taramanın anlık istatistiklerini gösterir (işlenen, eklenen, atlanan, hata sayısı) |
| `/tara_iptal` | Devam eden taramayı durdurur |

### `/set` Komutu Kullanımı

```
/set https://m.imdb.com/title/tt665723
```

1. `/set` komutunu IMDb URL'siyle birlikte gönder.
2. İlgili film veya dizi dosyalarını kanala ilet.
3. İşlem bittikten sonra sadece `/set` yazarak bağlantıyı temizle.

---

# 💳 Abonelik Sistemi

`SUBSCRIPTION=true` yapıldığında aktif aboneliği olmayan kullanıcılar Stremio'da stream göremez; yenileme linkine yönlendirilir.

## Abonelik Planları

Yönetici panelinden (**Abonelik Yönetimi** sayfası) oluşturulur. Her planın:
- **Adı** (örn. Aylık, Üç Aylık)
- **Süresi** (gün cinsinden)
- **Fiyatı** (görüntüleme amaçlı)
- **Açıklaması**

vardır. Planlar MongoDB'de saklanır; bot yeniden başlatılmadan düzenlenebilir.

## Bot Ödeme Akışı

```
Kullanıcı → /start → Plan seçer → Ödeme ekran görüntüsü gönderir
         → Onaylayıcı bildirim alır → Onayla / Reddet
         → Onayda:
             ✅ Abonelik DB'ye kaydedilir
             🔑 Stremio eklenti token'ı otomatik oluşturulur
             📨 Kullanıcı Stremio kurulum linki + grup davet linki alır
```

**Onaylayıcı Butonları** (`APPROVER_IDS` listesindeki adminler):

| Buton | İşlem |
|:---|:---|
| ✅ Onayla | Aboneliği aktif eder, token oluşturur, gruba davet gönderir |
| ❌ Reddet | Kullanıcıya red mesajı gönderir |

## Erişim Yönetimi

Yönetici panelinde **Erişim Yönetimi** sayfasında tüm kullanıcılar ve token'lar tam kontrol altında tutulur.

| Sütun | Açıklama |
|:---|:---|
| Durum | 🟢 Aktif / 🔴 Süresi Dolmuş |
| Kullanıcı | Görünen ad veya `User {id}` |
| Eklenti Linki | Stremio kurulum URL'si + kopyala butonu |
| Oluşturulma | Token oluşturma tarihi |
| Bitiş | Abonelik bitiş tarihi |

| Buton | Açıklama |
|:---|:---|
| 📅 **Ata** | Abonelik planı ata veya uzat |
| ➕ **Uzat** | Aktif aboneliğe gün ekle |
| ➖ **Kısalt** | Aktif abonelikten gün çıkar |
| 🚫 **İptal** | Aboneliği tamamen sıfırla |
| 🗑️ **Token Sil** | Yalnızca eklenti token'ını sil |
| 🔗 **Kullanıcı ID Bağla** | Eski/orphan token'ı Telegram ID'sine bağla |

## Stremio Eklenti Entegrasyonu

Her kullanıcı ödeme onayında otomatik oluşturulan benzersiz bir token alır:

```
https://your-domain.com/stremio/{token}/manifest.json
```

**Dinamik Manifest:**

| Durum | Eklenti Adı |
|:---|:---|
| Aktif, bitiş tarihli | `Telegram — Bitiş: 28 Mar 2026` |
| Aktif, süresiz | `Telegram — Aktif` |
| Abonelik kapalı | `Telegram` |

**Süresi Dolmuş Stream Yanıtı:**

```json
{
  "name": "🚫 Abonelik Süresi Doldu",
  "title": "Aboneliğiniz sona erdi.\nDevam etmek için botu yenileyin.",
  "url": "https://t.me/your_bot"
}
```

**Yapılandırma Sayfası:**

```
https://your-domain.com/stremio/{token}/configure
```

Bu sayfa kullanıcıya abonelik durumunu, bitiş tarihini ve eklenti kurulum adımlarını gösterir.

---

# 🔧 Yapılandırma Rehberi

Tüm ayarlar `config.env` dosyasında tanımlanır.

## Telegram API

| Değişken | Açıklama |
|:---|:---|
| `API_ID` | [my.telegram.org](https://my.telegram.org) adresinden alınan Telegram API ID |
| `API_HASH` | Telegram API Hash |
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather)'dan alınan ana bot token'ı |
| `HELPER_BOT_TOKEN` | Yardımcı bot token'ı — stream yükünü paylaşır (isteğe bağlı) |
| `OWNER_ID` | Bot sahibinin Telegram kullanıcı ID'si (tam yönetim yetkisi) |

## Sunucu & Ağ

| Değişken | Açıklama | Varsayılan |
|:---|:---|:---|
| `BASE_URL` | Dışarıdan erişilebilir tam URL — sonda `/` olmadan (örn. `https://example.com`) | — |
| `PORT` | FastAPI sunucusunun dinleyeceği port | `8000` |

## Performans

| Değişken | Açıklama | Varsayılan |
|:---|:---|:---|
| `PARALLEL` | Paralel Telegram bağlantısı sayısı. Yüksek değer hızı artırır, API yükünü de artırır | `1` |
| `PRE_FETCH` | Önceden yüklenecek stream bloğu sayısı. Yüksek değer oynatmayı pürüzsüzleştirir | `1` |
| `MAX_CONCURRENT_DOWNLOADS` | Eş zamanlı indirme limiti (Telegram/URL/GDrive). Boş = sınırsız | — |
| `MAX_CONCURRENT_UPLOADS` | Eş zamanlı yükleme (DB kayıt + metadata) limiti | `1` |

## Kanal & Veritabanı

| Değişken | Açıklama |
|:---|:---|
| `AUTH_CHANNEL` | Botun dosya okuyacağı kanal ID'leri — virgülle ayrılır (örn. `-1001234567890,-1009876543210`) |
| `DATABASE` | MongoDB bağlantı URI'leri — virgülle ayrılır. Yük dengeleme için en az iki veritabanı önerilir |

## TMDB & Güncelleme

| Değişken | Açıklama |
|:---|:---|
| `TMDB_API` | [themoviedb.org](https://www.themoviedb.org/settings/api) adresinden alınan TMDB API anahtarı |
| `UPSTREAM_REPO` | Otomatik güncelleme için kaynak GitHub repo URL'si |
| `UPSTREAM_BRANCH` | Takip edilecek branch adı |

## Davranış Ayarları

| Değişken | Açıklama | Varsayılan |
|:---|:---|:---|
| `REPLACE_MODE` | `true` → aynı kalitedeki dosyaların üzerine yazar; `false` → birden fazla kaynak tutar | `true` |
| `HIDE_CATALOG` | `true` → Stremio katalog listesini gizler (Cinemeta zorunlu olur) | `false` |

## Güvenlik & Oturum

| Değişken | Açıklama |
|:---|:---|
| `SESSION_SECRET_KEY` | Web arayüzü oturum şifreleme anahtarı (uzun ve rastgele bir string önerilir) |
| `TOKEN_HMAC_SECRET` | Stream token imzalama anahtarı. Boş bırakılırsa `SESSION_SECRET_KEY` kullanılır |
| `TRUSTED_PROXY_CIDRS` | X-Forwarded-For başlığına güvenilecek proxy IP aralıkları. Boş = başlığa güvenilmez (aşağıya bak) |

### 🌐 `TRUSTED_PROXY_CIDRS` Nasıl Doldurulur?

Bu ayar, brute-force korumasının **gerçek kullanıcı IP adresini** doğru tespit edebilmesi için gereklidir.

**Sorun:** Uygulamanın önünde Nginx, Cloudflare veya başka bir reverse proxy varsa, sunucuya gelen bağlantının direkt IP'si kullanıcıya değil proxy'ye aittir. Gerçek IP ise `X-Forwarded-For` HTTP başlığında gelir. Ancak bu başlığa körü körüne güvenilirse, bir saldırgan sahte `X-Forwarded-For` değeri göndererek ban sistemini atlayabilir.

**Çözüm:** Yalnızca tanımlı güvenilir proxy IP aralıklarından gelen isteklerde bu başlık dikkate alınır.

> Değer olarak virgülle ayrılmış **CIDR notasyonlu** IP aralıkları girilir. Birden fazla aralık için aralarına boşluk bırakma.

#### Senaryo 1 — Reverse proxy yok (uygulama direkt internete açık)

```env
TRUSTED_PROXY_CIDRS=
```

Boş bırak. Varsayılan davranış budur; `X-Forwarded-For` başlığına hiç güvenilmez.

---

#### Senaryo 2 — Aynı sunucuda Nginx / Caddy var

```env
TRUSTED_PROXY_CIDRS=127.0.0.1/32
```

Uygulama ve proxy aynı makinede olduğundan localhost güvenilir proxy olarak eklenir.

---

#### Senaryo 3 — Ayrı bir sunucuda Nginx / HAProxy var

Proxy sunucusunun iç ağ IP'sini yaz:

```env
TRUSTED_PROXY_CIDRS=192.168.1.10/32
```

---

#### Senaryo 4 — Cloudflare kullanıyorsun

Cloudflare'nin tüm IP aralıklarını ekle:

```env
TRUSTED_PROXY_CIDRS=103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,104.16.0.0/13,104.24.0.0/14,108.162.192.0/18,131.0.72.0/22,141.101.64.0/18,162.158.0.0/15,172.64.0.0/13,173.245.48.0/20,188.114.96.0/20,190.93.240.0/20,197.234.240.0/22,198.41.128.0/17
```

> 💡 Güncel liste için: [https://www.cloudflare.com/ips/](https://www.cloudflare.com/ips/)

---

#### Senaryo 5 — Docker iç ağı

```env
TRUSTED_PROXY_CIDRS=172.16.0.0/12
```

Docker'ın varsayılan köprü ağı bu aralıkta çalışır. Compose ile aynı stack içindeyse bu yeterlidir.

---

#### Senaryo 6 — Birden fazla proxy / karma ortam

Virgülle ayırarak birleştirebilirsin:

```env
TRUSTED_PROXY_CIDRS=127.0.0.1/32,10.0.0.0/8,172.16.0.0/12
```

| Kurulum | Değer |
|:---|:---|
| Proxy yok | *(boş)* |
| Localhost Nginx/Caddy | `127.0.0.1/32` |
| Ayrı sunucu proxy | `<proxy-sunucu-ip>/32` |
| Cloudflare | Cloudflare IP listesi |
| Docker iç ağı | `172.16.0.0/12` |
| Karma ortam | Virgülle birleştir |

### 🔑 Güvenli Key Üretimi (PowerShell)

`SESSION_SECRET_KEY` ve `TOKEN_HMAC_SECRET` için kriptografik olarak güvenli rastgele değerler üretmek üzere aşağıdaki PowerShell komutlarından birini kullan:

**Yöntem 1 — Her iki key'i masaüstündeki dosyaya yaz:**

```powershell
$rng = New-Object Security.Cryptography.RNGCryptoServiceProvider

$bytes1 = New-Object byte[] 64
$rng.GetBytes($bytes1)
$sessionKey = [System.BitConverter]::ToString($bytes1).Replace('-','').ToLower()

$bytes2 = New-Object byte[] 64
$rng.GetBytes($bytes2)
$hmacKey = [System.BitConverter]::ToString($bytes2).Replace('-','').ToLower()

"SESSION_SECRET_KEY=`"$sessionKey`"" | Out-File "$env:USERPROFILE\Desktop\anahtar.txt"
"TOKEN_HMAC_SECRET=`"$hmacKey`"" | Add-Content "$env:USERPROFILE\Desktop\anahtar.txt"
```

**Yöntem 2 — Terminale yazdır (hızlı kopyala-yapıştır):**

```powershell
$rng = New-Object Security.Cryptography.RNGCryptoServiceProvider

$b = New-Object byte[] 64; $rng.GetBytes($b)
Write-Host "SESSION_SECRET_KEY=" ([System.BitConverter]::ToString($b).Replace('-','').ToLower())

$b = New-Object byte[] 64; $rng.GetBytes($b)
Write-Host "TOKEN_HMAC_SECRET=" ([System.BitConverter]::ToString($b).Replace('-','').ToLower())
```

**Yöntem 3 — `config.env` dosyasına otomatik yaz:**

```powershell
$rng = New-Object Security.Cryptography.RNGCryptoServiceProvider

$b1 = New-Object byte[] 64; $rng.GetBytes($b1)
$b2 = New-Object byte[] 64; $rng.GetBytes($b2)

$s = [System.BitConverter]::ToString($b1).Replace('-','').ToLower()
$h = [System.BitConverter]::ToString($b2).Replace('-','').ToLower()

(Get-Content config.env) `
  -replace 'SESSION_SECRET_KEY=""', "SESSION_SECRET_KEY=`"$s`"" `
  -replace 'TOKEN_HMAC_SECRET=""',  "TOKEN_HMAC_SECRET=`"$h`""  `
| Set-Content config.env
```

> ⚠️ Üretilen key'leri `.gitignore`'a eklenmiş `config.env` dışında hiçbir yere commit etme.

## Hız & Limit Ayarları

| Değişken | Açıklama | Varsayılan |
|:---|:---|:---|
| `YENILEME` | Stream token geçerlilik süresi (saat). Video izleme ve indirme için geçerli | `6 saat` |
| `HIZ_LIMITI` | Global hız limiti (Mbit/s). Boş = sınırsız. Örn: `50` → 50 Mbit/s | — |
| `LIMIT_SIFIRLAMA` | Günlük kullanım limitinin sıfırlanacağı UTC saati — `SS:DD` formatı. Örn: `06:00` | `00:00 UTC` |

## Brute-Force Koruması

| Değişken | Açıklama | Varsayılan |
|:---|:---|:---|
| `BRUTE_WINDOW` | Kaç saniye içindeki başarısız girişler sayılsın? | `60` |
| `BRUTE_MAX` | Pencere içinde kaç başarısız girişten sonra IP engellensin? | `10` |
| `BRUTE_BAN` | IP kaç saniye boyunca engellensin? | `600` (10 dk) |

## 🌐 Proxy Ayarları

Bazı ülkelerde Telegram'a doğrudan erişim kısıtlanmış olabilir. Bu durumda stream trafiğini bir proxy üzerinden yönlendirmek için aşağıdaki ayarları kullanabilirsin.

| Değişken | Açıklama | Varsayılan |
|:---|:---|:---|
| `Proxy` | `true` → proxy sistemi aktif; `false` → devre dışı | `false` |
| `ProxyType` | Proxy protokolü: `HTTP` veya `HTTPS` | `HTTPS` |
| `HTTP_Proxy_URL` | Proxy URL'si — sonda `?url=` ile bitmeli (örn. `https://PROXYURL/?url=`) | — |
| `PROXY_MODE` | `1` → Sadece normal (proxy yok) · `2` → Proxy + Normal (ikisi birden) · `3` → Sadece proxy | `1` |

### 🔧 Ücretsiz Proxy Oluşturma (Cloudflare Workers)

Cloudflare Workers üzerinde ücretsiz bir proxy worker kurabilirsin:

**1️⃣** [https://dash.cloudflare.com/](https://dash.cloudflare.com/) adresine git ve hesabına giriş yap.

**2️⃣** Sol menüden **Workers & Pages** → **Create** → **Worker** seç.

**3️⃣** Worker'a bir isim ver (örn. `proxy`) ve **Deploy** butonuna bas.

**4️⃣** **Edit Code** butonuna tıkla, açılan editördeki tüm kodu sil ve aşağıdakini yapıştır:

```js
export default {
  async fetch(request) {
    try {
      const url = new URL(request.url);

      // Get target URL (everything after "/")
      let target = url.pathname.slice(1);

      // If using ?url= format
      if (!target && url.searchParams.get("url")) {
        target = url.searchParams.get("url");
      }

      if (!target || !target.startsWith("http")) {
        return new Response("Invalid URL", { status: 400 });
      }

      // Forward headers (important for streaming)
      const headers = new Headers(request.headers);
      headers.set("Host", new URL(target).host);

      const response = await fetch(target, {
        method: request.method,
        headers: headers,
        redirect: "follow"
      });

      // Copy response headers
      const newHeaders = new Headers(response.headers);

      // Allow streaming + CORS
      newHeaders.set("Access-Control-Allow-Origin", "*");
      newHeaders.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
      newHeaders.set("Access-Control-Allow-Headers", "*");

      return new Response(response.body, {
        status: response.status,
        headers: newHeaders
      });

    } catch (err) {
      return new Response("Proxy Error: " + err.message, { status: 500 });
    }
  }
};
```

**5️⃣** Sağ üstteki **Deploy** butonuna bas.

**6️⃣** Worker URL'n otomatik oluşturulur (örn. `https://proxy.kullanici.workers.dev`). Bu URL'yi `config.env`'e şu şekilde ekle:

```env
Proxy=true
ProxyType=HTTPS
HTTP_Proxy_URL="https://proxy.kullanici.workers.dev/?url="
PROXY_MODE=2
```

> 💡 `PROXY_MODE=2` ile sistem önce proxy'yi, başarısız olursa normal bağlantıyı dener. Sadece proxy üzerinden gitmesini istiyorsan `PROXY_MODE=3` kullan.

## Abonelik Sistemi

| Değişken | Açıklama | Varsayılan |
|:---|:---|:---|
| `SUBSCRIPTION` | `true` → abonelik sistemi aktif; `false` → herkese otomatik token üretilir | `false` |
| `SUBSCRIPTION_URL` | Süresi dolmuş kullanıcılara gösterilecek Telegram bot/kanal URL'si | `https://t.me/` |
| `APPROVER_IDS` | Ödeme onaylayıcı admin Telegram ID'leri — virgülle ayrılır | — |
| `WEBSITESI` | `false` → bakım modu (abonelere giriş bilgisi gönderilmez) | `false` |

## Stremio Eklenti Kimliği

| Değişken | Açıklama | Varsayılan |
|:---|:---|:---|
| `ISIM` | Eklentinin Stremio'da görünen adı | `KARTAL` |
| `EKLENTI_ACIKLAMASI` | Stremio'da gösterilen eklenti açıklaması | `Dizi ve film arşivi.` |
| `EKLENTI_LOGOSU` | Eklenti logo URL'si | — |
| `BOLUM_RESIMI` | Bölüm/episode için varsayılan görsel URL'si | — |

## 🤖 Ek CDN Botları (Çoklu Token Sistemi)

| Değişken | Açıklama |
|:---|:---|
| `MULTI_TOKEN1` | 1. yardımcı bot token'ı |
| `MULTI_TOKEN2` | 2. yardımcı bot token'ı |
| `MULTI_TOKEN3` | 3. yardımcı bot token'ı |
| `MULTI_TOKEN4`, `MULTI_TOKEN5`, ... | İstediğin kadar eklenebilir |

Sistem, `MULTI_TOKEN` ile başlayan tüm ortam değişkenlerini sırayla otomatik olarak okur — sayı sınırı yoktur.

### Neden Çoklu Token?

Bot yüksek sayıda eş zamanlı istek aldığında Telegram, ana botu hız sınırına tabi tutabilir. Bunu önlemek için:

1. [@BotFather](https://t.me/BotFather) üzerinden ek botlar oluştur.
2. Her botu **AUTH_CHANNEL**'ına **Admin** olarak ekle.
3. Token'ları `config.env`'e `MULTI_TOKEN1`, `MULTI_TOKEN2`, `MULTI_TOKEN3`... şeklinde ekle.

Sistem tüm botlar arasında yükü otomatik dengeler; her bot bağlandığında hangi Telegram veri merkezine (DC) düştüğü loglanır.

---

# 🚀 Kurulum Rehberi

## Gereksinimler

- 🟢 Public IP'li bir **VPS** (Ubuntu önerilir — DigitalOcean, AWS, Vultr vb.)
- 🌐 Bir **alan adı**
- 🐳 **Docker** ve **Docker Compose**

## 1️⃣ Projeyi Klonla & Yapılandır

```bash
git clone https://github.com/kartal788/ts
cd ts
mv sample_config.env config.env
nano config.env
```

Tüm zorunlu değişkenleri doldurup kaydet (`Ctrl+O`, `Enter`, `Ctrl+X`).


Sunucu şu adreste çalışır: `http://<vps-ip>:8001`

**`config.env` güncellemesi için:**

```bash
nano config.env          # değişiklikleri yap
docker compose restart   # yeniden başlat (imaj yeniden oluşturulmaz)
```

## 🔵 Manuel Docker

```bash
docker build -t telegram-stremio .
docker run -d -p 8001:8001 telegram-stremio
```

> ⚠️ Sunucunun dinleyeceği port `config.env`'deki `PORT` değişkeninden okunur (varsayılan: `8001`).

Sunucu şu adreste çalışır: `http://<vps-ip>:<PORT>`

## 🌐 Alan Adı & HTTPS (Zorunlu)

### DNS Kaydı

Domain kayıt sağlayıcında A kaydı ekle:

| Tür | Ad | Değer |
|---|---|---|
| A | @ | `<VPS IP>` |

### Caddy ile HTTPS Kurulumu

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

```bash
sudo nano /etc/caddy/Caddyfile
```

```caddy
your-domain.com {
    reverse_proxy localhost:8001
}
```

```bash
sudo systemctl reload caddy
```

Sunucu artık şu adreste güvenli çalışır: `https://your-domain.com`

---

# 📺 Stremio Kurulumu

### 1️⃣ Stremio'yu İndir

👉 [https://www.stremio.com/downloads](https://www.stremio.com/downloads)

### 2️⃣ Giriş Yap

Stremio hesabın ile giriş yap veya yeni hesap oluştur.

### 3️⃣ Eklentiyi Stremio'ya Ekle

1. Stremio'da **Eklentiler** bölümüne git (🧩 simgesi).
2. Arama çubuğuna eklenti URL'sini yapıştır:

```
https://<your-domain>/stremio/{token}/manifest.json
```

---

## ⚙️ Cinemeta Kaldırma (İsteğe Bağlı)

Yalnızca kendi eklentini kullanmak istiyorsan Cinemeta'yı kaldırabilirsin.

### 1. Adım

Stremio'da diğer tüm eklentileri kaldır. Cinemeta'yı kaldırmaya çalış — engel çıkarsa 2. adıma geç.

### 2. Adım

**Chrome** ile [https://web.stremio.com/](https://web.stremio.com/) adresine giriş yap. Tarayıcı konsolunu aç (`Ctrl+Shift+J`) ve aşağıdaki kodu yapıştır:

```js
(function() {
    const token = JSON.parse(localStorage.getItem("profile")).auth.key;
    const requestData = { type: "AddonCollectionGet", authKey: token, update: true };

    fetch('https://api.strem.io/api/addonCollectionGet', {
        method: 'POST', body: JSON.stringify(requestData)
    })
    .then(r => r.json())
    .then(data => {
        if (data && data.result) {
            let result = JSON.stringify(data.result).substring(1)
                .replace(/\"protected\":true/g, '"protected":false')
                .replace('"idPrefixes":["tmdb:"]', '"idPrefixes":["tmdb:","tt"]');
            const index = result.indexOf("}}],");
            if (index !== -1) result = result.substring(0, index + 3) + "}";
            let addons = '{"type":"AddonCollectionSet","authKey":"' + token + '",' + result;
            fetch('https://api.strem.io/api/addonCollectionSet', {
                method: 'POST', body: addons
            })
            .then(r => r.text())
            .then(d => console.log('Başarılı:', d))
            .catch(e => console.error('Hata:', e));
        }
    });
})();
```

### 3. Adım

Konsolda şu mesajı görene kadar bekle:

```
Başarılı: {"result":{"success":true}}
```

Sayfayı yenile (**F5**). Artık Cinemeta'yı eklentiler listesinden kaldırabilirsin.
