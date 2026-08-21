import logging
_logger = logging.getLogger(__name__)
"""
yayin_routes.py
───────────────
Sunucu-taraflı HLS/M3U8 proxy yayın sistemi.

Akış:
  1. Admin /api/yayin üzerinden yayın tanımlar (ad, URL, buffer_seconds, vb.)
  2. /api/yayin/<id>/start  → arka planda HLS fetcher başlar, segmentler önbelleğe alınır.
  3. /api/yayin/<id>/stop   → fetcher durdurulur, DB'de active=False → Stremio kataloğundan gizlenir.
  4. Üye    /yayin/stream/<id>/playlist.m3u8?token=<token>  isteğiyle bağlanır.
     - Token doğrulanır, kota kontrol edilir.
     - Doğru EXT-X-MEDIA-SEQUENCE ile canlı HLS manifestosu verilir.
     - Segmentler global sıra numarasıyla adreslendiğinden race condition olmaz.
     - Her segment'in byte boyutu token kotasından düşülür.
"""

import asyncio
import logging
import mimetypes
import os
import re
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from Backend import db
from Backend.fastapi.security.credentials import require_auth
from Backend.fastapi.security.tokens import verify_token

logger = logging.getLogger(__name__)

# Türkiye zaman dilimi (UTC+3)
TZ_TR = timezone(timedelta(hours=3))


def _parse_tr_datetime(dt_str: str) -> Optional[datetime]:
    """
    'YYYY-MM-DDTHH:MM' (HTML datetime-local) veya 'YYYY-MM-DD HH:MM' biçimini
    Türkiye saati olarak parse eder, UTC-aware datetime döndürür.
    Hatalıysa None döner.
    """
    if not dt_str:
        return None
    dt_str = dt_str.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = datetime.strptime(dt_str, fmt)
            return naive.replace(tzinfo=TZ_TR)
        except ValueError:
            continue
    return None


def _now_tr() -> datetime:
    """Türkiye saatiyle şimdiki zamanı döndürür."""
    return datetime.now(tz=TZ_TR)

router = APIRouter(tags=["Yayın"])

# ─── Segment veri yapısı ──────────────────────────────────────────────────────

class Segment:
    """Önbellekteki tek bir HLS segmenti."""
    __slots__ = ("seq", "uri", "data", "duration")

    def __init__(self, seq: int, uri: str, data: bytes, duration: float):
        self.seq      = seq       # Global artan sıra numarası
        self.uri      = uri
        self.data     = data
        self.duration = duration  # #EXTINF değerinden okunan gerçek süre (saniye)


# ─── In-memory broadcast state ────────────────────────────────────────────────

class BroadcastSession:
    """Tek bir yayına ait canlı durum."""

    def __init__(self, broadcast_id: str, stream_url: str, buffer_seconds: int = 30, name: str = ""):
        self.broadcast_id   = broadcast_id
        self.stream_url     = stream_url
        self.buffer_seconds = buffer_seconds
        self.name            = name or "Canlı Yayın"

        # Önbelleğe alınan segmentler: deque of Segment
        self.segments: deque      = deque()
        self.segment_lock         = asyncio.Lock()

        # Global sıra sayacı — hiç sıfırlanmaz
        self._next_seq: int       = 0

        # Durumlar
        self.active               = False
        self.fetcher_task: Optional[asyncio.Task] = None

        # İstatistikler
        self.viewer_count         = 0
        self.total_bytes_served   = 0
        self.segment_size_kb      = 0
        self.buffered_segments    = 0

    def status_dict(self) -> dict:
        return {
            "active":             self.active,
            "buffer_seconds":     self.buffer_seconds,
            "segment_size_kb":    self.segment_size_kb,
            "buffered_segments":  self.buffered_segments,
            "viewer_count":       self.viewer_count,
            "total_bytes_served": self.total_bytes_served,
        }


# broadcast_id → BroadcastSession
_sessions: Dict[str, BroadcastSession] = {}

# ─── İzleyici izleme-geçmişi takibi ───────────────────────────────────────────
# Canlı yayın segmentleri, VOD indirmelerin aksine tek seferlik değil sürekli
# istekler halinde geldiğinden, her segment için ayrı bir stream_analytics
# kaydı açmak yerine "{broadcast_id}::{token}" bazında biriktirilip periyodik
# olarak (ve izleyici ayrıldığında) tek kayıt halinde DB'ye yazılır.
# key → {"broadcast_id","token","title","start_ts","last_ts","total_bytes"}
_viewer_activity: Dict[str, dict] = {}

_VIEWER_IDLE_TIMEOUT   = 25   # saniye — bu süre segment isteği gelmezse izleyici ayrılmış sayılır
_VIEWER_CHECKPOINT_SEC = 300  # saniye — uzun süre izleyen kullanıcı için ara kayıt aralığı


async def _flush_viewer_entry(entry: dict, status: str) -> None:
    """Bir izleyici oturumunu stream_analytics'e (izleme geçmişi) yazar."""
    try:
        total_bytes = entry.get("total_bytes", 0)
        if total_bytes <= 0:
            return
        duration = max(time.time() - entry["start_ts"], 0.001)
        # NOT: "avg_mbps"/"peak_mbps" alanları bu kod tabanında MB/s (megabayt/sn)
        # anlamında kullanılıyor — stream_routes.py / custom_dl.py ile aynı birim.
        avg_mbps = round((total_bytes / (1024 * 1024)) / duration, 3)
        log_entry = {
            "stream_id":    f"yayin:{entry['broadcast_id']}:{entry['token']}:{int(entry['start_ts'])}",
            "msg_id":       None,
            "chat_id":      None,
            "dc_id":        None,
            "client_index": None,
            "total_bytes":  total_bytes,
            "duration":     duration,
            "avg_mbps":     avg_mbps,
            "peak_mbps":    avg_mbps,
            "status":       status,
            "parallelism":  1,
            "chunk_size":   None,
            "meta": {
                "title":      entry.get("title") or "Canlı Yayın",
                "user_token": entry.get("token"),
            },
        }
        await db.log_stream_stats(log_entry)
    except Exception as e:
        logger.warning(f"[İzleyici-Kayıt] Kayıt hatası: {e}")


async def _viewer_flush_loop():
    """
    Periyodik olarak aktif canlı yayın izleyicilerini kontrol eder:
    - Belirli süre segment isteği gelmeyen izleyiciler "ayrıldı" sayılır ve
      izleme geçmişine (stream_analytics) kaydedilir.
    - Uzun süredir kesintisiz izleyenler için ara kayıt (checkpoint) atılır.
    """
    logger.info("[İzleyici-Kayıt] Başlatıldı — canlı yayın izleme geçmişi periyodik olarak kaydediliyor.")
    while True:
        try:
            await asyncio.sleep(15)
            now_ts = time.time()

            stale_keys = []
            for key, entry in list(_viewer_activity.items()):
                idle    = now_ts - entry["last_ts"]
                watched = now_ts - entry["start_ts"]

                if idle > _VIEWER_IDLE_TIMEOUT:
                    stale_keys.append(key)
                elif watched >= _VIEWER_CHECKPOINT_SEC:
                    await _flush_viewer_entry(entry, status="active")
                    entry["start_ts"]    = now_ts
                    entry["total_bytes"] = 0

            for key in stale_keys:
                entry = _viewer_activity.pop(key, None)
                if entry:
                    await _flush_viewer_entry(entry, status="finished")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[İzleyici-Kayıt] Döngü hatası: {e}")

    logger.info("[İzleyici-Kayıt] Durduruldu.")


# ─── Zamanlayıcı (scheduler) ──────────────────────────────────────────────────

_scheduler_task: Optional[asyncio.Task] = None
_viewer_flush_task: Optional[asyncio.Task] = None


async def _scheduler_loop():
    """
    Her 30 saniyede bir tüm yayınları kontrol eder.
    Her yayının 'schedules' alanı: [{"start": "YYYY-MM-DDTHH:MM", "stop": "YYYY-MM-DDTHH:MM"}, ...]
    - Herhangi bir çiftin başlangıcı gelmiş + bitişi geçmemişse → başlat
    - Aktif yayının tüm çiftlerinin bitişi geçmişse → durdur
    Türkiye saati (UTC+3) kullanılır.
    """
    logger.info("[Zamanlayıcı] Başlatıldı — zamanlanmış yayınları izliyor (Türkiye saati).")
    while True:
        try:
            await asyncio.sleep(30)
            broadcasts = await db.get_broadcasts()
            now = _now_tr()

            for bc in broadcasts:
                bid = bc.get("_id", "")
                if not bid:
                    continue

                schedules = bc.get("schedules") or []
                # Eski tek-çift alanlarını da destekle (geriye dönük uyumluluk)
                if not schedules:
                    s = bc.get("scheduled_start", "")
                    e = bc.get("scheduled_stop",  "")
                    if s or e:
                        schedules = [{"start": s, "stop": e}]

                if not schedules:
                    continue

                is_active = bc.get("active", False)
                name = bc.get("name", "?")

                # Her çifti değerlendir
                should_be_active = False
                for pair in schedules:
                    start_dt = _parse_tr_datetime(pair.get("start", ""))
                    stop_dt  = _parse_tr_datetime(pair.get("stop",  ""))

                    if not start_dt:
                        continue

                    started = now >= start_dt
                    stopped = bool(stop_dt) and now >= stop_dt

                    if started and not stopped:
                        should_be_active = True
                        break

                # ── Başlatma ──
                if should_be_active and not is_active:
                    logger.info(f"[Zamanlayıcı] '{name}' ({bid}) başlatılıyor.")
                    try:
                        await _start_session(bid, bc)
                        await db.update_broadcast(bid, {"active": True})
                    except Exception as e:
                        logger.error(f"[Zamanlayıcı] Başlatma hatası ({bid}): {e}")

                # ── Durdurma ──
                elif not should_be_active and is_active:
                    # Sadece zamanlamadan kaynaklanan durdurma:
                    # schedules varsa ve hiçbir aktif pencere yoksa durdur
                    logger.info(f"[Zamanlayıcı] '{name}' ({bid}) durduruluyor.")
                    await _stop_session(bid)
                    await db.update_broadcast(bid, {"active": False})

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Zamanlayıcı] Döngü hatası: {e}")

    logger.info("[Zamanlayıcı] Durduruldu.")


