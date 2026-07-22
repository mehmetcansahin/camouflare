from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from camouflare.app import (
    _close_page,
    _context_options,
    _read_json_payload,
    _session_for_request,
    _session_reaper,
    create_app,
)
from camouflare.cleanup import CleanupSupervisor
from camouflare.commands import _close_proxy_best_effort, execute_request
from camouflare.config import Settings
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.limits import ResourceLimitError
from camouflare.metrics import REQUEST_COUNTER
from camouflare.models import Solution, V1Request, V1Response
from camouflare.sessions import SessionManager
from tests.fakes import FakeBrowser, FakeBrowserFactory, FakeContext, FakePage


def _streaming_request(body: bytes, *, content_length: int | None = None) -> Request:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1",
            "raw_path": b"/v1",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        },
        receive,
    )


@pytest.mark.anyio
async def test_documentation_endpoint_serves_advanced_html() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/documentation")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Camouflare API Documentation</title>" in response.text
    assert "POST /v1" in response.text
    assert "sessions.create" in response.text
    assert "request.get" in response.text
    assert "request.post" in response.text
    assert "returnScreenshot" in response.text
    assert "PROMETHEUS_ENABLED" in response.text
    assert "PROXY_URL" in response.text
    assert "PROXY_SERVER" in response.text
    assert '"proxy": {' in response.text
    assert "/openapi.json" in response.text


@pytest.mark.anyio
async def test_documentation_endpoint_includes_command_examples_and_error_reference() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/documentation")

    assert response.status_code == 200
    assert 'id="request-get"' in response.text
    assert 'id="request-post"' in response.text
    assert 'id="error-reference"' in response.text
    assert '"cmd": "sessions.list"' in response.text
    assert '"cmd": "request.post"' in response.text
    assert "requires both" in response.text
    assert "HTTP 503" in response.text
    assert "returnOnlyCookies omits" in response.text


@pytest.mark.anyio
async def test_documentation_endpoint_does_not_touch_browser_pool() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/documentation")

    assert response.status_code == 200
    assert factory.created == []


@pytest.mark.anyio
async def test_openapi_documents_v1_request_schema_and_error_responses() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    operation = schema["paths"]["/v1"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]

    assert operation["summary"] == "Run a Camouflare command"
    assert "sessions.create" in operation["description"]
    assert "request.post" in operation["description"]
    assert "500" in operation["responses"]
    assert "503" in operation["responses"]
    assert request_schema["properties"]["cmd"]["description"].startswith("Command to run")
    assert "maxTimeout" in request_schema["properties"]
    assert "returnOnlyCookies" in request_schema["properties"]
    assert request_schema["properties"]["headers"]["anyOf"][0]["type"] == "object"
    assert "request.get" in operation["requestBody"]["content"]["application/json"]["examples"]


@pytest.mark.anyio
async def test_v1_request_get_matches_flaresolverr_response_shape() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "maxTimeout": 60000,
            },
        )

    await app.state.pool.close()

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["solution"]["url"] == "https://example.com"
    assert body["solution"]["userAgent"] == "FakeBrowser/1.0"
    assert "startTimestamp" in body
    assert "endTimestamp" in body


@pytest.mark.anyio
async def test_api_token_protects_non_health_endpoints() -> None:
    app = create_app(
        settings=Settings(camouflare_api_token="secret-token"),
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        v1_response = await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com"},
        )
        ready_response = await client.get("/ready")
        diagnostics_response = await client.get("/diagnostics")
        documentation_response = await client.get("/documentation")
        health_response = await client.get("/health")

    assert v1_response.status_code == 401
    assert v1_response.json() == {"detail": "Unauthorized"}
    assert ready_response.status_code == 401
    assert ready_response.json() == {"detail": "Unauthorized"}
    assert diagnostics_response.status_code == 401
    assert diagnostics_response.json() == {"detail": "Unauthorized"}
    assert documentation_response.status_code == 401
    assert documentation_response.json() == {"detail": "Unauthorized"}
    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_api_token_accepts_authorization_bearer_header() -> None:
    factory = FakeBrowserFactory()
    app = create_app(
        settings=Settings(camouflare_api_token="secret-token"),
        browser_factory=factory,
        lifespan_enabled=False,
    )
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            headers={"Authorization": "Bearer secret-token"},
            json={"cmd": "request.get", "url": "https://example.com"},
        )

    await app.state.pool.close()

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.anyio
async def test_api_token_accepts_x_api_token_header() -> None:
    app = create_app(
        settings=Settings(camouflare_api_token="secret-token"),
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/documentation", headers={"X-API-Token": "secret-token"})

    assert response.status_code == 200
    assert "<title>Camouflare API Documentation</title>" in response.text


@pytest.mark.anyio
async def test_diagnostics_is_token_protected_passive_and_reports_capacity() -> None:
    factory = FakeBrowserFactory()
    app = create_app(
        settings=Settings(
            camouflare_api_token="secret-token",
            pool_max_browsers=1,
            pool_max_contexts_per_browser=2,
        ),
        browser_factory=factory,
        lifespan_enabled=False,
    )
    await app.state.pool.start()

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/diagnostics",
                headers={"X-API-Token": "secret-token"},
            )
    finally:
        await app.state.pool.close()
        await app.state.cleanup.close()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["capacity_state"] == "available"
    assert body["pool"] == {
        "ready_browser_slots": 1,
        "retiring_browser_slots": 0,
        "creating_slots": 0,
        "closing_slots": 0,
        "active_contexts": 0,
        "transient_contexts": 0,
        "persistent_contexts": 0,
        "waiting_requests": 0,
        "usable_context_slots": 2,
        "idle_recyclable_slots": 0,
        "max_browsers": 1,
        "max_contexts_per_browser": 2,
        "max_slots": 2,
    }
    assert body["sessions"] == {
        "active": 0,
        "in_use": 0,
        "closing": 0,
        "max_sessions": 32,
    }
    assert body["cleanup"] == {
        "in_flight": 0,
        "oldest_age_seconds": None,
        "by_kind": {},
    }
    assert set(body["runtime"]) == {"playwright_version", "playwright_cancel_patch"}
    assert "secret-token" not in response.text
    assert factory.created[0].contexts == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("updates", "expected_state"),
    [
        ({"ready_browser_slots": 1, "usable_context_slots": 1}, "available"),
        ({"creating_slots": 1}, "recovering"),
        ({"active_contexts": 1, "transient_contexts": 1}, "saturated"),
        (
            {
                "max_browsers": 0,
                "max_contexts_per_browser": 0,
                "max_slots": 0,
            },
            "unavailable",
        ),
        ({}, "unavailable"),
    ],
)
async def test_diagnostics_classifies_capacity_without_leasing_browser(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, int],
    expected_state: str,
) -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    snapshot = {
        "browser_slots": 0,
        "ready_browser_slots": 0,
        "retiring_browser_slots": 0,
        "creating_slots": 0,
        "closing_slots": 0,
        "active_contexts": 0,
        "transient_contexts": 0,
        "persistent_contexts": 0,
        "waiting_requests": 0,
        "usable_context_slots": 0,
        "idle_recyclable_slots": 0,
        "max_browsers": 2,
        "max_contexts_per_browser": 1,
        "max_slots": 2,
    }
    snapshot.update(updates)
    monkeypatch.setattr(app.state.pool, "snapshot", lambda: snapshot)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/diagnostics")
    finally:
        await app.state.pool.close()
        await app.state.cleanup.close()

    assert response.status_code == 200
    assert response.json()["capacity_state"] == expected_state


