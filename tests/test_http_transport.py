from __future__ import annotations

import asyncio
import base64
import gzip
import socket
from contextlib import AbstractAsyncContextManager
from ipaddress import ip_address
from typing import Any

import httpcore
import pytest
import socksio.exceptions
from httpx import ASGITransport, AsyncClient

from camouflare import navigation
from camouflare.app import create_app
from camouflare.config import Settings
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.http_transport import (
    HopRequest,
    HttpFetch,
    HttpTransport,
    PublicAddressBackend,
    is_public_address,
)
from camouflare.limits import ResourceLimitError, ResourceLimits
from camouflare.models import V1Request
from camouflare.timer import TimeoutTimer
from tests.fakes import FakeBrowserFactory
from tests.http_fakes import FakeConnector, FakeHttpResponse, fake_transport

_TARGET = "https://cdn.example.com/event/G1/poster.jpg"


async def _post_v1(app: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.post("/v1", json=payload)
    return response.status_code, response.json()


def _app(connector: FakeConnector, **settings: Any) -> tuple[Any, FakeBrowserFactory]:
    factory = FakeBrowserFactory()
    app = create_app(
        settings=Settings(**settings),
        browser_factory=factory,
        http_connector=connector,
        lifespan_enabled=False,
    )
    return app, factory


@pytest.mark.anyio
async def test_http_transport_returns_exact_bytes_without_a_browser() -> None:
    body = bytes(range(256)) * 10
    connector = FakeConnector(
        [
            FakeHttpResponse(
                200,
                [
                    ("Content-Type", "image/jpeg"),
                    ("X-Variant", "a"),
                    ("x-variant", "b"),
                    ("Set-Cookie", "first=1; Path=/"),
                    ("Set-Cookie", "second=2; Path=/"),
                ],
                body,
            )
        ]
    )
    app, factory = _app(connector)

    status, payload = await _post_v1(
        app,
        {
            "cmd": "request.get",
            "url": _TARGET,
            "transport": "http",
            "responseEncoding": "base64",
            "headers": {"Accept": "image/*", "Referer": "https://www.example.com/"},
        },
    )

    assert status == 200
    assert payload["status"] == "ok"
    solution = payload["solution"]
    assert base64.b64decode(solution["response"]) == body
    assert solution["responseEncoding"] == "base64"
    assert solution["bodyBytes"] == len(body)
    assert solution["headers"] == {
        "content-type": "image/jpeg",
        "x-variant": "a, b",
        "set-cookie": "first=1; Path=/\nsecond=2; Path=/",
    }
    assert {cookie["name"] for cookie in solution["cookies"]} == {"first", "second"}
    assert "fallbackUsed" not in payload
    assert factory.created == []
    [hop] = connector.hops
    assert hop.method == "GET"
    assert hop.headers["accept"] == "image/*"
    assert hop.headers["referer"] == "https://www.example.com/"
    assert hop.block_private_targets is True
    assert hop.proxy is None


@pytest.mark.anyio
async def test_http_transport_text_body_describes_the_decoded_bytes() -> None:
    decoded = '{"seans": "İstanbul"}'.encode()
    wire = gzip.compress(decoded)
    connector = FakeConnector(
        [
            FakeHttpResponse(
                200,
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Encoding", "gzip"),
                    ("Content-Length", str(len(wire))),
                ],
                wire,
            )
        ]
    )
    app, _ = _app(connector)

    _, payload = await _post_v1(
        app,
        {
            "cmd": "request.get",
            "url": "https://api.example.com/sessions",
            "transport": "http",
            "headers": {"X-Requested-With": "XMLHttpRequest"},
        },
    )

    solution = payload["solution"]
    assert solution["response"] == '{"seans": "İstanbul"}'
    assert solution["responseEncoding"] == "text"
    assert solution["bodyBytes"] == len(decoded)
    assert solution["headers"] == {"content-type": "application/json; charset=utf-8"}
    assert connector.hops[0].headers["x-requested-with"] == "XMLHttpRequest"


@pytest.mark.anyio
async def test_request_head_returns_headers_and_never_reads_a_body() -> None:
    connector = FakeConnector(
        [
            FakeHttpResponse(
                200,
                [("Content-Type", "image/jpeg"), ("Content-Length", "26366")],
                b"must not be read",
            )
        ]
    )
    app, factory = _app(connector)

    status, payload = await _post_v1(app, {"cmd": "request.head", "url": _TARGET})

    assert status == 200
    solution = payload["solution"]
    assert solution["status"] == 200
    assert solution["headers"] == {"content-type": "image/jpeg", "content-length": "26366"}
    assert solution["response"] == ""
    assert solution["bodyBytes"] == 0
    assert connector.hops[0].method == "HEAD"
    assert connector.consumed_bytes == [0]
    assert factory.created == []


