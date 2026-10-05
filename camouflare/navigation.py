from __future__ import annotations

import asyncio
import inspect
import logging
import time
import zlib
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
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

from camouflare.challenge import CHALLENGE_RESULT_RESERVE_MS, content_has_challenge_markers
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
from camouflare.observability import redact_url
from camouflare.protocols import BrowserContextLike, PageLike, ResponseLike
from camouflare.solution import is_best_effort_browser_error, response_charset
from camouflare.timer import TimeoutTimer

ALLOWED_URL_SCHEMES = ("http", "https")
# Caps each bounded browser GET stage (DOM readiness, then the commit grace) and the
# share of the navigation budget the browser stages leave for an eligible timeout
# fallback, so an uncommitted navigation never waits out a long maxTimeout.
DOMCONTENTLOADED_NAVIGATION_TIMEOUT_MS = 15000
DIRECT_HTTP_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    # Advertise only the content codings _read_direct_http_body can decode.
    "Accept-Encoding": "gzip, deflate",
}
DIRECT_HTTP_MAX_REDIRECTS = 10
DIRECT_HTTP_MAX_WORKERS = 4
DIRECT_HTTP_CHALLENGE_PROBE_BYTES = 65_536
_DIRECT_HTTP_CHUNK_BYTES = 65_536
_DIRECT_HTTP_MAX_CONTENT_CODINGS = 4
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


class _DirectHttpDecodeError(CamouflareError):
    """A direct HTTP body could not be decoded into the bytes the server encoded."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            error_code=V1ErrorCode.RESPONSE_DECODE_ERROR,
            retryable=False,
        )


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
        direct_response = await fetch_direct(url, request, _navigation_timer(timer))
        await _import_direct_response_cookies(page, direct_response)
        return direct_response
    navigation_timer = _navigation_timer(timer)
    if allow_direct_http_first and should_try_direct_get_first(request):
        direct_response = await try_direct_http_get_first(
            url,
            request,
            navigation_timer,
            page=page,
            direct_http_get=fetch_direct,
        )
        if direct_response is not None:
            return direct_response

    timeout_fallback = allow_direct_http_fallback and (
        should_try_direct_get_after_navigation_timeout(request)
    )
    # An eligible timeout fallback is one more stage: the browser stages leave it up to
    # one stage cap of the navigation budget, never more than half of what is left.
    fallback_reserve_ms = (
        min(DOMCONTENTLOADED_NAVIGATION_TIMEOUT_MS, max(0, _unspent_ms(navigation_timer)) // 2)
        if timeout_fallback
        else 0
    )
    browser_timer = _timer_keeping(navigation_timer, fallback_reserve_ms)
    if browser_timer is None or _unspent_ms(browser_timer) < 2:
        # Navigation and the commit decision each need a positive Playwright timeout.
        # Do not start a doomed navigation that could change the page during collection.
        raise TimeoutError("Navigation timed out before browser navigation could start.")

    try:
        return await page.goto(
            url,
            timeout=_stage_timeout_ms(browser_timer, reserve_ms=1),
            wait_until="domcontentloaded",
        )
    except Exception as exc:
        if is_timeout_error(exc):
            logger.info(
                "Navigation timed out before domcontentloaded; waiting for commit.",
                extra={"target": redact_url(url), "error": type(exc).__name__},
            )
            try:
                # This resolves immediately for a committed document, keeping it and
                # the remaining request budget for challenge clearance and collection.
                await page.wait_for_url(
                    lambda current_url: (
                        urlsplit(str(current_url)).scheme.lower() in ALLOWED_URL_SCHEMES
                    ),
                    wait_until="commit",
                    timeout=_stage_timeout_ms(browser_timer),
                )
                return None
            except Exception as commit_exc:
                # Only a degenerate budget is spent here: the browser stages leave the
                # fallback its share, and an attempt with no time left cannot finish.
                if (
                    timeout_fallback
                    and is_timeout_error(commit_exc)
                    and _unspent_ms(navigation_timer) > 0
                ):
                    logger.info(
                        "Navigation timed out before commit; trying direct HTTP fallback.",
                        extra={
                            "target": redact_url(url),
                            "error": type(commit_exc).__name__,
                        },
                    )
                    direct_response = await try_direct_http_get_after_navigation_timeout(
                        url,
                        request,
                        navigation_timer,
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
                                "target": redact_url(url),
                                "error": type(commit_exc).__name__,
                            },
                        )
                        return await _fallback_after_browser_transport(
                            url,
                            request,
                            navigation_timer,
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
                    extra={"target": redact_url(url), "error": type(exc).__name__},
                )
                return await _fallback_after_browser_transport(
                    url,
                    request,
                    navigation_timer,
                    browser_error=exc,
                    fetch_direct=fetch_direct,
                    page=page,
                )
            _emit_browser_transport_error(exc, fallback_used=False)
        raise


def _result_reserve_ms(timer: TimeoutTimer) -> int:
    """Request time navigation leaves for classifying and collecting the result.

    It is the challenge wait's result reserve; a budget too short for it keeps half
    instead, so a short ``maxTimeout`` still navigates and still collects.
    """
    return min(CHALLENGE_RESULT_RESERVE_MS, max(1, timer.timeout_ms // 2))


def _unspent_ms(timer: TimeoutTimer) -> int:
    """Milliseconds before ``timer``'s deadline; zero or negative once it has passed."""
    return timer.timeout_ms - timer.elapsed_ms


