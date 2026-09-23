"""
mediainfo_bot.py
─────────────────
Yönetici (owner) botla özel mesajda (DM) bir video/dosya gönderdiğinde YA DA
doğrudan bir video/ses linki paylaştığında, dosyanın/bağlantının TAMAMINI
İNDİRMEDEN yalnızca ilk birkaç MB'lık kısmını indirir, bu kısmı MediaInfo
(varsa) veya ffprobe (yedek) ile analiz eder ve sonucu düz bir METİN MESAJI
olarak (dosya eklemeden) geri gönderir.

Kullanım: Herhangi bir komut gerekmez — yönetici botla DM'de bir video,
belge (video/ses dosyası), ses dosyası ya da http(s):// ile başlayan bir
medya linki paylaştığında otomatik çalışır.

Not: Yalnızca dosyanın/bağlantının başındaki birkaç MB indirildiği için,
meta verileri (moov atom vb.) dosyanın SONUNDA tutan bazı "faststart"
olmayan MP4 dosyalarında analiz başarısız olabilir. Bu, tam indirme
yapmamanın kabul edilen bir sınırlamasıdır. Ayrıca linkli dosyalarda
sunucu HTTP Range isteğini desteklemiyorsa toplam dosya boyutu
bilinmeyebilir.
"""

import asyncio
import json
import math
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

from Backend.helper.custom_filter import CustomFilters
from Backend.helper.pyro import get_readable_file_size, clean_filename
from Backend.logger import LOGGER

try:
    from pymediainfo import MediaInfo
    _HAS_PYMEDIAINFO = True
except Exception:
    _HAS_PYMEDIAINFO = False

_FFPROBE_BIN = shutil.which("ffprobe") or "ffprobe"

_WORK_DIR = Path("/tmp/mediainfo_bot")
_WORK_DIR.mkdir(parents=True, exist_ok=True)

# Tüm dosyayı indirmek yerine KADEMELİ (aşamalı) indirme yapılır:
# önce en küçük adım (1MB) indirilip analiz denenir; analiz başarısız
# olursa bir SONRAKİ adıma kadar TAMAMLANIR (baştan değil, kaldığı yerden
# devam edilerek) ve tekrar denenir. İlk başarılı analizde durulur;
# son adımda (varsayılan 8MB) da başarısız olursa pes edilir.
def _parse_partial_steps_mb() -> list[int]:
    raw = os.environ.get("MEDIAINFO_PARTIAL_STEPS_MB", "1,3,8")
    steps: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0:
            steps.append(value)
    steps = sorted(set(steps))
    return steps or [1, 3, 8]


_PARTIAL_DOWNLOAD_STEPS_MB = _parse_partial_steps_mb()
_PARTIAL_DOWNLOAD_MAX_MB = _PARTIAL_DOWNLOAD_STEPS_MB[-1]
_STREAM_CHUNK_SIZE = 1024 * 1024  # pyrogram/pyrofork stream_media chunk ~1MB

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_CONTENT_TYPE_EXT = {
    "video/mp4": ".mp4",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/mpeg": ".mpg",
    "video/x-flv": ".flv",
    "audio/mpeg": ".mp3",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "application/vnd.apple.mpegurl": ".m3u8",
    "application/x-mpegurl": ".m3u8",
}

# ─── Dil / kanal etiketleri (yayin_routes.py'daki mantığın sade kopyası) ──────
_LANGUAGE_NAMES = {
    "tr": "Türkçe", "tur": "Türkçe",
    "en": "İngilizce", "eng": "İngilizce",
    "de": "Almanca", "ger": "Almanca", "deu": "Almanca",
    "fr": "Fransızca", "fre": "Fransızca", "fra": "Fransızca",
    "es": "İspanyolca", "spa": "İspanyolca",
    "it": "İtalyanca", "ita": "İtalyanca",
    "ru": "Rusça", "rus": "Rusça",
    "ar": "Arapça", "ara": "Arapça",
    "ja": "Japonca", "jpn": "Japonca",
    "ko": "Korece", "kor": "Korece",
    "zh": "Çince", "chi": "Çince", "zho": "Çince",
    "nl": "Felemenkçe", "dut": "Felemenkçe", "nld": "Felemenkçe",
    "pt": "Portekizce", "por": "Portekizce",
    "pl": "Lehçe", "pol": "Lehçe",
    "und": "Bilinmeyen",
}


