import logging
import re
_logger = logging.getLogger(__name__)
import math
import secrets
import mimetypes
import time
from typing import Dict

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse

from collections import deque

from Backend import db
from Backend.helper.encrypt import decode_string
from Backend.helper.exceptions import InvalidHash
from Backend.helper.custom_dl import ByteStreamer, ACTIVE_STREAMS, RECENT_STREAMS, get_adaptive_chunk_size
from Backend.helper.virtual_dl import resolve_virtual_parts, virtual_stream_generator
from Backend.pyrofork.bot import StreamBot, work_loads, multi_clients, client_dc_map, client_failures, client_avg_mbps
from Backend.config import Telegram
from Backend.logger import LOGGER
from Backend.fastapi.security.tokens import verify_token
from Backend.fastapi.security.credentials import require_auth
import asyncio


router = APIRouter(tags=["Streaming"])


def safe_content_disposition(fname: str, disposition: str = "inline") -> str:
    """
    RFC 5987 uyumlu Content-Disposition üretir.
    ASCII-dışı (Türkçe vb.) karakterler latin-1'e encode edilemediği için
    HTTP header'larında doğrudan kullanılamaz (UnicodeEncodeError'a yol açar).
    Böyle durumlarda filename* parametresiyle UTF-8 olarak gönderilir,
    ayrıca eski istemciler için ASCII'ye indirgenmiş bir filename de eklenir.
    """
    try:
        fname.encode("latin-1")
        return f'{disposition}; filename="{fname}"'
    except UnicodeEncodeError:
        from urllib.parse import quote as _urlquote
        ascii_fallback = fname.encode("ascii", "ignore").decode("ascii") or "file"
        encoded = _urlquote(fname, safe="")
        return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"

_SPLIT_SUFFIX_RE = re.compile(
    r'\.(mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv)\.\d{2,3}$', re.IGNORECASE,
)
_SPLIT_PART_RE = re.compile(r'\.part\d+\.(\w+)$', re.IGNORECASE)


def clean_split_filename(fname: str) -> str:
    """
    Çok parçalı (split) Telegram dosyaları tek bir sanal akış olarak
    birleştirilip kullanıcıya gönderildiğinden, ilk parçanın adındaki
    sahte parça numarası (".mkv.001", ".mp4.002" vb.) indirme dosya
    adında görünmemeli — bu sadece kaynak parçanın numarasıdır, birleşik
    dosyanın bir parçası değildir.
    """
    if not fname:
        return fname
    m = _SPLIT_SUFFIX_RE.search(fname)
    if m:
        return fname[: m.start()] + "." + m.group(1)
    m = _SPLIT_PART_RE.search(fname)
    if m:
        return fname[: m.start()] + "." + m.group(1)
    return fname


_streamer_by_client: Dict = {}

# Aynı process içinde aynı token için bildirim tekrarını önleyen in-memory set'ler.
# Gece sıfırlamasında DB bayrakları sıfırlanır; bu set'ler process restart'ta zaten temizlenir.
_daily_warn_sent:     set = set()   # token → %80 uyarısı bu oturumda gönderildi
_daily_finished_sent: set = set()   # token → %100 bitti bu oturumda gönderildi


def _force_stop_token_streams(token: str) -> int:
    """Token'a ait tüm aktif stream'lere force_stop flag'i set eder.
    Bir sonraki chunk gönderiminde consumer duracaktır.
    Döndürür: durdurulan stream sayısı.
    """
    count = 0
    for sid, info in list(ACTIVE_STREAMS.items()):
        if info.get("meta", {}).get("user_token") == token and info.get("status") == "active":
            info["force_stop"] = True
            count += 1
    if count:
        LOGGER.info("force_stop set for %d stream(s) of token %s", count, token[:8])
    return count


def _require_admin(request: Request) -> bool:
    """Stream istatistikleri endpoint'leri için admin oturum kontrolü."""
    from Backend.fastapi.security.credentials import is_authenticated
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    return True


def make_json_safe(obj):
    if isinstance(obj, deque):
        return list(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="ignore")
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    return obj


