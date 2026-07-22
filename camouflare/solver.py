from __future__ import annotations

import asyncio
import json
import logging
import weakref
from contextlib import AsyncExitStack, suppress
from typing import Any

from camouflare import challenge as _challenge
from camouflare import navigation as _navigation
from camouflare import solution as _solution
from camouflare.captcha import CaptchaProvider, NoCaptchaProvider
from camouflare.challenge import (
    ChallengeSolveError,
    RequestTimeoutError,
    Sleep,
)
from camouflare.challenge import (
    challenge_detected as _challenge_detected,
)
from camouflare.challenge import (
    challenge_markers_remain as _challenge_markers_remain,
)
from camouflare.challenge import (
    solve_challenge as _solve_challenge,
)
from camouflare.challenge import (
    wait_for_challenge_cleared as _wait_for_challenge_cleared,
)
from camouflare.challenge import (
    wait_requested as _wait_requested,
)
from camouflare.cleanup import CleanupSupervisor
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.limits import ResourceLimitError, ResourceLimits
from camouflare.metrics import record_browser_transport_error, record_challenge, record_timeout
from camouflare.models import V1Request, V1Response
from camouflare.navigation import active_resource_limits
from camouflare.protocols import (
    BrowserContextLike,
    MainFrameResponseHolder,
    PageLike,
    ResponseLike,
    RouteLike,
)
from camouflare.solution import (
    collect_solution as _collect_solution,
)
from camouflare.solution import (
    navigation_error_message as _navigation_error_message,
)
from camouflare.solution import (
    response_text_or_page_content as _response_text_or_page_content,
)
from camouflare.solution import (
    safe_page_content as _safe_page_content,
)
from camouflare.solution import (
    safe_page_title as _safe_page_title,
)
from camouflare.timer import TimeoutTimer

MEDIA_PATTERNS = (
    "**/*.{png,jpg,jpeg,gif,webp,bmp,svg,ico,avif}",
    "**/*.{css,woff,woff2,ttf,otf,eot}",
)

logger = logging.getLogger(__name__)

# Kept as module aliases for compatibility with existing integrations and tests
# that patch the private direct-HTTP seam. New code should import navigation.
ALLOWED_URL_SCHEMES = _navigation.ALLOWED_URL_SCHEMES
DIRECT_HTTP_DEFAULT_HEADERS = _navigation.DIRECT_HTTP_DEFAULT_HEADERS
DOMCONTENTLOADED_NAVIGATION_TIMEOUT_MS = _navigation.DOMCONTENTLOADED_NAVIGATION_TIMEOUT_MS
RawResponse = _navigation.RawResponse
_HTTP_OPENER = _navigation._HTTP_OPENER
_ACTIVE_LIMITS = _navigation._ACTIVE_LIMITS
_build_http_opener = _navigation._build_http_opener
_direct_http_get = _navigation._direct_http_get
_direct_http_get_sync = _navigation._direct_http_get_sync
_can_submit_hidden_form = _navigation.can_submit_hidden_form
_clean_url = _navigation.clean_url
_cookie_header_for_url = _navigation.cookie_header_for_url
_has_header = _navigation.has_header
_is_timeout_error = _navigation.is_timeout_error
_post_with_context_request = _navigation.post_with_context_request
_quote_url_for_http = _navigation.quote_url_for_http
_raw_response_from_api_response = _navigation.raw_response_from_api_response
_resolve_navigation_value = _navigation.resolve_navigation_value
_set_default_header = _navigation.set_default_header
_should_try_direct_get_after_navigation_timeout = (
    _navigation.should_try_direct_get_after_navigation_timeout
)
_should_try_direct_get_first = _navigation.should_try_direct_get_first
_submit_hidden_form = _navigation.submit_hidden_form
_submit_post_navigation = _navigation.submit_post_navigation
_target_request_headers = _navigation.target_request_headers
_try_direct_http_get_after_navigation_timeout = (
    _navigation.try_direct_http_get_after_navigation_timeout
)
_try_direct_http_get_first = _navigation.try_direct_http_get_first
_wait_networkidle_best_effort = _navigation.wait_networkidle_best_effort