@pytest.mark.anyio
async def test_diagnostics_falls_back_to_http_200_when_snapshot_reader_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    def fail_snapshot() -> object:
        raise RuntimeError("corrupt capacity state")

    monkeypatch.setattr(app.state.pool, "snapshot", fail_snapshot)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/diagnostics")

    assert response.status_code == 200
    assert response.json()["capacity_state"] == "unavailable"
    assert response.json()["pool"]["max_slots"] > 0


@pytest.mark.anyio
async def test_diagnostics_sanitizes_non_finite_cleanup_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    monkeypatch.setattr(
        app.state.cleanup,
        "snapshot",
        lambda: {
            "in_flight": 1,
            "oldest_age_seconds": float("inf"),
            "by_kind": {"context": 1},
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/diagnostics")

    assert response.status_code == 200
    assert response.json()["cleanup"]["oldest_age_seconds"] is None


@pytest.mark.anyio
async def test_diagnostics_sanitizes_non_finite_integer_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    monkeypatch.setattr(
        app.state.pool,
        "snapshot",
        lambda: {
            "active_contexts": float("inf"),
            "usable_context_slots": float("-inf"),
            "max_browsers": float("inf"),
            "max_contexts_per_browser": float("inf"),
            "max_slots": float("inf"),
        },
    )
    monkeypatch.setattr(
        app.state.cleanup,
        "snapshot",
        lambda: {
            "in_flight": float("inf"),
            "oldest_age_seconds": None,
            "by_kind": {"context": float("inf")},
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capacity_state"] == "unavailable"
    assert payload["pool"]["active_contexts"] == 0
    assert payload["pool"]["max_slots"] > 0
    assert payload["cleanup"]["in_flight"] == 0
    assert payload["cleanup"]["by_kind"]["context"] == 0


@pytest.mark.anyio
async def test_diagnostics_never_exposes_session_or_proxy_secrets() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    app.state.sessions.register_existing(
        "private-session-id",
        FakeContext(),
        proxy={
            "server": "http://proxy.internal:8080",
            "username": "proxy-user",
            "password": "proxy-password",
        },
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/diagnostics")
    finally:
        await app.state.sessions.close()

    assert response.status_code == 200
    for secret in (
        "private-session-id",
        "proxy.internal",
        "proxy-user",
        "proxy-password",
    ):
        assert secret not in response.text


@pytest.mark.anyio
async def test_api_token_rejects_wrong_token() -> None:
    app = create_app(
        settings=Settings(camouflare_api_token="secret-token"),
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/documentation", headers={"X-API-Token": "wrong-token"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.anyio
async def test_sessions_commands_and_session_request() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        create_response = await client.post(
            "/v1",
            json={"cmd": "sessions.create", "session": "abc"},
        )
        list_response = await client.post("/v1", json={"cmd": "sessions.list"})
        get_response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "session": "abc",
            },
        )
        destroy_response = await client.post(
            "/v1",
            json={"cmd": "sessions.destroy", "session": "abc"},
        )

    await app.state.pool.close()

    assert create_response.json()["session"] == "abc"
    assert list_response.json()["sessions"] == ["abc"]
    assert get_response.json()["status"] == "ok"
    assert destroy_response.json()["status"] == "ok"


@pytest.mark.anyio
async def test_sessions_create_enforces_max_sessions() -> None:
    factory = FakeBrowserFactory()
    # Give the pool more persistent capacity than max_sessions so the session-count
    # limit (not the pool capacity guard) is what rejects the second create.
    app = create_app(
        settings=Settings(max_sessions=1, pool_max_contexts_per_browser=2),
        browser_factory=factory,
        lifespan_enabled=False,
    )
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_response = await client.post(
            "/v1",
            json={"cmd": "sessions.create", "session": "a"},
        )
        second_response = await client.post(
            "/v1",
            json={"cmd": "sessions.create", "session": "b"},
        )
        list_response = await client.post("/v1", json={"cmd": "sessions.list"})

    await app.state.sessions.close()
    await app.state.pool.close()

    assert first_response.status_code == 200
    assert second_response.status_code == 500
    assert "Maximum sessions reached" in second_response.json()["message"]
    assert list_response.json()["sessions"] == ["a"]


@pytest.mark.anyio
async def test_session_request_closes_page_but_keeps_context() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post("/v1", json={"cmd": "sessions.create", "session": "abc"})
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "session": "abc",
            },
        )

    session = app.state.sessions.get("abc")
    assert response.status_code == 200
    assert session is not None
    assert session.context.closed is False
    assert session.context.pages[-1].closed is True

    await app.state.sessions.close()
    await app.state.pool.close()


@pytest.mark.anyio
async def test_v1_returns_503_when_pool_is_saturated() -> None:
    factory = FakeBrowserFactory()
    settings = Settings(
        pool_max_browsers=1,
        pool_max_contexts_per_browser=1,
        pool_acquire_timeout_ms=10,
    )
    app = create_app(settings=settings, browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    held_context = app.state.pool.lease_context()
    await held_context.__aenter__()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1",
                json={
                    "cmd": "request.get",
                    "url": "https://example.com",
                    "maxTimeout": 60000,
                },
            )
    finally:
        await held_context.__aexit__(None, None, None)
        await app.state.pool.close()

    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["errorCode"] == "POOL_UNAVAILABLE"
    assert response.json()["retryable"] is True


@pytest.mark.anyio
async def test_max_timeout_bounds_pool_wait() -> None:
    factory = FakeBrowserFactory()
    settings = Settings(
        pool_max_browsers=1,
        pool_max_contexts_per_browser=1,
        pool_acquire_timeout_ms=5000,
    )
    app = create_app(settings=settings, browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    held_context = app.state.pool.lease_context()
    await held_context.__aenter__()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/v1",
                    json={
                        "cmd": "request.get",
                        "url": "https://example.com",
                        "maxTimeout": 20,
                    },
                ),
                timeout=0.5,
            )
    finally:
        await held_context.__aexit__(None, None, None)
        await app.state.pool.close()

    assert response.status_code == 500
    assert response.json()["status"] == "error"
    assert "maxTimeout" in response.json()["message"]
    assert response.json()["errorCode"] == "REQUEST_TIMEOUT"
    assert app.state.pool.snapshot().active_contexts == 0
    assert app.state.pool.snapshot().waiting_requests == 0


@pytest.mark.anyio
async def test_max_timeout_cancels_dedicated_dispatch_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_future = asyncio.get_running_loop().create_future()

    def cancel_callback_when_task_is_cancelled(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            callback_future.cancel()

    async def blocking_dispatch(
        _service: object,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.add_done_callback(cancel_callback_when_task_is_cancelled)
        await asyncio.Event().wait()

    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    monkeypatch.setattr(type(app.state.command_service), "dispatch", blocking_dispatch)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1",
                json={
                    "cmd": "request.get",
                    "url": "https://example.com",
                    "maxTimeout": 20,
                },
            )

        await asyncio.sleep(0)
        assert response.status_code == 500
        assert "maxTimeout" in response.json()["message"]
        assert callback_future.cancelled()
    finally:
        if not callback_future.done():
            callback_future.cancel()
        await app.state.pool.close()


@pytest.mark.anyio
async def test_max_timeout_does_not_wait_for_stuck_cancellation_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def stubborn_dispatch(
        _service: object,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_cleanup.wait()
        finally:
            cleanup_finished.set()

    app = create_app(
        settings=Settings(cleanup_timeout_seconds=1),
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )
    monkeypatch.setattr(type(app.state.command_service), "dispatch", stubborn_dispatch)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            started = time.monotonic()
            response = await client.post(
                "/v1",
                json={
                    "cmd": "request.get",
                    "url": "https://example.com",
                    "maxTimeout": 20,
                },
            )
            elapsed = time.monotonic() - started
            diagnostics = await client.get("/diagnostics")

        assert response.status_code == 500
        assert "maxTimeout" in response.json()["message"]
        assert elapsed < 0.5
        assert diagnostics.status_code == 200
        assert diagnostics.json()["capacity_state"] == "recovering"
        assert diagnostics.json()["cleanup"]["by_kind"]["request"] == 1
    finally:
        release_cleanup.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)
        await app.state.pool.close()
        await app.state.cleanup.close()

    assert app.state.cleanup.snapshot().in_flight == 0


@pytest.mark.anyio
async def test_invalid_command_returns_flaresolverr_error_envelope() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/v1", json={"cmd": "nope"})

    await app.state.pool.close()

    assert response.status_code == 500
    assert response.json()["status"] == "error"
    assert "invalid" in response.json()["message"]
    assert response.json()["errorCode"] == "INVALID_REQUEST"
    assert response.json()["retryable"] is False


@pytest.mark.anyio
async def test_destroying_missing_session_has_stable_not_found_code() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={"cmd": "sessions.destroy", "session": "missing"},
        )

    assert response.status_code == 500
    assert response.json()["errorCode"] == "SESSION_NOT_FOUND"
    assert response.json()["retryable"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        (V1ErrorCode.INVALID_REQUEST, 500),
        (V1ErrorCode.SESSION_NOT_FOUND, 500),
        (V1ErrorCode.RESOURCE_LIMIT_EXCEEDED, 500),
        (V1ErrorCode.POOL_UNAVAILABLE, 503),
        (V1ErrorCode.REQUEST_TIMEOUT, 500),
        (V1ErrorCode.NAVIGATION_TIMEOUT, 500),
        (V1ErrorCode.BROWSER_TRANSPORT_CLOSED, 500),
        (V1ErrorCode.CHALLENGE_FAILED, 500),
        (V1ErrorCode.INTERNAL_ERROR, 500),
    ],
)
async def test_v1_maps_every_bounded_domain_error_to_compatible_status(
    monkeypatch: pytest.MonkeyPatch,
    error_code: V1ErrorCode,
    expected_status: int,
) -> None:
    async def fail_dispatch(_service: object, *_args: object, **_kwargs: object) -> None:
        raise CamouflareError(
            "Expected failure.",
            error_code=error_code,
            retryable=error_code is V1ErrorCode.POOL_UNAVAILABLE,
        )

    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    monkeypatch.setattr(type(app.state.command_service), "dispatch", fail_dispatch)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/v1", json={"cmd": "sessions.list"})

    body = response.json()
    assert response.status_code == expected_status
    assert body["status"] == "error"
    assert body["errorCode"] == error_code.value
    assert body["retryable"] is (error_code is V1ErrorCode.POOL_UNAVAILABLE)


