from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from http import HTTPStatus
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from ipaddress import ip_address
from threading import BoundedSemaphore, Lock, Thread
from typing import Any, TypeAlias, cast
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    OpenerDirector,
)
from urllib.request import (
    Request as URLRequest,
)

from camouflare.challenge import content_has_challenge_markers
from camouflare.cookie_policy import is_public_suffix
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.limits import (
    ResourceLimitError,
    ResourceLimits,
    ensure_bytes_size,
    ensure_text_size,
)
from camouflare.metrics import record_browser_transport_error
from camouflare.models import V1Request
from camouflare.protocols import BrowserContextLike, PageLike, ResponseLike
from camouflare.solution import is_best_effort_browser_error, response_charset
from camouflare.timer import TimeoutTimer

ALLOWED_URL_SCHEMES = ("http", "https")
DOMCONTENTLOADED_NAVIGATION_TIMEOUT_MS = 15000
DIRECT_HTTP_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}
DIRECT_HTTP_MAX_REDIRECTS = 10
DIRECT_HTTP_MAX_WORKERS = 4
DIRECT_HTTP_CHALLENGE_PROBE_BYTES = 65_536
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

logger = logging.getLogger(__name__)
_ACTIVE_LIMITS: ContextVar[ResourceLimits | None] = ContextVar(
    "camouflare_active_resource_limits",
    default=None,
)


class RawResponse:
    raw_body = True

    def __init__(
        self,
        *,
        url: str,
        status: int,
        headers: dict[str, str],
        body: str,
        cookies: list[dict[str, Any]] | None = None,
        solution_cookies: list[dict[str, Any]] | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.headers = headers
        self._body = body
        self.cookies = cookies or []
        self.solution_cookies = solution_cookies if solution_cookies is not None else self.cookies
        self.user_agent = user_agent
        self.fallback_used = False

    async def text(self) -> str:
        return self._body


NavigationResponse: TypeAlias = ResponseLike | RawResponse | None
DirectHttpGet: TypeAlias = Callable[[str, V1Request, TimeoutTimer], Awaitable[RawResponse]]


class DirectHttpExecutor:
    """Bounded direct-HTTP workers owned by one application runtime."""

    def __init__(self, *, max_workers: int = DIRECT_HTTP_MAX_WORKERS) -> None:
        if max_workers <= 0:
            raise ValueError("Direct HTTP worker count must be greater than zero.")
        self._worker_slots = BoundedSemaphore(max_workers)
        self._state_lock = Lock()
        self._workers: set[ConcurrentFuture[RawResponse]] = set()
        self._accepting = True
        self._thread_sequence = 0

    @property
    def accepting(self) -> bool:
        with self._state_lock:
            return self._accepting

    @property
    def active_workers(self) -> int:
        with self._state_lock:
            return len(self._workers)

    async def get(
        self,
        url: str,
        request: V1Request,
        timer: TimeoutTimer,
    ) -> RawResponse:
        limits = _ACTIVE_LIMITS.get() or ResourceLimits()
        deadline = time.monotonic() + timer.remaining_seconds
        await self._acquire_worker_slot(deadline)
        try:
            with self._state_lock:
                if not self._accepting:
                    raise RuntimeError("Direct HTTP executor is quiesced.")
                worker = self._start_worker(
                    _direct_http_get_sync,
                    url,
                    request,
                    max(0.001, deadline - time.monotonic()),
                    limits.response_body_bytes,
                )
                self._workers.add(worker)
        except BaseException:
            self._worker_slots.release()
            raise

        worker.add_done_callback(self._worker_done)
        result = asyncio.wrap_future(worker)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.add_done_callback(_consume_direct_http_result)
                raise TimeoutError("Direct HTTP request exceeded the request deadline.")
            done, _ = await asyncio.wait({result}, timeout=remaining)
            if not done:
                result.add_done_callback(_consume_direct_http_result)
                raise TimeoutError("Direct HTTP request exceeded the request deadline.")
            return result.result()
        except asyncio.CancelledError:
            result.add_done_callback(_consume_direct_http_result)
            raise

    async def quiesce(self) -> None:
        """Reject new submissions while allowing already-running work to drain."""

        with self._state_lock:
            self._accepting = False

    async def close(self) -> None:
        """Quiesce submissions and wait asynchronously for running workers."""

        await self.quiesce()
        while self.active_workers:
            await asyncio.sleep(0.01)

    def _start_worker(
        self,
        function: Callable[..., RawResponse],
        *args: Any,
    ) -> ConcurrentFuture[RawResponse]:
        """Start one daemon worker; the semaphore guarantees there is no queue."""

        future: ConcurrentFuture[RawResponse] = ConcurrentFuture()
        self._thread_sequence += 1

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                response = function(*args)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(response)

        thread = Thread(
            target=run,
            name=f"camouflare-direct-http-{self._thread_sequence}",
            daemon=True,
        )
        thread.start()
        return future

    async def _acquire_worker_slot(self, deadline: float) -> None:
        while True:
            with self._state_lock:
                if not self._accepting:
                    raise RuntimeError("Direct HTTP executor is quiesced.")
            if self._worker_slots.acquire(blocking=False):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Direct HTTP worker capacity was unavailable before the deadline."
                )
            await asyncio.sleep(min(0.01, remaining))

    def _worker_done(self, worker: ConcurrentFuture[RawResponse]) -> None:
        with self._state_lock:
            self._workers.discard(worker)
        self._worker_slots.release()