def parse_range_header(range_header: str, file_size: int):
    """
    Parse HTTP Range header.

    Supports:
    bytes=1000-2000
    bytes=1000-
    bytes=-2000
    """
    if not range_header:
        return 0, file_size - 1

    try:
        value = range_header.replace("bytes=", "").strip()
        start_str, end_str = value.split("-")

        if start_str == "":
            length = int(end_str)
            start = file_size - length
            end = file_size - 1
        elif end_str == "":
            start = int(start_str)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str)

    except Exception:
        raise HTTPException(
            status_code=416,
            detail="Invalid Range header",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    if start < 0:
        start = 0

    if end >= file_size:
        end = file_size - 1

    if end < start:
        raise HTTPException(
            status_code=416,
            detail="Requested Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    return start, end


def select_best_client(target_dc: int) -> int:
    """Pick the best available client.

    Score = work_loads + 3 × client_failures
    Failures are weighted 3× so a bot that has been timing out / erroring
    is deprioritised even if its current workload is low.
    DC-aware selection is kept but currently commented out (uncomment to
    prefer same-DC bots).
    """
    def _score(idx: int) -> int:
        return work_loads.get(idx, 0) + 3 * client_failures.get(idx, 0)

    # --- DC-aware selection (uncomment to enable) ---------------------------
    # matching = [
    #     idx for idx, dc in client_dc_map.items()
    #     if dc == target_dc and idx in multi_clients
    # ]
    # if matching:
    #     selected = min(matching, key=_score)
    #     LOGGER.debug("DC-match client %s (DC %s) score=%s", selected, target_dc, _score(selected))
    #     return selected
    # ------------------------------------------------------------------------

    if multi_clients:
        selected = min(multi_clients.keys(), key=_score)
        LOGGER.debug(
            "Selected client %s (DC %s) score=%s",
            selected, client_dc_map.get(selected, "?"), _score(selected),
        )
        return selected

    return 0


async def decay_client_failures() -> None:
    """Every 5 minutes reduce each client's failure count by 1 (floor 0).

    This lets bots self-recover after a temporary DC issue without manual
    intervention.  The coroutine is started once as a background task on
    first import.
    """
    while True:
        await asyncio.sleep(300)  # 5 minutes
        for k in list(client_failures):
            if client_failures.get(k, 0) > 0:
                client_failures[k] = max(0, client_failures[k] - 1)
                LOGGER.debug("Failure decay: client %s failures → %s", k, client_failures[k])



def _rebalance_user_streams(token: str, total_rate_mbps: float) -> None:
    """Kullanıcının aktif stream'leri arasında toplam hız limitini eşit böler.

    Her stream'in ACTIVE_STREAMS kaydındaki ``rate_limit_mbps`` alanı güncellenir.
    custom_dl.py'deki throttle döngüsü bu değeri dinamik olarak okur, dolayısıyla
    değişiklik anlık olarak devreye girer.
    """
    if total_rate_mbps <= 0:
        return
    user_streams = [
        s for s in ACTIVE_STREAMS.values()
        if s.get("status") == "active" and s.get("meta", {}).get("user_token") == token
    ]
    count = len(user_streams)
    if count == 0:
        return
    per_stream = total_rate_mbps / count
    for s in user_streams:
        s["rate_limit_mbps"] = per_stream
    LOGGER.debug(
        "Rebalanced %d stream(s) for token %s: %.2f Mbps each (total %.2f Mbps)",
        count, token[:8], per_stream, total_rate_mbps,
    )


def _split_stream_snapshot(stream_id: str, part_count: int):
    """Parçalı (split/virtual) akışlarda her fiziksel parça, ByteStreamer
    tarafından ACTIVE_STREAMS içinde `{stream_id}-p{index}` anahtarıyla ayrı
    bir kayıt olarak izlenir (bkz. virtual_dl.virtual_stream_generator).
    Bu yüzden ana `stream_id` ACTIVE_STREAMS'te hiç oluşmaz ve doğrudan
    ACTIVE_STREAMS.get(stream_id) her zaman None döner.

    Bu fonksiyon, tüm parçaların (aktif veya RECENT_STREAMS'e taşınmış)
    toplam byte sayısını toplayarak sanal akışın gerçek toplam kullanımını
    hesaplar. Ayrıca son parçanın tamamlanıp tamamlanmadığını da döndürür.
    """
    total = 0
    last_part_active = False
    last_part_found = False
    for i in range(part_count):
        sub_id = f"{stream_id}-p{i}"
        entry = ACTIVE_STREAMS.get(sub_id)
        if entry is not None:
            total += entry.get("total_bytes", 0)
            if i == part_count - 1:
                last_part_active = True
                last_part_found = True
            continue
        for rec in RECENT_STREAMS:
            if rec.get("stream_id") == sub_id:
                total += rec.get("total_bytes", 0)
                if i == part_count - 1:
                    last_part_found = True
                break
    # Akış bittiğinde son parça artık aktif değildir ama (RECENT_STREAMS'te
    # bulunmuş ya da hiç başlamamış olsa da) tamamlanmış sayılır.
    finished = (not last_part_active)
    return total, finished, last_part_found


async def track_usage_from_stats(stream_id: str, token: str, token_data: dict, part_count: int = None):
    await asyncio.sleep(2)
    
    limits = token_data.get("limits", {}) if token_data else {}
    usage = token_data.get("usage", {}) if token_data else {}
    
    daily_limit_gb = limits.get("daily_limit_gb")
    monthly_limit_gb = limits.get("monthly_limit_gb")
    
    initial_daily_bytes = usage.get("daily", {}).get("bytes", 0)
    initial_monthly_bytes = usage.get("monthly", {}).get("bytes", 0)
    
    last_tracked_bytes = 0
    update_interval = 10
    
    try:
        while True:
            await asyncio.sleep(update_interval)

            if part_count:
                # ── Parçalı (split) dosya akışı ─────────────────────────
                current_bytes, finished, _ = _split_stream_snapshot(stream_id, part_count)
                delta = current_bytes - last_tracked_bytes
                if delta > 0:
                    try:
                        await db.update_token_usage(token, delta)
                        last_tracked_bytes = current_bytes
                        LOGGER.debug(f"Updated usage (split) for {stream_id}: +{delta} bytes (total: {current_bytes})")
                    except Exception as e:
                        LOGGER.error(f"Periodic usage update (split) failed: {e}")

                if finished:
                    # Son parça artık aktif değil → akış tamamen bitti.
                    if daily_limit_gb and daily_limit_gb > 0:
                        final_daily_gb = (initial_daily_bytes + current_bytes) / (1024 ** 3)
                        used_pct = round((final_daily_gb / daily_limit_gb) * 100, 1)
                        tg_user_id = token_data.get("user_id") if token_data else None
                        from pyrogram import enums as _pyrogram_enums
                        if final_daily_gb >= daily_limit_gb:
                            _force_stop_token_streams(token)
                            if token not in _daily_finished_sent:
                                try:
                                    already_finished = await db.get_token_daily_limit_finished(token)
                                    if not already_finished:
                                        _daily_finished_sent.add(token)
                                        if tg_user_id:
                                            try:
                                                await StreamBot.send_message(
                                                    chat_id=int(tg_user_id),
                                                    text=(
                                                        f"🔴 Günlük Limitiniz Doldu!\n"
                                                        f"📊 Kullanım : {round(final_daily_gb, 2)} GB / {daily_limit_gb} GB (%{used_pct})\n"
                                                        f"⚠️ Bugünkü günlük limitiniz aşıldı."
                                                    ),
                                                    parse_mode=_pyrogram_enums.ParseMode.HTML,
                                                )
                                                LOGGER.info(f"Daily limit 100% finished sent (on close) to user {tg_user_id} for token {token[:8]}")
                                            except Exception as warn_err:
                                                LOGGER.warning(f"Daily limit finished notification failed (on close) for {token[:8]}: {warn_err}")
                                        await db.mark_token_daily_limit_finished(token)
                                except Exception as e:
                                    LOGGER.warning(f"Daily limit finished check (on close) failed for {token[:8]}: {e}")
                    return

                # Aylık limit kontrolü (parçalı akış hâlâ devam ediyor)
                if monthly_limit_gb and monthly_limit_gb > 0:
                    current_monthly_gb = (initial_monthly_bytes + current_bytes) / (1024 ** 3)
                    if current_monthly_gb >= monthly_limit_gb:
                        LOGGER.debug(f"Monthly limit reached for token, stream {stream_id} may be blocked by verify_token")

                # Günlük limit kontrolü (parçalı akış hâlâ devam ediyor)
                if daily_limit_gb and daily_limit_gb > 0:
                    current_daily_gb = (initial_daily_bytes + current_bytes) / (1024 ** 3)
                    used_pct = round((current_daily_gb / daily_limit_gb) * 100, 1)
                    tg_user_id = token_data.get("user_id") if token_data else None
                    if current_daily_gb >= daily_limit_gb:
                        _force_stop_token_streams(token)
                        if token not in _daily_finished_sent:
                            try:
                                already_finished = await db.get_token_daily_limit_finished(token)
                                if not already_finished:
                                    _daily_finished_sent.add(token)
                                    if tg_user_id:
                                        from pyrogram import enums as _pyrogram_enums
                                        try:
                                            await StreamBot.send_message(
                                                chat_id=int(tg_user_id),
                                                text=(
                                                    f"🔴 Günlük Limitiniz Doldu!\n"
                                                    f"📊 Kullanım : {round(current_daily_gb, 2)} GB / {daily_limit_gb} GB (%{used_pct})\n"
                                                    f"⚠️ Bugünkü günlük limitiniz aşıldı."
                                                ),
                                                parse_mode=_pyrogram_enums.ParseMode.HTML,
                                            )
                                            LOGGER.info(f"Daily limit 100% finished sent to user {tg_user_id} for token {token[:8]}")
                                        except Exception as warn_err:
                                            LOGGER.warning(f"Daily limit finished notification failed for {token[:8]}: {warn_err}")
                                    await db.mark_token_daily_limit_finished(token)
                            except Exception as warn_err:
                                LOGGER.warning(f"Daily limit finished check failed for {token[:8]}: {warn_err}")
                continue

            stream_info = ACTIVE_STREAMS.get(stream_id)
            if not stream_info:
                for rec in RECENT_STREAMS:
                    if rec.get("stream_id") == stream_id:
                        final_bytes = rec.get("total_bytes", 0)
                        delta = final_bytes - last_tracked_bytes
                        if delta > 0:
                            try:
                                await db.update_token_usage(token, delta)
                                LOGGER.debug(f"Final usage update for {stream_id}: {delta} bytes")
                            except Exception as e:
                                LOGGER.error(f"Final usage update failed: {e}")
                        # Stream kapandıktan sonra sadece %100 limitini kontrol et
                        if daily_limit_gb and daily_limit_gb > 0:
                            final_daily_gb = (initial_daily_bytes + final_bytes) / (1024 ** 3)
                            used_pct = round((final_daily_gb / daily_limit_gb) * 100, 1)
                            tg_user_id = token_data.get("user_id") if token_data else None
                            from pyrogram import enums as _pyrogram_enums
                            # %100 — limit doldu, token devre dışı bırak
                            if final_daily_gb >= daily_limit_gb:
                                # Limit aşıldığında diğer aktif stream'leri de durdur
                                _force_stop_token_streams(token)
                                if token not in _daily_finished_sent:
                                    try:
                                        already_finished = await db.get_token_daily_limit_finished(token)
                                        if not already_finished:
                                            _daily_finished_sent.add(token)
                                            if tg_user_id:
                                                try:
                                                    await StreamBot.send_message(
                                                        chat_id=int(tg_user_id),
                                                        text=(
                                                            f"🔴 Günlük Limitiniz Doldu!\n"
                                                            f"📊 Kullanım : {round(final_daily_gb, 2)} GB / {daily_limit_gb} GB (%{used_pct})\n"
                                                            f"⚠️ Bugünkü günlük limitiniz aşıldı."
                                                        ),
                                                        parse_mode=_pyrogram_enums.ParseMode.HTML,
                                                    )
                                                    LOGGER.info(f"Daily limit 100% finished sent (on close) to user {tg_user_id} for token {token[:8]}")
                                                except Exception as warn_err:
                                                    LOGGER.warning(f"Daily limit finished notification failed (on close) for {token[:8]}: {warn_err}")
                                            await db.mark_token_daily_limit_finished(token)
                                    except Exception as e:
                                        LOGGER.warning(f"Daily limit finished check (on close) failed for {token[:8]}: {e}")
                        break
                return
            
            current_bytes = stream_info.get("total_bytes", 0)
            delta = current_bytes - last_tracked_bytes
            
            if delta > 0:
                try:
                    await db.update_token_usage(token, delta)
                    last_tracked_bytes = current_bytes
                    LOGGER.debug(f"Updated usage for {stream_id}: +{delta} bytes (total: {current_bytes})")
                except Exception as e:
                    LOGGER.error(f"Periodic usage update failed: {e}")
            
            # Check limits — sadece %100 bildir ve token'ı devre dışı bırak
            if daily_limit_gb and daily_limit_gb > 0:
                current_daily_gb = (initial_daily_bytes + current_bytes) / (1024 ** 3)
                used_pct = round((current_daily_gb / daily_limit_gb) * 100, 1)
                tg_user_id = token_data.get("user_id") if token_data else None
                if current_daily_gb >= daily_limit_gb:
                    # Limit aşıldığı her döngüde aktif stream'leri durdur
                    _force_stop_token_streams(token)
                    if token not in _daily_finished_sent:
                        try:
                            already_finished = await db.get_token_daily_limit_finished(token)
                            if not already_finished:
                                _daily_finished_sent.add(token)
                                if tg_user_id:
                                    from pyrogram import enums as _pyrogram_enums
                                    try:
                                        await StreamBot.send_message(
                                            chat_id=int(tg_user_id),
                                            text=(
                                                f"🔴 Günlük Limitiniz Doldu!\n"
                                                f"📊 Kullanım : {round(current_daily_gb, 2)} GB / {daily_limit_gb} GB (%{used_pct})\n"
                                                f"⚠️ Bugünkü günlük limitiniz aşıldı."
                                            ),
                                            parse_mode=_pyrogram_enums.ParseMode.HTML,
                                        )
                                        LOGGER.info(f"Daily limit 100% finished sent to user {tg_user_id} for token {token[:8]}")
                                    except Exception as warn_err:
                                        LOGGER.warning(f"Daily limit finished notification failed for {token[:8]}: {warn_err}")
                                await db.mark_token_daily_limit_finished(token)
                        except Exception as warn_err:
                            LOGGER.warning(f"Daily limit finished check failed for {token[:8]}: {warn_err}")
            
            if monthly_limit_gb and monthly_limit_gb > 0:
                current_monthly_gb = (initial_monthly_bytes + current_bytes) / (1024 ** 3)
                if current_monthly_gb >= monthly_limit_gb:
                    LOGGER.debug(f"Monthly limit reached for token, stream {stream_id} may be blocked by verify_token")
                    
    except asyncio.CancelledError:
        if part_count:
            current_bytes, _, _ = _split_stream_snapshot(stream_id, part_count)
            delta = current_bytes - last_tracked_bytes
            if delta > 0:
                try:
                    await db.update_token_usage(token, delta)
                    LOGGER.info(f"Cancelled - final update (split) for {stream_id}: {delta} bytes")
                except Exception as e:
                    LOGGER.error(f"Cancelled usage update (split) failed: {e}")
        else:
            stream_info = ACTIVE_STREAMS.get(stream_id)
            if stream_info:
                current_bytes = stream_info.get("total_bytes", 0)
                delta = current_bytes - last_tracked_bytes
                if delta > 0:
                    try:
                        await db.update_token_usage(token, delta)
                        LOGGER.info(f"Cancelled - final update for {stream_id}: {delta} bytes")
                    except Exception as e:
                        LOGGER.error(f"Cancelled usage update failed: {e}")


@router.get("/dl/{token}/{id}/{gecicitoken}/{name}")
@router.head("/dl/{token}/{id}/{gecicitoken}/{name}")
async def stream_handler(
    request: Request,
    token: str,
    id: str,
    gecicitoken: str,
    name: str,
    dl: int = 0,
    token_data: dict = Depends(verify_token),
):

    # --- Günlük limit kontrolü: limit dolmuşsa yeni stream başlatma ---
    _limits = token_data.get("limits", {}) if token_data else {}
    _daily_limit_gb = _limits.get("daily_limit_gb")
    if _daily_limit_gb and _daily_limit_gb > 0:
        _limit_finished = await db.get_token_daily_limit_finished(token)
        if _limit_finished:
            # Bayrağa körü körüne güvenme: gerçek kullanımı da doğrula.
            # Limit artırıldıysa, sıfırlama çalışmadıysa veya bayrak yanlış
            # set edildiyse kullanıcı haksız yere bloke olabilir.
            _tok_doc = await db.get_api_token(token)
            _real_daily_bytes = (_tok_doc or {}).get("usage", {}).get("daily", {}).get("bytes", 0)
            _real_daily_gb = _real_daily_bytes / (1024 ** 3)
            if _real_daily_gb < _daily_limit_gb:
                # Gerçek kullanım limitin altında → bayrağı temizle ve geç
                LOGGER.warning(
                    f"[stream] daily_limit_finished bayrağı yanlış — "                    f"gerçek kullanım {_real_daily_gb:.2f} GB / limit {_daily_limit_gb} GB — bayrak sıfırlandı. token={token[:12]}"
                )
                await db.dbs["tracking"]["api_tokens"].update_one(
                    {"token": token},
                    {"$set": {"daily_limit_finished": False, "daily_limit_warned": False, "daily_limit_disabled": False}}
                )
            else:
                raise HTTPException(status_code=429, detail="Günlük limit doldu. Yeni yayın başlatılamaz.")

    try:
        decoded = await decode_string(id)
    except Exception as _dec_err:
        LOGGER.error(f"[dl] decode_string hatası — id={id[:30]}... hata={_dec_err}")
        raise HTTPException(status_code=400, detail=f"Geçersiz dosya ID'si: {_dec_err}")

    msg_id = decoded.get("msg_id")

    # ── Split (virtual) dosya — parts payload ────────────────────────────────
    if "parts" in decoded:
        from Backend.helper.stream_token import media_token_manager as _mtm_v
        if not _mtm_v.verify(gecicitoken, token, id):
            raise HTTPException(
                status_code=403,
                detail="Geçersiz veya süresi dolmuş link. Tekrar izlemek/indirmek için sayfayı yenileyin.",
            )
        return await virtual_media_streamer(
            request=request,
            parts_payload=decoded["parts"],
            token=token,
            token_data=token_data,
            stream_id_hash=id,
        )

    # ── Yerel dosya (ZipModu) ──────────────────────────────────────────────
    # Yerel dosyalar için gecici token zorunlu — Telegram için atlanır.
    # Nedeni: media_token_manager memory'de tutulduğundan bot restart veya
    # Stremio'nun paralel HEAD+GET isteklerinde token geçersizleşir.
    local_path = decoded.get("local_path")
    if local_path and not msg_id:
        LOGGER.info(f"[dl] local_path isteği — dosya={local_path} token={token[:8]}...")
        from Backend.helper.stream_token import media_token_manager
        if not media_token_manager.verify(gecicitoken, token, id):
            LOGGER.warning(
                f"[dl] Geçersiz gecici token — gecicitoken={gecicitoken[:10]}... "
                f"token={token[:8]}... id={id[:20]}..."
            )
            raise HTTPException(
                status_code=403,
                detail="Geçersiz veya süresi dolmuş link. Tekrar izlemek/indirmek için sayfayı yenileyin.",
            )
        return await local_file_streamer(request, local_path, token_data, token, force_download=bool(dl))

    # ── Google Drive dosyası ─────────────────────────────────────────────────
    gdrive_file_id = decoded.get("gdrive_file_id")
    if gdrive_file_id and not msg_id:
        from Backend.helper.stream_token import media_token_manager
        if not media_token_manager.verify(gecicitoken, token, id):
            LOGGER.warning(
                f"[dl] GDrive geçersiz gecici token — gecicitoken={gecicitoken[:10]}... "
                f"token={token[:8]}... id={id[:20]}..."
            )
            raise HTTPException(
                status_code=403,
                detail="Geçersiz veya süresi dolmuş link. Tekrar izlemek/indirmek için sayfayı yenileyin.",
            )
        return await gdrive_streamer(request, gdrive_file_id, token_data, token, force_download=bool(dl))

    # ── Rclone dosyası ────────────────────────────────────────────────────────
    rclone_remote = decoded.get("rclone_remote")
    rclone_path   = decoded.get("rclone_path")
    if rclone_remote and rclone_path and not msg_id:
        from Backend.helper.stream_token import media_token_manager
        if not media_token_manager.verify(gecicitoken, token, id):
            LOGGER.warning(
                f"[dl] Rclone geçersiz gecici token — gecicitoken={gecicitoken[:10]}... "
                f"token={token[:8]}... id={id[:20]}..."
            )
            raise HTTPException(
                status_code=403,
                detail="Geçersiz veya süresi dolmuş link. Tekrar izlemek/indirmek için sayfayı yenileyin.",
            )
        return await rclone_streamer(request, rclone_remote, rclone_path, token_data, token, force_download=bool(dl))

    if not msg_id:
        raise HTTPException(status_code=400, detail="Missing id")

    # ── Telegram dosyası — gecici token doğrulaması ───────────────────────────
    from Backend.helper.stream_token import media_token_manager as _mtm
    if not _mtm.verify(gecicitoken, token, id):
        LOGGER.warning(
            f"[dl] Telegram geçersiz gecici token — gecicitoken={gecicitoken[:10]}... "
            f"token={token[:8]}... id={id[:20]}..."
        )
        raise HTTPException(
            status_code=403,
            detail="Geçersiz veya süresi dolmuş link. Tekrar izlemek/indirmek için sayfayı yenileyin.",
        )

    chat_id = int(f"-100{decoded['chat_id']}")
    message = await StreamBot.get_messages(chat_id, int(msg_id))
    file = message.video or message.document
    secure_hash = file.file_unique_id[:6]

    return await media_streamer(
        request=request,
        chat_id=chat_id,
        msg_id=int(msg_id),
        secure_hash=secure_hash,
        token=token,
        token_data=token_data,
        stream_id_hash=id,
        force_download=bool(dl),
    )


async def virtual_media_streamer(
    request: Request,
    parts_payload: list,
    token: str,
    token_data: dict = None,
    stream_id_hash: str = None,
):
    """Split (parçalanmış) Telegram dosyalarını tek bir sanal akış olarak sunar.
    parts_payload: [{"chat_id": ..., "msg_id": ..., "part_number": ...}, ...]
    """
    import math as _math
    import secrets as _sec
    import mimetypes as _mt
    from urllib.parse import unquote as _unquote
    from fastapi.responses import Response as _PlainResp

    index = select_best_client(0)
    tg_client = multi_clients[index]
    if tg_client not in _streamer_by_client:
        _streamer_by_client[tg_client] = ByteStreamer(tg_client, index)
    streamer: ByteStreamer = _streamer_by_client[tg_client]

    parts, file_size = await resolve_virtual_parts(parts_payload, streamer)
    if not parts or file_size <= 0:
        raise HTTPException(status_code=404, detail="Split media parts not found")

    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    req_length = end - start + 1
    chunk_size = 1024 * 1024
    stream_id = _sec.token_hex(8)
    decoded_name = _unquote(request.path_params.get("name", ""))

    db_title = None
    if stream_id_hash:
        db_title = await db.get_title_by_stream_id(stream_id_hash)
    final_title = db_title if db_title else decoded_name

    meta = {
        "request_path": str(request.url.path),
        "client_host": request.client.host if request.client else None,
        "title": final_title,
        "user_name": token_data.get("name", "Unknown") if token_data else "Unknown",
        "user_token": token,
        "split_parts": len(parts),
    }

    prefetch_count = Telegram.PARALLEL
    parallelism = Telegram.PRE_FETCH

    if token and token_data:
        asyncio.create_task(track_usage_from_stats(stream_id, token, token_data, part_count=len(parts)))

    if token:
        await db.add_device_session(token, stream_id)

    first_file_id = parts[0]["file_id"]
    file_name = first_file_id.file_name or f"{_sec.token_hex(4)}.bin"
    mime_type = first_file_id.mime_type or _mt.guess_type(file_name)[0] or "application/octet-stream"
    if "." not in file_name and "/" in mime_type:
        file_name = f"{file_name}.{mime_type.split('/')[1]}"
    # Parçalar tek bir dosya halinde birleştirilip gönderiliyor; isimdeki
    # sahte parça numarasını (".mkv.001" vb.) indirme adından temizle.
    file_name = clean_split_filename(file_name)

    common_headers = {
        "Content-Type": mime_type,
        "Content-Disposition": safe_content_disposition(file_name, "inline"),
        "Accept-Ranges": "bytes",
        "Content-Length": str(req_length),
        "Cache-Control": "public, max-age=3600",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }
    if range_header:
        common_headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        status = 206
    else:
        status = 200

    if request.method == "HEAD":
        if token:
            await db.remove_device_session(token, stream_id)
        return _PlainResp(status_code=status, headers=common_headers)

    body_gen = virtual_stream_generator(
        parts=parts, start=start, end=end, chunk_size=chunk_size,
        streamer=streamer, client_index=index, request=request, meta=meta,
        stream_id=stream_id, parallelism=parallelism, prefetch_count=prefetch_count,
    )

    return StreamingResponse(body_gen, headers=common_headers, status_code=status, media_type=mime_type)


async def gdrive_streamer(request: Request, gdrive_file_id: str, token_data: dict = None, token: str = None, force_download: bool = False):
    """
    Google Drive dosyasını HTTP Range destekli olarak stream eder.
    token.pickle ile Drive API'a bağlanır; içerik doğrudan istemciye iletilir.
    """
    import mimetypes as _mt
    from pathlib import Path as _Path

    # ── Drive bağlantısı ─────────────────────────────────────────────────────
    try:
        from googleapiclient.discovery import build as _build
        from google.auth.transport.requests import Request as _GRequest
        import pickle as _pickle

        _GDRIVE_TOKEN_PATH = _Path(__file__).parent.parent.parent.parent / "gdrive_token.pickle"
        if not _GDRIVE_TOKEN_PATH.exists():
            raise HTTPException(status_code=503, detail="gdrive_token.pickle bulunamadı. /ayarlar → Dosya Ekle ile yükleyin.")

        with open(_GDRIVE_TOKEN_PATH, "rb") as _f:
            _creds = _pickle.load(_f)
        if _creds.expired and _creds.refresh_token:
            _creds.refresh(_GRequest())
        _svc = _build("drive", "v3", credentials=_creds, cache_discovery=False)
    except HTTPException:
        raise
    except Exception as _e:
        _logger.error("GDrive bağlantı hatası", exc_info=True)
        raise HTTPException(status_code=503, detail="Google Drive bağlantısı kurulamadı")

    # ── Dosya metadata ────────────────────────────────────────────────────────
    try:
        _meta = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _svc.files().get(fileId=gdrive_file_id, fields="id,name,size,mimeType").execute()
        )
    except Exception as _e:
        _logger.error("GDrive dosya hatası", exc_info=True)
        raise HTTPException(status_code=404, detail="Drive dosyası bulunamadı")

    file_name = _meta.get("name", "video.mkv")
    file_size = int(_meta.get("size", 0))
    mime_type = _meta.get("mimeType") or _mt.guess_type(file_name)[0] or "video/x-matroska"

    if not file_size:
        raise HTTPException(status_code=404, detail="Drive dosyası boş veya boyut bilgisi yok.")

    # ── Range header ──────────────────────────────────────────────────────────
    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    req_length = end - start + 1

    # ── Hız limiti ────────────────────────────────────────────────────────────
    _global_rate = 0.0
    try:
        if (Telegram.HIZ_LIMITI or "").strip():
            _global_rate = float(Telegram.HIZ_LIMITI)
    except ValueError:
        pass
    _user_rate = 0.0
    if token_data:
        try:
            _user_rate = float(token_data.get("limits", {}).get("speed_limit_mbps") or 0)
        except (ValueError, TypeError):
            pass
    total_rate = _user_rate if _user_rate > 0 else _global_rate

    stream_id = secrets.token_hex(8)

    ACTIVE_STREAMS[stream_id] = {
        "stream_id": stream_id,
        "status": "active",
        "total_bytes": 0,
        "start_ts": time.time(),
        "last_ts": time.time(),
        "avg_mbps": 0.0,
        "instant_mbps": 0.0,
        "peak_mbps": 0.0,
        "rate_limit_mbps": total_rate,
        "meta": {
            "title": file_name,
            "client_host": request.client.host if request.client else None,
            "user_name": token_data.get("name", "Unknown") if token_data else "Unknown",
            "user_token": token or "",
        },
    }

    # Aktif cihaz session'ı DB'ye kaydet
    if token:
        await db.add_device_session(token, stream_id)

    # HEAD isteği — body yok
    if request.method == "HEAD":
        ACTIVE_STREAMS.pop(stream_id, None)
        if token:
            await db.remove_device_session(token, stream_id)
        headers = {
            "Content-Type": mime_type,
            "Content-Length": str(req_length),
            "Content-Disposition": safe_content_disposition(file_name, "inline"),
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        }
        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        from fastapi.responses import Response as _Resp
        return _Resp(status_code=206 if range_header else 200, headers=headers)

    # ── Streaming generator ───────────────────────────────────────────────────
    async def _gdrive_gen():
        import requests as _req_lib

        _sent = 0
        _t0 = time.time()
        _throttle_start = time.monotonic()
        _throttle_sent = 0
        _chunk_size = 4 * 1024 * 1024  # 4 MB chunks

        _pos = start
        try:
            while _pos <= end:
                _chunk_end = min(_pos + _chunk_size - 1, end)

                # Range header'lı direkt GET — MediaIoBaseDownload Range'i eziyor, bu daha güvenilir
                _resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda _s=_pos, _e=_chunk_end: _req_lib.get(
                        f"https://www.googleapis.com/drive/v3/files/{gdrive_file_id}?alt=media",
                        headers={
                            "Authorization": f"Bearer {_creds.token}",
                            "Range": f"bytes={_s}-{_e}",
                        },
                        timeout=60,
                    )
                )

                if _resp.status_code not in (200, 206):
                    LOGGER.error(f"[gdrive] chunk hatası: HTTP {_resp.status_code} pos={_pos}")
                    break

                data = _resp.content
                if not data:
                    break

                _sent += len(data)
                _pos += len(data)

                # ACTIVE_STREAMS güncelle
                _info = ACTIVE_STREAMS.get(stream_id)
                if _info is not None:
                    _elapsed = (time.time() - _t0) or 0.001
                    _info["total_bytes"] = _sent
                    _info["last_ts"] = time.time()
                    _info["avg_mbps"] = round((_sent / _elapsed) / (1024 * 1024), 2)

                    # Throttle
                    _lim = _info.get("rate_limit_mbps", 0.0)
                    if _lim > 0:
                        _rate_bps = _lim * 1024 * 1024 / 8
                        _throttle_sent += len(data)
                        _exp = _throttle_sent / _rate_bps
                        _slp = _exp - (time.monotonic() - _throttle_start)
                        if _slp > 0.005:
                            await asyncio.sleep(_slp)

                if ACTIVE_STREAMS.get(stream_id, {}).get("force_stop"):
                    LOGGER.info("force_stop set for stream %s — stopping generator", stream_id)
                    _info = ACTIVE_STREAMS.get(stream_id)
                    if _info:
                        _info["status"] = "cancelled"
                    raise asyncio.CancelledError("daily_limit_exceeded")
                yield data

        finally:
            _end_ts = time.time()
            _dur = _end_ts - _t0
            _avg = round((_sent / (1024 * 1024)) / max(_dur, 1e-6), 3)
            _info = ACTIVE_STREAMS.get(stream_id)
            if _info:
                _info["status"] = "finished"
                _info["end_ts"] = _end_ts
                _info["total_bytes"] = _sent
                _info["duration"] = _dur
                _info["avg_mbps"] = _avg

            async def _pop():
                await asyncio.sleep(3)
                try:
                    if stream_id in ACTIVE_STREAMS:
                        RECENT_STREAMS.appendleft(ACTIVE_STREAMS.pop(stream_id))
                except Exception:
                    pass
                if token:
                    await db.remove_device_session(token, stream_id)
            asyncio.create_task(_pop())

    if token and token_data:
        asyncio.create_task(track_usage_from_stats(stream_id, token, token_data))

    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(req_length),
        "Content-Disposition": safe_content_disposition(file_name, "attachment" if force_download else "inline"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    from fastapi.responses import StreamingResponse as _SR
    return _SR(
        _gdrive_gen(),
        status_code=206 if range_header else 200,
        media_type=mime_type,
        headers=headers,
    )


async def rclone_streamer(
    request: Request,
    rclone_remote: str,
    rclone_path: str,
    token_data: dict = None,
    token: str = None,
    force_download: bool = False,
):
    """
    Rclone sürücüsündeki dosyayı HTTP Range destekli stream eder.
    rclone cat ile pipe üzerinden veri aktarır; Stremio ve tarayıcı seek destekler.
    """
    import mimetypes as _mt
    import subprocess as _sp
    import shutil as _shutil
    from pathlib import Path as _Path

    _RCLONE_CONF = _Path(__file__).parent.parent.parent.parent / "rclone.conf"
    if not _RCLONE_CONF.exists():
        raise HTTPException(status_code=503, detail="rclone.conf bulunamadı. /ayarlar → Dosya Ekle ile yükleyin.")

    # rclone binary bul
    def _find_rclone() -> str:
        found = _shutil.which("rclone")
        if found:
            return found
        for c in ["/usr/bin/rclone", "/usr/local/bin/rclone", "/usr/sbin/rclone", "/opt/rclone/rclone"]:
            if _Path(c).is_file():
                return c
        raise HTTPException(status_code=503, detail="rclone binary bulunamadı. Docker image yeniden build edilmeli.")
    _RCLONE = _find_rclone()

    # ── Dosya boyutunu al ─────────────────────────────────────────────────────
    try:
        import json as _json
        remote_path = f"{rclone_remote}:{rclone_path}"
        parent_dir  = str(_Path(rclone_path).parent).replace("\\", "/")
        if parent_dir in (".", ""):
            parent_dir = ""
        list_target = f"{rclone_remote}:{parent_dir}"
        _sz_result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _sp.run(
                [_RCLONE, "lsjson", "--config", str(_RCLONE_CONF), list_target, "--no-modtime"],
                capture_output=True, text=True, timeout=60
            )
        )
        file_size = 0
        file_name = _Path(rclone_path).name
        if _sz_result.returncode == 0:
            for it in _json.loads(_sz_result.stdout or "[]"):
                if it.get("Name") == file_name:
                    file_size = it.get("Size", 0)
                    break
        if not file_size:
            raise HTTPException(status_code=404, detail=f"Rclone dosyası bulunamadı veya boyut bilgisi yok: {remote_path}")
    except HTTPException:
        raise
    except Exception as _e:
        _logger.error("Rclone boyut hatası", exc_info=True)
        raise HTTPException(status_code=503, detail="Rclone bağlantı hatası")

    mime_type = _mt.guess_type(file_name)[0] or "video/x-matroska"

    # ── Range header ──────────────────────────────────────────────────────────
    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    req_length  = end - start + 1

    # ── Hız limiti ────────────────────────────────────────────────────────────
    _global_rate = 0.0
    try:
        if (Telegram.HIZ_LIMITI or "").strip():
            _global_rate = float(Telegram.HIZ_LIMITI)
    except ValueError:
        pass
    _user_rate = 0.0
    if token_data:
        try:
            _user_rate = float(token_data.get("limits", {}).get("speed_limit_mbps") or 0)
        except (ValueError, TypeError):
            pass
    total_rate = _user_rate if _user_rate > 0 else _global_rate

    stream_id = secrets.token_hex(8)
    ACTIVE_STREAMS[stream_id] = {
        "stream_id": stream_id,
        "status": "active",
        "total_bytes": 0,
        "start_ts": time.time(),
        "last_ts": time.time(),
        "avg_mbps": 0.0,
        "instant_mbps": 0.0,
        "peak_mbps": 0.0,
        "rate_limit_mbps": total_rate,
        "meta": {
            "title": file_name,
            "client_host": request.client.host if request.client else None,
            "user_name": token_data.get("name", "Unknown") if token_data else "Unknown",
            "user_token": token or "",
        },
    }

    # Aktif cihaz session'ı DB'ye kaydet
    if token:
        await db.add_device_session(token, stream_id)

    # HEAD isteği
    if request.method == "HEAD":
        ACTIVE_STREAMS.pop(stream_id, None)
        if token:
            await db.remove_device_session(token, stream_id)
        headers = {
            "Content-Type": mime_type,
            "Content-Length": str(req_length),
            "Content-Disposition": safe_content_disposition(file_name, "inline"),
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        }
        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        from fastapi.responses import Response as _Resp
        return _Resp(status_code=206 if range_header else 200, headers=headers)

    # ── Streaming generator ───────────────────────────────────────────────────
    async def _rclone_gen():
        _sent = 0
        _t0   = time.time()
        _throttle_start = time.monotonic()
        _throttle_sent  = 0
        _chunk_size = 4 * 1024 * 1024  # 4 MB

        # rclone cat ile belirtilen byte aralığını oku
        cmd = [
            _RCLONE, "cat",
            "--config", str(_RCLONE_CONF),
            f"{rclone_remote}:{rclone_path}",
            "--offset", str(start),
            "--count",  str(req_length),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            remaining = req_length
            while remaining > 0:
                chunk_sz = min(_chunk_size, remaining)
                data = await proc.stdout.read(chunk_sz)
                if not data:
                    break
                _sent     += len(data)
                remaining -= len(data)

                _info = ACTIVE_STREAMS.get(stream_id)
                if _info is not None:
                    _elapsed = (time.time() - _t0) or 0.001
                    _info["total_bytes"] = _sent
                    _info["last_ts"]     = time.time()
                    _info["avg_mbps"]    = round((_sent / _elapsed) / (1024 * 1024), 2)

                    _lim = _info.get("rate_limit_mbps", 0.0)
                    if _lim > 0:
                        _rate_bps = _lim * 1024 * 1024 / 8
                        _throttle_sent += len(data)
                        _exp = _throttle_sent / _rate_bps
                        _slp = _exp - (time.monotonic() - _throttle_start)
                        if _slp > 0.005:
                            await asyncio.sleep(_slp)

                if ACTIVE_STREAMS.get(stream_id, {}).get("force_stop"):
                    LOGGER.info("force_stop set for stream %s — stopping generator", stream_id)
                    _info = ACTIVE_STREAMS.get(stream_id)
                    if _info:
                        _info["status"] = "cancelled"
                    raise asyncio.CancelledError("daily_limit_exceeded")
                yield data

            await proc.wait()

        finally:
            _end_ts = time.time()
            _dur    = _end_ts - _t0
            _avg    = round((_sent / (1024 * 1024)) / max(_dur, 1e-6), 3)
            _info   = ACTIVE_STREAMS.get(stream_id)
            if _info:
                _info["status"]      = "finished"
                _info["end_ts"]      = _end_ts
                _info["total_bytes"] = _sent
                _info["duration"]    = _dur
                _info["avg_mbps"]    = _avg

            async def _pop():
                await asyncio.sleep(3)
                try:
                    if stream_id in ACTIVE_STREAMS:
                        RECENT_STREAMS.appendleft(ACTIVE_STREAMS.pop(stream_id))
                except Exception:
                    pass
                if token:
                    await db.remove_device_session(token, stream_id)
            asyncio.create_task(_pop())

    if token and token_data:
        asyncio.create_task(track_usage_from_stats(stream_id, token, token_data))

    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(req_length),
        "Content-Disposition": safe_content_disposition(file_name, "attachment" if force_download else "inline"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    from fastapi.responses import StreamingResponse as _SR
    return _SR(
        _rclone_gen(),
        status_code=206 if range_header else 200,
        media_type=mime_type,
        headers=headers,
    )


async def local_file_streamer(request: Request, local_path: str, token_data: dict = None, token: str = None, force_download: bool = False):
    """
    /tmp/zipwork/ içindeki yerel dosyaları doğrudan HTTP Range destekli olarak stream eder.
    Stremio ve tarayıcı seek işlemlerini destekler.
    Bandwidth kullanımı token_data ile takip edilir (Telegram dosyalarıyla aynı mantık).
    """
    import mimetypes as _mt
    import os as _os
    from pathlib import Path as _Path
    from fastapi.responses import StreamingResponse, Response

    p = _Path(local_path).resolve()

    # ── Path Traversal Koruması ───────────────────────────────────────────────
    # local_path'in izin verilen dizinler içinde kalması zorunludur.
    # SUNUCU_DIR: web panelinden eklenen dosyalar
    # WORK_DIR  : bot üzerinden yüklenen dosyalar (sunucuyayukle)
    _default_sunucu = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "uploads"
    )
    _SUNUCU_DIR = _Path(_os.getenv("SUNUCU_DIR", _default_sunucu)).resolve()

    # WORK_DIR, SUNUCU_DIR ile aynı (sunucuyayukle.py WORK_DIR = SUNUCU_DIR)
    # Ek olarak geçici session alt dizinlerine de izin ver
    _allowed = str(_SUNUCU_DIR)

    if not str(p).startswith(_allowed + _os.sep) and str(p) != _allowed:
        LOGGER.warning(
            f"[dl] Path traversal girişimi engellendi — "
            f"local_path={local_path!r} token={str(token)[:8]}..."
        )
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")

    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Yerel dosya bulunamadı. Silinmiş olabilir.")

    file_size = p.stat().st_size
    mime_type = _mt.guess_type(p.name)[0] or "video/x-matroska"

    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    req_length = end - start + 1

    stream_id = secrets.token_hex(8)

    # ── Kullanıcı bazlı hız limiti (yerel dosya) ─────────────────────────────
    _local_global_rate = 0.0
    _local_raw = (Telegram.HIZ_LIMITI or "").strip()
    try:
        if _local_raw:
            _local_global_rate = float(_local_raw)
    except ValueError:
        pass

    _local_user_rate = 0.0
    if token_data:
        try:
            _local_user_rate = float(token_data.get("limits", {}).get("speed_limit_mbps") or 0)
        except (ValueError, TypeError):
            pass

    _local_total_rate = _local_user_rate if _local_user_rate > 0 else _local_global_rate

    # Yeni stream eklenmeden önce aktif sayıyı bul (+1 bu stream)
    if _local_total_rate > 0 and token:
        _local_active_count = sum(
            1 for s in ACTIVE_STREAMS.values()
            if s.get("status") == "active" and s.get("meta", {}).get("user_token") == token
        )
        _local_per_stream = _local_total_rate / (_local_active_count + 1)
    else:
        _local_per_stream = 0.0
    # ─────────────────────────────────────────────────────────────────────────

    # ACTIVE_STREAMS'e kaydet — track_usage_from_stats bunu izler
    ACTIVE_STREAMS[stream_id] = {
        "stream_id": stream_id,
        "local_path": local_path,
        "status": "active",
        "total_bytes": 0,
        "start_ts": time.time(),
        "last_ts": time.time(),
        "avg_mbps": 0.0,
        "instant_mbps": 0.0,
        "peak_mbps": 0.0,
        "rate_limit_mbps": _local_per_stream,  # Dinamik throttle için
        "meta": {
            "title": p.name,
            "client_host": request.client.host if request.client else None,
            "user_name": token_data.get("name", "Unknown") if token_data else "Unknown",
            "user_token": token or "",  # Kullanıcı bazlı dengeleme için
        },
    }

    # Aktif cihaz session'ı DB'ye kaydet
    if token:
        await db.add_device_session(token, stream_id)

    # Mevcut diğer kullanıcı stream'lerini dengele
    if _local_total_rate > 0 and token:
        _rebalance_user_streams(token, _local_total_rate)

    async def _iter_file(s: int, length: int, chunk: int = 1024 * 1024):
        remaining = length
        sent = 0
        t0 = time.time()
        _throttle_start = time.monotonic()
        _throttle_sent = 0
        _finished_normally = False
        try:
            with p.open("rb") as fh:
                fh.seek(s)
                while remaining > 0:
                    read_size = min(chunk, remaining)
                    data = fh.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    sent += len(data)

                    # ACTIVE_STREAMS kaydını güncelle
                    info = ACTIVE_STREAMS.get(stream_id)
                    if info is not None:
                        elapsed = (time.time() - t0) or 0.001
                        info["total_bytes"] = sent
                        info["last_ts"] = time.time()
                        info["avg_mbps"] = round((sent / elapsed) / (1024 * 1024), 2)

                        # ── Hız limiti throttle (dinamik — kullanıcı bazlı) ──────
                        _current_limit = info.get("rate_limit_mbps", 0.0)
                        _rate_bps = (_current_limit * 1024 * 1024 / 8) if _current_limit > 0 else 0.0
                        if _rate_bps > 0:
                            _throttle_sent += len(data)
                            _elapsed_w = time.monotonic() - _throttle_start
                            _expected  = _throttle_sent / _rate_bps
                            _sleep     = _expected - _elapsed_w
                            if _sleep > 0.005:
                                await asyncio.sleep(_sleep)
                        # ────────────────────────────────────────────────────────

                    if ACTIVE_STREAMS.get(stream_id, {}).get("force_stop"):
                        LOGGER.info("force_stop set for stream %s — stopping generator", stream_id)
                        _info = ACTIVE_STREAMS.get(stream_id)
                        if _info:
                            _info["status"] = "cancelled"
                        raise asyncio.CancelledError("daily_limit_exceeded")
                    yield data

            _finished_normally = True

        except (asyncio.CancelledError, GeneratorExit):
            # İstemci bağlantıyı kesti veya indirme iptal edildi
            _finished_normally = False
            raise
        except Exception:
            _finished_normally = False
            raise
        finally:
            # Bağlantı kesilse de, iptal edilse de, normal bitse de — her zaman temizle
            end_ts = time.time()
            duration = end_ts - t0 if end_ts > t0 else 0.0
            avg_mbps = round((sent / (1024 * 1024)) / (duration if duration > 0 else 1e-6), 3)
            peak_mbps = avg_mbps

            final_status = "finished" if _finished_normally else "cancelled"

            info = ACTIVE_STREAMS.get(stream_id)
            if info is not None:
                info["status"] = final_status
                info["end_ts"] = end_ts
                info["total_bytes"] = sent
                info["duration"] = duration
                info["avg_mbps"] = avg_mbps
                info["peak_mbps"] = peak_mbps

            # Sadece anlamlı boyuttaki istekleri kaydet (HEAD/küçük range isteklerini atla)
            if sent > 0:
                clean_title = p.stem  # uzantısız ad
                log_entry = {
                    "stream_id":    stream_id,
                    "msg_id":       None,
                    "chat_id":      None,
                    "dc_id":        None,
                    "client_index": None,
                    "total_bytes":  sent,
                    "duration":     duration,
                    "avg_mbps":     avg_mbps,
                    "peak_mbps":    peak_mbps,
                    "status":       final_status,
                    "parallelism":  1,
                    "chunk_size":   chunk,
                    "meta": {
                        "title": clean_title,
                    },
                }
                asyncio.create_task(db.log_stream_stats(log_entry))

            async def _delayed_pop():
                await asyncio.sleep(3)
                try:
                    if stream_id in ACTIVE_STREAMS:
                        RECENT_STREAMS.appendleft(ACTIVE_STREAMS.pop(stream_id))
                except Exception:
                    pass
                if token:
                    await db.remove_device_session(token, stream_id)
                # Stream kapandı — kalan kullanıcı stream'lerini dengele
                if _local_total_rate > 0 and token:
                    _rebalance_user_streams(token, _local_total_rate)
            asyncio.create_task(_delayed_pop())

    # RFC 5987: Türkçe/UTF-8 karakterleri latin-1'e encode edilemez,
    # filename* parametresiyle URL-encode olarak gönder.
    from urllib.parse import quote as _urlquote
    def _safe_disp(fname: str, disp: str = "inline") -> str:
        try:
            fname.encode("latin-1")
            return disp + '; filename="' + fname + '"'
        except (UnicodeEncodeError, UnicodeDecodeError):
            encoded = _urlquote(fname, safe="")
            return disp + "; filename*=UTF-8''" + encoded

    head_headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(req_length),
        "Content-Disposition": _safe_disp(p.name, "attachment" if force_download else "inline"),
    }

    if request.method == "HEAD":
        # HEAD isteğinde stream açılmaz, ACTIVE_STREAMS kaydını temizle
        ACTIVE_STREAMS.pop(stream_id, None)
        if token:
            await db.remove_device_session(token, stream_id)
        return Response(headers=head_headers, media_type=mime_type)

    # GET: Content-Length dahil — tarayıcı/oynatıcı toplam boyutu görebilsin
    get_headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(req_length),
        "Content-Disposition": _safe_disp(p.name, "attachment" if force_download else "inline"),
    }

    # Bandwidth takibini başlat (token varsa)
    if token and token_data:
        asyncio.create_task(track_usage_from_stats(stream_id, token, token_data))

    status_code = 206 if range_header else 200
    return StreamingResponse(
        _iter_file(start, req_length),
        status_code=status_code,
        media_type=mime_type,
        headers=get_headers,
    )


