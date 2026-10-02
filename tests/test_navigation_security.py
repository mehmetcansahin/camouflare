from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from email.message import Message
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import camouflare.navigation as navigation
from camouflare.app import create_app
from camouflare.limits import ResourceLimits
from camouflare.models import V1Request
from camouflare.observability import JsonLogFormatter
from camouflare.timer import TimeoutTimer
from tests.fakes import FakeBrowserFactory, FakeContext


class _HttpResponse:
    def __init__(
        self,
        *,
        url: str,
        status: int,
        headers: list[tuple[str, str]] | None = None,
        body: bytes = b"ok",
    ) -> None:
        self._url = url
        self.status = status
        self.headers = Message()
        for name, value in headers or []:
            self.headers.add_header(name, value)
        self._body = body

    def __enter__(self) -> _HttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def info(self) -> Message:
        return self.headers

    def geturl(self) -> str:
        return self._url

    def read(self, size: int | None = None) -> bytes:
        return self._body if size is None else self._body[:size]


def test_direct_http_cross_origin_redirect_strips_all_caller_headers_and_scopes_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_headers: list[dict[str, str]] = []
    timeouts: list[float] = []
    responses = iter(
        [
            _HttpResponse(
                url="https://origin.test/start",
                status=302,
                headers=[
                    ("Location", "https://destination.test/final"),
                    ("Set-Cookie", "origin_only=1; Path=/; HttpOnly"),
                ],
            ),
            _HttpResponse(
                url="https://destination.test/final",
                status=200,
                headers=[
                    (
                        "Set-Cookie",
                        "destination_only=2; Path=/; Secure; HttpOnly; SameSite=Lax",
                    )
                ],
            ),
        ]
    )

    def fake_open(request: Any, timeout: float) -> _HttpResponse:
        opened_headers.append({name.lower(): value for name, value in request.header_items()})
        timeouts.append(timeout)
        if len(timeouts) == 1:
            time.sleep(0.01)
        return next(responses)

    monkeypatch.setattr(navigation._HTTP_OPENER, "open", fake_open)
    response = navigation._direct_http_get_sync(
        "https://origin.test/start",
        V1Request(
            cmd="request.get",
            url="https://origin.test/start",
            headers={
                "Authorization": "Bearer secret",
                "Cookie": "manual=secret",
                "Host": "origin.test",
                "Proxy-Authorization": "Basic secret",
                "X-API-Key": "api-secret",
                "X-Trace": "caller-trace",
                "Referer": "https://private.test/account",
                "Accept": "application/private",
                "User-Agent": "BrowserIdentity/1.0",
            },
        ),
        1,
    )

    assert opened_headers[0]["authorization"] == "Bearer secret"
    assert opened_headers[0]["cookie"] == "manual=secret"
    assert "authorization" not in opened_headers[1]
    assert "proxy-authorization" not in opened_headers[1]
    assert "host" not in opened_headers[1]
    assert "cookie" not in opened_headers[1]
    assert "x-api-key" not in opened_headers[1]
    assert "x-trace" not in opened_headers[1]
    assert "referer" not in opened_headers[1]
    assert "accept" not in opened_headers[1]
    assert opened_headers[1]["user-agent"] == "BrowserIdentity/1.0"
    assert timeouts[1] < timeouts[0]
    assert {cookie["name"] for cookie in response.cookies} == {
        "origin_only",
        "destination_only",
    }
    destination_cookie = next(
        cookie for cookie in response.cookies if cookie["name"] == "destination_only"
    )
    assert destination_cookie["secure"] is True
    assert destination_cookie["httpOnly"] is True
    assert destination_cookie["sameSite"] == "Lax"


def test_direct_http_rejects_public_suffix_cookie_across_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_headers: list[dict[str, str]] = []
    responses = iter(
        [
            _HttpResponse(
                url="https://attacker.co.uk/start",
                status=302,
                headers=[
                    ("Location", "https://victim.co.uk/final"),
                    ("Set-Cookie", "supercookie=secret; Domain=co.uk; Path=/"),
                ],
            ),
            _HttpResponse(url="https://victim.co.uk/final", status=200),
        ]
    )

    def fake_open(request: Any, timeout: float) -> _HttpResponse:
        opened_headers.append({name.lower(): value for name, value in request.header_items()})
        return next(responses)

    monkeypatch.setattr(navigation._HTTP_OPENER, "open", fake_open)
    response = navigation._direct_http_get_sync(
        "https://attacker.co.uk/start",
        V1Request(cmd="request.get", url="https://attacker.co.uk/start"),
        1,
    )

    assert "cookie" not in opened_headers[1]
    assert all(cookie["name"] != "supercookie" for cookie in response.cookies)