def _language_label(code: str | None, name: str | None = None) -> str:
    if code:
        label = _LANGUAGE_NAMES.get(code.strip().lower())
        if label:
            return label
    if name:
        return name
    return (code or "Bilinmeyen").upper()


def _channel_layout_label(channels) -> str:
    if not channels:
        return "—"
    raw = str(channels).split("/")[0].strip()
    mapping = {"1": "Mono", "2": "2.0 (Stereo)", "6": "5.1", "8": "7.1"}
    return mapping.get(raw, f"{raw} kanal" if raw.isdigit() else raw)


def _fmt_duration(seconds) -> str:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}s {m}dk {s}sn"
    if m:
        return f"{m}dk {s}sn"
    return f"{s}sn"


def _fmt_bitrate(bps) -> str:
    try:
        bps = float(bps)
    except (TypeError, ValueError):
        return "—"
    if bps >= 1_000_000:
        return f"{bps/1_000_000:.2f} Mb/s"
    if bps >= 1_000:
        return f"{bps/1_000:.0f} kb/s"
    return f"{bps:.0f} b/s"


# ─── MediaInfo (native kütüphane varsa) ───────────────────────────────────────

def _try_pymediainfo_parse(path: str) -> "MediaInfo | None":
    """pymediainfo (libmediainfo) kuruluysa dosyayı parse edip track nesnelerini döndürür."""
    if not _HAS_PYMEDIAINFO:
        return None
    try:
        mi = MediaInfo.parse(path)
        if mi and getattr(mi, "tracks", None):
            return mi
        return None
    except Exception as e:
        LOGGER.warning(f"[MediaInfo] pymediainfo başarısız, ffprobe'a düşülüyor: {e}")
        return None


# ─── ffprobe (yedek analiz) ────────────────────────────────────────────────────

