"""
youtube_ingest.py
──────────────────
YouTube canlı yayınlarını (live broadcast) yt-dlp ile çözüp, ffmpeg ile
sunucu içinde yerel bir HLS akışına (m3u8 + .ts segmentleri) dönüştürür.

Neden ffmpeg?
  YouTube 1080p'nin üzerindeki kaliteler (1440p, 2160p/4K vb.) için ayrı
  video-only ve audio-only DASH akışları sağlar. Tek parça bir HLS/M3U8
  linki yoktur. Bu modül yt-dlp ile en uygun video+audio format çiftini
  bulur, ffmpeg ile "-c copy" (yeniden kodlama yapmadan, hızlı) tek bir HLS
  akışına muxler ve diske yazar.

Akış:
  1. start_ingest() çağrılır → arka planda bir "supervisor" task başlar.
  2. Supervisor, yt-dlp ile YouTube linkini çözer (cookies.txt varsa kullanır),
     seçilen kaliteye en yakın video+audio formatlarını bulur, ffmpeg'i başlatır.
  3. ffmpeg çıktısı `<cache_dir>/<broadcast_id>/index.m3u8` dosyasına yazılır.
  4. Bu dosya, yayin_routes.py içindeki dahili (yalnız localhost) bir HTTP
     route ile sunulur; mevcut `_hls_fetcher` bu URL'yi normal bir HLS
     kaynağı gibi okur — segment önbellekleme/token/kota sistemi değişmeden çalışır.
  5. googlevideo.com linkleri belirli bir süre sonra geçersiz olabileceğinden
     (veya ffmpeg başka bir sebeple düşerse), supervisor ffmpeg'in çökmesini
     izler ve otomatik olarak yeniden çözüp yeniden başlatır.
  6. Çözülen gerçek kalite (çözünürlük/fps/codec) loglanır ve
     get_resolved_quality() ile panelde gösterilmek üzere okunabilir.
"""

import asyncio
import logging
import pathlib
import shutil
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Proje kökü: Backend/helper/youtube_ingest.py → Backend/helper → Backend → kök
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
COOKIES_PATH  = _PROJECT_ROOT / "cookies.txt"
CACHE_ROOT    = _PROJECT_ROOT / "Backend" / "fastapi" / "yayin_youtube_cache"

# Panelde gösterilen kalite etiketi → hedef dikey çözünürlük (px). None = en iyi.
QUALITY_HEIGHT_MAP: Dict[str, Optional[int]] = {
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p":  720,
    "480p":  480,
    "360p":  360,
    "best":  None,
}

_RESTART_MIN_DELAY = 5.0
_RESTART_MAX_DELAY = 60.0
_PLAYLIST_WAIT_TIMEOUT = 30.0  # ffmpeg'in ilk m3u8'i yazması için beklenecek üst sınır (sn)


@dataclass
class _Ingest:
    broadcast_id: str
    youtube_url: str
    quality: str
    out_dir: pathlib.Path
    should_run: bool = True
    proc: Optional[asyncio.subprocess.Process] = None
    task: Optional[asyncio.Task] = None
    resolved_quality: str = ""
    last_error: str = ""
    restart_count: int = 0


_active: Dict[str, _Ingest] = {}


def _format_selector(height: Optional[int]) -> str:
    """yt-dlp format seçici string'i üretir. Sadece indirilebilir/canlı akış
    formatlarını (m3u8/https doğrudan akışlar) hedefler; yerel dosyaya
    indirme yapılmaz, sadece URL'ler çözülür."""
    if height:
        return (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/"
            "bestvideo+bestaudio/best"
        )
    return "bestvideo+bestaudio/best"


class CookiesRequiredError(RuntimeError):
    """YouTube 'Sign in to confirm you're not a bot' engeline takıldığında fırlatılır."""