CHALLENGE_CLEAR_POLL_MS = _challenge.CHALLENGE_CLEAR_POLL_MS
CHALLENGE_MARKERS = _challenge.CHALLENGE_MARKERS
CHALLENGE_TITLES = _challenge.CHALLENGE_TITLES
_challenge_state = _challenge.challenge_state
_content_has_challenge_markers = _challenge.content_has_challenge_markers
_title_is_challenge = _challenge.title_is_challenge

BEST_EFFORT_BROWSER_ERROR_MARKERS = _solution.BEST_EFFORT_BROWSER_ERROR_MARKERS
_html_title = _solution.html_title
_is_best_effort_browser_error = _solution.is_best_effort_browser_error
_log_best_effort_collection_failure = _solution.log_best_effort_collection_failure
_page_url = _solution.page_url
_response_charset = _solution.response_charset
_response_url = _solution.response_url
_safe_context_cookies = _solution.safe_context_cookies
_safe_user_agent = _solution.safe_user_agent
_should_return_raw_response_body = _solution.should_return_raw_response_body

# Routes are attached to persistent contexts at most once. Weak ownership avoids
# extending a browser context's lifetime just because media blocking was used.
_media_routes: weakref.WeakKeyDictionary[BrowserContextLike, list[tuple[str, Any]]] = (
    weakref.WeakKeyDictionary()
)


