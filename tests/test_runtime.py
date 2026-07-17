from __future__ import annotations

import asyncio

import pytest

from camouflare.pool import BrowserPool
from camouflare.runtime import shutdown_runtime
from tests.fakes import FakeBrowserFactory


@pytest.mark.anyio
async def test_shutdown_continues_in_order_when_session_cleanup_fails() -> None:
    events: list[str] = []

    class Sessions:
        async def close(self) -> None:
            events.append("sessions")
            raise RuntimeError("session cleanup failed")

    class Pool:
        async def quiesce(self) -> None:
            events.append("quiesce")

        async def close(self) -> None:
            events.append("pool")

    await shutdown_runtime(sessions=Sessions(), pool=Pool(), timeout_seconds=1)

    assert events == ["quiesce", "sessions", "pool"]


@pytest.mark.anyio
async def test_shutdown_continues_when_owned_phase_self_cancels() -> None:
    events: list[str] = []

    class Sessions:
        async def close(self) -> None:
            events.append("sessions")
            raise asyncio.CancelledError

    class Cleanup:
        async def close(self) -> None:
            events.append("cleanup")

    class Pool:
        async def quiesce(self) -> None:
            events.append("quiesce")

        async def close(self) -> None:
            events.append("pool")

    await shutdown_runtime(
        sessions=Sessions(),
        pool=Pool(),
        cleanup=Cleanup(),
        timeout_seconds=1,
    )

    assert events == ["quiesce", "sessions", "pool", "cleanup"]


@pytest.mark.anyio
async def test_shutdown_drains_owned_cleanup_after_closing_pool() -> None:
    events: list[str] = []

    class Sessions:
        async def close(self) -> None:
            events.append("sessions")

    class Cleanup:
        async def close(self) -> None:
            events.append("cleanup")

    class Pool:
        async def quiesce(self) -> None:
            events.append("quiesce")

        async def close(self) -> None:
            events.append("pool")

    await shutdown_runtime(
        sessions=Sessions(),
        cleanup=Cleanup(),
        pool=Pool(),
        timeout_seconds=1,
    )

    assert events == ["quiesce", "sessions", "pool", "cleanup"]


@pytest.mark.anyio
async def test_shutdown_uses_one_deadline_for_ordered_cleanup() -> None:
    events: list[str] = []
    session_cancelled = asyncio.Event()

    class Sessions:
        async def close(self) -> None:
            events.append("sessions")
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                session_cancelled.set()
                raise

    class Pool:
        async def quiesce(self) -> None:
            events.append("quiesce")

        async def close(self) -> None:
            events.append("pool")

    await shutdown_runtime(
        sessions=Sessions(),
        pool=Pool(),
        timeout_seconds=0.01,
    )

    assert session_cancelled.is_set()
    assert events == ["quiesce", "sessions", "pool"]


@pytest.mark.anyio
async def test_quiesced_pool_closes_browser_after_late_persistent_release() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(browser_factory=factory, min_browsers=1, max_browsers=1)
    await pool.start()
    persistent = await pool.create_persistent_context()
    finish_session = asyncio.Event()

    class Sessions:
        async def close(self) -> None:
            try:
                await finish_session.wait()
            except asyncio.CancelledError:
                await finish_session.wait()
            await persistent.close()

    await shutdown_runtime(sessions=Sessions(), pool=pool, timeout_seconds=0.01)
    assert factory.created[0].closed is False

    finish_session.set()
    for _ in range(30):
        if factory.created[0].closed:
            break
        await asyncio.sleep(0)

    assert factory.created[0].closed is True
    assert pool.snapshot().browser_slots == 0
