from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal, cast

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

# Metric labels must never contain request-derived values.  These allowlists keep
# cardinality bounded even when a caller accidentally passes an exception name or
# another dynamic string to one of the integration hooks below.
_CONTEXT_KINDS = frozenset({"transient", "persistent", "other"})
_ACQUIRE_RESULTS = frozenset({"success", "timeout", "error", "cancelled", "rejected", "other"})
_BROWSER_EVENTS = frozenset({"created", "recycled", "disconnected", "error", "other"})
_RECYCLE_REASONS = frozenset({"max_uses", "max_age", "disconnected", "shutdown", "error", "other"})
_SESSION_EVENTS = frozenset(
    {"created", "destroyed", "expired", "rotated", "rejected", "error", "other"}
)
_TIMEOUT_PHASES = frozenset(
    {
        "request",
        "pool_acquire",
        "navigation",
        "challenge",
        "collection",
        "readiness",
        "cleanup",
        "shutdown",
        "other",
    }
)
_CHALLENGE_OUTCOMES = frozenset(
    {"not_detected", "detected", "solved", "failed", "timeout", "skipped", "error", "other"}
)
_PAYLOAD_KINDS = frozenset({"request", "response", "screenshot", "solution", "other"})
_CLEANUP_KINDS = frozenset(
    {"request", "readiness", "page", "context", "browser", "proxy", "captcha", "session", "other"}
)
_CLEANUP_RESULTS = frozenset({"success", "cancelled", "timeout", "error", "other"})
_READINESS_RESULTS = frozenset({"success", "timeout", "unavailable", "error", "other"})
_ACQUIRE_TIMEOUT_REASONS = frozenset(
    {"capacity", "deadline", "browser_launch", "shutdown", "other"}
)
_ASYNCIO_UNHANDLED_KINDS = frozenset({"future", "task", "async_generator", "other"})
_V1_COMMANDS = frozenset(
    {
        "sessions.create",
        "sessions.list",
        "sessions.destroy",
        "request.get",
        "request.post",
        "invalid",
        "unknown",
    }
)
_V1_ERROR_CODES = frozenset(
    {
        "INVALID_REQUEST",
        "SESSION_NOT_FOUND",
        "RESOURCE_LIMIT_EXCEEDED",
        "POOL_UNAVAILABLE",
        "REQUEST_TIMEOUT",
        "NAVIGATION_TIMEOUT",
        "BROWSER_TRANSPORT_CLOSED",
        "CHALLENGE_FAILED",
        "INTERNAL_ERROR",
    }
)
_BROWSER_TRANSPORT_PHASES = frozenset(
    {"acquire", "browser_launch", "context_create", "navigation", "collection", "cleanup", "other"}
)

ContextKind = Literal["transient", "persistent"]


def _registered_collector(name: str) -> Counter | Gauge | Histogram | None:
    """Return an existing collector when this module is reloaded.

    prometheus_client's default registry is process-global.  Looking up an
    already-registered collector keeps test reloads and alternate import paths
    from raising a duplicate-timeseries ValueError.
    """

    collectors = getattr(REGISTRY, "_names_to_collectors", {})
    collector = collectors.get(name)
    if isinstance(collector, (Counter, Gauge, Histogram)):
        return collector
    return None


def _counter(name: str, documentation: str, labels: tuple[str, ...] = ()) -> Counter:
    existing = _registered_collector(name)
    if existing is not None:
        if not isinstance(existing, Counter):
            raise RuntimeError(f"Prometheus collector {name!r} has an incompatible type.")
        return existing
    return Counter(name, documentation, labels)


def _gauge(name: str, documentation: str, labels: tuple[str, ...] = ()) -> Gauge:
    existing = _registered_collector(name)
    if existing is not None:
        if not isinstance(existing, Gauge):
            raise RuntimeError(f"Prometheus collector {name!r} has an incompatible type.")
        return existing
    return Gauge(name, documentation, labels)


