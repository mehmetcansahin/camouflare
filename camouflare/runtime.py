from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from camouflare.config import Settings
from camouflare.metrics import record_timeout
from camouflare.protocols import AsyncClose
from camouflare.sessions import SessionManager

logger = logging.getLogger(__name__)

AppLifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def make_runtime_lifespan(settings: Settings) -> AppLifespan:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        await app.state.pool.start()
        reaper = asyncio.create_task(
            session_reaper(
                app.state.sessions,
                interval_seconds=settings.session_reaper_interval_seconds,
            ),
            name="camouflare-session-reaper",
        )
        try:
            yield
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
            await shutdown_runtime(
                sessions=app.state.sessions,
                pool=app.state.pool,
                timeout_seconds=settings.shutdown_timeout_seconds,
            )

    return lifespan


async def session_reaper(
    sessions: SessionManager,
    *,
    interval_seconds: int,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await sessions.prune_expired()
        except Exception:
            logger.exception("Failed to prune expired sessions.")


async def shutdown_runtime(
    *,
    sessions: AsyncClose,
    pool: AsyncClose,
    timeout_seconds: float,
) -> None:
    async def close_resource(name: str, close: Callable[[], Awaitable[None]]) -> None:
        try:
            await close()
        except Exception:
            logger.exception("Failed while closing %s.", name)

    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(
                close_resource("sessions", sessions.close),
                close_resource("browser pool", pool.close),
            )
    except TimeoutError:
        record_timeout("shutdown")
        logger.error("Shutdown deadline expired while closing runtime resources.")
