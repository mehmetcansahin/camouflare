from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import threading
import time
import zlib
from collections.abc import Iterator
from email.message import Message
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

import camouflare.navigation as navigation
import camouflare.solver as solver_module
import camouflare.timer as timer_module
from camouflare.app import create_app
from camouflare.challenge import content_has_challenge_markers
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.limits import ResourceLimitError, ResourceLimits
from camouflare.models import V1Request
from camouflare.observability import JsonLogFormatter
from camouflare.solver import solve_request
from camouflare.timer import TimeoutTimer
from tests.fakes import FakeBrowserFactory, FakeContext, FakePage, FakeResponse


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


class _EncodedResponseServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _EncodedResponseHandler)
        self.routes: dict[str, tuple[int, list[tuple[str, str]], bytes]] = {}

    def route(
        self,
        path: str,
        body: bytes,
        *,
        status: int = 200,
        content_encoding: str | None = None,
        content_type: str = "text/html; charset=utf-8",
    ) -> str:
        headers = [("Content-Type", content_type)]
        if content_encoding is not None:
            headers.append(("Content-Encoding", content_encoding))
        self.routes[path] = (status, headers, body)
        return f"http://127.0.0.1:{self.server_port}{path}"


class _EncodedResponseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        status, headers, body = cast(_EncodedResponseServer, self.server).routes[self.path]
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def encoded_http_server() -> Iterator[_EncodedResponseServer]:
    server = _EncodedResponseServer()
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


# Hex tokens keep the compressed body larger than one socket read.
_JSON_BODY = json.dumps(
    {
        "city": "İstanbul",
        "items": [
            {"id": index, "token": hashlib.sha256(str(index).encode()).hexdigest()}
            for index in range(4_000)
        ],
    },
    ensure_ascii=False,
).encode()
_GZIP_JSON_BODY = gzip.compress(_JSON_BODY)
_GZIP_JSON_BODY_WITH_BAD_CRC = (
    _GZIP_JSON_BODY[:-8] + bytes([_GZIP_JSON_BODY[-8] ^ 0xFF]) + _GZIP_JSON_BODY[-7:]
)


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


@pytest.mark.parametrize(
    ("content_encoding", "wire_body"),
    [
        pytest.param(None, _JSON_BODY, id="identity"),
        pytest.param("gzip", _GZIP_JSON_BODY, id="gzip"),
        pytest.param(
            "gzip",
            gzip.compress(_JSON_BODY[:100_000]) + gzip.compress(_JSON_BODY[100_000:]),
            id="concatenated-gzip-members",
        ),
        pytest.param("deflate", zlib.compress(_JSON_BODY), id="zlib-deflate"),
        pytest.param("deflate", _raw_deflate(_JSON_BODY), id="raw-deflate"),
        pytest.param(
            "deflate, gzip",
            gzip.compress(zlib.compress(_JSON_BODY)),
            id="deflate-then-gzip",
        ),
    ],
)
def test_direct_http_decodes_supported_content_encodings(
    encoded_http_server: _EncodedResponseServer,
    content_encoding: str | None,
    wire_body: bytes,
) -> None:
    url = encoded_http_server.route(
        "/data.json",
        wire_body,
        content_encoding=content_encoding,
        content_type="application/json; charset=utf-8",
    )

    response = navigation._direct_http_get_sync(url, V1Request(cmd="request.get", url=url), 5)

    assert response.status == 200
    assert response._body == _JSON_BODY.decode("utf-8")


@pytest.mark.parametrize("status", [403, 503])
def test_direct_http_returns_decoded_error_status_pages(
    encoded_http_server: _EncodedResponseServer,
    status: int,
) -> None:
    page = "<html><title>Upstream refused</title><body>İstanbul</body></html>"
    url = encoded_http_server.route(
        "/refused",
        gzip.compress(page.encode("utf-8")),
        status=status,
        content_encoding="gzip",
    )

    response = navigation._direct_http_get_sync(url, V1Request(cmd="request.get", url=url), 5)

    assert response.status == status
    assert response._body == page