def _histogram(
    name: str,
    documentation: str,
    labels: tuple[str, ...] = (),
    *,
    buckets: tuple[float, ...] | None = None,
) -> Histogram:
    existing = _registered_collector(name)
    if existing is not None:
        if not isinstance(existing, Histogram):
            raise RuntimeError(f"Prometheus collector {name!r} has an incompatible type.")
        return existing
    if buckets is None:
        return Histogram(name, documentation, labels)
    return Histogram(name, documentation, labels, buckets=buckets)


REQUEST_COUNTER = _counter(
    "camouflare_request_total",
    "Total /v1 requests by command and result.",
    ("command", "result"),
)
REQUEST_DURATION = _histogram(
    "camouflare_request_duration_seconds",
    "Duration of /v1 requests.",
    ("command",),
)
IN_FLIGHT_REQUESTS = _gauge(
    "camouflare_in_flight_requests",
    "Number of HTTP requests currently being processed.",
)
POOL_WAITING_REQUESTS = _gauge(
    "camouflare_pool_waiting_requests",
    "Number of requests waiting for browser-context capacity.",
)
POOL_USABLE_CONTEXT_SLOTS = _gauge(
    "camouflare_pool_usable_context_slots",
    "Number of context slots that can currently accept work.",
)
POOL_IDLE_RECYCLABLE_SLOTS = _gauge(
    "camouflare_pool_idle_recyclable_slots",
    "Number of idle browser slots awaiting lifecycle recycling.",
)
BROWSER_STATE = _gauge(
    "camouflare_browsers",
    "Number of browser processes by lifecycle state.",
    ("state",),
)
CONTEXT_STATE = _gauge(
    "camouflare_contexts",
    "Number of browser contexts by ownership kind.",
    ("kind",),
)
POOL_ACQUIRE_COUNTER = _counter(
    "camouflare_pool_acquire_total",
    "Browser-context acquisition attempts by kind and result.",
    ("kind", "result"),
)
POOL_ACQUIRE_DURATION = _histogram(
    "camouflare_pool_acquire_duration_seconds",
    "Browser-context acquisition duration by kind and result.",
    ("kind", "result"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
BROWSER_EVENT_COUNTER = _counter(
    "camouflare_browser_event_total",
    "Browser lifecycle events.",
    ("event",),
)
BROWSER_RECYCLE_COUNTER = _counter(
    "camouflare_browser_recycle_total",
    "Browser recycle operations by reason.",
    ("reason",),
)
SESSION_STATE = _gauge(
    "camouflare_sessions",
    "Session counts by state.",
    ("state",),
)
SESSION_EVENT_COUNTER = _counter(
    "camouflare_session_event_total",
    "Session lifecycle events.",
    ("event",),
)
TIMEOUT_COUNTER = _counter(
    "camouflare_timeout_total",
    "Timeouts by bounded execution phase.",
    ("phase",),
)
CHALLENGE_COUNTER = _counter(
    "camouflare_challenge_total",
    "Challenge outcomes.",
    ("outcome",),
)
PAYLOAD_SIZE = _histogram(
    "camouflare_payload_bytes",
    "Payload size before response serialization by kind.",
    ("kind",),
    buckets=(
        1_024,
        4_096,
        16_384,
        65_536,
        262_144,
        1_048_576,
        4_194_304,
        16_777_216,
        33_554_432,
        67_108_864,
    ),
)
CLEANUP_IN_FLIGHT = _gauge(
    "camouflare_cleanup_in_flight",
    "Number of detached cleanup operations by bounded kind.",
    ("kind",),
)
CLEANUP_COUNTER = _counter(
    "camouflare_cleanup_total",
    "Detached cleanup operations by bounded kind and result.",
    ("kind", "result"),
)
CLEANUP_DURATION = _histogram(
    "camouflare_cleanup_duration_seconds",
    "Duration of detached cleanup operations by bounded kind and result.",
    ("kind", "result"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
READINESS_COUNTER = _counter(
    "camouflare_readiness_total",
    "Browser readiness probes by result.",
    ("result",),
)
READINESS_DURATION = _histogram(
    "camouflare_readiness_duration_seconds",
    "Duration of browser readiness probes by result.",
    ("result",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30),
)
POOL_ACQUIRE_TIMEOUT_COUNTER = _counter(
    "camouflare_pool_acquire_timeout_total",
    "Browser-context acquisition timeouts by bounded reason.",
    ("reason",),
)
ASYNCIO_UNHANDLED_COUNTER = _counter(
    "camouflare_asyncio_unhandled_total",
    "Unhandled asyncio exception-handler events by bounded kind.",
    ("kind",),
)
V1_ERROR_COUNTER = _counter(
    "camouflare_v1_error_total",
    "Total /v1 errors by bounded command and error code.",
    ("command", "error_code"),
)
BROWSER_TRANSPORT_ERROR_COUNTER = _counter(
    "camouflare_browser_transport_error_total",
    "Browser transport errors by bounded phase.",
    ("phase",),
)


def _bounded(value: str, allowed: frozenset[str]) -> str:
    normalized = value.strip().lower()
    return normalized if normalized in allowed else "other"


def request_started() -> None:
    IN_FLIGHT_REQUESTS.inc()


def request_finished() -> None:
    IN_FLIGHT_REQUESTS.dec()


@contextmanager
def track_in_flight_request() -> Iterator[None]:
    request_started()
    try:
        yield
    finally:
        request_finished()


def set_pool_snapshot(
    *,
    browser_slots: int,
    creating_slots: int,
    closing_slots: int,
    transient_contexts: int,
    persistent_contexts: int,
    waiting_requests: int,
    ready_browser_slots: int | None = None,
    retiring_browser_slots: int = 0,
    usable_context_slots: int | None = None,
    idle_recyclable_slots: int = 0,
) -> None:
    """Publish an atomic-looking view of the pool's bounded state dimensions."""

    ready_slots = browser_slots if ready_browser_slots is None else ready_browser_slots
    BROWSER_STATE.labels(state="ready").set(max(0, ready_slots))
    BROWSER_STATE.labels(state="retiring").set(max(0, retiring_browser_slots))
    BROWSER_STATE.labels(state="starting").set(max(0, creating_slots))
    BROWSER_STATE.labels(state="closing").set(max(0, closing_slots))
    CONTEXT_STATE.labels(kind="transient").set(max(0, transient_contexts))
    CONTEXT_STATE.labels(kind="persistent").set(max(0, persistent_contexts))
    POOL_WAITING_REQUESTS.set(max(0, waiting_requests))
    if usable_context_slots is not None:
        POOL_USABLE_CONTEXT_SLOTS.set(max(0, usable_context_slots))
    POOL_IDLE_RECYCLABLE_SLOTS.set(max(0, idle_recyclable_slots))


def set_session_snapshot(*, active: int, in_use: int, closing: int = 0) -> None:
    SESSION_STATE.labels(state="active").set(max(0, active))
    SESSION_STATE.labels(state="in_use").set(max(0, in_use))
    SESSION_STATE.labels(state="closing").set(max(0, closing))


def observe_pool_acquire(*, kind: str, result: str, duration_seconds: float) -> None:
    bounded_kind = _bounded(kind, _CONTEXT_KINDS)
    bounded_result = _bounded(result, _ACQUIRE_RESULTS)
    labels = {"kind": bounded_kind, "result": bounded_result}
    POOL_ACQUIRE_COUNTER.labels(**labels).inc()
    POOL_ACQUIRE_DURATION.labels(**labels).observe(max(0.0, duration_seconds))


def record_browser_event(event: str) -> None:
    BROWSER_EVENT_COUNTER.labels(event=_bounded(event, _BROWSER_EVENTS)).inc()


def record_browser_recycle(reason: str) -> None:
    BROWSER_RECYCLE_COUNTER.labels(reason=_bounded(reason, _RECYCLE_REASONS)).inc()
    record_browser_event("recycled")


def record_session_event(event: str) -> None:
    SESSION_EVENT_COUNTER.labels(event=_bounded(event, _SESSION_EVENTS)).inc()


def record_timeout(phase: str) -> None:
    TIMEOUT_COUNTER.labels(phase=_bounded(phase, _TIMEOUT_PHASES)).inc()


def set_cleanup_snapshot(*, by_kind: dict[str, int]) -> None:
    bounded_counts = {kind: 0 for kind in _CLEANUP_KINDS}
    for kind, count in by_kind.items():
        bounded = _bounded(kind, _CLEANUP_KINDS)
        bounded_counts[bounded] += max(0, count)
    for kind, count in bounded_counts.items():
        CLEANUP_IN_FLIGHT.labels(kind=kind).set(count)


def record_cleanup(*, kind: str, result: str, duration_seconds: float) -> None:
    bounded_kind = _bounded(kind, _CLEANUP_KINDS)
    bounded_result = _bounded(result, _CLEANUP_RESULTS)
    labels = {"kind": bounded_kind, "result": bounded_result}
    CLEANUP_COUNTER.labels(**labels).inc()
    CLEANUP_DURATION.labels(**labels).observe(max(0.0, duration_seconds))


def observe_readiness(*, result: str, duration_seconds: float) -> None:
    bounded_result = _bounded(result, _READINESS_RESULTS)
    READINESS_COUNTER.labels(result=bounded_result).inc()
    READINESS_DURATION.labels(result=bounded_result).observe(max(0.0, duration_seconds))


def record_pool_acquire_timeout(reason: str) -> None:
    POOL_ACQUIRE_TIMEOUT_COUNTER.labels(reason=_bounded(reason, _ACQUIRE_TIMEOUT_REASONS)).inc()


def record_asyncio_unhandled(kind: str) -> None:
    ASYNCIO_UNHANDLED_COUNTER.labels(kind=_bounded(kind, _ASYNCIO_UNHANDLED_KINDS)).inc()


def record_v1_error(command: str, error_code: str) -> None:
    bounded_command = command if command in _V1_COMMANDS else "unknown"
    normalized_code = str(getattr(error_code, "value", error_code)).strip().upper()
    bounded_code = normalized_code if normalized_code in _V1_ERROR_CODES else "INTERNAL_ERROR"
    V1_ERROR_COUNTER.labels(command=bounded_command, error_code=bounded_code).inc()


def record_browser_transport_error(phase: str) -> None:
    BROWSER_TRANSPORT_ERROR_COUNTER.labels(phase=_bounded(phase, _BROWSER_TRANSPORT_PHASES)).inc()


def install_asyncio_exception_metrics() -> Callable[[], None]:
    """Install one low-cardinality exception counter per running event loop."""

    loop = asyncio.get_running_loop()
    current = loop.get_exception_handler()
    if getattr(current, "__camouflare_asyncio_metrics__", False):
        return lambda: None
    previous = current

    def handler(current_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        record_asyncio_unhandled(_asyncio_unhandled_kind(context))
        if previous is not None:
            previous(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    handler.__dict__["__camouflare_asyncio_metrics__"] = True
    loop.set_exception_handler(handler)

    def restore() -> None:
        if loop.get_exception_handler() is handler:
            loop.set_exception_handler(previous)

    return restore


def _asyncio_unhandled_kind(context: Mapping[str, Any]) -> str:
    if context.get("asyncgen") is not None:
        return "async_generator"
    if context.get("task") is not None:
        return "task"
    future = context.get("future")
    if isinstance(future, asyncio.Task):
        return "task"
    if future is not None:
        return "future"
    message = str(context.get("message", "")).lower()
    if (
        "async generator" in message
        or "async_generator" in message
        or "asynchronous generator" in message
    ):
        return "async_generator"
    return "other"


def record_challenge(outcome: str) -> None:
    CHALLENGE_COUNTER.labels(outcome=_bounded(outcome, _CHALLENGE_OUTCOMES)).inc()


def observe_payload_size(kind: str, size_bytes: int) -> None:
    PAYLOAD_SIZE.labels(kind=_bounded(kind, _PAYLOAD_KINDS)).observe(max(0, size_bytes))


def metric_value(metric: Gauge, **labels: str) -> float:
    """Read a gauge value for snapshots and focused tests without exporting internals."""

    child = metric.labels(**labels) if labels else metric
    return float(cast(Gauge, child)._value.get())


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