# Denenecek istemci (player_client) sırası. Her giriş (clients, use_cookies) çiftidir.
# use_cookies=False → o denemede cookies.txt kasıtlı olarak gönderilmez: bazı istemciler
# (örn. android_vr) cookie ile hiç çalışmıyor/PO-token istiyor ama cookiesiz halde
# YouTube'un SABR/"page needs to be reloaded" bloğunu bazen atlatabiliyor.
# None = yt-dlp'nin kendi güncel varsayılan istemci listesini kullanır.
_CLIENT_ATTEMPTS = [
    (["web_embedded"], True),
    (["android_vr"], False),
    (["web_embedded", "tv"], True),
    (None, True),
    (["tv"], True),
    (["web", "tv"], True),
]


def _extract_formats(youtube_url: str, quality: str):
    """Senkron (blocking) — executor içinde çağrılmalı.
    Dönen: (video_fmt: dict, audio_fmt: dict|None, raw_info: dict)"""
    import yt_dlp

    height = QUALITY_HEIGHT_MAP.get(quality)
    has_cookies = COOKIES_PATH.exists()
    logger.info(
        f"YouTube çözümü başlıyor — cookies.txt {'bulundu (' + str(COOKIES_PATH) + ')' if has_cookies else 'bulunamadı, cookiesiz denenecek'}, "
        f"denenecek istemciler: {[(c or 'default', uc) for c, uc in _CLIENT_ATTEMPTS]}"
    )

    last_exc: Optional[Exception] = None
    saw_bot_check = False

    for clients, use_cookies in _CLIENT_ATTEMPTS:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "format": _format_selector(height),
        }
        if clients:
            ydl_opts["extractor_args"] = {"youtube": {"player_client": clients}}
        if has_cookies and use_cookies:
            ydl_opts["cookiefile"] = str(COOKIES_PATH)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
            if info is None:
                raise RuntimeError("yt-dlp video bilgisini çözemedi.")
            video_fmt, audio_fmt = _pick_formats(info)
            if clients:
                logger.info(f"YouTube çözümü '{','.join(clients)}' istemcisiyle başarılı oldu.")
            return video_fmt, audio_fmt, info
        except Exception as e:
            last_exc = e
            msg = str(e)
            if "Sign in to confirm" in msg or "not a bot" in msg:
                saw_bot_check = True
            logger.warning(
                f"YouTube çözüm denemesi başarısız — client={clients or 'default'}, "
                f"cookies={'var' if (has_cookies and use_cookies) else 'yok'}, hata_tipi={type(e).__name__}: {msg}"
            )
            continue

    # Tüm istemci denemeleri başarısız oldu
    logger.error(
        f"YouTube çözümü tüm istemci denemelerinde başarısız oldu "
        f"(denenenler: {[(c or 'default', uc) for c, uc in _CLIENT_ATTEMPTS]}, cookies={'var' if has_cookies else 'yok'}). "
        f"Son hata: {type(last_exc).__name__ if last_exc else '?'}: {last_exc}"
    )
    if saw_bot_check and not has_cookies:
        raise CookiesRequiredError(
            "YouTube bu isteği bot şüphesiyle reddetti (sunucu IP'si data-center olduğu için "
            "sıkça tetiklenir). Ayarlar → Dosya Ekle bölümünden geçerli, oturum açılmış bir "
            "YouTube hesabına ait cookies.txt yükleyin ve tekrar deneyin."
        )
    if saw_bot_check and has_cookies:
        raise CookiesRequiredError(
            "YouTube isteği bot şüphesiyle reddetmeye devam ediyor. Yüklü olan cookies.txt "
            "süresi dolmuş veya geçersiz olabilir — tarayıcınızda youtube.com'a giriş yapmış "
            "durumdayken cookies'i yeniden dışa aktarıp güncelleyin."
        )
    raise last_exc or RuntimeError("YouTube video bilgisi çözülemedi (bilinmeyen hata).")


