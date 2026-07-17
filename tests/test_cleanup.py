from __future__ import annotations

import asyncio

import pytest

from camouflare.cleanup import CleanupScope, CleanupSupervisor


@pytest.mark.anyio
async def test_supervisor_tracks_and_consumes_task_failure() -> None:
    supervisor = CleanupSupervisor(timeout_seconds=0.1)

    async def fail() -> None:
        raise RuntimeError("boom")

    supervisor.start(fail(), kind="context")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert supervisor.snapshot().in_flight == 0


@pytest.mark.anyio
async def test_supervisor_run_has_hard_timeout_and_drains_cancelled_task() -> None:
    supervisor = CleanupSupervisor(timeout_seconds=0.01)
    blocker = asyncio.Event()

    with pytest.raises(TimeoutError, match="page cleanup exceeded"):
        await supervisor.run(blocker.wait(), kind="page")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert supervisor.snapshot().in_flight == 0


@pytest.mark.anyio
async def test_cancelling_waiter_does_not_cancel_owned_cleanup() -> None:
    supervisor = CleanupSupervisor(timeout_seconds=1)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await finish.wait()

    waiter = asyncio.create_task(supervisor.run(cleanup(), kind="browser"))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert supervisor.snapshot().by_kind == {"browser": 1}
    finish.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert supervisor.snapshot().in_flight == 0


@pytest.mark.anyio
async def test_cancelling_waiter_keeps_independent_cleanup_deadline() -> None:
    supervisor = CleanupSupervisor(timeout_seconds=0.01)
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    allow_finish = asyncio.Event()

    async def stubborn_cleanup() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_finish.wait()

    waiter = asyncio.create_task(supervisor.run(stubborn_cleanup(), kind="context"))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    await asyncio.wait_for(cancellation_seen.wait(), timeout=0.1)
    assert supervisor.snapshot().by_kind == {"context": 1}

    allow_finish.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert supervisor.snapshot().in_flight == 0


@pytest.mark.anyio
async def test_tracking_existing_task_applies_cleanup_deadline() -> None:
    supervisor = CleanupSupervisor(timeout_seconds=0.01)
    cancellation_seen = asyncio.Event()

    async def cleanup() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancellation_seen.set()

    task = asyncio.create_task(cleanup())
    supervisor.track(task, kind="dispatch")

    await asyncio.wait_for(cancellation_seen.wait(), timeout=0.1)
    await asyncio.sleep(0)
    assert supervisor.snapshot().in_flight == 0


@pytest.mark.anyio
async def test_timeout_result_survives_cleanup_return_race() -> None:
    for _ in range(100):
        supervisor = CleanupSupervisor(timeout_seconds=0.001)

        async def returns_after_cancellation() -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return "too late"

        with pytest.raises(TimeoutError):
            await supervisor.run(returns_after_cancellation(), kind="page")


@pytest.mark.anyio
async def test_nested_cleanup_inherits_cancelled_owner_absolute_deadline() -> None:
    supervisor = CleanupSupervisor(timeout_seconds=1)
    owner_started = asyncio.Event()
    nested_cancelled = asyncio.Event()

    async def owner() -> None:
        try:
            owner_started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await supervisor.run(asyncio.sleep(0.03), kind="page", timeout_seconds=1)

            async def nested() -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    nested_cancelled.set()

            await supervisor.run(nested(), kind="context", timeout_seconds=1)

    task = asyncio.create_task(owner())
    await owner_started.wait()
    started = asyncio.get_running_loop().time()
    task.cancel()
    supervisor.track(task, kind="request", timeout_seconds=0.05)

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(nested_cancelled.wait(), timeout=0.1)
    assert asyncio.get_running_loop().time() - started < 0.09


@pytest.mark.anyio
async def test_close_drains_cleanup_spawned_during_parent_cancellation() -> None:
    supervisor = CleanupSupervisor(timeout_seconds=1)
    parent_started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def child() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            child_cancelled.set()

    async def parent() -> None:
        try:
            parent_started.set()
            await asyncio.Event().wait()
        finally:
            supervisor.start(child(), kind="context")

    supervisor.start(parent(), kind="request")
    await parent_started.wait()
    await supervisor.close(timeout_seconds=0.1)

    await asyncio.wait_for(child_cancelled.wait(), timeout=0.1)
    assert supervisor.snapshot().in_flight == 0


@pytest.mark.anyio
async def test_existing_future_adopts_narrower_deadline_and_additional_group() -> None:
    supervisor = CleanupSupervisor(timeout_seconds=0.2)
    cancellation_seen = asyncio.Event()
    allow_finish = asyncio.Event()

    async def stubborn_cleanup() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_finish.wait()

    task = supervisor.start(stubborn_cleanup(), kind="browser", timeout_seconds=0.2)
    original_group = supervisor.group_for(task)
    alias_group = object()
    scope = CleanupScope(
        deadline=asyncio.get_running_loop().time() + 0.02,
        group=alias_group,
    )
    started = asyncio.get_running_loop().time()

    with pytest.raises(TimeoutError):
        await supervisor.run(
            task,
            kind="browser",
            timeout_seconds=0.2,
            scope=scope,
        )

    assert asyncio.get_running_loop().time() - started < 0.08
    await asyncio.wait_for(cancellation_seen.wait(), timeout=0.1)
    original_waiter = asyncio.create_task(supervisor.wait_for_group(original_group))
    alias_waiter = asyncio.create_task(supervisor.wait_for_group(alias_group))
    await asyncio.sleep(0)
    assert not original_waiter.done()
    assert not alias_waiter.done()

    allow_finish.set()
    await asyncio.wait_for(asyncio.gather(original_waiter, alias_waiter), timeout=0.1)
    assert supervisor.snapshot().in_flight == 0
