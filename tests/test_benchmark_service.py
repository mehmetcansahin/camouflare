from __future__ import annotations

import sys

import httpx
import pytest

from scripts.benchmark_service import (
    Sample,
    _aggregate,
    _build_acceptance,
    _counter_delta,
    _fetch_counters,
    _is_idle,
    _metrics_observed,
    _nearest_rank,
    _parse_counter_samples,
    _poll_idle,
    _sum_metric,
    _wait_idle,
    main,
)

REQUIRED_ARGUMENTS = [
    "benchmark_service.py",
    "--base-url",
    "http://svc",
    "--target-url",
    "http://target/",
    "--output",
    "/dev/null",
    "--environment-label",
    "unit test",
]

CLEAN_DELTA = {
    'camouflare_request_total{command="request.get",result="ok"}': 45.0,
    'camouflare_browser_recycle_total{reason="max_uses"}': 8.0,
}

IDLE_SNAPSHOT = {
    "pool": {
        "active_contexts": 0,
        "waiting_requests": 0,
        "creating_slots": 0,
        "closing_slots": 0,
    },
    "cleanup": {"in_flight": 0},
}


def test_nearest_rank_uses_successful_sample_order_statistics() -> None:
    values = [40.0, 10.0, 30.0, 20.0]

    assert _nearest_rank(values, 50) == 20.0
    assert _nearest_rank(values, 95) == 40.0
    assert _nearest_rank([], 50) is None


def test_counter_parser_and_delta_preserve_labelled_series() -> None:
    before = _parse_counter_samples(
        """
# HELP camouflare_request_total Total requests.
camouflare_request_total{command="request.get",result="ok"} 2
camouflare_in_flight_requests 0
"""
    )
    # Error series are created lazily on first use, so a failure that happens
    # during the run appears in the closing scrape only. The delta must report
    # it from the "after" side; iterating "before" would drop it and let
    # zero_v1_errors pass vacuously.
    after = _parse_counter_samples(
        """
camouflare_request_total{command="request.get",result="ok"} 5
camouflare_v1_error_total{command="request.get",error_code="INTERNAL_ERROR"} 0
camouflare_v1_error_total{command="request.get",error_code="REQUEST_TIMEOUT"} 2
"""
    )

    assert _counter_delta(before, after) == {
        'camouflare_request_total{command="request.get",result="ok"}': 3.0,
        'camouflare_v1_error_total{command="request.get",error_code="REQUEST_TIMEOUT"}': 2.0,
    }


def test_counter_delta_reports_a_reset_counter_as_a_negative_change() -> None:
    before = {"camouflare_request_total": 10.0, "camouflare_timeout_total": 1.0}
    after = {"camouflare_request_total": 4.0, "camouflare_timeout_total": 1.0}

    # A counter that went backwards means the service restarted between the
    # scrapes. The delta exposes that rather than hiding it behind the zero
    # filter, so the evidence file shows the run was not measured cleanly.
    assert _counter_delta(before, after) == {"camouflare_request_total": -6.0}


def test_aggregate_separates_timeouts_server_errors_and_client_errors() -> None:
    samples = [
        Sample(
            load="concurrency_4",
            round=1,
            sample=1,
            kind="success",
            client_ms=100.0,
            http_status=200,
            internal_ms=80,
            version="1.3.3",
        ),
        Sample(
            load="concurrency_4",
            round=1,
            sample=2,
            kind="client_timeout",
            client_ms=70_000.0,
        ),
        Sample(
            load="concurrency_4",
            round=1,
            sample=3,
            kind="http_500",
            client_ms=120.0,
            http_status=500,
            internal_ms=90,
            error="failed",
        ),
        Sample(
            load="concurrency_4",
            round=1,
            sample=4,
            kind="client_error",
            client_ms=10.0,
            error="disconnected",
        ),
    ]

    assert _aggregate("concurrency_4", 4, samples) == {
        "load": "4 concurrent",
        "concurrency": 4,
        "requests": 4,
        "successes": 1,
        "success_rate": 0.25,
        "timeouts": 1,
        "server_errors": 1,
        "client_errors": 1,
        "client_p50_ms": 100.0,
        "client_p95_ms": 100.0,
        "internal_p50_ms": 80.0,
        "internal_p95_ms": 80.0,
    }


def test_sum_metric_adds_labelled_series_without_matching_a_longer_name() -> None:
    delta = {
        'camouflare_browser_recycle_total{reason="max_uses"}': 8.0,
        'camouflare_browser_recycle_total{reason="error"}': 1.0,
        "camouflare_browser_recycle_total_extra": 99.0,
    }

    assert _sum_metric(delta, "camouflare_browser_recycle_total") == 9.0


def test_is_idle_requires_every_pool_and_cleanup_counter_to_be_zero() -> None:
    assert _is_idle(IDLE_SNAPSHOT) is True

    for section, key in (
        ("pool", "active_contexts"),
        ("pool", "waiting_requests"),
        ("pool", "creating_slots"),
        ("pool", "closing_slots"),
        ("cleanup", "in_flight"),
    ):
        busy = {name: dict(values) for name, values in IDLE_SNAPSHOT.items()}
        busy[section][key] = 1

        assert _is_idle(busy) is False


def test_is_idle_rejects_a_snapshot_missing_the_expected_sections() -> None:
    assert _is_idle({}) is False
    assert _is_idle({"pool": {}, "cleanup": {}}) is False


