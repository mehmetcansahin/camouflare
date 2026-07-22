from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from camouflare.captcha import CaptchaProvider
from camouflare.cleanup import CleanupSupervisor
from camouflare.config import Settings, normalize_proxy
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.metrics import record_session_event
from camouflare.models import V1Request, V1Response
from camouflare.pool import BrowserPool, PersistentCapacityError
from camouflare.protocols import BrowserProxy, ContextOptions, PageLike
from camouflare.proxy import open_proxy_lease
from camouflare.sessions import Session, SessionManager
from camouflare.solution import is_best_effort_browser_error
from camouflare.solver import solve_request

logger = logging.getLogger(__name__)

KNOWN_COMMANDS = frozenset(
    {
        "sessions.create",
        "sessions.list",
        "sessions.destroy",
        "request.get",
        "request.post",
    }
)


@dataclass(frozen=True)
class CommandService:
    settings: Settings
    pool: BrowserPool
    sessions: SessionManager
    captcha_provider: CaptchaProvider
    cleanup: CleanupSupervisor | None = None

    async def dispatch(self, request: V1Request, *, start_timestamp: int) -> V1Response:
        return await dispatch_v1(
            request,
            pool=self.pool,
            sessions=self.sessions,
            settings=self.settings,
            captcha_provider=self.captcha_provider,
            cleanup=self.cleanup,
            start_timestamp=start_timestamp,
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
) -> V1Response:
    if not request.cmd:
        raise CamouflareError(
            "Request parameter 'cmd' is mandatory.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    if request.cmd != "sessions.destroy":
        # Keep the target alive until session_for_request can rotate it while
        # preserving its proxy and TTL.
        await sessions.prune_expired(exclude=request.session)

    if request.cmd == "sessions.create":
        return await sessions_create(
            request,
            pool=pool,
            sessions=sessions,
            settings=settings,
            cleanup=cleanup,
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
    if request.cmd in {"request.get", "request.post"}:
        return await execute_request(
            request,
            pool=pool,
            sessions=sessions,
            settings=settings,
            captcha_provider=captcha_provider,
            cleanup=cleanup,
            start_timestamp=start_timestamp,
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
) -> V1Response:
    session_id = request.session
    existing = sessions.get(session_id) if session_id else None
    if existing is not None:
        return V1Response(
            status="ok",
            message="Session already exists.",
            session=existing.session_id,
            version=settings.version,
        )
    proxy = resolve_proxy(request.proxy, settings.env_proxy)
    proxy_lease = await open_proxy_lease(proxy)
    ttl_seconds = resolve_ttl_seconds(request, settings)
    try:
        persistent = await pool.create_persistent_context(
            **context_options(proxy_lease.browser_proxy, request)
        )
    except PersistentCapacityError:
        await _close_proxy_best_effort(proxy_lease, cleanup=cleanup, settings=settings)
        if session_id is not None:
            for _ in range(3):
                winner = sessions.get(session_id)
                if winner is not None:
                    return V1Response(
                        status="ok",
                        message="Session already exists.",
                        session=winner.session_id,
                        version=settings.version,
                    )
                await asyncio.sleep(0)
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
    return V1Response(
        status="ok",
        message="Session already exists." if not created else "Session created successfully.",
        session=session.session_id,
        version=settings.version,
    )


async def execute_request(
    request: V1Request,
    *,
    pool: BrowserPool,
    sessions: SessionManager,
    settings: Settings,
    captcha_provider: CaptchaProvider,
    start_timestamp: int,
    cleanup: CleanupSupervisor | None = None,
) -> V1Response:
    if request.cmd == "request.get" and not request.url:
        raise CamouflareError(
            "Request parameter 'url' is mandatory in 'request.get' command.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    if request.cmd == "request.post" and not request.url:
        raise CamouflareError(
            "Request parameter 'url' is mandatory in 'request.post' command.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    if request.cmd == "request.post" and request.post_data is None:
        raise CamouflareError(
            "Request parameter 'postData' is mandatory in 'request.post' command.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )

    if request.session:
        session = await session_for_request(
            request,
            pool=pool,
            sessions=sessions,
            settings=settings,
            cleanup=cleanup,
        )
        sessions.mark_in_use(session)
        try:
            async with session.lock:
                if sessions.get(request.session) is not session:
                    raise RuntimeError("The session was closed by a concurrent request.")
                session.touch()
                page = await session.context.new_page()
                try:
                    response = await solve_request(
                        request,
                        context=session.context,
                        page=page,
                        captcha_provider=captcha_provider,
                        limits=settings.resource_limits,
                        allow_direct_http_fallback=session.proxy is None,
                        allow_direct_http_first=False,
                        cleanup_supervisor=cleanup,
                        cleanup_timeout_seconds=settings.cleanup_timeout_seconds,
                    )
                finally:
                    await close_page(
                        page,
                        cleanup_supervisor=cleanup,
                        timeout_seconds=settings.cleanup_timeout_seconds,
                    )
        finally:
            sessions.mark_released(session)
    else:
        proxy = resolve_proxy(request.proxy, settings.env_proxy)
        proxy_lease = await open_proxy_lease(proxy)
        response = None
        try:
            try:
                async with pool.lease_context(
                    **context_options(proxy_lease.browser_proxy, request)
                ) as lease:
                    page = await lease.context.new_page()
                    try:
                        response = await solve_request(
                            request,
                            context=lease.context,
                            page=page,
                            captcha_provider=captcha_provider,
                            limits=settings.resource_limits,
                            allow_direct_http_fallback=proxy is None,
                            allow_direct_http_first=proxy is None,
                            cleanup_supervisor=cleanup,
                            cleanup_timeout_seconds=settings.cleanup_timeout_seconds,
                        )
                    finally:
                        await close_page(
                            page,
                            cleanup_supervisor=cleanup,
                            timeout_seconds=settings.cleanup_timeout_seconds,
                        )
            except Exception:
                if response is None:
                    raise
                logger.warning(
                    "Ignoring browser cleanup error after a completed solve.", exc_info=True
                )
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


async def session_for_request(
    request: V1Request,
    *,
    pool: BrowserPool,
    sessions: SessionManager,
    settings: Settings,
    cleanup: CleanupSupervisor | None = None,
) -> Session:
    assert request.session is not None
    ttl_seconds = resolve_ttl_seconds(request, settings)
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
    proxy_lease = await open_proxy_lease(proxy)
    try:
        persistent = await pool.create_persistent_context(
            **context_options(proxy_lease.browser_proxy, request)
        )
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

    try:
        session, created = sessions.register_or_get(
            request.session,
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
    return session


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
