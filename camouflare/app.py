from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError

from camouflare.browser import (
    make_camoufox_browser_factory,
    playwright_cancel_patch_status,
)
from camouflare.captcha import CaptchaProvider, ClickSolverProvider, NoCaptchaProvider
from camouflare.cleanup import CleanupSupervisor
from camouflare.commands import (
    KNOWN_COMMANDS,
    CommandService,
    close_page,
    context_options,
    dispatch_v1,
    execute_request,
    generated_session_id,
    resolve_proxy,
    resolve_ttl_seconds,
    session_for_request,
    sessions_create,
)
from camouflare.config import Settings
from camouflare.documentation import (
    DOCUMENTATION_HTML,
    V1_ENDPOINT_DESCRIPTION,
    V1_REQUEST_EXAMPLES,
)
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.limits import ResourceLimitError, ensure_json_size, json_size
from camouflare.metrics import (
    REQUEST_COUNTER,
    REQUEST_DURATION,
    install_asyncio_exception_metrics,
    metrics_response,
    observe_payload_size,
    observe_readiness,
    record_timeout,
    record_v1_error,
    request_finished,
    request_started,
)
from camouflare.models import (
    DiagnosticsCleanupStatus,
    DiagnosticsPoolStatus,
    DiagnosticsResponse,
    DiagnosticsRuntimeStatus,
    DiagnosticsSessionStatus,
    HealthResponse,
    IndexResponse,
    V1Request,
    V1Response,
)
from camouflare.observability import bind_request_id, reset_request_id, resolve_request_id
from camouflare.pool import BrowserPool, PersistentCapacityError, PoolAcquireTimeout
from camouflare.protocols import BrowserFactory
from camouflare.runtime import make_runtime_lifespan, session_reaper, shutdown_runtime
from camouflare.sessions import SessionManager

logger = logging.getLogger(__name__)

AppLifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]

# Private aliases are retained for callers that used the original single-module
# implementation. Public integrations should use create_app and /v1.
_KNOWN_COMMANDS = KNOWN_COMMANDS
_close_page = close_page
_context_options = context_options
_generated_session_id = generated_session_id
_request = execute_request
_resolve_proxy = resolve_proxy
_resolve_ttl_seconds = resolve_ttl_seconds
_session_for_request = session_for_request
_session_reaper = session_reaper
_sessions_create = sessions_create
_shutdown_runtime = shutdown_runtime


