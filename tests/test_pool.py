from __future__ import annotations

import asyncio

import pytest

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
        acquire_timeout_seconds=0.01,
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
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def new_context(self, **options: object) -> FakeContext:
            self.calls += 1
            if self.calls == 1:
                raise asyncio.CancelledError()
            return await super().new_context(**options)

    class CancelFirstFactory(FakeBrowserFactory):
        async def __call__(self) -> CancelFirstBrowser:
            browser = CancelFirstBrowser()
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
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def new_context(self, **options: object) -> FakeContext:
            self.calls += 1
            if self.calls == 1:
                raise asyncio.CancelledError()
            return await super().new_context(**options)

    class CancelFirstFactory(FakeBrowserFactory):
        async def __call__(self) -> CancelFirstBrowser:
            browser = CancelFirstBrowser()
            self.created.append(browser)
            return browser

    pool = BrowserPool(
        browser_factory=CancelFirstFactory(),
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