def _pick_formats(info: dict):
    """extract_info() sonucundan (video_fmt, audio_fmt) çifti çıkarır."""
    requested = info.get("requested_formats")
    if requested:
        video_fmt = next((f for f in requested if f.get("vcodec") not in (None, "none")), requested[0])
        audio_fmt = next(
            (f for f in requested if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")),
            None,
        )
    else:
        video_fmt = info
        audio_fmt = None

    if not video_fmt.get("url"):
        raise RuntimeError("Seçilen format için oynatılabilir bir URL bulunamadı.")
    return video_fmt, audio_fmt


def _describe_quality(video_fmt: dict, audio_fmt: Optional[dict]) -> str:
    w = video_fmt.get("width")
    h = video_fmt.get("height")
    fps = video_fmt.get("fps")
    vcodec = video_fmt.get("vcodec") or "?"
    v_itag = video_fmt.get("format_id") or "?"
    tbr = video_fmt.get("tbr") or video_fmt.get("vbr")

    parts = []
    if w and h:
        parts.append(f"{w}x{h} ({h}p)")
    if fps:
        parts.append(f"{fps:.0f}fps" if isinstance(fps, float) else f"{fps}fps")
    parts.append(f"video={vcodec} [itag {v_itag}]")
    if tbr:
        parts.append(f"~{tbr:.0f} kbps")
    if audio_fmt:
        a_itag = audio_fmt.get("format_id") or "?"
        parts.append(f"audio={audio_fmt.get('acodec') or '?'} [itag {a_itag}]")
    else:
        parts.append("audio=dahili (video ile birlikte)")
    return ", ".join(parts)


def _build_ffmpeg_cmd(video_fmt: dict, audio_fmt: Optional[dict], out_dir: pathlib.Path) -> list:
    cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-nostdin"]

    def add_input(fmt: dict):
        headers = fmt.get("http_headers") or {}
        if headers:
            header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            cmd.extend(["-headers", header_str])
        cmd.extend([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", fmt["url"],
        ])

    add_input(video_fmt)
    if audio_fmt:
        add_input(audio_fmt)
        cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])

    cmd.extend([
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "8",
        "-hls_flags", "delete_segments+append_list+independent_segments",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(out_dir / "seg_%08d.ts"),
        str(out_dir / "index.m3u8"),
    ])
    return cmd


async def _drain_stderr(proc: asyncio.subprocess.Process, broadcast_id: str):
    try:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").strip()
            if text:
                logger.debug(f"[Yayın {broadcast_id}] [ffmpeg] {text}")
    except Exception:
        pass


async def _run_once(ing: _Ingest):
    """Tek bir yt-dlp çözümü + ffmpeg çalıştırması. ffmpeg süreci bitene kadar bekler."""
    loop = asyncio.get_event_loop()
    video_fmt, audio_fmt, _info = await loop.run_in_executor(
        None, _extract_formats, ing.youtube_url, ing.quality
    )

    ing.resolved_quality = _describe_quality(video_fmt, audio_fmt)
    logger.info(
        f"[Yayın {ing.broadcast_id}] YouTube kalite çözüldü — istenen: {ing.quality} → "
        f"gerçek: {ing.resolved_quality}"
    )

    ing.out_dir.mkdir(parents=True, exist_ok=True)
    # Önceki koşudan kalan dosyaları temizle (eski segmentler karışmasın)
    for f in ing.out_dir.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass

    cmd = _build_ffmpeg_cmd(video_fmt, audio_fmt, ing.out_dir)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    ing.proc = proc
    stderr_task = asyncio.create_task(_drain_stderr(proc, ing.broadcast_id))

    returncode = await proc.wait()
    await stderr_task
    ing.proc = None

    if ing.should_run:
        logger.warning(
            f"[Yayın {ing.broadcast_id}] ffmpeg (YouTube ingest) sonlandı — çıkış kodu: {returncode}"
        )