async def media_streamer(
    request: Request,
    chat_id: int,
    msg_id: int,
    secure_hash: str,
    token: str,
    token_data: dict = None,
    stream_id_hash: str = None,
    force_download: bool = False,
):
    temp_client = multi_clients[min(multi_clients.keys(), key=lambda i: work_loads.get(i, 0) + 3 * client_failures.get(i, 0))]
    if temp_client not in _streamer_by_client:
        idx = next((i for i, c in multi_clients.items() if c is temp_client), -1)
        _streamer_by_client[temp_client] = ByteStreamer(temp_client, idx)
    temp_streamer = _streamer_by_client[temp_client]

    file_id = await temp_streamer.get_file_properties(chat_id=chat_id, message_id=msg_id)

    if file_id.unique_id[:6] != secure_hash:
            raise InvalidHash

    target_dc = file_id.dc_id
    LOGGER.debug(f"File msg_id={msg_id} is in DC {target_dc}")

    index = select_best_client(target_dc)
    tg_client = multi_clients[index]

    if tg_client not in _streamer_by_client:
        _streamer_by_client[tg_client] = ByteStreamer(tg_client, index)
    streamer: ByteStreamer = _streamer_by_client[tg_client]

    file_size = file_id.file_size
    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    req_length = end - start + 1

    # Adaptive chunk size based on this client's recent measured throughput
    chunk_size = get_adaptive_chunk_size(index)
    offset = start - (start % chunk_size)
    first_part_cut = start - offset
    last_part_cut = (end % chunk_size) + 1
    if last_part_cut == 1 and end >= chunk_size:
        last_part_cut = chunk_size
    part_count = math.ceil(end / chunk_size) - math.floor(offset / chunk_size)

    from urllib.parse import unquote
    
    stream_id = secrets.token_hex(8)
    
    # Extract original title from the URL path name, fallback to raw name
    decoded_name = unquote(request.path_params.get("name", ""))
    
    # Look up the real title from the database using the Stremio stream_id_hash
    db_title = None
    db_imdb_id = None
    db_cert_tr = None
    db_cert_de = None
    db_cert_us = None
    if stream_id_hash:
        db_title = await db.get_title_by_stream_id(stream_id_hash)
        LOGGER.info(f"Stream lookup for hash '{stream_id_hash}' returned title: {db_title}")
        # imdb_id ve sertifika alanlarını al — izleme geçmişi için
        try:
            _doc = await db.get_document_by_stream_id(stream_id_hash)
            if _doc:
                db_imdb_id = _doc.get("imdb_id")
                db_cert_tr = _doc.get("certification_tr")
                db_cert_de = _doc.get("certification_de")
                db_cert_us = _doc.get("certification_us")
        except Exception:
            pass

    final_title = db_title if db_title else decoded_name

    meta = {
        "request_path": str(request.url.path),
        "client_host": request.client.host if request.client else None,
        "title": final_title,
        "imdb_id": db_imdb_id,          # İzleme geçmişi / öneri kataloğu için
        "certification_tr": db_cert_tr,  # TR sertifikası
        "certification_de": db_cert_de,  # DE sertifikası
        "certification_us": db_cert_us,  # US sertifikası
        "user_name": token_data.get("name", "Unknown") if token_data else "Unknown",
        "user_token": token,  # Kullanıcı bazlı hız dengeleme için
    }

    prefetch_count = Telegram.PARALLEL
    parallelism = Telegram.PRE_FETCH

    # ── Hız limiti: kullanıcı bazlı > global config ──────────────────
    # token_data["limits"]["speed_limit_mbps"] varsa kullanıcıya özel limit,
    # yoksa config'deki HIZ_LIMITI global limiti, o da boşsa 0 (sınırsız).
    # Limit kullanıcı başınadır: kullanıcının kaç aktif stream'i varsa
    # toplam limit o sayıya bölünür ve her stream eşit pay alır.
    _global_rate = 0.0
    _raw = (Telegram.HIZ_LIMITI or "").strip()
    try:
        if _raw:
            _global_rate = float(_raw)
    except ValueError:
        pass

    _user_rate = 0.0
    if token_data:
        try:
            _user_rate = float(token_data.get("limits", {}).get("speed_limit_mbps") or 0)
        except (ValueError, TypeError):
            pass

    # Kullanıcı limiti varsa onu, yoksa global limiti kullan
    total_rate_limit_mbps = _user_rate if _user_rate > 0 else _global_rate
    # ─────────────────────────────────────────────────────────────────

    # Kullanıcının şu anki aktif stream sayısını bul ve limiti böl.
    # Bu yeni stream henüz ACTIVE_STREAMS'e eklenmedi, bu yüzden +1 ekliyoruz.
    if total_rate_limit_mbps > 0:
        user_active_count = sum(
            1 for s in ACTIVE_STREAMS.values()
            if s.get("status") == "active" and s.get("meta", {}).get("user_token") == token
        )
        per_stream_rate = total_rate_limit_mbps / (user_active_count + 1)
    else:
        per_stream_rate = 0.0

    body_gen = await streamer.prefetch_stream(
        file_id=file_id,
        client_index=index,
        offset=offset,
        first_part_cut=first_part_cut,
        last_part_cut=last_part_cut,
        part_count=part_count,
        chunk_size=chunk_size,
        prefetch=prefetch_count,
        stream_id=stream_id,
        meta=meta,
        parallelism=parallelism,
        request=request,
        rate_limit_mbps=per_stream_rate,
        chat_id=chat_id,
        message_id=msg_id,
    )

    # Yeni stream ACTIVE_STREAMS'e eklendi; mevcut diğer kullanıcı stream'lerini dengele.
    if total_rate_limit_mbps > 0:
        _rebalance_user_streams(token, total_rate_limit_mbps)

    asyncio.create_task(track_usage_from_stats(stream_id, token, token_data))

    # Stream kapandığında kullanıcı bazlı hız dengelemesini tetikle
    if total_rate_limit_mbps > 0:
        async def _rebalance_on_close():
            # Stream'in ACTIVE_STREAMS'den çıkmasını bekle (custom_dl ~3 sn sonra taşır)
            await asyncio.sleep(4)
            _rebalance_user_streams(token, total_rate_limit_mbps)
        asyncio.create_task(_rebalance_on_close())

    file_name = file_id.file_name or f"{secrets.token_hex(4)}.bin"
    mime_type = file_id.mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"

    if "." not in file_name and "/" in mime_type:
        file_name = f"{file_name}.{mime_type.split('/')[1]}"

    # HEAD: return headers only (no body), include Content-Length so the
    # client knows the file size without opening a stream.
    # GET: do NOT set Content-Length on the StreamingResponse.
    # If a Telegram chunk fetch times out mid-stream the generator exits early,
    # delivering fewer bytes than the declared length.  h11 enforces
    # Content-Length strictly and raises LocalProtocolError in that case.
    # Without Content-Length, uvicorn uses chunked transfer encoding which
    # handles early termination gracefully.  Stremio / media players
    # are fine with chunked 206 responses.

    # HEAD request support
    from fastapi.responses import Response as PlainResponse
    from urllib.parse import quote as urlquote

    def safe_content_disposition(fname: str, disposition: str = "inline") -> str:
        """
        RFC 5987 uyumlu Content-Disposition üretir.
        ASCII-dışı (Türkçe vb.) karakterler latin-1'e encode edilemez;
        filename* parametresiyle UTF-8 olarak gönderilir.
        Arşiv dosyaları (zip/7z) için disposition='attachment' kullanılır.
        """
        try:
            ascii_name = fname.encode("latin-1").decode("latin-1")
            return f'{disposition}; filename="{ascii_name}"'
        except (UnicodeEncodeError, UnicodeDecodeError):
            encoded = urlquote(fname, safe="")
            return f"{disposition}; filename*=UTF-8''{encoded}"

    import re as _re_disp
    def _is_archive_fn(n: str) -> bool:
        nl = n.lower()
        if nl.endswith((".zip", ".7z", ".rar")):
            return True
        if _re_disp.search(r'\.(zip|7z|rar|z)\.\d+$', nl):
            return True
        if _re_disp.search(r'\.part\d+\.rar$', nl):
            return True
        return False
    _content_disp = "attachment" if (_is_archive_fn(file_name) or force_download) else "inline"

    if request.method == "HEAD":
        headers = {
            "Content-Type": mime_type,
            "Content-Length": str(req_length),
            "Content-Disposition": safe_content_disposition(file_name, _content_disp),
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
        }

        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        return PlainResponse(
            status_code=206 if range_header else 200,
            headers=headers,
        )

    # GET streaming response: Content-Length GÖNDERİLİR.
    # Bu sayede indirme sırasında tarayıcı/oynatıcı toplam boyutu görür ve
    # ilerleme çubuğu doğru çalışır.
    # NOT: Telegram chunk fetch başarısız olursa generator erken çıkabilir;
    # bu durumda h11 LocalProtocolError fırlatabilir — bu kabul edilebilir
    # bir trade-off'tur, eski (orijinal) sistem de bu şekilde çalışıyordu.
    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(req_length),
        "Content-Disposition": safe_content_disposition(file_name, _content_disp),
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }

    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        status = 206
    else:
        status = 200

    return StreamingResponse(
        body_gen,
        headers=headers,
        status_code=status,
        media_type=mime_type,
    )