async def _ffprobe_json(path: str) -> dict | None:
    cmd = [
        _FFPROBE_BIN, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0 or not stdout:
            LOGGER.warning(f"[MediaInfo] ffprobe hata: {stderr.decode(errors='ignore')[:300]}")
            return None
        return json.loads(stdout.decode("utf-8", errors="ignore"))
    except FileNotFoundError:
        LOGGER.error("[MediaInfo] ffprobe sunucuda bulunamadı.")
        return None
    except Exception as e:
        LOGGER.warning(f"[MediaInfo] ffprobe çalıştırılamadı: {e}")
        return None


def _clean_value(value, default="—"):
    if value is None:
        return default
    value = str(value).strip()
    return value if value else default


def _yes_no(value) -> str:
    return "Evet" if value else "Hayır"


def _stream_flags(disposition: dict | None) -> str:
    disposition = disposition or {}
    flags = []
    if disposition.get("default"):
        flags.append("Varsayılan")
    if disposition.get("forced"):
        flags.append("Zorunlu")
    return ", ".join(flags) if flags else "—"


def _fps_label(stream: dict) -> str:
    fps = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or ""
    try:
        num, den = str(fps).split("/", 1)
        value = float(num) / float(den)
        return f"{value:.3f} FPS" if value else "—"
    except Exception:
        return _clean_value(fps)


def _bit_depth(stream: dict) -> str:
    value = stream.get("bits_per_raw_sample") or stream.get("bits_per_coded_sample")
    if value:
        return f"{value}-bit"
    pix_fmt = str(stream.get("pix_fmt") or "")
    match = re.search(r"(?:p|le|be)(10|12|16)(?:le|be)?$", pix_fmt)
    if match:
        return f"{match.group(1)}-bit"
    return "—"


def _hdr_label(stream: dict) -> str:
    transfer = str(stream.get("color_transfer") or "").lower()
    primaries = str(stream.get("color_primaries") or "").lower()
    side_data = stream.get("side_data_list") or []
    side_text = " ".join(str(x).lower() for x in side_data)
    codec = str(stream.get("codec_name") or "").lower()

    if "dovi" in side_text or "dolby vision" in side_text or codec in {"dvhe", "dvh1"}:
        return "Dolby Vision"
    if transfer in {"smpte2084", "pq"}:
        if "bt2020" in primaries or primaries in {"bt2020-10", "bt2020-12"}:
            return "HDR10 / PQ"
        return "HDR / PQ"
    if transfer == "arib-std-b67":
        return "HLG"
    return "SDR"


def _chroma_label(stream: dict) -> str:
    value = stream.get("chroma_location")
    pix_fmt = str(stream.get("pix_fmt") or "")
    # ffprobe's pix_fmt is useful as a compact fallback (yuv420p, yuv420p10le...).
    match = re.match(r"yuv(420|422|444|411|440)", pix_fmt)
    if match:
        return f"{match.group(1)[:3]}" if match.group(1) != "420" else "4:2:0"
    return _clean_value(value)


def _language_and_title(tags: dict) -> tuple[str, str | None]:
    lang = tags.get("language") or tags.get("LANGUAGE")
    title = tags.get("title") or tags.get("TITLE")
    return _language_label(lang), title


def _report_header(filename: str, filesize: int, analyzed_bytes: int, engine: str,
                   format_name: str, duration, overall_bitrate) -> list[str]:
    """Telegram için sade, mobil ekranda rahat okunan üst bilgi."""
    analyzed = (
        "doğrudan bağlantı"
        if analyzed_bytes == 0
        else f"ilk {get_readable_file_size(analyzed_bytes)}"
    )
    return [
        "🧾 MEDIAINFO",
        "",
        f"📄 {filename}",
        f"💾 {get_readable_file_size(filesize) if filesize else 'Bilinmiyor'}",
        f"⏱ {_fmt_duration(duration)}",
        f"📦 {_clean_value(format_name)}",
        f"⚡ {_fmt_bitrate(overall_bitrate)}",
    ]


def _stream_flags(disposition: dict | None) -> str:
    disposition = disposition or {}
    flags = []
    if disposition.get("forced"):
        flags.append("Zorunlu")
    if disposition.get("default"):
        flags.append("Varsayılan")
    return ", ".join(flags)


def _stream_info_line(flags: str) -> str | None:
    return f"Durum: {flags}" if flags else None


def _build_text_report(data: dict, filename: str, full_filesize: int,
                       analyzed_bytes: int, source: str,
                       fallback_duration=None) -> str:
    """Telegram için sade ve okunabilir ffprobe raporu."""
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    engine = "MediaInfo" if source == "pymediainfo" else "ffprobe"

    duration = fmt.get("duration") or fallback_duration
    overall_bitrate = None
    if duration and full_filesize:
        try:
            overall_bitrate = (full_filesize * 8) / float(duration)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if overall_bitrate is None:
        overall_bitrate = fmt.get("bit_rate")

    lines = _report_header(
        filename, full_filesize, analyzed_bytes, engine,
        fmt.get("format_long_name") or fmt.get("format_name"),
        duration, overall_bitrate,
    )

    v_idx = a_idx = s_idx = 0
    for s in streams:
        codec_type = s.get("codec_type")
        disposition = s.get("disposition", {}) or {}
        tags = s.get("tags", {}) or {}
        lang_label, title = _language_and_title(tags)
        flags = _stream_flags(disposition)

        if codec_type == "video" and disposition.get("attached_pic"):
            continue

        if codec_type == "video":
            v_idx += 1
            w, h = s.get("width"), s.get("height")
            profile = s.get("profile")
            bit_depth = _bit_depth(s)
            hdr = _hdr_label(s)
            chroma = _chroma_label(s)
            pix_fmt = s.get("pix_fmt")

            codec = _clean_value(s.get("codec_name")).upper()
            if profile:
                codec += f" / {profile}"

            lines += [
                "",
                f"🎬 VIDEO #{v_idx}",
                f"   {codec}",
                f"   {f'{w}×{h}' if w and h else '—'}",
                f"   {_clean_value(s.get('display_aspect_ratio'))}",
                f"   {_fps_label(s)}",
                f"   {bit_depth}",
                f"   {hdr}",
                f"   {chroma}",
            ]
            # Pixel format yalnızca gerçekten anlamlıysa gösterilir.
            if title:
                lines.append(f"   🏷 {title}")
            if flags:
                lines.append(f"   {flags}")

        elif codec_type == "audio":
            a_idx += 1
            codec = _clean_value(s.get("codec_name")).upper()
            profile = s.get("profile")
            if profile:
                codec += f" / {profile}"

            lines += [
                "",
                f"🔊 AUDIO #{a_idx}",
                f"   {codec}",
                f"   {_channel_layout_label(s.get('channels'))}",
                f"   {_fmt_bitrate(s.get('bit_rate'))}",
                f"   {_clean_value(s.get('sample_rate'))} Hz",
                f"   {lang_label}",
            ]
            if title:
                lines.append(f"   🏷 {title}")
            if flags:
                lines.append(f"   {flags}")

        elif codec_type == "subtitle":
            s_idx += 1
            codec = _clean_value(s.get("codec_name")).upper()
            lines += [
                "",
                f"💬 SUBTITLE #{s_idx}",
                f"   {lang_label}",
                f"   {codec}",
                f"   Forced: {'Evet' if disposition.get('forced') else 'Hayır'}",
                f"   Default: {'Evet' if disposition.get('default') else 'Hayır'}",
            ]
            if title:
                lines.append(f"   🏷 {title}")

    if v_idx == a_idx == s_idx == 0:
        lines += [
            "",
            "⚠️ ANALİZ BAŞARISIZ",
            "Video, ses veya altyazı akışı tespit edilemedi.",
        ]

    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3970] + "\n\n… rapor kısaltıldı"