@pytest.mark.anyio
async def test_origin_bound_get_uses_direct_transport_without_browser_navigation() -> None:
    calls: list[tuple[str, V1Request]] = []
    request = V1Request(
        cmd="request.get",
        url="https://origin.test/start",
        headers={
            "Authorization": "Bearer secret",
            "X-API-Key": "api-secret",
            "Referrer": "https://private.test/account",
            "User-Agent": "BrowserIdentity/1.0",
        },
    )

    async def direct_get(
        url: str,
        direct_request: V1Request,
        timer: TimeoutTimer,
    ) -> navigation.RawResponse:
        calls.append((url, direct_request))
        return navigation.RawResponse(
            url="https://destination.test/final",
            status=200,
            headers={"content-type": "text/html"},
            body="<html><title>Just a moment...</title></html>",
        )

    context = FakeContext()
    page = await context.new_page()
    response = await navigation.navigate_get(
        page,
        request,
        TimeoutTimer(1000),
        ResourceLimits(),
        direct_http_get=direct_get,
    )

    assert response is not None
    assert response.url == "https://destination.test/final"
    assert calls == [("https://origin.test/start", request)]
    assert page.goto_calls == []


@pytest.mark.anyio
async def test_origin_bound_get_rejects_session_or_proxy_browser_path() -> None:
    context = FakeContext()
    page = await context.new_page()
    request = V1Request(
        cmd="request.get",
        url="https://origin.test/start",
        headers={"Authorization": "Bearer secret"},
    )

    with pytest.raises(navigation.CamouflareError, match="stateless request without a proxy"):
        await navigation.navigate_get(
            page,
            request,
            TimeoutTimer(1000),
            ResourceLimits(),
            allow_direct_http_first=False,
        )

    assert page.goto_calls == []


@pytest.mark.anyio
async def test_buffered_header_get_and_post_reject_browser_screenshots() -> None:
    context = FakeContext()
    page = await context.new_page()

    with pytest.raises(navigation.CamouflareError, match="returnScreenshot"):
        await navigation.navigate_get(
            page,
            V1Request(
                cmd="request.get",
                url="https://origin.test/start",
                headers={"X-API-Key": "secret"},
                returnScreenshot=True,
            ),
            TimeoutTimer(1000),
            ResourceLimits(),
        )

    with pytest.raises(navigation.CamouflareError, match="returnScreenshot"):
        await navigation.submit_post(
            context,
            page,
            V1Request(
                cmd="request.post",
                url="https://origin.test/start",
                postData="item=1",
                returnScreenshot=True,
            ),
            TimeoutTimer(1000),
            ResourceLimits(),
        )

    assert page.goto_calls == []


@pytest.mark.anyio
async def test_submit_post_prefers_context_request_and_disables_automatic_redirects() -> None:
    class Response:
        status = 302
        url = "https://origin.test/login"

        def __init__(self) -> None:
            self.headers = {"location": "https://destination.test/collect"}

        async def body(self) -> bytes:
            return b""

        async def dispose(self) -> None:
            return None

    class ApiRequest:
        def __init__(self) -> None:
            self.call: dict[str, Any] | None = None

        async def post(self, url: str, **kwargs: Any) -> Response:
            self.call = {"url": url, **kwargs}
            return Response()

    context = FakeContext()
    api_request = ApiRequest()
    context.request = api_request  # type: ignore[attr-defined]
    page = await context.new_page()
    response = await navigation.submit_post(
        context,
        page,
        V1Request(
            cmd="request.post",
            url="https://origin.test/login",
            postData="username=alice",
            headers={"X-API-Key": "secret", "Referer": "https://private.test/"},
        ),
        TimeoutTimer(1000),
        ResourceLimits(),
    )

    assert response is not None
    assert response.status == 302
    assert api_request.call is not None
    assert api_request.call["max_redirects"] == 0
    assert page.goto_calls == []


