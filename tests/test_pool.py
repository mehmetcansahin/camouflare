from __future__ import annotations

import asyncio
import time

import pytest

from camouflare.browser import CamoufoxBrowserHandle
from camouflare.cleanup import CleanupSupervisor
from camouflare.pool import BrowserPool, PersistentCapacityError, PoolAcquireTimeout
from tests.fakes import DelayedFakeBrowserFactory, FakeBrowser, FakeBrowserFactory, FakeContext


@pytest.mark.anyio
async def test_pool_reuses_warm_browser_without_spawning_per_request() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=2,
        max_contexts_per_browser=2,
        browser_max_uses=10,
        browser_max_age_seconds=3600,
    )
    await pool.start()

    async with pool.lease_context() as first:
        assert first.context is not None
    async with pool.lease_context() as second:
        assert second.context is not None

    await pool.close()

    assert len(factory.created) == 1
    assert factory.created[0].contexts[0].closed is True
    assert factory.created[0].contexts[1].closed is True
    assert factory.created[0].closed is True


@pytest.mark.anyio
async def test_pool_recycles_browser_after_max_uses() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
        browser_max_uses=1,
        browser_max_age_seconds=3600,
    )
    await pool.start()

    async with pool.lease_context():
        pass
    async with pool.lease_context():
        pass

    await pool.close()

    assert len(factory.created) == 2
    assert factory.created[0].closed is True


@pytest.mark.anyio
async def test_context_closes_when_request_raises() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(browser_factory=factory, min_browsers=1, max_browsers=1)
    await pool.start()

    with pytest.raises(RuntimeError):
        async with pool.lease_context() as lease:
            context = lease.context
            raise RuntimeError("boom")

    await pool.close()

    assert context.closed is True


@pytest.mark.anyio
async def test_pool_times_out_when_all_context_capacity_is_busy() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
        acquire_timeout_seconds=0.01,
    )
    await pool.start()

    async with pool.lease_context():
        with pytest.raises(PoolAcquireTimeout):
            async with pool.lease_context():
                pass

    await pool.close()


@pytest.mark.anyio
async def test_context_close_failure_still_releases_pool_capacity() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
        acquire_timeout_seconds=0.1,
    )
    await pool.start()

    with pytest.raises(RuntimeError, match="context close failed"):
        async with pool.lease_context() as lease:
            lease.context.fail_close = True

    async with pool.lease_context() as lease:
        assert lease.context is not None

    await pool.close()


@pytest.mark.anyio
async def test_concurrent_browser_creation_does_not_exceed_pool_max() -> None:
    factory = DelayedFakeBrowserFactory(delay=0.01)
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=0,
        max_browsers=2,
        max_contexts_per_browser=1,
    )
    await pool.start()

    async def use_context() -> None:
        async with pool.lease_context():
            pass

    await asyncio.gather(*(use_context() for _ in range(6)))
    await pool.close()

    assert len(factory.created) <= 2


@pytest.mark.anyio
async def test_persistent_context_creation_failure_releases_pool_capacity() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
    )
    await pool.start()
    factory.created[0].fail_new_context = True

    with pytest.raises(RuntimeError, match="new context failed"):
        await pool.create_persistent_context()

    factory.created[0].fail_new_context = False
    async with pool.lease_context() as lease:
        assert lease.context is not None

    await pool.close()


@pytest.mark.anyio
async def test_new_context_browser_closed_error_discards_slot() -> None:
    class ClosedBrowser:
        def __init__(self) -> None:
            self.closed = False

        async def new_context(self, **_: object) -> FakeContext:
            raise RuntimeError("Browser has been closed")

        async def close(self) -> None:
            self.closed = True

    class RecoveringFactory(FakeBrowserFactory):
        async def __call__(self):  # type: ignore[no-untyped-def]
            if not self.created:
                browser = ClosedBrowser()
                self.created.append(browser)  # type: ignore[arg-type]
                return browser
            return await super().__call__()

    factory = RecoveringFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
    )
    await pool.start()

    with pytest.raises(RuntimeError, match="Browser has been closed"):
        async with pool.lease_context():
            pass

    async with pool.lease_context() as lease:
        assert lease.context is not None

    await pool.close()

    assert len(factory.created) == 2
    assert factory.created[0].closed is True


@pytest.mark.anyio
async def test_context_close_browser_closed_error_discards_slot_without_poisoning_pool() -> None:
    class ClosingDeadContext(FakeContext):
        async def close(self) -> None:
            self.closed = True
            raise RuntimeError("Target page, context or browser has been closed")

    class ClosingDeadBrowser:
        def __init__(self) -> None:
            self.contexts: list[FakeContext] = []
            self.closed = False

        async def new_context(self, **options: object) -> FakeContext:
            context = ClosingDeadContext(options=options)  # type: ignore[arg-type]
            self.contexts.append(context)
            return context

        async def close(self) -> None:
            self.closed = True

    class RecoveringFactory(FakeBrowserFactory):
        async def __call__(self):  # type: ignore[no-untyped-def]
            if not self.created:
                browser = ClosingDeadBrowser()
                self.created.append(browser)  # type: ignore[arg-type]
                return browser
            return await super().__call__()

    factory = RecoveringFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
    )
    await pool.start()

    async with pool.lease_context() as lease:
        assert lease.context is not None

    async with pool.lease_context() as lease:
        assert lease.context is not None

    await pool.close()

    assert len(factory.created) == 2
    assert factory.created[0].closed is True