def start_scheduler():
    """Uygulama başlarken çağrılır — scheduler task'ı başlatır."""
    global _scheduler_task, _viewer_flush_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())
    if _viewer_flush_task is None or _viewer_flush_task.done():
        _viewer_flush_task = asyncio.create_task(_viewer_flush_loop())


def stop_scheduler():
    """Uygulama kapanırken çağrılır — scheduler task'ı durdurur."""
    global _scheduler_task, _viewer_flush_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
    if _viewer_flush_task and not _viewer_flush_task.done():
        _viewer_flush_task.cancel()

# ─── Yardımcı fonksiyonlar ────────────────────────────────────────────────────

def _base_url(url: str) -> str:
    """Verilen URL'nin base'ini döndürür (son / dahil)."""
    parts = url.split("?")[0].rsplit("/", 1)
    return parts[0] + "/" if len(parts) > 1 else ""


def _detect_stream_type(url: str, content_type: str, body_start: bytes) -> str:
    """
    URL ve yanıt içeriğine bakarak stream tipini tespit eder.
    Dönüş değerleri:
      'hls'     — standart HLS M3U8 segment listesi
      'master'  — HLS master playlist (EXT-X-STREAM-INF içerir)
      'ts'      — doğrudan MPEG-TS akışı
      'xtream'  — Xtream Codes formatı (host:port/user/pass/id)
    """
    url_lower = url.lower().split("?")[0]

    # İçerik tipine göre hızlı karar
    ct = (content_type or "").lower()
    if "mpegts" in ct or "octet-stream" in ct:
        return "ts"

    # Body başına bak
    try:
        head = body_start.decode("utf-8", errors="ignore")
    except Exception:
        head = ""

    if head.startswith("#EXTM3U"):
        if "EXT-X-STREAM-INF" in head or "EXT-X-MEDIA" in head:
            return "master"
        if "EXTINF" in head or "EXT-X-TARGETDURATION" in head:
            return "hls"
        # #EXTM3U var ama segment yok → Xtream master
        return "master"

    # İlk byte MPEG-TS sync byte kontrolü (0x47) — URL kontrolünden ÖNCE
    if body_start and body_start[0:1] == b"\x47":
        return "ts"

    # URL yapısına göre: /user/pass/<sayı> → Xtream TS
    # Örn: http://host:8080/user/pass/361367
    parts = url_lower.rstrip("/").rsplit("/", 3)
    if len(parts) == 4:
        segment = parts[-1]
        if segment.isdigit():
            return "xtream"

    return "hls"  # varsayılan


def _pick_best_hls_from_master(manifest: str, base: str) -> str:
    """
    Master playlist'ten en yüksek bant genişlikli HLS stream URL'ini seçer.
    Bulamazsa ilk stream URL'ini döner.
    Relative URL'leri (../ dahil) urllib.parse.urljoin ile doğru resolve eder.
    """
    from urllib.parse import urljoin
    best_url = None
    best_bw = -1
    lines = manifest.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            bw = 0
            m = re.search(r"BANDWIDTH=(\d+)", line)
            if m:
                bw = int(m.group(1))
            # Bir sonraki satır URL
            for j in range(i + 1, len(lines)):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    stream_url = nxt if nxt.startswith("http") else urljoin(base + "/", nxt)
                    if bw > best_bw:
                        best_bw = bw
                        best_url = stream_url
                    break
    return best_url or ""


def _parse_m3u8(manifest: str, base: str) -> List[Tuple[str, float]]:
    """
    M3U8 metnini parse eder.
    Returns: [(uri, duration_seconds), ...]
    Sadece yeni, daha önce görülmemiş segment'lerin listesi döner.
    """
    result = []
    pending_duration = 2.0  # varsayılan, #EXTINF bulunamazsa
    for line in manifest.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            # #EXTINF:6.006, veya #EXTINF:6.006000,title
            try:
                pending_duration = float(line[8:].split(",")[0])
            except ValueError:
                pending_duration = 2.0
        elif line and not line.startswith("#"):
            uri = line if line.startswith("http") else base + line
            result.append((uri, pending_duration))
            pending_duration = 2.0
    return result


# ─── HLS Fetcher ──────────────────────────────────────────────────────────────