def _pymediainfo_to_text_report(mi: "MediaInfo", filename: str,
                                full_filesize: int, analyzed_bytes: int) -> str:
    """pymediainfo sonucunu ffprobe ile aynı sade Telegram formatına çevirir."""
    general = next((t for t in mi.tracks if t.track_type == "General"), None)
    duration = getattr(general, "duration", None)
    if duration:
        duration = float(duration) / 1000.0

    overall_bitrate = None
    if duration and full_filesize:
        try:
            overall_bitrate = (full_filesize * 8) / duration
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if overall_bitrate is None:
        overall_bitrate = getattr(general, "overall_bit_rate", None)

    lines = _report_header(
        filename, full_filesize, analyzed_bytes, "MediaInfo",
        getattr(general, "format", None), duration, overall_bitrate,
    )

    v_idx = a_idx = s_idx = 0
    for t in mi.tracks:
        track_type = t.track_type
        flags = []
        if getattr(t, "forced", None) in (1, "1", True):
            flags.append("Zorunlu")
        if getattr(t, "default", None) in (1, "1", True):
            flags.append("Varsayılan")
        flags_text = ", ".join(flags)
        title = getattr(t, "title", None)

        if track_type == "Video":
            v_idx += 1
            w, h = getattr(t, "width", None), getattr(t, "height", None)
            bit_depth = getattr(t, "bit_depth", None)
            if bit_depth:
                bit_depth = f"{bit_depth}-bit" if str(bit_depth).isdigit() else str(bit_depth)
            else:
                bit_depth = "—"

            hdr = getattr(t, "hdr_format", None) or getattr(t, "hdr_format_string", None)
            if not hdr:
                transfer = str(getattr(t, "transfer_characteristics", None) or "").lower()
                hdr = "HDR10" if "smpte2084" in transfer or "pq" in transfer else ("HLG" if "hlg" in transfer else "SDR")

            codec = _clean_value(getattr(t, "format", None)).upper()
            profile = getattr(t, "format_profile", None)
            if profile:
                codec += f" / {profile}"

            lines += [
                "",
                f"🎬 VIDEO #{v_idx}",
                f"   {codec}",
                f"   {f'{w}×{h}' if w and h else '—'}",
                f"   {_clean_value(getattr(t, 'display_aspect_ratio', None))}",
                f"   {_clean_value(getattr(t, 'frame_rate', None), '—')} FPS" if getattr(t, 'frame_rate', None) else "   —",
                f"   {bit_depth}",
                f"   {_clean_value(hdr)}",
                f"   {_clean_value(getattr(t, 'chroma_subsampling', None))}",
            ]
            if getattr(t, "title", None):
                lines.append(f"   🏷 {title}")
            if flags_text:
                lines.append(f"   {flags_text}")

        elif track_type == "Audio":
            a_idx += 1
            codec = _clean_value(getattr(t, "format", None)).upper()
            profile = getattr(t, "format_profile", None)
            if profile:
                codec += f" / {profile}"
            lines += [
                "",
                f"🔊 AUDIO #{a_idx}",
                f"   {codec}",
                f"   {_channel_layout_label(getattr(t, 'channel_s', None))}",
                f"   {_fmt_bitrate(getattr(t, 'bit_rate', None))}",
                f"   {_clean_value(getattr(t, 'sampling_rate', None))} Hz",
                f"   {_language_label(getattr(t, 'language', None), None)}",
            ]
            if title:
                lines.append(f"   🏷 {title}")
            if flags_text:
                lines.append(f"   {flags_text}")

        elif track_type == "Text":
            s_idx += 1
            lines += [
                "",
                f"💬 SUBTITLE #{s_idx}",
                f"   {_language_label(getattr(t, 'language', None), None)}",
                f"   {_clean_value(getattr(t, 'format', None)).upper()}",
                f"   Forced: {'Evet' if 'Zorunlu' in flags else 'Hayır'}",
                f"   Default: {'Evet' if 'Varsayılan' in flags else 'Hayır'}",
            ]
            if title:
                lines.append(f"   🏷 {title}")

    if v_idx == a_idx == s_idx == 0:
        lines += [
            "",
            "⚠️ ANALİZ BAŞARISIZ",
            "Video, ses veya altyazı akışı tespit edilemedi.",
        ]

    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3970] + "\n\n… rapor kısaltıldı"


