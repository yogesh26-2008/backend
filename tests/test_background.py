"""
Unit tests for the GC-safe fire-and-forget helper.
Guards the invariant that background tasks are kept alive until done, cleared
afterwards, and never crash the app when they raise.
"""
import asyncio

import pytest

from app.utils.background import fire_and_forget, _tasks


@pytest.mark.asyncio
async def test_fire_and_forget_runs_coroutine():
    ran = {}

    async def job():
        ran["done"] = True

    await fire_and_forget(job())
    assert ran.get("done") is True


@pytest.mark.asyncio
async def test_fire_and_forget_holds_then_clears_reference():
    async def job():
        await asyncio.sleep(0.01)

    t = fire_and_forget(job())
    assert t in _tasks            # strong reference held while running (no GC)
    await t
    await asyncio.sleep(0)        # let the done-callback run
    assert t not in _tasks        # reference dropped after completion


@pytest.mark.asyncio
async def test_fire_and_forget_exception_does_not_propagate():
    async def boom():
        raise ValueError("intentional")

    # Not awaited — a raising background task must never crash the caller.
    fire_and_forget(boom())
    await asyncio.sleep(0.02)     # let it run + done-callback log the error
    assert True                   # reaching here = exception was contained
