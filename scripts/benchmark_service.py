#!/usr/bin/env python3
"""Benchmark a running Camouflare service and write reproducible JSON evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class Sample:
    load: str
    round: int
    sample: int
    kind: str
    client_ms: float
    http_status: int | None = None
    internal_ms: int | None = None
    error: str | None = None
    version: str | None = None


def _nearest_rank(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def _parse_counter_samples(text: str) -> dict[str, float]:
    samples: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            descriptor, raw_value = line.rsplit(None, 1)
            name = descriptor.split("{", 1)[0]
            if not name.startswith("camouflare_") or not name.endswith("_total"):
                continue
            samples[descriptor] = float(raw_value)
        except ValueError:
            continue
    return samples


def _counter_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {
        descriptor: after_value - before.get(descriptor, 0.0)
        for descriptor, after_value in sorted(after.items())
        if after_value - before.get(descriptor, 0.0) != 0
    }


def _internal_duration_ms(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    start = body.get("startTimestamp")
    end = body.get("endTimestamp")
    if isinstance(start, int) and isinstance(end, int) and end >= start:
        return end - start
    return None


def _error_message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return fallback[:500]


async def _request(
    client: httpx.AsyncClient,
    *,
    load: str,
    round_number: int,
    sample_number: int,
    target_url: str,
    api_timeout_ms: int,
) -> Sample:
    started = time.perf_counter()
    try:
        response = await client.post(
            "/v1",
            json={
                "cmd": "request.get",
                "url": target_url,
                "maxTimeout": api_timeout_ms,
            },
        )
    except httpx.TimeoutException:
        return Sample(
            load=load,
            round=round_number,
            sample=sample_number,
            kind="client_timeout",
            client_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    except httpx.HTTPError as exc:
        return Sample(
            load=load,
            round=round_number,
            sample=sample_number,
            kind="client_error",
            client_ms=round((time.perf_counter() - started) * 1000, 3),
            error=f"{type(exc).__name__}: {exc}",
        )

    client_ms = round((time.perf_counter() - started) * 1000, 3)
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    internal_ms = _internal_duration_ms(body)
    version = body.get("version") if isinstance(body, dict) else None
    if response.status_code == 200 and isinstance(body, dict) and body.get("status") == "ok":
        return Sample(
            load=load,
            round=round_number,
            sample=sample_number,
            kind="success",
            http_status=response.status_code,
            client_ms=client_ms,
            internal_ms=internal_ms,
            version=version if isinstance(version, str) else None,
        )
    return Sample(
        load=load,
        round=round_number,
        sample=sample_number,
        kind=f"http_{response.status_code}",
        http_status=response.status_code,
        client_ms=client_ms,
        internal_ms=internal_ms,
        error=_error_message(body, response.text),
        version=version if isinstance(version, str) else None,
    )


async def _fetch_diagnostics(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/diagnostics")
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("/diagnostics did not return a JSON object")
    return body


def _is_idle(diagnostics: dict[str, Any]) -> bool:
    pool = diagnostics.get("pool")
    cleanup = diagnostics.get("cleanup")
    if not isinstance(pool, dict) or not isinstance(cleanup, dict):
        return False
    return (
        pool.get("active_contexts") == 0
        and pool.get("waiting_requests") == 0
        and pool.get("creating_slots") == 0
        and pool.get("closing_slots") == 0
        and cleanup.get("in_flight") == 0
    )


async def _poll_idle(
    client: httpx.AsyncClient,
    *,
    timeout_seconds: float = 60,
) -> tuple[dict[str, Any], bool]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        latest = await _fetch_diagnostics(client)
        if _is_idle(latest):
            return latest, True
        if time.monotonic() >= deadline:
            return latest, False
        await asyncio.sleep(0.25)


async def _wait_idle(
    client: httpx.AsyncClient,
    *,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    latest, idle = await _poll_idle(client, timeout_seconds=timeout_seconds)
    if not idle:
        raise RuntimeError(f"Camouflare did not become idle within {timeout_seconds}s: {latest}")
    return latest


async def _fetch_counters(client: httpx.AsyncClient, *, required: bool) -> dict[str, float]:
    response = await client.get("/metrics")
    if response.status_code == 404 and not required:
        return {}
    response.raise_for_status()
    samples = _parse_counter_samples(response.text)
    if required and not samples:
        raise RuntimeError(
            "/metrics returned no camouflare_*_total counters, so the metric-derived "
            "acceptance checks cannot be verified"
        )
    return samples


async def _run_sequential(
    client: httpx.AsyncClient,
    *,
    requests: int,
    target_url: str,
    api_timeout_ms: int,
) -> list[Sample]:
    results: list[Sample] = []
    for request_number in range(1, requests + 1):
        await _wait_idle(client)
        results.append(
            await _request(
                client,
                load="sequential",
                round_number=request_number,
                sample_number=1,
                target_url=target_url,
                api_timeout_ms=api_timeout_ms,
            )
        )
        await _wait_idle(client)
    return results


async def _run_concurrent(
    client: httpx.AsyncClient,
    *,
    concurrency: int,
    rounds: int,
    target_url: str,
    api_timeout_ms: int,
) -> list[Sample]:
    load = f"concurrency_{concurrency}"
    results: list[Sample] = []
    for round_number in range(1, rounds + 1):
        await _wait_idle(client)
        round_results = await asyncio.gather(
            *(
                _request(
                    client,
                    load=load,
                    round_number=round_number,
                    sample_number=sample_number,
                    target_url=target_url,
                    api_timeout_ms=api_timeout_ms,
                )
                for sample_number in range(1, concurrency + 1)
            )
        )
        results.extend(round_results)
        await _wait_idle(client)
    return results


def _aggregate(load: str, concurrency: int, samples: list[Sample]) -> dict[str, Any]:
    successful = [sample for sample in samples if sample.kind == "success"]
    client_latencies = [sample.client_ms for sample in successful]
    internal_latencies = [
        float(sample.internal_ms) for sample in successful if sample.internal_ms is not None
    ]
    return {
        "load": "Sequential baseline" if load == "sequential" else f"{concurrency} concurrent",
        "concurrency": concurrency,
        "requests": len(samples),
        "successes": len(successful),
        "success_rate": round(len(successful) / len(samples), 4) if samples else 0,
        "timeouts": sum(sample.kind == "client_timeout" for sample in samples),
        "server_errors": sum(sample.kind.startswith("http_") for sample in samples),
        "client_errors": sum(sample.kind == "client_error" for sample in samples),
        "client_p50_ms": _nearest_rank(client_latencies, 50),
        "client_p95_ms": _nearest_rank(client_latencies, 95),
        "internal_p50_ms": _nearest_rank(internal_latencies, 50),
        "internal_p95_ms": _nearest_rank(internal_latencies, 95),
    }


def _sum_metric(delta: dict[str, float], metric_name: str) -> float:
    return sum(
        value
        for descriptor, value in delta.items()
        if descriptor == metric_name or descriptor.startswith(f"{metric_name}{{")
    )


def _metrics_observed(before: dict[str, float], after: dict[str, float]) -> bool:
    # Both scrapes have to carry counters. The delta is built from the second one,
    # so a single missing scrape would silently zero every metric-derived check.
    return bool(before) and bool(after)


def _build_acceptance(
    metric_delta: dict[str, float],
    *,
    failures: list[dict[str, Any]],
    idle_after: bool,
    metrics_observed: bool,
    minimum_recycles: int | None,
) -> dict[str, bool]:
    # A run that scraped no Camouflare counters cannot assert the metric-derived
    # invariants, so it fails them instead of recording a vacuous pass.
    def zero(metric_name: str) -> bool:
        return metrics_observed and _sum_metric(metric_delta, metric_name) == 0

    acceptance = {
        "all_requests_succeeded": not failures,
        "zero_acquire_timeouts": zero("camouflare_pool_acquire_timeout_total"),
        "zero_asyncio_unhandled": zero("camouflare_asyncio_unhandled_total"),
        "zero_browser_transport_errors": zero("camouflare_browser_transport_error_total"),
        "zero_v1_errors": zero("camouflare_v1_error_total"),
        "idle_after": idle_after,
    }
    # Recorded only when the run actually constrained recycles: "at least zero
    # recycles" is not a constraint and does not belong in the evidence.
    if minimum_recycles is not None:
        acceptance["minimum_recycles_observed"] = (
            metrics_observed
            and _sum_metric(metric_delta, "camouflare_browser_recycle_total") >= minimum_recycles
        )
    return acceptance


async def _run(args: argparse.Namespace) -> int:
    token = os.environ.get("CAMOUFLARE_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("CAMOUFLARE_API_TOKEN is required")
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(args.client_timeout_seconds)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        metadata_response = await client.get("/")
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if args.expected_version and version != args.expected_version:
            raise RuntimeError(f"Expected Camouflare {args.expected_version}, found {version!r}")

        for warmup_number in range(1, args.warmup_requests + 1):
            warmup = await _request(
                client,
                load="warmup",
                round_number=warmup_number,
                sample_number=1,
                target_url=args.target_url,
                api_timeout_ms=args.api_timeout_ms,
            )
            if warmup.kind != "success":
                raise RuntimeError(f"Warmup request failed: {warmup}")
        idle_before = await _wait_idle(client)
        counters_before = await _fetch_counters(client, required=args.require_metrics)

        grouped: dict[str, list[Sample]] = {
            "sequential": await _run_sequential(
                client,
                requests=args.sequential_requests,
                target_url=args.target_url,
                api_timeout_ms=args.api_timeout_ms,
            )
        }
        for concurrency in args.concurrency:
            grouped[f"concurrency_{concurrency}"] = await _run_concurrent(
                client,
                concurrency=concurrency,
                rounds=args.rounds,
                target_url=args.target_url,
                api_timeout_ms=args.api_timeout_ms,
            )

        # The idle gates before and between rounds abort the run because a busy pool
        # would invalidate the next measurement. After the last round a pool that
        # never settles is itself the finding, so record it instead of losing the run.
        idle_after, settled_after = await _poll_idle(client)
        counters_after = await _fetch_counters(client, required=args.require_metrics)

    all_samples = [sample for samples in grouped.values() for sample in samples]
    failures = [asdict(sample) for sample in all_samples if sample.kind != "success"]
    metric_delta = _counter_delta(counters_before, counters_after)
    aggregate_rows = [
        _aggregate(
            load,
            1 if load == "sequential" else int(load.removeprefix("concurrency_")),
            samples,
        )
        for load, samples in grouped.items()
    ]
    acceptance = _build_acceptance(
        metric_delta,
        failures=failures,
        idle_after=settled_after,
        metrics_observed=_metrics_observed(counters_before, counters_after),
        minimum_recycles=args.minimum_recycles,
    )
    method: dict[str, Any] = {
        "command": "request.get",
        "api_max_timeout_ms": args.api_timeout_ms,
        "client_timeout_seconds": args.client_timeout_seconds,
        "sequential_requests": args.sequential_requests,
        "concurrency_levels": args.concurrency,
        "rounds_per_concurrency": args.rounds,
        "warmup_requests_excluded": args.warmup_requests,
        "percentile_method": "nearest-rank",
        "latency_percentiles_include": "successful requests only",
        "idle_gate": "active=0, waiting=0, creating=0, closing=0, cleanup_in_flight=0",
        "metrics_required": args.require_metrics,
    }
    if args.minimum_recycles is not None:
        method["minimum_recycles"] = args.minimum_recycles
    payload = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "environment": {
            "label": args.environment_label,
            "client_os": platform.platform(),
            "client_python": platform.python_version(),
            "image": args.image,
        },
        "service": {
            "base_url": args.base_url,
            "target_url": args.target_url,
            "version": version,
        },
        "method": method,
        "diagnostics": {"idle_before": idle_before, "idle_after": idle_after},
        "aggregates": aggregate_rows,
        "successful_latency_samples_ms": {
            load: {
                "client": [sample.client_ms for sample in samples if sample.kind == "success"],
                "internal": [
                    sample.internal_ms
                    for sample in samples
                    if sample.kind == "success" and sample.internal_ms is not None
                ],
            }
            for load, samples in grouped.items()
        },
        "failures": failures,
        "prometheus_delta": metric_delta,
        "acceptance": acceptance,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "acceptance": acceptance}, indent=2))
    return 0 if all(acceptance.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--environment-label", required=True)
    parser.add_argument("--image")
    parser.add_argument("--sequential-requests", type=int, default=10)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--warmup-requests", type=int, default=3)
    parser.add_argument("--api-timeout-ms", type=int, default=60_000)
    parser.add_argument("--client-timeout-seconds", type=float, default=70)
    parser.add_argument("--minimum-recycles", type=int, default=None)
    parser.add_argument("--require-metrics", action="store_true")
    args = parser.parse_args()
    if args.sequential_requests <= 0 or args.rounds <= 0 or args.warmup_requests <= 0:
        parser.error("request counts and rounds must be greater than zero")
    if any(concurrency <= 0 for concurrency in args.concurrency):
        parser.error("concurrency values must be greater than zero")
    # Results are grouped by level, so a repeated level would silently overwrite
    # the earlier pass, failures included.
    if len(set(args.concurrency)) != len(args.concurrency):
        parser.error("concurrency values must be unique")
    if args.minimum_recycles is not None and args.minimum_recycles < 1:
        parser.error("--minimum-recycles must be at least one")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