@pytest.mark.parametrize(
    ("target", "expected_cookie_header"),
    [
        ("https://example.com/admin/x", "host_only=1; domain_cookie=2"),
        ("https://sub.example.com/admin/x", "domain_cookie=2"),
        ("http://example.com/admin/x", None),
        ("https://example.com/administrator", None),
    ],
)
def test_direct_http_cookie_header_respects_host_path_secure_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    expected_cookie_header: str | None,
) -> None:
    cookies = [
        {
            "name": "host_only",
            "value": "1",
            "url": "https://example.com/admin/page",
            "path": "/admin",
            "secure": True,
        },
        {
            "name": "domain_cookie",
            "value": "2",
            "domain": ".example.com",
            "path": "/admin",
            "secure": True,
        },
        {
            "name": "expired",
            "value": "3",
            "domain": ".example.com",
            "path": "/admin",
            "expires": time.time() - 60,
        },
    ]
    opened_headers: list[dict[str, str]] = []

    def fake_open(request: Any, timeout: float) -> _HttpResponse:
        opened_headers.append({name.lower(): value for name, value in request.header_items()})
        return _HttpResponse(url=target, status=200)

    monkeypatch.setattr(navigation._HTTP_OPENER, "open", fake_open)
    navigation._direct_http_get_sync(
        target,
        V1Request(cmd="request.get", url=target, cookies=cookies),
        1,
    )

    assert opened_headers[0].get("cookie") == expected_cookie_header


@pytest.mark.anyio
async def test_direct_response_cookies_are_imported_into_browser_context() -> None:
    response = navigation.RawResponse(
        url="https://example.com/data?ajax=true",
        status=200,
        headers={},
        body="ok",
        cookies=[
            {
                "name": "direct",
                "value": "cookie",
                "domain": "example.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }
        ],
    )

    async def direct_get(
        url: str,
        request: V1Request,
        timer: TimeoutTimer,
    ) -> navigation.RawResponse:
        return response

    context = FakeContext()
    page = await context.new_page()
    result = await navigation.navigate_get(
        page,
        V1Request(cmd="request.get", url="https://example.com/data?ajax=true"),
        TimeoutTimer(1000),
        ResourceLimits(),
        direct_http_get=direct_get,
    )

    assert result is response
    assert context.cookies_added == response.cookies
    assert page.goto_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "direct_candidate",
    [
        navigation.try_direct_http_get_first,
        navigation.try_direct_http_get_after_navigation_timeout,
    ],
)
async def test_direct_ajax_candidates_reject_non_2xx_responses(
    direct_candidate: Any,
) -> None:
    response = navigation.RawResponse(
        url="https://example.com/data?ajax=true",
        status=500,
        headers={"content-type": "text/plain"},
        body="ordinary upstream error",
        cookies=[
            {
                "name": "must_not_import",
                "value": "1",
                "domain": "example.com",
                "path": "/",
            }
        ],
    )

    async def direct_get(
        url: str,
        request: V1Request,
        timer: TimeoutTimer,
    ) -> navigation.RawResponse:
        return response

    context = FakeContext()
    page = await context.new_page()
    result = await direct_candidate(
        response.url,
        V1Request(cmd="request.get", url=response.url),
        TimeoutTimer(1000),
        page=page,
        direct_http_get=direct_get,
    )

    assert result is None
    assert context.cookies_added == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, "forbidden"),
        (200, "<html><title>Just a moment...</title></html>"),
    ],
)
async def test_ajax_preflight_fallback_json_log_identifies_only_target_origin(
    caplog: pytest.LogCaptureFixture,
    status: int,
    body: str,
) -> None:
    target = (
        "https://log-user:log-pass@example.com:8443/reset/one-time-path-token/confirm"
        "?ajax=true&sig=query-secret#fragment-secret"
    )

    async def direct_get(
        url: str,
        request: V1Request,
        timer: TimeoutTimer,
    ) -> navigation.RawResponse:
        return navigation.RawResponse(url=url, status=status, headers={}, body=body)

    context = FakeContext()
    page = await context.new_page()
    with caplog.at_level(logging.INFO, logger="camouflare.navigation"):
        await navigation.navigate_get(
            page,
            V1Request(cmd="request.get", url=target),
            TimeoutTimer(1000),
            ResourceLimits(),
            direct_http_get=direct_get,
        )

    assert len(page.goto_calls) == 1
    preflight = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Direct HTTP GET preflight returned")
    ]
    assert len(preflight) == 1
    emitted = JsonLogFormatter().format(preflight[0])
    for secret in (
        "/reset",
        "one-time-path-token",
        "query-secret",
        "fragment-secret",
        "log-user",
        "log-pass",
    ):
        assert secret not in emitted
    assert json.loads(emitted)["fields"]["target"] == "https://example.com:8443"