@pytest.mark.anyio
async def test_follow_redirects_zero_returns_the_redirect_for_caller_approval() -> None:
    connector = FakeConnector(
        [FakeHttpResponse(302, [("Location", "https://www.example.com/approved")], b"")]
    )
    app, _ = _app(connector)

    _, payload = await _post_v1(
        app,
        {"cmd": "request.get", "url": _TARGET, "transport": "http", "followRedirects": 0},
    )

    assert payload["status"] == "ok"
    assert payload["solution"]["status"] == 302
    assert payload["solution"]["headers"]["location"] == "https://www.example.com/approved"
    assert len(connector.hops) == 1


@pytest.mark.anyio
async def test_redirect_past_the_limit_is_returned_as_the_response() -> None:
    transport, connector = fake_transport(
        lambda hop: FakeHttpResponse(302, [("Location", f"{hop.url}x")]),
    )

    result = await transport.fetch(HttpFetch(url=_TARGET, max_redirects=2), timeout_seconds=5)

    assert result.status == 302
    assert [hop.url for hop in connector.hops] == [_TARGET, f"{_TARGET}x", f"{_TARGET}xx"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("location", "proxy", "followed"),
    [
        ("http://cdn.example.com/plain", None, False),
        ("https://169.254.169.254/latest/meta-data", None, False),
        ("https://[::ffff:10.0.0.1]/", None, False),
        ("https://10.0.0.1/", {"server": "socks5://proxy.example:1080"}, True),
    ],
)
async def test_redirect_targets_follow_the_target_policy(
    location: str,
    proxy: dict[str, str] | None,
    followed: bool,
) -> None:
    transport, connector = fake_transport(
        [FakeHttpResponse(302, [("Location", location)]), FakeHttpResponse(200, body=b"ok")]
    )
    fetch = HttpFetch(url=_TARGET, proxy=proxy, require_https=True, block_private_targets=True)

    if followed:
        result = await transport.fetch(fetch, timeout_seconds=5)
        assert result.body == b"ok"
        assert len(connector.hops) == 2
    else:
        with pytest.raises(CamouflareError) as raised:
            await transport.fetch(fetch, timeout_seconds=5)
        assert raised.value.error_code is V1ErrorCode.INVALID_REQUEST
        assert len(connector.hops) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "failed_layer"),
    [
        (FakeHttpResponse(200, body=b"x" * 40), None),
        (FakeHttpResponse(200, body=[b"x" * 21, b"x" * 20]), "Response body"),
        # 24 wire bytes fit the limit; the 64 bytes they decode to do not.
        (
            FakeHttpResponse(200, [("Content-Encoding", "gzip")], gzip.compress(b"x" * 64)),
            "Decoded response body",
        ),
    ],
)
async def test_max_response_bytes_bounds_wire_and_decoded_bodies(
    response: FakeHttpResponse,
    failed_layer: str | None,
) -> None:
    connector = FakeConnector([response])
    app, _ = _app(connector)

    status, payload = await _post_v1(
        app,
        {
            "cmd": "request.get",
            "url": _TARGET,
            "transport": "http",
            "maxResponseBytes": 40,
            "responseEncoding": "base64",
        },
    )

    if failed_layer is None:
        assert status == 200
        assert payload["solution"]["bodyBytes"] == 40
    else:
        assert status == 500
        assert payload["errorCode"] == "RESOURCE_LIMIT_EXCEEDED"
        assert payload["retryable"] is False
        assert f"{failed_layer} exceeds" in payload["message"]


@pytest.mark.anyio
async def test_http_transport_cookies_only_never_reads_the_body() -> None:
    connector = FakeConnector(
        [FakeHttpResponse(200, [("Set-Cookie", "sid=1; Path=/")], b"x" * 100_000)]
    )
    app, _ = _app(connector)

    status, payload = await _post_v1(
        app,
        {"cmd": "request.get", "url": _TARGET, "transport": "http", "returnOnlyCookies": True},
    )

    assert status == 200
    assert set(payload["solution"]) == {"url", "status", "cookies", "userAgent"}
    assert [cookie["name"] for cookie in payload["solution"]["cookies"]] == ["sid"]
    assert connector.consumed_bytes == [0]