_ACTIVE_DIRECT_HTTP_EXECUTOR: ContextVar[DirectHttpExecutor | None] = ContextVar(
    "camouflare_active_direct_http_executor",
    default=None,
)


@contextmanager
def active_direct_http_executor(executor: DirectHttpExecutor):
    token: Token[DirectHttpExecutor | None] = _ACTIVE_DIRECT_HTTP_EXECUTOR.set(executor)
    try:
        yield
    finally:
        _ACTIVE_DIRECT_HTTP_EXECUTOR.reset(token)


def _build_http_opener() -> OpenerDirector:
    # Deliberately omit FileHandler/FTPHandler/DataHandler (and ProxyHandler):
    # the fallback is restricted to HTTP(S), including across redirects.
    opener = OpenerDirector()
    # Redirects are deliberately handled in _direct_http_get_sync so that
    # credentials can be removed before a cross-origin follow-up request and
    # every hop shares one deadline.
    for handler in (HTTPHandler, HTTPSHandler):
        opener.add_handler(handler())
    return opener


_HTTP_OPENER = _build_http_opener()


@contextmanager
def active_resource_limits(limits: ResourceLimits):
    token: Token[ResourceLimits | None] = _ACTIVE_LIMITS.set(limits)
    try:
        yield
    finally:
        _ACTIVE_LIMITS.reset(token)


async def navigate_get(
    page: PageLike,
    request: V1Request,
    timer: TimeoutTimer,
    limits: ResourceLimits,
    *,
    allow_direct_http_fallback: bool = True,
    allow_direct_http_first: bool = True,
    direct_http_get: DirectHttpGet | None = None,
) -> NavigationResponse:
    fetch_direct = direct_http_get or _direct_http_get
    url = clean_url(request.url)
    if origin_bound_target_headers(request):
        if not allow_direct_http_first:
            raise CamouflareError(
                "Non-User-Agent headers on request.get require a stateless request "
                "without a proxy.",
                error_code=V1ErrorCode.INVALID_REQUEST,
            )
        if request.return_screenshot and not request.return_only_cookies:
            raise CamouflareError(
                "returnScreenshot is not supported when request.get uses origin-bound headers.",
                error_code=V1ErrorCode.INVALID_REQUEST,
            )
        direct_response = await fetch_direct(url, request, timer)
        await _import_direct_response_cookies(page, direct_response)
        return direct_response
    if allow_direct_http_first and should_try_direct_get_first(request):
        direct_response = await try_direct_http_get_first(
            url,
            request,
            timer,
            page=page,
            direct_http_get=fetch_direct,
        )
        if direct_response is not None:
            return direct_response

    try:
        return await page.goto(
            url,
            timeout=min(timer.remaining_ms, DOMCONTENTLOADED_NAVIGATION_TIMEOUT_MS),
            wait_until="domcontentloaded",
        )
    except Exception as exc:
        if is_timeout_error(exc):
            logger.info(
                "Navigation timed out before domcontentloaded; waiting for commit.",
                extra={"target": safe_log_url(url), "error": type(exc).__name__},
            )
            try:
                await page.wait_for_url(
                    lambda current_url: (
                        urlsplit(str(current_url)).scheme.lower() in ALLOWED_URL_SCHEMES
                    ),
                    wait_until="commit",
                    timeout=timer.remaining_ms,
                )
                return None
            except Exception as commit_exc:
                if (
                    allow_direct_http_fallback
                    and is_timeout_error(commit_exc)
                    and should_try_direct_get_after_navigation_timeout(request)
                ):
                    logger.info(
                        "Navigation timed out before commit; trying direct HTTP fallback.",
                        extra={
                            "target": safe_log_url(url),
                            "error": type(commit_exc).__name__,
                        },
                    )
                    direct_response = await try_direct_http_get_after_navigation_timeout(
                        url,
                        request,
                        timer,
                        page=page,
                        direct_http_get=fetch_direct,
                    )
                    if direct_response is not None:
                        return direct_response
                if is_best_effort_browser_error(commit_exc):
                    if allow_direct_http_fallback:
                        logger.info(
                            "Browser transport closed during GET navigation; "
                            "falling back to direct HTTP.",
                            extra={
                                "target": safe_log_url(url),
                                "error": type(commit_exc).__name__,
                            },
                        )
                        return await _fallback_after_browser_transport(
                            url,
                            request,
                            timer,
                            browser_error=commit_exc,
                            fetch_direct=fetch_direct,
                            page=page,
                        )
                    _emit_browser_transport_error(commit_exc, fallback_used=False)
                raise
        if is_best_effort_browser_error(exc):
            if allow_direct_http_fallback:
                logger.info(
                    "Browser transport closed during GET navigation; falling back to direct HTTP.",
                    extra={"target": safe_log_url(url), "error": type(exc).__name__},
                )
                return await _fallback_after_browser_transport(
                    url,
                    request,
                    timer,
                    browser_error=exc,
                    fetch_direct=fetch_direct,
                    page=page,
                )
            _emit_browser_transport_error(exc, fallback_used=False)
        raise