async def _try_analyze_file(
    path: str, filename: str, full_filesize: int, analyzed_bytes: int,
    fallback_duration=None,
) -> str | None:
    """İndirilen (kısmi) dosyayı önce pymediainfo, olmazsa ffprobe ile
    analiz etmeyi dener. Akış/track tespit edilemezse None döner ki
    çağıran bir sonraki (daha büyük) kademeye geçebilsin."""
    mi_obj = await asyncio.to_thread(_try_pymediainfo_parse, path)
    if mi_obj is not None and getattr(mi_obj, "tracks", None):
        has_stream = any(t.track_type in ("Video", "Audio", "Text") for t in mi_obj.tracks)
        if has_stream:
            return _pymediainfo_to_text_report(mi_obj, filename, full_filesize, analyzed_bytes)

    data = await _ffprobe_json(path)
    if data and data.get("streams"):
        return _build_text_report(
            data, filename, full_filesize, analyzed_bytes, "ffprobe", fallback_duration
        )
    return None


async def _download_partial_steps(
    client: Client, message: Message, dest: Path, steps_bytes: list[int]
):
    """Dosyayı KADEMELİ olarak indirir: `steps_bytes` içindeki her eşiğe
    ulaşıldığında o ana kadar diske yazılmış toplam bayt sayısını yield
    eder ve çağırana analiz denemesi için kontrolü bırakır.

    Önemli: indirme her adımda BAŞTAN değil, kaldığı yerden (aynı açık
    dosya tanıtıcısı ve aynı stream_media akışı üzerinden) devam eder;
    yalnızca gerekli olan EK kısım indirilir. Dosyanın tamamı asla
    indirilmez — akış en fazla en büyük eşiğe kadar (`limit` parçası)
    çekilir.

    pyrofork/pyrogram'ın stream_media() metodu dosyayı ~1MB'lık parçalar
    (chunk) halinde CDN'den okur; `limit` parametresi kaç parça
    indirileceğini belirtir.
    """
    max_target = steps_bytes[-1]
    limit_chunks = max(1, math.ceil(max_target / _STREAM_CHUNK_SIZE))
    written = 0
    step_i = 0
    last_yielded = 0
    with open(dest, "wb") as f:
        async for chunk in client.stream_media(message, limit=limit_chunks):
            f.write(chunk)
            written += len(chunk)
            if step_i < len(steps_bytes) and written >= steps_bytes[step_i]:
                f.flush()
                step_i += 1
                last_yielded = written
                yield written
        # Akış, en büyük eşiğe ulaşmadan bitti (dosya zaten küçük) —
        # elde olan son hali de analiz için bildir.
        if written > last_yielded:
            yield written


