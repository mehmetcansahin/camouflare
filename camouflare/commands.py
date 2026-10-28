from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import uuid4

from camouflare.captcha import CaptchaProvider
from camouflare.cleanup import CleanupSupervisor
from camouflare.config import Settings, normalize_proxy
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.http_transport import HttpFetch, HttpResult, HttpTransport, with_default_headers
from camouflare.limits import (
    MAX_COOKIE_BYTES,
    MAX_COOKIES,
    MAX_HTTP_REDIRECTS,
    ResourceLimitError,
    ensure_json_size,
    utf8_size,
)
from camouflare.metrics import observe_payload_size, record_session_event
from camouflare.models import Solution, V1Request, V1Response
from camouflare.navigation import (
    active_http_transport,
    origin_bound_target_headers,
    target_request_headers,
)
from camouflare.pool import BrowserPool, PersistentCapacityError
from camouflare.protocols import BrowserProxy, ContextOptions, PageLike
from camouflare.proxy import open_proxy_lease
from camouflare.sessions import Session, SessionManager, SessionUnavailableError
from camouflare.solution import is_best_effort_browser_error, response_charset
from camouflare.solver import solve_request
from camouflare.timer import TimeoutTimer

logger = logging.getLogger(__name__)

KNOWN_COMMANDS = frozenset(
    {
        "sessions.create",
        "sessions.list",
        "sessions.destroy",
        "request.get",
        "request.post",
        "request.head",
    }
)

# Parameters that only the HTTP transport can honor.
_HTTP_TRANSPORT_PARAMETERS = (
    ("responseEncoding", "response_encoding"),
    ("maxResponseBytes", "max_response_bytes"),
    ("followRedirects", "follow_redirects"),
)
# Framing and proxy headers belong to the transport, never to the caller.
_HTTP_TRANSPORT_FORBIDDEN_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# Keeps a classified transport error ahead of the /v1 hard deadline.
_HTTP_TRANSPORT_RESERVE_MS = 1000


@dataclass(frozen=True)
class CommandService:
    settings: Settings
    pool: BrowserPool
    sessions: SessionManager
    captcha_provider: CaptchaProvider
    cleanup: CleanupSupervisor | None = None
    http_transport: HttpTransport | None = None

    async def dispatch(
        self,
        request: V1Request,
        *,
        start_timestamp: int,
        timer: TimeoutTimer | None = None,
    ) -> V1Response:
        if self.http_transport is None:
            return await self._dispatch(request, start_timestamp=start_timestamp, timer=timer)
        with active_http_transport(self.http_transport):
            return await self._dispatch(request, start_timestamp=start_timestamp, timer=timer)

    async def _dispatch(
        self,
        request: V1Request,
        *,
        start_timestamp: int,
        timer: TimeoutTimer | None,
    ) -> V1Response:
        return await dispatch_v1(
            request,
            pool=self.pool,
            sessions=self.sessions,
            settings=self.settings,
            captcha_provider=self.captcha_provider,
            cleanup=self.cleanup,
            start_timestamp=start_timestamp,
            timer=timer,
            http_transport=self.http_transport,
        )


