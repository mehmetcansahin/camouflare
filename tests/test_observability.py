from __future__ import annotations

import asyncio
import importlib
import io
import json
import logging

import pytest

import camouflare.metrics as metrics
from camouflare.observability import (
    MAX_REQUEST_ID_LENGTH,
    REDACTED,
    JsonLogFormatter,
    TextLogFormatter,
    bind_request_id,
    configure_logging,
    get_request_id,
    is_valid_request_id,
    redact_mapping,
    redact_text,
    redact_url,
    request_id_context,
    reset_request_id,
    resolve_request_id,
)


def _sample_value(collector: object, sample_name: str, **labels: str) -> float:
    for family in collector.collect():  # type: ignore[attr-defined]
        for sample in family.samples:
            if sample.name == sample_name and sample.labels == labels:
                return float(sample.value)
    return 0.0


def test_request_id_validation_and_generation() -> None:
    supplied = "client-request-123"
    assert is_valid_request_id(supplied)
    assert is_valid_request_id("x" * MAX_REQUEST_ID_LENGTH)
    assert not is_valid_request_id("")
    assert not is_valid_request_id("x" * (MAX_REQUEST_ID_LENGTH + 1))
    assert not is_valid_request_id("contains\nnewline")
    assert not is_valid_request_id("non-ascii-ğ")
    assert resolve_request_id(supplied) == supplied
    assert is_valid_request_id(resolve_request_id("unsafe\nvalue"))


def test_request_id_context_is_nested_and_resets() -> None:
    assert get_request_id() is None

    token = bind_request_id("direct-binding")
    assert get_request_id() == "direct-binding"
    reset_request_id(token)
    assert get_request_id() is None
    with request_id_context("outer") as outer:
        assert outer == "outer"
        assert get_request_id() == "outer"
        with request_id_context("inner"):
            assert get_request_id() == "inner"
        assert get_request_id() == "outer"
    assert get_request_id() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://user:password@example.com/path?q=secret#fragment",
            "https://example.com/path",
        ),
        ("socks5://name:pass@[2001:db8::1]:1080", "socks5://[2001:db8::1]:1080"),
        ("https://example.com/plain", "https://example.com/plain"),
    ],
)
def test_redact_url_removes_credentials_and_query(raw: str, expected: str) -> None:
    assert redact_url(raw) == expected


def test_redaction_helpers_cover_nested_secrets_and_rendered_messages() -> None:
    redacted = redact_mapping(
        {
            "url": "https://user:pass@example.com/path?token=secret",
            "headers": {"Authorization": "Bearer abc", "X-Trace": "safe"},
            "proxy": {
                "server": "http://proxy-user:proxy-pass@proxy.test:8080?x=y",
                "username": "proxy-user",
                "password": "proxy-pass",
            },
            "postData": '{"secret": "value"}',
        }
    )
    assert redacted == {
        "url": "https://example.com/path",
        "headers": {"Authorization": REDACTED, "X-Trace": "safe"},
        "proxy": {
            "server": "http://proxy.test:8080",
            "username": REDACTED,
            "password": REDACTED,
        },
        "postData": REDACTED,
    }

    message = redact_text(
        "fetch https://u:p@example.com/a?q=secret, token=abc, authorization=Bearer xyz"
    )
    assert "secret" not in message
    assert "abc" not in message
    assert "Bearer xyz" not in message
    assert "https://example.com/a" in message

    access_message = redact_text(
        '127.0.0.1:1234 - "GET /v1?token=top-secret&cookie=hidden HTTP/1.1" 200'
    )
    assert access_message == '127.0.0.1:1234 - "GET /v1 HTTP/1.1" 200'


def test_text_and_json_formatters_include_context_and_redact() -> None:
    record = logging.LogRecord(
        name="camouflare.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="opening %s token=top-secret",
        args=("https://user:pass@example.com/a?key=value",),
        exc_info=None,
    )
    record.url = "https://user:pass@example.com/a?key=value"
    record.password = "top-secret"

    with request_id_context("request-42"):
        text_output = TextLogFormatter().format(record)
        json_output = json.loads(JsonLogFormatter().format(record))

    assert "request-42" in text_output
    assert "top-secret" not in text_output
    assert "user:pass" not in text_output
    assert json_output["request_id"] == "request-42"
    assert json_output["fields"] == {
        "url": "https://example.com/a",
        "password": REDACTED,
    }
    assert "top-secret" not in json_output["message"]