# ─── HTTP(S) link desteği ──────────────────────────────────────────────────────

def _filename_from_url(url: str, content_type: str | None = None) -> str:
    """URL'nin path kısmından (veya Content-Type'tan) makul bir dosya adı üretir."""
    path = unquote(urlsplit(url).path)
    name = os.path.basename(path.rstrip("/"))
    if name and "." in name:
        return name
    ext = _CONTENT_TYPE_EXT.get((content_type or "").split(";")[0].strip().lower(), "")
    return (name or "link_dosyasi") + ext


async def _download_partial_from_url_steps(
    url: str, dest: Path, steps_bytes: list[int]
):
    """HTTP(S) linkten dosyanın TAMAMINI DEĞİL, KADEMELİ olarak yalnızca
    gereken kadarını indirir.

    Önce HTTP Range header'ı ile ("bytes=0-N") en büyük adımı kapsayacak
    kadarlık kısmı ister. `steps_bytes` içindeki her eşiğe ulaşıldığında
    (o ana kadar indirilen bayt, sunucunun bildirdiği toplam dosya boyutu,
    content_type) üçlüsünü yield eder; bağlantı adımlar arasında AÇIK
    kalır, yani bir önceki adımdan devam edilir, baştan indirilmez.
    Sunucu Range'i desteklemeyip tüm dosyayı göndermeye başlarsa bile, en
    büyük eşiğe ulaşır ulaşılmaz bağlantı kapatılır ve geri kalanı hiç
    indirilmez.
    """
    max_target = steps_bytes[-1]
    headers = {
        "Range": f"bytes=0-{max_target - 1}",
        "User-Agent": "Mozilla/5.0 (compatible; MediaInfoBot/1.0)",
    }
    written = 0
    full_size = 0
    content_type = None
    step_i = 0
    last_yielded = 0
    timeout = httpx.Timeout(15.0, read=30.0)

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as http:
        async with http.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type")

            content_range = resp.headers.get("Content-Range")
            if content_range and "/" in content_range:
                try:
                    full_size = int(content_range.rsplit("/", 1)[-1])
                except ValueError:
                    full_size = 0

            if not full_size and resp.status_code == 200:
                # Sunucu Range isteğini yok saymış (206 değil 200 döndü),
                # yani tüm dosyayı göndermeye başlamış; bu durumda
                # Content-Length gerçek toplam boyuttur.
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        full_size = int(cl)
                    except ValueError:
                        full_size = 0

            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                    f.write(chunk)
                    written += len(chunk)
                    if step_i < len(steps_bytes) and written >= steps_bytes[step_i]:
                        f.flush()
                        step_i += 1
                        last_yielded = written
                        yield written, full_size, content_type
                    if written >= max_target:
                        break  # sunucu Range'i desteklemese bile burada kes
                if written > last_yielded:
                    yield written, full_size, content_type


