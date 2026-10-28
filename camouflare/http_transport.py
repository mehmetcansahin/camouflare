"""Stateless HTTP transport that never leases a browser.

It serves explicit ``transport: "http"`` and ``request.head`` commands and the
browser path's direct HTTP GET preflight and fallback. One redirect loop, cookie
policy, body decoder, and byte/time budget cover every caller; only the target
policy (HTTPS-only, private-address blocking) differs between them.
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import socket
import ssl
import time
import zlib
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any, Literal, Protocol, TypeAlias, cast
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import Request as URLRequest

import httpcore
import socksio.exceptions

from camouflare.cookie_policy import is_public_suffix
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.limits import MAX_HTTP_REDIRECTS, ResourceLimitError

ALLOWED_URL_SCHEMES = ("http", "https")
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    # Advertise only the content codings the body decoder can remove.
    "Accept-Encoding": "gzip, deflate",
}
DEFAULT_MAX_CONCURRENCY = 8
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHUNK_BYTES = 65_536
_MAX_CONTENT_CODINGS = 4
_KEEPALIVE_EXPIRY_SECONDS = 5.0
_PROXY_POOL_CONNECTIONS = 4
# A target-side SOCKS5 reply means the proxy works but could not reach the target.
_SOCKS_TARGET_FAILURES = (
    "Network unreachable",
    "Host unreachable",
    "Connection refused",
    "TTL expired",
)
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")

BodyMode: TypeAlias = Literal["full", "prefix", "none"]
Resolver: TypeAlias = Callable[[str, int], Awaitable[list[str]]]


class HttpDecodeError(CamouflareError):
    """An HTTP body could not be decoded into the bytes the server encoded."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            error_code=V1ErrorCode.RESPONSE_DECODE_ERROR,
            retryable=False,
        )


@dataclass(frozen=True)
class HttpFetch:
    """One stateless HTTP exchange, including the redirects it may follow."""

    url: str
    method: Literal["GET", "HEAD"] = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    # Lower-cased names removed before any request to a different origin.
    origin_bound_header_names: frozenset[str] = frozenset()
    cookies: list[dict[str, Any]] | None = None
    proxy: Mapping[str, str] | None = None
    max_redirects: int = MAX_HTTP_REDIRECTS
    max_body_bytes: int = 33_554_432
    # "prefix" keeps at most max_body_bytes without validating the unread rest.
    body: BodyMode = "full"
    require_https: bool = False
    # Ignored with a proxy: the proxy resolves and connects to the target.
    block_private_targets: bool = False


@dataclass(frozen=True)
class HttpResult:
    url: str
    status: int
    headers: list[tuple[str, str]]
    message: http.client.HTTPMessage
    body: bytes
    # Whether body is the result of removing the response's content codings.
    content_decoded: bool
    # Playwright add_cookies shape and FlareSolverr solution shape of the jar.
    cookies: list[dict[str, Any]]
    solution_cookies: list[dict[str, Any]]
    user_agent: str | None


@dataclass
class HopProgress:
    """How far one hop got; set by the connector, read when classifying a failure."""

    # True once a proxy has connected the hop to the target: later failures are the
    # target's, earlier ones the proxy's.
    proxy_tunnel_open: bool = False


@dataclass(frozen=True)
class HopRequest:
    method: str
    url: str
    headers: list[tuple[str, str]]
    timeout_seconds: float
    progress: HopProgress = field(default_factory=HopProgress)


@dataclass(frozen=True)
class HopResponse:
    status: int
    headers: list[tuple[str, str]]
    chunks: AsyncIterator[bytes]


HopSender: TypeAlias = Callable[[HopRequest], AbstractAsyncContextManager[HopResponse]]


class HttpConnector(Protocol):
    """Sends one hop at a time; the transport owns redirects, cookies, and bodies."""

    def session(
        self,
        *,
        proxy: Mapping[str, str] | None,
        block_private_targets: bool,
    ) -> AbstractAsyncContextManager[HopSender]: ...

    async def aclose(self) -> None: ...