def _emit_browser_transport_error(exc: Exception, *, fallback_used: bool) -> None:
    record_browser_transport_error("navigation")
    logger.warning(
        "Browser transport error.",
        extra={
            "phase": "navigation",
            "error_type": type(exc).__name__,
            "browser_state": None,
            "slot_uses": None,
            "slot_active_contexts": None,
            "retire_reason": None,
            "fallback_used": fallback_used,
        },
    )


def _mark_direct_http_fallback(response: Any) -> Any:
    response.fallback_used = True
    return response


async def _fallback_after_browser_transport(
    url: str,
    request: V1Request,
    timer: TimeoutTimer,
    *,
    browser_error: Exception,
    fetch_direct: DirectHttpGet,
    page: PageLike,
) -> NavigationResponse:
    try:
        response = await fetch_direct(url, request, timer)
    except ResourceLimitError:
        _emit_browser_transport_error(browser_error, fallback_used=False)
        raise
    except Exception as fallback_error:
        logger.info(
            "Direct HTTP fallback after browser transport failure failed; "
            "preserving browser transport error.",
            extra={"target": safe_log_url(url), "error": type(fallback_error).__name__},
        )
        _emit_browser_transport_error(browser_error, fallback_used=False)
        raise browser_error from fallback_error
    try:
        await _import_direct_response_cookies(page, response)
    except Exception as cookie_error:
        # The browser context may be the component whose transport just died.
        # Keep the successful direct response; solution collection can fall back
        # to the response's cookie jar when the context remains unavailable.
        logger.info(
            "Could not import direct HTTP cookies into the failed browser context.",
            extra={"target": safe_log_url(url), "error": type(cookie_error).__name__},
        )
    _emit_browser_transport_error(browser_error, fallback_used=True)
    return _mark_direct_http_fallback(response)


async def try_direct_http_get_first(
    url: str,
    request: V1Request,
    timer: TimeoutTimer,
    *,
    page: PageLike | None = None,
    direct_http_get: DirectHttpGet | None = None,
) -> RawResponse | None:
    fetch_direct = direct_http_get or _direct_http_get
    try:
        response = await fetch_direct(url, request, timer)
    except ResourceLimitError:
        raise
    except Exception as exc:
        logger.info(
            "Direct HTTP GET preflight failed; falling back to browser navigation.",
            extra={"target": safe_log_url(url), "error": type(exc).__name__},
        )
        return None

    if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
        logger.info(
            "Direct HTTP GET preflight returned a non-2xx response; "
            "falling back to browser navigation.",
            extra={"target": safe_log_url(url), "status": response.status},
        )
        return None
    body = await response.text()
    if content_has_challenge_markers(body):
        logger.info(
            "Direct HTTP GET preflight returned challenge HTML; "
            "falling back to browser navigation.",
            extra={"target": safe_log_url(url), "status": response.status},
        )
        return None
    try:
        await _import_direct_response_cookies(page, response)
    except Exception as exc:
        logger.info(
            "Direct HTTP GET cookie import failed; falling back to browser navigation.",
            extra={"target": safe_log_url(url), "error": type(exc).__name__},
        )
        return None
    return response