@pytest.mark.anyio
async def test_persistent_capacity_is_bounded_by_reservation() -> None:
    factory = FakeBrowserFactory()
    # capacity = 1 browser * 2 contexts = 2; reserve 1 -> at most 1 persistent context.
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=2,
        reserved_transient_contexts=1,
        acquire_timeout_seconds=0.05,
    )
    await pool.start()
    assert pool.max_persistent_contexts == 1

    first = await pool.create_persistent_context()

    # The second persistent request is rejected immediately, not after the acquire
    # timeout, because it would eat the slot reserved for transient work.
    with pytest.raises(PersistentCapacityError):
        await pool.create_persistent_context()

    # The reserved slot still serves a stateless lease while the session is alive.
    async with pool.lease_context() as lease:
        assert lease.context is not None

    await first.close()
    # Once the persistent context is released, capacity frees up again.
    second = await pool.create_persistent_context()
    await second.close()

    await pool.close()


@pytest.mark.anyio
async def test_discarded_slot_with_active_sibling_closes_on_last_release() -> None:
    class DeadClosingContext(FakeContext):
        async def close(self) -> None:
            self.closed = True
            raise RuntimeError("Target page, context or browser has been closed")

    class MixedBrowser(FakeBrowser):
        async def new_context(self, **options: object) -> FakeContext:
            self.context_options.append(options)
            context: FakeContext = (
                DeadClosingContext(options=options)
                if not self.contexts
                else FakeContext(self, options)
            )
            self.contexts.append(context)
            return context

    class MixedFactory(FakeBrowserFactory):
        async def __call__(self) -> MixedBrowser:
            browser = MixedBrowser()
            self.created.append(browser)
            return browser

    factory = MixedFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=2,
    )
    await pool.start()

    lease_a = pool.lease_context()
    await lease_a.__aenter__()  # first (dead-closing) context, active
    lease_b = pool.lease_context()
    await lease_b.__aenter__()  # sibling context on the same browser, active

    # Closing A raises a disconnect error -> slot is discarded, but B is still
    # active so the browser must not close yet.
    await lease_a.__aexit__(None, None, None)
    assert factory.created[0].closed is False

    # When the last sibling releases, the discarded slot's browser is finally closed
    # instead of being leaked.
    await lease_b.__aexit__(None, None, None)
    assert factory.created[0].closed is True

    await pool.close()


@pytest.mark.anyio
async def test_start_closes_created_browsers_when_a_sibling_factory_fails() -> None:
    class FlakyFactory(FakeBrowserFactory):
        async def __call__(self) -> FakeBrowser:
            if not self.created:
                return await super().__call__()
            raise RuntimeError("factory boom")

    factory = FlakyFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=2,
        max_browsers=2,
    )

    with pytest.raises(RuntimeError, match="factory boom"):
        await pool.start()

    # The browser that did come up must be closed, not leaked.
    assert len(factory.created) == 1
    assert factory.created[0].closed is True


@pytest.mark.anyio
async def test_persistent_context_cancellation_releases_slot_and_reservation() -> None:
    class CancelFirstBrowser(FakeBrowser):
        def __init__(self, *, cancel_new_context: bool) -> None:
            super().__init__()
            self.cancel_new_context = cancel_new_context

        async def new_context(self, **options: object) -> FakeContext:
            if self.cancel_new_context:
                self.cancel_new_context = False
                raise asyncio.CancelledError()
            return await super().new_context(**options)

    class CancelFirstFactory(FakeBrowserFactory):
        async def __call__(self) -> CancelFirstBrowser:
            browser = CancelFirstBrowser(cancel_new_context=not self.created)
            self.created.append(browser)
            return browser

    factory = CancelFirstFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=2,
        reserved_transient_contexts=1,
        acquire_timeout_seconds=0.05,
    )
    await pool.start()

    # A cancellation while opening the context must not pin the slot or the
    # persistent reservation.
    with pytest.raises(asyncio.CancelledError):
        await pool.create_persistent_context()

    # Reservation freed: a fresh persistent context can still be created.
    lease = await pool.create_persistent_context()
    assert lease.context is not None
    # Slot freed: a stateless lease still works alongside it.
    async with pool.lease_context() as transient:
        assert transient.context is not None

    await lease.close()
    await pool.close()


@pytest.mark.anyio
async def test_transient_context_cancellation_releases_slot() -> None:
    class CancelFirstBrowser(FakeBrowser):
        def __init__(self, *, cancel_new_context: bool) -> None:
            super().__init__()
            self.cancel_new_context = cancel_new_context

        async def new_context(self, **options: object) -> FakeContext:
            if self.cancel_new_context:
                self.cancel_new_context = False
                raise asyncio.CancelledError()
            return await super().new_context(**options)

    class CancelFirstFactory(FakeBrowserFactory):
        async def __call__(self) -> CancelFirstBrowser:
            browser = CancelFirstBrowser(cancel_new_context=not self.created)
            self.created.append(browser)
            return browser

    factory = CancelFirstFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
        acquire_timeout_seconds=0.05,
    )
    await pool.start()

    with pytest.raises(asyncio.CancelledError):
        async with pool.lease_context():
            pass

    async with pool.lease_context() as lease:
        assert lease.context is not None

    assert len(factory.created) == 2
    assert factory.created[0].closed is True
    await pool.close()


