"""In-memory ring buffer of recent log records, exposed to admins in the UI.

A single handler is attached to the root logger at startup; it keeps the last
`capacity` records so the admin console can show recent server activity without
shipping logs off-box or reading files. Bounded + lock-guarded, so it's safe
under concurrent requests and can't grow without limit.
"""
from __future__ import annotations

import logging
from collections import deque
from threading import Lock

_handler: RingBufferHandler | None = None


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 500):
        super().__init__()
        self._buf: deque[dict] = deque(maxlen=capacity)
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — logging must never raise
            return
        with self._lock:
            self._buf.append({
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "msg": msg,
            })

    def records(self, limit: int = 200, level: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._buf)
        if level:
            want = level.upper()
            items = [r for r in items if r["level"] == want]
        return items[-limit:]


def install(capacity: int = 500) -> RingBufferHandler:
    """Attach the ring buffer to the root logger (idempotent)."""
    global _handler
    if _handler is None:
        _handler = RingBufferHandler(capacity)
        _handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(_handler)
    return _handler


def get_handler() -> RingBufferHandler | None:
    return _handler
