from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from camouflare.metrics import (
    observe_pool_acquire,
    record_browser_event,
    record_browser_recycle,
    record_timeout,
    set_pool_snapshot,
)
from camouflare.protocols import (
    AsyncCleanup,
    BrowserContextLike,
    BrowserFactory,
    BrowserLike,
)

logger = logging.getLogger(__name__)

BROWSER_DISCONNECTED_MARKERS = (
    "browser has been closed",
    "target page, context or browser has been closed",
    "connection closed",
    "transport closed",
    "browser closed",
    "browser.context.newcontext",
)


class PoolAcquireTimeout(TimeoutError):
    """Raised when no browser context capacity becomes available in time."""


class PersistentCapacityError(RuntimeError):
    """Raised when no persistent-context capacity is left for a new session.

    Persistent (session) contexts occupy a pool slot for their whole lifetime, so
    they are capped below the total pool capacity to keep headroom for stateless
    requests and the health probe. This is raised immediately instead of blocking
    until the acquire timeout, so callers get a clear answer rather than a hang.
    """


@dataclass
class BrowserSlot:
    browser: BrowserLike
    created_at: float = field(default_factory=time.monotonic)
    active_contexts: int = 0
    uses: int = 0
    closing: bool = False


@dataclass
class ContextLease:
    context: BrowserContextLike
    browser: BrowserLike


@dataclass
class PersistentContextLease:
    context: BrowserContextLike
    browser: BrowserLike
    _release: AsyncCleanup
    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        await self._release()
        self._closed = True


@dataclass(frozen=True)
class PoolSnapshot:
    """Immutable, low-cardinality view of browser-pool capacity."""

    browser_slots: int
    creating_slots: int
    closing_slots: int
    active_contexts: int
    transient_contexts: int
    persistent_contexts: int
    waiting_requests: int
    max_slots: int


