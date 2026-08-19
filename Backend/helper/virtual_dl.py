import math
import time
from typing import Dict, List, Optional, Tuple

from fastapi import Request

from Backend.logger import LOGGER
from Backend.helper.custom_dl import ByteStreamer
from Backend.helper.exceptions import ChunkFetchError


async def resolve_virtual_parts(
    parts_payload: List[dict],
    streamer: ByteStreamer,
) -> Tuple[List[Dict], int]:
    """Fetch Telegram file metadata for every part (in order) and compute
    cumulative virtual offsets. Returns (parts, total_size)."""
    parts: List[Dict] = []
    cum = 0
    for idx, p in enumerate(parts_payload):
        chat_id = int(f"-100{p['chat_id']}")
        msg_id = int(p["msg_id"])
        file_id = await streamer.get_file_properties(chat_id=chat_id, message_id=msg_id)
        size = file_id.file_size
        parts.append({
            "index": idx,
            "chat_id": chat_id,
            "msg_id": msg_id,
            "file_id": file_id,
            "size": size,
            "cum_start": cum,
        })
        cum += size
    return parts, cum


def parts_overlapping_range(parts: List[Dict], start: int, end: int) -> List[Dict]:
    return [p for p in parts if not (p["cum_start"] + p["size"] - 1 < start or p["cum_start"] > end)]


async def virtual_stream_generator(
    parts: List[Dict],
    start: int,
    end: int,
    chunk_size: int,
    streamer: ByteStreamer,
    client_index: int,
    request: Optional[Request],
    meta: Optional[dict],
    stream_id: str,
    parallelism: int,
    prefetch_count: int,
):
    """Yields raw bytes covering the virtual range [start, end] (inclusive),
    crossing as many underlying parts as needed, completely transparently
    to the caller."""
    pos = start
    for part in parts:
        part_start = part["cum_start"]
        part_end = part_start + part["size"] - 1
        if part_end < pos:
            continue
        if part_start > end:
            break

        local_start = max(pos, part_start) - part_start
        local_end = min(end, part_end) - part_start

        offset = local_start - (local_start % chunk_size)
        first_part_cut = local_start - offset
        # last_part_cut: son chunk içinde kaç byte alınacağı.
        # local_end % chunk_size == chunk_size-1 → tam chunk → chunk_size byte
        # local_end % chunk_size == 0 → sadece ilk byte → 1 byte (BUG: bu durum
        # hiç oluşmamalı çünkü local_end 0-indexed; düzeltilmiş formül:
        _rem = local_end % chunk_size
        last_part_cut = chunk_size if _rem == chunk_size - 1 else _rem + 1
        part_count = (local_end // chunk_size) - (offset // chunk_size) + 1

        body_gen = await streamer.prefetch_stream(
            file_id=part["file_id"],
            client_index=client_index,
            offset=offset,
            first_part_cut=first_part_cut,
            last_part_cut=last_part_cut,
            part_count=part_count,
            chunk_size=chunk_size,
            prefetch=prefetch_count,
            stream_id=f"{stream_id}-p{part['index']}",
            meta=meta,
            parallelism=parallelism,
            request=request,
            chat_id=part["chat_id"],
            message_id=part["msg_id"],
        )

        try:
            async for chunk in body_gen:
                yield chunk
        except ChunkFetchError:
            # KRİTİK: part['index'] içindeki veri tükenmeden bir chunk fetch
            # hatası yüzünden buraya düştük. Bunu "bu parça bitti, sıradaki
            # fiziksel parçaya (mkv.002 vb.) geç" diye yorumlamak YANLIŞ —
            # yanlış dosyanın byte'larını video akışına ekleyip oynatıcıyı
            # bozardı. Bunun yerine stream'i burada açıkça durduruyoruz.
            LOGGER.error(
                "Virtual stream %s: part %s (msg_id=%s) içinde kurtarılamayan "
                "chunk hatası — sıradaki parçaya GEÇİLMİYOR, stream durduruluyor.",
                stream_id, part["index"], part.get("msg_id"),
            )
            raise

        # If the client disconnected mid-part, ByteStreamer's own consumer
        # already stopped yielding for us; don't waste bandwidth fetching
        # the next part on a dead connection.
        if request is not None:
            try:
                if await request.is_disconnected():
                    LOGGER.debug("Virtual stream %s: client gone, stopping at part %s", stream_id, part["index"])
                    return
            except Exception:
                pass

        pos = part_end + 1
        if pos > end:
            break