@pytest.mark.anyio
async def test_repeated_cancellation_cannot_orphan_transient_accounting() -> None:
    body_started = asyncio.Event()
    pool = BrowserPool(
        browser_factory=FakeBrowserFactory(),
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
    )
    await pool.start()

    async def use_context() -> None:
        async with pool.lease_context():
            body_started.set()
            await asyncio.Event().wait()

    request = asyncio.create_task(use_context())
    await body_started.wait()
    async with pool._condition:
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await request
    for _ in range(20):
        if pool.snapshot().active_contexts == 0:
            break
        await asyncio.sleep(0)
    assert pool.snapshot().active_contexts == 0
    await pool.close()


@pytest.mark.anyio
async def test_cancelled_dynamic_browser_creation_releases_creation_reservation() -> None:
    class CancelFirstFactory(FakeBrowserFactory):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def __call__(self) -> FakeBrowser:
            self.calls += 1
            if self.calls == 1:
                raise asyncio.CancelledError()
            return await super().__call__()

    factory = CancelFirstFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=0,
        max_browsers=1,
        max_contexts_per_browser=1,
        acquire_timeout_seconds=0.05,
    )
    await pool.start()

    with pytest.raises(asyncio.CancelledError):
        async with pool.lease_context():
            pass

    async with pool.lease_context() as lease:
        assert lease.context is not None

    await pool.close()


@pytest.mark.anyio
async def test_lease_context_body_error_is_not_masked_by_close_error() -> None:
    class FailCloseContext(FakeContext):
        async def close(self) -> None:
            self.closed = True
            raise RuntimeError("close boom")

    class FailCloseBrowser(FakeBrowser):
        async def new_context(self, **options: object) -> FailCloseContext:
            context = FailCloseContext(self, options)
            self.contexts.append(context)
            return context

    class FailCloseFactory(FakeBrowserFactory):
        async def __call__(self) -> FailCloseBrowser:
            browser = FailCloseBrowser()
            self.created.append(browser)
            return browser

    pool = BrowserPool(browser_factory=FailCloseFactory(), min_browsers=1, max_browsers=1)
    await pool.start()

    # The consumer's exception is the real cause and must survive; the cleanup
    # close error must not replace it during unwinding.
    with pytest.raises(RuntimeError, match="body boom"):
        async with pool.lease_context():
            raise RuntimeError("body boom")

    await pool.close()


@pytest.mark.anyio
async def test_pool_shutdown_closes_remaining_browsers_after_one_failure() -> None:
    class Browser(FakeBrowser):
        def __init__(self, *, fail: bool) -> None:
            super().__init__()
            self.fail = fail

        async def close(self) -> None:
            self.closed = True
            if self.fail:
                raise RuntimeError("browser close failed")

    class Factory(FakeBrowserFactory):
        async def __call__(self) -> Browser:
            browser = Browser(fail=not self.created)
            self.created.append(browser)
            return browser

    factory = Factory()
    pool = BrowserPool(browser_factory=factory, min_browsers=2, max_browsers=2)
    await pool.start()

    await pool.close()

    assert len(factory.created) == 2
    assert all(browser.closed for browser in factory.created)


@pytest.mark.anyio
async def test_pool_snapshot_tracks_capacity_without_exposing_mutable_state() -> None:
    pool = BrowserPool(
        browser_factory=FakeBrowserFactory(),
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=2,
    )
    await pool.start()

    started = pool.snapshot()
    assert started.browser_slots == 1
    assert started.active_contexts == 0
    assert started.max_slots == 2

    async with pool.lease_context():
        leased = pool.snapshot()
        assert leased.active_contexts == 1
        assert leased.transient_contexts == 1

    assert pool.snapshot().active_contexts == 0
    await pool.close()
    assert pool.snapshot().browser_slots == 0


@pytest.mark.anyio
async def test_pool_snapshot_does_not_hide_transient_accounting_corruption() -> None:
    pool = BrowserPool(
        browser_factory=FakeBrowserFactory(),
        min_browsers=1,
        max_browsers=1,
    )
    await pool.start()
    pool._transient_contexts = 1

    with pytest.raises(RuntimeError, match="Transient-context accounting"):
        pool.snapshot()

    pool._transient_contexts = 0
    await pool.close()


@pytest.mark.anyio
async def test_idle_max_age_slot_is_reclaimed_during_acquire() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
        browser_max_age_seconds=60,
        acquire_timeout_seconds=0.05,
    )
    await pool.start()
    original = factory.created[0]
    pool._slots[0].created_at -= 61

    stale = pool.snapshot()
    assert stale.ready_browser_slots == 0
    assert stale.retiring_browser_slots == 1
    assert stale.idle_recyclable_slots == 1
    assert stale.usable_context_slots == 0

    async with pool.lease_context() as lease:
        assert lease.browser is factory.created[1]

    await pool.close()

    assert len(factory.created) == 2
    assert original.closed is True


