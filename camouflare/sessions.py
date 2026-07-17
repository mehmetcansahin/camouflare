from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from camouflare.cleanup import CleanupSupervisor
from camouflare.metrics import record_session_event, set_session_snapshot
from camouflare.protocols import AsyncCleanup, BrowserContextLike

logger = logging.getLogger(__name__)


@dataclass
class Session:
    session_id: str
    context: BrowserContextLike
    proxy: dict[str, str] | None = None
    ttl_seconds: int | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    on_close: AsyncCleanup | None = None
    # Number of in-flight requests that have checked out this session and may be
    # about to acquire ``lock``. Incremented synchronously the moment a request
    # takes the session (before it awaits the lock), so prune/rotation can tell a
    # just-woken lock waiter apart from a truly idle session -- ``lock.locked()``
    # briefly reports False in the gap between release and the waiter resuming.
    in_use: int = 0

    def expired(self) -> bool:
        if self.ttl_seconds is None:
            return False
        return time.monotonic() - self.created_at >= self.ttl_seconds

    def touch(self) -> None:
        self.last_used_at = time.monotonic()

    async def close(self) -> None:
        if self.on_close is not None:
            await self.on_close()
            return
        await self.context.close()


@dataclass(frozen=True)
class SessionSnapshot:
    """Immutable summary of session state for metrics and operational checks."""

    active: int
    in_use: int
    closing: int
    max_sessions: int


@dataclass(frozen=True)
class _ClosingSession:
    """A session whose cleanup is owned by the manager, not its caller."""

    session: Session
    task: asyncio.Task[None]
    reason: str


