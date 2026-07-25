class InvalidHash(Exception):
    message = 'Invalid hash!'


class FIleNotFound(Exception):
    message = 'File not found!'


class ChunkFetchError(Exception):
    """Raised when a chunk could not be fetched after exhausting all retries.

    This must NEVER be silently swallowed as "end of part" — callers (e.g.
    virtual_stream_generator) must stop/abort the stream instead of moving
    on to the next physical part, otherwise the player ends up splicing in
    the wrong file's bytes (looks like an unwanted jump to the next part).
    """
    def __init__(self, stream_id: str, seq_idx: int = None, message: str = None):
        self.stream_id = stream_id
        self.seq_idx = seq_idx
        self.message = message or f"Chunk fetch failed for stream={stream_id} seq={seq_idx}"
        super().__init__(self.message)