def create_app(
    *,
    settings: Settings | None = None,
    browser_factory: BrowserFactory | None = None,
    captcha_provider: CaptchaProvider | None = None,
    lifespan: AppLifespan | None = None,
    lifespan_enabled: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    cleanup = CleanupSupervisor(timeout_seconds=settings.cleanup_timeout_seconds)
    factory = browser_factory or make_camoufox_browser_factory(
        settings,
        cleanup_supervisor=cleanup,
    )
    if captcha_provider is not None:
        provider = captcha_provider
    elif browser_factory is None and settings.challenge_solver == "click":
        provider = ClickSolverProvider()
    else:
        provider = NoCaptchaProvider()

    pool = BrowserPool(
        browser_factory=factory,
        min_browsers=settings.pool_min_browsers,
        max_browsers=settings.pool_max_browsers,
        max_contexts_per_browser=settings.pool_max_contexts_per_browser,
        browser_max_uses=settings.browser_max_uses,
        browser_max_age_seconds=settings.browser_max_age_seconds,
        acquire_timeout_seconds=settings.pool_acquire_timeout_seconds,
        reserved_transient_contexts=settings.pool_reserved_transient_contexts,
        cleanup_timeout_seconds=settings.cleanup_timeout_seconds,
        cleanup_supervisor=cleanup,
    )
    sessions = SessionManager(
        max_sessions=settings.max_sessions,
        default_ttl_seconds=settings.session_ttl_seconds,
        cleanup_timeout_seconds=settings.cleanup_timeout_seconds,
        cleanup_supervisor=cleanup,
    )
    command_service = CommandService(
        settings=settings,
        pool=pool,
        sessions=sessions,
        captcha_provider=provider,
        cleanup=cleanup,
    )

    if lifespan is None and lifespan_enabled:
        app_lifespan = make_runtime_lifespan(settings)
    elif lifespan is not None:
        app_lifespan = lifespan
    else:
        app_lifespan = None

    app = FastAPI(
        title="Camouflare",
        description=(
            "A FlareSolverr-compatible FastAPI service backed by Camoufox. "
            "Use /v1 for browser-backed commands and /documentation for the "
            "expanded human-readable guide."
        ),
        version=settings.version,
        lifespan=app_lifespan,
    )
    app.add_middleware(GZipMiddleware)
    app.state.settings = settings
    app.state.pool = pool
    app.state.sessions = sessions
    app.state.captcha_provider = provider
    app.state.command_service = command_service
    app.state.resource_limits = settings.resource_limits
    app.state.cleanup = cleanup

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        install_asyncio_exception_metrics()
        request_id = resolve_request_id(request.headers.get("x-request-id"))
        token = bind_request_id(request_id)
        request_started()
        try:
            if (
                settings.camouflare_api_token
                and request.url.path != "/health"
                and not _api_token_matches(request, settings.camouflare_api_token)
            ):
                response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
            else:
                try:
                    response = await call_next(request)
                except Exception:
                    logger.exception("Unhandled HTTP request error.")
                    response = JSONResponse(
                        {"detail": "Internal Server Error"},
                        status_code=500,
                    )
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_finished()
            reset_request_id(token)

    @app.get(
        "/",
        response_model=IndexResponse,
        tags=["Service"],
        summary="Read service metadata",
        description="Returns basic service metadata and the configured Camouflare version.",
    )
    async def index() -> dict[str, Any]:
        return IndexResponse(version=settings.version).model_dump(by_alias=True)

    @app.get("/docs-redirect", include_in_schema=False)
    async def docs_redirect() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get(
        "/documentation",
        response_class=HTMLResponse,
        tags=["Service"],
        summary="Read expanded API documentation",
        description=(
            "Serves a human-readable API reference with command examples, "
            "request fields, response envelopes, errors, sessions, proxy usage, "
            "and configuration."
        ),
    )
    async def documentation() -> HTMLResponse:
        return HTMLResponse(DOCUMENTATION_HTML)

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["Service"],
        summary="Check service liveness",
        description="Returns ok when the API process is alive without reading browser state.",
    )
    async def health() -> dict[str, str]:
        return HealthResponse().model_dump()

    @app.get(
        "/diagnostics",
        response_model=DiagnosticsResponse,
        tags=["Service"],
        summary="Read passive runtime diagnostics",
        description=(
            "Returns browser-pool, session, cleanup, and runtime state without "
            "leasing browser capacity. This endpoint uses the configured API token."
        ),
    )
    async def diagnostics() -> dict[str, Any]:
        return _diagnostics_response(app, settings).model_dump()

    @app.get(
        "/ready",
        response_model=HealthResponse,
        tags=["Service"],
        summary="Check browser readiness",
        description=(
            "Leases a browser context, opens a page, and evaluates JavaScript. "
            "Returns 503 when the browser pool cannot serve a page."
        ),
        responses={
            503: {
                "description": "Browser pool unavailable.",
                "content": {
                    "application/json": {
                        "example": {"status": "error", "message": "browser unavailable"}
                    }
                },
            }
        },
    )
    async def ready() -> Any:
        started = time.monotonic()
        probe_task = asyncio.create_task(
            _readiness_probe(app),
            name="camouflare-readiness-probe",
        )
        try:
            await _await_with_hard_deadline(
                probe_task,
                timeout_seconds=settings.readiness_timeout_seconds,
                cleanup=app.state.cleanup,
                cleanup_kind="readiness",
            )
        except PoolAcquireTimeout as exc:
            observe_readiness(result="unavailable", duration_seconds=time.monotonic() - started)
            return JSONResponse(
                {"status": "error", "message": str(exc)},
                status_code=503,
            )
        except TimeoutError as exc:
            record_timeout("readiness")
            observe_readiness(result="timeout", duration_seconds=time.monotonic() - started)
            message = str(exc) or "Readiness check exceeded its configured timeout."
            return JSONResponse(
                {"status": "error", "message": message},
                status_code=503,
            )
        except Exception as exc:
            observe_readiness(result="unavailable", duration_seconds=time.monotonic() - started)
            return JSONResponse(
                {"status": "error", "message": str(exc)},
                status_code=503,
            )
        observe_readiness(result="success", duration_seconds=time.monotonic() - started)
        return HealthResponse().model_dump()

    @app.get(
        "/metrics",
        tags=["Service"],
        summary="Read Prometheus metrics",
        description=(
            "Returns Prometheus metrics when PROMETHEUS_ENABLED=true. Returns 404 "
            "when metrics are disabled."
        ),
        responses={
            200: {
                "description": "Prometheus metrics text payload.",
                "content": {
                    "text/plain": {
                        "example": (
                            "# HELP camouflare_request_total Total /v1 requests by "
                            "command and result."
                        )
                    }
                },
            },
            404: {
                "description": "Metrics disabled.",
                "content": {
                    "application/json": {"example": {"detail": "Prometheus metrics are disabled."}}
                },
            },
        },
    )
    async def metrics() -> Any:
        if not settings.prometheus_enabled:
            return JSONResponse({"detail": "Prometheus metrics are disabled."}, status_code=404)
        # Age-based recycle state changes while idle, without a pool mutation.
        # Refresh its computed gauges at scrape time while keeping metrics best-effort.
        with suppress(Exception):
            app.state.pool.snapshot()
        return metrics_response()

    @app.post(
        "/v1",
        response_model=V1Response,
        tags=["Commands"],
        summary="Run a Camouflare command",
        description=V1_ENDPOINT_DESCRIPTION,
        responses={
            500: {
                "model": V1Response,
                "description": (
                    "Command failed, command is invalid, a required field is missing, "
                    "or a requested session does not exist."
                ),
            },
            503: {
                "model": V1Response,
                "description": "Browser pool capacity was unavailable before timeout.",
            },
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": V1Request.model_json_schema(by_alias=True),
                        "examples": V1_REQUEST_EXAMPLES,
                    }
                },
            }
        },
    )
    async def controller_v1(request: Request) -> JSONResponse:
        start_timestamp = int(time.time() * 1000)
        command = "unknown"
        result = "error"
        target_host: str | None = None
        started = time.monotonic()
        try:
            payload = await _read_json_payload(
                request,
                maximum_bytes=settings.max_request_body_bytes,
            )
            observe_payload_size("request", request.state.request_body_bytes)
            v1_request = V1Request.model_validate(payload)
            _validate_request_runtime_limits(v1_request, settings)
            command = v1_request.cmd if v1_request.cmd in KNOWN_COMMANDS else "invalid"
            target_host = _safe_target_host(v1_request.url)
            dispatch_task = asyncio.create_task(
                command_service.dispatch(
                    v1_request,
                    start_timestamp=start_timestamp,
                ),
                name=f"camouflare-command-{command}",
            )
            response = await _await_with_hard_deadline(
                dispatch_task,
                timeout_seconds=v1_request.max_timeout / 1000,
                cleanup=app.state.cleanup,
                cleanup_kind="request",
            )
            result = response.status
            status_code = 200 if response.status == "ok" else 500
        except ValidationError as exc:
            response = V1Response.error(
                _validation_error_message(exc),
                version=settings.version,
                start_timestamp=start_timestamp,
                error_code=V1ErrorCode.INVALID_REQUEST,
                retryable=False,
            )
            status_code = 500
        except ResourceLimitError as exc:
            response = V1Response.error(
                f"Error: {exc}",
                version=settings.version,
                start_timestamp=start_timestamp,
                error_code=V1ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                retryable=False,
            )
            status_code = 500
        except (PoolAcquireTimeout, PersistentCapacityError) as exc:
            response = V1Response.error(
                f"Error: {exc}",
                version=settings.version,
                start_timestamp=start_timestamp,
                error_code=V1ErrorCode.POOL_UNAVAILABLE,
                retryable=True,
            )
            status_code = 503
        except CamouflareError as exc:
            response = V1Response.error(
                str(exc),
                version=settings.version,
                start_timestamp=start_timestamp,
                error_code=exc.error_code,
                retryable=exc.retryable,
                request_outcome_unknown=exc.request_outcome_unknown,
                fallback_used=exc.fallback_used,
                solution=exc.solution,
            )
            status_code = 503 if exc.error_code is V1ErrorCode.POOL_UNAVAILABLE else 500
        except TimeoutError:
            record_timeout("request")
            response = V1Response.error(
                "Error: Request exceeded maxTimeout.",
                version=settings.version,
                start_timestamp=start_timestamp,
                error_code=V1ErrorCode.REQUEST_TIMEOUT,
                retryable=command != "request.post",
                request_outcome_unknown=command == "request.post",
            )
            status_code = 500
        except Exception as exc:
            logger.exception(
                "Unexpected /v1 command error.",
                extra={"error_code": V1ErrorCode.INTERNAL_ERROR.value},
            )
            response = V1Response.error(
                f"Error: {exc}",
                version=settings.version,
                start_timestamp=start_timestamp,
                error_code=V1ErrorCode.INTERNAL_ERROR,
                retryable=False,
            )
            status_code = 500
        response.version = settings.version
        response.start_timestamp = start_timestamp
        response.end_timestamp = int(time.time() * 1000)
        payload = response.model_dump(by_alias=True, exclude_none=True)
        try:
            ensure_json_size(
                payload,
                settings.max_solution_bytes,
                label="Solution payload",
            )
        except ResourceLimitError as exc:
            response = V1Response.error(
                f"Error: {exc}",
                version=settings.version,
                start_timestamp=start_timestamp,
                error_code=V1ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                retryable=False,
            )
            response.end_timestamp = int(time.time() * 1000)
            payload = response.model_dump(by_alias=True, exclude_none=True)
            status_code = 500
            result = "error"

        observe_payload_size("solution", json_size(payload))
        REQUEST_COUNTER.labels(command=command, result=result).inc()
        REQUEST_DURATION.labels(command=command).observe(time.monotonic() - started)
        serialized_error_code = (
            response.error_code.value if response.error_code is not None else None
        )
        if response.status == "error":
            record_v1_error(
                command,
                serialized_error_code or V1ErrorCode.INTERNAL_ERROR.value,
            )
        logger.info(
            "V1 request completed.",
            extra={
                "command": command,
                "result": response.status,
                "http_status": status_code,
                "error_code": serialized_error_code,
                "retryable": response.retryable,
                "request_outcome_unknown": response.request_outcome_unknown,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "target_host": target_host,
                "fallback_used": bool(response.fallback_used),
            },
        )
        return JSONResponse(payload, status_code=status_code)

    return app