async def try_direct_http_get_after_navigation_timeout(
    url: str,
    request: V1Request,
    timer: TimeoutTimer,
    *,
    page: PageLike | None = None,
    direct_http_get: DirectHttpGet | None = None,
) -> RawResponse | None:
    fetch_direct = direct_http_get or _direct_http_get
    try:
        response = await fetch_direct(url, request, timer)
    except ResourceLimitError:
        raise
    except Exception as exc:
        logger.info(
            "Direct HTTP GET fallback after navigation timeout failed; "
            "preserving browser navigation error.",
            extra={"target": safe_log_url(url), "error": type(exc).__name__},
        )
        return None

    if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
        logger.info(
            "Direct HTTP GET fallback after navigation timeout returned a non-2xx response; "
            "preserving browser navigation error.",
            extra={"target": safe_log_url(url), "status": response.status},
        )
        return None
    body = await response.text()
    if content_has_challenge_markers(body):
        logger.info(
            "Direct HTTP GET fallback after navigation timeout returned challenge HTML; "
            "preserving browser navigation error.",
            extra={"target": safe_log_url(url), "status": response.status},
        )
        return None
    try:
        await _import_direct_response_cookies(page, response)
    except Exception as exc:
        logger.info(
            "Direct HTTP GET cookie import after navigation timeout failed; "
            "preserving browser navigation error.",
            extra={"target": safe_log_url(url), "error": type(exc).__name__},
        )
        return None
    return _mark_direct_http_fallback(response)


async def _import_direct_response_cookies(
    page: PageLike | None,
    response: RawResponse,
) -> None:
    cookies = getattr(response, "cookies", None)
    if page is None or not cookies:
        return
    context = getattr(page, "context", None)
    add_cookies = getattr(context, "add_cookies", None)
    if callable(add_cookies):
        await cast(
            Callable[[list[dict[str, Any]]], Awaitable[None]],
            add_cookies,
        )(cookies)


def should_try_direct_get_first(request: V1Request) -> bool:
    if request.return_screenshot or request.wait_in_seconds or request.cookies:
        return False
    query = urlsplit(clean_url(request.url)).query.lower()
    return "ajax=true" in query.split("&")


def should_try_direct_get_after_navigation_timeout(request: V1Request) -> bool:
    return should_try_direct_get_first(request)


def is_timeout_error(exc: Exception) -> bool:
    class_name = type(exc).__name__.lower()
    message = str(exc).lower()
    return "timeout" in class_name or "timed out" in message or "timeout" in message