def test_configure_logging_supports_json_and_rejects_unknown_format() -> None:
    stream = io.StringIO()
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    access_logger = logging.getLogger("uvicorn.access")
    original_access_handlers = access_logger.handlers[:]
    original_access_propagate = access_logger.propagate
    try:
        access_logger.addHandler(logging.StreamHandler(io.StringIO()))
        access_logger.propagate = False
        configure_logging(level=logging.INFO, log_format="json", stream=stream)
        with request_id_context("configured-request"):
            logging.getLogger("camouflare.configured").info("hello")
        payload = json.loads(stream.getvalue())
        assert payload["request_id"] == "configured-request"
        assert payload["message"] == "hello"
        assert access_logger.handlers == []
        assert access_logger.propagate is True
    finally:
        root.handlers.clear()
        root.handlers.extend(original_handlers)
        root.setLevel(original_level)
        access_logger.handlers.clear()
        access_logger.handlers.extend(original_access_handlers)
        access_logger.propagate = original_access_propagate

    with pytest.raises(ValueError, match="text or json"):
        configure_logging(log_format="xml")


def test_metrics_hooks_bound_labels_and_publish_snapshots() -> None:
    metrics.set_pool_snapshot(
        browser_slots=2,
        creating_slots=1,
        closing_slots=1,
        transient_contexts=3,
        persistent_contexts=4,
        waiting_requests=5,
        retiring_browser_slots=1,
        usable_context_slots=7,
        idle_recyclable_slots=2,
    )
    metrics.set_session_snapshot(active=6, in_use=2, closing=1)

    assert metrics.metric_value(metrics.BROWSER_STATE, state="ready") == 2
    assert metrics.metric_value(metrics.BROWSER_STATE, state="starting") == 1
    assert metrics.metric_value(metrics.BROWSER_STATE, state="retiring") == 1
    assert metrics.metric_value(metrics.CONTEXT_STATE, kind="transient") == 3
    assert metrics.metric_value(metrics.SESSION_STATE, state="active") == 6
    assert metrics.metric_value(metrics.SESSION_STATE, state="closing") == 1
    assert metrics.metric_value(metrics.POOL_WAITING_REQUESTS) == 5
    assert metrics.metric_value(metrics.POOL_USABLE_CONTEXT_SLOTS) == 7
    assert metrics.metric_value(metrics.POOL_IDLE_RECYCLABLE_SLOTS) == 2

    before = _sample_value(
        metrics.POOL_ACQUIRE_COUNTER,
        "camouflare_pool_acquire_total",
        kind="other",
        result="other",
    )
    metrics.observe_pool_acquire(
        kind="attacker-controlled-kind",
        result="attacker-controlled-result",
        duration_seconds=-1,
    )
    after = _sample_value(
        metrics.POOL_ACQUIRE_COUNTER,
        "camouflare_pool_acquire_total",
        kind="other",
        result="other",
    )
    assert after == before + 1

    label_values = {
        tuple(sample.labels.values())
        for family in metrics.POOL_ACQUIRE_COUNTER.collect()
        for sample in family.samples
    }
    assert ("attacker-controlled-kind", "attacker-controlled-result") not in label_values


def test_metric_lifecycle_hooks_are_balanced_and_bounded() -> None:
    in_flight_before = metrics.metric_value(metrics.IN_FLIGHT_REQUESTS)
    with (
        pytest.raises(RuntimeError, match="boom"),
        metrics.track_in_flight_request(),
    ):
        assert metrics.metric_value(metrics.IN_FLIGHT_REQUESTS) == in_flight_before + 1
        raise RuntimeError("boom")
    assert metrics.metric_value(metrics.IN_FLIGHT_REQUESTS) == in_flight_before

    cases = (
        (
            metrics.BROWSER_EVENT_COUNTER,
            "camouflare_browser_event_total",
            {"event": "other"},
            lambda: metrics.record_browser_event("dynamic-browser-event"),
        ),
        (
            metrics.BROWSER_RECYCLE_COUNTER,
            "camouflare_browser_recycle_total",
            {"reason": "other"},
            lambda: metrics.record_browser_recycle("dynamic-recycle-reason"),
        ),
        (
            metrics.SESSION_EVENT_COUNTER,
            "camouflare_session_event_total",
            {"event": "other"},
            lambda: metrics.record_session_event("dynamic-session-event"),
        ),
        (
            metrics.TIMEOUT_COUNTER,
            "camouflare_timeout_total",
            {"phase": "other"},
            lambda: metrics.record_timeout("dynamic-timeout-phase"),
        ),
        (
            metrics.CHALLENGE_COUNTER,
            "camouflare_challenge_total",
            {"outcome": "other"},
            lambda: metrics.record_challenge("dynamic-challenge-outcome"),
        ),
    )
    for collector, sample_name, labels, record in cases:
        before = _sample_value(collector, sample_name, **labels)
        record()
        assert _sample_value(collector, sample_name, **labels) == before + 1

    payload_before = _sample_value(
        metrics.PAYLOAD_SIZE,
        "camouflare_payload_bytes_count",
        kind="other",
    )
    metrics.observe_payload_size("dynamic-payload-kind", -1)
    assert (
        _sample_value(metrics.PAYLOAD_SIZE, "camouflare_payload_bytes_count", kind="other")
        == payload_before + 1
    )

    body = bytes(metrics.metrics_response().body)
    assert b"camouflare_in_flight_requests" in body