@pytest.mark.parametrize(
    ("content_encoding", "wire_body"),
    [
        pytest.param("br", b"opaque brotli bytes", id="brotli"),
        pytest.param("zstd", b"opaque zstd bytes", id="zstd"),
        pytest.param("gzip", _JSON_BODY, id="plain-bytes-labelled-gzip"),
        pytest.param("gzip", b"", id="empty-gzip-stream"),
        pytest.param("deflate", b"", id="empty-deflate-stream"),
        pytest.param("gzip", _GZIP_JSON_BODY[:-4], id="truncated-gzip"),
        pytest.param("gzip", _GZIP_JSON_BODY_WITH_BAD_CRC, id="gzip-crc-mismatch"),
        pytest.param("gzip", _GZIP_JSON_BODY + b"<html>", id="gzip-trailing-garbage"),
        pytest.param("deflate", zlib.compress(_JSON_BODY)[:-4], id="truncated-deflate"),
    ],
)
def test_direct_http_rejects_unsupported_or_malformed_content_encodings(
    encoded_http_server: _EncodedResponseServer,
    content_encoding: str,
    wire_body: bytes,
) -> None:
    url = encoded_http_server.route("/broken", wire_body, content_encoding=content_encoding)

    with pytest.raises(CamouflareError) as excinfo:
        navigation._direct_http_get_sync(url, V1Request(cmd="request.get", url=url), 5)

    assert excinfo.value.error_code is V1ErrorCode.RESPONSE_DECODE_ERROR
    assert excinfo.value.retryable is False


@pytest.mark.parametrize(("status", "encoding"), [(204, "gzip"), (304, "br")])
def test_bodyless_http_status_does_not_require_an_encoded_body(
    encoded_http_server: _EncodedResponseServer,
    status: int,
    encoding: str,
) -> None:
    url = encoded_http_server.route("/no-body", b"", status=status, content_encoding=encoding)

    response = navigation._direct_http_get_sync(url, V1Request(cmd="request.get", url=url), 5)

    assert response.status == status
    assert response._body == ""


def test_direct_http_limits_decoded_and_intermediate_bytes_not_only_wire_bytes(
    encoded_http_server: _EncodedResponseServer,
) -> None:
    limit = 4_096
    at_limit = encoded_http_server.route(
        "/at-limit",
        gzip.compress(b"0" * limit),
        content_encoding="gzip",
    )
    over_limit = encoded_http_server.route(
        "/over-limit",
        gzip.compress(b"0" * (limit + 1)),
        content_encoding="gzip",
    )
    # The inner layer decodes to two bytes, but the outer layer first inflates it
    # (with legal NUL member padding) past the limit.
    oversized_layer = encoded_http_server.route(
        "/oversized-layer",
        gzip.compress(gzip.compress(b"ok") + b"\x00" * (limit * 2)),
        content_encoding="gzip, gzip",
    )

    response = navigation._direct_http_get_sync(
        at_limit,
        V1Request(cmd="request.get", url=at_limit),
        5,
        maximum_body_bytes=limit,
    )
    assert response._body == "0" * limit
    for url in (over_limit, oversized_layer):
        with pytest.raises(ResourceLimitError):
            navigation._direct_http_get_sync(
                url,
                V1Request(cmd="request.get", url=url),
                5,
                maximum_body_bytes=limit,
            )


def test_direct_http_cookies_only_inspects_decoded_prefix_of_compressed_challenge(
    encoded_http_server: _EncodedResponseServer,
) -> None:
    challenge = b"<html><head><title>Just a moment...</title></head><body>" + b"x" * 4_194_304
    # The gzip trailer is missing: a full read is malformed, while the cookies-only
    # probe stops after the decoded prefix it inspects and never reaches the trailer.
    url = encoded_http_server.route(
        "/challenge",
        gzip.compress(challenge)[:-8],
        content_encoding="gzip",
    )

    probe = navigation._direct_http_get_sync(
        url,
        V1Request(cmd="request.get", url=url, returnOnlyCookies=True),
        5,
    )

    assert probe._body == challenge[: navigation.DIRECT_HTTP_CHALLENGE_PROBE_BYTES].decode()
    assert content_has_challenge_markers(probe._body)
    with pytest.raises(CamouflareError) as excinfo:
        navigation._direct_http_get_sync(url, V1Request(cmd="request.get", url=url), 5)
    assert excinfo.value.error_code is V1ErrorCode.RESPONSE_DECODE_ERROR


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("allow_direct_http_first", "goto_failure", "commit_times_out"),
    [
        pytest.param(True, None, False, id="ajax-preflight"),
        pytest.param(
            False,
            TimeoutError("navigation timed out"),
            True,
            id="navigation-timeout-fallback",
        ),
        pytest.param(False, RuntimeError("Target closed"), False, id="transport-closed-fallback"),
    ],
)
async def test_direct_http_decode_errors_surface_from_every_direct_get_path(
    encoded_http_server: _EncodedResponseServer,
    allow_direct_http_first: bool,
    goto_failure: Exception | None,
    commit_times_out: bool,
) -> None:
    url = encoded_http_server.route("/data?ajax=true", b"opaque", content_encoding="br")
    context = FakeContext()
    page = await context.new_page()
    if goto_failure is not None:
        page.goto_failures["domcontentloaded"] = goto_failure
    if commit_times_out:
        page.wait_failures.add("commit")
    executor = navigation.DirectHttpExecutor()
    try:
        with (
            navigation.active_direct_http_executor(executor),
            pytest.raises(CamouflareError) as excinfo,
        ):
            await navigation.navigate_get(
                page,
                V1Request(cmd="request.get", url=url),
                TimeoutTimer(5_000),
                ResourceLimits(),
                allow_direct_http_first=allow_direct_http_first,
            )
    finally:
        await executor.close()

    assert excinfo.value.error_code is V1ErrorCode.RESPONSE_DECODE_ERROR
    assert excinfo.value.retryable is False


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