async def wait_networkidle_best_effort(page: PageLike, timer: TimeoutTimer) -> None:
    timeout = min(5000, timer.remaining_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        return


async def submit_post(
    context: BrowserContextLike,
    page: PageLike,
    request: V1Request,
    timer: TimeoutTimer,
    limits: ResourceLimits,
    *,
    on_request_started: Callable[[], None] | None = None,
) -> NavigationResponse:
    if request.return_screenshot and not request.return_only_cookies:
        raise CamouflareError(
            "returnScreenshot is not supported for request.post because POST "
            "responses are not loaded into a browser page.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    response = await post_with_context_request(
        context,
        request,
        timer,
        limits,
        on_request_started=on_request_started,
    )
    if response is not None:
        # A POST response is terminal even when it contains challenge HTML. Replaying
        # the business request through a second browser path could duplicate effects.
        # Consumers receive the first response and decide whether another attempt is safe.
        return response
    raise CamouflareError(
        "request.post requires a browser context request transport so redirects "
        "can remain disabled.",
        error_code=V1ErrorCode.INVALID_REQUEST,
    )


async def post_with_context_request(
    context: BrowserContextLike,
    request: V1Request,
    timer: TimeoutTimer,
    limits: ResourceLimits,
    *,
    on_request_started: Callable[[], None] | None = None,
) -> RawResponse | None:
    api_request = getattr(context, "request", None)
    post = getattr(api_request, "post", None)
    if post is None:
        return None

    url = clean_url(request.url)
    headers = target_request_headers(
        request,
        default_content_type="application/x-www-form-urlencoded",
    )
    if on_request_started is not None:
        on_request_started()
    response = await post(
        url,
        data=request.post_data or "",
        headers=headers,
        timeout=timer.remaining_ms,
        max_redirects=0,
    )
    return await raw_response_from_api_response(
        response,
        fallback_url=url,
        limits=limits,
        read_body=not request.return_only_cookies,
    )


async def raw_response_from_api_response(
    response: Any,
    *,
    fallback_url: str,
    limits: ResourceLimits,
    read_body: bool = True,
) -> RawResponse:
    try:
        headers = dict(getattr(response, "headers", {}) or {})
        body_reader = getattr(response, "body", None) if read_body else None
        if not read_body:
            body = ""
        elif callable(body_reader):
            raw_body = await cast(Callable[[], Awaitable[bytes]], body_reader)()
            ensure_bytes_size(
                raw_body,
                limits.response_body_bytes,
                label="Response body",
            )
            body = raw_body.decode(response_charset(headers), errors="replace")
        else:
            text_reader = getattr(response, "text", None)
            body = (
                await cast(Callable[[], Awaitable[str]], text_reader)()
                if callable(text_reader)
                else ""
            )
            ensure_text_size(
                body,
                limits.response_body_bytes,
                label="Response body",
            )
        return RawResponse(
            url=str(getattr(response, "url", fallback_url) or fallback_url),
            status=int(getattr(response, "status", HTTPStatus.OK)),
            headers=headers,
            body=body,
        )
    finally:
        dispose = getattr(response, "dispose", None)
        if callable(dispose):
            try:
                result = dispose()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("Failed to dispose API response body.", exc_info=True)


async def _direct_http_get(
    url: str,
    request: V1Request,
    timer: TimeoutTimer,
) -> RawResponse:
    executor = _ACTIVE_DIRECT_HTTP_EXECUTOR.get()
    if executor is None:
        raise RuntimeError("Direct HTTP executor is not configured for this runtime.")
    return await executor.get(url, request, timer)


def _consume_direct_http_result(result: asyncio.Future[RawResponse]) -> None:
    with suppress(asyncio.CancelledError):
        result.exception()


def _direct_http_get_sync(
    url: str,
    request: V1Request,
    timeout_seconds: float,
    maximum_body_bytes: int = 33_554_432,
) -> RawResponse:
    deadline = time.monotonic() + max(0.001, timeout_seconds)
    headers = target_request_headers(request)
    origin_bound_header_names = {name.lower() for name in origin_bound_target_headers(request)}
    for name, value in DIRECT_HTTP_DEFAULT_HEADERS.items():
        set_default_header(headers, name, value)
    cookie_jar = _cookie_jar_from_browser_cookies(request.cookies, source_url=url)
    current_url = clean_url(url)
    target_origin = _url_origin(current_url)

    for redirect_count in range(DIRECT_HTTP_MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Direct HTTP request exceeded the request deadline.")
        hop_headers = _headers_for_origin(
            headers,
            origin_bound_header_names=origin_bound_header_names,
            target_origin=target_origin,
            request_url=current_url,
        )
        if not has_header(hop_headers, "Cookie"):
            cookie_probe = URLRequest(quote_url_for_http(current_url), method="GET")
            cookie_jar.add_cookie_header(cookie_probe)
            cookie_header = cookie_probe.get_header("Cookie")
            if cookie_header is not None:
                hop_headers["Cookie"] = str(cookie_header)
        http_request = URLRequest(
            quote_url_for_http(current_url),
            headers=hop_headers,
            method="GET",
        )
        with _HTTP_OPENER.open(http_request, timeout=max(0.001, remaining)) as response:
            _extract_response_cookies(cookie_jar, response, http_request)
            status_value = getattr(response, "status", None)
            if status_value is None:
                status_value = response.getcode()
            status = int(status_value)
            response_url = str(response.geturl() or current_url)
            location = _response_header(response.headers, "Location")
            if status in _REDIRECT_STATUSES and location:
                if redirect_count >= DIRECT_HTTP_MAX_REDIRECTS:
                    raise HTTPError(
                        response_url,
                        status,
                        "Direct HTTP redirect limit exceeded",
                        response.headers,
                        response,
                    )
                next_url = clean_url(urljoin(response_url, location))
                current_url = next_url
                continue
            body_limit = (
                DIRECT_HTTP_CHALLENGE_PROBE_BYTES
                if request.return_only_cookies
                else maximum_body_bytes
            )
            raw_body = _read_direct_http_body(
                response,
                maximum_body_bytes=body_limit,
                deadline=deadline,
            )
            if request.return_only_cookies:
                # Cookies-only responses never serialize the body. Retain just a
                # bounded prefix so challenge interstitials can still be classified.
                raw_body = raw_body[:DIRECT_HTTP_CHALLENGE_PROBE_BYTES]
            else:
                ensure_bytes_size(raw_body, maximum_body_bytes, label="Response body")
            body = raw_body.decode(response_charset(response.headers), errors="replace")
            browser_cookies = [_cookie_to_browser_cookie(cookie) for cookie in cookie_jar]
            return RawResponse(
                url=response_url,
                status=status,
                headers=dict(response.headers.items()),
                body=body,
                cookies=browser_cookies,
                solution_cookies=[_cookie_to_solution_cookie(cookie) for cookie in cookie_jar],
                user_agent=_response_header(headers, "User-Agent"),
            )

    raise RuntimeError("Direct HTTP redirect processing ended unexpectedly.")


def _read_direct_http_body(
    response: Any,
    *,
    maximum_body_bytes: int,
    deadline: float,
) -> bytes:
    read_one_chunk = getattr(response, "read1", None)
    if not callable(read_one_chunk):
        try:
            return cast(bytes, response.read(maximum_body_bytes + 1))
        except TypeError:
            return cast(bytes, response.read())

    body = bytearray()
    while len(body) <= maximum_body_bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Direct HTTP request exceeded the request deadline.")
        _set_response_socket_timeout(response, remaining)
        maximum_chunk = min(65_536, maximum_body_bytes + 1 - len(body))
        chunk = cast(bytes, read_one_chunk(maximum_chunk))
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body)


def _set_response_socket_timeout(response: Any, timeout_seconds: float) -> None:
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    settimeout = getattr(sock, "settimeout", None)
    if callable(settimeout):
        settimeout(max(0.001, timeout_seconds))


def _headers_for_origin(
    headers: Mapping[str, str],
    *,
    origin_bound_header_names: set[str],
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


def _response_header(headers: Any, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        return str(value) if value is not None else None
    for header_name, value in headers.items():
        if str(header_name).lower() == name.lower():
            return str(value)
    return None


def _extract_response_cookies(
    cookie_jar: CookieJar,
    response: Any,
    request: URLRequest,
) -> None:
    existing_cookies = {(cookie.domain, cookie.path, cookie.name): cookie for cookie in cookie_jar}
    info = getattr(response, "info", None)
    if callable(info):
        cookie_jar.extract_cookies(response, request)
    elif callable(getattr(response.headers, "get_all", None)):
        cookie_jar.extract_cookies(cast(Any, _CookieResponse(response.headers)), request)
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
    def __init__(self, headers: Any) -> None:
        self._headers = headers

    def info(self) -> Any:
        return self._headers


def _cookie_jar_from_browser_cookies(
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


def safe_log_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit((parts.scheme, host + port, parts.path, "", ""))


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


def target_request_headers(
    request: V1Request,
    *,
    default_content_type: str | None = None,
) -> dict[str, str]:
    headers = origin_bound_target_headers(request)
    user_agent = request.target_user_agent()
    if user_agent:
        headers["User-Agent"] = user_agent
    if default_content_type:
        set_default_header(headers, "Content-Type", default_content_type)
    return headers


def origin_bound_target_headers(request: V1Request) -> dict[str, str]:
    headers = request.target_headers()
    referer = request.target_referer()
    if referer:
        headers["Referer"] = referer
    return headers


def set_default_header(headers: dict[str, str], name: str, value: str) -> None:
    if not has_header(headers, name):
        headers[name] = value


def has_header(headers: Mapping[str, str], name: str) -> bool:
    normalized = name.lower()
    return any(key.lower() == normalized for key in headers)