@pytest.mark.anyio
async def test_aged_persistent_browser_keeps_transient_headroom_until_drain() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=2,
        browser_max_age_seconds=60,
        reserved_transient_contexts=1,
        acquire_timeout_seconds=0.05,
    )
    await pool.start()
    original = factory.created[0]
    persistent = await pool.create_persistent_context()
    pool._slots[0].created_at -= 61

    aged = pool.snapshot()
    assert aged.retiring_browser_slots == 1
    assert aged.usable_context_slots == 1

    transient = pool.lease_context()
    transient_lease = await transient.__aenter__()
    assert transient_lease.browser is original

    await persistent.close()
    assert original.closed is False

    await transient.__aexit__(None, None, None)
    assert original.closed is True

    async with pool.lease_context() as replacement:
        assert replacement.browser is not original

    await pool.close()


@pytest.mark.anyio
async def test_concurrent_acquires_replace_all_idle_aged_slots_once() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=2,
        max_browsers=2,
        max_contexts_per_browser=1,
        browser_max_age_seconds=60,
        acquire_timeout_seconds=0.2,
    )
    await pool.start()
    originals = list(factory.created)
    for slot in pool._slots:
        slot.created_at -= 61

    first_wave_ready = asyncio.Event()
    release_first_wave = asyncio.Event()
    entered = 0
    entered_lock = asyncio.Lock()

    async def use_context() -> None:
        nonlocal entered
        async with pool.lease_context():
            async with entered_lock:
                entered += 1
                is_first_wave = entered <= 2
                if entered == 2:
                    first_wave_ready.set()
            if is_first_wave:
                await release_first_wave.wait()

    tasks = [asyncio.create_task(use_context()) for _ in range(6)]
    await asyncio.wait_for(first_wave_ready.wait(), timeout=0.5)
    release_first_wave.set()
    await asyncio.gather(*tasks)
    await pool.close()

    assert len(factory.created) == 4
    assert all(browser.closed for browser in originals)


@pytest.mark.anyio
async def test_browser_factory_is_bounded_by_absolute_acquire_deadline() -> None:
    factory_started = asyncio.Event()
    allow_cancelled_factory_to_finish = asyncio.Event()
    created: list[FakeBrowser] = []
    calls = 0

    async def stubborn_factory() -> FakeBrowser:
        nonlocal calls
        calls += 1
        if calls > 1:
            browser = FakeBrowser()
            created.append(browser)
            return browser
        factory_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Simulate a third-party launch coroutine that performs slow unwind.
            await allow_cancelled_factory_to_finish.wait()
        browser = FakeBrowser()
        created.append(browser)
        return browser

    pool = BrowserPool(
        browser_factory=stubborn_factory,
        min_browsers=0,
        max_browsers=1,
        max_contexts_per_browser=1,
        acquire_timeout_seconds=0.02,
    )
    await pool.start()

    started = time.monotonic()
    with pytest.raises(PoolAcquireTimeout, match="creating browser"):
        async with pool.lease_context():
            pass
    elapsed = time.monotonic() - started

    assert factory_started.is_set()
    assert elapsed < 0.2
    assert pool.snapshot().creating_slots == 0

    # The cancellation-resistant launch is quarantined and cannot consume the
    # usable browser-creation limit forever.
    async with pool.lease_context() as recovered:
        assert recovered.context is not None

    allow_cancelled_factory_to_finish.set()
    for _ in range(20):
        if not pool._create_watchers:
            break
        await asyncio.sleep(0)
    assert not pool._create_watchers
    await pool.close()
    assert all(browser.closed for browser in created)


@pytest.mark.anyio
async def test_repeated_cancellation_resistant_launches_open_bounded_circuit() -> None:
    finish_factories = asyncio.Event()
    created: list[FakeBrowser] = []
    calls = 0

    async def cancellation_resistant_factory() -> FakeBrowser:
        nonlocal calls
        calls += 1
        while not finish_factories.is_set():
            try:
                await finish_factories.wait()
            except asyncio.CancelledError:
                continue
        browser = FakeBrowser()
        created.append(browser)
        return browser

    pool = BrowserPool(
        browser_factory=cancellation_resistant_factory,
        min_browsers=0,
        max_browsers=1,
        max_contexts_per_browser=1,
        acquire_timeout_seconds=0.01,
    )
    await pool.start()

    for _ in range(3):
        with pytest.raises(PoolAcquireTimeout):
            async with pool.lease_context():
                pass

    assert calls == 2
    assert len(pool._create_watchers) == 2
    assert pool.snapshot().creating_slots == 0

    finish_factories.set()
    for _ in range(40):
        if not pool._create_watchers:
            break
        await asyncio.sleep(0)
    assert not pool._create_watchers
    await pool.close()
    assert all(browser.closed for browser in created)


