from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Protocol, cast

from fastapi import FastAPI

from camouflare.cleanup import CleanupSupervisor
from camouflare.config import Settings
from camouflare.metrics import install_asyncio_exception_metrics, record_timeout
from camouflare.protocols import AsyncClose
from camouflare.sessions import SessionManager

logger = logging.getLogger(__name__)

AppLifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


class PoolMaintenance(Protocol):
    async def maintain(self) -> None: ...


def make_runtime_lifespan(settings: Settings) -> AppLifespan:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        restore_exception_handler = install_asyncio_exception_metrics()
        try:
            await app.state.pool.start()
            reaper = asyncio.create_task(
                session_reaper(
                    app.state.sessions,
                    interval_seconds=settings.session_reaper_interval_seconds,
                ),
                name="camouflare-session-reaper",
            )
            maintainer = asyncio.create_task(
                pool_maintainer(
                    app.state.pool,
                    interval_seconds=settings.pool_maintenance_interval_seconds,
                ),
                name="camouflare-pool-maintainer",
            )
            try:
                yield
            finally:

                async def finalize_runtime() -> None:
                    reaper.cancel()
                    maintainer.cancel()
                    await asyncio.gather(reaper, maintainer, return_exceptions=True)
                    await shutdown_runtime(
                        sessions=app.state.sessions,
                        pool=app.state.pool,
                        direct_http=getattr(app.state, "direct_http", None),
                        cleanup=getattr(app.state, "cleanup", None),
                        timeout_seconds=settings.shutdown_timeout_seconds,
                    )

                finalizer = asyncio.create_task(
                    finalize_runtime(),
                    name="camouflare-runtime-finalizer",
                )
                finalizer.add_done_callback(_consume_shutdown_task)
                try:
                    await asyncio.shield(finalizer)
                except asyncio.CancelledError:
                    # A host may cancel the lifespan task during its own shutdown
                    # deadline. The owned finalizer must still quiesce acquisition
                    # and initiate every cleanup phase before cancellation escapes.
                    with suppress(BaseException):
                        await asyncio.shield(finalizer)
                    raise
        finally:
            restore_exception_handler()

    return lifespan


async def session_reaper(
    sessions: SessionManager,
    *,
    interval_seconds: int,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await sessions.prune_expired(wait_for_cleanup=False)
        except Exception:
            logger.exception("Failed to prune expired sessions.")


async def pool_maintainer(
    pool: PoolMaintenance,
    *,
    interval_seconds: int,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await pool.maintain()
        except Exception:
            logger.exception("Failed to maintain the browser pool.")


async def shutdown_runtime(
    *,
    sessions: AsyncClose,
    pool: AsyncClose,
    direct_http: AsyncClose | None = None,
    cleanup: AsyncClose | None = None,
    timeout_seconds: float,
) -> None:
    shutdown = asyncio.create_task(
        _shutdown_runtime_owned(
            sessions=sessions,
            pool=pool,
            direct_http=direct_http,
            cleanup=cleanup,
            timeout_seconds=timeout_seconds,
        ),
        name="camouflare-runtime-shutdown",
    )
    shutdown.add_done_callback(_consume_shutdown_task)
    try:
        await asyncio.shield(shutdown)
    except asyncio.CancelledError:
        # Cancellation of the supervisor is distinct from cancellation of one
        # owned phase. Preserve the caller's cancellation, but only after the
        # bounded shutdown sequence has initiated every ordered phase.
        with suppress(BaseException):
            await asyncio.shield(shutdown)
        raise


async def _shutdown_runtime_owned(
    *,
    sessions: AsyncClose,
    pool: AsyncClose,
    direct_http: AsyncClose | None = None,
    cleanup: AsyncClose | None = None,
    timeout_seconds: float,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    deadline_expired = False

    async def close_resource(name: str, close: Callable[[], Awaitable[None]]) -> None:
        nonlocal deadline_expired

        def consume_phase_result(task: asyncio.Task[None]) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                # Cancellation raised by the owned phase is a phase failure, not
                # cancellation of this ordered shutdown supervisor.
                logger.error("Shutdown phase %s cancelled itself.", name)
            except Exception:
                logger.exception("Failed while closing %s.", name)

        async def invoke_close() -> None:
            await close()

        task = asyncio.create_task(invoke_close(), name=f"camouflare-shutdown-{name}")
        task.add_done_callback(_consume_shutdown_task)
        # Start every ordered phase even if a prior phase exhausted the budget.
        # In particular, pool.quiesce has already made late lease releases retire.
        await asyncio.sleep(0)
        if task.done():
            consume_phase_result(task)
            return
        remaining = max(0.0, deadline - loop.time())
        if remaining == 0:
            deadline_expired = True
            task.cancel()
            return
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if task not in done:
            deadline_expired = True
            task.cancel()
            return
        consume_phase_result(task)

    # A single absolute deadline covers the ordered sequence. Each later phase is
    # still initiated after exhaustion so it can establish ownership/cancellation.
    quiesce = getattr(pool, "quiesce", None)
    if callable(quiesce):
        await close_resource(
            "browser-pool-acquisition",
            cast(Callable[[], Awaitable[None]], quiesce),
        )
    direct_http_quiesce = getattr(direct_http, "quiesce", None)
    if callable(direct_http_quiesce):
        await close_resource(
            "direct-http-acquisition",
            cast(Callable[[], Awaitable[None]], direct_http_quiesce),
        )
    await close_resource("sessions", sessions.close)
    await close_resource("browser-pool", pool.close)
    if direct_http is not None:
        await close_resource("direct-http-workers", direct_http.close)
    if isinstance(cleanup, CleanupSupervisor):
        remaining = max(0.0, deadline - loop.time())
        await cleanup.close(timeout_seconds=remaining)
        deadline_expired = deadline_expired or loop.time() >= deadline
    elif cleanup is not None:
        await close_resource("background-cleanup", cleanup.close)

    if deadline_expired:
        record_timeout("shutdown")
        logger.error("Shutdown deadline expired while closing runtime resources.")


def _consume_shutdown_task(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    with suppress(BaseException):
        task.exception()
