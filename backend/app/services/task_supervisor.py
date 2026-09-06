from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any


logger = logging.getLogger("breaktwenty.task_supervisor")

_tasks: set[asyncio.Task[Any]] = set()
_accepting_tasks = False


def start_task_supervisor() -> None:
    global _accepting_tasks
    _accepting_tasks = True


def create_tracked_task(
    coroutine: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    if not _accepting_tasks:
        coroutine.close()
        raise RuntimeError("Background task admission is closed")
    task = asyncio.create_task(coroutine, name=name)
    _tasks.add(task)
    task.add_done_callback(_task_done)
    return task


def _task_done(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return
    if error is not None:
        logger.error(
            "supervised background task failed task=%s error_type=%s",
            task.get_name(),
            type(error).__name__,
        )


def supervised_task_count() -> int:
    return len(_tasks)


async def stop_task_supervisor(*, timeout_seconds: float = 20.0) -> None:
    global _accepting_tasks
    _accepting_tasks = False
    current = asyncio.current_task()
    pending = [task for task in tuple(_tasks) if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    if not pending:
        return
    done, still_pending = await asyncio.wait(pending, timeout=max(0.0, timeout_seconds))
    for task in done:
        _tasks.discard(task)
    if still_pending:
        logger.error(
            "background task shutdown deadline exceeded pending=%s",
            len(still_pending),
        )