def _safe_target_host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    normalized = host.lower().rstrip(".")[:253]
    if not normalized or any(
        character.isspace() or ord(character) < 0x20 for character in normalized
    ):
        return None
    return normalized


async def _await_with_hard_deadline(
    task: asyncio.Task[Any],
    *,
    timeout_seconds: float,
    cleanup: CleanupSupervisor,
    cleanup_kind: str,
) -> Any:
    """Wait for a task without making response latency depend on cancellation unwind."""

    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except BaseException:
        task.cancel()
        cleanup.track(task, kind=cleanup_kind)
        raise
    if task in done:
        return task.result()
    task.cancel()
    cleanup.track(task, kind=cleanup_kind)
    raise TimeoutError(f"Operation exceeded its {timeout_seconds:g}-second deadline.")


async def _readiness_probe(app: FastAPI) -> None:
    async with app.state.pool.lease_context(**context_options(None)) as lease:
        page = await lease.context.new_page()
        try:
            await page.evaluate("navigator.userAgent")
        finally:
            await close_page(
                page,
                cleanup_supervisor=app.state.cleanup,
                timeout_seconds=app.state.settings.cleanup_timeout_seconds,
            )


def _diagnostics_response(app: FastAPI, settings: Settings) -> DiagnosticsResponse:
    try:
        pool_snapshot = app.state.pool.snapshot()
    except Exception:
        logger.warning("Unable to read browser-pool diagnostics snapshot.", exc_info=True)
        pool_snapshot = {}
    try:
        session_snapshot = app.state.sessions.snapshot()
    except Exception:
        logger.warning("Unable to read session diagnostics snapshot.", exc_info=True)
        session_snapshot = {}
    try:
        cleanup_snapshot = app.state.cleanup.snapshot()
    except Exception:
        logger.warning("Unable to read cleanup diagnostics snapshot.", exc_info=True)
        cleanup_snapshot = {}

    max_browsers = _snapshot_positive_int(
        pool_snapshot,
        "max_browsers",
        settings.pool_max_browsers,
    )
    max_contexts_per_browser = _snapshot_positive_int(
        pool_snapshot,
        "max_contexts_per_browser",
        settings.pool_max_contexts_per_browser,
    )
    max_slots = _snapshot_positive_int(
        pool_snapshot,
        "max_slots",
        max_browsers * max_contexts_per_browser,
    )
    ready_browser_slots = _snapshot_int(
        pool_snapshot,
        "ready_browser_slots",
        _snapshot_int(pool_snapshot, "browser_slots", 0),
    )
    active_contexts = _snapshot_int(pool_snapshot, "active_contexts", 0)
    usable_context_slots = _snapshot_int(
        pool_snapshot,
        "usable_context_slots",
        max(0, ready_browser_slots * max_contexts_per_browser - active_contexts),
    )
    pool = DiagnosticsPoolStatus(
        ready_browser_slots=ready_browser_slots,
        retiring_browser_slots=_snapshot_int(pool_snapshot, "retiring_browser_slots", 0),
        creating_slots=_snapshot_int(pool_snapshot, "creating_slots", 0),
        closing_slots=_snapshot_int(pool_snapshot, "closing_slots", 0),
        active_contexts=active_contexts,
        transient_contexts=_snapshot_int(pool_snapshot, "transient_contexts", 0),
        persistent_contexts=_snapshot_int(pool_snapshot, "persistent_contexts", 0),
        waiting_requests=_snapshot_int(pool_snapshot, "waiting_requests", 0),
        usable_context_slots=usable_context_slots,
        idle_recyclable_slots=_snapshot_int(pool_snapshot, "idle_recyclable_slots", 0),
        max_browsers=max_browsers,
        max_contexts_per_browser=max_contexts_per_browser,
        max_slots=max_slots,
    )
    sessions = DiagnosticsSessionStatus(
        active=_snapshot_int(session_snapshot, "active", 0),
        in_use=_snapshot_int(session_snapshot, "in_use", 0),
        closing=_snapshot_int(session_snapshot, "closing", 0),
        max_sessions=_snapshot_positive_int(
            session_snapshot,
            "max_sessions",
            settings.max_sessions,
        ),
    )
    cleanup_by_kind = _snapshot_mapping(cleanup_snapshot, "by_kind")
    cleanup = DiagnosticsCleanupStatus(
        in_flight=_snapshot_int(cleanup_snapshot, "in_flight", 0),
        oldest_age_seconds=_snapshot_optional_non_negative_float(
            cleanup_snapshot,
            "oldest_age_seconds",
        ),
        by_kind={str(kind): _non_negative_int(count) for kind, count in cleanup_by_kind.items()},
    )
    runtime = DiagnosticsRuntimeStatus(
        playwright_version=_playwright_version(),
        playwright_cancel_patch=playwright_cancel_patch_status(),
    )
    return DiagnosticsResponse(
        capacity_state=_capacity_state(pool, cleanup),
        pool=pool,
        sessions=sessions,
        cleanup=cleanup,
        runtime=runtime,
    )


