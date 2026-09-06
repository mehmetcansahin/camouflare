from __future__ import annotations

import asyncio
import threading

import pytest

from camouflare.app import create_app
from camouflare.cleanup import CleanupSupervisor
from camouflare.config import Settings
from camouflare.models import V1Request
from camouflare.navigation import DirectHttpExecutor, RawResponse
from camouflare.pool import BrowserPool
from camouflare.runtime import (
    make_runtime_lifespan,
    pool_maintainer,
    session_reaper,
    shutdown_runtime,
)
from camouflare.sessions import SessionManager
from camouflare.timer import TimeoutTimer
from tests.fakes import FakeBrowserFactory, FakeContext


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
async def test_cancelling_shutdown_caller_still_initiates_every_phase() -> None:
    events: list[str] = []
    sessions_started = asyncio.Event()

    class Sessions:
        async def close(self) -> None:
            events.append("sessions")
            sessions_started.set()
            await asyncio.Future()

    class Pool:
        async def quiesce(self) -> None:
            events.append("quiesce")

        async def close(self) -> None:
            events.append("pool")

    class Cleanup:
        async def close(self) -> None:
            events.append("cleanup")

    class DirectHttp:
        async def quiesce(self) -> None:
            events.append("direct-quiesce")

        async def close(self) -> None:
            events.append("direct-close")

    shutdown = asyncio.create_task(
        shutdown_runtime(
            sessions=Sessions(),
            pool=Pool(),
            direct_http=DirectHttp(),
            cleanup=Cleanup(),
            timeout_seconds=0.01,
        )
    )
    await sessions_started.wait()
    shutdown.cancel()

    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert events == [
        "quiesce",
        "direct-quiesce",
        "sessions",
        "pool",
        "direct-close",
        "cleanup",
    ]


@pytest.mark.anyio
async def test_shutdown_quiesces_direct_http_and_owns_running_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = threading.Event()
    finish_worker = threading.Event()

    def blocking_get(*args: object, **kwargs: object) -> RawResponse:
        worker_started.set()
        finish_worker.wait()
        return RawResponse(url="https://example.com", status=200, headers={}, body="ok")

    monkeypatch.setattr("camouflare.navigation._direct_http_get_sync", blocking_get)

    class Resource:
        async def close(self) -> None:
            return None

    direct_http = DirectHttpExecutor(max_workers=1)
    request = asyncio.create_task(
        direct_http.get(
            "https://example.com",
            V1Request(cmd="request.get", url="https://example.com"),
            TimeoutTimer(1000),
        )
    )
    for _ in range(100):
        if worker_started.is_set():
            break
        await asyncio.sleep(0.001)

    try:
        await shutdown_runtime(
            sessions=Resource(),
            pool=Resource(),
            direct_http=direct_http,
            timeout_seconds=0.01,
        )

        assert direct_http.accepting is False
        assert direct_http.active_workers == 1
        with pytest.raises(RuntimeError, match="quiesced"):
            await direct_http.get(
                "https://example.com",
                V1Request(cmd="request.get", url="https://example.com"),
                TimeoutTimer(1000),
            )
    finally:
        finish_worker.set()
        await request
        await direct_http.close()
    assert direct_http.active_workers == 0


@pytest.mark.anyio
async def test_session_reaper_does_not_wait_for_stubborn_expiration_cleanup() -> None:
    stubborn_started = asyncio.Event()
    finish_stubborn = asyncio.Event()

    async def stubborn_close() -> None:
        stubborn_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await finish_stubborn.wait()

    cleanup = CleanupSupervisor(timeout_seconds=0.01)
    sessions = SessionManager(
        max_sessions=2,
        default_ttl_seconds=0,
        cleanup_timeout_seconds=0.01,
        cleanup_supervisor=cleanup,
    )
    sessions.register_existing("stubborn", FakeContext(), on_close=stubborn_close)
    reaper = asyncio.create_task(session_reaper(sessions, interval_seconds=0))
    await stubborn_started.wait()
    await asyncio.sleep(0.02)

    later = FakeContext()
    sessions.register_existing("later", later)
    for _ in range(40):
        if sessions.get("later") is None and later.closed:
            break
        await asyncio.sleep(0.002)

    assert sessions.get("later") is None
    assert later.closed is True
    assert sessions.is_closing("stubborn") is True

    reaper.cancel()
    await asyncio.gather(reaper, return_exceptions=True)
    finish_stubborn.set()
    await sessions.close()
    await cleanup.close(timeout_seconds=0.1)


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


@pytest.mark.anyio
async def test_pool_maintainer_keeps_ticking_after_a_failed_tick() -> None:
    ticks = 0

    class Pool:
        async def maintain(self) -> None:
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                raise RuntimeError("first tick failed")

    task = asyncio.create_task(pool_maintainer(Pool(), interval_seconds=0))
    for _ in range(100):
        if ticks >= 3:
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert ticks >= 3


@pytest.mark.anyio
async def test_runtime_lifespan_runs_the_pool_maintainer_beside_the_reaper() -> None:
    app = create_app(
        settings=Settings(pool_maintenance_interval_seconds=1),
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )

    async with make_runtime_lifespan(app.state.settings)(app):
        names = {task.get_name() for task in asyncio.all_tasks()}
        assert "camouflare-pool-maintainer" in names
        assert "camouflare-session-reaper" in names

    names = {task.get_name() for task in asyncio.all_tasks()}
    assert "camouflare-pool-maintainer" not in names
    assert "camouflare-session-reaper" not in names
    assert app.state.direct_http.accepting is False
