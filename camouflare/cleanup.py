from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from camouflare import metrics

T = TypeVar("T")

_CLEANUP_KINDS = frozenset(
    {"request", "readiness", "page", "context", "browser", "proxy", "captcha", "session"}
)


@dataclass(frozen=True)
class CleanupSnapshot:
    in_flight: int
    oldest_age_seconds: float | None
    by_kind: dict[str, int]


@dataclass(frozen=True)
class CleanupScope:
    """Absolute deadline and ownership group inherited by nested cleanup."""

    deadline: float
    group: object
    groups: frozenset[object] = frozenset()

    @property
    def ownership_groups(self) -> frozenset[object]:
        return self.groups or frozenset({self.group})


@dataclass(frozen=True)
class _TrackedCleanup:
    kind: str
    started_at: float
    deadline: float
    group: object
    groups: frozenset[object]


class CleanupScopeHold:
    """Keep a cleanup ownership group alive across an untracked hand-off."""

    def __init__(
        self,
        supervisor: CleanupSupervisor,
        groups: frozenset[object],
    ) -> None:
        self._supervisor = supervisor
        self._groups = groups
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        for group in self._groups:
            self._supervisor._release_scope_hold(group)


class CleanupSupervisor:
    """Own background cleanup work independently from caller cancellation.

    Tasks kept here always have their result consumed.  A timed-out HTTP request
    can therefore return immediately while its cancellation unwind continues in a
    strongly referenced task that remains visible to diagnostics and metrics.
    """

    def __init__(self, *, timeout_seconds: float = 10) -> None:
        if timeout_seconds <= 0:
            raise ValueError("cleanup timeout must be greater than zero")
        self.timeout_seconds = timeout_seconds
        self._tasks: dict[asyncio.Future[Any], _TrackedCleanup] = {}
        self._deadline_handles: dict[asyncio.Future[Any], asyncio.TimerHandle] = {}
        self._timed_out: set[asyncio.Future[Any]] = set()
        self._run_waiters: dict[asyncio.Future[Any], int] = {}
        self._group_tasks: dict[object, set[asyncio.Future[Any]]] = {}
        self._group_holds: dict[object, int] = {}
        self._group_events: dict[object, asyncio.Event] = {}
        self._shutdown_deadline: float | None = None
        self._publish_snapshot()

    def snapshot(self) -> CleanupSnapshot:
        now = time.monotonic()
        by_kind: dict[str, int] = {}
        oldest_started_at: float | None = None
        for tracked in self._tasks.values():
            by_kind[tracked.kind] = by_kind.get(tracked.kind, 0) + 1
            if oldest_started_at is None or tracked.started_at < oldest_started_at:
                oldest_started_at = tracked.started_at
        return CleanupSnapshot(
            in_flight=len(self._tasks),
            oldest_age_seconds=(
                None if oldest_started_at is None else max(0.0, now - oldest_started_at)
            ),
            by_kind=dict(sorted(by_kind.items())),
        )

    def track(
        self,
        task: asyncio.Future[T],
        *,
        kind: str,
        timeout_seconds: float | None = None,
        scope: CleanupScope | None = None,
    ) -> asyncio.Future[T]:
        """Take ownership of an existing task and consume its eventual result."""

        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("cleanup timeout must be greater than zero")
        now = time.monotonic()
        deadline = now + timeout
        inherited_scope = scope or self.current_scope()
        if inherited_scope is not None:
            deadline = min(deadline, inherited_scope.deadline)
        if self._shutdown_deadline is not None:
            deadline = min(deadline, self._shutdown_deadline)

        tracked = self._tasks.get(task)
        if tracked is not None:
            if deadline < tracked.deadline:
                self._shorten_deadline(task, deadline)
                tracked = self._tasks[task]
            if inherited_scope is not None:
                new_groups = inherited_scope.ownership_groups - tracked.groups
                if new_groups:
                    merged_groups = tracked.groups | new_groups
                    self._tasks[task] = replace(tracked, groups=merged_groups)
                    for group in new_groups:
                        self._add_task_to_group(group, task)
            return task

        if inherited_scope is not None:
            group = inherited_scope.group
            groups = inherited_scope.ownership_groups
        else:
            group = object()
            groups = frozenset({group})
        tracked = _TrackedCleanup(
            kind=_canonical_kind(kind),
            started_at=now,
            deadline=deadline,
            group=group,
            groups=groups,
        )
        self._tasks[task] = tracked
        for ownership_group in groups:
            self._add_task_to_group(ownership_group, task)
        task.add_done_callback(self._task_finished)
        self._deadline_handles[task] = asyncio.get_running_loop().call_later(
            max(0.0, deadline - now),
            self._deadline_expired,
            task,
        )
        self._publish_snapshot()
        return task

    def _add_task_to_group(
        self,
        group: object,
        task: asyncio.Future[Any],
    ) -> None:
        event = self._group_events.get(group)
        if event is not None and event.is_set():
            self._group_events[group] = asyncio.Event()
        self._group_tasks.setdefault(group, set()).add(task)

    def current_scope(self) -> CleanupScope | None:
        """Capture the current task's cleanup deadline for an owned hand-off."""

        current = asyncio.current_task()
        tracked = self._tasks.get(current) if current is not None else None
        if tracked is None:
            return None
        return CleanupScope(
            deadline=tracked.deadline,
            group=tracked.group,
            groups=tracked.groups,
        )

    def retain_scope(self, scope: CleanupScope) -> CleanupScopeHold:
        """Prevent a group waiter from observing a gap between nested tasks."""

        groups = scope.ownership_groups
        for group in groups:
            event = self._group_events.get(group)
            if event is not None and event.is_set():
                self._group_events[group] = asyncio.Event()
            self._group_holds[group] = self._group_holds.get(group, 0) + 1
        return CleanupScopeHold(self, groups)

    def start(
        self,
        awaitable: Awaitable[T],
        *,
        kind: str,
        timeout_seconds: float | None = None,
        scope: CleanupScope | None = None,
    ) -> asyncio.Future[T]:
        task = asyncio.ensure_future(awaitable)
        if isinstance(task, asyncio.Task):
            task.set_name(f"camouflare-cleanup-{_canonical_kind(kind)}")
        return self.track(
            task,
            kind=kind,
            timeout_seconds=timeout_seconds,
            scope=scope,
        )

    def group_for(self, task: asyncio.Future[Any]) -> object:
        """Return the ownership group shared by a cleanup task and its descendants."""

        tracked = self._tasks.get(task)
        if tracked is None:
            raise RuntimeError("Cleanup task is no longer tracked")
        return tracked.group

    async def wait_for_group(self, group: object) -> None:
        """Wait until a root cleanup and every nested physical cleanup has finished."""

        while self._group_tasks.get(group) or self._group_holds.get(group, 0) > 0:
            event = self._group_events.setdefault(group, asyncio.Event())
            await event.wait()
            if self._group_events.get(group) is event:
                self._group_events.pop(group, None)

    async def run(
        self,
        awaitable: Awaitable[T],
        *,
        kind: str,
        timeout_seconds: float | None = None,
        scope: CleanupScope | None = None,
    ) -> T:
        """Run cleanup under a hard deadline without tying it to the caller task."""

        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("cleanup timeout must be greater than zero")
        task = self.start(
            awaitable,
            kind=kind,
            timeout_seconds=timeout,
            scope=scope,
        )
        tracked = self._tasks[task]
        self._run_waiters[task] = self._run_waiters.get(task, 0) + 1
        try:
            remaining = max(0.0, tracked.deadline - time.monotonic())
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if task not in done:
                self._mark_timed_out(task)
                raise TimeoutError(f"{kind} cleanup exceeded {timeout:g} seconds")
            if task in self._timed_out:
                raise TimeoutError(f"{kind} cleanup exceeded {timeout:g} seconds")
            return task.result()
        finally:
            waiters = self._run_waiters.get(task, 1) - 1
            if waiters <= 0:
                self._run_waiters.pop(task, None)
                if task.done():
                    self._timed_out.discard(task)
            else:
                self._run_waiters[task] = waiters

    async def close(self, *, timeout_seconds: float | None = None) -> None:
        """Cancel and drain owned tasks within a bounded runtime-shutdown budget."""

        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout < 0:
            raise ValueError("cleanup timeout must be zero or greater")
        deadline = time.monotonic() + timeout
        if self._shutdown_deadline is None:
            self._shutdown_deadline = deadline
        else:
            self._shutdown_deadline = min(self._shutdown_deadline, deadline)

        while True:
            tasks = set(self._tasks)
            if not tasks:
                # Let cancellation finalizers scheduled by the last completed task
                # register any nested cleanup before declaring the supervisor drained.
                await asyncio.sleep(0)
                if not self._tasks:
                    return
                continue

            for task in tasks:
                self._shorten_deadline(task, self._shutdown_deadline)
                task.cancel()
            remaining = max(0.0, self._shutdown_deadline - time.monotonic())
            if remaining > 0:
                await asyncio.wait(tasks, timeout=remaining)
            await asyncio.sleep(0)
            if time.monotonic() >= self._shutdown_deadline:
                for task in set(self._tasks):
                    self._mark_timed_out(task)
                await asyncio.sleep(0)
                return

    def _task_finished(self, task: asyncio.Future[Any]) -> None:
        tracked = self._tasks.pop(task, None)
        if tracked is None:
            return
        deadline_handle = self._deadline_handles.pop(task, None)
        if deadline_handle is not None:
            deadline_handle.cancel()
        if task in self._timed_out:
            # The timeout result was emitted at the hard deadline.  Consume the
            # task's eventual result here without reporting a second outcome.
            if not task.cancelled():
                with suppress(BaseException):
                    task.exception()
            if self._run_waiters.get(task, 0) == 0:
                self._timed_out.discard(task)
            for group in tracked.groups:
                self._group_task_finished(group, task)
            self._publish_snapshot()
            return
        result = "success"
        if task.cancelled():
            result = "cancelled"
        else:
            try:
                exception = task.exception()
            except BaseException:
                result = "error"
            else:
                if isinstance(exception, TimeoutError):
                    result = "timeout"
                elif exception is not None:
                    result = "error"
        recorder = getattr(metrics, "record_cleanup", None)
        if callable(recorder):
            recorder(
                kind=tracked.kind,
                result=result,
                duration_seconds=max(0.0, time.monotonic() - tracked.started_at),
            )
        for group in tracked.groups:
            self._group_task_finished(group, task)
        self._publish_snapshot()

    def _deadline_expired(self, task: asyncio.Future[Any]) -> None:
        self._deadline_handles.pop(task, None)
        self._mark_timed_out(task)

    def _mark_timed_out(self, task: asyncio.Future[Any]) -> None:
        tracked = self._tasks.get(task)
        if tracked is None or task.done() or task in self._timed_out:
            return
        self._timed_out.add(task)
        recorder = getattr(metrics, "record_cleanup", None)
        if callable(recorder):
            recorder(
                kind=tracked.kind,
                result="timeout",
                duration_seconds=max(0.0, time.monotonic() - tracked.started_at),
            )
        task.cancel()

    def _shorten_deadline(self, task: asyncio.Future[Any], deadline: float) -> None:
        tracked = self._tasks.get(task)
        if tracked is None or tracked.deadline <= deadline:
            return
        self._tasks[task] = replace(tracked, deadline=deadline)
        handle = self._deadline_handles.pop(task, None)
        if handle is not None:
            handle.cancel()
        self._deadline_handles[task] = asyncio.get_running_loop().call_later(
            max(0.0, deadline - time.monotonic()),
            self._deadline_expired,
            task,
        )

    def _group_task_finished(self, group: object, task: asyncio.Future[Any]) -> None:
        tasks = self._group_tasks.get(group)
        if tasks is None:
            return
        tasks.discard(task)
        if tasks:
            return
        self._group_tasks.pop(group, None)
        self._finish_group_if_idle(group)

    def _release_scope_hold(self, group: object) -> None:
        holds = self._group_holds.get(group, 0)
        if holds <= 0:
            raise RuntimeError("Cleanup-scope hold accounting underflow")
        if holds == 1:
            self._group_holds.pop(group, None)
        else:
            self._group_holds[group] = holds - 1
        self._finish_group_if_idle(group)

    def _finish_group_if_idle(self, group: object) -> None:
        if self._group_tasks.get(group) or self._group_holds.get(group, 0) > 0:
            return
        event = self._group_events.get(group)
        if event is not None:
            event.set()

    def _publish_snapshot(self) -> None:
        publisher = getattr(metrics, "set_cleanup_snapshot", None)
        if callable(publisher):
            publisher(by_kind=self.snapshot().by_kind)


def _canonical_kind(kind: str) -> str:
    return kind if kind in _CLEANUP_KINDS else "other"
