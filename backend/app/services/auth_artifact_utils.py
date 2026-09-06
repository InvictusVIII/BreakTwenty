from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import TypeVar

T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_provider(provider: str | None) -> str:
    return str(provider or "").strip().lower()


def normalize_artifact_kind(artifact_kind: str | None) -> str:
    return str(artifact_kind or "").strip()


def run_async_from_sync(awaitable: Awaitable[T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
    raise RuntimeError(
        "Synchronous auth-artifact adapters cannot run on an active event loop; "
        "use the async artifact API"
    )