@Client.on_message(
    filters.private
    & CustomFilters.owner
    & (filters.video | filters.document | filters.audio)
)
async def mediainfo_on_media(client: Client, message: Message):
    media = message.video or message.audio or message.document
    if media is None:
        return

    # Belge ise, gerçekten video/ses dosyası mı diye mime-type ile kabaca süz
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if not (mime.startswith("video/") or mime.startswith("audio/")):
            return

    steps_mb = _PARTIAL_DOWNLOAD_STEPS_MB
    status = await message.reply_text(
        f"⏳ İlk {steps_mb[0]}MB indiriliyor…", quote=True
    )

    orig_name = getattr(media, "file_name", None) or f"media_{media.file_unique_id}"
    safe_name = clean_filename(orig_name) or orig_name
    dest = _WORK_DIR / f"{message.id}_{safe_name}"

    full_filesize = getattr(media, "file_size", 0) or 0
    fallback_duration = getattr(media, "duration", None)  # Telegram'ın verdiği süre (varsa)
    steps_bytes = [mb * 1024 * 1024 for mb in steps_mb]

    # ── Dosyanın TAMAMINI DEĞİL, KADEMELİ olarak indir: 1MB → yetmezse
    #    3MB'a tamamla → yetmezse 8MB'a çık; ilk başarılı analizde dur ──
    analyzed_bytes = 0
    report_text = None
    step_i = 0
    agen = _download_partial_steps(client, message, dest, steps_bytes)
    try:
        async for written in agen:
            analyzed_bytes = written
            await status.edit_text(
                f"🔍 {get_readable_file_size(written)} analiz ediliyor…"
            )
            report_text = await _try_analyze_file(
                str(dest), orig_name, full_filesize, analyzed_bytes, fallback_duration
            )
            if report_text:
                break
            step_i += 1
            if step_i < len(steps_mb):
                await status.edit_text(
                    f"⏳ {steps_mb[step_i - 1]}MB yetersiz, "
                    f"{steps_mb[step_i]}MB'a tamamlanıyor…"
                )
    except Exception as e:
        LOGGER.error(f"[MediaInfo] Kısmi indirme hatası: {e}")
        await status.edit_text(f"❌ Dosya indirilemedi: {e}")
        dest.unlink(missing_ok=True)
        return
    finally:
        await agen.aclose()

    if not dest.exists() or analyzed_bytes == 0:
        await status.edit_text("❌ Dosya indirilemedi.")
        dest.unlink(missing_ok=True)
        return

    dest.unlink(missing_ok=True)  # indirilen küçük parça artık gerekmiyor

    if not report_text:
        await status.edit_text(
            "❌ Teknik bilgi çıkarılamadı. Sunucuda ne MediaInfo (libmediainfo) "
            "ne de ffprobe çalışır durumda değil ya da indirilen ön parça "
            "analiz için yeterli/uygun değil."
        )
        return

    # ── Sonucu dosya olarak DEĞİL, düz metin mesajı olarak gönder ──
    try:
        await status.edit_text(report_text)
    except Exception as e:
        LOGGER.error(f"[MediaInfo] Rapor gönderilemedi: {e}")
        try:
            await message.reply_text(report_text, quote=True)
            await status.delete()
        except Exception as e2:
            LOGGER.error(f"[MediaInfo] Rapor mesajı da gönderilemedi: {e2}")