@pytest.mark.anyio
@pytest.mark.parametrize("location", ["ftp://cdn.example.com/x", "https://cdn.example.com:99999/"])
async def test_a_redirect_that_cannot_be_requested_is_returned_as_the_response(
    location: str,
) -> None:
    transport, connector = fake_transport([FakeHttpResponse(302, [("Location", location)])])

    result = await transport.fetch(HttpFetch(url=_TARGET, require_https=True), timeout_seconds=5)

    assert result.status == 302
    assert result.message["Location"] == location
    assert len(connector.hops) == 1


@pytest.mark.anyio
async def test_ipv6_literal_targets_send_a_bracketed_host_header() -> None:
    transport, connector = fake_transport(lambda hop: FakeHttpResponse(200))

    await transport.fetch(HttpFetch(url="https://[2606:4700::1111]:8443/x"), timeout_seconds=5)
    await transport.fetch(HttpFetch(url="https://[2606:4700::1111]/x"), timeout_seconds=5)
    await transport.fetch(HttpFetch(url="https://cdn.example.com/x"), timeout_seconds=5)

    assert [hop.headers.get("host") for hop in connector.hops] == [
        "[2606:4700::1111]:8443",
        "[2606:4700::1111]",
        None,
    ]


@pytest.mark.anyio
async def test_declared_oversized_body_is_rejected_without_reading_it() -> None:
    transport, connector = fake_transport(
        [FakeHttpResponse(200, [("Content-Length", "11")], b"x" * 11)]
    )

    with pytest.raises(ResourceLimitError):
        await transport.fetch(HttpFetch(url=_TARGET, max_body_bytes=10), timeout_seconds=5)

    assert connector.consumed_bytes == [0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        ({"cmd": "request.get", "responseEncoding": "base64"}, "INVALID_REQUEST"),
        ({"cmd": "request.get", "transport": "browser", "followRedirects": 0}, "INVALID_REQUEST"),
        (
            {"cmd": "request.post", "transport": "http", "postData": "a=b"},
            "INVALID_REQUEST",
        ),
        ({"cmd": "request.get", "transport": "http", "session": "s1"}, "INVALID_REQUEST"),
        ({"cmd": "request.get", "transport": "http", "returnScreenshot": True}, "INVALID_REQUEST"),
        ({"cmd": "request.get", "transport": "http", "waitInSeconds": 2}, "INVALID_REQUEST"),
        ({"cmd": "request.get", "transport": "http", "disableMedia": True}, "INVALID_REQUEST"),
        (
            {"cmd": "request.get", "transport": "http", "session_ttl_minutes": 5},
            "INVALID_REQUEST",
        ),
        (
            {"cmd": "request.get", "transport": "http", "headers": {"X-City": "İstanbul"}},
            "INVALID_REQUEST",
        ),
        ({"cmd": "sessions.create", "transport": "http"}, "INVALID_REQUEST"),
        ({"cmd": "sessions.create", "responseEncoding": "base64"}, "INVALID_REQUEST"),
        ({"cmd": "sessions.list", "followRedirects": 0}, "INVALID_REQUEST"),
        (
            {"cmd": "request.get", "transport": "http", "headers": {"Host": "internal"}},
            "INVALID_REQUEST",
        ),
        ({"cmd": "request.head", "transport": "browser"}, "INVALID_REQUEST"),
        (
            {"cmd": "request.get", "transport": "browser", "headers": {"X-Requested-With": "1"}},
            "INVALID_REQUEST",
        ),
        (
            {"cmd": "request.get", "transport": "http", "url": "http://cdn.example.com/"},
            "INVALID_REQUEST",
        ),
        (
            {
                "cmd": "request.get",
                "transport": "http",
                "proxy": {"url": "socks4://proxy.example:1080"},
            },
            "INVALID_REQUEST",
        ),
        (
            {"cmd": "request.get", "transport": "http", "maxResponseBytes": 33_554_433},
            "RESOURCE_LIMIT_EXCEEDED",
        ),
    ],
)
async def test_requests_the_http_transport_cannot_honor_are_rejected_before_sending(
    payload: dict[str, Any],
    error_code: str,
) -> None:
    connector = FakeConnector([FakeHttpResponse(200, body=b"unexpected")])
    app, factory = _app(connector)

    status, response = await _post_v1(app, {"url": _TARGET, **payload})

    assert status == 500
    assert response["errorCode"] == error_code
    assert connector.hops == []
    assert factory.created == []