_AJAX_URL = "https://example.com/search?ajax=true"
_DIRECT_BODY = '<div class="product">direct</div>'


class _VirtualClock:
    """An exact millisecond clock for request timers; simulated waits advance it."""

    def __init__(self) -> None:
        self._now = Fraction(0)

    def monotonic(self) -> Fraction:
        return self._now

    @property
    def ms(self) -> int:
        return int(self._now * 1000)

    def advance(self, milliseconds: int) -> None:
        self._now += Fraction(milliseconds, 1000)


@pytest.fixture
def virtual_clock(monkeypatch: pytest.MonkeyPatch) -> _VirtualClock:
    clock = _VirtualClock()
    # Only request timers read this clock; the event loop keeps real time.
    monkeypatch.setattr(timer_module, "time", clock)
    return clock


class _ClockedContext(FakeContext):
    """Records the virtual time at which solution collection reads cookies."""

    def __init__(self, clock: _VirtualClock) -> None:
        super().__init__()
        self.clock = clock
        self.collected_at_ms: int | None = None

    async def cookies(self) -> list[dict[str, Any]]:
        self.collected_at_ms = self.clock.ms
        return await super().cookies()


class _SlowTargetPage(FakePage):
    """A Playwright-like page for a target slow to answer or to finish its DOM.

    The server answers ``commit_after_ms`` after navigation starts, committing
    ``document``; the DOM is ready ``dom_ready_after_ms`` later. ``None`` means never.
    Like Playwright, each wait lasts until its event or its timeout (0 meaning no
    timeout) on the virtual clock, and every timeout given is recorded.
    """

    def __init__(
        self,
        context: FakeContext,
        clock: _VirtualClock,
        *,
        commit_after_ms: int | None,
        dom_ready_after_ms: int | None = 0,
        status: int = 200,
        document: tuple[str, str] = (
            "Example",
            "<html><title>Example</title><body>ok</body></html>",
        ),
    ) -> None:
        super().__init__(context)
        context.pages.append(self)
        self.clock = clock
        self.commit_after_ms = commit_after_ms
        self.dom_ready_after_ms = dom_ready_after_ms
        self.status = status
        self.document = document
        self.timeouts: list[float | None] = []
        self.title_value = ""
        self.content_value = "<html><head></head><body></body></html>"
        self.committed = False
        self._navigation: tuple[str, int] | None = None

    def _commit_at(self) -> int | None:
        if self._navigation is None or self.commit_after_ms is None:
            return None
        return self._navigation[1] + self.commit_after_ms

    def _dom_ready_at(self) -> int | None:
        commit_at = self._commit_at()
        if commit_at is None or self.dom_ready_after_ms is None:
            return None
        return commit_at + self.dom_ready_after_ms

    def _commit_if_due(self) -> None:
        commit_at = self._commit_at()
        if self.committed or self._navigation is None or commit_at is None:
            return
        if commit_at > self.clock.ms:
            return
        self.committed = True
        self.url = self._navigation[0]
        self.title_value, self.content_value = self.document
        self.emit_navigation_response(status=self.status, url=self.url)

    async def _wait_until(self, event_at_ms: int | None, timeout: float | None) -> None:
        self.timeouts.append(timeout)
        limit_ms = self.clock.ms + int(timeout) if timeout else None
        if event_at_ms is not None and (limit_ms is None or event_at_ms <= limit_ms):
            self.clock.advance(max(0, event_at_ms - self.clock.ms))
            self._commit_if_due()
            return
        if limit_ms is None:
            raise AssertionError("Playwright would wait forever for an event that never comes.")
        self.clock.advance(int(timeout or 0))
        self._commit_if_due()
        raise TimeoutError(f"Timeout {timeout}ms exceeded.")

    async def goto(
        self,
        url: str,
        *,
        timeout: float | None = None,
        wait_until: str | None = None,
        referer: str | None = None,
    ) -> FakeResponse:
        self.goto_calls.append(
            {"url": url, "timeout": timeout, "wait_until": wait_until, "referer": referer}
        )
        self._navigation = (url, self.clock.ms)
        await self._wait_until(self._dom_ready_at(), timeout)
        return FakeResponse(status=self.status)

    async def wait_for_url(
        self,
        matcher: Any,
        *,
        timeout: float | None = None,
        wait_until: str | None = None,
    ) -> None:
        self.wait_for_url_calls.append({"timeout": timeout, "wait_until": wait_until})
        if self.committed and matcher(self.url):
            # Playwright resolves an already committed URL before its timeout can fire.
            self.timeouts.append(timeout)
            return
        await self._wait_until(self._commit_at(), timeout)

    async def wait_for_load_state(
        self,
        state: str = "load",
        *,
        timeout: float | None = None,
    ) -> None:
        self.load_states.append(state)
        if self._navigation is None:
            self.timeouts.append(timeout)
            return
        # A navigation request still in flight keeps the page from becoming idle.
        await self._wait_until(self._dom_ready_at(), timeout)


