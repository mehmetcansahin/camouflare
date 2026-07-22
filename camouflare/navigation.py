from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from html import escape
from http import HTTPStatus
from typing import Any, TypeAlias, cast
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit
from urllib.request import (
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
)
from urllib.request import (
    Request as URLRequest,
)

from camouflare.challenge import content_has_challenge_markers
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.limits import (
    ResourceLimitError,
    ResourceLimits,
    ensure_bytes_size,
    ensure_text_size,
)
from camouflare.metrics import record_browser_transport_error
from camouflare.models import V1Request
from camouflare.protocols import BrowserContextLike, PageLike, ResponseLike, RouteLike
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
    ) -> None:
        self.url = url
        self.status = status
        self.headers = headers
        self._body = body
        self.fallback_used = False

    async def text(self) -> str:
        return self._body


NavigationResponse: TypeAlias = ResponseLike | RawResponse | None
DirectHttpGet: TypeAlias = Callable[[str, V1Request, TimeoutTimer], Awaitable[RawResponse]]


def _build_http_opener() -> OpenerDirector:
    # Deliberately omit FileHandler/FTPHandler/DataHandler (and ProxyHandler):
    # the fallback is restricted to HTTP(S), including across redirects.
    opener = OpenerDirector()
    for handler in (
        HTTPHandler,
        HTTPSHandler,
        HTTPDefaultErrorHandler,
        HTTPRedirectHandler,
        HTTPErrorProcessor,
    ):
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
    referer = request.target_referer()
    if allow_direct_http_first and should_try_direct_get_first(request):
        direct_response = await try_direct_http_get_first(
            url, request, timer, direct_http_get=fetch_direct
        )
        if direct_response is not None:
            return direct_response

    try:
        return await page.goto(
            url,
            timeout=min(timer.remaining_ms, DOMCONTENTLOADED_NAVIGATION_TIMEOUT_MS),
            wait_until="domcontentloaded",
            referer=referer,
        )
    except Exception as exc:
        if is_timeout_error(exc):
            logger.info(
                "Navigation timed out before domcontentloaded; waiting for commit.",
                extra={"target": safe_log_url(url), "error": type(exc).__name__},
            )
            try:
                await page.wait_for_load_state("commit", timeout=timer.remaining_ms)
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
    _emit_browser_transport_error(browser_error, fallback_used=True)
    return _mark_direct_http_fallback(response)