class HttpTransport:
    """Bounded HTTP transport owned by one application runtime."""

    def __init__(
        self,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        connector: HttpConnector | None = None,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("HTTP transport concurrency must be greater than zero.")
        self._max_concurrency = max_concurrency
        self._connector = connector or HttpcoreConnector(max_connections=max_concurrency)
        self._slots = asyncio.Semaphore(max_concurrency)
        self._active = 0
        self._waiting = 0
        self._accepting = True
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def active_requests(self) -> int:
        return self._active

    def snapshot(self) -> dict[str, int]:
        return {
            "max_concurrency": self._max_concurrency,
            "active_requests": self._active,
            "waiting_requests": self._waiting,
            "available_slots": max(0, self._max_concurrency - self._active),
        }

    async def fetch(
        self,
        request: HttpFetch,
        *,
        timeout_seconds: float,
        wait_for_capacity: bool = True,
    ) -> HttpResult:
        """Send request; without wait_for_capacity, fail at once when no slot is free."""

        deadline = time.monotonic() + max(0.001, timeout_seconds)
        await self._acquire(deadline, wait=wait_for_capacity)
        hops: list[HopProgress] = []
        try:
            try:
                async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                    return await self._fetch(request, deadline, hops)
            except (CamouflareError, ResourceLimitError):
                raise
            except Exception as exc:
                classified = classify_transport_error(
                    exc,
                    proxied=request.proxy is not None,
                    proxy_tunnel_open=bool(hops) and hops[-1].proxy_tunnel_open,
                )
                if classified is None:
                    raise
                raise classified from exc
        finally:
            self._release()

    async def quiesce(self) -> None:
        """Reject new requests while letting in-flight requests finish."""

        self._accepting = False

    async def close(self) -> None:
        """Quiesce, wait for in-flight requests, then close pooled connections."""

        await self.quiesce()
        await self._idle.wait()
        await self._connector.aclose()

    async def _acquire(self, deadline: float, *, wait: bool) -> None:
        if not self._accepting:
            raise _shutting_down_error()
        remaining = deadline - time.monotonic()
        if self._slots.locked() or remaining <= 0:
            # Only a request that actually queues counts as waiting, on every Python
            # version (3.11's wait_for suspends even when a slot is free).
            self._waiting += 1
            try:
                if remaining <= 0 or not wait:
                    raise TimeoutError
                await asyncio.wait_for(self._slots.acquire(), timeout=remaining)
            except TimeoutError as exc:
                raise CamouflareError(
                    "Timed out waiting for HTTP transport capacity.",
                    error_code=V1ErrorCode.POOL_UNAVAILABLE,
                    retryable=True,
                ) from exc
            finally:
                self._waiting -= 1
        else:
            # A free slot is taken without suspending.
            await self._slots.acquire()
        if not self._accepting:
            self._slots.release()
            raise _shutting_down_error()
        self._active += 1
        self._idle.clear()

    def _release(self) -> None:
        self._active -= 1
        self._slots.release()
        if self._active == 0:
            self._idle.set()

    async def _fetch(
        self,
        request: HttpFetch,
        deadline: float,
        hops: list[HopProgress],
    ) -> HttpResult:
        if request.proxy is not None:
            proxy_server_url(request.proxy)
        block_private = request.block_private_targets and request.proxy is None
        cookie_jar = cookie_jar_from_browser_cookies(request.cookies, source_url=request.url)
        current_url = clean_url(request.url)
        _check_target(current_url, request, block_private=block_private, redirected=False)
        target_origin = _url_origin(current_url)
        redirects = 0

        async with self._connector.session(
            proxy=request.proxy,
            block_private_targets=block_private,
        ) as send:
            while True:
                hop_headers = _headers_for_origin(
                    request.headers,
                    origin_bound_header_names=request.origin_bound_header_names,
                    target_origin=target_origin,
                    request_url=current_url,
                )
                # The cookie jar sees the same URL form browser cookies were scoped to.
                cookie_request = URLRequest(quote_url_for_http(current_url), method=request.method)
                if not has_header(hop_headers, "Cookie"):
                    cookie_jar.add_cookie_header(cookie_request)
                    cookie_header = cookie_request.get_header("Cookie")
                    if cookie_header is not None:
                        hop_headers["Cookie"] = str(cookie_header)
                wire_url = _wire_url(current_url)
                host = _bracketed_host_header(wire_url)
                if host is not None and not has_header(hop_headers, "Host"):
                    hop_headers = {"Host": host, **hop_headers}
                hop = HopRequest(
                    method=request.method,
                    url=wire_url,
                    headers=_wire_headers(hop_headers),
                    timeout_seconds=_remaining_seconds(deadline),
                )
                hops.append(hop.progress)
                async with send(hop) as response:
                    message = _header_message(response.headers)
                    _extract_response_cookies(cookie_jar, message, cookie_request)
                    location = message.get("Location")
                    next_url = (
                        _redirect_url(current_url, str(location))
                        if response.status in REDIRECT_STATUSES
                        and location
                        and redirects < request.max_redirects
                        else None
                    )
                    # A redirect past the limit, or to a URL that cannot be requested, is
                    # returned as the response for the caller to judge.
                    if next_url is not None:
                        _check_target(
                            next_url, request, block_private=block_private, redirected=True
                        )
                        current_url = next_url
                        redirects += 1
                        continue
                    body, content_decoded = await _read_response_body(
                        response,
                        message,
                        method=request.method,
                        mode=request.body,
                        maximum_bytes=request.max_body_bytes,
                        deadline=deadline,
                    )
                    return HttpResult(
                        # The percent-encoded URL actually requested, as urllib reported it.
                        url=quote_url_for_http(current_url),
                        status=response.status,
                        headers=list(response.headers),
                        message=message,
                        body=body,
                        content_decoded=content_decoded,
                        cookies=[_cookie_to_browser_cookie(cookie) for cookie in cookie_jar],
                        solution_cookies=[
                            _cookie_to_solution_cookie(cookie) for cookie in cookie_jar
                        ],
                        user_agent=_header_value(request.headers, "User-Agent"),
                    )


class HttpcoreConnector:
    """httpcore-backed hops: shared pools without a proxy, a per-request pool with one."""

    def __init__(
        self,
        *,
        max_connections: int,
        ssl_context: ssl.SSLContext | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        # The system trust store, as the standard-library client used before.
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=self._ssl_context,
            max_connections=max_connections,
            keepalive_expiry=_KEEPALIVE_EXPIRY_SECONDS,
        )
        self._public_pool = httpcore.AsyncConnectionPool(
            ssl_context=self._ssl_context,
            max_connections=max_connections,
            keepalive_expiry=_KEEPALIVE_EXPIRY_SECONDS,
            network_backend=PublicAddressBackend(resolver=resolver),
        )

    @asynccontextmanager
    async def session(
        self,
        *,
        proxy: Mapping[str, str] | None,
        block_private_targets: bool,
    ) -> AsyncGenerator[HopSender]:
        if proxy is None:
            yield _pool_sender(self._public_pool if block_private_targets else self._pool)
            return
        pool = httpcore.AsyncConnectionPool(
            ssl_context=self._ssl_context,
            proxy=httpcore_proxy(proxy),
            max_connections=_PROXY_POOL_CONNECTIONS,
        )
        try:
            yield _pool_sender(pool)
        finally:
            await _close_pool(pool)

    async def aclose(self) -> None:
        await self._pool.aclose()
        await self._public_pool.aclose()


async def _close_pool(pool: httpcore.AsyncConnectionPool) -> None:
    """Close a per-request pool even when the request that owns it is cancelled."""

    closing = asyncio.ensure_future(pool.aclose())
    try:
        await asyncio.shield(closing)
    except asyncio.CancelledError:
        closing.add_done_callback(_consume_task_result)
        raise


def _consume_task_result(task: asyncio.Future[None]) -> None:
    if not task.cancelled():
        task.exception()


class PublicAddressBackend(httpcore.AsyncNetworkBackend):
    """Connects only to globally routable addresses it resolved itself.

    Resolution and connection use the same vetted addresses, so a DNS answer that
    changes between a check and the connect cannot reach a private network. TLS
    still verifies the certificate against the URL hostname.
    """

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver or system_resolver
        # httpcore types AnyIOBackend through an import fallback; it is a network backend.
        self._inner: httpcore.AsyncNetworkBackend = inner or cast(
            httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend()
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            if timeout is None:
                addresses = await self._resolver(host, port)
            else:
                async with asyncio.timeout(timeout):
                    addresses = await self._resolver(host, port)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"Timed out resolving {host}.") from exc
        except OSError as exc:
            raise httpcore.ConnectError(f"Could not resolve {host}: {exc}") from exc
        if not addresses:
            raise httpcore.ConnectError(f"Could not resolve {host}.")
        for address in addresses:
            if not is_public_address(ip_address(address)):
                raise _private_target_error(host)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix socket targets are not allowed.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


async def system_resolver(host: str, port: int) -> list[str]:
    literal = _ip_literal(host)
    if literal is not None:
        return [str(literal)]
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(address: IPv4Address | IPv6Address) -> bool:
    """Whether address is globally routable, including IPv4 embedded in IPv6."""

    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public_address(address.ipv4_mapped)
        if address in _NAT64_NETWORK:
            return is_public_address(IPv4Address(int(address) & 0xFFFFFFFF))
        if address.sixtofour is not None and not is_public_address(address.sixtofour):
            return False
    return address.is_global and not address.is_multicast


def proxy_server_url(proxy: Mapping[str, str]) -> str:
    """Return the proxy URL with a scheme, rejecting proxies the transport cannot use."""

    server = proxy["server"]
    if "://" not in server:
        server = f"http://{server}"
    scheme = urlsplit(server).scheme.lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise CamouflareError(
            "The HTTP transport supports HTTP, HTTPS, and SOCKS5 proxies.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    return server


def httpcore_proxy(proxy: Mapping[str, str]) -> httpcore.Proxy:
    server = proxy_server_url(proxy)
    username = proxy.get("username")
    password = proxy.get("password")
    auth = (
        ((username or "").encode("utf-8"), (password or "").encode("utf-8"))
        if username is not None or password is not None
        else None
    )
    return httpcore.Proxy(url=server, auth=auth)


def classify_transport_error(
    exc: Exception,
    *,
    proxied: bool,
    proxy_tunnel_open: bool = False,
) -> CamouflareError | None:
    """Map a network failure to a public error, or None for an unexpected one.

    Through a proxy, a failure before the proxy connected the target is the proxy's;
    after that it is classified as the target's, like a request without a proxy.
    """

    message = str(exc) or type(exc).__name__
    if isinstance(exc, httpcore.ProxyError):
        return _proxy_error(message)
    if isinstance(exc, httpcore.LocalProtocolError):
        return CamouflareError(
            f"The HTTP request is invalid: {message}",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    # httpcore does not wrap SOCKS handshake parse errors (e.g. a non-SOCKS server).
    if isinstance(exc, socksio.exceptions.SOCKSError) or (
        proxied and not proxy_tunnel_open and isinstance(exc, _NETWORK_FAILURES)
    ):
        return _proxy_phase_error(exc, message)
    if isinstance(exc, (TimeoutError, httpcore.TimeoutException)):
        return CamouflareError(
            "HTTP request timed out before the target responded.",
            error_code=V1ErrorCode.NAVIGATION_TIMEOUT,
            retryable=True,
        )
    if isinstance(exc, httpcore.ConnectError):
        if "CERTIFICATE_VERIFY_FAILED" in message:
            return CamouflareError(
                f"The target TLS certificate could not be verified: {message}",
                error_code=V1ErrorCode.TARGET_UNREACHABLE,
                retryable=False,
            )
        if "SSL" in message or "TLS" in message:
            return CamouflareError(
                f"TLS handshake with the target failed: {message}",
                error_code=V1ErrorCode.TARGET_UNREACHABLE,
                retryable=True,
            )
        return CamouflareError(
            f"Could not connect to the target: {message}",
            error_code=V1ErrorCode.TARGET_UNREACHABLE,
            retryable=True,
        )
    if isinstance(exc, (httpcore.NetworkError, httpcore.RemoteProtocolError)):
        return CamouflareError(
            f"The target connection failed: {message}",
            error_code=V1ErrorCode.TARGET_UNREACHABLE,
            retryable=True,
        )
    return None


_NETWORK_FAILURES = (
    TimeoutError,
    httpcore.TimeoutException,
    httpcore.NetworkError,
    httpcore.RemoteProtocolError,
)


def _proxy_phase_error(exc: Exception, message: str) -> CamouflareError:
    if isinstance(exc, (TimeoutError, httpcore.TimeoutException)):
        return CamouflareError(
            "Timed out connecting through the proxy.",
            error_code=V1ErrorCode.PROXY_CONNECTION_ERROR,
            retryable=True,
        )
    return CamouflareError(
        f"The proxy connection failed: {message}",
        error_code=V1ErrorCode.PROXY_CONNECTION_ERROR,
        # An HTTPS proxy whose certificate fails verification fails the same way again.
        retryable="CERTIFICATE_VERIFY_FAILED" not in message,
    )


def _proxy_error(message: str) -> CamouflareError:
    if any(failure in message for failure in _SOCKS_TARGET_FAILURES) or message[:4] in {
        "502 ",
        "504 ",
    }:
        return CamouflareError(
            f"The proxy could not reach the target: {message}",
            error_code=V1ErrorCode.TARGET_UNREACHABLE,
            retryable=True,
        )
    connect_status = int(message[:3]) if message[:3].isdigit() and message[3:4] == " " else None
    if "username/password" in message.lower() or (
        connect_status is not None and 400 <= connect_status < 500
    ):
        # Rejected credentials or a proxy policy denial fail the same way again.
        return CamouflareError(
            f"The proxy refused the request: {message}",
            error_code=V1ErrorCode.PROXY_CONNECTION_ERROR,
            retryable=False,
        )
    return CamouflareError(
        f"The proxy connection failed: {message}",
        error_code=V1ErrorCode.PROXY_CONNECTION_ERROR,
        retryable=True,
    )


def with_default_headers(headers: Mapping[str, str]) -> dict[str, str]:
    merged = dict(headers)
    for name, value in DEFAULT_HEADERS.items():
        set_default_header(merged, name, value)
    return merged


def clean_url(url: str | None) -> str:
    if not url:
        raise CamouflareError(
            "Request parameter 'url' is mandatory.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    cleaned = url.replace('"', "").strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise CamouflareError(
            "Request parameter 'url' must be a valid absolute http or https URL.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    try:
        parsed = urlsplit(cleaned)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise CamouflareError(
            "Request parameter 'url' must be a valid absolute http or https URL.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        ) from exc
    if scheme not in ALLOWED_URL_SCHEMES:
        raise CamouflareError(
            "Request parameter 'url' must use the http or https scheme.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    if hostname is None or any(character.isspace() for character in hostname):
        raise CamouflareError(
            "Request parameter 'url' must be a valid absolute http or https URL.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    return cleaned


def quote_url_for_http(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe="/%"),
            quote(parts.query, safe="=&?/%+;,:"),
            quote(parts.fragment, safe="=&?/%+;,:"),
        )
    )


def set_default_header(headers: dict[str, str], name: str, value: str) -> None:
    if not has_header(headers, name):
        headers[name] = value


def has_header(headers: Mapping[str, str], name: str) -> bool:
    normalized = name.lower()
    return any(key.lower() == normalized for key in headers)


def _shutting_down_error() -> CamouflareError:
    return CamouflareError(
        "The HTTP transport is shutting down.",
        error_code=V1ErrorCode.POOL_UNAVAILABLE,
        retryable=True,
    )


def _private_target_error(host: str | None) -> CamouflareError:
    return CamouflareError(
        f"Target host {host!r} resolves to a private, loopback, link-local, or "
        "otherwise non-public address.",
        error_code=V1ErrorCode.INVALID_REQUEST,
    )


def _check_target(
    url: str,
    request: HttpFetch,
    *,
    block_private: bool,
    redirected: bool,
) -> None:
    parts = urlsplit(url)
    if request.require_https and parts.scheme.lower() != "https":
        subject = "A redirect target" if redirected else "Request parameter 'url'"
        raise CamouflareError(
            f"{subject} must use https for the HTTP transport.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    if block_private:
        literal = _ip_literal(parts.hostname)
        if literal is not None and not is_public_address(literal):
            raise _private_target_error(parts.hostname)


def _redirect_url(current_url: str, location: str) -> str | None:
    """Return the absolute URL a Location names, or None when it cannot be requested."""

    try:
        return clean_url(urljoin(current_url, location))
    except (CamouflareError, ValueError):
        return None


def _bracketed_host_header(wire_url: str) -> str | None:
    """Return the RFC 9112 Host value for an IPv6-literal URL, which httpcore omits."""

    parts = urlsplit(wire_url)
    host = parts.hostname
    if host is None or ":" not in host:
        return None
    default_port = 443 if parts.scheme.lower() == "https" else 80
    port = parts.port
    return f"[{host}]" if port is None or port == default_port else f"[{host}]:{port}"


def _ip_literal(host: str | None) -> IPv4Address | IPv6Address | None:
    if not host:
        return None
    try:
        return ip_address(host.strip("[]"))
    except ValueError:
        return None


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("HTTP request exceeded the request deadline.")
    return remaining


def _wire_url(url: str) -> str:
    """Return the ASCII request-target URL, with an IDNA-encoded hostname."""

    quoted = urlsplit(quote_url_for_http(url))
    hostname = quoted.hostname or ""
    if hostname.isascii():
        return urlunsplit(quoted)
    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise CamouflareError(
            "Request parameter 'url' has an invalid internationalized hostname.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        ) from exc
    netloc = ascii_host if quoted.port is None else f"{ascii_host}:{quoted.port}"
    if quoted.username is not None:
        userinfo = quoted.username
        if quoted.password is not None:
            userinfo = f"{userinfo}:{quoted.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit(quoted._replace(netloc=netloc))


def _wire_headers(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    for name, value in headers.items():
        try:
            value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise CamouflareError(
                f"Request header {name!r} must contain only Latin-1 characters.",
                error_code=V1ErrorCode.INVALID_REQUEST,
            ) from exc
    return list(headers.items())


def _pool_sender(pool: httpcore.AsyncConnectionPool) -> HopSender:
    @asynccontextmanager
    async def send(hop: HopRequest) -> AsyncGenerator[HopResponse]:
        timeout = max(0.001, hop.timeout_seconds)
        # Sockets this hop opened. httpcore leaves one open when a TLS or SOCKS handshake
        # on it is interrupted, for example by the request deadline.
        opened: list[httpcore.AsyncNetworkStream] = []
        request_sent = False

        async def trace(event: str, info: dict[str, Any]) -> None:
            nonlocal request_sent
            if event.endswith(".connect_tcp.complete") and info.get("return_value") is not None:
                opened.append(info["return_value"])
            if event.endswith(".send_request_headers.started") and (
                getattr(info.get("request"), "method", b"") != b"CONNECT"
            ):
                request_sent = True
            # A completed SOCKS handshake, a CONNECT tunnel upgrading to TLS, or the
            # request itself going out means the proxy has reached the target.
            if request_sent or event in _PROXY_TUNNEL_OPEN_EVENTS:
                hop.progress.proxy_tunnel_open = True

        try:
            async with pool.stream(
                hop.method,
                hop.url,
                headers=[
                    (name.encode("ascii"), value.encode("latin-1")) for name, value in hop.headers
                ],
                extensions={
                    "timeout": {
                        "connect": timeout,
                        "read": timeout,
                        "write": timeout,
                        "pool": timeout,
                    },
                    "trace": trace,
                },
            ) as response:
                yield HopResponse(
                    status=response.status,
                    headers=[
                        (name.decode("latin-1"), value.decode("latin-1"))
                        for name, value in response.headers
                    ],
                    chunks=response.aiter_stream(),
                )
        except BaseException:
            # A connection that never carried this request is unusable; once it did,
            # httpcore owns and closes it.
            if not request_sent:
                for stream in opened:
                    await _close_quietly(stream.aclose())
            raise

    return send


async def _close_quietly(closing: Awaitable[None]) -> None:
    """Finish a best-effort close even when the caller is being cancelled."""

    task = asyncio.ensure_future(closing)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_consume_task_result)
        raise
    except Exception:
        return


_PROXY_TUNNEL_OPEN_EVENTS = frozenset(
    {"socks.setup_socks5_connection.complete", "proxy.start_tls.started"}
)


def _header_message(headers: Iterable[tuple[str, str]]) -> http.client.HTTPMessage:
    message = http.client.HTTPMessage()
    for name, value in headers:
        # Message item assignment appends, so repeated fields such as Set-Cookie stay.
        message[name] = value
    return message


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    normalized = name.lower()
    for header_name, value in headers.items():
        if header_name.lower() == normalized:
            return value
    return None


async def _read_response_body(
    response: HopResponse,
    message: http.client.HTTPMessage,
    *,
    method: str,
    mode: BodyMode,
    maximum_bytes: int,
    deadline: float,
) -> tuple[bytes, bool]:
    if method == "HEAD" or mode == "none" or response.status in (204, 304):
        return b"", False
    prefix_only = mode == "prefix"
    decoders = _content_decoders(_response_content_codings(message))
    if not prefix_only:
        _reject_declared_oversize(message, maximum_bytes)
    # Read one byte past the limit at most, so an oversized body is detected
    # without buffering the remainder.
    wire: list[bytes] = []
    received = 0
    async for chunk in response.chunks:
        if not chunk:
            continue
        wire.append(chunk)
        received += len(chunk)
        if received > maximum_bytes:
            break
    body = _decode_body(
        wire,
        decoders,
        maximum_bytes=maximum_bytes,
        deadline=deadline,
        prefix_only=prefix_only,
    )
    return body, bool(decoders)


def _reject_declared_oversize(message: http.client.HTTPMessage, maximum_bytes: int) -> None:
    declared = str(message.get("Content-Length") or "").strip()
    if declared.isdigit() and int(declared) > maximum_bytes:
        raise ResourceLimitError(
            f"Response body exceeds the configured {maximum_bytes}-byte limit."
        )


def _decode_body(
    wire_chunks: Iterable[bytes],
    decoders: list[_ContentDecoder],
    *,
    maximum_bytes: int,
    deadline: float,
    prefix_only: bool,
) -> bytes:
    """Remove content codings within byte and time bounds.

    Wire bytes, the output of every decoding layer, and the final body are each
    limited to ``maximum_bytes``. A full read raises ``ResourceLimitError`` past
    that limit and rejects truncated or malformed encodings. ``prefix_only``
    instead stops at the limit and returns the decoded prefix without integrity
    checks for the remainder it deliberately did not read.
    """

    wire = _limited_body_chunks(
        iter(wire_chunks),
        maximum_bytes=maximum_bytes,
        label="Response body",
        prefix_only=prefix_only,
    )
    parts: list[bytes] = []
    try:
        decoded: Iterator[bytes] = wire
        for decoder in decoders:
            decoded = _limited_body_chunks(
                _decoded_body_chunks(decoder, decoded, deadline=deadline),
                maximum_bytes=maximum_bytes,
                label="Decoded response body",
                prefix_only=prefix_only,
            )
        for chunk in decoded:
            parts.append(chunk)
    except _BodyPrefixComplete:
        pass
    return b"".join(parts)


class _BodyPrefixComplete(Exception):
    """Internal signal: a prefix-only body read has every byte it may inspect."""


def _ensure_deadline(deadline: float) -> None:
    if deadline - time.monotonic() <= 0:
        raise TimeoutError("HTTP request exceeded the request deadline.")


def _limited_body_chunks(
    chunks: Iterable[bytes],
    *,
    maximum_bytes: int,
    label: str,
    prefix_only: bool,
) -> Iterator[bytes]:
    received = 0
    for chunk in chunks:
        available = maximum_bytes - received
        if len(chunk) > available:
            if not prefix_only:
                raise ResourceLimitError(
                    f"{label} exceeds the configured {maximum_bytes}-byte limit."
                )
            if available:
                yield chunk[:available]
            raise _BodyPrefixComplete
        received += len(chunk)
        yield chunk


def _decoded_body_chunks(
    decoder: _ContentDecoder,
    chunks: Iterable[bytes],
    *,
    deadline: float,
) -> Iterator[bytes]:
    for data in chunks:
        while True:
            _ensure_deadline(deadline)
            try:
                output = decoder.decompress(data)
            except zlib.error as exc:
                raise HttpDecodeError(
                    f"HTTP response body has malformed {decoder.coding} content encoding."
                ) from exc
            data = b""
            if output:
                yield output
            if not decoder.output_pending:
                break
    decoder.finish()


def _response_content_codings(headers: http.client.HTTPMessage) -> list[str]:
    """Return non-identity content codings in the order the server applied them."""

    values = [str(value) for value in headers.get_all("Content-Encoding") or ()]
    codings: list[str] = []
    for value in values:
        for token in value.split(","):
            coding = token.strip().lower()
            if coding and coding != "identity":
                codings.append("gzip" if coding == "x-gzip" else coding)
    return codings


def _content_decoders(codings: list[str]) -> list[_ContentDecoder]:
    """Create decoders in removal order: the last coding applied comes off first."""

    if len(codings) > _MAX_CONTENT_CODINGS:
        raise HttpDecodeError(
            f"HTTP response uses more than {_MAX_CONTENT_CODINGS} content encodings."
        )
    decoders: list[_ContentDecoder] = []
    for coding in reversed(codings):
        if coding == "gzip":
            decoders.append(_GzipDecoder())
        elif coding == "deflate":
            decoders.append(_DeflateDecoder())
        else:
            raise HttpDecodeError(
                f"HTTP response uses unsupported content encoding {coding[:64]!r}."
            )
    return decoders


class _GzipDecoder:
    """Incremental gzip decoder that accepts concatenated members (RFC 1952)."""

    coding = "gzip"

    def __init__(self) -> None:
        self._member: Any = None
        self._input = b""
        self._members_completed = 0
        self.output_pending = False

    def decompress(self, data: bytes) -> bytes:
        """Return at most one chunk; call again with no data while output_pending."""

        self._input += data
        output = bytearray()
        while len(output) < _CHUNK_BYTES:
            if self._member is None:
                if self._members_completed:
                    # Like gzip.decompress, accept NUL padding after a complete member.
                    self._input = self._input.lstrip(b"\x00")
                if not self._input:
                    break
                self._member = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            output += self._member.decompress(self._input, _CHUNK_BYTES - len(output))
            if self._member.eof:
                self._input = self._member.unused_data
                self._member = None
                self._members_completed += 1
                continue
            self._input = self._member.unconsumed_tail
            if len(output) < _CHUNK_BYTES:
                break
        self.output_pending = len(output) >= _CHUNK_BYTES
        return bytes(output)

    def finish(self) -> None:
        if self._members_completed == 0 or self._member is not None or self._input:
            raise HttpDecodeError("HTTP response body ended inside its gzip content encoding.")


_DEFLATE_TRAILING_DATA_MESSAGE = (
    "HTTP response body has data after the end of its deflate content encoding."
)


class _DeflateDecoder:
    """Incremental HTTP deflate decoder for zlib-wrapped or raw RFC 1951 data."""

    coding = "deflate"

    def __init__(self) -> None:
        self._stream: Any = None
        self._input = b""
        self.output_pending = False

    def decompress(self, data: bytes) -> bytes:
        """Return at most one chunk; call again with no data while output_pending."""

        self._input += data
        self.output_pending = False
        if self._stream is None:
            if len(self._input) < 2:
                return b""
            # HTTP deflate is zlib-wrapped (RFC 9110), but some servers send raw deflate.
            self._stream = zlib.decompressobj(
                wbits=zlib.MAX_WBITS if _has_zlib_header(self._input) else -zlib.MAX_WBITS
            )
        if self._stream.eof:
            if self._input:
                raise HttpDecodeError(_DEFLATE_TRAILING_DATA_MESSAGE)
            return b""
        output: bytes = self._stream.decompress(self._input, _CHUNK_BYTES)
        if self._stream.eof:
            if self._stream.unused_data:
                raise HttpDecodeError(_DEFLATE_TRAILING_DATA_MESSAGE)
            self._input = b""
        else:
            self._input = self._stream.unconsumed_tail
            self.output_pending = len(output) >= _CHUNK_BYTES
        return output

    def finish(self) -> None:
        if self._input or self._stream is None or not self._stream.eof:
            raise HttpDecodeError("HTTP response body ended inside its deflate content encoding.")


_ContentDecoder: TypeAlias = _GzipDecoder | _DeflateDecoder


def _has_zlib_header(data: bytes) -> bool:
    """Whether data starts with an RFC 1950 header: CM=8, CINFO<=7, valid FCHECK."""

    cmf, flags = data[0], data[1]
    return cmf & 0x0F == 8 and cmf >> 4 <= 7 and ((cmf << 8) | flags) % 31 == 0


def _headers_for_origin(
    headers: Mapping[str, str],
    *,
    origin_bound_header_names: frozenset[str],
    target_origin: tuple[str, str, int | None],
    request_url: str,
) -> dict[str, str]:
    if _safe_url_origin(request_url) == target_origin:
        return dict(headers)
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in origin_bound_header_names
    }


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    default_port = 443 if parts.scheme.lower() == "https" else 80
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or default_port


def _safe_url_origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        return _url_origin(url)
    except ValueError:
        return None


def _extract_response_cookies(
    cookie_jar: CookieJar,
    headers: http.client.HTTPMessage,
    request: URLRequest,
) -> None:
    existing_cookies = {(cookie.domain, cookie.path, cookie.name): cookie for cookie in cookie_jar}
    cookie_jar.extract_cookies(cast(Any, _CookieResponse(headers)), request)
    source = urlsplit(request.full_url)
    secure_source = source.scheme.lower() == "https"
    source_is_ip = _is_ip_address(source.hostname)
    for cookie in list(cookie_jar):
        key = (cookie.domain, cookie.path, cookie.name)
        if existing_cookies.get(key) is cookie:
            # Security attributes on cookies from earlier HTTPS hops were
            # already checked against their own response origin.
            continue
        same_site = _cookie_nonstandard_attr(cookie, "SameSite")
        invalid_prefix = (cookie.name.startswith("__Secure-") and not cookie.secure) or (
            cookie.name.startswith("__Host-")
            and (not cookie.secure or cookie.domain_specified or cookie.path != "/")
        )
        if (
            # A standalone top-level client has no browser partition
            # context; treating CHIPS cookies as unpartitioned broadens scope.
            _cookie_has_nonstandard_attr(cookie, "Partitioned")
            or (cookie.domain_specified and is_public_suffix(cookie.domain))
            # Browsers do not accept Domain cookies from IP-literal hosts.
            # Python's CookieJar otherwise treats suffixes such as ``0.0.1``
            # as matching multiple unrelated IPv4 addresses.
            or (cookie.domain_specified and source_is_ip)
            or (cookie.secure and not secure_source)
            or (same_site is not None and str(same_site).casefold() == "none" and not cookie.secure)
            or invalid_prefix
        ):
            cookie_jar.clear(cookie.domain, cookie.path, cookie.name)
            previous_cookie = existing_cookies.get(key)
            if previous_cookie is not None:
                cookie_jar.set_cookie(previous_cookie)


def _is_ip_address(hostname: str | None) -> bool:
    if not hostname:
        return False
    try:
        ip_address(hostname)
    except ValueError:
        return False
    return True


def _cookie_nonstandard_attr(cookie: Cookie, name: str) -> Any | None:
    normalized_name = name.casefold()
    for attribute_name, value in getattr(cookie, "_rest", {}).items():
        if str(attribute_name).casefold() == normalized_name:
            return value
    return None


def _cookie_has_nonstandard_attr(cookie: Cookie, name: str) -> bool:
    normalized_name = name.casefold()
    return any(
        str(attribute_name).casefold() == normalized_name
        for attribute_name in getattr(cookie, "_rest", {})
    )


class _CookieResponse:
    def __init__(self, headers: http.client.HTTPMessage) -> None:
        self._headers = headers

    def info(self) -> http.client.HTTPMessage:
        return self._headers


def cookie_jar_from_browser_cookies(
    cookies: list[dict[str, Any]] | None,
    *,
    source_url: str | None = None,
) -> CookieJar:
    cookie_jar = CookieJar(
        policy=DefaultCookiePolicy(
            strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain,
        )
    )
    source_is_ip = (
        _is_ip_address(urlsplit(clean_url(source_url)).hostname)
        if source_url is not None
        else False
    )
    for browser_cookie in cookies or []:
        cookie = _browser_cookie_to_cookie(browser_cookie)
        if (
            cookie is not None
            and not cookie.is_expired()
            and not (cookie.domain_specified and is_public_suffix(cookie.domain))
            and not (cookie.domain_specified and source_is_ip)
        ):
            cookie_jar.set_cookie(cookie)
    return cookie_jar


def _browser_cookie_to_cookie(browser_cookie: Mapping[str, Any]) -> Cookie | None:
    if browser_cookie.get("partitionKey"):
        return None
    name = browser_cookie.get("name")
    value = browser_cookie.get("value")
    if name is None or value is None:
        return None

    cookie_url = browser_cookie.get("url")
    if cookie_url:
        url_parts = urlsplit(str(cookie_url))
        domain = url_parts.hostname or ""
        domain_specified = False
        domain_initial_dot = False
    else:
        domain = str(browser_cookie.get("domain") or "")
        domain_specified = bool(domain)
        domain_initial_dot = domain.startswith(".")
    if not domain:
        return None

    expires_value = browser_cookie.get("expires")
    expires: int | None
    if expires_value is None or float(expires_value) == -1:
        expires = None
    else:
        expires = int(float(expires_value))
    rest: dict[str, Any] = {}
    if browser_cookie.get("httpOnly"):
        rest["HttpOnly"] = None
    if browser_cookie.get("sameSite"):
        rest["SameSite"] = str(browser_cookie["sameSite"])

    return Cookie(
        version=0,
        name=str(name),
        value=str(value),
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=domain_specified,
        domain_initial_dot=domain_initial_dot,
        path=str(browser_cookie.get("path") or "/"),
        path_specified=browser_cookie.get("path") is not None,
        secure=bool(browser_cookie.get("secure", False)),
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest=rest,
        rfc2109=False,
    )


def _cookie_to_browser_cookie(cookie: Cookie) -> dict[str, Any]:
    browser_cookie: dict[str, Any] = {
        "name": cookie.name,
        "value": cookie.value,
        "secure": cookie.secure,
        "httpOnly": _cookie_has_nonstandard_attr(cookie, "HttpOnly"),
    }
    if cookie.domain_specified:
        browser_cookie["domain"] = cookie.domain
        browser_cookie["path"] = cookie.path
    else:
        scheme = "https" if cookie.secure else "http"
        path = cookie.path or "/"
        scope_path = f"{path.rstrip('/')}/__camouflare_cookie_scope__"
        browser_cookie["url"] = f"{scheme}://{cookie.domain}{scope_path}"
    if cookie.expires is not None:
        browser_cookie["expires"] = float(cookie.expires)
    same_site = _canonical_same_site(_cookie_nonstandard_attr(cookie, "SameSite"))
    if same_site is not None:
        browser_cookie["sameSite"] = same_site
    return browser_cookie


def _cookie_to_solution_cookie(cookie: Cookie) -> dict[str, Any]:
    solution_cookie: dict[str, Any] = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "secure": cookie.secure,
        "httpOnly": _cookie_has_nonstandard_attr(cookie, "HttpOnly"),
    }
    if cookie.expires is not None:
        solution_cookie["expires"] = float(cookie.expires)
        solution_cookie["expiry"] = cookie.expires
    same_site = _canonical_same_site(_cookie_nonstandard_attr(cookie, "SameSite"))
    if same_site is not None:
        solution_cookie["sameSite"] = same_site
    return solution_cookie


def _canonical_same_site(value: Any | None) -> str | None:
    if value is None:
        return None
    return {"strict": "Strict", "lax": "Lax", "none": "None"}.get(str(value).casefold())