class BrowserPool:
    def __init__(
        self,
        *,
        browser_factory: BrowserFactory,
        min_browsers: int = 1,
        max_browsers: int = 2,
        max_contexts_per_browser: int = 1,
        browser_max_uses: int = 200,
        browser_max_age_seconds: int = 7200,
        acquire_timeout_seconds: float = 30,
        reserved_transient_contexts: int = 0,
    ) -> None:
        self._browser_factory = browser_factory
        self._min_browsers = min_browsers
        self._max_browsers = max_browsers
        self._max_contexts_per_browser = max_contexts_per_browser
        self._browser_max_uses = browser_max_uses
        self._browser_max_age_seconds = browser_max_age_seconds
        self._acquire_timeout_seconds = acquire_timeout_seconds
        total_capacity = max_browsers * max_contexts_per_browser
        self._max_persistent_contexts = max(0, total_capacity - reserved_transient_contexts)
        self._slots: list[BrowserSlot] = []
        self._creating_slots = 0
        self._closing_slots = 0
        self._persistent_contexts = 0
        self._waiting_requests = 0
        self._condition = asyncio.Condition()
        self._started = False
        self._closed = False

    @property
    def max_persistent_contexts(self) -> int:
        return self._max_persistent_contexts

    def snapshot(self) -> PoolSnapshot:
        """Return a read-only operational snapshot without exposing pool internals."""

        return self._snapshot_unlocked()

    async def start(self) -> None:
        async with self._condition:
            if self._started:
                return
            self._started = True
        results = await asyncio.gather(
            *(self._create_slot() for _ in range(self._min_browsers)),
            return_exceptions=True,
        )
        slots = [result for result in results if isinstance(result, BrowserSlot)]
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            # A sibling factory failed; close the browsers that did come up so they
            # are not leaked, then surface the first failure.
            await asyncio.gather(
                *(self._close_slot(slot) for slot in slots),
                return_exceptions=True,
            )
            raise errors[0]
        async with self._condition:
            self._slots.extend(slots)
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            slots = list(self._slots)
            self._slots.clear()
            self._closing_slots += len(slots)
            for _ in slots:
                record_browser_recycle("shutdown")
            self._publish_metrics_unlocked()
            self._condition.notify_all()
        results = await asyncio.gather(
            *(self._close_slot(slot) for slot in slots),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.error("Failed to close browser during shutdown: %s", result)
        async with self._condition:
            self._closing_slots = max(0, self._closing_slots - len(slots))
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    @asynccontextmanager
    async def lease_context(self, **context_options: Any) -> AsyncIterator[ContextLease]:
        slot = await self._observed_acquire_slot("transient")
        context = None
        body_failed = False
        try:
            context = await slot.browser.new_context(**context_options)
            yield ContextLease(context=context, browser=slot.browser)
        except BaseException as exc:
            body_failed = True
            if context is None:
                await self._release_slot(slot, discard=_is_browser_disconnected_error(exc))
            raise
        finally:
            close_error: BaseException | None = None
            discard = False
            disconnected = False
            if context is not None:
                try:
                    await context.close()
                except BaseException as exc:
                    close_error = exc
                disconnected = close_error is not None and _is_browser_disconnected_error(
                    close_error
                )
                discard = close_error is not None
                await self._release_slot(
                    slot,
                    discard=discard,
                    discard_reason="disconnected" if disconnected else "error",
                )
            # Only surface a cleanup error when the leased body itself succeeded;
            # otherwise the consumer's exception is the real cause and re-raising
            # close_error here would mask it during unwinding.
            if close_error is not None and not disconnected:
                if body_failed:
                    logger.warning(
                        "Ignoring context close error while unwinding a failed lease: %s",
                        close_error,
                    )
                else:
                    raise close_error

    async def create_persistent_context(self, **context_options: Any) -> PersistentContextLease:
        async with self._condition:
            if self._closed:
                observe_pool_acquire(kind="persistent", result="rejected", duration_seconds=0)
                raise RuntimeError("Browser pool is closed")
            if self._persistent_contexts >= self._max_persistent_contexts:
                observe_pool_acquire(kind="persistent", result="rejected", duration_seconds=0)
                raise PersistentCapacityError(
                    "No persistent-context capacity is available for a new session "
                    f"(limit {self._max_persistent_contexts})."
                )
            self._persistent_contexts += 1
            self._publish_metrics_unlocked()
        slot = None
        try:
            slot = await self._observed_acquire_slot("persistent")
            context = await slot.browser.new_context(**context_options)
        except BaseException as exc:
            # BaseException (not just Exception) so a task cancellation while acquiring
            # or opening the context still frees the slot and the reservation instead
            # of pinning both for the pool's lifetime.
            if slot is not None:
                await self._release_slot(slot, discard=_is_browser_disconnected_error(exc))
            await self._release_persistent_reservation()
            raise

        released = False
        release_lock = asyncio.Lock()

        async def release() -> None:
            nonlocal released
            async with release_lock:
                if released:
                    return

                errors: list[BaseException] = []
                close_error: BaseException | None = None
                try:
                    await context.close()
                except BaseException as exc:
                    close_error = exc
                    errors.append(exc)

                disconnected = close_error is not None and _is_browser_disconnected_error(
                    close_error
                )
                discard = close_error is not None
                slot_released = False
                reservation_released = False
                try:
                    await self._release_slot(
                        slot,
                        discard=discard,
                        discard_reason="disconnected" if disconnected else "error",
                    )
                    slot_released = True
                except BaseException as exc:
                    errors.append(exc)
                try:
                    await self._release_persistent_reservation()
                    reservation_released = True
                except BaseException as exc:
                    errors.append(exc)

                released = slot_released and reservation_released
                for error in errors:
                    if not (error is close_error and disconnected):
                        raise error

        return PersistentContextLease(context=context, browser=slot.browser, _release=release)

    async def _release_persistent_reservation(self) -> None:
        async with self._condition:
            self._persistent_contexts = max(0, self._persistent_contexts - 1)
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    async def _observed_acquire_slot(self, kind: str) -> BrowserSlot:
        started = time.monotonic()
        result = "error"
        try:
            slot = await self._acquire_slot()
        except PoolAcquireTimeout:
            result = "timeout"
            record_timeout("pool_acquire")
            raise
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        else:
            result = "success"
            return slot
        finally:
            observe_pool_acquire(
                kind=kind,
                result=result,
                duration_seconds=time.monotonic() - started,
            )

    async def _acquire_slot(self) -> BrowserSlot:
        deadline = time.monotonic() + self._acquire_timeout_seconds
        while True:
            should_create = False
            async with self._condition:
                if self._closed:
                    raise RuntimeError("Browser pool is closed")
                for slot in self._slots:
                    if self._slot_available(slot):
                        slot.active_contexts += 1
                        slot.uses += 1
                        self._publish_metrics_unlocked()
                        return slot
                if len(self._slots) + self._creating_slots < self._max_browsers:
                    self._creating_slots += 1
                    self._publish_metrics_unlocked()
                    should_create = True
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "Timed out waiting for browser context capacity.",
                            extra={"pool": self._capacity_snapshot_unlocked()},
                        )
                        raise PoolAcquireTimeout("Timed out waiting for browser context capacity.")
                    self._waiting_requests += 1
                    self._publish_metrics_unlocked()
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                    except TimeoutError as exc:
                        logger.warning(
                            "Timed out waiting for browser context capacity.",
                            extra={"pool": self._capacity_snapshot_unlocked()},
                        )
                        raise PoolAcquireTimeout(
                            "Timed out waiting for browser context capacity."
                        ) from exc
                    finally:
                        self._waiting_requests = max(0, self._waiting_requests - 1)
                        self._publish_metrics_unlocked()
            if should_create:
                try:
                    slot = await self._create_slot()
                except BaseException:
                    async with self._condition:
                        self._creating_slots = max(0, self._creating_slots - 1)
                        self._publish_metrics_unlocked()
                        self._condition.notify_all()
                    raise
                async with self._condition:
                    self._creating_slots = max(0, self._creating_slots - 1)
                    if self._closed:
                        close_created_slot = True
                    else:
                        slot.active_contexts = 1
                        slot.uses = 1
                        self._slots.append(slot)
                        self._publish_metrics_unlocked()
                        self._condition.notify_all()
                        return slot
                if close_created_slot:
                    await self._close_slot(slot)
                    raise RuntimeError("Browser pool is closed")

    async def _release_slot(
        self,
        slot: BrowserSlot,
        *,
        discard: bool = False,
        discard_reason: str = "disconnected",
    ) -> None:
        should_close = False
        newly_closing = False
        async with self._condition:
            slot.active_contexts = max(0, slot.active_contexts - 1)
            if discard or (slot.active_contexts == 0 and self._should_recycle(slot)):
                newly_closing = not slot.closing
                slot.closing = True
                if slot in self._slots:
                    self._slots.remove(slot)
                if newly_closing:
                    self._closing_slots += 1
                    reason = discard_reason if discard else self._recycle_reason(slot)
                    record_browser_recycle(reason)
                    if discard and discard_reason == "disconnected":
                        record_browser_event("disconnected")
                    elif discard:
                        record_browser_event("error")
            # A slot marked closing (here or by an earlier discard while siblings
            # were still active) must be closed once its last context releases,
            # otherwise a removed-but-still-open browser leaks.
            should_close = slot.closing and slot.active_contexts == 0
            self._publish_metrics_unlocked()
            self._condition.notify_all()
        if should_close:
            try:
                await self._close_slot(slot)
            finally:
                async with self._condition:
                    self._closing_slots = max(0, self._closing_slots - 1)
                    self._publish_metrics_unlocked()
                    self._condition.notify_all()

    def _slot_available(self, slot: BrowserSlot) -> bool:
        return (
            not slot.closing
            and slot.active_contexts < self._max_contexts_per_browser
            and not self._should_recycle(slot)
        )

    def _snapshot_unlocked(self) -> PoolSnapshot:
        active_contexts = sum(slot.active_contexts for slot in self._slots)
        return PoolSnapshot(
            browser_slots=len(self._slots),
            creating_slots=self._creating_slots,
            closing_slots=self._closing_slots,
            active_contexts=active_contexts,
            transient_contexts=max(0, active_contexts - self._persistent_contexts),
            persistent_contexts=self._persistent_contexts,
            waiting_requests=self._waiting_requests,
            max_slots=self._max_browsers * self._max_contexts_per_browser,
        )

    def _capacity_snapshot_unlocked(self) -> dict[str, int]:
        snapshot = self._snapshot_unlocked()
        return {
            "max_slots": snapshot.max_slots,
            "active_contexts": snapshot.active_contexts,
            "persistent_contexts": snapshot.persistent_contexts,
            "creating_slots": snapshot.creating_slots,
            "browser_slots": snapshot.browser_slots,
            "closing_slots": snapshot.closing_slots,
            "waiting_requests": snapshot.waiting_requests,
        }

    def _publish_metrics_unlocked(self) -> None:
        snapshot = self._snapshot_unlocked()
        set_pool_snapshot(
            browser_slots=snapshot.browser_slots,
            creating_slots=snapshot.creating_slots,
            closing_slots=snapshot.closing_slots,
            transient_contexts=snapshot.transient_contexts,
            persistent_contexts=snapshot.persistent_contexts,
            waiting_requests=snapshot.waiting_requests,
        )

    def _should_recycle(self, slot: BrowserSlot) -> bool:
        return self._recycle_reason(slot) != "other"

    def _recycle_reason(self, slot: BrowserSlot) -> str:
        if slot.uses >= self._browser_max_uses:
            return "max_uses"
        if time.monotonic() - slot.created_at >= self._browser_max_age_seconds:
            return "max_age"
        return "other"

    async def _create_slot(self) -> BrowserSlot:
        try:
            browser = await self._browser_factory()
        except BaseException:
            record_browser_event("error")
            raise
        record_browser_event("created")
        return BrowserSlot(browser=browser)

    async def _close_slot(self, slot: BrowserSlot) -> None:
        close = getattr(slot.browser, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:
                if _is_browser_disconnected_error(exc):
                    record_browser_event("disconnected")
                    return
                record_browser_event("error")
                raise


def _is_browser_disconnected_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in BROWSER_DISCONNECTED_MARKERS)