@pytest.mark.anyio
async def test_execute_request_turns_solver_failure_into_typed_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial_solution = Solution(
        url="https://example.com/partial",
        status=0,
        cookies=[],
    )

    async def fail_solve(*_args: object, **_kwargs: object) -> V1Response:
        return V1Response(
            status="error",
            message="Navigation timed out.",
            error_code=V1ErrorCode.NAVIGATION_TIMEOUT,
            retryable=True,
            request_outcome_unknown=False,
            solution=partial_solution,
        )

    monkeypatch.setattr("camouflare.commands.solve_request", fail_solve)
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    with pytest.raises(CamouflareError) as raised:
        await execute_request(
            V1Request(cmd="request.get", url="https://example.com"),
            pool=app.state.pool,
            sessions=app.state.sessions,
            settings=app.state.settings,
            captcha_provider=app.state.captcha_provider,
            start_timestamp=1,
            cleanup=app.state.cleanup,
        )

    await app.state.pool.close()
    assert raised.value.error_code is V1ErrorCode.NAVIGATION_TIMEOUT
    assert raised.value.retryable is True
    assert raised.value.solution is partial_solution


@pytest.mark.anyio
async def test_v1_emits_one_safe_structured_completion_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    with caplog.at_level(logging.INFO, logger="camouflare.app"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1",
                json={
                    "cmd": "request.get",
                    "url": "https://user:pass@example.com/path?secret=1",
                },
            )

    await app.state.pool.close()
    completion = [record for record in caplog.records if record.message == "V1 request completed."]
    assert len(completion) == 1
    record = completion[0]
    assert record.command == "request.get"  # type: ignore[attr-defined]
    assert record.result == "ok"  # type: ignore[attr-defined]
    assert record.http_status == response.status_code  # type: ignore[attr-defined]
    assert record.error_code is None  # type: ignore[attr-defined]
    assert record.retryable is None  # type: ignore[attr-defined]
    assert record.request_outcome_unknown is None  # type: ignore[attr-defined]
    assert isinstance(record.duration_ms, int)  # type: ignore[attr-defined]
    assert record.target_host == "example.com"  # type: ignore[attr-defined]
    assert record.fallback_used is False  # type: ignore[attr-defined]
    assert "secret=1" not in record.getMessage()
    assert "user:pass" not in record.getMessage()