@pytest.mark.anyio
async def test_direct_http_worker_returns_at_deadline_even_if_thread_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_get(*args: Any, **kwargs: Any) -> navigation.RawResponse:
        time.sleep(0.05)
        return navigation.RawResponse(url="https://example.com", status=200, headers={}, body="ok")

    monkeypatch.setattr(navigation, "_direct_http_get_sync", slow_get)
    executor = navigation.DirectHttpExecutor()
    started = time.monotonic()
    with (
        navigation.active_direct_http_executor(executor),
        pytest.raises(TimeoutError, match="deadline"),
    ):
        await navigation._direct_http_get(
            "https://example.com",
            V1Request(cmd="request.get", url="https://example.com"),
            TimeoutTimer(5),
        )
    assert time.monotonic() - started < 0.04
    await asyncio.sleep(0.06)
    await executor.close()


@pytest.mark.anyio
async def test_direct_http_worker_concurrency_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def slow_get(*args: Any, **kwargs: Any) -> navigation.RawResponse:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.02)
            return navigation.RawResponse(
                url="https://example.com", status=200, headers={}, body="ok"
            )
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(navigation, "_direct_http_get_sync", slow_get)
    request = V1Request(cmd="request.get", url="https://example.com")
    executor = navigation.DirectHttpExecutor()
    with navigation.active_direct_http_executor(executor):
        await asyncio.gather(
            *(
                navigation._direct_http_get(
                    "https://example.com",
                    request,
                    TimeoutTimer(1000),
                )
                for _ in range(navigation.DIRECT_HTTP_MAX_WORKERS * 2)
            )
        )
    await executor.close()

    assert maximum_active <= navigation.DIRECT_HTTP_MAX_WORKERS


@pytest.mark.anyio
async def test_app_command_uses_its_runtime_owned_direct_http_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def direct_get(*args: Any, **kwargs: Any) -> navigation.RawResponse:
        return navigation.RawResponse(
            url="https://example.com/data?ajax=true",
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body="<html><title>Direct</title><body>owned worker</body></html>",
        )

    monkeypatch.setattr(navigation, "_direct_http_get_sync", direct_get)
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    await app.state.pool.start()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.post(
                "/v1",
                json={
                    "cmd": "request.get",
                    "url": "https://example.com/data?ajax=true",
                },
            )
    finally:
        await app.state.sessions.close()
        await app.state.pool.close()
        await app.state.direct_http.close()

    assert response.status_code == 200
    assert "owned worker" in response.json()["solution"]["response"]


def test_direct_http_keeps_prior_secure_cookie_across_https_to_http_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_headers: list[dict[str, str]] = []
    responses = iter(
        [
            _HttpResponse(
                url="https://origin.test/start",
                status=302,
                headers=[
                    ("Location", "http://origin.test/final"),
                    ("Set-Cookie", "keep=1; Secure; Path=/"),
                ],
            ),
            _HttpResponse(url="http://origin.test/final", status=200),
        ]
    )

    def fake_open(request: Any, timeout: float) -> _HttpResponse:
        opened_headers.append({name.lower(): value for name, value in request.header_items()})
        return next(responses)

    monkeypatch.setattr(navigation._HTTP_OPENER, "open", fake_open)
    response = navigation._direct_http_get_sync(
        "https://origin.test/start",
        V1Request(cmd="request.get", url="https://origin.test/start"),
        1,
    )

    assert "cookie" not in opened_headers[1]
    assert [cookie["name"] for cookie in response.solution_cookies] == ["keep"]
    assert response.solution_cookies[0]["domain"] == "origin.test"
    assert response.solution_cookies[0]["path"] == "/"


