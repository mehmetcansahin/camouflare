from __future__ import annotations

import asyncio

import pytest

from camouflare.runtime import shutdown_runtime


@pytest.mark.anyio
async def test_shutdown_starts_all_cleanup_even_when_one_resource_fails() -> None:
    events: list[str] = []

    class Sessions:
        async def close(self) -> None:
            events.append("sessions")
            raise RuntimeError("session cleanup failed")

    class Pool:
        async def close(self) -> None:
            events.append("pool")

    await shutdown_runtime(sessions=Sessions(), pool=Pool(), timeout_seconds=1)

    assert set(events) == {"sessions", "pool"}


@pytest.mark.anyio
async def test_shutdown_uses_one_deadline_for_concurrent_cleanup() -> None:
    started = asyncio.Event()
    cancelled: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.append(self.name)
                raise

    await shutdown_runtime(
        sessions=Resource("sessions"),
        pool=Resource("pool"),
        timeout_seconds=0.01,
    )

    assert started.is_set()
    assert set(cancelled) == {"sessions", "pool"}