def _snapshot_int(snapshot: object, name: str, default: int) -> int:
    if isinstance(snapshot, Mapping):
        value = snapshot.get(name, default)
    else:
        value = getattr(snapshot, name, default)
    return _parsed_non_negative_int(value, default)


def _snapshot_positive_int(snapshot: object, name: str, default: int) -> int:
    if isinstance(snapshot, Mapping):
        value = snapshot.get(name, default)
    else:
        value = getattr(snapshot, name, default)
    parsed = _parsed_non_negative_int(value, default)
    return parsed if parsed > 0 else max(1, default)


def _snapshot_mapping(snapshot: object, name: str) -> Mapping[object, object]:
    value = snapshot.get(name, {}) if isinstance(snapshot, Mapping) else getattr(snapshot, name, {})
    return value if isinstance(value, Mapping) else {}


def _snapshot_optional_non_negative_float(snapshot: object, name: str) -> float | None:
    value = snapshot.get(name) if isinstance(snapshot, Mapping) else getattr(snapshot, name, None)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed) if math.isfinite(parsed) else None


def _non_negative_int(value: object) -> int:
    return _parsed_non_negative_int(value, 0)


def _parsed_non_negative_int(value: object, default: int) -> int:
    if not isinstance(value, str | bytes | bytearray | int | float):
        return max(0, default)
    try:
        return max(0, int(value))
    except (OverflowError, TypeError, ValueError):
        return max(0, default)