async def _supervisor(ing: _Ingest):
    delay = _RESTART_MIN_DELAY
    while ing.should_run:
        try:
            await _run_once(ing)
            delay = _RESTART_MIN_DELAY  # başarılı bir koşu sonrası bekleme süresini sıfırla
        except asyncio.CancelledError:
            raise
        except Exception as e:
            ing.last_error = str(e)
            logger.error(f"[Yayın {ing.broadcast_id}] YouTube ingest hatası: {e}")

        if not ing.should_run:
            break

        ing.restart_count += 1
        logger.warning(
            f"[Yayın {ing.broadcast_id}] YouTube ingest {delay:.0f}sn sonra yeniden denenecek "
            f"(deneme #{ing.restart_count})."
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        delay = min(delay * 1.6, _RESTART_MAX_DELAY)


async def start_ingest(broadcast_id: str, youtube_url: str, quality: str, port: int) -> str:
    """YouTube ingest'i başlatır ve üyelere sunulacak yerel HLS URL'sini döndürür.
    İlk segmentlerin oluşması için kısa bir süre bekler; oluşmazsa hata fırlatır."""
    if broadcast_id in _active:
        await stop_ingest(broadcast_id)

    quality = quality if quality in QUALITY_HEIGHT_MAP else "1080p"
    out_dir = CACHE_ROOT / broadcast_id
    ing = _Ingest(broadcast_id=broadcast_id, youtube_url=youtube_url, quality=quality, out_dir=out_dir)
    _active[broadcast_id] = ing
    ing.task = asyncio.create_task(_supervisor(ing))

    playlist_path = out_dir / "index.m3u8"
    waited = 0.0
    step = 0.5
    while waited < _PLAYLIST_WAIT_TIMEOUT:
        if playlist_path.exists() and playlist_path.stat().st_size > 0:
            break
        if ing.task.done():
            # supervisor'ın ilk denemesi çöktü ve henüz yeniden başlamadıysa bile
            # devam ediyor olabilir; sadece task tamamen bitmişse (should_run False) hata ver
            break
        await asyncio.sleep(step)
        waited += step

    if not (playlist_path.exists() and playlist_path.stat().st_size > 0):
        err = ing.last_error or "zaman aşımı"
        await stop_ingest(broadcast_id)
        raise RuntimeError(
            f"YouTube yayını başlatılamadı ({err}). "
            "Yayının gerçekten canlı olduğundan ve gerekiyorsa Ayarlar → Dosya Ekle "
            "bölümünden güncel bir cookies.txt yüklediğinizden emin olun."
        )

    return f"http://127.0.0.1:{port}/internal/yayin-yt/{broadcast_id}/index.m3u8"


async def stop_ingest(broadcast_id: str) -> None:
    ing = _active.pop(broadcast_id, None)
    if not ing:
        return
    ing.should_run = False
    if ing.proc and ing.proc.returncode is None:
        try:
            ing.proc.terminate()
            await asyncio.wait_for(ing.proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                ing.proc.kill()
            except ProcessLookupError:
                pass
        except Exception:
            pass
    if ing.task and not ing.task.done():
        ing.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(ing.task), timeout=3)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    shutil.rmtree(ing.out_dir, ignore_errors=True)
    logger.info(f"[Yayın {broadcast_id}] YouTube ingest durduruldu.")


def get_resolved_quality(broadcast_id: str) -> str:
    ing = _active.get(broadcast_id)
    return ing.resolved_quality if ing else ""


def is_running(broadcast_id: str) -> bool:
    return broadcast_id in _active


def local_file_path(broadcast_id: str, filename: str) -> Optional[pathlib.Path]:
    """Dahili HTTP route için: path traversal'a kapalı şekilde dosya yolu üretir."""
    safe_name = pathlib.Path(filename).name  # ".."/alt dizinleri at
    path = (CACHE_ROOT / broadcast_id / safe_name).resolve()
    try:
        path.relative_to(CACHE_ROOT.resolve())
    except ValueError:
        return None
    return path