@pytest.mark.anyio
async def test_cancellation_after_factory_success_registers_idle_slot_without_leak() -> None:
    factory_started = asyncio.Event()
    factory_can_finish = asyncio.Event()
    factory = FakeBrowserFactory()

    async def delayed_factory() -> FakeBrowser:
        factory_started.set()
        await factory_can_finish.wait()
        return await factory()

    pool = BrowserPool(
        browser_factory=delayed_factory,
        min_browsers=0,
        max_browsers=1,
        acquire_timeout_seconds=0.5,
    )
    await pool.start()

    async def acquire() -> None:
        async with pool.lease_context():
            pass

    acquire_task = asyncio.create_task(acquire())
    await factory_started.wait()

    # Hold the condition after the factory completes so registration cannot finish,
    # then cancel the requester at the exact ownership hand-off boundary.
    async with pool._condition:
        factory_can_finish.set()
        creation = next(iter(pool._create_tasks))
        while not creation.done():
            await asyncio.sleep(0)
        while not any(
            task.get_name() == "camouflare-pool-register-browser" for task in asyncio.all_tasks()
        ):
            await asyncio.sleep(0)
        acquire_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await acquire_task

    for _ in range(20):
        if pool.snapshot().creating_slots == 0:
            break
        await asyncio.sleep(0)

    snapshot = pool.snapshot()
    assert snapshot.browser_slots == 1
    assert snapshot.active_contexts == 0
    assert snapshot.creating_slots == 0

    async with pool.lease_context() as recovered:
        assert recovered.browser is factory.created[0]
    await pool.close()


@pytest.mark.anyio
async def test_pool_close_waits_for_active_lease_before_closing_browser() -> None:
    factory = FakeBrowserFactory()
    pool = BrowserPool(browser_factory=factory, min_browsers=1, max_browsers=1)
    await pool.start()

    lease = pool.lease_context()
    await lease.__aenter__()
    close_task = asyncio.create_task(pool.close())
    await asyncio.sleep(0)

    assert close_task.done() is False
    assert factory.created[0].closed is False
    with pytest.raises(RuntimeError, match="closed"):
        async with pool.lease_context():
            pass

    await lease.__aexit__(None, None, None)
    await asyncio.wait_for(close_task, timeout=0.5)
    assert factory.created[0].closed is True


@pytest.mark.anyio
async def test_persistent_close_error_releases_accounting_exactly_once_on_retry() -> None:
    class FailOnceContext(FakeContext):
        def __init__(self, browser: FakeBrowser, options: dict[str, object]) -> None:
            super().__init__(browser, options)
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            self.closed = True
            if self.close_calls == 1:
                raise RuntimeError("persistent close failed")

    class FailOnceBrowser(FakeBrowser):
        async def new_context(self, **options: object) -> FailOnceContext:
            context = FailOnceContext(self, options)
            self.contexts.append(context)
            return context

    class Factory(FakeBrowserFactory):
        async def __call__(self) -> FailOnceBrowser:
            browser = FailOnceBrowser()
            self.created.append(browser)
            return browser

    pool = BrowserPool(
        browser_factory=Factory(),
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
    )
    await pool.start()
    persistent = await pool.create_persistent_context()

    with pytest.raises(RuntimeError, match="persistent close failed"):
        await persistent.close()

    failed = pool.snapshot()
    assert failed.active_contexts == 0
    assert failed.persistent_contexts == 0

    await persistent.close()
    retried = pool.snapshot()
    assert retried.active_contexts == 0
    assert retried.persistent_contexts == 0
    assert persistent.context.close_calls == 1
    await pool.close()


@pytest.mark.anyio
async def test_failed_persistent_acquire_releases_reservation_while_browser_close_hangs() -> None:
    browser_close_started = asyncio.Event()
    finish_browser_close = asyncio.Event()

    class DisconnectedBrowser(FakeBrowser):
        async def new_context(self, **options: object) -> FakeContext:
            raise RuntimeError("Browser has been closed")

        async def close(self) -> None:
            browser_close_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish_browser_close.wait()
            self.closed = True

    class Factory(FakeBrowserFactory):
        async def __call__(self) -> DisconnectedBrowser:
            browser = DisconnectedBrowser()
            self.created.append(browser)
            return browser

    factory = Factory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        cleanup_timeout_seconds=0.01,
    )
    await pool.start()

    acquire = asyncio.create_task(pool.create_persistent_context())
    await browser_close_started.wait()
    with pytest.raises(RuntimeError, match="Browser has been closed"):
        await acquire

    snapshot = pool.snapshot()
    assert snapshot.active_contexts == 0
    assert snapshot.persistent_contexts == 0

    finish_browser_close.set()
    for _ in range(20):
        if factory.created[0].closed:
            break
        await asyncio.sleep(0)
    await pool.close()


@pytest.mark.anyio
async def test_repeated_cancellation_cannot_orphan_persistent_accounting() -> None:
    new_context_started = asyncio.Event()
    browser_close_started = asyncio.Event()
    finish_browser_close = asyncio.Event()

    class CancellationResistantBrowser(FakeBrowser):
        async def new_context(self, **options: object) -> FakeContext:
            new_context_started.set()
            await asyncio.Event().wait()
            return FakeContext(self, options)

        async def close(self) -> None:
            browser_close_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish_browser_close.wait()
            self.closed = True

    class Factory(FakeBrowserFactory):
        async def __call__(self) -> CancellationResistantBrowser:
            browser = CancellationResistantBrowser()
            self.created.append(browser)
            return browser

    pool = BrowserPool(
        browser_factory=Factory(),
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
        browser_max_uses=1,
        cleanup_timeout_seconds=0.01,
    )
    await pool.start()

    acquire = asyncio.create_task(pool.create_persistent_context())
    await new_context_started.wait()
    async with pool._condition:
        acquire.cancel()
        await asyncio.sleep(0)
        acquire.cancel()
        await asyncio.sleep(0)
        acquire.cancel()
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await acquire
    for _ in range(20):
        if pool.snapshot().persistent_contexts == 0:
            break
        await asyncio.sleep(0)
    assert pool.snapshot().active_contexts == 0
    assert pool.snapshot().persistent_contexts == 0

    await browser_close_started.wait()
    finish_browser_close.set()
    await pool.close()