def _capacity_state(
    pool: DiagnosticsPoolStatus,
    cleanup: DiagnosticsCleanupStatus,
) -> Literal["available", "saturated", "recovering", "unavailable"]:
    if pool.usable_context_slots > 0:
        return "available"
    if (
        pool.retiring_browser_slots > 0
        or pool.creating_slots > 0
        or pool.closing_slots > 0
        or pool.idle_recyclable_slots > 0
        or cleanup.in_flight > 0
    ):
        return "recovering"
    if pool.active_contexts > 0 or pool.waiting_requests > 0:
        return "saturated"
    return "unavailable"


def _playwright_version() -> str:
    try:
        return version("playwright")
    except PackageNotFoundError:
        return "unknown"


async def _dispatch_v1(
    v1_request: V1Request,
    *,
    app: FastAPI,
    start_timestamp: int,
) -> V1Response:
    service = getattr(app.state, "command_service", None)
    if service is not None:
        return await service.dispatch(v1_request, start_timestamp=start_timestamp)
    return await dispatch_v1(
        v1_request,
        pool=app.state.pool,
        sessions=app.state.sessions,
        settings=app.state.settings,
        captcha_provider=app.state.captcha_provider,
        cleanup=getattr(app.state, "cleanup", None),
        start_timestamp=start_timestamp,
    )