async def solve_request(
    request: V1Request,
    *,
    context: BrowserContextLike,
    page: PageLike,
    captcha_provider: CaptchaProvider | None = None,
    limits: ResourceLimits | None = None,
    allow_direct_http_fallback: bool = True,
    allow_direct_http_first: bool = True,
    sleep: Sleep = asyncio.sleep,
    cleanup_supervisor: CleanupSupervisor | None = None,
    cleanup_timeout_seconds: float = 10,
) -> V1Response:
    """Solve one FlareSolverr-compatible request using an existing page/context."""
    provider = captcha_provider or NoCaptchaProvider()
    active_limits = limits or ResourceLimits()
    supervisor = cleanup_supervisor or CleanupSupervisor(timeout_seconds=cleanup_timeout_seconds)
    stack = AsyncExitStack()
    body_failed = False
    with active_resource_limits(active_limits):
        try:
            prepare = getattr(provider, "prepare", None)
            if prepare is not None:
                await stack.enter_async_context(prepare(page=page))
            return await _run_solve(
                request,
                context=context,
                page=page,
                provider=provider,
                limits=active_limits,
                allow_direct_http_fallback=allow_direct_http_fallback,
                allow_direct_http_first=allow_direct_http_first,
                sleep=sleep,
            )
        except BaseException:
            body_failed = True
            raise
        finally:
            try:
                await supervisor.run(
                    stack.aclose(),
                    kind="captcha",
                    timeout_seconds=cleanup_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if body_failed:
                    logger.warning(
                        "Ignoring captcha cleanup error while unwinding a failed request.",
                        exc_info=True,
                    )
                else:
                    logger.warning("Ignoring captcha cleanup error.", exc_info=True)


async def _run_solve(
    request: V1Request,
    *,
    context: BrowserContextLike,
    page: PageLike,
    provider: CaptchaProvider,
    limits: ResourceLimits,
    allow_direct_http_fallback: bool = True,
    allow_direct_http_first: bool = True,
    sleep: Sleep = asyncio.sleep,
) -> V1Response:
    timer = TimeoutTimer(request.max_timeout)
    response: Any = None
    final_response: MainFrameResponseHolder = {"value": None}
    turnstile_token = None

    _track_main_frame_responses(page, final_response)
    try:
        target_headers = request.target_headers()
        target_user_agent = request.target_user_agent()
        if target_user_agent:
            target_headers = {**target_headers, "User-Agent": target_user_agent}
        if target_headers:
            await page.set_extra_http_headers(target_headers)
        await _install_user_agent_override(page, target_user_agent)
        await _apply_media_blocking(context, bool(request.disable_media))
        if request.cookies:
            await context.add_cookies(request.cookies)
    except Exception as exc:
        logger.exception(
            "Unexpected request setup error.",
            extra={"error_code": V1ErrorCode.INTERNAL_ERROR.value},
        )
        content = await _safe_page_content(page, limits)
        return V1Response(
            status="error",
            message=f"Request setup failed: {exc}",
            error_code=V1ErrorCode.INTERNAL_ERROR,
            retryable=False,
            solution=await _collect_solution(
                request,
                context=context,
                page=page,
                page_response=None,
                content=content,
                turnstile_token=turnstile_token,
                limits=limits,
            ),
        )

    try:
        if request.cmd == "request.post":
            response = await _submit_post(context, page, request, timer, limits)
        else:
            response = await _navigate_get(
                page,
                request,
                timer,
                limits,
                allow_direct_http_fallback=allow_direct_http_fallback,
                allow_direct_http_first=allow_direct_http_first,
            )
    except ResourceLimitError:
        raise
    except Exception as exc:
        timed_out = _is_timeout_error(exc)
        transport_closed = _is_best_effort_browser_error(exc)
        domain_error = exc if isinstance(exc, CamouflareError) else None
        if timed_out:
            record_timeout("navigation")
        if transport_closed and request.cmd == "request.post":
            _emit_browser_transport_error(exc, phase="navigation")
        if domain_error is None and not timed_out and not transport_closed:
            logger.exception(
                "Unexpected navigation error.",
                extra={"error_code": V1ErrorCode.INTERNAL_ERROR.value},
            )
        content = await _safe_page_content(page, limits)
        return V1Response(
            status="error",
            message=_navigation_error_message(exc, page=page, page_response=response),
            error_code=(
                domain_error.error_code
                if domain_error is not None
                else V1ErrorCode.BROWSER_TRANSPORT_CLOSED
                if transport_closed
                else V1ErrorCode.NAVIGATION_TIMEOUT
                if timed_out
                else V1ErrorCode.INTERNAL_ERROR
            ),
            retryable=(
                domain_error.retryable
                if domain_error is not None
                else request.cmd == "request.get" and (transport_closed or timed_out)
            ),
            request_outcome_unknown=(
                domain_error.request_outcome_unknown
                if domain_error is not None
                else request.cmd == "request.post" and (transport_closed or timed_out)
            ),
            solution=await _collect_solution(
                request,
                context=context,
                page=page,
                page_response=response,
                content=content,
                turnstile_token=turnstile_token,
                limits=limits,
            ),
        )

    fallback_used = True if getattr(response, "fallback_used", False) else None
    await _wait_networkidle_best_effort(page, timer)
    detected = await _challenge_detected(page, limits)
    record_challenge("detected" if detected else "not_detected")
    if detected:
        await _apply_media_blocking(context, False)
        try:
            turnstile_token = await _solve_challenge(
                provider, page=page, request=request, timer=timer
            )
        except ChallengeSolveError as exc:
            if "timed out" in str(exc).lower():
                record_timeout("challenge")
                record_challenge("timeout")
            else:
                record_challenge("failed")
            content = await _safe_page_content(page, limits)
            return V1Response(
                status="error",
                message=str(exc),
                error_code=V1ErrorCode.CHALLENGE_FAILED,
                retryable=False,
                fallback_used=fallback_used,
                solution=await _collect_solution(
                    request,
                    context=context,
                    page=page,
                    page_response=response,
                    content=content,
                    turnstile_token=turnstile_token,
                    limits=limits,
                ),
            )
        await _wait_for_challenge_cleared(page, timer, limits=limits, sleep=sleep)

    if request.wait_in_seconds and request.wait_in_seconds > 0:
        try:
            await _wait_requested(request.wait_in_seconds, sleep=sleep, timer=timer)
        except RequestTimeoutError as exc:
            record_timeout("collection")
            content = await _safe_page_content(page, limits)
            return V1Response(
                status="error",
                message=str(exc),
                error_code=V1ErrorCode.REQUEST_TIMEOUT,
                retryable=request.cmd == "request.get",
                request_outcome_unknown=False,
                fallback_used=fallback_used,
                solution=await _collect_solution(
                    request,
                    context=context,
                    page=page,
                    page_response=response,
                    content=content,
                    turnstile_token=turnstile_token,
                    limits=limits,
                ),
            )

    effective_response = (
        response if getattr(response, "raw_body", False) else final_response["value"] or response
    )
    content = await _response_text_or_page_content(page, effective_response, limits)
    title = await _safe_page_title(page)
    if _challenge_markers_remain(title, content):
        if detected:
            record_challenge("failed")
        return V1Response(
            status="error",
            message="Challenge remained after solving attempt.",
            error_code=V1ErrorCode.CHALLENGE_FAILED,
            retryable=False,
            fallback_used=fallback_used,
            solution=await _collect_solution(
                request,
                context=context,
                page=page,
                page_response=effective_response,
                content=content,
                turnstile_token=turnstile_token,
                limits=limits,
            ),
        )

    if detected:
        record_challenge("solved")
    return V1Response(
        status="ok",
        message="Challenge solved!" if detected else "Challenge not detected!",
        fallback_used=fallback_used,
        solution=await _collect_solution(
            request,
            context=context,
            page=page,
            page_response=effective_response,
            content=content,
            turnstile_token=turnstile_token,
            limits=limits,
        ),
    )


def _emit_browser_transport_error(exc: Exception, *, phase: str) -> None:
    record_browser_transport_error(phase)
    logger.warning(
        "Browser transport error.",
        extra={
            "phase": phase,
            "error_type": type(exc).__name__,
            "browser_state": None,
            "slot_uses": None,
            "slot_active_contexts": None,
            "retire_reason": None,
            "fallback_used": False,
        },
    )


async def _navigate_get(
    page: PageLike,
    request: V1Request,
    timer: TimeoutTimer,
    limits: ResourceLimits,
    *,
    allow_direct_http_fallback: bool = True,
    allow_direct_http_first: bool = True,
) -> Any:
    return await _navigation.navigate_get(
        page,
        request,
        timer,
        limits,
        allow_direct_http_fallback=allow_direct_http_fallback,
        allow_direct_http_first=allow_direct_http_first,
        direct_http_get=_direct_http_get,
    )


async def _submit_post(
    context: BrowserContextLike,
    page: PageLike,
    request: V1Request,
    timer: TimeoutTimer,
    limits: ResourceLimits,
) -> Any:
    return await _navigation.submit_post(context, page, request, timer, limits)


async def _submit_post_form(
    page: PageLike,
    request: V1Request,
    timer: TimeoutTimer,
) -> Any:
    return await _navigation.submit_post_form(page, request, timer)


async def _apply_media_blocking(context: BrowserContextLike, enabled: bool) -> None:
    installed = _media_routes.get(context)
    if enabled:
        if installed is not None:
            return

        async def abort(route: RouteLike) -> None:
            await route.abort()

        handlers: list[tuple[str, Any]] = []
        for pattern in MEDIA_PATTERNS:
            await context.route(pattern, abort)
            handlers.append((pattern, abort))
        _media_routes[context] = handlers
        return

    if installed is None:
        return
    unroute = getattr(context, "unroute", None)
    if unroute is not None:
        for pattern, handler in installed:
            with suppress(Exception):
                await unroute(pattern, handler)
    del _media_routes[context]


def _track_main_frame_responses(
    page: PageLike,
    holder: MainFrameResponseHolder,
) -> None:
    on = getattr(page, "on", None)
    if on is None:
        return

    def record(response: ResponseLike) -> None:
        try:
            request = response.request  # type: ignore[attr-defined]
            if request.is_navigation_request() and response.frame == page.main_frame:  # type: ignore[attr-defined]
                holder["value"] = response
        except Exception:
            return

    on("response", record)


async def _install_user_agent_override(page: PageLike, user_agent: str | None) -> None:
    if not user_agent:
        return
    add_init_script = getattr(page, "add_init_script", None)
    if add_init_script is None:
        return

    serialized = json.dumps(user_agent)
    await add_init_script(
        f"""
(() => {{
  const userAgent = {serialized};
  try {{
    if (typeof window.setNavigatorUserAgent === "function") {{
      window.setNavigatorUserAgent(userAgent);
    }}
  }} catch (_) {{}}
  try {{
    Object.defineProperty(Navigator.prototype, "userAgent", {{
      get: () => userAgent,
      configurable: true,
    }});
  }} catch (_) {{}}
}})();
"""
    )
