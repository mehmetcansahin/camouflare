from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from camouflare.app import create_app
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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser_client() -> AsyncIterator[AsyncClient]:
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    unhandled_futures: list[dict[str, object]] = []

    def capture_unhandled_future(
        event_loop: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        if context.get("message") == "Future exception was never retrieved":
            unhandled_futures.append(context)
            return
        if previous_exception_handler is not None:
            previous_exception_handler(event_loop, context)
        else:
            event_loop.default_exception_handler(context)

    loop.set_exception_handler(capture_unhandled_future)
    app = create_app(
        settings=make_browser_test_settings(),
        browser_factory=make_offline_camoufox_factory(),
        lifespan_enabled=False,
    )
    try:
        await app.state.pool.start()
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://camouflare.test",
        ) as client:
            yield client
    finally:
        try:
            await app.state.sessions.close()
        finally:
            try:
                await app.state.pool.close()
            finally:
                gc.collect()
                await asyncio.sleep(0)
                loop.set_exception_handler(previous_exception_handler)

    if unhandled_futures:
        failures = []
        for context in unhandled_futures:
            exception = context.get("exception")
            failures.append(f"{type(exception).__name__}: {exception}")
        pytest.fail("Unhandled asyncio future(s) during browser cleanup: " + "; ".join(failures))