@pytest.mark.anyio
async def test_v1_expected_error_has_completion_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    with caplog.at_level(logging.INFO, logger="camouflare.app"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/v1", json={"cmd": "nope"})

    completion = next(
        record for record in caplog.records if record.message == "V1 request completed."
    )
    assert response.json()["errorCode"] == "INVALID_REQUEST"
    assert completion.error_code == "INVALID_REQUEST"  # type: ignore[attr-defined]
    assert completion.exc_info is None
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.anyio
async def test_v1_unexpected_error_logs_traceback_and_internal_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_dispatch(_service: object, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("unexpected command failure")

    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    monkeypatch.setattr(type(app.state.command_service), "dispatch", fail_dispatch)

    with caplog.at_level(logging.INFO, logger="camouflare.app"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/v1", json={"cmd": "sessions.list"})

    body = response.json()
    assert body["errorCode"] == "INTERNAL_ERROR"
    unexpected = next(
        record for record in caplog.records if record.message == "Unexpected /v1 command error."
    )
    assert unexpected.exc_info is not None
    completion = next(
        record for record in caplog.records if record.message == "V1 request completed."
    )
    assert completion.error_code == "INTERNAL_ERROR"  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_v1_exposes_browser_to_direct_get_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransportFailureContext(FakeContext):
        async def new_page(self) -> FakePage:
            page = FakePage(self)
            page.goto_failures["domcontentloaded"] = RuntimeError(
                "Page.goto: Connection closed while reading from the driver"
            )
            self.pages.append(page)
            return page

    class TransportFailureBrowser(FakeBrowser):
        async def new_context(self, **options: Any) -> FakeContext:
            context = TransportFailureContext(self, options)
            self.contexts.append(context)
            self.context_options.append(options)
            return context

    class TransportFailureFactory(FakeBrowserFactory):
        async def __call__(self) -> FakeBrowser:
            browser = TransportFailureBrowser()
            self.created.append(browser)
            return browser

    async def direct_get(url: str, *_args: object) -> object:
        response = SimpleNamespace(
            status=200,
            headers={"content-type": "text/html"},
            raw_body=True,
            url=url,
            fallback_used=False,
        )

        async def text() -> str:
            return "<html><title>Direct</title><body>fallback</body></html>"

        response.text = text
        return response

    monkeypatch.setattr("camouflare.solver._direct_http_get", direct_get)
    app = create_app(browser_factory=TransportFailureFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com/fallback"},
        )

    await app.state.pool.close()
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["fallbackUsed"] is True


@pytest.mark.anyio
async def test_v1_preserves_browser_to_direct_fallback_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransportFailureContext(FakeContext):
        async def new_page(self) -> FakePage:
            page = FakePage(self)
            page.goto_failures["domcontentloaded"] = RuntimeError(
                "Page.goto: Connection closed while reading from the driver"
            )
            self.pages.append(page)
            return page

    class TransportFailureBrowser(FakeBrowser):
        async def new_context(self, **options: Any) -> FakeContext:
            context = TransportFailureContext(self, options)
            self.contexts.append(context)
            self.context_options.append(options)
            return context

    class TransportFailureFactory(FakeBrowserFactory):
        async def __call__(self) -> FakeBrowser:
            browser = TransportFailureBrowser()
            self.created.append(browser)
            return browser

    async def direct_get(url: str, *_args: object) -> object:
        response = SimpleNamespace(
            status=403,
            headers={"content-type": "text/html"},
            raw_body=True,
            url=url,
            fallback_used=False,
        )

        async def text() -> str:
            return "<html><title>Just a moment...</title></html>"

        response.text = text
        return response

    monkeypatch.setattr("camouflare.solver._direct_http_get", direct_get)
    app = create_app(browser_factory=TransportFailureFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com/fallback"},
        )

    await app.state.pool.close()
    body = response.json()
    assert response.status_code == 500
    assert body["status"] == "error"
    assert body["errorCode"] == "CHALLENGE_FAILED"
    assert body["fallbackUsed"] is True


@pytest.mark.anyio
async def test_v1_post_transport_failure_is_not_retried_or_sent_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransportFailureContext(FakeContext):
        async def new_page(self) -> FakePage:
            page = FakePage(self)
            page.goto_failures["domcontentloaded"] = RuntimeError(
                "Page.goto: Connection closed while reading from the driver"
            )
            self.pages.append(page)
            return page

    class TransportFailureBrowser(FakeBrowser):
        async def new_context(self, **options: Any) -> FakeContext:
            context = TransportFailureContext(self, options)
            self.contexts.append(context)
            self.context_options.append(options)
            return context

    class TransportFailureFactory(FakeBrowserFactory):
        async def __call__(self) -> FakeBrowser:
            browser = TransportFailureBrowser()
            self.created.append(browser)
            return browser

    async def forbidden_direct_get(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("POST must not use direct HTTP")

    monkeypatch.setattr("camouflare.solver._direct_http_get", forbidden_direct_get)
    factory = TransportFailureFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.post",
                "url": "https://example.com/orders",
                "postData": "item=1",
            },
        )

    await app.state.pool.close()
    body = response.json()
    assert response.status_code == 500
    assert body["errorCode"] == "BROWSER_TRANSPORT_CLOSED"
    assert body["retryable"] is False
    assert body["requestOutcomeUnknown"] is True
    assert len(factory.created[0].contexts[0].pages[0].goto_calls) == 1


@pytest.mark.anyio
async def test_health_reports_liveness_without_creating_request_browser() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert factory.created == []


@pytest.mark.anyio
async def test_health_does_not_expose_current_browser_pool_state() -> None:
    factory = FakeBrowserFactory()
    settings = Settings(pool_max_browsers=1, pool_max_contexts_per_browser=2)
    app = create_app(settings=settings, browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with (
        app.state.pool.lease_context(),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.get("/health")

    await app.state.pool.close()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(factory.created) == 1
    assert len(factory.created[0].contexts) == 1


@pytest.mark.anyio
async def test_ready_reports_browser_pool_readiness() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready")

    await app.state.pool.close()

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert factory.created[0].context_options == [{"no_viewport": True}]
    assert factory.created[0].contexts[0].pages[0].closed is True


@pytest.mark.anyio
async def test_metrics_scrape_refreshes_idle_max_age_capacity_gauges() -> None:
    app = create_app(
        settings=Settings(
            prometheus_enabled=True,
            browser_max_age_minutes=1,
            pool_min_browsers=1,
            pool_max_browsers=1,
        ),
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )
    await app.state.pool.start()
    app.state.pool._slots[0].created_at -= 61

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/metrics")
    finally:
        await app.state.pool.close()
        await app.state.cleanup.close()

    assert response.status_code == 200
    assert 'camouflare_browsers{state="ready"} 0.0' in response.text
    assert "camouflare_pool_idle_recyclable_slots 1.0" in response.text


@pytest.mark.anyio
async def test_health_stays_ok_when_browser_pool_is_unavailable() -> None:
    async def failing_factory():
        raise RuntimeError("browser unavailable")

    app = create_app(browser_factory=failing_factory, lifespan_enabled=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.anyio
async def test_health_stays_ok_when_pool_snapshot_reader_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    def fail_snapshot() -> object:
        raise RuntimeError("corrupt capacity state")

    monkeypatch.setattr(app.state.pool, "snapshot", fail_snapshot)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_ready_returns_503_when_browser_pool_is_unavailable() -> None:
    async def failing_factory():
        raise RuntimeError("browser unavailable")

    app = create_app(browser_factory=failing_factory, lifespan_enabled=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "error"


@pytest.mark.anyio
async def test_ready_hard_deadline_does_not_wait_for_stuck_probe_cleanup() -> None:
    release_cleanup = asyncio.Event()
    probe_finished = asyncio.Event()

    class StubbornPage(FakePage):
        async def evaluate(self, script: str) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_cleanup.wait()
            finally:
                probe_finished.set()
            return "FakeBrowser/1.0"

    class StubbornContext(FakeContext):
        async def new_page(self) -> StubbornPage:
            page = StubbornPage(self)
            self.pages.append(page)
            return page

    class StubbornPool:
        def __init__(self) -> None:
            self.context = StubbornContext()

        @asynccontextmanager
        async def lease_context(self, **_options: Any):
            yield SimpleNamespace(context=self.context)

    app = create_app(
        settings=Settings(readiness_timeout_ms=20, cleanup_timeout_seconds=1),
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )
    app.state.pool = StubbornPool()

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            started = time.monotonic()
            response = await client.get("/ready")
            elapsed = time.monotonic() - started

        assert response.status_code == 503
        assert response.json()["status"] == "error"
        assert set(response.json()) == {"status", "message"}
        assert elapsed < 0.5
        assert app.state.cleanup.snapshot().by_kind["readiness"] == 1
    finally:
        release_cleanup.set()
        await asyncio.wait_for(probe_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)
        await app.state.cleanup.close()

    assert app.state.cleanup.snapshot().in_flight == 0


def test_context_options_disable_default_viewport_for_camoufox() -> None:
    assert _context_options(None) == {"no_viewport": True}
    assert _context_options({"server": "http://p:1"}) == {
        "no_viewport": True,
        "proxy": {"server": "http://p:1"},
    }


@pytest.mark.anyio
async def test_close_page_ignores_browser_transport_closed_error() -> None:
    class ClosedTransportPage:
        async def close(self) -> None:
            raise RuntimeError("Page.close: Connection closed while reading from the driver")

    await _close_page(ClosedTransportPage())


@pytest.mark.anyio
async def test_close_page_swallows_unexpected_close_error() -> None:
    # Page close is best-effort cleanup after the solution is already collected;
    # even an unexpected close error must not propagate and discard the result.
    class BrokenPage:
        async def close(self) -> None:
            raise RuntimeError("unexpected close failure")

    await _close_page(BrokenPage())


@pytest.mark.anyio
async def test_hanging_page_close_is_bounded_and_remains_tracked() -> None:
    close_started = asyncio.Event()
    finish_close = asyncio.Event()

    class HangingPage:
        async def close(self) -> None:
            close_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish_close.wait()

    cleanup = CleanupSupervisor(timeout_seconds=0.01)
    started = time.monotonic()
    await _close_page(
        HangingPage(),
        cleanup_supervisor=cleanup,
        timeout_seconds=0.01,
    )

    assert close_started.is_set()
    assert time.monotonic() - started < 0.2
    assert cleanup.snapshot().by_kind == {"page": 1}

    finish_close.set()
    for _ in range(20):
        if cleanup.snapshot().in_flight == 0:
            break
        await asyncio.sleep(0)
    assert cleanup.snapshot().in_flight == 0
    await cleanup.close()


@pytest.mark.anyio
async def test_hanging_proxy_close_is_bounded_and_remains_tracked() -> None:
    finish_close = asyncio.Event()

    class HangingProxyLease:
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish_close.wait()

    cleanup = CleanupSupervisor(timeout_seconds=0.01)
    settings = Settings(cleanup_timeout_seconds=0.01)
    started = time.monotonic()
    await _close_proxy_best_effort(
        HangingProxyLease(),
        cleanup=cleanup,
        settings=settings,
    )

    assert time.monotonic() - started < 0.2
    assert cleanup.snapshot().by_kind == {"proxy": 1}

    finish_close.set()
    for _ in range(20):
        if cleanup.snapshot().in_flight == 0:
            break
        await asyncio.sleep(0)
    assert cleanup.snapshot().in_flight == 0
    await cleanup.close()


@pytest.mark.anyio
async def test_v1_request_user_agent_configures_new_context() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "userAgent": "PassoCrawler/1.0",
                "headers": {"User-Agent": "IgnoredBecauseUserAgentFieldWins/1.0"},
            },
        )

    await app.state.pool.close()

    body = response.json()
    assert response.status_code == 200
    assert factory.created[0].context_options[-1]["user_agent"] == "PassoCrawler/1.0"
    assert body["solution"]["userAgent"] == "PassoCrawler/1.0"


@pytest.mark.anyio
async def test_v1_request_user_agent_header_configures_new_context() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "headers": {"user-agent": "BuBiletCrawler/1.0"},
            },
        )

    await app.state.pool.close()

    body = response.json()
    assert response.status_code == 200
    assert factory.created[0].context_options[-1]["user_agent"] == "BuBiletCrawler/1.0"
    assert body["solution"]["userAgent"] == "BuBiletCrawler/1.0"


def test_app_factory_allows_lifespan_override_for_tests() -> None:
    @asynccontextmanager
    async def lifespan(_: Any):
        yield

    app = create_app(browser_factory=FakeBrowserFactory(), lifespan=lifespan)

    assert app.title == "Camouflare"
    assert app.router.lifespan_context is lifespan


@pytest.mark.anyio
async def test_request_with_malformed_proxy_is_rejected() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "proxy": {"username": "u", "password": "p"},  # no server/url
            },
        )

    await app.state.pool.close()

    assert response.status_code == 500
    assert "proxy" in response.json()["message"].lower()


