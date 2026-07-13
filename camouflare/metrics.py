from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, cast

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
    {"request", "pool_acquire", "navigation", "challenge", "collection", "shutdown", "other"}
)
_CHALLENGE_OUTCOMES = frozenset(
    {"not_detected", "detected", "solved", "failed", "timeout", "skipped", "error", "other"}
)
_PAYLOAD_KINDS = frozenset({"request", "response", "screenshot", "solution", "other"})

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
) -> None:
    """Publish an atomic-looking view of the pool's bounded state dimensions."""

    BROWSER_STATE.labels(state="ready").set(max(0, browser_slots))
    BROWSER_STATE.labels(state="starting").set(max(0, creating_slots))
    BROWSER_STATE.labels(state="closing").set(max(0, closing_slots))
    CONTEXT_STATE.labels(kind="transient").set(max(0, transient_contexts))
    CONTEXT_STATE.labels(kind="persistent").set(max(0, persistent_contexts))
    POOL_WAITING_REQUESTS.set(max(0, waiting_requests))


def set_session_snapshot(*, active: int, in_use: int) -> None:
    SESSION_STATE.labels(state="active").set(max(0, active))
    SESSION_STATE.labels(state="in_use").set(max(0, in_use))


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