def _direct_response(
    url: str, *, status: int = 200, body: str = _DIRECT_BODY
) -> navigation.RawResponse:
    return navigation.RawResponse(
        url=url,
        status=status,
        headers={"content-type": "text/html; charset=utf-8"},
        body=body,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("url", "direct_http_allowed"),
    [
        pytest.param("https://example.com/slow", True, id="fallback-ineligible"),
        pytest.param(_AJAX_URL, False, id="direct-http-disabled"),
    ],
)
async def test_uncommitted_browser_get_gives_up_independently_of_max_timeout(
    virtual_clock: _VirtualClock,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    direct_http_allowed: bool,
) -> None:
    direct_calls = 0

    async def direct_get(direct_url: str, request: V1Request, timer: TimeoutTimer) -> Any:
        nonlocal direct_calls
        direct_calls += 1
        return _direct_response(direct_url)

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)
    waited_ms: dict[int, int] = {}
    for max_timeout in (60_000, 120_000):
        context = _ClockedContext(virtual_clock)
        page = _SlowTargetPage(context, virtual_clock, commit_after_ms=None)
        started_ms = virtual_clock.ms

        result = await solve_request(
            V1Request(cmd="request.get", url=url, maxTimeout=max_timeout),
            context=context,
            page=page,
            allow_direct_http_fallback=direct_http_allowed,
            allow_direct_http_first=direct_http_allowed,
            timer=TimeoutTimer(max_timeout),
        )

        assert result.error_code is V1ErrorCode.NAVIGATION_TIMEOUT
        assert result.retryable is True
        assert result.solution is not None
        assert context.collected_at_ms is not None
        assert context.collected_at_ms < started_ms + max_timeout
        waited_ms[max_timeout] = context.collected_at_ms - started_ms

    assert direct_calls == 0
    # Bounded stages give up on an uncommitted document after the same wait for any
    # maxTimeout instead of spending a larger budget waiting for a commit.
    assert waited_ms[60_000] == waited_ms[120_000]