def test_direct_http_rejects_case_insensitive_unsafe_cookie_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        navigation._HTTP_OPENER,
        "open",
        lambda request, timeout: _HttpResponse(
            url="http://origin.test/",
            status=200,
            headers=[
                ("Set-Cookie", "partitioned_cookie=1; partitioned; Secure; Path=/"),
                ("Set-Cookie", "none_cookie=1; SameSite=none; Path=/"),
            ],
        ),
    )

    response = navigation._direct_http_get_sync(
        "http://origin.test/",
        V1Request(cmd="request.get", url="http://origin.test/"),
        1,
    )

    assert response.cookies == []
    assert response.solution_cookies == []


def test_direct_http_rejects_domain_cookie_from_ip_literal_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_headers: list[dict[str, str]] = []
    responses = iter(
        [
            _HttpResponse(
                url="http://127.0.0.1/start",
                status=302,
                headers=[
                    ("Location", "http://10.0.0.1/final"),
                    ("Set-Cookie", "partial_ip=secret; Domain=0.0.1; Path=/"),
                ],
            ),
            _HttpResponse(url="http://10.0.0.1/final", status=200),
        ]
    )

    def fake_open(request: Any, timeout: float) -> _HttpResponse:
        opened_headers.append({name.lower(): value for name, value in request.header_items()})
        return next(responses)

    monkeypatch.setattr(navigation._HTTP_OPENER, "open", fake_open)
    response = navigation._direct_http_get_sync(
        "http://127.0.0.1/start",
        V1Request(
            cmd="request.get",
            url="http://127.0.0.1/start",
            cookies=[
                {
                    "name": "caller_partial_ip",
                    "value": "secret",
                    "domain": "0.0.1",
                    "path": "/",
                }
            ],
        ),
        1,
    )

    assert "cookie" not in opened_headers[0]
    assert "cookie" not in opened_headers[1]
    assert response.cookies == []


def test_direct_http_cookies_only_reads_bounded_challenge_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_body = b"x" * (navigation.DIRECT_HTTP_CHALLENGE_PROBE_BYTES * 2)
    read_sizes: list[int | None] = []
    response = _HttpResponse(url="https://origin.test/", status=200, body=response_body)
    original_read = response.read

    def tracked_read(size: int | None = None) -> bytes:
        read_sizes.append(size)
        return original_read(size)

    response.read = tracked_read  # type: ignore[method-assign]
    monkeypatch.setattr(navigation._HTTP_OPENER, "open", lambda request, timeout: response)

    result = navigation._direct_http_get_sync(
        "https://origin.test/",
        V1Request(
            cmd="request.get",
            url="https://origin.test/",
            returnOnlyCookies=True,
        ),
        1,
        maximum_body_bytes=20,
    )

    assert read_sizes == [navigation.DIRECT_HTTP_CHALLENGE_PROBE_BYTES + 1]
    assert len(result._body) == navigation.DIRECT_HTTP_CHALLENGE_PROBE_BYTES
    assert result._body == "x" * navigation.DIRECT_HTTP_CHALLENGE_PROBE_BYTES


def test_direct_http_materializes_error_status_and_reports_exact_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        navigation._HTTP_OPENER,
        "open",
        lambda request, timeout: _HttpResponse(
            url="https://origin.test/challenge",
            status=403,
            body=b"<html><title>Just a moment...</title></html>",
        ),
    )

    response = navigation._direct_http_get_sync(
        "https://origin.test/challenge",
        V1Request(
            cmd="request.get",
            url="https://origin.test/challenge",
            userAgent="Exact-UA/1.0",
        ),
        1,
    )

    assert response.status == 403
    assert response._body == "<html><title>Just a moment...</title></html>"
    assert response.user_agent == "Exact-UA/1.0"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"cmd": "request.get", "url": "https://[::1"},
        {
            "cmd": "request.get",
            "url": "https://example.com",
            "proxy": {"url": "http://proxy.example:not-a-port"},
        },
        {
            "cmd": "request.get",
            "url": "https://example.com",
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "url": "https://example.com",
                    "domain": "example.com",
                }
            ],
        },
    ],
)
async def test_malformed_network_inputs_return_invalid_request(
    payload: dict[str, Any],
) -> None:
    app = create_app(browser_factory=FakeBrowserFactory(), lifespan_enabled=False)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post("/v1", json=payload)

    assert response.status_code == 500
    assert response.json()["errorCode"] == "INVALID_REQUEST"