@pytest.mark.anyio
async def test_repeated_cancellation_during_persistent_close_cannot_leak_accounting() -> None:
    context_close_started = asyncio.Event()

    class CancellationContext(FakeContext):
        async def close(self) -> None:
            context_close_started.set()
            await asyncio.Event().wait()

    class CancellationBrowser(FakeBrowser):
        async def new_context(self, **options: object) -> CancellationContext:
            context = CancellationContext(self, options)
            self.contexts.append(context)
            return context

    class Factory(FakeBrowserFactory):
        async def __call__(self) -> CancellationBrowser:
            browser = CancellationBrowser()
            self.created.append(browser)
            return browser

    pool = BrowserPool(
        browser_factory=Factory(),
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
        cleanup_timeout_seconds=0.1,
    )
    await pool.start()
    persistent = await pool.create_persistent_context()

    close = asyncio.create_task(persistent.close())
    await context_close_started.wait()
    async with pool._condition:
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await close
    for _ in range(20):
        if pool.snapshot().persistent_contexts == 0:
            break
        await asyncio.sleep(0)
    assert pool.snapshot().active_contexts == 0
    assert pool.snapshot().persistent_contexts == 0
    await pool.close()


@pytest.mark.anyio
async def test_failed_start_can_be_retried() -> None:
    class FailFirstFactory(FakeBrowserFactory):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def __call__(self) -> FakeBrowser:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("first launch failed")
            return await super().__call__()

    factory = FailFirstFactory()
    pool = BrowserPool(browser_factory=factory, min_browsers=1, max_browsers=1)

    with pytest.raises(RuntimeError, match="first launch failed"):
        await pool.start()
    await pool.start()

    assert pool.snapshot().browser_slots == 1
    await pool.close()


@pytest.mark.anyio
async def test_start_launch_timeout_is_bounded_and_allows_healthy_retry() -> None:
    finish_stubborn_factory = asyncio.Event()
    calls = 0
    created: list[FakeBrowser] = []

    async def factory() -> FakeBrowser:
        nonlocal calls
        calls += 1
        if calls == 1:
            while not finish_stubborn_factory.is_set():
                try:
                    await finish_stubborn_factory.wait()
                except asyncio.CancelledError:
                    continue
        browser = FakeBrowser()
        created.append(browser)
        return browser

    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        acquire_timeout_seconds=0.01,
    )

    started = time.monotonic()
    with pytest.raises(PoolAcquireTimeout, match="starting browser pool"):
        await pool.start()
    assert time.monotonic() - started < 0.2
    assert pool._started is False
    assert len(pool._create_watchers) == 1

    await pool.start()
    assert pool.snapshot().ready_browser_slots == 1

    finish_stubborn_factory.set()
    for _ in range(40):
        if not pool._create_watchers:
            break
        await asyncio.sleep(0)
    await pool.close()
    assert all(browser.closed for browser in created)


@pytest.mark.anyio
async def test_cancelling_start_cannot_wait_on_cancellation_resistant_factory() -> None:
    factory_started = asyncio.Event()
    finish_factory = asyncio.Event()

    async def factory() -> FakeBrowser:
        factory_started.set()
        while not finish_factory.is_set():
            try:
                await finish_factory.wait()
            except asyncio.CancelledError:
                continue
        return FakeBrowser()

    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        acquire_timeout_seconds=1,
    )
    start = asyncio.create_task(pool.start())
    await factory_started.wait()
    start.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(start, timeout=0.1)
    assert pool._started is False
    assert len(pool._create_watchers) == 1

    finish_factory.set()
    for _ in range(40):
        if not pool._create_watchers:
            break
        await asyncio.sleep(0)
    await pool.close()


@pytest.mark.anyio
async def test_cancelling_start_during_registration_closes_created_browser() -> None:
    factory_started = asyncio.Event()
    release_factory = asyncio.Event()
    registration_started = asyncio.Event()
    release_registration = asyncio.Event()
    created: list[FakeBrowser] = []

    async def factory() -> FakeBrowser:
        factory_started.set()
        await release_factory.wait()
        browser = FakeBrowser()
        created.append(browser)
        return browser

    pool = BrowserPool(browser_factory=factory, min_browsers=1, max_browsers=1)
    original_register = pool._register_started_slots

    async def delayed_register(
        slots: list[object],
        registration: object,
    ) -> bool:
        registration_started.set()
        await release_registration.wait()
        return await original_register(slots, registration)  # type: ignore[arg-type]

    pool._register_started_slots = delayed_register  # type: ignore[method-assign]
    start_task = asyncio.create_task(pool.start())
    await factory_started.wait()
    release_factory.set()
    await registration_started.wait()
    start_task.cancel()
    await asyncio.sleep(0)
    release_registration.set()

    with pytest.raises(asyncio.CancelledError):
        await start_task
    for _ in range(20):
        if created and created[0].closed:
            break
        await asyncio.sleep(0)

    assert created[0].closed is True
    assert pool.snapshot().browser_slots == 0
    assert pool._started is False