@pytest.mark.anyio
@pytest.mark.parametrize("max_timeout", [60_000, 3_000])
@pytest.mark.parametrize("preflight", [False, True], ids=["fallback-only", "after-preflight"])
@pytest.mark.parametrize("answers", ["promptly", "at-its-deadline"])
async def test_eligible_timeout_fallback_returns_content_with_collection_time_left(
    virtual_clock: _VirtualClock,
    monkeypatch: pytest.MonkeyPatch,
    max_timeout: int,
    preflight: bool,
    answers: str,
) -> None:
    direct_budgets_ms: list[int] = []

    async def direct_get(direct_url: str, request: V1Request, timer: TimeoutTimer) -> Any:
        direct_budgets_ms.append(timer.remaining_ms)
        if preflight and len(direct_budgets_ms) == 1:
            virtual_clock.advance(50)
            return _direct_response(direct_url, status=503, body="busy")
        # A prompt server needs 100 ms; a slow one answers just inside its deadline.
        needed_ms = 100 if answers == "promptly" else timer.remaining_ms - 1
        if needed_ms >= timer.remaining_ms:
            virtual_clock.advance(timer.remaining_ms)
            raise TimeoutError("Direct HTTP request exceeded the request deadline.")
        virtual_clock.advance(needed_ms)
        return _direct_response(direct_url)

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)
    context = _ClockedContext(virtual_clock)
    page = _SlowTargetPage(context, virtual_clock, commit_after_ms=None)
    deadline_ms = virtual_clock.ms + max_timeout

    result = await solve_request(
        V1Request(cmd="request.get", url=_AJAX_URL, maxTimeout=max_timeout),
        context=context,
        page=page,
        allow_direct_http_first=preflight,
        timer=TimeoutTimer(max_timeout),
    )

    assert result.status == "ok"
    assert result.fallback_used is True
    assert result.solution is not None
    assert result.solution.response == _DIRECT_BODY
    assert len(direct_budgets_ms) == (2 if preflight else 1)
    assert len(page.goto_calls) == 1
    assert context.collected_at_ms is not None
    assert context.collected_at_ms < deadline_ms