@pytest.mark.anyio
async def test_request_level_proxy_is_applied_to_new_context() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "proxy": {
                    "url": "http://proxy.example:8080",
                    "username": "user",
                    "password": "pass",
                },
            },
        )

    await app.state.pool.close()

    assert response.status_code == 200
    assert factory.created[0].context_options[-1]["proxy"] == {
        "server": "http://proxy.example:8080",
        "username": "user",
        "password": "pass",
    }


@pytest.mark.anyio
async def test_authenticated_socks5h_proxy_url_uses_local_authless_bridge() -> None:
    factory = FakeBrowserFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "proxy": {"url": "socks5h://user:pass@185.184.26.78:1080"},
            },
        )

    await app.state.pool.close()

    assert response.status_code == 200
    proxy = factory.created[0].context_options[-1]["proxy"]
    assert set(proxy) == {"server"}
    assert proxy["server"].startswith("socks5://127.0.0.1:")


@pytest.mark.anyio
async def test_unknown_cmd_does_not_create_unbounded_metric_label() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    bogus = "totally-unknown-command-xyz"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post("/v1", json={"cmd": bogus})

    await app.state.pool.close()

    labels = {
        sample.labels.get("command")
        for metric in REQUEST_COUNTER.collect()
        for sample in metric.samples
    }
    assert bogus not in labels
    assert "invalid" in labels