@Client.on_message(
    filters.private
    & CustomFilters.owner
    & filters.text
    & filters.regex(_URL_RE)
    & ~filters.command("")  # /komut şeklindeki mesajları bu handler yakalamasın
)
async def mediainfo_on_link(client: Client, message: Message):
    """Yönetici DM'de http(s):// ile başlayan bir medya linki paylaşırsa,
    dosyanın tamamını indirmeden ilk birkaç MB'ını (HTTP Range ile) çekip
    aynı MediaInfo/ffprobe analizini bu link için de yapar."""
    text = (message.text or "").strip()
    match = _URL_RE.search(text)
    if not match:
        return
    # Mesaj sonundaki noktalama/parantez gibi karakterleri linkten ayıkla
    url = match.group(0).rstrip(").,]}>\"'")

    steps_mb = _PARTIAL_DOWNLOAD_STEPS_MB
    status = await message.reply_text(
        f"⏳ Bağlantıdan ilk {steps_mb[0]}MB indiriliyor…", quote=True
    )

    dest = _WORK_DIR / f"{message.id}_link_download"
    steps_bytes = [mb * 1024 * 1024 for mb in steps_mb]
    is_m3u8 = url.split("?", 1)[0].lower().endswith(".m3u8")

    if is_m3u8:
        # m3u8 zaten küçük bir metin playlist'i; indirmeye gerek yok,
        # ffprobe/MediaInfo'ya doğrudan URL üzerinden baktırılır. Kademeli
        # indirme burada uygulanmaz çünkü zaten dosya indirilmiyor.
        content_type = "application/vnd.apple.mpegurl"
        orig_name = _filename_from_url(url, content_type)
        await status.edit_text("🔍 MediaInfo analizi yapılıyor…")
        report_text = await _try_analyze_file(url, orig_name, 0, 0)
        if not report_text:
            await status.edit_text(
                "❌ Teknik bilgi çıkarılamadı. Bağlantı geçerli bir medya dosyasına "
                "işaret etmiyor olabilir, sunucu isteğe yanıt vermiyor olabilir ya "
                "da indirilen ön parça analiz için yeterli/uygun değil."
            )
            return
        try:
            await status.edit_text(report_text)
        except Exception as e:
            LOGGER.error(f"[MediaInfo] Rapor gönderilemedi: {e}")
            try:
                await message.reply_text(report_text, quote=True)
                await status.delete()
            except Exception as e2:
                LOGGER.error(f"[MediaInfo] Rapor mesajı da gönderilemedi: {e2}")
        return

    # ── Bağlantıdan dosyanın TAMAMINI DEĞİL, KADEMELİ olarak indir:
    #    1MB → yetmezse 3MB'a tamamla → yetmezse 8MB'a çık; ilk başarılı
    #    analizde dur ──
    analyzed_bytes = 0
    full_filesize = 0
    content_type = None
    orig_name = _filename_from_url(url)
    report_text = None
    step_i = 0
    agen = _download_partial_from_url_steps(url, dest, steps_bytes)
    try:
        async for written, full_filesize, content_type in agen:
            analyzed_bytes = written
            orig_name = _filename_from_url(url, content_type)
            await status.edit_text(
                f"🔍 {get_readable_file_size(written)} analiz ediliyor…"
            )
            report_text = await _try_analyze_file(
                str(dest), orig_name, full_filesize, analyzed_bytes
            )
            if report_text:
                break
            step_i += 1
            if step_i < len(steps_mb):
                await status.edit_text(
                    f"⏳ {steps_mb[step_i - 1]}MB yetersiz, "
                    f"{steps_mb[step_i]}MB'a tamamlanıyor…"
                )
    except httpx.HTTPStatusError as e:
        await status.edit_text(
            f"❌ Bağlantı indirilemedi: HTTP {e.response.status_code}"
        )
        dest.unlink(missing_ok=True)
        return
    except Exception as e:
        LOGGER.error(f"[MediaInfo] Link indirme hatası: {e}")
        await status.edit_text(f"❌ Bağlantı indirilemedi: {e}")
        dest.unlink(missing_ok=True)
        return
    finally:
        await agen.aclose()

    if not dest.exists() or analyzed_bytes == 0:
        await status.edit_text("❌ Bağlantıdan veri indirilemedi.")
        dest.unlink(missing_ok=True)
        return

    dest.unlink(missing_ok=True)

    if not report_text:
        await status.edit_text(
            "❌ Teknik bilgi çıkarılamadı. Bağlantı geçerli bir medya dosyasına "
            "işaret etmiyor olabilir, sunucu isteğe yanıt vermiyor olabilir ya "
            "da indirilen ön parça analiz için yeterli/uygun değil."
        )
        return

    try:
        await status.edit_text(report_text)
    except Exception as e:
        LOGGER.error(f"[MediaInfo] Rapor gönderilemedi: {e}")
        try:
            await message.reply_text(report_text, quote=True)
            await status.delete()
        except Exception as e2:
            LOGGER.error(f"[MediaInfo] Rapor mesajı da gönderilemedi: {e2}")