class SessionManager:
    def __init__(
        self,
        *,
        max_sessions: int,
        default_ttl_seconds: int,
        cleanup_timeout_seconds: float = 10,
        cleanup_supervisor: CleanupSupervisor | None = None,
    ) -> None:
        if cleanup_timeout_seconds <= 0:
            raise ValueError("cleanup timeout must be greater than zero")
        self._max_sessions = max_sessions
        self._default_ttl_seconds = default_ttl_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._cleanup = cleanup_supervisor or CleanupSupervisor(
            timeout_seconds=cleanup_timeout_seconds
        )
        self._sessions: dict[str, Session] = {}
        self._closing: dict[str, _ClosingSession] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def snapshot(self) -> SessionSnapshot:
        closing_sessions = [closing.session for closing in self._closing.values()]
        return SessionSnapshot(
            active=len(self._sessions),
            in_use=sum(session.in_use for session in self._sessions.values())
            + sum(session.in_use for session in closing_sessions),
            closing=len(self._closing),
            max_sessions=self._max_sessions,
        )

    def mark_in_use(self, session: Session) -> None:
        session.in_use += 1
        self._publish_metrics()

    def mark_released(self, session: Session) -> None:
        if session.in_use <= 0:
            raise RuntimeError("Session in-use accounting underflow.")
        session.in_use -= 1
        self._publish_metrics()

    def _publish_metrics(self) -> None:
        snapshot = self.snapshot()
        set_session_snapshot(
            active=snapshot.active,
            in_use=snapshot.in_use,
            closing=snapshot.closing,
        )

    def register_or_get(
        self,
        session_id: str,
        context: BrowserContextLike,
        *,
        proxy: dict[str, str] | None = None,
        on_close: AsyncCleanup | None = None,
        ttl_seconds: int | None = None,
    ) -> tuple[Session, bool]:
        """Atomically register a session, or return the existing one if the id is taken.

        The check-and-insert runs without awaiting, so two concurrent callers that
        both built a context for the same new id cannot both win: the first inserts,
        the second gets ``(existing, False)`` and is expected to discard its context.
        """
        if self._closed:
            raise RuntimeError("Session manager is closed.")
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing, False
        if session_id in self._closing:
            raise RuntimeError("Session is still closing.")
        if len(self._sessions) + len(self._closing) >= self._max_sessions:
            record_session_event("rejected")
            raise RuntimeError("Maximum sessions reached.")
        session = Session(
            session_id=session_id,
            context=context,
            proxy=proxy,
            ttl_seconds=self._default_ttl_seconds if ttl_seconds is None else ttl_seconds,
            on_close=on_close,
        )
        self._sessions[session_id] = session
        record_session_event("created")
        self._publish_metrics()
        return session, True

    def register_existing(
        self,
        session_id: str,
        context: BrowserContextLike,
        *,
        proxy: dict[str, str] | None = None,
        on_close: AsyncCleanup | None = None,
        ttl_seconds: int | None = None,
    ) -> Session:
        session, created = self.register_or_get(
            session_id, context, proxy=proxy, on_close=on_close, ttl_seconds=ttl_seconds
        )
        if not created:
            raise RuntimeError("Session already exists.")
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_ids(self) -> list[str]:
        return sorted(self._sessions)

    def is_closing(self, session_id: str) -> bool:
        """Return whether an id is reserved by an in-progress cleanup."""

        return session_id in self._closing

    async def prune_expired(self, *, exclude: str | None = None) -> list[str]:
        async with self._lock:
            if self._closed:
                return []
            expired_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session_id != exclude
                and session.expired()
                and session.in_use == 0
                and not session.lock.locked()
            ]
            expired_sessions = [self._sessions.pop(session_id) for session_id in expired_ids]
            closing = [
                self._start_closing_unlocked(session, reason="expired")
                for session in expired_sessions
            ]
            self._publish_metrics()
        for _session in expired_sessions:
            record_session_event("expired")
        # All expired cleanups are started before waiting for any one of them.
        # Shielding preserves manager ownership if the reaper itself is cancelled.
        if closing:
            await asyncio.gather(
                *(asyncio.shield(item.task) for item in closing),
                return_exceptions=True,
            )
        return sorted(expired_ids)

    async def destroy(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                closing = self._start_closing_unlocked(session, reason="destroyed")
            else:
                closing = self._closing.get(session_id)
            self._publish_metrics()
        if closing is None:
            return False
        # The manager-owned task keeps running if this request is cancelled. A
        # retry attaches to that same task instead of starting overlapping cleanup.
        await asyncio.shield(closing.task)
        return True

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            pending = list(self._closing.values())
            pending.extend(
                self._start_closing_unlocked(session, reason="destroyed") for session in sessions
            )
            self._publish_metrics()
        if pending:
            # Do not transfer task ownership to the caller: shutdown deadlines may
            # cancel this coroutine, but each cleanup remains tracked to completion.
            await asyncio.gather(
                *(asyncio.shield(item.task) for item in pending),
                return_exceptions=True,
            )

    def _start_closing_unlocked(self, session: Session, *, reason: str) -> _ClosingSession:
        existing = self._closing.get(session.session_id)
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._close_session(session),
            name="camouflare-session-close",
        )
        closing = _ClosingSession(session=session, task=task, reason=reason)
        self._closing[session.session_id] = closing
        task.add_done_callback(
            lambda completed, session_id=session.session_id: self._closing_done(
                session_id, completed
            )
        )
        return closing

    async def _close_session(self, session: Session) -> None:
        # Wait for an in-flight request to leave the shared context before closing
        # it. Requests queued behind the remover will observe that the session is no
        # longer registered and must not use the context.
        async with session.lock:
            task = self._cleanup.start(
                session.close(),
                kind="session",
                timeout_seconds=self._cleanup_timeout_seconds,
            )
            group = self._cleanup.group_for(task)
            error: BaseException | None = None
            try:
                await asyncio.shield(task)
            except BaseException as exc:
                error = exc
            # A timed-out parent may have spawned context/browser/proxy cleanup.
            # Keep the session id and capacity tombstone until that whole physical
            # cleanup group has actually stopped.
            await self._cleanup.wait_for_group(group)
            if error is not None:
                raise error

    def _closing_done(self, session_id: str, task: asyncio.Task[None]) -> None:
        closing = self._closing.get(session_id)
        if closing is None or closing.task is not task:
            # Still consume the result if a future implementation replaces a task.
            self._consume_cleanup_result(session_id, task, reason="unknown")
            return
        self._closing.pop(session_id, None)
        succeeded = self._consume_cleanup_result(session_id, task, reason=closing.reason)
        if succeeded and closing.reason == "destroyed":
            record_session_event("destroyed")
        self._publish_metrics()

    @staticmethod
    def _consume_cleanup_result(
        session_id: str,
        task: asyncio.Task[None],
        *,
        reason: str,
    ) -> bool:
        try:
            error = task.exception()
        except asyncio.CancelledError:
            error = asyncio.CancelledError()
        if error is None:
            return True
        record_session_event("error")
        logger.error(
            "Failed to close session (%s): %s",
            reason,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        return False