@pytest.mark.anyio
async def test_expired_session_rotation_preserves_proxy() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()
    pool = app.state.pool
    sessions = app.state.sessions
    settings = app.state.settings

    persistent = await pool.create_persistent_context()
    original = sessions.register_existing(
        "abc",
        persistent.context,
        proxy={"server": "http://p:1"},
        on_close=persistent.close,
        ttl_seconds=0,
    )
    request = V1Request(cmd="request.get", url="https://example.com", session="abc")

    rotated = await _session_for_request(request, pool=pool, sessions=sessions, settings=settings)

    assert rotated is not original
    assert rotated.proxy == {"server": "http://p:1"}

    await app.state.sessions.close()
    await pool.close()


@pytest.mark.anyio
async def test_expired_but_locked_session_is_not_rotated() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()
    pool = app.state.pool
    sessions = app.state.sessions
    settings = app.state.settings

    persistent = await pool.create_persistent_context()
    session = sessions.register_existing(
        "abc", persistent.context, on_close=persistent.close, ttl_seconds=0
    )
    request = V1Request(cmd="request.get", url="https://example.com", session="abc")

    async with session.lock:  # an in-flight request holds the session
        result = await _session_for_request(
            request, pool=pool, sessions=sessions, settings=settings
        )

    assert result is session
    assert session.context.closed is False

    await app.state.sessions.close()
    await pool.close()


@pytest.mark.anyio
async def test_session_request_recreates_context_after_expiry() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post("/v1", json={"cmd": "sessions.create", "session": "abc"})
        first = app.state.sessions.get("abc")
        first.created_at -= 10_000  # force the session past its ttl
        response = await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com", "session": "abc"},
        )
        second = app.state.sessions.get("abc")

    assert response.status_code == 200
    assert second is not None
    assert second is not first
    assert first.context.closed is True
    assert second.context.closed is False

    await app.state.sessions.close()
    await app.state.pool.close()


@pytest.mark.anyio
async def test_session_request_stores_custom_ttl_for_pruning() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "session": "abc",
                "session_ttl_minutes": 240,
            },
        )

    session = app.state.sessions.get("abc")
    assert session is not None
    assert session.ttl_seconds == 240 * 60

    await app.state.sessions.close()
    await app.state.pool.close()


@pytest.mark.anyio
async def test_request_post_requires_url() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/v1", json={"cmd": "request.post", "postData": "a=b"})

    await app.state.pool.close()

    assert response.status_code == 500
    assert "url" in response.json()["message"].lower()
    assert response.json()["errorCode"] == "INVALID_REQUEST"


@pytest.mark.anyio
async def test_non_positive_max_timeout_returns_concise_error() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com", "maxTimeout": 0},
        )

    await app.state.pool.close()

    body = response.json()
    assert response.status_code == 500
    assert body["status"] == "error"
    # Concise message: no pydantic docs URL / version dump leaked to the caller.
    assert "errors.pydantic.dev" not in body["message"]
    assert "maxTimeout" in body["message"] or "max_timeout" in body["message"]


@pytest.mark.anyio
async def test_sessions_create_reports_request_start_before_end() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/v1", json={"cmd": "sessions.create", "session": "abc"})

    await app.state.sessions.close()
    await app.state.pool.close()

    body = response.json()
    assert body["startTimestamp"] <= body["endTimestamp"]