@router.get("/stream/stats")
async def get_stream_stats(_: bool = Depends(_require_admin)):
    now = time.time()
    PRUNE_SECONDS = 3

    for sid, info in list(ACTIVE_STREAMS.items()):
        status = info.get("status")
        # Check end_ts first, which is set when a stream organically finishes
        last_ts = info.get("end_ts") or info.get("last_ts") or info.get("start_ts", now)
        if status in ("cancelled", "error", "finished"):
            if now - last_ts > PRUNE_SECONDS:
                try:
                    RECENT_STREAMS.appendleft(ACTIVE_STREAMS.pop(sid))
                except KeyError:
                    pass

    active = []
    for sid, info in ACTIVE_STREAMS.items():
        # Sadece gerçekten aktif ve veri transfer etmiş stream'leri göster
        if info.get("status") != "active":
            continue
        if (info.get("total_bytes") or 0) <= 0:
            continue
        meta = info.get("meta", {})
        start_ts = info.get("start_ts") or now
        active.append(
            {
                "stream_id": sid,
                "msg_id": info.get("msg_id"),
                "chat_id": info.get("chat_id"),
                "title": meta.get("title"),
                "client_index": info.get("client_index"),
                "dc_id": info.get("dc_id"),
                "status": info.get("status"),
                "total_bytes": info.get("total_bytes"),
                "instant_mbps": round(info.get("instant_mbps", 0.0), 3),
                "avg_mbps": round(info.get("avg_mbps", 0.0), 3),
                "peak_mbps": round(info.get("peak_mbps", 0.0), 3),
                "duration": round(now - start_ts, 1),
                "start_ts": start_ts,
                "meta": {
                    "title": meta.get("title"),
                    "user_name": meta.get("user_name"),
                    "client_host": meta.get("client_host"),
                    "user_token": meta.get("user_token"),
                },
            }
        )

    recent = []
    for info in RECENT_STREAMS:
        recent.append(
            {
                "stream_id": info.get("stream_id"),
                "msg_id": info.get("msg_id"),
                "chat_id": info.get("chat_id"),
                "title": info.get("meta", {}).get("title"),
                "client_index": info.get("client_index"),
                "dc_id": info.get("dc_id"),
                "status": info.get("status"),
                "total_bytes": info.get("total_bytes"),
                "duration": info.get("duration"),
                "avg_mbps": round(info.get("avg_mbps", 0.0), 3),
                "start_ts": info.get("start_ts"),
                "end_ts": info.get("end_ts"),
            }
        )

    return JSONResponse(
        {
            "active_streams": active,
            "recent_streams": recent,
            "client_dc_map": client_dc_map,
            "work_loads": work_loads,
        }
    )

@router.get("/stream/stats/{stream_id}")
async def get_stream_detail(stream_id: str, _: bool = Depends(_require_admin)):
    info = ACTIVE_STREAMS.get(stream_id)
    if info:
        return JSONResponse(make_json_safe(info))

    for rec in RECENT_STREAMS:
        if rec.get("stream_id") == stream_id:
            return JSONResponse(make_json_safe(rec))

    raise HTTPException(status_code=404, detail="Stream not found")