async def _hls_fetcher(session: BroadcastSession):
    """
    Arka planda çalışır. Desteklenen URL tipleri:

      • Standart HLS (.m3u8)  — manifest indir → segment listesi parse et → indir
      • Master HLS playlist   — en iyi alt-stream seç → HLS moduna geç
      • Xtream Codes IPTV     — host:port/user/pass/id → önce .m3u8 dene, çalışmazsa TS akışı
      • Doğrudan MPEG-TS      — byte akışı → buffer_seconds'lık parçalara böl
    """
    seen_uris: set = set()
    base           = _base_url(session.stream_url)
    poll_interval  = 2.0
    active_url     = session.stream_url   # master sonrası değişebilir
    stream_mode    = None                  # 'hls' | 'ts' — ilk istekten belirlenir

    # MPEG-TS mod parametreleri
    # TS chunk süresi: buffer'ın 1/4'ü, min 2s, max 6s
    # Örn: buffer=10→2s, buffer=20→5s, buffer=30→6s (max)
    TS_CHUNK_SECONDS = max(2, min(session.buffer_seconds // 4, 6))
    ts_bitrate_bps   = 2_000_000   # 2 Mbit/s başlangıç tahmini

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        headers={"User-Agent": "Mozilla/5.0 (compatible; HLSProxy/1.0)"},
    ) as client:

        # ── Adım 1: Stream tipini tespit et ──────────────────────────────────
        while session.active and stream_mode is None:
            try:
                probe_url = active_url
                url_lower = active_url.lower().split("?")[0]

                # Xtream Codes URL'si mi? (son parça sayısal → /user/pass/12345)
                # .ts veya .m3u8 uzantısını soyarak kontrol et
                _url_stem = url_lower.rstrip("/")
                if _url_stem.endswith(".ts") or _url_stem.endswith(".m3u8"):
                    _url_stem = _url_stem.rsplit(".", 1)[0]
                is_xtream = (
                    not url_lower.endswith(".m3u8")
                    and _url_stem.rsplit("/", 1)[-1].isdigit()
                )

                if is_xtream:
                    base_xtream = active_url.rstrip("/")

                    # 1) .m3u8 dene
                    m3u8_url = base_xtream + ".m3u8"
                    logger.info(f"[Yayın {session.broadcast_id}] Xtream URL tespit edildi, M3U8 deneniyor: {m3u8_url}")
                    try:
                        resp_m3u8 = await client.get(m3u8_url)
                        if resp_m3u8.status_code == 200:
                            ct_m  = resp_m3u8.headers.get("content-type", "")
                            body512 = resp_m3u8.content[:512]
                            # Content-type ne olursa olsun, body'ye bak (Xtream bazen octet-stream döner)
                            try:
                                head512 = body512.decode("utf-8", errors="ignore")
                            except Exception:
                                head512 = ""
                            if head512.startswith("#EXTM3U"):
                                # Gerçek M3U8 içeriği var — content-type'ı görmezden gel
                                det_m = _detect_stream_type(m3u8_url, "application/vnd.apple.mpegurl", body512)
                            else:
                                det_m = _detect_stream_type(m3u8_url, ct_m, body512)
                            if det_m == "master":
                                chosen = _pick_best_hls_from_master(resp_m3u8.text, _base_url(m3u8_url))
                                if chosen:
                                    logger.info(f"[Yayın {session.broadcast_id}] Xtream Master → HLS seçildi: {chosen}")
                                    active_url  = chosen
                                    base        = _base_url(chosen)
                                    stream_mode = "hls"
                                else:
                                    active_url  = m3u8_url
                                    base        = _base_url(m3u8_url)
                                    stream_mode = "hls"
                            elif det_m == "hls":
                                active_url  = m3u8_url
                                base        = _base_url(m3u8_url)
                                stream_mode = "hls"
                            # det_m == "ts" → aşağıya düş
                    except Exception as e_m3u8:
                        logger.debug(f"[Yayın {session.broadcast_id}] Xtream .m3u8 exception: {e_m3u8}")

                    if stream_mode is None:
                        # 2) .ts GET isteğiyle kontrol et (HEAD güvenilmez — Xtream'de her zaman 200 dönebilir)
                        ts_url = base_xtream + ".ts"
                        logger.info(f"[Yayın {session.broadcast_id}] Xtream .m3u8 başarısız → .ts deneniyor: {ts_url}")
                        try:
                            async with client.stream(
                                "GET", ts_url,
                                timeout=httpx.Timeout(connect=10, read=5, write=10, pool=10),
                            ) as resp_ts:
                                if resp_ts.status_code in (200, 206):
                                    # İlk birkaç byte'ı oku — gerçekten veri var mı?
                                    first_bytes = b""
                                    async for chunk in resp_ts.aiter_bytes(chunk_size=512):
                                        first_bytes = chunk
                                        break
                                    if first_bytes:
                                        logger.info(f"[Yayın {session.broadcast_id}] Xtream .ts GET OK → TS akış modu")
                                        active_url  = ts_url
                                        stream_mode = "ts"
                                    else:
                                        logger.info(f"[Yayın {session.broadcast_id}] Xtream .ts boş yanıt → uzantısız deneniyor")
                                else:
                                    logger.info(f"[Yayın {session.broadcast_id}] Xtream .ts HTTP {resp_ts.status_code} → uzantısız deneniyor")
                        except Exception as e_ts:
                            logger.debug(f"[Yayın {session.broadcast_id}] Xtream .ts exception: {e_ts}")

                    if stream_mode is None:
                        # 3) Uzantısız direkt TS akışı olarak dene (orijinal URL)
                        logger.info(f"[Yayın {session.broadcast_id}] Xtream → Uzantısız TS akışı deneniyor: {active_url}")
                        stream_mode = "ts"

                    logger.info(f"[Yayın {session.broadcast_id}] Stream modu: {stream_mode} → {active_url}")
                    continue  # while döngüsü → stream_mode set, döngüden çıkılır

                # ── Xtream dışı URL ──────────────────────────────────────────
                resp = await client.get(probe_url)
                ct         = resp.headers.get("content-type", "")
                body_start = resp.content[:512]
                detected   = _detect_stream_type(probe_url, ct, body_start)

                if detected == "master":
                    chosen = _pick_best_hls_from_master(resp.text, _base_url(probe_url))
                    if chosen:
                        logger.info(f"[Yayın {session.broadcast_id}] Master playlist → HLS seçildi: {chosen}")
                        active_url  = chosen
                        base        = _base_url(chosen)
                        stream_mode = "hls"
                    else:
                        logger.warning(f"[Yayın {session.broadcast_id}] Master'dan stream seçilemedi → HLS olarak denenecek: {probe_url}")
                        active_url  = probe_url
                        base        = _base_url(probe_url)
                        stream_mode = "hls"

                elif detected == "hls":
                    active_url  = probe_url
                    base        = _base_url(probe_url)
                    stream_mode = "hls"

                else:
                    # ts / bilinmeyen → TS akış modu
                    active_url  = session.stream_url
                    stream_mode = "ts"

                logger.info(f"[Yayın {session.broadcast_id}] Stream modu: {stream_mode} → {active_url}")

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"[Yayın {session.broadcast_id}] Tip tespiti hatası: {e} — 5 sn sonra tekrar")
                await asyncio.sleep(5)

        # ── Adım 2a: HLS modu ────────────────────────────────────────────────
        if stream_mode == "hls":
            while session.active:
                try:
                    resp = await client.get(active_url)
                    resp.raise_for_status()
                    manifest = resp.text

                    # Beklenmedik master playlist gelirse alt-stream seç
                    if "EXT-X-STREAM-INF" in manifest:
                        chosen = _pick_best_hls_from_master(manifest, _base_url(active_url))
                        if chosen:
                            active_url = chosen
                            base = _base_url(chosen)
                        continue

                    all_segs = _parse_m3u8(manifest, base)
                    new_segs = [(uri, dur) for uri, dur in all_segs if uri not in seen_uris]

                    for uri, duration in new_segs:
                        if not session.active:
                            break
                        try:
                            seg_resp = await client.get(uri)
                            seg_resp.raise_for_status()
                            data = seg_resp.content

                            async with session.segment_lock:
                                seq = session._next_seq
                                session._next_seq += 1
                                seg = Segment(seq=seq, uri=uri, data=data, duration=duration)
                                session.segments.append(seg)
                                session.segment_size_kb   = len(data) // 1024
                                session.buffered_segments = len(session.segments)

                            seen_uris.add(uri)
                            poll_interval = max(1.0, duration / 2)

                            logger.debug(
                                f"[Yayın {session.broadcast_id}] HLS seg#{seq} "
                                f"{uri[-50:]} ({len(data)//1024} KB, {duration:.1f}s)"
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.warning(f"[Yayın {session.broadcast_id}] Segment indirme hatası: {e}")

                    # Eski segmentleri temizle
                    # Kural: en az 15 segment tut; üst limit buffer_seconds+10 sn
                    # ama segment sayısı 15'in katına (15, 30, 45…) yuvarlanır
                    async with session.segment_lock:
                        segs_list = list(session.segments)
                        total_dur = sum(s.duration for s in segs_list)
                        MIN_SEGS  = 15
                        while len(segs_list) > MIN_SEGS and total_dur > session.buffer_seconds + 10:
                            removed = segs_list.pop(0)
                            total_dur -= removed.duration
                        # Segment sayısını 15'in katına yuvarla (aşağı)
                        target = max(MIN_SEGS, (len(segs_list) // MIN_SEGS) * MIN_SEGS)
                        while len(segs_list) > target:
                            segs_list.pop(0)
                        session.segments = deque(segs_list)
                        session.buffered_segments = len(session.segments)

                    # seen_uris'i temizle: yalnızca kaynak playlistte artık hiç olmayan
                    # (ve önbellekten de düşmüş) URI'leri at.
                    # ESKİ KOD: seen_uris &= active_uris → kaynak hızlı ilerlerse
                    # seen_uris boşalıyor, eski segmentler tekrar indiriliyor,
                    # duplicate seq → player 404 alıp donuyordu.
                    # DÜZELTME: sadece son manifest'teki URI'lerin dışında kalanları tut,
                    # böylece hiçbir zaman geriye gidilmez.
                    current_manifest_uris = {uri for uri, _ in all_segs}
                    # seen_uris'e yeni manifest'tekileri ekle (zaten ekli olanları korur)
                    # Ve çok eskimiş (hem manifest'te yok hem önbellekte yok) olanları sil
                    stale = seen_uris - current_manifest_uris - {s.uri for s in session.segments}
                    seen_uris -= stale

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[Yayın {session.broadcast_id}] HLS fetcher hatası: {e}")
                    poll_interval = 3.0

                # Yeni segment bulunamadıysa hızlı tekrar dene; aksi hâlde normal bekle
                # Önbellek dolmamışsa agresif polling yap
                async with session.segment_lock:
                    buf_total = sum(s.duration for s in session.segments)
                if buf_total < session.buffer_seconds * 0.5:
                    # Buffer yarıdan azsa hızlı polling — oynatıcı açılıyor olabilir
                    await asyncio.sleep(min(poll_interval, 1.0))
                elif not new_segs:
                    await asyncio.sleep(min(poll_interval, 2.0))
                else:
                    await asyncio.sleep(poll_interval)

        # ── Adım 2b: MPEG-TS akış modu ───────────────────────────────────────
        elif stream_mode == "ts":
            logger.info(f"[Yayın {session.broadcast_id}] MPEG-TS akış modu başladı ({TS_CHUNK_SECONDS}s chunk)")
            reconnect_delay = 1.0   # her başarısız denemede artar, max 10 sn
            total_received  = 0     # bu oturumda toplam alınan byte

            while session.active:
                try:
                    chunk_bytes = int(ts_bitrate_bps * TS_CHUNK_SECONDS / 8)
                    chunk_bytes = max(chunk_bytes, 188 * 200)        # en az 200 TS paketi
                    chunk_bytes = (chunk_bytes // 188) * 188          # 188 byte'ın katı

                    buf = bytearray()
                    chunk_start = asyncio.get_event_loop().time()
                    connect_time = asyncio.get_event_loop().time()
                    bytes_this_conn = 0

                    # Tek bağlantıda akışı sonuna kadar oku — döngü başına dönme
                    async with client.stream(
                        "GET", active_url,
                        timeout=httpx.Timeout(connect=15, read=30, write=10, pool=10),
                        headers={"User-Agent": "Mozilla/5.0 (compatible; HLSProxy/1.0)",
                                 "Connection": "keep-alive"},
                    ) as resp:
                        resp.raise_for_status()
                        async for raw in resp.aiter_bytes(chunk_size=65536):
                            if not session.active:
                                break
                            buf.extend(raw)
                            bytes_this_conn += len(raw)
                            total_received  += len(raw)

                            while len(buf) >= chunk_bytes:
                                data  = bytes(buf[:chunk_bytes])
                                buf   = buf[chunk_bytes:]

                                elapsed = asyncio.get_event_loop().time() - chunk_start
                                chunk_start = asyncio.get_event_loop().time()

                                async with session.segment_lock:
                                    seq = session._next_seq
                                    session._next_seq += 1
                                    seg = Segment(
                                        seq=seq,
                                        uri=f"ts_chunk_{seq}",
                                        data=data,
                                        duration=float(TS_CHUNK_SECONDS),
                                    )
                                    session.segments.append(seg)
                                    session.segment_size_kb   = len(data) // 1024
                                    session.buffered_segments = len(session.segments)

                                # Gerçek bit hızını güncelle
                                ts_bitrate_bps = max(500_000, int(len(data) * 8 / TS_CHUNK_SECONDS))

                                # Eski segmentleri temizle
                                # Kural: en az 15 segment tut; üst limit buffer_seconds+10 sn
                                # ama segment sayısı 15'in katına (15, 30, 45…) yuvarlanır
                                async with session.segment_lock:
                                    segs_list = list(session.segments)
                                    total_dur = sum(s.duration for s in segs_list)
                                    MIN_SEGS  = 15
                                    while len(segs_list) > MIN_SEGS and total_dur > session.buffer_seconds + 10:
                                        segs_list.pop(0)
                                        total_dur -= TS_CHUNK_SECONDS
                                    # Segment sayısını 15'in katına yuvarla (aşağı)
                                    target = max(MIN_SEGS, (len(segs_list) // MIN_SEGS) * MIN_SEGS)
                                    while len(segs_list) > target:
                                        segs_list.pop(0)
                                    session.segments = deque(segs_list)
                                    session.buffered_segments = len(session.segments)

                                logger.debug(
                                    f"[Yayın {session.broadcast_id}] TS chunk#{seq} "
                                    f"({len(data)//1024} KB)"
                                )

                    # Akış kapandı — ne kadar veri geldi?
                    conn_duration = asyncio.get_event_loop().time() - connect_time
                    if session.active:
                        if bytes_this_conn == 0:
                            # Hiç veri gelmedi → sunucu bu URL'yi kabul etmiyor
                            logger.warning(
                                f"[Yayın {session.broadcast_id}] TS bağlantısı veri vermeden kapandı "
                                f"(conn={conn_duration:.1f}s, url={active_url}) — "
                                f"{reconnect_delay:.0f}s beklenip yeniden denenecek"
                            )
                            await asyncio.sleep(reconnect_delay)
                            reconnect_delay = min(reconnect_delay * 2, 10.0)
                        else:
                            logger.warning(
                                f"[Yayın {session.broadcast_id}] TS akışı kapandı "
                                f"({bytes_this_conn // 1024} KB, {conn_duration:.1f}s) — yeniden bağlanıyor"
                            )
                            reconnect_delay = 1.0  # başarılı bağlantı sonrası sıfırla
                            await asyncio.sleep(1)

                except asyncio.CancelledError:
                    break
                except httpx.HTTPStatusError as e:
                    status_code = e.response.status_code
                    # 404 ve .ts uzantılıysa → uzantısız orijinal URL'ye geç (kalıcı fallback)
                    if status_code == 404 and active_url.endswith(".ts"):
                        fallback_url = active_url[:-3]  # ".ts" kaldır
                        logger.warning(
                            f"[Yayın {session.broadcast_id}] .ts URL 404 → uzantısız URL'ye geçiliyor: {fallback_url}"
                        )
                        active_url      = fallback_url
                        reconnect_delay = 1.0
                        await asyncio.sleep(1)
                    else:
                        logger.error(
                            f"[Yayın {session.broadcast_id}] TS HTTP {status_code} hatası "
                            f"({active_url}) — {reconnect_delay:.0f}s sonra yeniden bağlanıyor"
                        )
                        await asyncio.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 10.0)
                except Exception as e:
                    logger.error(f"[Yayın {session.broadcast_id}] TS akış hatası: {e} — {reconnect_delay:.0f}s sonra yeniden bağlanıyor")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 10.0)

    logger.info(f"[Yayın {session.broadcast_id}] Fetcher durdu.")


# ─── Admin: CRUD ──────────────────────────────────────────────────────────────

@router.get("/api/yayin")
async def yayin_list(_: bool = Depends(require_auth)):
    """Tüm yayınları listele."""
    broadcasts = await db.get_broadcasts()
    return {"broadcasts": broadcasts}


@router.post("/api/yayin")
async def yayin_add(payload: dict, _: bool = Depends(require_auth)):
    """Yeni yayın ekle."""
    payload.setdefault("buffer_seconds", 30)
    payload.setdefault("active", False)
    bc = await db.add_broadcast(payload)
    return bc


@router.put("/api/yayin/{broadcast_id}")
async def yayin_update(broadcast_id: str, payload: dict, _: bool = Depends(require_auth)):
    """Yayını güncelle."""
    ok = await db.update_broadcast(broadcast_id, payload)
    if not ok:
        raise HTTPException(status_code=404, detail="Yayın bulunamadı")
    # Eğer session aktifse, URL veya buffer değişmiş olabilir; restart et
    if broadcast_id in _sessions:
        session = _sessions[broadcast_id]
        if session.active:
            await _stop_session(broadcast_id)
            updated = await db.get_broadcast(broadcast_id)
            if updated:
                await _start_session(broadcast_id, updated)
    return {"ok": True}


@router.delete("/api/yayin/{broadcast_id}")
async def yayin_delete(broadcast_id: str, _: bool = Depends(require_auth)):
    """Yayını sil."""
    await _stop_session(broadcast_id)
    ok = await db.delete_broadcast(broadcast_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Yayın bulunamadı")
    return {"ok": True}


# ─── Admin: Start / Stop ──────────────────────────────────────────────────────

async def _start_session(broadcast_id: str, bc: dict):
    # buffer_seconds çok küçükse (< 10) oynatıcı segmentlere yetişemez → minimum 10 zorla
    # Ek: 15 segment * TS_CHUNK_SECONDS (2-6s) = en az 30s buffer gerekebilir → minimum 30 zorla
    buffer_secs = max(30, int(bc.get("buffer_seconds", 30)))
    session = BroadcastSession(
        broadcast_id   = broadcast_id,
        stream_url     = bc["stream_url"],
        buffer_seconds = buffer_secs,
        name           = bc.get("name", "Canlı Yayın"),
    )
    session.active = True
    task = asyncio.create_task(_hls_fetcher(session))
    session.fetcher_task = task
    _sessions[broadcast_id] = session
    logger.info(f"[Yayın {broadcast_id}] Başlatıldı → {bc['stream_url']}")


async def _stop_session(broadcast_id: str):
    session = _sessions.pop(broadcast_id, None)
    if session:
        session.active = False
        if session.fetcher_task and not session.fetcher_task.done():
            session.fetcher_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(session.fetcher_task), timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info(f"[Yayın {broadcast_id}] Durduruldu.")

    # Yayın durunca, o yayına ait bekleyen izleyici oturumlarını hemen
    # izleme geçmişine yaz — sonraki flush döngüsünü (≤40 sn) bekleme.
    stale_keys = [k for k, v in _viewer_activity.items() if v.get("broadcast_id") == broadcast_id]
    for key in stale_keys:
        entry = _viewer_activity.pop(key, None)
        if entry:
            await _flush_viewer_entry(entry, status="finished")


@router.post("/api/yayin/{broadcast_id}/start")
async def yayin_start(broadcast_id: str, _: bool = Depends(require_auth)):
    """Yayını başlat: fetcher'ı çalıştır, DB'de active=True yap."""
    bc = await db.get_broadcast(broadcast_id)
    if not bc:
        raise HTTPException(status_code=404, detail="Yayın bulunamadı")
    if not bc.get("stream_url"):
        raise HTTPException(status_code=400, detail="Yayın URL'si tanımlanmamış")

    if broadcast_id in _sessions and _sessions[broadcast_id].active:
        return {"ok": True, "message": "Zaten yayında"}

    await _start_session(broadcast_id, bc)
    await db.update_broadcast(broadcast_id, {"active": True})
    actual_buf = max(30, int(bc.get("buffer_seconds", 30)))
    return {"ok": True, "message": "Yayın başlatıldı", "buffer_seconds": actual_buf}


@router.post("/api/yayin/{broadcast_id}/stop")
async def yayin_stop(broadcast_id: str, _: bool = Depends(require_auth)):
    """Yayını durdur: fetcher'ı kapat, DB'de active=False yap."""
    await _stop_session(broadcast_id)
    ok = await db.update_broadcast(broadcast_id, {"active": False})
    if not ok:
        raise HTTPException(status_code=404, detail="Yayın bulunamadı")
    return {"ok": True, "message": "Yayın durduruldu"}


@router.get("/api/yayin/{broadcast_id}/status")
async def yayin_status(broadcast_id: str, _: bool = Depends(require_auth)):
    """Yayın durumunu döndür."""
    session = _sessions.get(broadcast_id)
    if session:
        return session.status_dict()
    bc = await db.get_broadcast(broadcast_id)
    if not bc:
        raise HTTPException(status_code=404, detail="Yayın bulunamadı")
    return {
        "active":             False,
        "buffer_seconds":     bc.get("buffer_seconds", 30),
        "segment_size_kb":    0,
        "buffered_segments":  0,
        "viewer_count":       0,
        "total_bytes_served": 0,
    }


# ─── Üye: HLS Proxy Stream ────────────────────────────────────────────────────

@router.head("/yayin/stream/{broadcast_id}/playlist.m3u8")
async def yayin_member_playlist_head(broadcast_id: str, token: str = None):
    """HEAD desteği — bazı oynatıcılar önce HEAD atar."""
    return Response(
        status_code=200,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/yayin/stream/{broadcast_id}/playlist.m3u8")
async def yayin_member_playlist(broadcast_id: str, request: Request, token: str = None):
    """
    Üyeye özel HLS manifestosu döner.

    Duyuru yayınları (is_duyuru=True) için doğrudan /duyuru/hls/ adresine
    yönlendirme yapılır — bunların kendi ffmpeg HLS sunucusu var.

    Kritik davranışlar:
    - EXT-X-MEDIA-SEQUENCE: önbellekteki ilk segmentin global seq numarası
      → Oynatıcı canlı yayında nerede olduğunu doğru takip eder
    - EXT-X-TARGETDURATION: önbellekteki segmentlerin max gerçek süresi
      → Oynatıcı doğru polling aralığı hesaplar
    - Segment URL'leri seq numarasıyla adreslendiğinden deque kayması olmaz
    """
    if not token:
        raise HTTPException(status_code=401, detail="Token gerekli")
    token_data = await verify_token(token)
    if token_data.get("subscription_expired"):
        raise HTTPException(status_code=403, detail="Abonelik süresi dolmuş")
    if token_data.get("limit_exceeded"):
        raise HTTPException(status_code=429, detail="Veri kotası aşıldı")

    # ── Duyuru yayını mı? ──────────────────────────────────────────────────────
    # Broadcast DB'de is_duyuru=True işareti varsa kendi HLS sunucusuna yönlendir.
    try:
        bc = await db.get_broadcast(broadcast_id)
        if bc and bc.get("is_duyuru"):
            duyuru_session_id = bc.get("duyuru_session", "")
            sess = _duyuru_sessions.get(duyuru_session_id)
            if sess and sess.get("proc") and sess["proc"].poll() is None:
                # Duyuru HLS playlist'ini doğrudan proxy et
                hls_dir = _Path(sess["hls_dir"])
                m3u8_path = hls_dir / "stream.m3u8"
                if m3u8_path.exists():
                    base_url = str(request.base_url).rstrip("/")
                    raw = m3u8_path.read_text()
                    # Segment satırlarını absolute URL'ye çevir
                    lines_out = []
                    for line in raw.splitlines():
                        if line.startswith("seg") or (line and not line.startswith("#")):
                            lines_out.append(
                                f"{base_url}/duyuru/hls/{duyuru_session_id}/{line.strip()}"
                            )
                        else:
                            lines_out.append(line)
                    return Response(
                        content="\n".join(lines_out) + "\n",
                        media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                    )
            # Duyuru session'ı artık yok veya process ölmüş
            raise HTTPException(status_code=503, detail="Duyuru yayını aktif değil")
    except HTTPException:
        raise
    except Exception:
        pass  # Normal yayın akışına devam et

    # ── Normal broadcast yayını ────────────────────────────────────────────────
    session = _sessions.get(broadcast_id)
    if not session or not session.active:
        raise HTTPException(status_code=503, detail="Yayın aktif değil")

    async with session.segment_lock:
        segs = list(session.segments)

    if not segs:
        return Response(
            content="Henüz önbellek dolmadı, lütfen bekleyin…",
            status_code=503,
            headers={
                "Retry-After": "3",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    base_url       = str(request.base_url).rstrip("/")
    first_seq      = segs[0].seq
    max_duration   = max((s.duration for s in segs), default=2.0)
    # HLS standardı: TARGETDURATION tamsayı ve max segment süresinden büyük/eşit olmalı
    target_dur_int = max(1, int(max_duration) + 1)

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-ALLOW-CACHE:NO",
        f"#EXT-X-TARGETDURATION:{target_dur_int}",
        f"#EXT-X-MEDIA-SEQUENCE:{first_seq}",
    ]

    for seg in segs:
        seg_url = f"{base_url}/yayin/segment/{broadcast_id}/{seg.seq}?token={token}"
        lines.append(f"#EXTINF:{seg.duration:.3f},")
        lines.append(seg_url)

    # Canlı yayın: EXT-X-ENDLIST OLMAMALI — oynatıcı playlist'i yenilemeye devam eder

    m3u8_content = "\n".join(lines) + "\n"
    return Response(
        content=m3u8_content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma":        "no-cache",
        },
    )


@router.get("/yayin/segment/{broadcast_id}/{seg_seq}")
async def yayin_member_segment(broadcast_id: str, seg_seq: int, token: str = None):
    """
    Üyeye önbellekten global sıra numarasına göre segment gönderir.
    Index yerine seq kullanıldığından deque kaymasında 404 olmaz.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Token gerekli")
    token_data = await verify_token(token)
    if token_data.get("subscription_expired"):
        raise HTTPException(status_code=403, detail="Abonelik süresi dolmuş")
    if token_data.get("limit_exceeded"):
        raise HTTPException(status_code=429, detail="Veri kotası aşıldı")

    session = _sessions.get(broadcast_id)
    if not session or not session.active:
        raise HTTPException(status_code=503, detail="Yayın aktif değil")

    async with session.segment_lock:
        # seq numarasına göre ara — O(n) ama deque genelde 10-20 segment içerir
        seg = next((s for s in session.segments if s.seq == seg_seq), None)

    if seg is None:
        # Segment temizlendi (çok geç istek) veya henüz gelmedi
        # Retry-After ile oynatıcıya kısa süre sonra tekrar denemesi söylenir
        raise HTTPException(
            status_code=404,
            detail="Segment bulunamadı veya süresi doldu",
            headers={"Retry-After": "2", "Cache-Control": "no-cache"},
        )

    byte_count = len(seg.data)

    # Token kullanımını güncelle (arka planda, yanıtı geciktirmemek için)
    asyncio.create_task(db.update_token_usage(token, byte_count))

    # İstatistik güncelle
    session.total_bytes_served += byte_count
    session.viewer_count = max(session.viewer_count, 1)

    # ── İzleme geçmişi: izleyici aktivitesini biriktir ──────────────────────
    # (Üye detay sayfasındaki "İzleme Geçmişi" tablosu stream_analytics'ten
    #  beslendiğinden, canlı yayın izlemelerinin de burada birikip periyodik
    #  olarak _viewer_flush_loop tarafından DB'ye yazılması gerekir.)
    now_ts = time.time()
    viewer_key = f"{broadcast_id}::{token}"
    activity = _viewer_activity.get(viewer_key)
    if activity is None:
        activity = {
            "broadcast_id": broadcast_id,
            "token":        token,
            "title":        session.name,
            "start_ts":     now_ts,
            "last_ts":      now_ts,
            "total_bytes":  0,
        }
        _viewer_activity[viewer_key] = activity
    activity["title"]       = session.name  # yayın adı sonradan değişmiş olabilir
    activity["last_ts"]     = now_ts
    activity["total_bytes"] += byte_count

    # Segment formatını magic bytes'tan otomatik tespit et
    # fMP4 (fragmented MP4): 0x66747970 'ftyp' veya 0x6D6F6F66 'moof' başlangıcı
    # MPEG-TS: 0x47 sync byte ile başlar
    seg_mime = "video/MP2T"  # varsayılan
    if len(seg.data) >= 8:
        hdr = seg.data[:8]
        if hdr[4:8] in (b"ftyp", b"moof", b"styp"):
            seg_mime = "video/mp4"
        elif hdr[0] != 0x47:
            # TS sync byte değil, yine de MP2T dene ama fMP4 değil
            seg_mime = "video/MP2T"

    return Response(
        content=seg.data,
        media_type=seg_mime,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ─── Bekleme Ekranı (Standby) Endpoint'leri ──────────────────────────────────

def _standby_active(bc: dict) -> bool:
    """
    Verilen yayın kaydının bekleme medyasının hâlâ aktif olup olmadığını döndürür.
    - standby_media tanımlı değilse → False
    - duration_seconds == 0 → sınırsız (yayın başlayana kadar göster)
    - duration_seconds > 0  → yayının en erken scheduled_start'ından itibaren sayılır.
      scheduled_start yoksa şimdiki zamandan sayılır (güvenli taraf → göster).
    """
    sm = bc.get("standby_media") or {}
    if not sm.get("path"):
        return False
    dur = sm.get("duration_seconds", 0)
    if dur == 0:
        return True  # sınırsız
    # Süreyi en erken scheduled_start'tan itibaren say
    schedules = bc.get("schedules") or []
    if not schedules:
        s = bc.get("scheduled_start", "")
        if s:
            schedules = [{"start": s}]
    reference_dt = None
    for pair in schedules:
        dt = _parse_tr_datetime(pair.get("start", ""))
        if dt and (reference_dt is None or dt < reference_dt):
            reference_dt = dt
    if reference_dt is None:
        return True  # başlangıç zamanı bilinmiyor → göster
    now = _now_tr()
    elapsed = (now - reference_dt).total_seconds()
    return elapsed < dur


@router.get("/api/yayin/{broadcast_id}/standby-info")
async def yayin_standby_info(broadcast_id: str, _: bool = Depends(require_auth)):
    """Yayın bekleme medya bilgisini döndür."""
    bc = await db.get_broadcast(broadcast_id)
    if not bc:
        raise HTTPException(status_code=404, detail="Yayın bulunamadı")
    sm = bc.get("standby_media") or {}
    return {
        "has_standby": bool(sm.get("path")),
        "active":      _standby_active(bc),
        "standby_media": sm,
    }


@router.get("/yayin/standby/{broadcast_id}/media")
async def yayin_standby_media(broadcast_id: str, token: str = None, _loop: int = 0):
    """
    Üyeye bekleme medyasını gönderir.
    - source=url  → redirect
    - source=server → dosyayı sun
    Yayın aktifse veya bekleme süresi dolmuşsa 404 döner.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Token gerekli")
    token_data = await verify_token(token)
    if token_data.get("subscription_expired"):
        raise HTTPException(status_code=403, detail="Abonelik süresi dolmuş")

    bc = await db.get_broadcast(broadcast_id)
    if not bc:
        raise HTTPException(status_code=404, detail="Yayın bulunamadı")

    # Yayın aktifse standby gösterme (hem in-memory session hem DB durumu kontrol edilir)
    session = _sessions.get(broadcast_id)
    db_active = bc.get("active", False)
    if (session and session.active) or db_active:
        raise HTTPException(status_code=409, detail="Yayın aktif")

    if not _standby_active(bc):
        raise HTTPException(status_code=410, detail="Bekleme süresi doldu")

    sm = bc.get("standby_media") or {}
    path   = sm.get("path", "")
    source = sm.get("source", "url")

    if not path:
        raise HTTPException(status_code=404, detail="Bekleme medyası tanımlı değil")

    if source == "url":
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=path, status_code=302)

    # source == server → dosya sun
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Sunucu dosyası bulunamadı")

    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = "application/octet-stream"

    async def _file_iter():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _file_iter(),
        media_type=mime,
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/yayin/standby/{broadcast_id}/playlist.m3u8")
async def yayin_standby_playlist(broadcast_id: str, request: Request, token: str = None):
    """
    Video bekleme ekranı için döngüsel HLS playlist.

    Davranış:
    - Yayın aktif değil + standby video tanımlı → video URL'sini döngüsel
      canlı HLS olarak sun (EXT-X-ENDLIST YOK).
    - Her playlist yenilemesinde yayın durumu kontrol edilir.
      Yayın başladıysa → HTTP 302 ile canlı stream playlist'ine yönlendir.
      Player yönlendirmeyi takip eder, videoyu keserek canlıya geçer.
    - Sadece video (media_type="video") desteklenir.
      Resim tipi için bu endpoint 404 döndürür.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Token gerekli")
    token_data = await verify_token(token)
    if token_data.get("subscription_expired"):
        raise HTTPException(status_code=403, detail="Abonelik süresi dolmuş")

    bc = await db.get_broadcast(broadcast_id)
    if not bc:
        raise HTTPException(status_code=404, detail="Yayın bulunamadı")

    # ── Yayın başladıysa canlı stream'e yönlendir ──────────────────────────
    session  = _sessions.get(broadcast_id)
    db_active = bc.get("active", False)
    if (session and session.active) or db_active:
        from fastapi.responses import RedirectResponse
        base_url   = str(request.base_url).rstrip("/")
        live_url   = f"{base_url}/yayin/stream/{broadcast_id}/playlist.m3u8?token={token}"
        return RedirectResponse(url=live_url, status_code=302)

    if not _standby_active(bc):
        raise HTTPException(status_code=410, detail="Bekleme süresi doldu")

    sm         = bc.get("standby_media") or {}
    path       = sm.get("path", "")
    media_type = sm.get("media_type", "image")

    if not path:
        raise HTTPException(status_code=404, detail="Bekleme medyası tanımlı değil")

    if media_type != "video":
        raise HTTPException(status_code=404, detail="Sadece video tipi destekleniyor")

    base_url  = str(request.base_url).rstrip("/")
    media_url = f"{base_url}/yayin/standby/{broadcast_id}/media?token={token}"

    # ── Döngüsel canlı HLS playlist ───────────────────────────────────────
    # Videoyu tek segment olarak sun; HLS canlı playlist olarak döndür
    # (EXT-X-ENDLIST yok, EXT-X-PLAYLIST-TYPE yok → player canlı sayar).
    # TARGETDURATION ve MEDIA-SEQUENCE her istemde güncellenir.
    # Player playlist'i yenilediğinde yayın başlamışsa yukarıdaki 302
    # yönlendirmesi devreye girer → kesintisiz geçiş.
    #
    # Video süresi: duration_seconds alanından al; yoksa varsayılan 3600sn.
    dur = int(sm.get("duration_seconds") or 3600)
    # Kaç kez döndü: her döngü seq'i artırır, player süreksizlik görmez
    now_seq = int(time.time()) // max(dur, 1)

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{dur}",
        f"#EXT-X-MEDIA-SEQUENCE:{now_seq}",
        # Şu an oynatılan döngü
        f"#EXTINF:{dur}.000,",
        f"{media_url}&_loop={now_seq}",
        # Bir sonraki döngü (player önceden buffer eder)
        f"#EXTINF:{dur}.000,",
        f"{media_url}&_loop={now_seq + 1}",
    ]
    m3u8 = "\n".join(lines) + "\n"

    return Response(
        content=m3u8,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

# ─── Template route ───────────────────────────────────────────────────────────

from fastapi.templating import Jinja2Templates as _Jinja2Templates
from Backend.fastapi.themes import get_theme as _get_theme, get_all_themes as _get_all_themes
from Backend.config import Telegram as _Telegram
from Backend.fastapi.security.credentials import get_current_user as _get_current_user

_templates = _Jinja2Templates(directory="Backend/fastapi/templates")


@router.get("/yayin")
async def yayin_page(request: Request, _: bool = Depends(require_auth)):
    """Yayın yönetim sayfası."""
    owner_name = None
    try:
        owner = await db.get_user(_Telegram.OWNER_ID)
        owner_name = owner.get("first_name") if owner else None
    except Exception:
        pass

    theme_name   = request.session.get("theme", "purple_gradient")
    theme        = _get_theme(theme_name)
    current_user = _get_current_user(request)

    return _templates.TemplateResponse("yayin.html", {
        "request":       request,
        "theme":         theme,
        "themes":        _get_all_themes(),
        "current_theme": theme_name,
        "app_name":      _Telegram.ISIM,
        "current_user":  current_user,
        "owner_name":    owner_name,
    })


# ══════════════════════════════════════════════════════════════════════════════
# DUYURU YAYINI — FFmpeg loop stream
# ══════════════════════════════════════════════════════════════════════════════
import subprocess as _subprocess
import shutil as _shutil
import tempfile as _tempfile
import uuid as _uuid
import aiofiles as _aiofiles
from pathlib import Path as _Path
from fastapi import UploadFile, File, Form
from fastapi.responses import JSONResponse as _JSONResponse

_DUYURU_DIR = _Path(os.environ.get("SUNUCU_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads"))) / "_duyuru"
_DUYURU_DIR.mkdir(parents=True, exist_ok=True)

# {session_id: {"proc": Popen, "video_path": str, "hls_dir": str}}
_duyuru_sessions: dict = {}


def _ffmpeg_bin() -> str:
    f = _shutil.which("ffmpeg")
    if f:
        return f
    for p in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/app/.venv/bin/ffmpeg"):
        if _Path(p).is_file():
            return p
    raise RuntimeError("ffmpeg bulunamadı")


def _build_video_sync(image_path: str, poster_path: str | None,
                      logo_path: str | None, out_path: str) -> None:
    """
    Verilen yayın resminden 10 saniyelik MP4 üretir.
    Stremio/HLS uyumlu: yuv420p, baseline profile, -loop 1 yöntemi.
    """
    ffmpeg = _ffmpeg_bin()

    # Pillow ile güvenli JPEG'e dönüştür (webp/avif/png uyum sorunlarını giderir)
    safe_image = image_path
    try:
        from PIL import Image as _PILImage
        with _PILImage.open(image_path) as im:
            rgb = im.convert("RGB")
            safe_image = str(_Path(out_path).parent / "_bg_safe.jpg")
            rgb.save(safe_image, "JPEG", quality=92)
    except Exception:
        safe_image = image_path

    vf = (
        "scale=1280:720:force_original_aspect_ratio=decrease,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,"
        "fps=25"
    )

    # -loop 1: tek resmi sonsuz döngüde okuyup -t ile 10 saniyede keser.
    # -profile:v baseline + -level 3.0: maksimum Stremio/HLS uyumu
    cmd = [
        ffmpeg, "-y",
        "-loop", "1",
        "-i", safe_image,
        "-t", "10",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-profile:v", "baseline", "-level", "3.0",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        out_path,
    ]

    result = _subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg hatası: {result.stderr[-800:]}")


def _build_image_clip_sync(image_path: str, duration_sec: int, out_path: str) -> None:
    """
    Tek bir resimden `duration_sec` saniyelik sessiz MP4 klip üretir.
    Çoklu resim slayt gösterisi oluştururken her resim için çağrılır.
    """
    ffmpeg = _ffmpeg_bin()

    # Güvenli JPEG'e dönüştür
    safe_image = image_path
    try:
        from PIL import Image as _PILImage
        with _PILImage.open(image_path) as im:
            rgb = im.convert("RGB")
            safe_image = str(_Path(out_path).parent / f"_safe_{_Path(out_path).stem}.jpg")
            rgb.save(safe_image, "JPEG", quality=92)
    except Exception:
        safe_image = image_path

    vf = (
        "scale=1280:720:force_original_aspect_ratio=decrease,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,"
        "fps=25"
    )
    cmd = [
        ffmpeg, "-y",
        "-loop", "1",
        "-i", safe_image,
        "-t", str(duration_sec),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-profile:v", "baseline", "-level", "3.0",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        out_path,
    ]
    result = _subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg klip hatası: {result.stderr[-800:]}")


def _start_hls_loop_sync(video_path: str, hls_dir: str,
                         has_audio: bool = False,
                         music_path: str | None = None) -> _subprocess.Popen:
    """
    Video/resim dosyasını HLS loop olarak sunar.
    - has_audio  : video kendi sesini taşıyorsa True (video modunda)
    - music_path : opsiyonel müzik dosyası — video üzerine karıştırılır
    ffmpeg -stream_loop -1 → sonsuz döngü → HLS segmentlere böler.

    Video encode notu:
      copy yerine her zaman yeniden encode (libx264 baseline) yapılır.
      Bu sayede kaynak video formatından bağımsız olarak Stremio/HLS uyumu sağlanır.
    """
    ffmpeg = _ffmpeg_bin()
    _Path(hls_dir).mkdir(parents=True, exist_ok=True)
    playlist = str(_Path(hls_dir) / "stream.m3u8")
    hls_common = [
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", str(_Path(hls_dir) / "seg%05d.ts"),
        playlist,
    ]

    # Video encode parametreleri — Stremio/HLS uyumlu
    venc = [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-profile:v", "baseline", "-level", "3.0",
        "-pix_fmt", "yuv420p",
        "-g", "50", "-sc_threshold", "0",   # sabit keyframe aralığı
    ]

    if music_path and _Path(music_path).exists():
        # ── Müzik var ──────────────────────────────────────────────────────
        # Müziği ayrı input olarak ver; stream_loop yerine -shortest ile
        # video döngüsüne eşle. aloop filtresi bazı ffmpeg sürümlerinde
        # stream_loop ile çakıştığı için kullanılmıyor.
        if has_audio:
            # video sesi + müzik → amix
            filter_complex = (
                "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black[v];"
                "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2[a]"
            )
        else:
            # sadece müzik (resim tabanlı video — ses yok)
            filter_complex = (
                "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black[v];"
                "[1:a]aresample=44100[a]"
            )

        cmd = [
            ffmpeg,
            "-re", "-stream_loop", "-1", "-i", video_path,
            "-stream_loop", "-1", "-i", music_path,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            *venc,
            "-c:a", "aac", "-b:a", "128k",
            *hls_common,
        ]
    elif has_audio:
        # ── Video sesli, müzik yok ─────────────────────────────────────────
        cmd = [
            ffmpeg,
            "-re", "-stream_loop", "-1", "-i", video_path,
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                   "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black",
            *venc,
            "-c:a", "aac", "-b:a", "128k",
            *hls_common,
        ]
    else:
        # ── Ses yok, müzik yok (resim tabanlı yayın) ──────────────────────
        cmd = [
            ffmpeg,
            "-re", "-stream_loop", "-1", "-i", video_path,
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                   "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black",
            *venc,
            "-an",
            *hls_common,
        ]

    proc = _subprocess.Popen(
        cmd,
        stdout=_subprocess.DEVNULL,
        stderr=_subprocess.DEVNULL,
    )
    return proc


_UPLOADS_DIR = _Path(os.environ.get("SUNUCU_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")))


async def _resolve_media(
    upload: "UploadFile | None",
    url: "str | None",
    server_path: "str | None",
    dest_path: str,
) -> "str | None":
    """
    Üç kaynaktan birinden medya dosyası alır:
    1. Yüklenen dosya (upload)
    2. HTTP/HTTPS URL
    3. Sunucu yerel yolu (/app/Backend/uploads/ altında güvenlik kontrolü yapılır)
    Hiçbiri yoksa None döner.
    """
    # 1. Yüklenen dosya öncelikli
    if upload and upload.filename:
        async with _aiofiles.open(dest_path, "wb") as out:
            await out.write(await upload.read())
        return dest_path

    # 2. URL'den indir
    if url and url.strip():
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"Geçersiz URL: {url}")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": f"{url.split('/')[0]}//{url.split('/')[2]}/",
        }
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers=headers
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        async with _aiofiles.open(dest_path, "wb") as out:
            await out.write(resp.content)
        return dest_path

    # 3. Sunucu dosya yolu
    if server_path and server_path.strip():
        sp = server_path.strip()
        # Güvenlik: /app/Backend/uploads/ dışına çıkmayı engelle
        allowed_roots = [
            str(_UPLOADS_DIR),
            "/app/Backend/uploads",
            "/app/uploads",
        ]
        resolved = str(_Path(sp).resolve())
        if not any(resolved.startswith(root) for root in allowed_roots):
            raise ValueError(f"İzin verilmeyen dosya yolu: {sp}")
        src = _Path(sp)
        if not src.is_file():
            raise FileNotFoundError(f"Dosya bulunamadı: {sp}")
        _shutil.copy2(str(src), dest_path)
        return dest_path

    return None


@router.get("/api/duyuru/sunucu-dosyalar")
async def duyuru_sunucu_dosyalar(_: bool = Depends(require_auth)):
    """
    /app/Backend/uploads/ klasöründeki resim ve video dosyalarını listeler.
    """
    IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m4v"}
    AUDIO_EXT = {".mp3", ".aac", ".flac", ".ogg", ".wav", ".m4a", ".opus"}
    ALL_EXT = IMAGE_EXT | VIDEO_EXT | AUDIO_EXT

    roots = [_UPLOADS_DIR, _Path("/app/Backend/uploads"), _Path("/app/uploads")]
    files = []
    seen = set()

    for root in roots:
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in ALL_EXT:
                continue
            # _duyuru alt klasörlerini atla
            if "_duyuru" in f.parts:
                continue
            key = str(f.resolve())
            if key in seen:
                continue
            seen.add(key)
            files.append({
                "path": str(f),
                "name": f.name,
                "size": f.stat().st_size,
                "type": "video" if f.suffix.lower() in VIDEO_EXT else ("audio" if f.suffix.lower() in AUDIO_EXT else "image"),
            })

    return _JSONResponse({"ok": True, "files": files})


@router.post("/api/duyuru/hazirla")
async def duyuru_hazirla(
    request: Request,
    _: bool = Depends(require_auth),
    # ── Eski tekli alanlar (geriye dönük uyum) ──────────────────────────────
    image: UploadFile = File(None),
    poster: UploadFile = File(None),
    logo: UploadFile = File(None),
    aciklama: str = Form(default=""),
    image_url: str = Form(default=""),
    poster_url: str = Form(default=""),
    logo_url: str = Form(default=""),
    image_server: str = Form(default=""),
    poster_server: str = Form(default=""),
    logo_server: str = Form(default=""),
    image_is_video: str = Form(default=""),
    # ── Çoklu medya sayısı (yeni arayüz) ────────────────────────────────────
    image_count: str = Form(default=""),
    # ── Resim slayt geçiş süresi (saniye) ───────────────────────────────────
    slide_interval: str = Form(default="5"),
    # ── Müzik — eski tekli alanlar ──────────────────────────────────────────
    music: UploadFile = File(None),
    music_url: str = Form(default=""),
    music_server: str = Form(default=""),
    # ── Çoklu müzik sayısı (yeni arayüz) ────────────────────────────────────
    music_count: str = Form(default=""),
):
    """
    Resim(ler) veya video(lar)dan HLS yayın hazırlar, session_id döner.

    Yeni arayüz: image_count=N gönderilirse image_0…image_{N-1} (File),
    image_url_0…image_url_{N-1} (Form), image_is_video_0… (Form) okunur.
    music_count=M gönderilirse music_0…music_{M-1} ve music_url_0… okunur.

    Eski tekli arayüz (image, image_url, image_server, music…) hâlâ çalışır.
    """
    session_id = _uuid.uuid4().hex
    work_dir = _DUYURU_DIR / session_id
    work_dir.mkdir(parents=True, exist_ok=True)

    VIDEO_EXT_SET = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m4v"}

    try:
        form = await request.form()

        # ── 1. Medya öğelerini topla ─────────────────────────────────────────
        media_items: list[dict] = []   # {"path": str, "is_video": bool}
        n_images = int(image_count) if image_count.strip().isdigit() else 0

        if n_images > 0:
            # Yeni çoklu arayüz
            for i in range(n_images):
                upload_field = form.get(f"image_{i}")
                url_field    = str(form.get(f"image_url_{i}", "") or "").strip()
                is_vid_flag  = str(form.get(f"image_is_video_{i}", "0")) == "1"
                dest = str(work_dir / f"media_{i}.bin")

                resolved_path = None
                if upload_field and hasattr(upload_field, "filename") and upload_field.filename:
                    async with _aiofiles.open(dest, "wb") as f_out:
                        await f_out.write(await upload_field.read())
                    resolved_path = dest
                    # Uzantıdan video tespiti
                    ext = _Path(upload_field.filename).suffix.lower()
                    is_vid_flag = ext in VIDEO_EXT_SET
                elif url_field:
                    resolved_path = await _resolve_media(None, url_field, None, dest)
                    if resolved_path:
                        ext = _Path(url_field.split("?")[0]).suffix.lower()
                        is_vid_flag = is_vid_flag or (ext in VIDEO_EXT_SET)

                if resolved_path:
                    media_items.append({"path": resolved_path, "is_video": is_vid_flag})
        else:
            # Eski tekli arayüz — geriye dönük uyum
            is_vid = (image_is_video == "1") or (
                image_server and _Path(image_server.strip()).suffix.lower() in VIDEO_EXT_SET
            )
            if is_vid:
                src = (image_server or "").strip()
                if not src:
                    raise ValueError("Video modu için sunucu dosya yolu gereklidir.")
                allowed_roots = [str(_UPLOADS_DIR), "/app/Backend/uploads", "/app/uploads"]
                if not any(str(_Path(src).resolve()).startswith(r) for r in allowed_roots):
                    raise ValueError(f"İzin verilmeyen dosya yolu: {src}")
                if not _Path(src).is_file():
                    raise FileNotFoundError(f"Video dosyası bulunamadı: {src}")
                media_items.append({"path": src, "is_video": True})
            else:
                p = await _resolve_media(image, image_url or None, image_server or None,
                                         str(work_dir / "bg.jpg"))
                if p:
                    media_items.append({"path": p, "is_video": False})

        if not media_items:
            raise ValueError("Yayın resmi veya video zorunludur (dosya, URL veya sunucu yolu).")

        # ── 2. Modu belirle ──────────────────────────────────────────────────
        is_video_mode = any(m["is_video"] for m in media_items)

        # ── 3. video_path oluştur ────────────────────────────────────────────
        loop_ev = asyncio.get_event_loop()
        slide_sec = max(1, int(slide_interval) if slide_interval.strip().isdigit() else 5)

        if is_video_mode:
            # Video öğelerini filtrele (resimler varsa atla)
            video_items = [m for m in media_items if m["is_video"]]
            if len(video_items) == 1:
                video_path = video_items[0]["path"]
            else:
                # Birden fazla video → concat listesi oluştur
                concat_file = str(work_dir / "concat.txt")
                with open(concat_file, "w") as cf:
                    for m in video_items:
                        cf.write(f"file '{m['path']}'\n")
                concat_out = str(work_dir / "duyuru.mp4")
                ffmpeg = _ffmpeg_bin()
                result = _subprocess.run([
                    ffmpeg, "-y", "-f", "concat", "-safe", "0",
                    "-i", concat_file,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                    "-profile:v", "baseline", "-level", "3.0",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart",
                    concat_out,
                ], capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    raise RuntimeError(f"ffmpeg concat hatası: {result.stderr[-800:]}")
                video_path = concat_out
        else:
            # Resim modu
            image_paths = [m["path"] for m in media_items]
            if len(image_paths) == 1:
                # Tek resim → eski davranış (10 sn video)
                video_path = str(work_dir / "duyuru.mp4")
                await loop_ev.run_in_executor(
                    None, _build_video_sync, image_paths[0], None, None, video_path,
                )
            else:
                # Birden fazla resim → her resimden slide_sec sn klip, concat
                clip_paths = []
                for idx, img_path in enumerate(image_paths):
                    clip_out = str(work_dir / f"clip_{idx}.mp4")
                    await loop_ev.run_in_executor(
                        None, _build_image_clip_sync, img_path, slide_sec, clip_out,
                    )
                    clip_paths.append(clip_out)

                concat_file = str(work_dir / "concat.txt")
                with open(concat_file, "w") as cf:
                    for cp in clip_paths:
                        cf.write(f"file '{cp}'\n")
                concat_out = str(work_dir / "duyuru.mp4")
                ffmpeg = _ffmpeg_bin()
                result = _subprocess.run([
                    ffmpeg, "-y", "-f", "concat", "-safe", "0",
                    "-i", concat_file,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                    "-profile:v", "baseline", "-level", "3.0",
                    "-pix_fmt", "yuv420p", "-an",
                    "-movflags", "+faststart",
                    concat_out,
                ], capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    raise RuntimeError(f"ffmpeg slayt concat hatası: {result.stderr[-800:]}")
                video_path = concat_out

        # ── 4. Çoklu müzik öğelerini topla ve birleştir ──────────────────────
        n_music = int(music_count) if music_count.strip().isdigit() else 0
        music_paths: list[str] = []

        if n_music > 0:
            for i in range(n_music):
                m_upload = form.get(f"music_{i}")
                m_url    = str(form.get(f"music_url_{i}", "") or "").strip()
                m_dest   = str(work_dir / f"music_{i}.bin")
                mp = None
                if m_upload and hasattr(m_upload, "filename") and m_upload.filename:
                    async with _aiofiles.open(m_dest, "wb") as f_out:
                        await f_out.write(await m_upload.read())
                    mp = m_dest
                elif m_url:
                    mp = await _resolve_media(None, m_url, None, m_dest)
                if mp:
                    music_paths.append(mp)
        else:
            # Eski tekli arayüz
            mp = await _resolve_media(music, music_url or None, music_server or None,
                                      str(work_dir / "music.mp3"))
            if mp:
                music_paths.append(mp)

        # Birden fazla müzik → concat
        if len(music_paths) > 1:
            m_concat_file = str(work_dir / "music_concat.txt")
            with open(m_concat_file, "w") as cf:
                for mp in music_paths:
                    cf.write(f"file '{mp}'\n")
            m_concat_out = str(work_dir / "music_all.mp3")
            ffmpeg = _ffmpeg_bin()
            result = _subprocess.run([
                ffmpeg, "-y", "-f", "concat", "-safe", "0",
                "-i", m_concat_file,
                "-c:a", "libmp3lame", "-b:a", "192k",
                m_concat_out,
            ], capture_output=True, text=True, timeout=300)
            music_path_final = m_concat_out if result.returncode == 0 else music_paths[0]
        elif len(music_paths) == 1:
            music_path_final = music_paths[0]
        else:
            music_path_final = None

        # ── 5. Poster / logo ─────────────────────────────────────────────────
        poster_path = await _resolve_media(
            poster, poster_url or None, poster_server or None,
            str(work_dir / "poster.jpg")
        )
        logo_path = await _resolve_media(
            logo, logo_url or None, logo_server or None,
            str(work_dir / "logo.png")
        )

        _duyuru_sessions[session_id] = {
            "proc":        None,
            "video_path":  video_path,
            "hls_dir":     str(work_dir / "hls"),
            "work_dir":    str(work_dir),
            "poster_path": poster_path,
            "logo_path":   logo_path,
            "aciklama":    aciklama,
            "source_type": "video" if is_video_mode else "image",
            "music_path":  music_path_final,
        }

        return _JSONResponse({
            "ok": True,
            "session_id": session_id,
            "source_type": "video" if is_video_mode else "image",
            "media_count": len(media_items),
            "music_count": len(music_paths),
        })

    except Exception as e:
        _shutil.rmtree(str(work_dir), ignore_errors=True)
        import traceback, logging as _logging
        _logging.getLogger(__name__).error("duyuru_hazirla hatası: %s\n%s", e, traceback.format_exc())
        _logger.error("Internal error", exc_info=True)

        return _JSONResponse({"ok": False, "error": "Sunucu hatası"}, status_code=500)


@router.post("/api/duyuru/baslat/{session_id}")
async def duyuru_baslat(
    session_id: str,
    request: Request,
    _: bool = Depends(require_auth),
):
    """
    HLS loop yayınını başlatır ve Stremio canlı kataloğuna kaydeder.
    Body (JSON, opsiyonel): { "kanal_adi": "...", "order": 0, "logo": "...", "poster": "..." }
    """
    sess = _duyuru_sessions.get(session_id)
    if not sess:
        return _JSONResponse({"ok": False, "error": "Geçersiz session"}, status_code=404)
    if sess.get("proc") and sess["proc"].poll() is None:
        return _JSONResponse({"ok": True, "msg": "Zaten çalışıyor",
                              "stream_url": f"/duyuru/hls/{session_id}/stream.m3u8"})

    # Opsiyonel JSON gövdesi
    try:
        body = await request.json()
    except Exception:
        body = {}
    kanal_adi = (body.get("kanal_adi") or "Duyuru Yayını").strip() or "Duyuru Yayını"
    order     = int(body.get("order") or 0)

    # Önce frontend'den gelen URL'ye bak; yoksa hazirla aşamasında kaydedilen dosyadan
    # /duyuru/media/<session_id>/poster.jpg ve logo.png endpoint'i üzerinden servis edilir
    # Stremio dışarıdan eriştiği için mutlak URL gerekir
    _base_url_str = str(request.base_url).rstrip("/")

    def _media_url(key: str, filename: str) -> str:
        val = body.get(key, "").strip()
        if val:
            return val
        fpath = sess.get(f"{key}_path")
        if fpath and _Path(fpath).exists():
            return f"{_base_url_str}/duyuru/media/{session_id}/{filename}"
        return ""

    logo_url   = _media_url("logo",   "logo.png")
    poster_url = _media_url("poster", "poster.jpg")
    aciklama   = sess.get("aciklama", "")

    loop = asyncio.get_event_loop()
    proc = await loop.run_in_executor(
        None, _start_hls_loop_sync,
            sess["video_path"], sess["hls_dir"],
            sess.get("source_type") == "video",
            sess.get("music_path")
    )
    sess["proc"] = proc

    # m3u8 dosyasının oluşmasını bekle (max 15 sn, 100 ms aralıkla)
    m3u8 = _Path(sess["hls_dir"]) / "stream.m3u8"
    for _ in range(150):
        if m3u8.exists() and m3u8.stat().st_size > 0:
            break
        await asyncio.sleep(0.1)
    else:
        # Zaman aşımı — process çalışıyor mu kontrol et
        if proc.poll() is not None:
            stderr_hint = ""
            return _JSONResponse(
                {"ok": False, "error": "ffmpeg HLS başlatılamadı (process erken çıktı)."},
                status_code=500,
            )
        # Process hâlâ çalışıyor ama m3u8 yok → biraz daha bekle
        await asyncio.sleep(2)
        if not m3u8.exists():
            return _JSONResponse(
                {"ok": False, "error": "HLS playlist oluşturulamadı (zaman aşımı)."},
                status_code=500,
            )

    stream_url = f"/duyuru/hls/{session_id}/stream.m3u8"

    # Broadcasts DB'ye kaydet → Stremio kataloğunda görünsün
    try:
        bc_data = {
            "name":           kanal_adi,
            "stream_url":     stream_url,
            "order":          order,
            "active":         True,
            "buffer_seconds": 0,          # doğrudan HLS servis ediyoruz
            "logo":           logo_url,
            "poster":         poster_url,
            "description":    aciklama or "",
            "genres":         ["Duyuru"],
            "is_duyuru":      True,        # temizleme için işaret
            "duyuru_session": session_id,
        }
        bc = await db.add_broadcast(bc_data)
        # DB'deki aktif=True → Stremio kataloğunda görünür
        await db.update_broadcast(bc["_id"], {"active": True})
        sess["broadcast_id"] = bc["_id"]
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).warning("Duyuru broadcast DB kaydı başarısız: %s", e)

    return _JSONResponse({
        "ok": True,
        "stream_url": stream_url,
    })


@router.post("/api/duyuru/durdur/{session_id}")
async def duyuru_durdur(session_id: str, _: bool = Depends(require_auth)):
    """Yayını durdurur, DB kaydını siler ve geçici dosyaları temizler."""
    sess = _duyuru_sessions.pop(session_id, None)
    if not sess:
        return _JSONResponse({"ok": False, "error": "Geçersiz session"}, status_code=404)

    # ffmpeg'i durdur
    proc = sess.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # Stremio kataloğundan çıkar
    bc_id = sess.get("broadcast_id")
    if bc_id:
        try:
            await db.delete_broadcast(bc_id)
        except Exception:
            pass

    # Geçici dosyaları sil
    _shutil.rmtree(sess.get("work_dir", ""), ignore_errors=True)
    return _JSONResponse({"ok": True})


@router.get("/duyuru/media/{session_id}/{filename}")
async def duyuru_media_serve(session_id: str, filename: str):
    """Duyuru poster ve logo dosyalarını servis eder (auth gerekmez)."""
    sess = _duyuru_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404)

    work_dir = _Path(sess["work_dir"])
    # Güvenlik: sadece poster.jpg ve logo.png izinli
    allowed = {"poster.jpg", "logo.png"}
    if filename not in allowed:
        raise HTTPException(status_code=403)

    file_path = work_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404)

    import mimetypes as _mt
    mime, _ = _mt.guess_type(str(file_path))
    content = file_path.read_bytes()
    return Response(content=content, media_type=mime or "image/jpeg")


@router.get("/duyuru/hls/{session_id}/{filename}")
async def duyuru_hls_serve(session_id: str, filename: str):
    """HLS playlist ve segment dosyalarını sağlar (auth gerekmez — iç ağ)."""
    sess = _duyuru_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404)

    file_path = _Path(sess["hls_dir"]) / filename
    if not file_path.exists():
        raise HTTPException(status_code=404)

    import mimetypes as _mt
    mime, _ = _mt.guess_type(str(file_path))
    if filename.endswith(".m3u8"):
        mime = "application/vnd.apple.mpegurl"
    elif filename.endswith(".ts"):
        mime = "video/mp2t"

    content = file_path.read_bytes()
    return Response(content=content, media_type=mime or "application/octet-stream")