async def dispatch_v1(
    request: V1Request,
    *,
    pool: BrowserPool,
    sessions: SessionManager,
    settings: Settings,
    captcha_provider: CaptchaProvider,
    start_timestamp: int,
    cleanup: CleanupSupervisor | None = None,
    timer: TimeoutTimer | None = None,
    http_transport: HttpTransport | None = None,
) -> V1Response:
    # maxTimeout is one absolute budget for the whole command: session-creation
    # and solve waits share it instead of restarting it once a page is ready.
    timer = timer if timer is not None else TimeoutTimer(request.max_timeout)
    if not request.cmd:
        raise CamouflareError(
            "Request parameter 'cmd' is mandatory.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    if request.cmd in {"sessions.create", "sessions.list", "sessions.destroy"}:
        given = [
            name
            for name, field in _HTTP_TRANSPORT_PARAMETERS
            if getattr(request, field) is not None
        ]
        if request.transport == "http":
            given.insert(0, "transport")
        if given:
            raise _invalid_request(
                f"Request parameters {', '.join(given)} apply only to request commands "
                "over transport 'http'."
            )
    if request.cmd != "sessions.destroy":
        # Keep the target alive until session_for_request can rotate it while
        # preserving its proxy and TTL.
        await sessions.prune_expired(
            exclude=request.session,
            wait_for_cleanup=False,
        )

    if request.cmd == "sessions.create":
        return await sessions_create(
            request,
            pool=pool,
            sessions=sessions,
            settings=settings,
            cleanup=cleanup,
            timer=timer,
        )
    if request.cmd == "sessions.list":
        return V1Response(status="ok", sessions=sessions.list_ids(), version=settings.version)
    if request.cmd == "sessions.destroy":
        destroyed = await sessions.destroy(request.session)
        if not destroyed:
            raise CamouflareError(
                "The session doesn't exist.",
                error_code=V1ErrorCode.SESSION_NOT_FOUND,
            )
        return V1Response(
            status="ok",
            message="The session has been removed.",
            version=settings.version,
        )
    if request.cmd in {"request.get", "request.post", "request.head"}:
        return await execute_request(
            request,
            pool=pool,
            sessions=sessions,
            settings=settings,
            captcha_provider=captcha_provider,
            cleanup=cleanup,
            start_timestamp=start_timestamp,
            timer=timer,
            http_transport=http_transport,
        )
    raise CamouflareError(
        f"Request parameter 'cmd' = '{request.cmd}' is invalid.",
        error_code=V1ErrorCode.INVALID_REQUEST,
    )


async def sessions_create(
    request: V1Request,
    *,
    pool: BrowserPool,
    sessions: SessionManager,
    settings: Settings,
    cleanup: CleanupSupervisor | None = None,
    timer: TimeoutTimer | None = None,
) -> V1Response:
    timer = timer if timer is not None else TimeoutTimer(request.max_timeout)
    session_id = request.session or None
    existing = sessions.get(session_id) if session_id is not None else None
    if existing is not None:
        return V1Response(
            status="ok",
            message="Session already exists.",
            session=existing.session_id,
            version=settings.version,
        )
    session, created = await _create_session(
        request,
        session_id,
        proxy=resolve_proxy(request.proxy, settings.env_proxy),
        ttl_seconds=resolve_ttl_seconds(request, settings),
        pool=pool,
        sessions=sessions,
        settings=settings,
        cleanup=cleanup,
        timer=timer,
    )
    return V1Response(
        status="ok",
        message="Session already exists." if not created else "Session created successfully.",
        session=session.session_id,
        version=settings.version,
    )


async def _create_session(
    request: V1Request,
    session_id: str | None,
    *,
    proxy: dict[str, str] | None,
    ttl_seconds: int | None,
    pool: BrowserPool,
    sessions: SessionManager,
    settings: Settings,
    cleanup: CleanupSupervisor | None,
    timer: TimeoutTimer,
) -> tuple[Session, bool]:
    """Register a new persistent session, or return the winner of a same-id race."""

    if session_id is not None:
        # A closing tombstone still holds this id and its capacity. Building a
        # context first would only turn that into a misleading capacity error.
        sessions.raise_if_closing(session_id)
    proxy_lease = await open_proxy_lease(proxy)
    building = sessions.creating(session_id) if session_id is not None else nullcontext()
    try:
        with building:
            persistent = await pool.create_persistent_context(
                **context_options(proxy_lease.browser_proxy, request)
            )
    except PersistentCapacityError:
        await _close_proxy_best_effort(proxy_lease, cleanup=cleanup, settings=settings)
        if session_id is not None:
            # The last persistent slot may belong to a same-id request that is
            # still building its context. Wait for its actual outcome, within this
            # command's deadline, before rejecting a race that request will win.
            winner = await sessions.wait_for_creation(
                session_id,
                timeout=timer.remaining_seconds,
            )
            if winner is not None:
                return winner, False
        record_session_event("rejected")
        raise
    except BaseException:
        await _close_proxy_best_effort(proxy_lease, cleanup=cleanup, settings=settings)
        raise

    async def close_session_resources() -> None:
        await _close_persistent_resources(
            persistent,
            proxy_lease,
            cleanup=cleanup,
            settings=settings,
        )

    # Registration runs without awaiting after the build settles, so a same-id
    # waiter woken by that settlement always observes the registered winner.
    try:
        session, created = sessions.register_or_get(
            session_id or generated_session_id(),
            persistent.context,
            proxy=proxy,
            on_close=close_session_resources,
            ttl_seconds=ttl_seconds,
        )
    except BaseException:
        await close_session_resources()
        raise
    if not created:
        await close_session_resources()
    return session, created


async def execute_request(
    request: V1Request,
    *,
    pool: BrowserPool,
    sessions: SessionManager,
    settings: Settings,
    captcha_provider: CaptchaProvider,
    start_timestamp: int,
    cleanup: CleanupSupervisor | None = None,
    timer: TimeoutTimer | None = None,
    http_transport: HttpTransport | None = None,
) -> V1Response:
    timer = timer if timer is not None else TimeoutTimer(request.max_timeout)
    if request.cmd in {"request.get", "request.post", "request.head"} and not request.url:
        raise CamouflareError(
            f"Request parameter 'url' is mandatory in '{request.cmd}' command.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    if request.cmd == "request.post" and request.post_data is None:
        raise CamouflareError(
            "Request parameter 'postData' is mandatory in 'request.post' command.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )

    transport = request_transport(request)
    if transport == "http":
        if http_transport is None:
            raise RuntimeError("HTTP transport is not configured for this runtime.")
        response = await execute_http_request(
            request,
            http_transport=http_transport,
            settings=settings,
            timer=timer,
        )
        response.version = settings.version
        response.start_timestamp = start_timestamp
        return response
    # An explicit browser transport never substitutes a direct HTTP request.
    allow_direct_http = transport is None

    if request.session:
        session = await session_for_request(
            request,
            pool=pool,
            sessions=sessions,
            settings=settings,
            cleanup=cleanup,
            timer=timer,
        )
        sessions.mark_in_use(session)
        try:
            async with session.lock:
                if sessions.get(request.session) is not session:
                    # Evicted, destroyed, or rotated while this request waited for
                    # the lock. Nothing was sent, so any command may retry.
                    raise SessionUnavailableError(
                        "The session was closed by a concurrent request; "
                        "retry to use a fresh session."
                    )
                session.touch()
                existing_page_ids = _context_page_ids(session.context)
                page: PageLike | None = None
                try:
                    try:
                        page = await session.context.new_page()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # A failed page creation gives us no evidence that this
                        # persistent context can serve another request. Quarantine
                        # it even when the driver introduces an unfamiliar error
                        # string; only the public error classification remains
                        # marker-based.
                        await sessions.evict_if_current(session)
                        if is_best_effort_browser_error(exc):
                            raise _page_creation_transport_error(request) from exc
                        raise
                    response = await solve_request(
                        request,
                        context=session.context,
                        page=page,
                        captcha_provider=captcha_provider,
                        limits=settings.resource_limits,
                        # A direct client cannot faithfully reproduce all
                        # persistent browser-context state (notably cookies),
                        # so a failed session navigation must stay a browser
                        # transport error instead of making an unauthenticated
                        # fallback request.
                        allow_direct_http_fallback=False,
                        allow_direct_http_first=False,
                        cleanup_supervisor=cleanup,
                        cleanup_timeout_seconds=settings.cleanup_timeout_seconds,
                        timer=timer,
                    )
                    if response.error_code is V1ErrorCode.BROWSER_TRANSPORT_CLOSED:
                        await _evict_session_if_context_unusable(
                            session,
                            sessions=sessions,
                        )
                finally:
                    await close_request_pages(
                        session.context,
                        page,
                        existing_page_ids=existing_page_ids,
                        cleanup_supervisor=cleanup,
                        timeout_seconds=settings.cleanup_timeout_seconds,
                    )
        finally:
            sessions.mark_released(session)
    else:
        proxy = resolve_proxy(request.proxy, settings.env_proxy)
        proxy_lease = await open_proxy_lease(proxy)
        response = None
        solve_started = False
        try:
            try:
                async with pool.lease_context(
                    **context_options(proxy_lease.browser_proxy, request)
                ) as lease:
                    existing_page_ids = _context_page_ids(lease.context)
                    page = None
                    try:
                        page = await lease.context.new_page()
                        solve_started = True
                        response = await solve_request(
                            request,
                            context=lease.context,
                            page=page,
                            captcha_provider=captcha_provider,
                            limits=settings.resource_limits,
                            allow_direct_http_fallback=allow_direct_http and proxy is None,
                            allow_direct_http_first=allow_direct_http and proxy is None,
                            cleanup_supervisor=cleanup,
                            cleanup_timeout_seconds=settings.cleanup_timeout_seconds,
                            timer=timer,
                        )
                    finally:
                        await close_request_pages(
                            lease.context,
                            page,
                            existing_page_ids=existing_page_ids,
                            cleanup_supervisor=cleanup,
                            timeout_seconds=settings.cleanup_timeout_seconds,
                        )
            except Exception as exc:
                if response is not None:
                    logger.warning(
                        "Ignoring browser cleanup error after a completed solve.", exc_info=True
                    )
                elif not solve_started and is_best_effort_browser_error(exc):
                    # The browser transport closed while the leased context or
                    # its page was being opened, before any target request. A
                    # retry leases a fresh context, and the pool retires a
                    # disconnected browser before handing it out again.
                    raise _page_creation_transport_error(request) from exc
                else:
                    raise
        finally:
            await _close_proxy_best_effort(
                proxy_lease,
                cleanup=cleanup,
                settings=settings,
            )

    assert response is not None
    if response.status == "error":
        raise CamouflareError(
            response.message,
            error_code=response.error_code or V1ErrorCode.INTERNAL_ERROR,
            retryable=bool(response.retryable),
            request_outcome_unknown=bool(response.request_outcome_unknown),
            fallback_used=response.fallback_used,
            solution=response.solution,
        )
    response.version = settings.version
    response.start_timestamp = start_timestamp
    return response


def request_transport(request: V1Request) -> Literal["http", "browser"] | None:
    """Return the requested transport after rejecting options it cannot honor.

    None keeps the automatic browser-or-direct-HTTP selection of request.get and
    request.post.
    """

    transport = request.transport
    if request.cmd == "request.head":
        if transport == "browser":
            raise _invalid_request("request.head supports only transport 'http'.")
        transport = "http"
    if transport != "http":
        given = [
            name
            for name, field in _HTTP_TRANSPORT_PARAMETERS
            if getattr(request, field) is not None
        ]
        if given:
            raise _invalid_request(
                f"Request parameters {', '.join(given)} require transport 'http'."
            )
        if (
            transport == "browser"
            and request.cmd == "request.get"
            and origin_bound_target_headers(request)
        ):
            raise _invalid_request(
                "Transport 'browser' cannot send non-User-Agent headers on request.get; "
                "use transport 'http'."
            )
        return transport
    if request.cmd == "request.post":
        raise _invalid_request("request.post does not support transport 'http'.")
    unsupported = [
        name
        for name, present in (
            ("session", bool(request.session)),
            ("session_ttl_minutes", request.session_ttl_minutes is not None),
            ("returnScreenshot", request.return_screenshot),
            ("waitInSeconds", bool(request.wait_in_seconds)),
            ("disableMedia", bool(request.disable_media)),
            ("postData", request.post_data is not None),
        )
        if present
    ]
    if unsupported:
        raise _invalid_request(
            f"Transport 'http' does not support request parameters {', '.join(unsupported)}."
        )
    forbidden = sorted(
        {
            str(name)
            for name in request.headers or {}
            if str(name).lower() in _HTTP_TRANSPORT_FORBIDDEN_HEADERS
        }
    )
    if forbidden:
        raise _invalid_request(
            f"Transport 'http' manages these request headers itself: {', '.join(forbidden)}."
        )
    return "http"


async def execute_http_request(
    request: V1Request,
    *,
    http_transport: HttpTransport,
    settings: Settings,
    timer: TimeoutTimer,
) -> V1Response:
    """Run request.get or request.head over the HTTP transport, without a browser."""

    timeout_ms = timer.remaining_ms - min(_HTTP_TRANSPORT_RESERVE_MS, timer.remaining_ms // 10)
    result = await http_transport.fetch(
        HttpFetch(
            url=cast(str, request.url),
            method="HEAD" if request.cmd == "request.head" else "GET",
            headers=with_default_headers(target_request_headers(request)),
            origin_bound_header_names=frozenset(
                name.lower() for name in origin_bound_target_headers(request)
            ),
            cookies=request.cookies,
            proxy=resolve_proxy(request.proxy, settings.env_proxy),
            max_redirects=(
                MAX_HTTP_REDIRECTS if request.follow_redirects is None else request.follow_redirects
            ),
            max_body_bytes=request.max_response_bytes or settings.max_response_body_bytes,
            body="none" if request.return_only_cookies else "full",
            require_https=True,
            block_private_targets=True,
        ),
        timeout_seconds=timeout_ms / 1000,
    )
    return V1Response(
        status="ok",
        message="HTTP request completed.",
        solution=_http_solution(request, result),
    )


def _http_solution(request: V1Request, result: HttpResult) -> Solution:
    cookies = result.solution_cookies
    if len(cookies) > MAX_COOKIES:
        raise ResourceLimitError(
            f"Response cookies exceed the configured {MAX_COOKIES}-item limit."
        )
    ensure_json_size(cookies, MAX_COOKIE_BYTES, label="Response cookies")
    user_agent = result.user_agent or ""
    if request.return_only_cookies:
        return Solution(
            url=result.url, status=result.status, cookies=cookies, user_agent=user_agent
        )

    encoding = request.response_encoding or "text"
    if encoding == "base64":
        response = base64.b64encode(result.body).decode("ascii")
    else:
        response = result.body.decode(response_charset(result.message), errors="replace")
    observe_payload_size("response", utf8_size(response))
    return Solution(
        url=result.url,
        status=result.status,
        headers=_http_solution_headers(result),
        response=response,
        cookies=cookies,
        user_agent=user_agent,
        response_encoding=encoding,
        body_bytes=len(result.body),
    )


def _http_solution_headers(result: HttpResult) -> dict[str, str]:
    """Lower-cased response headers that describe the body actually returned.

    Repeated fields are combined (Set-Cookie with newlines, others with commas).
    Once content codings are removed, Content-Encoding and Content-Length describe
    the wire body, not the returned one, so they are omitted; bodyBytes is exact.
    """

    combined: dict[str, str] = {}
    for name, value in result.headers:
        key = name.lower()
        if result.content_decoded and key in {"content-encoding", "content-length"}:
            continue
        if key in combined:
            combined[key] += ("\n" if key == "set-cookie" else ", ") + value
        else:
            combined[key] = value
    return combined


def _invalid_request(message: str) -> CamouflareError:
    return CamouflareError(message, error_code=V1ErrorCode.INVALID_REQUEST)


async def session_for_request(
    request: V1Request,
    *,
    pool: BrowserPool,
    sessions: SessionManager,
    settings: Settings,
    cleanup: CleanupSupervisor | None = None,
    timer: TimeoutTimer | None = None,
) -> Session:
    assert request.session is not None
    timer = timer if timer is not None else TimeoutTimer(request.max_timeout)
    ttl_seconds: int | None = resolve_ttl_seconds(request, settings)
    existing = sessions.get(request.session)
    if existing is not None and (
        not existing.expired() or existing.lock.locked() or existing.in_use > 0
    ):
        return existing
    if existing is not None:
        proxy = existing.proxy
        if request.session_ttl_minutes is None:
            ttl_seconds = existing.ttl_seconds
        await sessions.destroy(request.session)
        record_session_event("rotated")
    else:
        proxy = resolve_proxy(request.proxy, settings.env_proxy)
    session, _created = await _create_session(
        request,
        request.session,
        proxy=proxy,
        ttl_seconds=ttl_seconds,
        pool=pool,
        sessions=sessions,
        settings=settings,
        cleanup=cleanup,
        timer=timer,
    )
    return session


def _page_creation_transport_error(request: V1Request) -> CamouflareError:
    return CamouflareError(
        "Browser transport closed before a request page could be created.",
        error_code=V1ErrorCode.BROWSER_TRANSPORT_CLOSED,
        retryable=request.cmd == "request.get",
        # No target request can have been sent before BrowserContext.new_page()
        # returns, so even a POST has a known (unsent) outcome.
        request_outcome_unknown=False,
    )


def context_options(
    proxy: dict[str, str] | None,
    request: V1Request | None = None,
) -> ContextOptions:
    options: ContextOptions = {"no_viewport": True}
    if proxy:
        options["proxy"] = cast(BrowserProxy, proxy)
    if request:
        user_agent = request.target_user_agent()
        if user_agent:
            options["user_agent"] = user_agent
    return options


def resolve_proxy(
    request_proxy: dict[str, Any] | None,
    env_proxy: dict[str, str] | None,
) -> dict[str, str] | None:
    proxy = request_proxy or env_proxy
    if not proxy:
        return None
    normalized = normalize_proxy(proxy)
    if normalized is None:
        raise CamouflareError(
            "Request parameter 'proxy' must include a 'url' (or 'server').",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    return normalized


def resolve_ttl_seconds(request: V1Request, settings: Settings) -> int:
    if request.session_ttl_minutes is not None:
        return request.session_ttl_minutes * 60
    return settings.session_ttl_seconds


async def close_page(
    page: PageLike,
    *,
    cleanup_supervisor: CleanupSupervisor | None = None,
    timeout_seconds: float = 10,
) -> None:
    supervisor = cleanup_supervisor or CleanupSupervisor(timeout_seconds=timeout_seconds)
    try:
        await supervisor.run(page.close(), kind="page", timeout_seconds=timeout_seconds)
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, Exception) and is_best_effort_browser_error(exc):
            logger.debug("Ignoring best-effort page close error: %s", exc)
        elif isinstance(exc, TimeoutError):
            logger.warning("Page cleanup exceeded %.3f seconds.", timeout_seconds)
        else:
            logger.warning("Ignoring page close error after a completed solve.", exc_info=True)


def _context_page_ids(context: Any) -> set[int]:
    try:
        return {id(page) for page in context.pages}
    except Exception:
        return set()


async def _evict_session_if_context_unusable(
    session: Session,
    *,
    sessions: SessionManager,
) -> None:
    """Probe a session after a transport error and quarantine only a dead context.

    A renderer crash is reported with the same public error code as a browser or
    context disconnect. Successfully opening a fresh page proves the persistent
    context is still usable; the normal request-page cleanup closes that probe page.
    Any probe failure leaves usability unproven, so the exact session is quarantined
    even if the driver error text is new to the public classifier.
    """

    try:
        await session.context.new_page()
    except asyncio.CancelledError:
        # The original solve already proved a transport failure, and cancellation
        # leaves this follow-up probe inconclusive. Do not return an unproven
        # persistent context to future callers.
        await sessions.evict_if_current(session)
        raise
    except Exception as exc:
        if not is_best_effort_browser_error(exc):
            logger.warning(
                "Session context probe failed with an unrecognized error after "
                "a browser transport failure; evicting the session.",
                exc_info=True,
            )
        await sessions.evict_if_current(session)


async def close_request_pages(
    context: Any,
    primary_page: PageLike | None,
    *,
    existing_page_ids: set[int],
    cleanup_supervisor: CleanupSupervisor | None = None,
    timeout_seconds: float = 10,
) -> None:
    """Close the primary page and every popup created during this request."""
    pages: list[PageLike] = []
    seen: set[int] = set()
    try:
        candidates = list(context.pages)
    except Exception:
        candidates = []
    if primary_page is not None:
        candidates.append(primary_page)
    for page in candidates:
        identity = id(page)
        if identity in existing_page_ids or identity in seen:
            continue
        seen.add(identity)
        pages.append(page)

    await asyncio.gather(
        *(
            close_page(
                page,
                cleanup_supervisor=cleanup_supervisor,
                timeout_seconds=timeout_seconds,
            )
            for page in pages
        )
    )


async def _close_proxy_best_effort(
    proxy_lease: Any,
    *,
    cleanup: CleanupSupervisor | None,
    settings: Settings,
) -> None:
    supervisor = cleanup or CleanupSupervisor(timeout_seconds=settings.cleanup_timeout_seconds)
    try:
        await supervisor.run(
            proxy_lease.close(),
            kind="proxy",
            timeout_seconds=settings.cleanup_timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Ignoring proxy cleanup error.", exc_info=True)


async def _close_persistent_resources(
    persistent: Any,
    proxy_lease: Any,
    *,
    cleanup: CleanupSupervisor | None,
    settings: Settings,
) -> None:
    supervisor = cleanup or CleanupSupervisor(timeout_seconds=settings.cleanup_timeout_seconds)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.cleanup_timeout_seconds
    try:
        await supervisor.run(
            persistent.close(),
            kind="context",
            timeout_seconds=max(0.001, deadline - loop.time()),
        )
    finally:
        await supervisor.run(
            proxy_lease.close(),
            kind="proxy",
            timeout_seconds=max(0.001, deadline - loop.time()),
        )


def generated_session_id() -> str:
    return str(uuid4())
