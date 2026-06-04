"""
Tests for the in-process background TaskQueue: execution + retry-on-failure.
Uses fresh TaskQueue instances (not the module singleton) so tests are isolated.
"""
import asyncio

import pytest

from app.task_queue import TaskQueue


@pytest.mark.asyncio
async def test_task_runs():
    q = TaskQueue()
    q.start()
    ran = asyncio.Event()

    async def job():
        ran.set()

    await q.enqueue(job)
    await asyncio.wait_for(ran.wait(), timeout=2)
    await q.stop()
    assert ran.is_set()


@pytest.mark.asyncio
async def test_task_retries_then_succeeds():
    q = TaskQueue()
    q.start()
    calls = {"n": 0}
    done = asyncio.Event()

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient failure")
        done.set()

    await q.enqueue(flaky)
    # First attempt fails → non-blocking retry after ~1s backoff → succeeds.
    await asyncio.wait_for(done.wait(), timeout=6)
    await q.stop()
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_failing_task_does_not_stop_the_queue():
    q = TaskQueue()
    q.start()
    ok = asyncio.Event()

    async def boom():
        raise RuntimeError("boom")

    async def good():
        ok.set()

    await q.enqueue(boom)   # fails, schedules a non-blocking retry
    await q.enqueue(good)   # must still run despite the failing task
    await asyncio.wait_for(ok.wait(), timeout=3)
    await q.stop()
    assert ok.is_set()