def test_resilience_metrics_are_bounded_and_exported() -> None:
    metrics.set_cleanup_snapshot(by_kind={"page": 2, "dynamic-kind": 3})
    assert metrics.metric_value(metrics.CLEANUP_IN_FLIGHT, kind="page") == 2
    assert metrics.metric_value(metrics.CLEANUP_IN_FLIGHT, kind="other") == 3

    cleanup_before = _sample_value(
        metrics.CLEANUP_COUNTER,
        "camouflare_cleanup_total",
        kind="other",
        result="other",
    )
    metrics.record_cleanup(
        kind="dynamic-kind",
        result="dynamic-result",
        duration_seconds=-1,
    )
    assert (
        _sample_value(
            metrics.CLEANUP_COUNTER,
            "camouflare_cleanup_total",
            kind="other",
            result="other",
        )
        == cleanup_before + 1
    )

    readiness_before = _sample_value(
        metrics.READINESS_COUNTER,
        "camouflare_readiness_total",
        result="other",
    )
    metrics.observe_readiness(result="dynamic-result", duration_seconds=-1)
    assert (
        _sample_value(
            metrics.READINESS_COUNTER,
            "camouflare_readiness_total",
            result="other",
        )
        == readiness_before + 1
    )

    acquire_before = _sample_value(
        metrics.POOL_ACQUIRE_TIMEOUT_COUNTER,
        "camouflare_pool_acquire_timeout_total",
        reason="other",
    )
    metrics.record_pool_acquire_timeout("dynamic-reason")
    assert (
        _sample_value(
            metrics.POOL_ACQUIRE_TIMEOUT_COUNTER,
            "camouflare_pool_acquire_timeout_total",
            reason="other",
        )
        == acquire_before + 1
    )

    asyncio_before = _sample_value(
        metrics.ASYNCIO_UNHANDLED_COUNTER,
        "camouflare_asyncio_unhandled_total",
        kind="other",
    )
    metrics.record_asyncio_unhandled("dynamic-kind")
    assert (
        _sample_value(
            metrics.ASYNCIO_UNHANDLED_COUNTER,
            "camouflare_asyncio_unhandled_total",
            kind="other",
        )
        == asyncio_before + 1
    )

    body = bytes(metrics.metrics_response().body)
    assert b"camouflare_pool_usable_context_slots" in body
    assert b"camouflare_pool_idle_recyclable_slots" in body
    assert b"camouflare_cleanup_total" in body
    assert b"camouflare_readiness_total" in body
    assert b"camouflare_pool_acquire_timeout_total" in body
    assert b"camouflare_asyncio_unhandled_total" in body


@pytest.mark.anyio
async def test_asyncio_exception_handler_counts_unhandled_future_without_dynamic_labels() -> None:
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    chained_contexts: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: chained_contexts.append(context))
    before = _sample_value(
        metrics.ASYNCIO_UNHANDLED_COUNTER,
        "camouflare_asyncio_unhandled_total",
        kind="future",
    )
    future = loop.create_future()

    try:
        restore = metrics.install_asyncio_exception_metrics()
        handler = loop.get_exception_handler()
        assert handler is not None
        context = {"message": "Future exception was never retrieved", "future": future}
        handler(loop, context)
    finally:
        if "restore" in locals():
            restore()
        future.cancel()
        loop.set_exception_handler(original_handler)

    assert (
        _sample_value(
            metrics.ASYNCIO_UNHANDLED_COUNTER,
            "camouflare_asyncio_unhandled_total",
            kind="future",
        )
        == before + 1
    )
    assert chained_contexts == [context]


@pytest.mark.anyio
async def test_asyncio_exception_metrics_reinstalls_after_handler_replacement() -> None:
    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    first_restore = metrics.install_asyncio_exception_metrics()
    replacement_contexts: list[dict[str, object]] = []

    def replacement(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        replacement_contexts.append(context)

    loop.set_exception_handler(replacement)

    try:
        second_restore = metrics.install_asyncio_exception_metrics()
        installed = loop.get_exception_handler()
        assert installed is not None and installed is not replacement
        context = {"message": "replacement-chain"}
        installed(loop, context)
        second_restore()
        assert loop.get_exception_handler() is replacement
    finally:
        first_restore()
        loop.set_exception_handler(original)

    assert replacement_contexts == [context]


def test_asyncio_exception_kind_recognizes_standard_async_generator_context() -> None:
    assert metrics._asyncio_unhandled_kind({"asyncgen": object()}) == "async_generator"
    assert (
        metrics._asyncio_unhandled_kind({"message": "error closing asynchronous generator"})
        == "async_generator"
    )


def test_metric_module_reload_reuses_registered_collectors() -> None:
    request_counter = metrics.REQUEST_COUNTER
    reloaded = importlib.reload(metrics)
    assert reloaded.REQUEST_COUNTER is request_counter