@pytest.mark.anyio
async def test_close_racing_start_cleans_up_factory_task() -> None:
    factory_started = asyncio.Event()
    release_factory = asyncio.Event()

    async def factory() -> FakeBrowser:
        factory_started.set()
        await release_factory.wait()
        return FakeBrowser()

    pool = BrowserPool(browser_factory=factory, min_browsers=1, max_browsers=1)
    start_task = asyncio.create_task(pool.start())
    await factory_started.wait()
    close_task = asyncio.create_task(pool.close())

    with pytest.raises(asyncio.CancelledError):
        await start_task
    await asyncio.wait_for(close_task, timeout=0.5)

    snapshot = pool.snapshot()
    assert snapshot.browser_slots == 0
    assert snapshot.creating_slots == 0
    assert snapshot.closing_slots == 0


@pytest.mark.anyio
async def test_hanging_context_close_does_not_pin_pool_capacity() -> None:
    cleanup_can_finish = asyncio.Event()

    class HangingContext(FakeContext):
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await cleanup_can_finish.wait()
            self.closed = True

    class HangingBrowser(FakeBrowser):
        async def new_context(self, **options: object) -> HangingContext:
            context = HangingContext(self, options)
            self.contexts.append(context)
            return context

    class FirstHangsFactory(FakeBrowserFactory):
        async def __call__(self) -> FakeBrowser:
            browser: FakeBrowser = HangingBrowser() if not self.created else FakeBrowser()
            self.created.append(browser)
            return browser

    pool = BrowserPool(
        browser_factory=FirstHangsFactory(),
        min_browsers=1,
        max_browsers=1,
        cleanup_timeout_seconds=0.01,
        acquire_timeout_seconds=0.05,
    )
    await pool.start()

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="cleanup timed out"):
        async with pool.lease_context():
            pass
    assert time.monotonic() - started < 0.2
    assert pool.snapshot().active_contexts == 0

    async with pool.lease_context() as recovered:
        assert recovered.context is not None

    cleanup_can_finish.set()
    await pool.close()


@pytest.mark.anyio
async def test_hanging_persistent_context_releases_accounting_and_recovers_capacity() -> None:
    cleanup_can_finish = asyncio.Event()

    class HangingContext(FakeContext):
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await cleanup_can_finish.wait()
            self.closed = True

    class HangingBrowser(FakeBrowser):
        async def new_context(self, **options: object) -> HangingContext:
            context = HangingContext(self, options)
            self.contexts.append(context)
            return context

    class FirstHangsFactory(FakeBrowserFactory):
        async def __call__(self) -> FakeBrowser:
            browser: FakeBrowser = HangingBrowser() if not self.created else FakeBrowser()
            self.created.append(browser)
            return browser

    cleanup = CleanupSupervisor(timeout_seconds=0.01)
    factory = FirstHangsFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        max_contexts_per_browser=1,
        cleanup_timeout_seconds=0.01,
        cleanup_supervisor=cleanup,
        acquire_timeout_seconds=0.1,
    )
    await pool.start()
    persistent = await pool.create_persistent_context()

    with pytest.raises(TimeoutError, match="cleanup exceeded"):
        await persistent.close()

    snapshot = pool.snapshot()
    assert snapshot.active_contexts == 0
    assert snapshot.persistent_contexts == 0
    assert cleanup.snapshot().by_kind.get("context") == 1

    async with pool.lease_context() as recovered:
        assert recovered.context is not None
        assert recovered.browser is factory.created[1]

    cleanup_can_finish.set()
    for _ in range(20):
        if cleanup.snapshot().in_flight == 0:
            break
        await asyncio.sleep(0)
    assert cleanup.snapshot().in_flight == 0
    await pool.close()
    await cleanup.close()


@pytest.mark.anyio
async def test_hanging_browser_close_is_bounded_and_result_is_consumed() -> None:
    cleanup_can_finish = asyncio.Event()

    class HangingBrowser(FakeBrowser):
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await cleanup_can_finish.wait()
            self.closed = True

    class Factory(FakeBrowserFactory):
        async def __call__(self) -> HangingBrowser:
            browser = HangingBrowser()
            self.created.append(browser)
            return browser

    pool = BrowserPool(
        browser_factory=Factory(),
        min_browsers=1,
        max_browsers=1,
        cleanup_timeout_seconds=0.01,
    )
    await pool.start()

    started = time.monotonic()
    await pool.close()
    assert time.monotonic() - started < 0.2
    assert pool.snapshot().closing_slots == 1

    cleanup_can_finish.set()
    for _ in range(40):
        if not pool._cleanup_tasks and pool.snapshot().closing_slots == 0:
            break
        await asyncio.sleep(0)
    assert not pool._cleanup_tasks
    assert pool.snapshot().closing_slots == 0