def test_metrics_are_observed_only_when_both_scrapes_carried_counters() -> None:
    counters = {"camouflare_request_total": 1.0}

    assert _metrics_observed(counters, counters) is True
    # The delta is built from the second scrape, so a missing scrape on either
    # side would zero every metric-derived check rather than measure it.
    assert _metrics_observed(counters, {}) is False
    assert _metrics_observed({}, counters) is False
    assert _metrics_observed({}, {}) is False


def test_acceptance_passes_when_every_measured_invariant_holds() -> None:
    acceptance = _build_acceptance(
        CLEAN_DELTA,
        failures=[],
        idle_after=True,
        metrics_observed=True,
        minimum_recycles=None,
    )

    assert acceptance == {
        "all_requests_succeeded": True,
        "zero_acquire_timeouts": True,
        "zero_asyncio_unhandled": True,
        "zero_browser_transport_errors": True,
        "zero_v1_errors": True,
        "idle_after": True,
    }


def test_acceptance_fails_metric_checks_when_no_counters_were_scraped() -> None:
    acceptance = _build_acceptance(
        {},
        failures=[],
        idle_after=True,
        metrics_observed=False,
        minimum_recycles=None,
    )

    assert acceptance["all_requests_succeeded"] is True
    assert acceptance["idle_after"] is True
    for unmeasured in (
        "zero_acquire_timeouts",
        "zero_asyncio_unhandled",
        "zero_browser_transport_errors",
        "zero_v1_errors",
    ):
        assert acceptance[unmeasured] is False


def test_acceptance_records_recycles_only_for_a_run_that_constrained_them() -> None:
    common = {"failures": [], "idle_after": True, "metrics_observed": True}
    unconstrained = _build_acceptance(CLEAN_DELTA, minimum_recycles=None, **common)
    met = _build_acceptance(CLEAN_DELTA, minimum_recycles=3, **common)
    unmet = _build_acceptance(CLEAN_DELTA, minimum_recycles=9, **common)

    assert "minimum_recycles_observed" not in unconstrained
    assert met["minimum_recycles_observed"] is True
    assert unmet["minimum_recycles_observed"] is False


def test_acceptance_reports_a_failed_request_and_a_counted_v1_error() -> None:
    acceptance = _build_acceptance(
        {'camouflare_v1_error_total{command="request.get",error_code="REQUEST_TIMEOUT"}': 2.0},
        failures=[{"kind": "http_500"}],
        idle_after=True,
        metrics_observed=True,
        minimum_recycles=None,
    )

    assert acceptance["all_requests_succeeded"] is False
    assert acceptance["zero_v1_errors"] is False
    assert acceptance["zero_acquire_timeouts"] is True


async def test_fetch_counters_rejects_a_metrics_body_without_camouflare_counters() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="not prometheus"))

    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
        with pytest.raises(RuntimeError, match="camouflare_"):
            await _fetch_counters(client, required=True)


async def test_fetch_counters_returns_no_counters_for_a_disabled_metrics_endpoint() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))

    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
        assert await _fetch_counters(client, required=False) == {}


async def test_fetch_counters_parses_a_served_metrics_body() -> None:
    body = 'camouflare_request_total{command="request.get",result="ok"} 45.0\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))

    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
        assert await _fetch_counters(client, required=True) == {
            'camouflare_request_total{command="request.get",result="ok"}': 45.0
        }


def _diagnostics_transport(snapshot: dict[str, object]) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json=snapshot))


async def test_poll_idle_returns_the_final_snapshot_and_whether_it_settled() -> None:
    busy = {name: dict(values) for name, values in IDLE_SNAPSHOT.items()}
    busy["pool"]["closing_slots"] = 1

    async with httpx.AsyncClient(
        transport=_diagnostics_transport(IDLE_SNAPSHOT), base_url="http://svc"
    ) as client:
        assert await _poll_idle(client, timeout_seconds=0) == (IDLE_SNAPSHOT, True)

    async with httpx.AsyncClient(
        transport=_diagnostics_transport(busy), base_url="http://svc"
    ) as client:
        assert await _poll_idle(client, timeout_seconds=0) == (busy, False)


async def test_wait_idle_raises_only_when_the_pool_never_settles() -> None:
    busy = {name: dict(values) for name, values in IDLE_SNAPSHOT.items()}
    busy["pool"]["active_contexts"] = 1

    async with httpx.AsyncClient(
        transport=_diagnostics_transport(IDLE_SNAPSHOT), base_url="http://svc"
    ) as client:
        assert await _wait_idle(client, timeout_seconds=0) == IDLE_SNAPSHOT

    async with httpx.AsyncClient(
        transport=_diagnostics_transport(busy), base_url="http://svc"
    ) as client:
        with pytest.raises(RuntimeError, match="did not become idle"):
            await _wait_idle(client, timeout_seconds=0)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--concurrency", "2", "2"], "concurrency values must be unique"),
        (["--concurrency", "0"], "concurrency values must be greater than zero"),
        (["--rounds", "0"], "request counts and rounds must be greater than zero"),
        (["--minimum-recycles", "0"], "--minimum-recycles must be at least one"),
    ],
)
def test_main_rejects_invalid_arguments_before_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    message: str,
) -> None:
    monkeypatch.setattr(sys, "argv", [*REQUIRED_ARGUMENTS, *arguments])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert message in capsys.readouterr().err