@pytest.mark.anyio
async def test_committed_challenge_after_dom_timeout_clears_with_the_request_budget(
    virtual_clock: _VirtualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = (
        "Just a moment...",
        '<html><title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/x">'
        "</script></html>",
    )
    cleared = ("Example", "<html><title>Example</title><body>cleared</body></html>")
    direct_calls = 0

    async def challenged_direct_get(
        direct_url: str, request: V1Request, timer: TimeoutTimer
    ) -> Any:
        nonlocal direct_calls
        direct_calls += 1
        virtual_clock.advance(50)
        return _direct_response(direct_url, status=403, body=challenge[1])

    monkeypatch.setattr(solver_module, "_direct_http_get", challenged_direct_get)
    context = _ClockedContext(virtual_clock)
    # The challenge commits at once, but its DOM never finishes loading.
    page = _SlowTargetPage(
        context,
        virtual_clock,
        commit_after_ms=0,
        dom_ready_after_ms=None,
        status=403,
        document=challenge,
    )
    started_ms = virtual_clock.ms
    clears_at_ms = started_ms + 40_000

    async def clearing_sleep(seconds: float) -> None:
        virtual_clock.advance(round(seconds * 1000))
        if virtual_clock.ms >= clears_at_ms and page.title_value != cleared[0]:
            page.title_value, page.content_value = cleared
            page.emit_navigation_response(status=200, url=_AJAX_URL)

    result = await solve_request(
        V1Request(cmd="request.get", url=_AJAX_URL, maxTimeout=60_000),
        context=context,
        page=page,
        sleep=clearing_sleep,
        timer=TimeoutTimer(60_000),
    )

    assert result.status == "ok"
    assert result.fallback_used is None
    # Only the preflight used direct HTTP: a committed document is never replaced.
    assert direct_calls == 1
    assert result.solution is not None
    assert result.solution.status == 200
    assert "cleared" in result.solution.response
    assert context.collected_at_ms is not None
    assert clears_at_ms <= context.collected_at_ms < started_ms + 60_000


@pytest.mark.anyio
@pytest.mark.parametrize("max_timeout", [1, 2, 9, 1_500])
@pytest.mark.parametrize(
    "url",
    ["https://example.com/slow", _AJAX_URL],
    ids=["browser-only", "with-timeout-fallback"],
)
async def test_short_budget_navigation_keeps_playwright_deadlines_and_terminates(
    virtual_clock: _VirtualClock,
    monkeypatch: pytest.MonkeyPatch,
    max_timeout: int,
    url: str,
) -> None:
    async def timing_out_direct_get(
        direct_url: str, request: V1Request, timer: TimeoutTimer
    ) -> Any:
        virtual_clock.advance(timer.remaining_ms)
        raise TimeoutError("Direct HTTP request exceeded the request deadline.")

    monkeypatch.setattr(solver_module, "_direct_http_get", timing_out_direct_get)
    context = _ClockedContext(virtual_clock)
    page = _SlowTargetPage(context, virtual_clock, commit_after_ms=None)
    deadline_ms = virtual_clock.ms + max_timeout

    result = await solve_request(
        V1Request(cmd="request.get", url=url, maxTimeout=max_timeout),
        context=context,
        page=page,
        allow_direct_http_first=False,
        timer=TimeoutTimer(max_timeout),
    )

    assert result.error_code is V1ErrorCode.NAVIGATION_TIMEOUT
    # Playwright treats a 0 ms timeout as no timeout at all.
    if max_timeout >= 9:
        assert page.timeouts
    assert all(timeout is not None and timeout >= 1 for timeout in page.timeouts)
    assert context.collected_at_ms is not None
    assert context.collected_at_ms < deadline_ms


@pytest.mark.anyio
@pytest.mark.parametrize("setup_ms", [0, 59_500], ids=["fresh-budget", "after-setup"])
@pytest.mark.parametrize("origin_bound", [False, True], ids=["ajax-preflight", "explicit-http"])
async def test_spent_navigation_budget_does_not_start_browser_navigation(
    virtual_clock: _VirtualClock,
    monkeypatch: pytest.MonkeyPatch,
    setup_ms: int,
    origin_bound: bool,
) -> None:
    direct_calls = 0

    async def timing_out_direct_get(
        direct_url: str, request: V1Request, timer: TimeoutTimer
    ) -> Any:
        nonlocal direct_calls
        direct_calls += 1
        virtual_clock.advance(timer.remaining_ms)
        raise TimeoutError("Direct HTTP request exceeded the request deadline.")

    monkeypatch.setattr(solver_module, "_direct_http_get", timing_out_direct_get)
    context = _ClockedContext(virtual_clock)
    # The browser would load this target at once, but no navigation time is left.
    page = _SlowTargetPage(context, virtual_clock, commit_after_ms=0)
    timer = TimeoutTimer(60_000)
    deadline_ms = virtual_clock.ms + 60_000
    # Pool/session waits share maxTimeout; preflight must not restart the budget.
    virtual_clock.advance(setup_ms)

    result = await solve_request(
        V1Request(
            cmd="request.get",
            url=_AJAX_URL,
            maxTimeout=60_000,
            headers={"Accept": "application/json"} if origin_bound else None,
        ),
        context=context,
        page=page,
        timer=timer,
    )

    assert result.error_code is V1ErrorCode.NAVIGATION_TIMEOUT
    assert result.retryable is True
    assert page.goto_calls == []
    assert direct_calls == 1
    assert context.collected_at_ms is not None
    assert context.collected_at_ms < deadline_ms


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("max_timeout", "commit_after_ms"),
    [
        pytest.param(9, 0, id="9ms"),
        pytest.param(20, 0, id="20ms"),
        pytest.param(3_000, 0, id="3000ms"),
        pytest.param(60_000, 20_000, id="commit-during-grace"),
    ],
)
async def test_dom_timeout_keeps_committed_browser_document_without_fallback(
    virtual_clock: _VirtualClock,
    monkeypatch: pytest.MonkeyPatch,
    max_timeout: int,
    commit_after_ms: int,
) -> None:
    direct_calls = 0

    async def direct_get(direct_url: str, request: V1Request, timer: TimeoutTimer) -> Any:
        nonlocal direct_calls
        direct_calls += 1
        return _direct_response(direct_url)

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)
    context = _ClockedContext(virtual_clock)
    browser_document = "<html><title>Browser</title><body>committed</body></html>"
    page = _SlowTargetPage(
        context,
        virtual_clock,
        commit_after_ms=commit_after_ms,
        dom_ready_after_ms=None,
        status=201,
        document=("Browser", browser_document),
    )
    deadline_ms = virtual_clock.ms + max_timeout

    result = await solve_request(
        V1Request(cmd="request.get", url=_AJAX_URL, maxTimeout=max_timeout),
        context=context,
        page=page,
        allow_direct_http_first=False,
        timer=TimeoutTimer(max_timeout),
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert result.solution.response == browser_document
    assert result.solution.status == 201
    assert result.fallback_used is None
    assert direct_calls == 0
    assert context.collected_at_ms is not None
    assert context.collected_at_ms < deadline_ms