async def try_direct_http_get_first(
    url: str,
    request: V1Request,
    timer: TimeoutTimer,
    *,
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

    body = await response.text()
    if content_has_challenge_markers(body):
        logger.info(
            "Direct HTTP GET preflight returned challenge HTML; "
            "falling back to browser navigation.",
            extra={"target": safe_log_url(url), "status": response.status},
        )
        return None
    return response


async def try_direct_http_get_after_navigation_timeout(
    url: str,
    request: V1Request,
    timer: TimeoutTimer,
    *,
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

    body = await response.text()
    if content_has_challenge_markers(body):
        logger.info(
            "Direct HTTP GET fallback after navigation timeout returned challenge HTML; "
            "preserving browser navigation error.",
            extra={"target": safe_log_url(url), "status": response.status},
        )
        return None
    return _mark_direct_http_fallback(response)


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


async def submit_post_form(
    page: PageLike,
    request: V1Request,
    timer: TimeoutTimer,
) -> NavigationResponse:
    url = clean_url(request.url)
    post_data = request.post_data or ""
    pairs = parse_qsl(post_data, keep_blank_values=True)
    if hasattr(page, "posted_form"):
        page.posted_form = dict(pairs)  # type: ignore[attr-defined]
        return await page.goto(
            url,
            timeout=timer.remaining_ms,
            wait_until="domcontentloaded",
            referer=request.target_referer(),
        )

    form = [f'<form id="camouflare-post-form" action="{escape(url)}" method="POST">']
    for key, value in pairs:
        form.append(f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">')
    form.append("</form>")
    await page.set_content("<html><body>" + "".join(form) + "</body></html>")
    expect_navigation = getattr(page, "expect_navigation", None)
    if expect_navigation is not None:
        async with expect_navigation(
            timeout=timer.remaining_ms,
            wait_until="domcontentloaded",
        ) as navigation:
            await submit_hidden_form(page)
        return await resolve_navigation_value(navigation.value)

    await submit_hidden_form(page)
    with suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=timer.remaining_ms)
    return None


async def submit_post(
    context: BrowserContextLike,
    page: PageLike,
    request: V1Request,
    timer: TimeoutTimer,
    limits: ResourceLimits,
) -> NavigationResponse:
    if callable(getattr(page, "route", None)):
        return await submit_post_navigation(page, request, timer)

    response = await post_with_context_request(context, request, timer, limits)
    if response is not None:
        # A POST response is terminal even when it contains challenge HTML. Replaying
        # the business request through a second browser path could duplicate effects.
        # Consumers receive the first response and decide whether another attempt is safe.
        return response

    if not can_submit_hidden_form(request):
        raise RuntimeError(
            "Browser page does not support POST navigation for this raw Content-Type."
        )
    return await submit_post_form(page, request, timer)


async def submit_post_navigation(
    page: PageLike,
    request: V1Request,
    timer: TimeoutTimer,
) -> NavigationResponse:
    url = clean_url(request.url)
    url_parts = urlsplit(url)
    route_url = urlunsplit(
        (url_parts.scheme, url_parts.netloc, url_parts.path, url_parts.query, "")
    )
    target_headers = target_request_headers(
        request,
        default_content_type="application/x-www-form-urlencoded",
    )

    async def continue_as_post(route: RouteLike) -> None:
        route_request = getattr(route, "request", None)
        all_headers = getattr(route_request, "all_headers", None)
        if callable(all_headers):
            read_headers = cast(
                Callable[[], Awaitable[Mapping[str, str]]],
                all_headers,
            )
            headers = dict(await read_headers())
        else:
            headers = dict(getattr(route_request, "headers", {}) or {})
        for name, value in target_headers.items():
            existing = next((key for key in headers if key.lower() == name.lower()), None)
            if existing is not None:
                del headers[existing]
            headers[name] = value
        for name in list(headers):
            if name.lower() == "content-length":
                del headers[name]
        await route.continue_(
            method="POST",
            post_data=request.post_data or "",
            headers=headers,
        )

    await page.route(route_url, continue_as_post, times=1)
    return await page.goto(
        url,
        timeout=timer.remaining_ms,
        wait_until="domcontentloaded",
        referer=request.target_referer(),
    )


async def post_with_context_request(
    context: BrowserContextLike,
    request: V1Request,
    timer: TimeoutTimer,
    limits: ResourceLimits,
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
    response = await post(
        url,
        data=request.post_data or "",
        headers=headers,
        timeout=timer.remaining_ms,
    )
    return await raw_response_from_api_response(
        response,
        fallback_url=url,
        limits=limits,
    )


async def submit_hidden_form(page: PageLike) -> None:
    await page.evaluate(
        """() => {
  const form = document.getElementById('camouflare-post-form');
  if (!form) {
    throw new Error('camouflare-post-form was not found');
  }
  form.submit();
}"""
    )


async def resolve_navigation_value(value: Any) -> NavigationResponse:
    if inspect.isawaitable(value):
        return await value
    return value


async def raw_response_from_api_response(
    response: Any,
    *,
    fallback_url: str,
    limits: ResourceLimits,
) -> RawResponse:
    try:
        headers = dict(getattr(response, "headers", {}) or {})
        body_reader = getattr(response, "body", None)
        if callable(body_reader):
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
    limits = _ACTIVE_LIMITS.get() or ResourceLimits()
    return await asyncio.to_thread(
        _direct_http_get_sync,
        url,
        request,
        timer.remaining_seconds,
        limits.response_body_bytes,
    )


def _direct_http_get_sync(
    url: str,
    request: V1Request,
    timeout_seconds: float,
    maximum_body_bytes: int = 33_554_432,
) -> RawResponse:
    headers = target_request_headers(request)
    for name, value in DIRECT_HTTP_DEFAULT_HEADERS.items():
        set_default_header(headers, name, value)
    cookie_header = cookie_header_for_url(request.cookies, url)
    if cookie_header:
        set_default_header(headers, "Cookie", cookie_header)

    http_request = URLRequest(quote_url_for_http(url), headers=headers, method="GET")
    with _HTTP_OPENER.open(http_request, timeout=timeout_seconds) as response:
        try:
            raw_body = response.read(maximum_body_bytes + 1)
        except TypeError:
            raw_body = response.read()
        ensure_bytes_size(raw_body, maximum_body_bytes, label="Response body")
        body = raw_body.decode(response_charset(response.headers), errors="replace")
        return RawResponse(
            url=response.geturl(),
            status=int(response.status),
            headers=dict(response.headers.items()),
            body=body,
        )


def clean_url(url: str | None) -> str:
    if not url:
        raise CamouflareError(
            "Request parameter 'url' is mandatory.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    cleaned = url.replace('"', "").strip()
    scheme = urlsplit(cleaned).scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise CamouflareError(
            "Request parameter 'url' must use the http or https scheme.",
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
    headers = request.target_headers()
    referer = request.target_referer()
    if referer:
        headers["Referer"] = referer
    user_agent = request.target_user_agent()
    if user_agent:
        headers["User-Agent"] = user_agent
    if default_content_type:
        set_default_header(headers, "Content-Type", default_content_type)
    return headers


def can_submit_hidden_form(request: V1Request) -> bool:
    headers = target_request_headers(
        request,
        default_content_type="application/x-www-form-urlencoded",
    )
    content_type = next(
        (value for name, value in headers.items() if name.lower() == "content-type"),
        "",
    )
    return content_type.split(";", 1)[0].strip().lower() == "application/x-www-form-urlencoded"


def set_default_header(headers: dict[str, str], name: str, value: str) -> None:
    if not has_header(headers, name):
        headers[name] = value


def cookie_header_for_url(cookies: list[dict[str, Any]] | None, url: str) -> str | None:
    if not cookies:
        return None
    host = (urlsplit(url).hostname or "").lower()
    pairs = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if name is None or value is None:
            continue
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if domain and host and not (host == domain or host.endswith("." + domain)):
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs) if pairs else None


def has_header(headers: Mapping[str, str], name: str) -> bool:
    normalized = name.lower()
    return any(key.lower() == normalized for key in headers)