def _timer_keeping(timer: TimeoutTimer, reserve_ms: int) -> TimeoutTimer | None:
    """Return a timer ending ``reserve_ms`` before ``timer``'s deadline, or None.

    The derived timer shares that deadline rather than granting a fresh budget, so a
    direct HTTP worker started with it also stops before the reserve.
    """
    if _unspent_ms(timer) <= reserve_ms:
        return None
    bounded = TimeoutTimer(timer.timeout_ms - reserve_ms)
    bounded.started = timer.started
    return bounded


def _navigation_timer(timer: TimeoutTimer) -> TimeoutTimer:
    """Leave collection time without denying short budgets useful navigation time."""
    # Pool/session setup may have spent most of maxTimeout before a page was ready.
    # Keep half of that remaining time available for navigation, down to 1 ms.
    reserve_ms = min(_result_reserve_ms(timer), max(1, _unspent_ms(timer) // 2))
    navigation_timer = _timer_keeping(timer, reserve_ms)
    if navigation_timer is None:
        raise TimeoutError("The request deadline left no time for navigation.")
    return navigation_timer


def _stage_timeout_ms(timer: TimeoutTimer, *, reserve_ms: int = 0) -> int:
    """Return one browser stage's Playwright timeout.

    Keep a positive commit decision even when DOM readiness spends a short budget.
    A floor of 1 is required because Playwright treats 0 as no timeout at all.
    """
    return min(DOMCONTENTLOADED_NAVIGATION_TIMEOUT_MS, max(1, _unspent_ms(timer) - reserve_ms))


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
    except (ResourceLimitError, _DirectHttpDecodeError):
        _emit_browser_transport_error(browser_error, fallback_used=False)
        raise
    except Exception as fallback_error:
        logger.info(
            "Direct HTTP fallback after browser transport failure failed; "
            "preserving browser transport error.",
            extra={"target": redact_url(url), "error": type(fallback_error).__name__},
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
            extra={"target": redact_url(url), "error": type(cookie_error).__name__},
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
    except (ResourceLimitError, _DirectHttpDecodeError):
        raise
    except Exception as exc:
        logger.info(
            "Direct HTTP GET preflight failed; falling back to browser navigation.",
            extra={"target": redact_url(url), "error": type(exc).__name__},
        )
        return None

    if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
        logger.info(
            "Direct HTTP GET preflight returned a non-2xx response; "
            "falling back to browser navigation.",
            extra={"target": redact_url(url), "status": response.status},
        )
        return None
    body = await response.text()
    if content_has_challenge_markers(body):
        logger.info(
            "Direct HTTP GET preflight returned challenge HTML; "
            "falling back to browser navigation.",
            extra={"target": redact_url(url), "status": response.status},
        )
        return None
    try:
        await _import_direct_response_cookies(page, response)
    except Exception as exc:
        logger.info(
            "Direct HTTP GET cookie import failed; falling back to browser navigation.",
            extra={"target": redact_url(url), "error": type(exc).__name__},
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
    except (ResourceLimitError, _DirectHttpDecodeError):
        raise
    except Exception as exc:
        logger.info(
            "Direct HTTP GET fallback after navigation timeout failed; "
            "preserving browser navigation error.",
            extra={"target": redact_url(url), "error": type(exc).__name__},
        )
        return None

    if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
        logger.info(
            "Direct HTTP GET fallback after navigation timeout returned a non-2xx response; "
            "preserving browser navigation error.",
            extra={"target": redact_url(url), "status": response.status},
        )
        return None
    body = await response.text()
    if content_has_challenge_markers(body):
        logger.info(
            "Direct HTTP GET fallback after navigation timeout returned challenge HTML; "
            "preserving browser navigation error.",
            extra={"target": redact_url(url), "status": response.status},
        )
        return None
    try:
        await _import_direct_response_cookies(page, response)
    except Exception as exc:
        logger.info(
            "Direct HTTP GET cookie import after navigation timeout failed; "
            "preserving browser navigation error.",
            extra={"target": redact_url(url), "error": type(exc).__name__},
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
    # Settling is optional, so it never spends the time kept for collecting the result.
    timeout = min(5000, _unspent_ms(timer) - _result_reserve_ms(timer))
    if timeout <= 0:
        return
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
            # Cookies-only responses never serialize the body. Retain just a bounded
            # decoded prefix so challenge interstitials can still be classified.
            if status in (204, 304):
                raw_body = b""
            else:
                raw_body = _read_direct_http_body(
                    response,
                    maximum_body_bytes=(
                        DIRECT_HTTP_CHALLENGE_PROBE_BYTES
                        if request.return_only_cookies
                        else maximum_body_bytes
                    ),
                    deadline=deadline,
                    prefix_only=request.return_only_cookies,
                )
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
    prefix_only: bool = False,
) -> bytes:
    """Read a body and remove its HTTP content codings within byte and time bounds.

    Wire bytes, the output of every decoding layer, and the final body are each
    limited to ``maximum_body_bytes``. A full read raises ``ResourceLimitError`` past
    that limit and rejects truncated or malformed encodings. ``prefix_only`` instead
    stops at the limit and returns the decoded prefix without integrity checks for
    the remainder it deliberately did not read.
    """

    decoders = _content_decoders(_response_content_codings(response.headers))
    wire = _limited_body_chunks(
        _direct_http_wire_chunks(response, maximum_bytes=maximum_body_bytes, deadline=deadline),
        maximum_bytes=maximum_body_bytes,
        label="Response body",
        prefix_only=prefix_only,
    )
    parts: list[bytes] = []
    try:
        decoded: Iterator[bytes] = wire
        for decoder in decoders:
            decoded = _limited_body_chunks(
                _decoded_body_chunks(decoder, decoded, deadline=deadline),
                maximum_bytes=maximum_body_bytes,
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


def _ensure_direct_http_deadline(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Direct HTTP request exceeded the request deadline.")
    return remaining


def _direct_http_wire_chunks(
    response: Any,
    *,
    maximum_bytes: int,
    deadline: float,
) -> Iterator[bytes]:
    """Yield undecoded body bytes, reading at most one byte beyond ``maximum_bytes``."""

    read_one_chunk = getattr(response, "read1", None)
    if not callable(read_one_chunk):
        _set_response_socket_timeout(response, _ensure_direct_http_deadline(deadline))
        try:
            body = cast(bytes, response.read(maximum_bytes + 1))
        except TypeError:
            body = cast(bytes, response.read())
        _ensure_direct_http_deadline(deadline)
        if body:
            yield body
        return

    received = 0
    while received <= maximum_bytes:
        _set_response_socket_timeout(response, _ensure_direct_http_deadline(deadline))
        maximum_chunk = min(_DIRECT_HTTP_CHUNK_BYTES, maximum_bytes + 1 - received)
        chunk = cast(bytes, read_one_chunk(maximum_chunk))
        _ensure_direct_http_deadline(deadline)
        if not chunk:
            return
        received += len(chunk)
        yield chunk


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
            _ensure_direct_http_deadline(deadline)
            try:
                output = decoder.decompress(data)
            except zlib.error as exc:
                raise _DirectHttpDecodeError(
                    f"Direct HTTP response body has malformed {decoder.coding} content encoding."
                ) from exc
            data = b""
            if output:
                yield output
            if not decoder.output_pending:
                break
    decoder.finish()


def _response_content_codings(headers: Any) -> list[str]:
    """Return non-identity content codings in the order the server applied them."""

    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = [
            str(value) for value in cast("Iterable[object]", get_all("Content-Encoding") or ())
        ]
    else:
        values = [
            str(value) for name, value in headers.items() if str(name).lower() == "content-encoding"
        ]
    codings: list[str] = []
    for value in values:
        for token in value.split(","):
            coding = token.strip().lower()
            if coding and coding != "identity":
                codings.append("gzip" if coding == "x-gzip" else coding)
    return codings


def _content_decoders(codings: list[str]) -> list[_ContentDecoder]:
    """Create decoders in removal order: the last coding applied comes off first."""

    if len(codings) > _DIRECT_HTTP_MAX_CONTENT_CODINGS:
        raise _DirectHttpDecodeError(
            f"Direct HTTP response uses more than {_DIRECT_HTTP_MAX_CONTENT_CODINGS} "
            "content encodings."
        )
    decoders: list[_ContentDecoder] = []
    for coding in reversed(codings):
        if coding == "gzip":
            decoders.append(_GzipDecoder())
        elif coding == "deflate":
            decoders.append(_DeflateDecoder())
        else:
            raise _DirectHttpDecodeError(
                f"Direct HTTP response uses unsupported content encoding {coding[:64]!r}."
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
        while len(output) < _DIRECT_HTTP_CHUNK_BYTES:
            if self._member is None:
                if self._members_completed:
                    # Like gzip.decompress, accept NUL padding after a complete member.
                    self._input = self._input.lstrip(b"\x00")
                if not self._input:
                    break
                self._member = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            output += self._member.decompress(
                self._input,
                _DIRECT_HTTP_CHUNK_BYTES - len(output),
            )
            if self._member.eof:
                self._input = self._member.unused_data
                self._member = None
                self._members_completed += 1
                continue
            self._input = self._member.unconsumed_tail
            if len(output) < _DIRECT_HTTP_CHUNK_BYTES:
                break
        self.output_pending = len(output) >= _DIRECT_HTTP_CHUNK_BYTES
        return bytes(output)

    def finish(self) -> None:
        if self._members_completed == 0 or self._member is not None or self._input:
            raise _DirectHttpDecodeError(
                "Direct HTTP response body ended inside its gzip content encoding."
            )


_DEFLATE_TRAILING_DATA_MESSAGE = (
    "Direct HTTP response body has data after the end of its deflate content encoding."
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
                raise _DirectHttpDecodeError(_DEFLATE_TRAILING_DATA_MESSAGE)
            return b""
        output: bytes = self._stream.decompress(self._input, _DIRECT_HTTP_CHUNK_BYTES)
        if self._stream.eof:
            if self._stream.unused_data:
                raise _DirectHttpDecodeError(_DEFLATE_TRAILING_DATA_MESSAGE)
            self._input = b""
        else:
            self._input = self._stream.unconsumed_tail
            self.output_pending = len(output) >= _DIRECT_HTTP_CHUNK_BYTES
        return output

    def finish(self) -> None:
        if self._input or self._stream is None or not self._stream.eof:
            raise _DirectHttpDecodeError(
                "Direct HTTP response body ended inside its deflate content encoding."
            )


_ContentDecoder: TypeAlias = _GzipDecoder | _DeflateDecoder


def _has_zlib_header(data: bytes) -> bool:
    """Whether data starts with an RFC 1950 header: CM=8, CINFO<=7, valid FCHECK."""

    cmf, flags = data[0], data[1]
    return cmf & 0x0F == 8 and cmf >> 4 <= 7 and ((cmf << 8) | flags) % 31 == 0


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
