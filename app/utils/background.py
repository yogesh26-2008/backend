"""
background.py — Safe fire-and-forget for async tasks.

Plain `asyncio.create_task()` does NOT keep a reference to the task, so the
event loop may garbage-collect it mid-execution (see CPython docs:
"Save a reference to the result of this function, to avoid a task disappearing
mid-execution"). This helper holds a strong reference until the task finishes,
then drops it automatically — and logs any unhandled exception instead of
swallowing it silently.

Use for genuine fire-and-forget work (push notifications, presence broadcasts).
For work that must survive failures with retries, use `app.task_queue` instead.
"""

import asyncio
import logging
from typing import Coroutine, Optional, Set

logger = logging.getLogger(__name__)

# Strong references to in-flight fire-and-forget tasks (prevents GC).
_tasks: Set[asyncio.Task] = set()


def fire_and_forget(coro: Coroutine, *, name: Optional[str] = None) -> asyncio.Task:
    """Schedule a coroutine without awaiting it, safely holding a reference."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            f"[background] task {task.get_name()} failed: {type(exc).__name__}: {exc}"
        )