@pytest.mark.anyio
async def test_http_transport_uses_the_default_proxy_without_blocking_private_targets() -> None:
    connector = FakeConnector([FakeHttpResponse(200, body=b"ok")])
    app, _ = _app(
        connector,
        proxy_url="socks5h://geo.example:1080",
        proxy_username="user",
        proxy_password="secret",
    )

    status, _ = await _post_v1(app, {"cmd": "request.get", "url": _TARGET, "transport": "http"})

    assert status == 200
    assert connector.sessions == [
        (
            {"server": "socks5://geo.example:1080", "username": "user", "password": "secret"},
            False,
        )
    ]


class _BlockingConnector(FakeConnector):
    """Holds every hop open until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__(lambda hop: FakeHttpResponse(200, body=b"done"))
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def session(self, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        outer = super().session(**kwargs)
        connector = self

        class _Session:
            async def __aenter__(self) -> Any:
                send = await outer.__aenter__()

                def blocking_send(hop: HopRequest) -> AbstractAsyncContextManager[Any]:
                    inner = send(hop)

                    class _Hop:
                        async def __aenter__(self) -> Any:
                            connector.started.set()
                            await connector.release.wait()
                            return await inner.__aenter__()

                        async def __aexit__(self, *exc: object) -> None:
                            await inner.__aexit__(*exc)

                    return _Hop()

                return blocking_send

            async def __aexit__(self, *exc: object) -> None:
                await outer.__aexit__(*exc)

        return _Session()


@pytest.mark.anyio
async def test_http_capacity_is_bounded_and_reported_separately_from_browsers() -> None:
    connector = _BlockingConnector()
    app, _ = _app(connector, http_max_concurrency=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        first = asyncio.create_task(
            client.post("/v1", json={"cmd": "request.head", "url": _TARGET})
        )
        await connector.started.wait()
        diagnostics = (await client.get("/diagnostics")).json()
        waiting = await client.post(
            "/v1", json={"cmd": "request.head", "url": _TARGET, "maxTimeout": 100}
        )
        connector.release.set()
        completed = await first

    assert diagnostics["http"] == {
        "max_concurrency": 1,
        "active_requests": 1,
        "waiting_requests": 0,
        "available_slots": 0,
    }
    assert waiting.status_code == 503
    assert waiting.json()["errorCode"] == "POOL_UNAVAILABLE"
    assert waiting.json()["retryable"] is True
    assert completed.status_code == 200


@pytest.mark.anyio
async def test_http_request_runs_while_every_browser_slot_is_leased() -> None:
    connector = FakeConnector([FakeHttpResponse(200, body=b"image")])
    app, _ = _app(
        connector,
        pool_min_browsers=1,
        pool_max_browsers=1,
        pool_max_contexts_per_browser=1,
        pool_reserved_transient_contexts=0,
        pool_acquire_timeout_ms=100,
    )
    await app.state.pool.start()
    try:
        async with app.state.pool.lease_context(no_viewport=True):
            browser_status, browser_payload = await _post_v1(
                app, {"cmd": "request.get", "url": _TARGET, "maxTimeout": 5000}
            )
            http_status, http_payload = await _post_v1(
                app,
                {
                    "cmd": "request.get",
                    "url": _TARGET,
                    "transport": "http",
                    "maxTimeout": 5000,
                },
            )
    finally:
        await app.state.pool.close()

    assert browser_status == 503
    assert browser_payload["errorCode"] == "POOL_UNAVAILABLE"
    assert http_status == 200
    assert http_payload["solution"]["response"] == "image"


@pytest.mark.anyio
@pytest.mark.parametrize(("transport", "direct_hops"), [(None, 1), ("browser", 0)])
async def test_browser_transport_never_substitutes_the_direct_http_preflight(
    transport: str | None,
    direct_hops: int,
) -> None:
    connector = FakeConnector(lambda hop: FakeHttpResponse(200, body=b"direct"))
    app, _ = _app(connector)
    await app.state.pool.start()
    payload: dict[str, Any] = {"cmd": "request.get", "url": "https://example.com/data?ajax=true"}
    if transport is not None:
        payload["transport"] = transport
    try:
        status, response = await _post_v1(app, payload)
    finally:
        await app.state.pool.close()

    assert status == 200
    assert len(connector.hops) == direct_hops
    expected = "direct" if direct_hops else "<html><title>Example</title><body>ok</body></html>"
    assert response["solution"]["response"] == expected


@pytest.mark.anyio
async def test_transport_quiesce_rejects_new_work_and_close_drains_in_flight() -> None:
    connector = _BlockingConnector()
    transport = HttpTransport(max_concurrency=2, connector=connector)
    in_flight = asyncio.create_task(transport.fetch(HttpFetch(url=_TARGET), timeout_seconds=5))
    await connector.started.wait()

    closing = asyncio.create_task(transport.close())
    await asyncio.sleep(0)
    with pytest.raises(CamouflareError) as rejected:
        await transport.fetch(HttpFetch(url=_TARGET), timeout_seconds=5)
    assert not closing.done()
    connector.release.set()
    result = await in_flight
    await closing

    assert rejected.value.error_code is V1ErrorCode.POOL_UNAVAILABLE
    assert result.body == b"done"
    assert connector.closed is True


_VERIFY_FAILED = "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "proxy", "tunnel_open", "error_code", "retryable"),
    [
        (httpcore.ConnectError("[Errno 61] refused"), None, False, "TARGET_UNREACHABLE", True),
        (httpcore.ConnectError(_VERIFY_FAILED), None, False, "TARGET_UNREACHABLE", False),
        (httpcore.ReadTimeout("timed out"), None, False, "NAVIGATION_TIMEOUT", True),
        # Before the proxy reached the target, every network failure is the proxy's.
        (
            httpcore.ConnectError("[Errno 61] refused"),
            "socks",
            False,
            "PROXY_CONNECTION_ERROR",
            True,
        ),
        (httpcore.ConnectTimeout("timed out"), "socks", False, "PROXY_CONNECTION_ERROR", True),
        (
            httpcore.RemoteProtocolError("illegal status line"),
            "http",
            False,
            "PROXY_CONNECTION_ERROR",
            True,
        ),
        (httpcore.ConnectError(_VERIFY_FAILED), "https", False, "PROXY_CONNECTION_ERROR", False),
        (
            socksio.exceptions.ProtocolError("Malformed reply"),
            "socks",
            False,
            "PROXY_CONNECTION_ERROR",
            True,
        ),
        # Once the tunnel is open, failures are the target's, as without a proxy.
        (httpcore.ConnectTimeout("timed out"), "http", True, "NAVIGATION_TIMEOUT", True),
        (httpcore.ConnectError(_VERIFY_FAILED), "http", True, "TARGET_UNREACHABLE", False),
        (
            httpcore.RemoteProtocolError("Server disconnected"),
            "socks",
            True,
            "TARGET_UNREACHABLE",
            True,
        ),
        # Replies from the proxy itself.
        (
            httpcore.ProxyError("Invalid username/password"),
            "socks",
            False,
            "PROXY_CONNECTION_ERROR",
            False,
        ),
        (
            httpcore.ProxyError("407 Proxy Authentication Required"),
            "http",
            False,
            "PROXY_CONNECTION_ERROR",
            False,
        ),
        (httpcore.ProxyError("403 Forbidden"), "http", False, "PROXY_CONNECTION_ERROR", False),
        (
            httpcore.ProxyError("503 Service Unavailable"),
            "http",
            False,
            "PROXY_CONNECTION_ERROR",
            True,
        ),
        (
            httpcore.ProxyError("Proxy Server could not connect: Host unreachable."),
            "socks",
            False,
            "TARGET_UNREACHABLE",
            True,
        ),
        (httpcore.ProxyError("502 Bad Gateway"), "http", False, "TARGET_UNREACHABLE", True),
        (
            httpcore.ProxyError("Proxy Server could not connect: General SOCKS server failure."),
            "socks",
            False,
            "PROXY_CONNECTION_ERROR",
            True,
        ),
    ],
)
async def test_network_failures_separate_proxy_faults_from_target_faults(
    error: Exception,
    proxy: str | None,
    tunnel_open: bool,
    error_code: str,
    retryable: bool,
) -> None:
    proxies = {
        "socks": {"server": "socks5://proxy.example:1080"},
        "http": {"server": "http://proxy.example:3128"},
        "https": {"server": "https://proxy.example:3129"},
    }
    transport, _ = fake_transport([FakeHttpResponse(error=error, proxy_tunnel_open=tunnel_open)])

    with pytest.raises(CamouflareError) as raised:
        await transport.fetch(
            HttpFetch(url=_TARGET, proxy=proxies.get(proxy or "")),
            timeout_seconds=5,
        )

    assert raised.value.error_code.value == error_code
    assert raised.value.retryable is retryable


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected: list[str] = []

    async def connect_tcp(self, host: str, port: int, **kwargs: Any) -> Any:
        self.connected.append(host)
        raise httpcore.ConnectError("recorded")

    async def connect_unix_socket(self, path: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("addresses", "allowed"),
    [
        (["93.184.215.14"], True),
        (["10.0.0.5"], False),
        (["93.184.215.14", "127.0.0.1"], False),
        (["::ffff:169.254.169.254"], False),
        (["64:ff9b::a00:1"], False),
        (["100.64.0.1"], False),
    ],
)
async def test_public_address_backend_connects_only_to_vetted_public_addresses(
    addresses: list[str],
    allowed: bool,
) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return addresses

    inner = _RecordingBackend()
    backend = PublicAddressBackend(resolver=resolver, inner=inner)

    if allowed:
        with pytest.raises(httpcore.ConnectError, match="recorded"):
            await backend.connect_tcp("cdn.example.com", 443)
        assert inner.connected == addresses
    else:
        with pytest.raises(CamouflareError) as raised:
            await backend.connect_tcp("cdn.example.com", 443)
        assert raised.value.error_code is V1ErrorCode.INVALID_REQUEST
        assert inner.connected == []


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("169.254.169.254", False),
        ("fd00::1", False),
        ("::1", False),
        ("224.0.0.1", False),
        ("::ffff:8.8.8.8", True),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    assert is_public_address(ip_address(address)) is public


async def _serve(handler: Any) -> tuple[asyncio.AbstractServer, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("url", "proxied", "error_code"),
    [
        # A target that accepts TCP but never answers the TLS handshake.
        ("https://127.0.0.1:{port}/", False, V1ErrorCode.NAVIGATION_TIMEOUT),
        # A SOCKS proxy that accepts TCP but never answers the handshake.
        ("https://cdn.example.com/", True, V1ErrorCode.PROXY_CONNECTION_ERROR),
    ],
)
async def test_an_interrupted_handshake_does_not_leak_its_socket(
    url: str,
    proxied: bool,
    error_code: V1ErrorCode,
) -> None:
    peer_closed = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()
        finally:
            peer_closed.set()
            writer.close()

    server, port = await _serve(silent)
    transport = HttpTransport(max_concurrency=1)
    proxy = {"server": f"socks5://127.0.0.1:{port}"} if proxied else None
    try:
        with pytest.raises(CamouflareError) as raised:
            await transport.fetch(
                HttpFetch(url=url.format(port=port), proxy=proxy), timeout_seconds=0.2
            )
        # The socket closes with the failed request, not when the transport shuts down.
        await asyncio.wait_for(peer_closed.wait(), timeout=2)
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()

    assert raised.value.error_code is error_code


async def _http_target(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    await reader.readuntil(b"\r\n\r\n")
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\nviaprx")
    await writer.drain()
    writer.close()


def _socks_server(seen: dict[str, Any], *, password: bytes = b"secret") -> Any:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _, method_count = await reader.readexactly(2)
        await reader.readexactly(method_count)
        writer.write(b"\x05\x02")
        _, user_length = await reader.readexactly(2)
        user = await reader.readexactly(user_length)
        given = await reader.readexactly((await reader.readexactly(1))[0])
        seen["auth"] = (user, given)
        if given != password:
            writer.write(b"\x01\x01")
            await writer.drain()
            writer.close()
            return
        writer.write(b"\x01\x00")
        _, _, _, address_type = await reader.readexactly(4)
        assert address_type == 3, "the target hostname must reach the proxy for remote DNS"
        host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        port = int.from_bytes(await reader.readexactly(2), "big")
        seen["target"] = (host, port)
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            while data := await source.read(65536):
                sink.write(data)
                await sink.drain()
            sink.close()

        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))

    return handle


@pytest.mark.anyio
@pytest.mark.parametrize(("password", "ok"), [("secret", True), ("wrong", False)])
async def test_socks5_proxy_authenticates_and_resolves_the_target_remotely(
    password: str,
    ok: bool,
) -> None:
    seen: dict[str, Any] = {}
    target, target_port = await _serve(_http_target)
    proxy, proxy_port = await _serve(_socks_server(seen))
    transport = HttpTransport(max_concurrency=1)
    fetch = HttpFetch(
        url=f"http://localhost:{target_port}/poster.jpg",
        proxy={
            "server": f"socks5://127.0.0.1:{proxy_port}",
            "username": "user",
            "password": password,
        },
    )
    try:
        if ok:
            result = await transport.fetch(fetch, timeout_seconds=5)
            assert result.body == b"viaprx"
            assert seen["target"] == ("localhost", target_port)
        else:
            with pytest.raises(CamouflareError) as raised:
                await transport.fetch(fetch, timeout_seconds=5)
            assert raised.value.error_code is V1ErrorCode.PROXY_CONNECTION_ERROR
            assert raised.value.retryable is False
        assert seen["auth"] == (b"user", password.encode())
    finally:
        await transport.close()
        for server in (proxy, target):
            server.close()
            await server.wait_closed()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "error_code", "retryable"),
    [
        (b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n", "PROXY_CONNECTION_ERROR", False),
        (b"HTTP/1.1 502 Bad Gateway\r\n\r\n", "TARGET_UNREACHABLE", True),
        (b"HTTP/1.1 403 Forbidden\r\n\r\n", "PROXY_CONNECTION_ERROR", False),
        # A proxy that is not an HTTP proxy or drops the CONNECT is the proxy's fault.
        (b"SSH-2.0-OpenSSH_9.6\r\n", "PROXY_CONNECTION_ERROR", True),
        (b"", "PROXY_CONNECTION_ERROR", True),
        # The tunnel opened, then the target end failed the TLS handshake.
        (b"HTTP/1.1 200 Connection Established\r\n\r\n", "TARGET_UNREACHABLE", True),
    ],
)
async def test_http_connect_proxy_tunnels_with_credentials(
    reply: bytes,
    error_code: str,
    retryable: bool,
) -> None:
    seen: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(reply)
        await writer.drain()
        writer.close()

    proxy, proxy_port = await _serve(handle)
    transport = HttpTransport(max_concurrency=1)
    try:
        with pytest.raises(CamouflareError) as raised:
            await transport.fetch(
                HttpFetch(
                    url=_TARGET,
                    proxy={
                        "server": f"http://127.0.0.1:{proxy_port}",
                        "username": "user",
                        "password": "secret",
                    },
                    require_https=True,
                    block_private_targets=True,
                ),
                timeout_seconds=5,
            )
    finally:
        await transport.close()
        proxy.close()
        await proxy.wait_closed()

    assert raised.value.error_code.value == error_code
    assert raised.value.retryable is retryable
    request = seen[0].decode()
    assert request.startswith("CONNECT cdn.example.com:443 HTTP/1.1\r\n")
    credentials = base64.b64encode(b"user:secret").decode()
    assert f"Proxy-Authorization: Basic {credentials}" in request


@pytest.mark.anyio
async def test_unreachable_proxy_is_a_retryable_proxy_fault() -> None:
    proxy, proxy_port = await _serve(_http_target)
    proxy.close()
    await proxy.wait_closed()
    transport = HttpTransport(max_concurrency=1)
    try:
        with pytest.raises(CamouflareError) as raised:
            await transport.fetch(
                HttpFetch(url=_TARGET, proxy={"server": f"socks5://127.0.0.1:{proxy_port}"}),
                timeout_seconds=5,
            )
    finally:
        await transport.close()

    assert raised.value.error_code is V1ErrorCode.PROXY_CONNECTION_ERROR
    assert raised.value.retryable is True


@pytest.mark.anyio
async def test_a_socks_port_that_does_not_speak_socks_is_a_proxy_fault() -> None:
    # The usual misconfiguration: socks5:// pointed at an HTTP proxy port.
    async def http_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(64)
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    proxy, proxy_port = await _serve(http_proxy)
    transport = HttpTransport(max_concurrency=1)
    try:
        with pytest.raises(CamouflareError) as raised:
            await transport.fetch(
                HttpFetch(url=_TARGET, proxy={"server": f"socks5://127.0.0.1:{proxy_port}"}),
                timeout_seconds=5,
            )
    finally:
        await transport.close()
        proxy.close()
        await proxy.wait_closed()

    assert raised.value.error_code is V1ErrorCode.PROXY_CONNECTION_ERROR
    assert raised.value.retryable is True


@pytest.mark.anyio
async def test_a_target_failure_behind_an_open_socks_tunnel_is_the_targets() -> None:
    async def silent_target(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.close()

    seen: dict[str, Any] = {}
    target, target_port = await _serve(silent_target)
    proxy, proxy_port = await _serve(_socks_server(seen))
    transport = HttpTransport(max_concurrency=1)
    try:
        with pytest.raises(CamouflareError) as raised:
            await transport.fetch(
                HttpFetch(
                    url=f"http://localhost:{target_port}/",
                    proxy={
                        "server": f"socks5://127.0.0.1:{proxy_port}",
                        "username": "user",
                        "password": "secret",
                    },
                ),
                timeout_seconds=5,
            )
    finally:
        await transport.close()
        for server in (proxy, target):
            server.close()
            await server.wait_closed()

    assert seen["target"] == ("localhost", target_port)
    assert raised.value.error_code is V1ErrorCode.TARGET_UNREACHABLE
    assert raised.value.retryable is True


def _chunked(body: bytes, size: int) -> bytes:
    parts = [body[offset : offset + size] for offset in range(0, len(body), size)]
    return b"".join(b"%x\r\n%b\r\n" % (len(part), part) for part in parts) + b"0\r\n\r\n"


@pytest.mark.anyio
async def test_direct_http_reads_a_real_chunked_gzip_response_after_a_redirect() -> None:
    decoded = b"<html><body>" + b"etkinlik " * 20_000 + b"</body></html>"
    requests: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                requests.append(head)
                if head.startswith(b"GET /start?"):
                    writer.write(
                        b"HTTP/1.1 302 Found\r\nLocation: /final\r\nContent-Length: 0\r\n\r\n"
                    )
                else:
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                        b"Content-Encoding: gzip\r\nTransfer-Encoding: chunked\r\n\r\n"
                        + _chunked(gzip.compress(decoded), 4096)
                    )
                await writer.drain()
        finally:
            writer.close()

    server, port = await _serve(handle)
    transport = HttpTransport(max_concurrency=1)
    request = V1Request(cmd="request.get", url=f"http://127.0.0.1:{port}/start?ajax=true")
    try:
        with (
            navigation.active_http_transport(transport),
            navigation.active_resource_limits(ResourceLimits()),
        ):
            response = await navigation._direct_http_get(
                str(request.url), request, TimeoutTimer(5_000)
            )
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()

    assert response.status == 200
    assert response.url == f"http://127.0.0.1:{port}/final"
    assert (await response.text()).encode() == decoded
    # Both hops reached the server with the plain IPv4 Host header.
    assert [head.split(b"\r\n", 1)[0] for head in requests] == [
        b"GET /start?ajax=true HTTP/1.1",
        b"GET /final HTTP/1.1",
    ]
    assert all(f"Host: 127.0.0.1:{port}\r\n".encode() in head for head in requests)


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.anyio
@pytest.mark.skipif(not _ipv6_loopback_available(), reason="IPv6 loopback is unavailable")
async def test_ipv6_target_receives_a_bracketed_host_header_on_the_wire() -> None:
    seen: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "::1", 0)
    port = int(server.sockets[0].getsockname()[1])
    transport = HttpTransport(max_concurrency=1)
    try:
        result = await transport.fetch(HttpFetch(url=f"http://[::1]:{port}/"), timeout_seconds=5)
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()

    assert result.status == 204
    host_lines = [line for line in seen[0].split(b"\r\n") if line.lower().startswith(b"host:")]
    assert host_lines == [f"Host: [::1]:{port}".encode()]


@pytest.mark.anyio
async def test_private_target_is_rejected_through_the_real_resolver_and_pool() -> None:
    connected: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.append(b"connected")
        writer.close()

    server, port = await _serve(handle)
    transport = HttpTransport(max_concurrency=1)
    try:
        with pytest.raises(CamouflareError) as raised:
            await transport.fetch(
                HttpFetch(
                    url=f"https://localhost:{port}/",
                    require_https=True,
                    block_private_targets=True,
                ),
                timeout_seconds=5,
            )
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()

    assert raised.value.error_code is V1ErrorCode.INVALID_REQUEST
    assert connected == []


@pytest.mark.anyio
async def test_direct_http_dns_failure_reaches_the_client_as_retryable_unreachable() -> None:
    connector = FakeConnector(
        [FakeHttpResponse(error=httpcore.ConnectError("[Errno 8] nodename nor servname"))]
    )
    app, _ = _app(connector)
    await app.state.pool.start()
    try:
        status, payload = await _post_v1(
            app,
            {
                "cmd": "request.get",
                "url": "https://unknown.example.invalid/api",
                "headers": {"X-Requested-With": "XMLHttpRequest"},
            },
        )
    finally:
        await app.state.pool.close()

    assert status == 500
    assert payload["errorCode"] == "TARGET_UNREACHABLE"
    assert payload["retryable"] is True
