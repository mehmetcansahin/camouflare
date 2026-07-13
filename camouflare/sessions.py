from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

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
    max_sessions: int


class SessionManager:
    def __init__(self, *, max_sessions: int, default_ttl_seconds: int) -> None:
        self._max_sessions = max_sessions
        self._default_ttl_seconds = default_ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            active=len(self._sessions),
            in_use=sum(session.in_use for session in self._sessions.values()),
            max_sessions=self._max_sessions,
        )

    def mark_in_use(self, session: Session) -> None:
        session.in_use += 1
        self._publish_metrics()

    def mark_released(self, session: Session) -> None:
        session.in_use = max(0, session.in_use - 1)
        self._publish_metrics()

    def _publish_metrics(self) -> None:
        snapshot = self.snapshot()
        set_session_snapshot(active=snapshot.active, in_use=snapshot.in_use)

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
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing, False
        if len(self._sessions) >= self._max_sessions:
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

    async def prune_expired(self, *, exclude: str | None = None) -> list[str]:
        async with self._lock:
            expired_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session_id != exclude
                and session.expired()
                and session.in_use == 0
                and not session.lock.locked()
            ]
            expired_sessions = [self._sessions.pop(session_id) for session_id in expired_ids]
            self._publish_metrics()
        for session in expired_sessions:
            record_session_event("expired")
            try:
                await session.close()
            except Exception:
                record_session_event("error")
                logger.exception("Failed to close expired session %s.", session.session_id)
        return sorted(expired_ids)

    async def destroy(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            self._publish_metrics()
        if session is None:
            return False
        # Wait for any in-flight request holding the session lock to finish before
        # closing the shared context, so we never close it out from under a request.
        try:
            async with session.lock:
                await session.close()
        except BaseException:
            record_session_event("error")
            raise
        record_session_event("destroyed")
        return True

    async def close(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._publish_metrics()
        results = await asyncio.gather(
            *(session.close() for session in sessions),
            return_exceptions=True,
        )
        for session, result in zip(sessions, results, strict=True):
            if isinstance(result, BaseException):
                record_session_event("error")
                logger.error(
                    "Failed to close session %s during shutdown: %s",
                    session.session_id,
                    result,
                )
            else:
                record_session_event("destroyed")
