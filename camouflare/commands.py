from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from camouflare.captcha import CaptchaProvider
from camouflare.config import Settings, normalize_proxy
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

    async def dispatch(self, request: V1Request, *, start_timestamp: int) -> V1Response:
        return await dispatch_v1(
            request,
            pool=self.pool,
            sessions=self.sessions,
            settings=self.settings,
            captcha_provider=self.captcha_provider,
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
) -> V1Response:
    if not request.cmd:
        raise RuntimeError("Request parameter 'cmd' is mandatory.")
    if request.cmd != "sessions.destroy":
        # Keep the target alive until session_for_request can rotate it while
        # preserving its proxy and TTL.
        await sessions.prune_expired(exclude=request.session)

    if request.cmd == "sessions.create":
        return await sessions_create(request, pool=pool, sessions=sessions, settings=settings)
    if request.cmd == "sessions.list":
        return V1Response(status="ok", sessions=sessions.list_ids(), version=settings.version)
    if request.cmd == "sessions.destroy":
        destroyed = await sessions.destroy(request.session)
        if not destroyed:
            raise RuntimeError("The session doesn't exist.")
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
            start_timestamp=start_timestamp,
        )
    raise RuntimeError(f"Request parameter 'cmd' = '{request.cmd}' is invalid.")


async def sessions_create(
    request: V1Request,
    *,
    pool: BrowserPool,
    sessions: SessionManager,
    settings: Settings,
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
        await proxy_lease.close()
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
    except Exception:
        await proxy_lease.close()
        raise

    async def close_session_resources() -> None:
        try:
            await persistent.close()
        finally:
            await proxy_lease.close()

    try:
        session, created = sessions.register_or_get(
            session_id or generated_session_id(),
            persistent.context,
            proxy=proxy,
            on_close=close_session_resources,
            ttl_seconds=ttl_seconds,
        )
    except Exception:
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
) -> V1Response:
    if request.cmd == "request.get" and not request.url:
        raise RuntimeError("Request parameter 'url' is mandatory in 'request.get' command.")
    if request.cmd == "request.post" and not request.url:
        raise RuntimeError("Request parameter 'url' is mandatory in 'request.post' command.")
    if request.cmd == "request.post" and request.post_data is None:
        raise RuntimeError("Request parameter 'postData' is mandatory in 'request.post' command.")

    if request.session:
        session = await session_for_request(
            request,
            pool=pool,
            sessions=sessions,
            settings=settings,
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
                    )
                finally:
                    await close_page(page)
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
                        )
                    finally:
                        await close_page(page)
            except Exception:
                if response is None:
                    raise
                logger.warning(
                    "Ignoring browser cleanup error after a completed solve.", exc_info=True
                )
        finally:
            await proxy_lease.close()

    assert response is not None
    response.version = settings.version
    response.start_timestamp = start_timestamp
    return response


async def session_for_request(
    request: V1Request,
    *,
    pool: BrowserPool,
    sessions: SessionManager,
    settings: Settings,
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
    except Exception:
        await proxy_lease.close()
        raise

    async def close_session_resources() -> None:
        try:
            await persistent.close()
        finally:
            await proxy_lease.close()

    try:
        session, created = sessions.register_or_get(
            request.session,
            persistent.context,
            proxy=proxy,
            on_close=close_session_resources,
            ttl_seconds=ttl_seconds,
        )
    except Exception:
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
        raise RuntimeError("Request parameter 'proxy' must include a 'url' (or 'server').")
    return normalized


def resolve_ttl_seconds(request: V1Request, settings: Settings) -> int:
    if request.session_ttl_minutes is not None:
        return request.session_ttl_minutes * 60
    return settings.session_ttl_seconds


async def close_page(page: PageLike) -> None:
    try:
        await page.close()
    except Exception as exc:
        if is_best_effort_browser_error(exc):
            logger.debug("Ignoring best-effort page close error: %s", exc)
        else:
            logger.warning("Ignoring page close error after a completed solve.", exc_info=True)


def generated_session_id() -> str:
    return str(uuid4())
