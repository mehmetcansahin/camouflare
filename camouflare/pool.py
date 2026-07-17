from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from camouflare.cleanup import CleanupScope, CleanupScopeHold, CleanupSupervisor
from camouflare.metrics import (
    observe_pool_acquire,
    record_browser_event,
    record_browser_recycle,
    record_pool_acquire_timeout,
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


@dataclass(eq=False)
class BrowserSlot:
    browser: BrowserLike
    created_at: float = field(default_factory=time.monotonic)
    active_contexts: int = 0
    uses: int = 0
    state: Literal["ready", "retiring", "closing"] = "ready"
    retire_reason: str | None = None
    recycle_recorded: bool = False

    @property
    def closing(self) -> bool:
        """Backward-compatible view used by older pool integrations."""

        return self.state == "closing"


@dataclass
class _SlotReservation:
    slot: BrowserSlot
    kind: Literal["transient", "persistent"]
    released: bool = False


@dataclass
class _PersistentReservation:
    released: bool = False


@dataclass
class _StartupRegistration:
    cancelled: bool = False


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
    ready_browser_slots: int
    retiring_browser_slots: int
    usable_context_slots: int
    idle_recyclable_slots: int
    max_browsers: int
    max_contexts_per_browser: int


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
        cleanup_timeout_seconds: float = 10,
        cleanup_supervisor: CleanupSupervisor | None = None,
    ) -> None:
        self._browser_factory = browser_factory
        self._min_browsers = min_browsers
        self._max_browsers = max_browsers
        self._max_contexts_per_browser = max_contexts_per_browser
        self._browser_max_uses = browser_max_uses
        self._browser_max_age_seconds = browser_max_age_seconds
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._cleanup_supervisor = cleanup_supervisor
        # A single abandoned launch generation may be replaced so one broken
        # third-party launch cannot pin capacity forever. A second full generation
        # opens the circuit and bounds cancellation-resistant launch tasks.
        self._max_abandoned_creations = max(1, max_browsers * 2)
        total_capacity = max_browsers * max_contexts_per_browser
        self._max_persistent_contexts = max(0, total_capacity - reserved_transient_contexts)
        self._slots: list[BrowserSlot] = []
        self._creating_slots = 0
        self._create_tasks: set[asyncio.Task[BrowserSlot]] = set()
        self._create_watchers: dict[asyncio.Task[BrowserSlot], asyncio.Task[None]] = {}
        self._close_tasks: dict[BrowserSlot, asyncio.Task[None]] = {}
        self._failed_close_slots: set[BrowserSlot] = set()
        self._physically_closed_slots: set[BrowserSlot] = set()
        self._watched_physical_close_tasks: set[asyncio.Future[Any]] = set()
        self._accounting_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_tasks: set[asyncio.Future[Any]] = set()
        self._transient_contexts = 0
        self._persistent_contexts = 0
        self._waiting_requests = 0
        self._condition = asyncio.Condition()
        self._close_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    @property
    def max_persistent_contexts(self) -> int:
        return self._max_persistent_contexts

    def snapshot(self) -> PoolSnapshot:
        """Return a read-only operational snapshot without exposing pool internals."""

        snapshot = self._snapshot_unlocked()
        self._publish_snapshot_metrics(snapshot)
        return snapshot

    async def start(self) -> None:
        async with self._condition:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("Browser pool is closed")
            if len(self._create_watchers) + self._min_browsers > self._max_abandoned_creations:
                raise PoolAcquireTimeout(
                    "Browser startup launch quarantine is at its bounded limit."
                )
            self._started = True
            tasks = [self._start_create_task_unlocked() for _ in range(self._min_browsers)]
            self._publish_metrics_unlocked()

        if not tasks:
            return

        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=self._acquire_timeout_seconds,
                return_when=asyncio.FIRST_EXCEPTION,
            )
        except BaseException:
            async with self._condition:
                for task in tasks:
                    self._abandon_create_task_unlocked(task)
                if not self._closed:
                    self._started = False
                self._publish_metrics_unlocked()
                self._condition.notify_all()
            raise

        slots: list[BrowserSlot] = []
        errors: list[BaseException] = []
        for task in done:
            try:
                slots.append(task.result())
            except BaseException as exc:
                errors.append(exc)

        if pending or errors:
            async with self._condition:
                for task in done:
                    self._finish_create_task_unlocked(task)
                for task in pending:
                    self._abandon_create_task_unlocked(task)
                close_tasks = [
                    self._schedule_close_unlocked(slot, reason="error") for slot in slots
                ]
                if not self._closed:
                    self._started = False
                if pending:
                    self._log_acquire_timeout_unlocked("browser_launch")
                self._publish_metrics_unlocked()
                self._condition.notify_all()
            await asyncio.gather(
                *(asyncio.shield(task) for task in close_tasks if task is not None),
                return_exceptions=True,
            )
            if errors:
                raise errors[0]
            raise PoolAcquireTimeout("Timed out while starting browser pool capacity.")

        async with self._condition:
            for task in tasks:
                self._finish_create_task_unlocked(task)
            self._publish_metrics_unlocked()
            self._condition.notify_all()

        registration_state = _StartupRegistration()
        registration = asyncio.create_task(
            self._register_started_slots(slots, registration_state),
            name="camouflare-pool-register-started-browsers",
        )
        registration.add_done_callback(self._background_task_done)
        try:
            registered = await asyncio.shield(registration)
        except BaseException:
            registration_state.cancelled = True
            self._started = False
            recovery = asyncio.create_task(
                self._recover_cancelled_start(registration, slots),
                name="camouflare-pool-recover-cancelled-start",
            )
            recovery.add_done_callback(self._background_task_done)
            with suppress(BaseException):
                await asyncio.shield(recovery)
            raise
        if not registered:
            raise RuntimeError("Browser pool is closed")

    async def quiesce(self) -> None:
        """Reject new acquisitions while allowing existing leases to drain."""

        async with self._condition:
            self._closed = True
            for task in list(self._create_tasks):
                self._abandon_create_task_unlocked(task)
            for slot in list(self._slots):
                self._mark_retiring_unlocked(slot, "shutdown")
                if slot.active_contexts == 0:
                    self._schedule_close_unlocked(slot, reason="shutdown")
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    async def close(self) -> None:
        # Serializing close calls makes shutdown idempotent while still allowing
        # releases to acquire the pool condition and drain active leases.
        async with self._close_lock:
            async with self._condition:
                if self._closed and not (
                    self._slots
                    or self._create_tasks
                    or self._create_watchers
                    or self._close_tasks
                    or self._failed_close_slots
                    or self._accounting_tasks
                ):
                    return

                self._closed = True
                for task in list(self._create_tasks):
                    self._abandon_create_task_unlocked(task)
                for slot in list(self._failed_close_slots):
                    self._schedule_failed_close_retry_unlocked(slot)
                for slot in list(self._slots):
                    self._mark_retiring_unlocked(slot, "shutdown")
                    if slot.active_contexts == 0:
                        self._schedule_close_unlocked(slot, reason="shutdown")
                self._publish_metrics_unlocked()
                self._condition.notify_all()

                # Active browsers stay open until their final context is released.
                # This avoids closing a browser out from under an in-flight request.
                while (
                    self._slots
                    or self._create_tasks
                    or self._create_watchers
                    or self._accounting_tasks
                ):
                    await self._condition.wait()

            while True:
                async with self._condition:
                    close_tasks = list(self._close_tasks.values())
                if not close_tasks:
                    break
                await asyncio.gather(
                    *(asyncio.shield(task) for task in close_tasks),
                    return_exceptions=True,
                )
                await asyncio.sleep(0)

    @asynccontextmanager
    async def lease_context(self, **context_options: Any) -> AsyncIterator[ContextLease]:
        reservation = await self._observed_acquire_slot("transient")
        slot = reservation.slot
        context = None
        body_failed = False
        body_cancelled = False
        try:
            context = await slot.browser.new_context(**context_options)
            yield ContextLease(context=context, browser=slot.browser)
        except BaseException as exc:
            body_failed = True
            body_cancelled = isinstance(exc, asyncio.CancelledError)
            if context is None:
                release_finalizer = self._start_slot_accounting_finalizer(
                    reservation,
                    discard=body_cancelled or _is_browser_disconnected_error(exc),
                    discard_reason=("error" if body_cancelled else "disconnected"),
                )
                with suppress(BaseException):
                    await asyncio.shield(release_finalizer)
            raise
        finally:
            close_error: BaseException | None = None
            release_error: BaseException | None = None
            discard = False
            disconnected = False
            if context is not None:
                try:
                    await self._bounded_cleanup(
                        context.close(),
                        label="browser context",
                    )
                except BaseException as exc:
                    close_error = exc
                disconnected = close_error is not None and _is_browser_disconnected_error(
                    close_error
                )
                discard = close_error is not None or body_cancelled
                release_finalizer = self._start_slot_accounting_finalizer(
                    reservation,
                    discard=discard,
                    discard_reason="disconnected" if disconnected else "error",
                )
                try:
                    await asyncio.shield(release_finalizer)
                except BaseException as exc:
                    release_error = exc
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
            if release_error is not None and not body_failed:
                raise release_error

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
            persistent_reservation = _PersistentReservation()
            self._persistent_contexts += 1
            self._publish_metrics_unlocked()
        slot_reservation: _SlotReservation | None = None
        try:
            slot_reservation = await self._observed_acquire_slot("persistent")
            slot = slot_reservation.slot
            context = await slot.browser.new_context(**context_options)
        except BaseException as exc:
            # BaseException (not just Exception) so a task cancellation while acquiring
            # or opening the context still frees the slot and the reservation instead
            # of pinning both for the pool's lifetime.
            disconnected = _is_browser_disconnected_error(exc)
            finalizer = self._start_persistent_accounting_finalizer(
                slot_reservation,
                persistent_reservation,
                discard=disconnected or isinstance(exc, asyncio.CancelledError),
                discard_reason="disconnected" if disconnected else "error",
            )
            # The pool owns the accounting finalizer. Repeated cancellation of the
            # request may interrupt this shield, but cannot orphan either counter.
            with suppress(BaseException):
                await asyncio.shield(finalizer)
            raise

        released = False
        context_close_attempted = False
        release_lock = asyncio.Lock()
        accounting_finalizer: asyncio.Task[None] | None = None

        async def release() -> None:
            nonlocal accounting_finalizer, context_close_attempted, released
            async with release_lock:
                if released:
                    return

                errors: list[BaseException] = []
                close_error: BaseException | None = None
                if not context_close_attempted:
                    context_close_attempted = True
                    try:
                        await self._bounded_cleanup(
                            context.close(),
                            label="persistent browser context",
                        )
                    except BaseException as exc:
                        close_error = exc
                        errors.append(exc)

                disconnected = close_error is not None and _is_browser_disconnected_error(
                    close_error
                )
                discard = close_error is not None
                if accounting_finalizer is None or (
                    accounting_finalizer.done()
                    and not (slot_reservation.released and persistent_reservation.released)
                ):
                    accounting_finalizer = self._start_persistent_accounting_finalizer(
                        slot_reservation,
                        persistent_reservation,
                        discard=discard,
                        discard_reason=("disconnected" if disconnected else "error"),
                    )
                try:
                    await asyncio.shield(accounting_finalizer)
                except BaseException as exc:
                    errors.append(exc)

                released = slot_reservation.released and persistent_reservation.released
                for error in errors:
                    if not (error is close_error and disconnected):
                        raise error

        return PersistentContextLease(context=context, browser=slot.browser, _release=release)

    async def _release_persistent_reservation(
        self,
        reservation: _PersistentReservation,
    ) -> None:
        async with self._condition:
            if reservation.released:
                return
            if self._persistent_contexts <= 0:
                raise RuntimeError("Persistent-context accounting underflow")
            reservation.released = True
            self._persistent_contexts -= 1
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    def _start_persistent_accounting_finalizer(
        self,
        slot_reservation: _SlotReservation | None,
        persistent_reservation: _PersistentReservation,
        *,
        discard: bool,
        discard_reason: str,
    ) -> asyncio.Task[None]:
        cleanup_scope = self._current_cleanup_scope()
        task = asyncio.create_task(
            self._finalize_persistent_accounting(
                slot_reservation,
                persistent_reservation,
                discard=discard,
                discard_reason=discard_reason,
                cleanup_scope=cleanup_scope,
            ),
            name="camouflare-pool-finalize-persistent-accounting",
        )
        scope_hold = self._retain_cleanup_scope(cleanup_scope)
        self._accounting_tasks.add(task)
        task.add_done_callback(lambda completed: self._accounting_task_done(completed, scope_hold))
        return task

    def _start_slot_accounting_finalizer(
        self,
        slot_reservation: _SlotReservation,
        *,
        discard: bool,
        discard_reason: str,
    ) -> asyncio.Task[None]:
        cleanup_scope = self._current_cleanup_scope()
        task = asyncio.create_task(
            self._finalize_slot_accounting(
                slot_reservation,
                discard=discard,
                discard_reason=discard_reason,
                cleanup_scope=cleanup_scope,
            ),
            name="camouflare-pool-finalize-transient-accounting",
        )
        scope_hold = self._retain_cleanup_scope(cleanup_scope)
        self._accounting_tasks.add(task)
        task.add_done_callback(lambda completed: self._accounting_task_done(completed, scope_hold))
        return task

    async def _finalize_persistent_accounting(
        self,
        slot_reservation: _SlotReservation | None,
        persistent_reservation: _PersistentReservation,
        *,
        discard: bool,
        discard_reason: str,
        cleanup_scope: CleanupScope | None,
    ) -> None:
        try:
            if slot_reservation is not None:
                await self._release_slot(
                    slot_reservation,
                    discard=discard,
                    discard_reason=discard_reason,
                    wait_for_close=False,
                    cleanup_scope=cleanup_scope,
                )
        finally:
            await self._release_persistent_reservation(persistent_reservation)

    async def _finalize_slot_accounting(
        self,
        slot_reservation: _SlotReservation,
        *,
        discard: bool,
        discard_reason: str,
        cleanup_scope: CleanupScope | None,
    ) -> None:
        await self._release_slot(
            slot_reservation,
            discard=discard,
            discard_reason=discard_reason,
            wait_for_close=False,
            cleanup_scope=cleanup_scope,
        )

    def _accounting_task_done(
        self,
        task: asyncio.Task[None],
        scope_hold: CleanupScopeHold | None,
    ) -> None:
        if scope_hold is not None:
            scope_hold.release()
        self._accounting_tasks.discard(task)
        if not task.cancelled():
            try:
                exception = task.exception()
            except BaseException as exc:
                logger.error("Browser-pool accounting finalizer failed: %s", exc)
            else:
                if exception is not None:
                    logger.error(
                        "Browser-pool accounting finalizer failed: %s",
                        exception,
                    )
        notification = asyncio.create_task(
            self._notify_pool_waiters(),
            name="camouflare-pool-notify-accounting-finished",
        )
        notification.add_done_callback(self._background_task_done)

    async def _notify_pool_waiters(self) -> None:
        async with self._condition:
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    async def _observed_acquire_slot(
        self,
        kind: Literal["transient", "persistent"],
    ) -> _SlotReservation:
        started = time.monotonic()
        result = "error"
        try:
            slot = await self._acquire_slot(kind)
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

    async def _acquire_slot(
        self,
        kind: Literal["transient", "persistent"],
    ) -> _SlotReservation:
        deadline = time.monotonic() + self._acquire_timeout_seconds
        while True:
            create_task: asyncio.Task[BrowserSlot] | None = None
            async with self._condition:
                if self._closed:
                    raise RuntimeError("Browser pool is closed")
                if time.monotonic() >= deadline:
                    self._log_acquire_timeout_unlocked("deadline")
                    raise PoolAcquireTimeout("Timed out waiting for browser context capacity.")

                self._refresh_recycling_unlocked()

                # Healthy slots are always preferred. Crossing a recycle limit on
                # this acquisition retires the browser, but the active lease remains
                # valid and siblings may still use its spare capacity as a fallback.
                for slot in self._slots:
                    if self._healthy_slot_available(slot):
                        reservation = self._reserve_slot_unlocked(slot, kind=kind)
                        self._publish_metrics_unlocked()
                        return reservation

                if (
                    len(self._slots) + len(self._create_tasks) < self._max_browsers
                    and len(self._create_watchers) < self._max_abandoned_creations
                ):
                    create_task = self._start_create_task_unlocked()
                    self._publish_metrics_unlocked()
                else:
                    for slot in self._slots:
                        if self._soft_retiring_slot_available(slot):
                            reservation = self._reserve_slot_unlocked(slot, kind=kind)
                            self._publish_metrics_unlocked()
                            return reservation

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._log_acquire_timeout_unlocked("capacity")
                        raise PoolAcquireTimeout("Timed out waiting for browser context capacity.")
                    self._waiting_requests += 1
                    self._publish_metrics_unlocked()
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                    except TimeoutError as exc:
                        self._log_acquire_timeout_unlocked("capacity")
                        raise PoolAcquireTimeout(
                            "Timed out waiting for browser context capacity."
                        ) from exc
                    finally:
                        if self._waiting_requests <= 0:
                            raise RuntimeError("Pool waiter accounting underflow")
                        self._waiting_requests -= 1
                        self._publish_metrics_unlocked()

            if create_task is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    async with self._condition:
                        self._abandon_create_task_unlocked(create_task)
                        self._log_acquire_timeout_unlocked("browser_launch")
                        self._publish_metrics_unlocked()
                        self._condition.notify_all()
                    raise PoolAcquireTimeout("Timed out while creating browser context capacity.")
                try:
                    done, _ = await asyncio.wait({create_task}, timeout=remaining)
                except BaseException:
                    async with self._condition:
                        self._abandon_create_task_unlocked(create_task)
                        self._publish_metrics_unlocked()
                        self._condition.notify_all()
                    raise

                if not done:
                    async with self._condition:
                        self._abandon_create_task_unlocked(create_task)
                        self._log_acquire_timeout_unlocked("browser_launch")
                        self._publish_metrics_unlocked()
                        self._condition.notify_all()
                    raise PoolAcquireTimeout("Timed out while creating browser context capacity.")

                try:
                    slot = create_task.result()
                except BaseException:
                    cleanup = asyncio.create_task(
                        self._discard_finished_create_task(create_task),
                        name="camouflare-pool-discard-browser-creation",
                    )
                    cleanup.add_done_callback(self._background_task_done)
                    await asyncio.shield(cleanup)
                    raise

                registration = asyncio.create_task(
                    self._register_created_slot(create_task, slot),
                    name="camouflare-pool-register-browser",
                )
                registration.add_done_callback(self._background_task_done)
                registered = await asyncio.shield(registration)
                if not registered:
                    raise RuntimeError("Browser pool is closed")
                # Registration leaves the slot idle. Reserving it on the next loop
                # makes cancellation during condition acquisition leak-free.

    async def _release_slot(
        self,
        reservation: _SlotReservation,
        *,
        discard: bool = False,
        discard_reason: str = "disconnected",
        wait_for_close: bool = True,
        cleanup_scope: CleanupScope | None = None,
    ) -> None:
        close_task: asyncio.Task[None] | None = None
        async with self._condition:
            if reservation.released:
                return
            slot = reservation.slot
            if slot.active_contexts <= 0:
                raise RuntimeError("Browser-slot context accounting underflow")
            if reservation.kind == "transient" and self._transient_contexts <= 0:
                raise RuntimeError("Transient-context accounting underflow")
            reservation.released = True
            slot.active_contexts -= 1
            if reservation.kind == "transient":
                self._transient_contexts -= 1

            if discard:
                self._mark_retiring_unlocked(slot, discard_reason)
                if discard_reason == "disconnected":
                    record_browser_event("disconnected")
                else:
                    record_browser_event("error")
            elif self._should_recycle(slot):
                self._mark_retiring_unlocked(slot, self._recycle_reason(slot))

            if slot.state == "retiring" and slot.active_contexts == 0:
                close_task = self._schedule_close_unlocked(
                    slot,
                    reason=slot.retire_reason or "other",
                    cleanup_scope=cleanup_scope,
                )
            self._publish_metrics_unlocked()
            self._condition.notify_all()
        if close_task is not None and wait_for_close:
            # The pool owns the task, so cancellation of the releasing request does
            # not cancel browser retirement. Awaiting the shield keeps the previous
            # close-on-last-release guarantee for responsive browser implementations.
            await asyncio.shield(close_task)

    def _snapshot_unlocked(self) -> PoolSnapshot:
        active_contexts = sum(slot.active_contexts for slot in self._slots)
        if self._transient_contexts > active_contexts:
            raise RuntimeError("Transient-context accounting exceeds active contexts")
        ready_browser_slots = 0
        retiring_browser_slots = 0
        usable_context_slots = 0
        idle_recyclable_slots = 0
        for slot in self._slots:
            effectively_retiring = slot.state == "retiring" or self._should_recycle(slot)
            if effectively_retiring:
                retiring_browser_slots += 1
                if slot.active_contexts == 0:
                    idle_recyclable_slots += 1
                elif self._is_soft_retire_reason(slot.retire_reason or self._recycle_reason(slot)):
                    usable_context_slots += max(
                        0,
                        self._max_contexts_per_browser - slot.active_contexts,
                    )
            else:
                ready_browser_slots += 1
                usable_context_slots += max(
                    0,
                    self._max_contexts_per_browser - slot.active_contexts,
                )

        return PoolSnapshot(
            browser_slots=len(self._slots),
            creating_slots=len(self._create_tasks),
            closing_slots=len(self._close_tasks) + len(self._failed_close_slots),
            active_contexts=active_contexts,
            transient_contexts=self._transient_contexts,
            persistent_contexts=self._persistent_contexts,
            waiting_requests=self._waiting_requests,
            max_slots=self._max_browsers * self._max_contexts_per_browser,
            ready_browser_slots=ready_browser_slots,
            retiring_browser_slots=retiring_browser_slots,
            usable_context_slots=usable_context_slots,
            idle_recyclable_slots=idle_recyclable_slots,
            max_browsers=self._max_browsers,
            max_contexts_per_browser=self._max_contexts_per_browser,
        )

    def _capacity_snapshot_unlocked(self) -> dict[str, int]:
        snapshot = self._snapshot_unlocked()
        return {
            "max_slots": snapshot.max_slots,
            "max_browsers": snapshot.max_browsers,
            "max_contexts_per_browser": snapshot.max_contexts_per_browser,
            "active_contexts": snapshot.active_contexts,
            "persistent_contexts": snapshot.persistent_contexts,
            "creating_slots": snapshot.creating_slots,
            "browser_slots": snapshot.browser_slots,
            "ready_browser_slots": snapshot.ready_browser_slots,
            "retiring_browser_slots": snapshot.retiring_browser_slots,
            "closing_slots": snapshot.closing_slots,
            "waiting_requests": snapshot.waiting_requests,
            "usable_context_slots": snapshot.usable_context_slots,
            "idle_recyclable_slots": snapshot.idle_recyclable_slots,
        }

    def _publish_metrics_unlocked(self) -> None:
        snapshot = self._snapshot_unlocked()
        self._publish_snapshot_metrics(snapshot)

    @staticmethod
    def _publish_snapshot_metrics(snapshot: PoolSnapshot) -> None:
        set_pool_snapshot(
            browser_slots=snapshot.browser_slots,
            creating_slots=snapshot.creating_slots,
            closing_slots=snapshot.closing_slots,
            transient_contexts=snapshot.transient_contexts,
            persistent_contexts=snapshot.persistent_contexts,
            waiting_requests=snapshot.waiting_requests,
            ready_browser_slots=snapshot.ready_browser_slots,
            retiring_browser_slots=snapshot.retiring_browser_slots,
            usable_context_slots=snapshot.usable_context_slots,
            idle_recyclable_slots=snapshot.idle_recyclable_slots,
        )

    def _reserve_slot_unlocked(
        self,
        slot: BrowserSlot,
        *,
        kind: Literal["transient", "persistent"],
    ) -> _SlotReservation:
        if slot.state == "closing":
            raise RuntimeError("Cannot reserve a closing browser slot")
        if slot.active_contexts >= self._max_contexts_per_browser:
            raise RuntimeError("Cannot reserve a browser slot at capacity")
        slot.active_contexts += 1
        slot.uses += 1
        if kind == "transient":
            self._transient_contexts += 1
        if slot.state == "ready" and self._should_recycle(slot):
            self._mark_retiring_unlocked(slot, self._recycle_reason(slot))
        return _SlotReservation(slot=slot, kind=kind)

    def _healthy_slot_available(self, slot: BrowserSlot) -> bool:
        return (
            slot.state == "ready"
            and slot.active_contexts < self._max_contexts_per_browser
            and not self._should_recycle(slot)
        )

    def _soft_retiring_slot_available(self, slot: BrowserSlot) -> bool:
        return (
            slot.state == "retiring"
            and slot.active_contexts > 0
            and slot.active_contexts < self._max_contexts_per_browser
            and self._is_soft_retire_reason(slot.retire_reason)
        )

    @staticmethod
    def _is_soft_retire_reason(reason: str | None) -> bool:
        return reason in {"max_uses", "max_age"}

    def _refresh_recycling_unlocked(self) -> None:
        for slot in list(self._slots):
            if slot.state == "ready" and self._should_recycle(slot):
                self._mark_retiring_unlocked(slot, self._recycle_reason(slot))
            if slot.state == "retiring" and slot.active_contexts == 0:
                self._schedule_close_unlocked(
                    slot,
                    reason=slot.retire_reason or "other",
                )

    def _mark_retiring_unlocked(self, slot: BrowserSlot, reason: str) -> None:
        if slot.state == "closing":
            return
        slot.state = "retiring"
        # Shutdown/disconnect/error are hard retirement reasons and must override
        # a prior soft max-age/max-use retirement.
        if slot.retire_reason is None or not self._is_soft_retire_reason(reason):
            slot.retire_reason = reason
        if not slot.recycle_recorded:
            slot.recycle_recorded = True
            record_browser_recycle(reason)

    def _schedule_close_unlocked(
        self,
        slot: BrowserSlot,
        *,
        reason: str,
        cleanup_scope: CleanupScope | None = None,
    ) -> asyncio.Task[None] | None:
        if slot.state == "closing":
            return self._close_tasks.get(slot)
        if slot.active_contexts != 0:
            raise RuntimeError("Cannot close a browser slot with active contexts")
        self._mark_retiring_unlocked(slot, reason)
        if slot in self._slots:
            self._slots.remove(slot)
        slot.state = "closing"
        cleanup_scope = cleanup_scope or self._current_cleanup_scope()
        return self._start_close_task_unlocked(
            slot,
            cleanup_scope=cleanup_scope,
            name="camouflare-pool-close-browser",
        )

    def _start_close_task_unlocked(
        self,
        slot: BrowserSlot,
        *,
        cleanup_scope: CleanupScope | None,
        name: str,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._run_close_slot(slot, cleanup_scope),
            name=name,
        )
        scope_hold = self._retain_cleanup_scope(cleanup_scope)
        self._close_tasks[slot] = task
        task.add_done_callback(lambda completed: self._close_task_done(slot, completed, scope_hold))
        return task

    async def _run_close_slot(
        self,
        slot: BrowserSlot,
        cleanup_scope: CleanupScope | None,
    ) -> None:
        succeeded = False
        try:
            # Camoufox close handles are explicitly retryable when __aexit__ fails.
            # One immediate retry handles that transient path; a repeatedly failing
            # handle remains quarantined and referenced for a later close() retry.
            for attempt in range(2):
                try:
                    await self._close_slot(slot, cleanup_scope=cleanup_scope)
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    if attempt == 1:
                        raise
                else:
                    succeeded = True
                    break
        except BaseException as exc:
            logger.error("Failed to close browser (%s): %s", slot.retire_reason, exc)
        finally:
            async with self._condition:
                self._finish_close_slot_unlocked(slot, succeeded=succeeded)

    def _close_task_done(
        self,
        slot: BrowserSlot,
        task: asyncio.Task[None],
        scope_hold: CleanupScopeHold | None,
    ) -> None:
        self._background_task_done(task)
        if self._close_tasks.get(slot) is not task:
            if scope_hold is not None:
                scope_hold.release()
            return
        try:
            reconciliation = asyncio.create_task(
                self._reconcile_aborted_close_task(slot, task, scope_hold),
                name="camouflare-pool-reconcile-aborted-browser-close",
            )
        except BaseException:
            if scope_hold is not None:
                scope_hold.release()
            raise
        reconciliation.add_done_callback(self._background_task_done)

    async def _reconcile_aborted_close_task(
        self,
        slot: BrowserSlot,
        task: asyncio.Task[None],
        scope_hold: CleanupScopeHold | None,
    ) -> None:
        try:
            async with self._condition:
                if self._close_tasks.get(slot) is not task:
                    return
                self._finish_close_slot_unlocked(slot, succeeded=False)
        finally:
            if scope_hold is not None:
                scope_hold.release()

    def _finish_close_slot_unlocked(self, slot: BrowserSlot, *, succeeded: bool) -> None:
        self._close_tasks.pop(slot, None)
        physically_closed = (
            slot in self._physically_closed_slots
            or bool(getattr(slot.browser, "closed", False))
            or bool(getattr(slot.browser, "_closed", False))
        )
        if succeeded or physically_closed:
            self._failed_close_slots.discard(slot)
            self._physically_closed_slots.discard(slot)
        else:
            self._failed_close_slots.add(slot)
        self._publish_metrics_unlocked()
        self._condition.notify_all()

    def _schedule_failed_close_retry_unlocked(self, slot: BrowserSlot) -> asyncio.Task[None]:
        existing = self._close_tasks.get(slot)
        if existing is not None:
            return existing
        self._failed_close_slots.discard(slot)
        cleanup_scope = self._current_cleanup_scope()
        return self._start_close_task_unlocked(
            slot,
            cleanup_scope=cleanup_scope,
            name="camouflare-pool-retry-close-browser",
        )

    def _start_create_task_unlocked(self) -> asyncio.Task[BrowserSlot]:
        task = asyncio.create_task(
            self._create_slot(),
            name="camouflare-pool-create-browser",
        )
        self._create_tasks.add(task)
        self._creating_slots = len(self._create_tasks)
        return task

    def _finish_create_task_unlocked(self, task: asyncio.Task[BrowserSlot]) -> None:
        self._create_tasks.discard(task)
        self._creating_slots = len(self._create_tasks)

    async def _discard_finished_create_task(
        self,
        task: asyncio.Task[BrowserSlot],
    ) -> None:
        async with self._condition:
            self._finish_create_task_unlocked(task)
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    async def _register_created_slot(
        self,
        task: asyncio.Task[BrowserSlot],
        slot: BrowserSlot,
    ) -> bool:
        async with self._condition:
            self._finish_create_task_unlocked(task)
            if self._closed:
                self._schedule_close_unlocked(slot, reason="shutdown")
                registered = False
            else:
                self._slots.append(slot)
                registered = True
            self._publish_metrics_unlocked()
            self._condition.notify_all()
            return registered

    async def _register_started_slots(
        self,
        slots: list[BrowserSlot],
        registration: _StartupRegistration,
    ) -> bool:
        async with self._condition:
            if self._closed or registration.cancelled:
                for slot in slots:
                    self._schedule_close_unlocked(
                        slot,
                        reason="shutdown" if self._closed else "error",
                    )
                registered = False
            else:
                self._slots.extend(slots)
                registered = True
            self._publish_metrics_unlocked()
            self._condition.notify_all()
            return registered

    async def _recover_cancelled_start(
        self,
        registration_task: asyncio.Task[bool],
        slots: list[BrowserSlot],
    ) -> None:
        with suppress(BaseException):
            await registration_task
        async with self._condition:
            for slot in slots:
                if slot in self._slots:
                    self._mark_retiring_unlocked(slot, "error")
                    if slot.active_contexts == 0:
                        self._schedule_close_unlocked(slot, reason="error")
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    def _abandon_create_task_unlocked(self, task: asyncio.Task[BrowserSlot]) -> None:
        if task not in self._create_tasks or task in self._create_watchers:
            return
        # A cancelled factory that ignores cancellation is quarantined outside the
        # usable creation count. This lets a later acquire launch a replacement;
        # the watcher closes any browser the abandoned factory eventually returns.
        self._finish_create_task_unlocked(task)
        task.cancel()
        if self._cleanup_supervisor is not None:
            self._cleanup_supervisor.track(
                task,
                kind="browser",
                timeout_seconds=self._cleanup_timeout_seconds,
            )
        watcher = asyncio.create_task(
            self._finish_abandoned_creation(task),
            name="camouflare-pool-finish-browser-creation",
        )
        watcher.add_done_callback(self._background_task_done)
        self._create_watchers[task] = watcher

    async def _finish_abandoned_creation(
        self,
        task: asyncio.Task[BrowserSlot],
    ) -> None:
        slot: BrowserSlot | None = None
        with suppress(BaseException):
            slot = await task
        async with self._condition:
            self._create_watchers.pop(task, None)
            if slot is not None:
                self._schedule_close_unlocked(
                    slot,
                    reason="shutdown" if self._closed else "error",
                )
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    def _log_acquire_timeout_unlocked(self, reason: str) -> None:
        capacity = self._capacity_snapshot_unlocked()
        record_pool_acquire_timeout(reason)
        logger.warning(
            "Timed out waiting for browser context capacity. "
            "reason=%s usable=%s active=%s ready=%s recyclable=%s "
            "creating=%s closing=%s waiting=%s",
            reason,
            capacity["usable_context_slots"],
            capacity["active_contexts"],
            capacity["ready_browser_slots"],
            capacity["idle_recyclable_slots"],
            capacity["creating_slots"],
            capacity["closing_slots"],
            capacity["waiting_requests"],
            extra={"pool": capacity, "reason": reason},
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

    async def _close_slot(
        self,
        slot: BrowserSlot,
        *,
        cleanup_scope: CleanupScope | None = None,
    ) -> None:
        if bool(getattr(slot.browser, "closed", False)) or bool(
            getattr(slot.browser, "_closed", False)
        ):
            return
        close = getattr(slot.browser, "close", None)
        if close is not None:
            try:
                start_close = getattr(slot.browser, "start_close", None)
                close_awaitable = start_close() if callable(start_close) else close()
                close_task = asyncio.ensure_future(cast(Awaitable[Any], close_awaitable))
                if isinstance(close_task, asyncio.Task):
                    close_task.set_name("camouflare-pool-physical-browser-close")
                self._watch_physical_close(slot, close_task)
                await self._bounded_cleanup(
                    close_task,
                    label="browser",
                    cleanup_scope=cleanup_scope,
                )
            except Exception as exc:
                if _is_browser_disconnected_error(exc):
                    record_browser_event("disconnected")
                    return
                record_browser_event("error")
                raise

    def _watch_physical_close(
        self,
        slot: BrowserSlot,
        task: asyncio.Future[Any],
    ) -> None:
        if task in self._watched_physical_close_tasks:
            return
        self._watched_physical_close_tasks.add(task)
        task.add_done_callback(lambda finished: self._physical_close_finished(slot, finished))

    def _physical_close_finished(
        self,
        slot: BrowserSlot,
        task: asyncio.Future[Any],
    ) -> None:
        self._watched_physical_close_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except BaseException:
            return
        if exception is not None:
            return
        self._physically_closed_slots.add(slot)
        reconcile = asyncio.create_task(
            self._reconcile_late_physical_close(slot),
            name="camouflare-pool-reconcile-browser-close",
        )
        reconcile.add_done_callback(self._background_task_done)

    async def _reconcile_late_physical_close(self, slot: BrowserSlot) -> None:
        # Let _run_close_slot publish its terminal state first when the physical
        # task and its hard deadline complete in the same event-loop turn.
        await asyncio.sleep(0)
        async with self._condition:
            close_task = self._close_tasks.get(slot)
            if close_task is not None and not close_task.done():
                return
            self._failed_close_slots.discard(slot)
            self._physically_closed_slots.discard(slot)
            self._publish_metrics_unlocked()
            self._condition.notify_all()

    async def _bounded_cleanup(
        self,
        awaitable: Any,
        *,
        label: str,
        cleanup_scope: CleanupScope | None = None,
    ) -> Any:
        """Run physical cleanup without allowing it to pin logical capacity.

        ``asyncio.wait`` is deliberately used instead of ``wait_for`` so a cleanup
        coroutine that mishandles cancellation cannot extend the configured hard
        deadline. The detached task remains tracked until it actually terminates,
        and its result is always consumed.
        """

        if self._cleanup_supervisor is not None:
            return await self._cleanup_supervisor.run(
                awaitable,
                kind="context" if "context" in label else "browser",
                timeout_seconds=self._cleanup_timeout_seconds,
                scope=cleanup_scope,
            )

        task = asyncio.ensure_future(awaitable)
        if isinstance(task, asyncio.Task):
            task.set_name(f"camouflare-pool-cleanup-{label.replace(' ', '-')}")
        self._cleanup_tasks.add(task)
        try:
            done, _ = await asyncio.wait(
                {task},
                timeout=self._cleanup_timeout_seconds,
            )
            if not done:
                task.cancel()
                raise TimeoutError(
                    f"{label.capitalize()} cleanup timed out after "
                    f"{self._cleanup_timeout_seconds:g} seconds."
                )
            return task.result()
        except BaseException:
            if not task.done():
                task.cancel()
            raise
        finally:
            if task.done():
                self._cleanup_tasks.discard(task)
            else:
                task.add_done_callback(self._cleanup_task_done)

    def _current_cleanup_scope(self) -> CleanupScope | None:
        if self._cleanup_supervisor is None:
            return None
        return self._cleanup_supervisor.current_scope()

    def _retain_cleanup_scope(
        self,
        cleanup_scope: CleanupScope | None,
    ) -> CleanupScopeHold | None:
        if self._cleanup_supervisor is None or cleanup_scope is None:
            return None
        return self._cleanup_supervisor.retain_scope(cleanup_scope)

    def _cleanup_task_done(self, task: asyncio.Future[Any]) -> None:
        self._cleanup_tasks.discard(task)
        self._background_task_done(task)

    @staticmethod
    def _background_task_done(task: asyncio.Future[Any]) -> None:
        if task.cancelled():
            return
        with suppress(BaseException):
            task.exception()


def _is_browser_disconnected_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in BROWSER_DISCONNECTED_MARKERS)
