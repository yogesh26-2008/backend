"""
task_queue.py
─────────────────────────────────────────────────────────────────────────────
Lightweight in-process background task queue with retry + exponential backoff.

Replaces fire-and-forget asyncio.create_task() calls across the codebase.
Tasks survive individual failures; the queue itself is durable across requests.

Design decisions
────────────────
- Single asyncio.Queue consumed by one worker coroutine per app process.
- 3 retries with exponential backoff (1s → 2s → 4s) before dead-letter log.
- Graceful shutdown: waits for in-flight task to finish before stopping.
- Zero new dependencies — works with existing setup.
- Drop-in upgrade path: swap enqueue() body for arq/rq when Redis queue needed.
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_RETRIES  = 3
_BASE_DELAY   = 1.0   # seconds — doubles on each retry (1 → 2 → 4)
_QUEUE_MAXSIZE = 1000  # prevent unbounded memory growth under load


class TaskQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Tuple] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background worker. Call once at app startup."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker(), name="task-queue-worker")
        logger.info("[TaskQueue] Worker started.")

    async def stop(self) -> None:
        """
        Gracefully stop the worker.
        Waits up to 10 seconds for the current task to finish.
        """
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._worker_task), timeout=10)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info("[TaskQueue] Worker stopped.")

    # ── Public API ────────────────────────────────────────────────────────────

    async def enqueue(self, fn: Callable[..., Coroutine], *args: Any, **kwargs: Any) -> None:
        """
        Schedule an async function for background execution.

        Usage (replaces asyncio.create_task):
            # Before:
            asyncio.create_task(send_notification(db, user_id))
            # After:
            await task_queue.enqueue(send_notification, db, user_id)
        """
        item = (fn, args, kwargs, 0)   # (fn, args, kwargs, attempt_number)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.error(
                f"[TaskQueue] Queue full ({_QUEUE_MAXSIZE} items) — "
                f"dropping task: {fn.__name__}"
            )

    # ── Internal worker ───────────────────────────────────────────────────────

    async def _worker(self) -> None:
        logger.info("[TaskQueue] Worker loop running.")
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break

            fn, args, kwargs, attempt = item
            task_name = getattr(fn, "__name__", repr(fn))

            try:
                await fn(*args, **kwargs)
                logger.debug(f"[TaskQueue] OK: {task_name} (attempt {attempt + 1})")

            except asyncio.CancelledError:
                # Propagate cancellation — don't swallow it
                self._queue.task_done()
                raise

            except Exception as exc:
                if attempt < _MAX_RETRIES:
                    delay = _BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"[TaskQueue] {task_name} failed "
                        f"(attempt {attempt + 1}/{_MAX_RETRIES + 1}), "
                        f"retrying in {delay:.0f}s — {type(exc).__name__}: {exc}"
                    )
                    await asyncio.sleep(delay)
                    # Re-enqueue with incremented attempt counter
                    retry_item = (fn, args, kwargs, attempt + 1)
                    try:
                        self._queue.put_nowait(retry_item)
                    except asyncio.QueueFull:
                        logger.error(
                            f"[TaskQueue] Queue full during retry — "
                            f"permanently dropping: {task_name}"
                        )
                else:
                    logger.error(
                        f"[TaskQueue] DEAD LETTER: {task_name} failed after "
                        f"{_MAX_RETRIES + 1} attempts — {type(exc).__name__}: {exc}"
                    )
            finally:
                self._queue.task_done()


# ── Module-level singleton ────────────────────────────────────────────────────
task_queue = TaskQueue()
