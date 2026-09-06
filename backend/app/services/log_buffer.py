from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

PROVIDER_LOG_BUFFER_BYTES_PER_PROVIDER = 4 * 1024 * 1024


class ProviderLogRingBuffer:
    def __init__(self, *, max_bytes_per_provider: int = PROVIDER_LOG_BUFFER_BYTES_PER_PROVIDER) -> None:
        self._max_bytes = int(max_bytes_per_provider)
        self._buffers: dict[
            str,
            deque[tuple[float, str, int, int | None, str | None, str | None]],
        ] = {}
        self._sizes: dict[str, int] = {}
        self._lock = Lock()

    def append(
        self,
        provider: str,
        timestamp: float,
        formatted: str,
        *,
        user_id: int | None = None,
        sync_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        key = str(provider or "").strip().lower()
        if not key:
            return
        line_size = len(formatted.encode("utf-8")) + 1
        with self._lock:
            buf = self._buffers.setdefault(key, deque())
            buf.append(
                (
                    float(timestamp),
                    formatted,
                    line_size,
                    int(user_id) if user_id is not None else None,
                    str(sync_id or "").strip() or None,
                    str(attempt_id or "").strip() or None,
                )
            )
            current_size = self._sizes.get(key, 0) + line_size
            while current_size > self._max_bytes and len(buf) > 1:
                _, _, evicted, _, _, _ = buf.popleft()
                current_size -= evicted
            self._sizes[key] = current_size

    def snapshot(
        self,
        provider: str,
        *,
        since_ts: float | None = None,
        until_ts: float | None = None,
        user_id: int | None = None,
        sync_id: str | None = None,
        attempt_id: str | None = None,
    ) -> list[tuple[float, str]]:
        key = str(provider or "").strip().lower()
        with self._lock:
            buf = list(self._buffers.get(key, ()))
        results: list[tuple[float, str]] = []
        normalized_sync_id = str(sync_id or "").strip()
        normalized_attempt_id = str(attempt_id or "").strip()
        for ts, formatted, _size, row_user_id, row_sync_id, row_attempt_id in buf:
            if since_ts is not None and ts < since_ts:
                continue
            if until_ts is not None and ts > until_ts:
                continue
            if user_id is not None and row_user_id != int(user_id):
                continue
            if normalized_sync_id:
                if row_sync_id != normalized_sync_id:
                    continue
            elif normalized_attempt_id:
                if row_attempt_id != normalized_attempt_id:
                    continue
            elif row_sync_id or row_attempt_id:
                continue
            results.append((ts, formatted))
        return results

_provider_log_buffer = ProviderLogRingBuffer()


class ProviderLogBufferHandler(logging.Handler):
    def __init__(self, buffer: ProviderLogRingBuffer | None = None) -> None:
        super().__init__(level=logging.DEBUG)
        self._buffer = buffer or _provider_log_buffer

    def emit(self, record: logging.LogRecord) -> None:
        provider = getattr(record, "breaktwenty_provider", None)
        if not provider:
            return
        try:
            formatted = self.format(record)
        except Exception:
            return
        try:
            self._buffer.append(
                str(provider),
                record.created,
                formatted,
                user_id=getattr(record, "breaktwenty_user_id", None),
                sync_id=getattr(record, "breaktwenty_sync_id", None),
                attempt_id=getattr(record, "breaktwenty_attempt_id", None),
            )
        except Exception:
            return


def render_buffer_snapshot(
    provider: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    user_id: int | None = None,
    sync_id: str | None = None,
    attempt_id: str | None = None,
) -> str:
    since_ts = since.timestamp() if isinstance(since, datetime) else None
    until_ts = until.timestamp() if isinstance(until, datetime) else None
    rows = _provider_log_buffer.snapshot(
        provider,
        since_ts=since_ts,
        until_ts=until_ts,
        user_id=user_id,
        sync_id=sync_id,
        attempt_id=attempt_id,
    )
    return "\n".join(formatted for _ts, formatted in rows)


def buffer_metadata(
    provider: str,
    *,
    user_id: int | None = None,
    sync_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    key = str(provider or "").strip().lower()
    rows = _provider_log_buffer.snapshot(
        key,
        user_id=user_id,
        sync_id=sync_id,
        attempt_id=attempt_id,
    )
    encoded_size = sum(len(formatted.encode("utf-8")) + 1 for _ts, formatted in rows)
    oldest_ts = rows[0][0] if rows else None
    newest_ts = rows[-1][0] if rows else None
    return {
        "provider": key,
        "scope_kind": "sync_attempt" if sync_id or attempt_id else "unscoped_events",
        "attempt_sync_id": str(sync_id or "") or None,
        "attempt_id": str(attempt_id or "") or None,
        "line_count": len(rows),
        "bytes": int(encoded_size),
        "max_bytes_per_provider": _provider_log_buffer._max_bytes,  # noqa: SLF001
        "oldest_timestamp": (
            datetime.fromtimestamp(oldest_ts, tz=timezone.utc).isoformat()
            if oldest_ts is not None
            else None
        ),
        "newest_timestamp": (
            datetime.fromtimestamp(newest_ts, tz=timezone.utc).isoformat()
            if newest_ts is not None
            else None
        ),
    }
