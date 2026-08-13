from pyrogram import Client, filters, enums
from pyrogram.types import Message
from Backend.helper.custom_filter import CustomFilters

# ─── /komutlar — Sadece yönetici (owner) kullanabilir ─────────────────────────
# Bottaki yönetici komutlarının tamamını tek bir mesajda listeler.
#
# Not: Komutlar burada DÜZ METİN olarak yazılır (kod bloğu <code> KULLANILMAZ).
# Telegram, "/" ile başlayan düz metinleri otomatik olarak tıklanabilir komut
# olarak algılar; bu sayede kullanıcı komuta dokunduğunda metin kopyalanmaz,
# doğrudan o komut bota mesaj olarak gönderilir. <code> içine alınırsa bu
# davranış bozulur ve dokunma yalnızca metni kopyalar.

ADMIN_COMMANDS = [
    ("/ayarlar",      None,             "Bot ayarlarını Telegram üzerinden yönetir."),
    ("/set",          "<imdb-url>",     "Sonraki yüklenen dosyayı belirtilen IMDb/TMDB kaydına bağlar."),
    ("/log",          None,             "En son log dosyasını gönderir."),
    ("/restart",      None,             "Botu yeniden başlatır."),
    ("/duyuru",       None,             "Tüm üyelere toplu mesaj gönderir."),
    ("/ekle",         None,             "Google Drive'dan içerik tarar ve onay sistemini başlatır."),
    ("/engelkaldir",  None,             "Engellenmiş bir kullanıcının yasağını kaldırır."),
    ("/plan",         None,             "Abonelik planları ekranında gösterilecek resmi ayarlar veya kaldırır; resimle birlikte gönderilirse direkt kaydeder."),
    ("/plan2",        None,             "/yukselt ekranında gösterilecek resmi ayarlar veya kaldırır."),
    ("/s",            None,             "URL veya dosyayı sunucuya yükler."),
    ("/sunucudansil", None,             "Sunucuya yüklenmiş bir içeriği siler."),
    ("/tara",         None,             "Tüm DB'yi silerek AUTH_CHANNEL kanallarını baştan tarar; video ve arşiv dosyalarını veritabanına ekler."),
    ("/m3ukontrol",   None,             "Verilen M3U linklerini eş zamanlı kontrol eder; gerçek .m3u döndüren çalışan linkleri filtreler ve sonuçları .txt olarak gönderir."),
]


def _build_komutlar_text() -> str:
    lines = ["<b>🛠 Yönetici Komutları</b>\n"]
    for cmd, arg_hint, desc in ADMIN_COMMANDS:
        # Komut kendisi düz metin bırakılır (tıklanınca doğrudan gönderilsin);
        # varsa argüman ipucu ayrı ve italik olarak yanına eklenir, komutun
        # kendisine dokunulduğunda yalnızca "/xxx" kısmı mesaj olarak gider.
        header = f"{cmd} <i>{arg_hint}</i>" if arg_hint else cmd
        lines.append(f"{header}\n{desc}\n")
    return "\n".join(lines).strip()


@Client.on_message(filters.command("komutlar") & filters.private & CustomFilters.owner)
async def komutlar_command(client: Client, message: Message):
    await message.reply_text(
        _build_komutlar_text(),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
        quote=True,
    )