@pytest.mark.anyio
async def test_persistent_capacity_exhaustion_returns_503_and_keeps_stateless_serving() -> None:
    factory = FakeBrowserFactory()
    # capacity = 1 browser * 2 contexts = 2; reserve 1 for transient -> 1 session max.
    settings = Settings(
        pool_max_browsers=1,
        pool_max_contexts_per_browser=2,
        pool_acquire_timeout_ms=50,
    )
    app = create_app(settings=settings, browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post("/v1", json={"cmd": "sessions.create", "session": "a"})
        second = await client.post("/v1", json={"cmd": "sessions.create", "session": "b"})
        # The reserved transient slot keeps stateless requests and health serving.
        stateless = await client.post(
            "/v1", json={"cmd": "request.get", "url": "https://example.com"}
        )
        health = await client.get("/health")

    await app.state.sessions.close()
    await app.state.pool.close()

    assert first.status_code == 200
    assert second.status_code == 503
    assert second.json()["status"] == "error"
    assert stateless.status_code == 200
    assert health.status_code == 200


@pytest.mark.anyio
async def test_no_session_context_close_failure_still_returns_solution() -> None:
    class FailCloseContext(FakeContext):
        def __init__(self, browser: Any = None, options: Any = None) -> None:
            super().__init__(browser, options)
            self.fail_close = True

    class FailCloseBrowser(FakeBrowser):
        async def new_context(self, **options: Any) -> FailCloseContext:
            context = FailCloseContext(self, options)
            self.contexts.append(context)
            self.context_options.append(options)
            return context

    class FailCloseFactory(FakeBrowserFactory):
        async def __call__(self) -> FailCloseBrowser:
            browser = FailCloseBrowser()
            self.created.append(browser)
            return browser

    factory = FailCloseFactory()
    app = create_app(browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1", json={"cmd": "request.get", "url": "https://example.com"}
        )
        replacement_response = await client.post(
            "/v1", json={"cmd": "request.get", "url": "https://example.com/next"}
        )

    await app.state.pool.close()

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["solution"]["url"] == "https://example.com"
    assert replacement_response.status_code == 200
    assert len(factory.created) == 2
    assert factory.created[0].closed is True


@pytest.mark.anyio
async def test_dispatch_prune_preserves_targeted_session_proxy_on_rotation() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1",
            json={"cmd": "sessions.create", "session": "abc", "proxy": {"url": "http://p:1"}},
        )
        first = app.state.sessions.get("abc")
        assert first is not None
        first.created_at -= 10_000  # force the session past its ttl

        response = await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com", "session": "abc"},
        )
        rotated = app.state.sessions.get("abc")

    assert response.status_code == 200
    assert rotated is not None
    assert rotated is not first
    # The dispatch-time prune must not drop the session's stored proxy (IP leak).
    assert rotated.proxy == {"server": "http://p:1"}

    await app.state.sessions.close()
    await app.state.pool.close()


@pytest.mark.anyio
async def test_session_rotation_preserves_custom_ttl() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1",
            json={"cmd": "sessions.create", "session": "abc", "session_ttl_minutes": 240},
        )
        first = app.state.sessions.get("abc")
        assert first is not None and first.ttl_seconds == 240 * 60
        first.created_at -= 240 * 60 + 10  # expire past its own 240-minute ttl

        await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com", "session": "abc"},
        )
        second = app.state.sessions.get("abc")

    assert second is not None
    assert second is not first
    # A rotation that does not restate the ttl must keep the session's custom ttl.
    assert second.ttl_seconds == 240 * 60

    await app.state.sessions.close()
    await app.state.pool.close()


@pytest.mark.anyio
async def test_concurrent_sessions_create_same_id_never_returns_503() -> None:
    settings = Settings(pool_max_browsers=1, pool_max_contexts_per_browser=2)
    app = create_app(
        settings=settings, browser_factory=FakeBrowserFactory(), lifespan_enabled=False
    )
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first, second = await asyncio.gather(
            client.post("/v1", json={"cmd": "sessions.create", "session": "abc"}),
            client.post("/v1", json={"cmd": "sessions.create", "session": "abc"}),
        )
        session_ids = app.state.sessions.list_ids()

    await app.state.sessions.close()
    await app.state.pool.close()

    # A same-id race must resolve idempotently, never a spurious capacity 503.
    assert first.status_code == 200
    assert second.status_code == 200
    assert session_ids == ["abc"]


class _ChallengePage(FakePage):
    def __init__(self, context: FakeContext) -> None:
        super().__init__(context)
        self.solved = False

    async def title(self) -> str:
        return "Example" if self.solved else "Just a moment..."

    async def content(self) -> str:
        if self.solved:
            return "<html><title>Example</title><body>ok</body></html>"
        return (
            "<html><title>Just a moment...</title>"
            '<script src="/cdn-cgi/challenge-platform/x"></script></html>'
        )


class _ChallengeContext(FakeContext):
    async def new_page(self) -> _ChallengePage:
        page = _ChallengePage(self)
        self.pages.append(page)
        return page


class _ChallengeBrowser(FakeBrowser):
    async def new_context(self, **options: Any) -> _ChallengeContext:
        self.context_options.append(options)
        context = _ChallengeContext(self, options)
        self.contexts.append(context)
        return context


class _ChallengeBrowserFactory:
    def __init__(self) -> None:
        self.created: list[_ChallengeBrowser] = []

    async def __call__(self) -> _ChallengeBrowser:
        browser = _ChallengeBrowser()
        self.created.append(browser)
        return browser


class _RecordingProvider:
    def __init__(self) -> None:
        self.prepared = 0
        self.solve_calls = 0

    @asynccontextmanager
    async def prepare(self, *, page: Any):  # type: ignore[no-untyped-def]
        self.prepared += 1
        yield

    async def solve(self, *, page: Any, request: Any, timer: Any) -> str | None:
        self.solve_calls += 1
        page.solved = True
        return None


@pytest.mark.anyio
async def test_fake_browser_factory_defaults_to_no_captcha_provider() -> None:
    from camouflare.captcha import NoCaptchaProvider

    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    assert isinstance(app.state.captcha_provider, NoCaptchaProvider)


@pytest.mark.anyio
async def test_real_factory_defaults_to_no_captcha_provider() -> None:
    from camouflare.captcha import NoCaptchaProvider

    app = create_app(lifespan_enabled=False)

    assert isinstance(app.state.captcha_provider, NoCaptchaProvider)


@pytest.mark.anyio
async def test_real_factory_uses_click_solver_provider_when_enabled() -> None:
    from camouflare.captcha import ClickSolverProvider

    app = create_app(settings=Settings(challenge_solver="click"), lifespan_enabled=False)

    assert isinstance(app.state.captcha_provider, ClickSolverProvider)


@pytest.mark.anyio
async def test_injected_provider_is_wired_through_to_solve_request() -> None:
    provider = _RecordingProvider()
    app = create_app(
        browser_factory=_ChallengeBrowserFactory(),
        captcha_provider=provider,
        lifespan_enabled=False,
    )
    assert app.state.captcha_provider is provider
    await app.state.pool.start()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com", "maxTimeout": 60000},
        )

    await app.state.pool.close()

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["message"] == "Challenge solved!"
    assert provider.prepared == 1
    assert provider.solve_calls == 1


