from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar, Token
from http import HTTPStatus
from typing import Any, TypeAlias, cast
from urllib.parse import urlsplit

from camouflare.challenge import CHALLENGE_RESULT_RESERVE_MS, content_has_challenge_markers
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.http_transport import (
    ALLOWED_URL_SCHEMES,
    HttpDecodeError,
    HttpFetch,
    HttpTransport,
    clean_url,
    set_default_header,
    with_default_headers,
)
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

# Caps each bounded browser GET stage (DOM readiness, then the commit grace) and the
# share of the navigation budget the browser stages leave for an eligible timeout
# fallback, so an uncommitted navigation never waits out a long maxTimeout.
DOMCONTENTLOADED_NAVIGATION_TIMEOUT_MS = 15000
DIRECT_HTTP_CHALLENGE_PROBE_BYTES = 65_536

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


_ACTIVE_HTTP_TRANSPORT: ContextVar[HttpTransport | None] = ContextVar(
    "camouflare_active_http_transport",
    default=None,
)


@contextmanager
def active_http_transport(transport: HttpTransport):
    token: Token[HttpTransport | None] = _ACTIVE_HTTP_TRANSPORT.set(transport)
    try:
        yield
    finally:
        _ACTIVE_HTTP_TRANSPORT.reset(token)


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
    except (ResourceLimitError, HttpDecodeError):
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
    except (ResourceLimitError, HttpDecodeError):
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
    except (ResourceLimitError, HttpDecodeError):
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
    transport = _ACTIVE_HTTP_TRANSPORT.get()
    if transport is None:
        raise RuntimeError("HTTP transport is not configured for this runtime.")
    limits = _ACTIVE_LIMITS.get() or ResourceLimits()
    result = await transport.fetch(
        HttpFetch(
            url=url,
            headers=with_default_headers(target_request_headers(request)),
            origin_bound_header_names=frozenset(
                name.lower() for name in origin_bound_target_headers(request)
            ),
            cookies=request.cookies,
            # Cookies-only responses never serialize the body. Retain just a bounded
            # decoded prefix so challenge interstitials can still be classified.
            max_body_bytes=(
                DIRECT_HTTP_CHALLENGE_PROBE_BYTES
                if request.return_only_cookies
                else limits.response_body_bytes
            ),
            body="prefix" if request.return_only_cookies else "full",
        ),
        timeout_seconds=timer.remaining_seconds,
        # Only a GET with origin-bound headers has no browser alternative (navigate_get
        # returns before the preflight and fallbacks for it). The preflight and
        # fallbacks give way to browser navigation at once instead of queueing behind
        # explicit HTTP-transport requests while this request holds a browser context.
        wait_for_capacity=bool(origin_bound_target_headers(request)),
    )
    return RawResponse(
        url=result.url,
        status=result.status,
        headers=dict(result.headers),
        body=result.body.decode(response_charset(result.message), errors="replace"),
        cookies=result.cookies,
        solution_cookies=result.solution_cookies,
        user_agent=result.user_agent,
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