async def _read_json_payload(request: Request, *, maximum_bytes: int) -> Any:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = -1
        if declared_length > maximum_bytes:
            raise ResourceLimitError(
                f"Request body exceeds the configured {maximum_bytes}-byte limit."
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum_bytes:
            raise ResourceLimitError(
                f"Request body exceeds the configured {maximum_bytes}-byte limit."
            )
        chunks.append(chunk)
    request.state.request_body_bytes = total
    return json.loads(b"".join(chunks))


def _validate_request_runtime_limits(request: V1Request, settings: Settings) -> None:
    if request.max_timeout > settings.max_timeout_ms:
        raise ResourceLimitError(
            "Request parameter 'maxTimeout' exceeds the configured "
            f"{settings.max_timeout_ms}-millisecond limit."
        )
    if (
        request.session_ttl_minutes is not None
        and request.session_ttl_minutes > settings.max_session_ttl_minutes
    ):
        raise ResourceLimitError(
            "Request parameter 'session_ttl_minutes' exceeds the configured "
            f"{settings.max_session_ttl_minutes}-minute limit."
        )


def _api_token_matches(request: Request, expected_token: str) -> bool:
    supplied_token = _api_token_from_request(request)
    return supplied_token is not None and secrets.compare_digest(supplied_token, expected_token)


def _api_token_from_request(request: Request) -> str | None:
    header_token = request.headers.get("x-api-token")
    if header_token:
        return header_token
    authorization = request.headers.get("authorization")
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _validation_error_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "payload"
        return f"Error: invalid request parameter '{location}': {first.get('msg', 'invalid')}."
    return "Error: invalid request payload."