@pytest.mark.anyio
async def test_limited_json_reader_accepts_boundary_and_rejects_chunked_overflow() -> None:
    body = b'{"cmd":"sessions.list"}'
    request = _streaming_request(body, content_length=1)

    payload = await _read_json_payload(request, maximum_bytes=len(body))

    assert payload == {"cmd": "sessions.list"}
    assert request.state.request_body_bytes == len(body)

    without_length = _streaming_request(body)
    assert await _read_json_payload(without_length, maximum_bytes=len(body)) == {
        "cmd": "sessions.list"
    }

    oversized = _streaming_request(body + b" ", content_length=1)
    with pytest.raises(ResourceLimitError, match="Request body"):
        await _read_json_payload(oversized, maximum_bytes=len(body))


@pytest.mark.anyio
async def test_v1_rejects_request_body_limit_with_flaresolverr_envelope() -> None:
    settings = Settings(max_request_body_bytes=64)
    app = create_app(
        settings=settings,
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )
    await app.state.pool.start()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1",
            json={"cmd": "request.post", "url": "https://example.com", "postData": "x" * 128},
        )

    await app.state.pool.close()

    assert response.status_code == 500
    assert response.json()["status"] == "error"
    assert "Request body" in response.json()["message"]
    assert response.json()["errorCode"] == "RESOURCE_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_v1_rejects_timeout_and_ttl_above_configured_ceilings() -> None:
    settings = Settings(
        max_timeout_ms=1000,
        session_ttl_minutes=10,
        max_session_ttl_minutes=10,
    )
    app = create_app(
        settings=settings,
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )
    await app.state.pool.start()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        timeout_boundary = await client.post(
            "/v1",
            json={"cmd": "sessions.list", "maxTimeout": 1000},
        )
        timeout_response = await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com", "maxTimeout": 1001},
        )
        ttl_response = await client.post(
            "/v1",
            json={
                "cmd": "sessions.create",
                "session": "limited",
                "session_ttl_minutes": 11,
                "maxTimeout": 1000,
            },
        )
        ttl_boundary = await client.post(
            "/v1",
            json={
                "cmd": "sessions.create",
                "session": "boundary",
                "session_ttl_minutes": 10,
                "maxTimeout": 1000,
            },
        )
        await client.post(
            "/v1",
            json={"cmd": "sessions.destroy", "session": "boundary", "maxTimeout": 1000},
        )

    await app.state.pool.close()

    assert timeout_boundary.status_code == 200
    assert timeout_response.status_code == 500
    assert "maxTimeout" in timeout_response.json()["message"]
    assert ttl_response.status_code == 500
    assert "session_ttl_minutes" in ttl_response.json()["message"]
    assert ttl_boundary.status_code == 200


@pytest.mark.anyio
async def test_response_limit_error_closes_transient_context() -> None:
    factory = FakeBrowserFactory()
    settings = Settings(max_response_body_bytes=8)
    app = create_app(settings=settings, browser_factory=factory, lifespan_enabled=False)
    await app.state.pool.start()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1",
            json={"cmd": "request.get", "url": "https://example.com"},
        )

    await app.state.pool.close()

    assert response.status_code == 500
    assert "Response body" in response.json()["message"]
    assert factory.created[0].contexts[0].closed is True
    assert factory.created[0].contexts[0].pages[0].closed is True


@pytest.mark.anyio
async def test_screenshot_limit_error_closes_page_and_returns_no_partial_solution() -> None:
    factory = FakeBrowserFactory()
    app = create_app(
        settings=Settings(max_screenshot_bytes=1),
        browser_factory=factory,
        lifespan_enabled=False,
    )
    await app.state.pool.start()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": "https://example.com",
                "returnScreenshot": True,
            },
        )

    assert response.status_code == 500
    assert response.json()["status"] == "error"
    assert "solution" not in response.json()
    assert factory.created[0].contexts[0].pages[0].closed is True
    assert factory.created[0].contexts[0].closed is True
    assert app.state.pool.snapshot().active_contexts == 0
    await app.state.pool.close()


@pytest.mark.anyio
async def test_solution_limit_accepts_exact_boundary_and_rejects_plus_one() -> None:
    async def issue(maximum: int):
        app = create_app(
            settings=Settings(max_solution_bytes=maximum),
            browser_factory=FakeBrowserFactory(),
            lifespan_enabled=False,
        )
        await app.state.pool.start()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                return await client.post(
                    "/v1",
                    json={"cmd": "request.get", "url": "https://example.com"},
                )
        finally:
            await app.state.pool.close()

    baseline = await issue(1_000_000)
    exact_size = len(baseline.content)
    accepted = await issue(exact_size)
    rejected = await issue(exact_size - 1)

    assert baseline.status_code == accepted.status_code == 200
    assert len(accepted.content) == exact_size
    assert rejected.status_code == 500
    assert "Solution payload" in rejected.json()["message"]


@pytest.mark.anyio
async def test_session_reaper_skips_in_use_then_closes_idle_expired_session() -> None:
    context = FakeContext()
    manager = SessionManager(max_sessions=2, default_ttl_seconds=0)
    session = manager.register_existing("expired", context)
    session.in_use = 1
    reaper = asyncio.create_task(_session_reaper(manager, interval_seconds=0))

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert context.closed is False

    session.in_use = 0
    for _ in range(5):
        await asyncio.sleep(0)
        if context.closed:
            break
    reaper.cancel()
    await asyncio.gather(reaper, return_exceptions=True)

    assert context.closed is True


@pytest.mark.anyio
async def test_request_id_is_validated_and_echoed_on_success() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        supplied = await client.get("/health", headers={"X-Request-ID": "caller-request-42"})
        generated = await client.get("/health", headers={"X-Request-ID": "x" * 129})

    assert supplied.headers["X-Request-ID"] == "caller-request-42"
    assert UUID(generated.headers["X-Request-ID"]).version == 4


@pytest.mark.anyio
async def test_request_id_is_returned_on_authentication_failure() -> None:
    app = create_app(
        settings=Settings(camouflare_api_token="secret"),
        browser_factory=FakeBrowserFactory(),
        lifespan_enabled=False,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready", headers={"X-Request-ID": "unauthorized-42"})

    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "unauthorized-42"


@pytest.mark.anyio
async def test_request_id_is_returned_on_unhandled_endpoint_error() -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)

    @app.get("/boom", include_in_schema=False)
    async def boom() -> None:
        raise RuntimeError("boom")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/boom", headers={"X-Request-ID": "failed-request-42"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "failed-request-42"
    assert response.json() == {"detail": "Internal Server Error"}
