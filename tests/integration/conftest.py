from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from camouflare.app import create_app
from camouflare.runtime import shutdown_runtime
from tests.integration.support import (
    LocalHttpServer,
    make_browser_test_settings,
    make_offline_camoufox_factory,
)


@pytest.fixture(scope="session")
def local_http_server() -> Iterator[LocalHttpServer]:
    server = LocalHttpServer.start()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture(scope="module")
def browser_app() -> FastAPI:
    return create_app(
        settings=make_browser_test_settings(),
        browser_factory=make_offline_camoufox_factory(),
        lifespan_enabled=False,
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser_client(browser_app: FastAPI) -> AsyncIterator[AsyncClient]:
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    unexpected_asyncio: list[dict[str, object]] = []
    baseline_tasks = set(asyncio.all_tasks())
    leaked_tasks: list[str] = []

    def capture_unexpected_asyncio(
        event_loop: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        message = str(context.get("message", "")).lower()
        if (
            "exception was never retrieved" in message
            or "task was destroyed" in message
            or "async generator" in message
            or "async_generator" in message
            or "asyncgenerator" in message
            or "asynchronous generator" in message
            or context.get("asyncgen") is not None
        ):
            unexpected_asyncio.append(context)
            return
        if previous_exception_handler is not None:
            previous_exception_handler(event_loop, context)
        else:
            event_loop.default_exception_handler(context)

    loop.set_exception_handler(capture_unexpected_asyncio)
    try:
        await browser_app.state.pool.start()
        async with AsyncClient(
            transport=ASGITransport(app=browser_app, raise_app_exceptions=False),
            base_url="http://camouflare.test",
        ) as client:
            yield client
    finally:
        try:
            await shutdown_runtime(
                sessions=browser_app.state.sessions,
                pool=browser_app.state.pool,
                cleanup=browser_app.state.cleanup,
                timeout_seconds=browser_app.state.settings.shutdown_timeout_seconds,
            )
        finally:
            gc.collect()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            current = asyncio.current_task()
            pending = [
                task
                for task in asyncio.all_tasks()
                if task not in baseline_tasks and task is not current and not task.done()
            ]
            leaked_tasks = [f"{task.get_name()}: {task.get_coro()!r}" for task in pending]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=0.2)
                await asyncio.sleep(0)
            loop.set_exception_handler(previous_exception_handler)

    if unexpected_asyncio:
        failures = []
        for context in unexpected_asyncio:
            message = context.get("message")
            exception = context.get("exception")
            failures.append(f"{message}: {type(exception).__name__}: {exception}")
        pytest.fail("Unexpected asyncio cleanup event(s): " + "; ".join(failures))
    if leaked_tasks:
        pytest.fail("Pending asyncio task(s) after browser cleanup: " + "; ".join(leaked_tasks))
