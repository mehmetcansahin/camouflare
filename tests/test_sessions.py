from __future__ import annotations

import asyncio

import pytest

from camouflare.sessions import SessionManager
from tests.fakes import DelayedFakeSessionContext, FakeContext


@pytest.mark.anyio
async def test_register_list_and_destroy_session() -> None:
    manager = SessionManager(max_sessions=4, default_ttl_seconds=3600)
    context = FakeContext()

    session = manager.register_existing("abc", context, proxy={"server": "http://p:1"})
    _, duplicate_created = manager.register_or_get("abc", FakeContext())
    session_ids = manager.list_ids()
    destroyed = await manager.destroy("abc")

    assert session.session_id == "abc"
    assert duplicate_created is False
    assert session_ids == ["abc"]
    assert destroyed is True
    assert context.closed is True


@pytest.mark.anyio
async def test_session_lock_serializes_same_session_requests() -> None:
    events: list[str] = []
    manager = SessionManager(max_sessions=4, default_ttl_seconds=3600)
    context = DelayedFakeSessionContext(events)
    session = manager.register_existing("abc", context)

    async def use_session(label: str) -> None:
        async with session.lock:
            events.append(f"{label}_entered")
            await context.new_page()
            events.append(f"{label}_leaving")

    await asyncio.gather(use_session("a"), use_session("b"))

    assert events == [
        "a_entered",
        "new_page_start",
        "new_page_end",
        "a_leaving",
        "b_entered",
        "new_page_start",
        "new_page_end",
        "b_leaving",
    ]


def test_session_expired_uses_stored_ttl() -> None:
    manager = SessionManager(max_sessions=4, default_ttl_seconds=3600)

    short_lived = manager.register_existing("short", FakeContext(), ttl_seconds=0)
    default_lived = manager.register_existing("default", FakeContext())

    assert short_lived.expired() is True
    # ttl_seconds omitted -> registered with the manager default (3600s), so still fresh
    assert default_lived.expired() is False


@pytest.mark.anyio
async def test_prune_expired_closes_only_expired_idle_sessions() -> None:
    manager = SessionManager(max_sessions=4, default_ttl_seconds=3600)
    expired_context = DelayedFakeSessionContext([])
    fresh_context = DelayedFakeSessionContext([])
    expired = manager.register_existing("expired", expired_context)
    manager.register_existing("fresh", fresh_context)
    expired.created_at -= 7200

    pruned = await manager.prune_expired()

    assert pruned == ["expired"]
    assert expired_context.closed is True
    assert fresh_context.closed is False
    assert manager.list_ids() == ["fresh"]


def test_register_or_get_returns_existing_instead_of_raising() -> None:
    manager = SessionManager(max_sessions=4, default_ttl_seconds=3600)
    first_context = FakeContext()
    second_context = FakeContext()

    first, created_first = manager.register_or_get("abc", first_context)
    second, created_second = manager.register_or_get("abc", second_context)

    assert created_first is True
    assert created_second is False
    assert second is first


@pytest.mark.anyio
async def test_prune_expired_honors_per_session_ttl() -> None:
    manager = SessionManager(max_sessions=4, default_ttl_seconds=60)
    long_context = FakeContext()
    long_lived = manager.register_existing("long", long_context, ttl_seconds=3600)
    long_lived.created_at -= 120  # older than the 60s default, within its own 3600s ttl

    pruned = await manager.prune_expired()

    assert pruned == []
    assert long_context.closed is False


@pytest.mark.anyio
async def test_prune_skips_in_use_session_even_when_expired() -> None:
    manager = SessionManager(max_sessions=4, default_ttl_seconds=0)
    context = FakeContext()
    session = manager.register_existing("abc", context)  # ttl 0 -> already expired

    # A request has checked the session out but not yet acquired its lock.
    session.in_use = 1
    assert await manager.prune_expired() == []
    assert context.closed is False

    # Once no request holds it, it prunes normally.
    session.in_use = 0
    assert await manager.prune_expired() == ["abc"]
    assert context.closed is True


@pytest.mark.anyio
async def test_destroy_waits_for_in_flight_lock_holder() -> None:
    manager = SessionManager(max_sessions=4, default_ttl_seconds=3600)
    context = FakeContext()
    session = manager.register_existing("abc", context)

    await session.lock.acquire()  # simulate an in-flight request holding the session
    destroy_task = asyncio.create_task(manager.destroy("abc"))
    await asyncio.sleep(0)  # give destroy a chance to pop and block on the lock

    # The session is unregistered immediately, but its context must not be closed
    # while the in-flight request still holds the lock.
    assert manager.get("abc") is None
    assert context.closed is False
    assert not destroy_task.done()

    session.lock.release()
    assert await destroy_task is True
    assert context.closed is True


@pytest.mark.anyio
async def test_shutdown_closes_remaining_sessions_after_one_close_failure() -> None:
    events: list[str] = []

    class Context(FakeContext):
        def __init__(self, name: str, *, fail: bool = False) -> None:
            super().__init__()
            self.name = name
            self.fail = fail

        async def close(self) -> None:
            events.append(self.name)
            self.closed = True
            if self.fail:
                raise RuntimeError(f"{self.name} close failed")

    manager = SessionManager(max_sessions=2, default_ttl_seconds=60)
    failing = Context("failing", fail=True)
    healthy = Context("healthy")
    manager.register_existing("failing", failing)
    manager.register_existing("healthy", healthy)

    await manager.close()

    assert set(events) == {"failing", "healthy"}
    assert failing.closed is True
    assert healthy.closed is True


def test_session_snapshot_tracks_active_and_checked_out_sessions() -> None:
    manager = SessionManager(max_sessions=3, default_ttl_seconds=60)
    session = manager.register_existing("active", FakeContext())

    assert manager.snapshot().active == 1
    assert manager.snapshot().in_use == 0
    assert manager.snapshot().max_sessions == 3

    manager.mark_in_use(session)
    assert manager.snapshot().in_use == 1

    manager.mark_released(session)
    assert manager.snapshot().in_use == 0