@pytest.mark.anyio
async def test_physical_camoufox_close_stays_visible_after_cleanup_timeout() -> None:
    finish_close = asyncio.Event()

    class Manager:
        async def __aexit__(self, *_args: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish_close.wait()

    handle = CamoufoxBrowserHandle(Manager(), object())

    async def factory() -> CamoufoxBrowserHandle:
        return handle

    cleanup = CleanupSupervisor(timeout_seconds=0.01)
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        cleanup_timeout_seconds=0.01,
        cleanup_supervisor=cleanup,
    )
    await pool.start()
    await pool.close()

    assert cleanup.snapshot().by_kind == {"browser": 1}
    assert pool.snapshot().closing_slots == 1

    finish_close.set()
    for _ in range(40):
        if cleanup.snapshot().in_flight == 0 and pool.snapshot().closing_slots == 0:
            break
        await asyncio.sleep(0)
    assert cleanup.snapshot().in_flight == 0
    assert pool.snapshot().closing_slots == 0


@pytest.mark.anyio
async def test_request_context_and_browser_close_share_one_cleanup_deadline() -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    context_cancelled_at: list[float] = []
    browser_cancelled_at: list[float] = []

    class DeadlineContext(FakeContext):
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                context_cancelled_at.append(loop.time())
            self.closed = True

    class DeadlineBrowser(FakeBrowser):
        async def new_context(self, **options: object) -> DeadlineContext:
            context = DeadlineContext(self, options)
            self.contexts.append(context)
            return context

        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                browser_cancelled_at.append(loop.time())
            self.closed = True

    class Factory(FakeBrowserFactory):
        async def __call__(self) -> DeadlineBrowser:
            browser = DeadlineBrowser()
            self.created.append(browser)
            return browser

    cleanup = CleanupSupervisor(timeout_seconds=0.05)
    pool = BrowserPool(
        browser_factory=Factory(),
        min_browsers=1,
        max_browsers=1,
        cleanup_timeout_seconds=0.05,
        cleanup_supervisor=cleanup,
    )
    await pool.start()

    async def use_context() -> None:
        async with pool.lease_context():
            entered.set()
            await asyncio.Event().wait()

    request = asyncio.create_task(use_context())
    await entered.wait()
    cleanup.track(request, kind="request", timeout_seconds=0.05)
    group = cleanup.group_for(request)
    started = loop.time()
    request.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.wait_for(cleanup.wait_for_group(group), timeout=0.2)

    assert len(context_cancelled_at) == 1
    assert len(browser_cancelled_at) == 1
    assert context_cancelled_at[0] - started >= 0.03
    assert browser_cancelled_at[0] - context_cancelled_at[0] < 0.02
    assert browser_cancelled_at[0] - started < 0.09
    assert pool.snapshot().active_contexts == 0
    assert pool.snapshot().closing_slots == 0

    await pool.close()
    await cleanup.close()


@pytest.mark.anyio
async def test_cancelling_pool_close_does_not_cancel_owned_browser_close() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class BlockingBrowser(FakeBrowser):
        async def close(self) -> None:
            close_started.set()
            await allow_close.wait()
            self.closed = True

    class Factory(FakeBrowserFactory):
        async def __call__(self) -> BlockingBrowser:
            browser = BlockingBrowser()
            self.created.append(browser)
            return browser

    cleanup = CleanupSupervisor(timeout_seconds=1)
    pool = BrowserPool(
        browser_factory=Factory(),
        min_browsers=1,
        max_browsers=1,
        cleanup_timeout_seconds=1,
        cleanup_supervisor=cleanup,
    )
    await pool.start()

    close_task = asyncio.create_task(pool.close())
    cleanup.track(close_task, kind="session", timeout_seconds=1)
    group = cleanup.group_for(close_task)
    await close_started.wait()
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    group_waiter = asyncio.create_task(cleanup.wait_for_group(group))
    await asyncio.sleep(0)
    assert not group_waiter.done()
    assert pool.snapshot().closing_slots == 1

    allow_close.set()
    await asyncio.wait_for(group_waiter, timeout=0.2)
    for _ in range(20):
        if pool.snapshot().closing_slots == 0:
            break
        await asyncio.sleep(0)
    assert pool.snapshot().closing_slots == 0
    assert not cleanup._group_holds

    await pool.close()
    await cleanup.close()


@pytest.mark.anyio
async def test_prestart_browser_close_cancellation_releases_scope_hold() -> None:
    cleanup = CleanupSupervisor(timeout_seconds=1)
    factory = FakeBrowserFactory()
    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=1,
        max_browsers=1,
        cleanup_timeout_seconds=1,
        cleanup_supervisor=cleanup,
    )
    await pool.start()

    async def cancel_close_before_first_step() -> None:
        await pool.quiesce()
        close_task = next(iter(pool._close_tasks.values()))
        close_task.cancel()

    operation = asyncio.create_task(cancel_close_before_first_step())
    cleanup.track(operation, kind="session", timeout_seconds=1)
    group = cleanup.group_for(operation)
    await operation
    await asyncio.wait_for(cleanup.wait_for_group(group), timeout=0.1)
    for _ in range(20):
        if not pool._close_tasks:
            break
        await asyncio.sleep(0)

    assert not pool._close_tasks
    assert not cleanup._group_holds
    assert pool.snapshot().closing_slots == 1

    await pool.close()
    assert factory.created[0].closed is True
    assert pool.snapshot().closing_slots == 0
    await cleanup.close()
