from __future__ import annotations

import asyncio
import gc
import inspect
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response

from camouflare.app import create_app
from tests.integration.support import (
    LocalHttpServer,
    make_browser_test_settings,
    make_offline_camoufox_factory,
)


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


pytestmark = [
    pytest.mark.soak,
    pytest.mark.browser,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _enabled("CAMOUFLARE_RUN_SOAK"),
        reason="set CAMOUFLARE_RUN_SOAK=1 to run the real-browser soak test",
    ),
]


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


@dataclass(frozen=True)
class SoakConfig:
    requests: int
    duration_seconds: float
    warmup_requests: int
    max_rss_growth_percent: float
    settle_seconds: float
    request_timeout_ms: int

    @classmethod
    def from_env(cls) -> SoakConfig:
        return cls(
            requests=_positive_int("CAMOUFLARE_SOAK_REQUESTS", 1_000),
            duration_seconds=_non_negative_float("CAMOUFLARE_SOAK_DURATION_SECONDS", 3_600),
            # Firefox lazily initializes process-level caches and descriptors across
            # early contexts. Establish a steady-state baseline before measuring
            # long-run resource growth.
            warmup_requests=_positive_int("CAMOUFLARE_SOAK_WARMUP_REQUESTS", 100),
            max_rss_growth_percent=_non_negative_float(
                "CAMOUFLARE_SOAK_MAX_RSS_GROWTH_PERCENT", 15
            ),
            settle_seconds=_non_negative_float("CAMOUFLARE_SOAK_SETTLE_SECONDS", 5),
            request_timeout_ms=_positive_int("CAMOUFLARE_SOAK_REQUEST_TIMEOUT_MS", 15_000),
        )


def _process_tree(root_pid: int | None = None) -> tuple[dict[int, tuple[int, int]], set[int]]:
    root_pid = root_pid or os.getpid()
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="],
        check=True,
        capture_output=True,
        text=True,
    )
    processes: dict[int, tuple[int, int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        pid, parent_pid, rss_kib = (int(field) for field in fields)
        processes[pid] = (parent_pid, rss_kib)

    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent_pid, _) in processes.items():
            if parent_pid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return processes, descendants


def _process_tree_rss_bytes(root_pid: int | None = None) -> int:
    """Return resident memory for pytest and all current browser descendants."""

    processes, descendants = _process_tree(root_pid)
    return sum(processes.get(pid, (0, 0))[1] for pid in descendants) * 1024


def _process_tree_open_fds(root_pid: int | None = None) -> int | None:
    """Count open descriptors for the test process tree where the OS exposes them."""

    _, descendants = _process_tree(root_pid)
    proc_root = Path("/proc")
    if proc_root.is_dir():
        total = 0
        for pid in descendants:
            try:
                total += sum(1 for _ in (proc_root / str(pid) / "fd").iterdir())
            except (FileNotFoundError, PermissionError):
                continue
        return total

    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", ",".join(str(pid) for pid in descendants), "-F", "f"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return sum(1 for line in result.stdout.splitlines() if line.startswith("f"))


async def _active_context_count(pool: Any) -> int:
    snapshot_factory = getattr(pool, "snapshot", None)
    if callable(snapshot_factory):
        snapshot = snapshot_factory()
        if inspect.isawaitable(snapshot):
            snapshot = await snapshot
        for attribute in ("active_contexts", "contexts_active"):
            value = getattr(snapshot, attribute, None)
            if value is not None:
                return int(value)

    # Compatibility with the original pool while the public snapshot interface is added.
    return sum(int(slot.active_contexts) for slot in getattr(pool, "_slots", ()))


def _assert_success(response: Response) -> None:
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "ok", body
    assert body["solution"]["status"] == 200, body
    assert 'id="get-result">get-ok' in body["solution"]["response"], body


async def test_real_browser_memory_and_contexts_stay_bounded() -> None:
    config = SoakConfig.from_env()
    server = LocalHttpServer.start()
    app = create_app(
        settings=make_browser_test_settings(),
        browser_factory=make_offline_camoufox_factory(),
        lifespan_enabled=False,
    )
    await app.state.pool.start()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://camouflare.test",
        ) as client:
            payload = {
                "cmd": "request.get",
                "url": f"{server.base_url}/get",
                "maxTimeout": config.request_timeout_ms,
            }

            for _ in range(config.warmup_requests):
                _assert_success(await client.post("/v1", json=payload))

            gc.collect()
            if config.settle_seconds:
                await asyncio.sleep(config.settle_seconds)
            baseline_rss = _process_tree_rss_bytes()
            baseline_fds = _process_tree_open_fds()
            baseline_pool = app.state.pool.snapshot()
            assert baseline_rss > 0
            assert baseline_pool.active_contexts == 0

            started = time.monotonic()
            for completed in range(1, config.requests + 1):
                _assert_success(await client.post("/v1", json=payload))
                target_elapsed = config.duration_seconds * completed / config.requests
                sleep_for = target_elapsed - (time.monotonic() - started)
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

            elapsed = time.monotonic() - started
            gc.collect()
            if config.settle_seconds:
                await asyncio.sleep(config.settle_seconds)
            final_rss = _process_tree_rss_bytes()
            final_fds = _process_tree_open_fds()
            final_pool = app.state.pool.snapshot()
            growth_percent = max(0.0, (final_rss - baseline_rss) / baseline_rss * 100)

            assert elapsed >= config.duration_seconds
            assert server.request_count >= config.warmup_requests + config.requests
            assert await _active_context_count(app.state.pool) == 0
            assert final_pool.browser_slots == baseline_pool.browser_slots
            assert final_pool.active_contexts == baseline_pool.active_contexts
            if baseline_fds is not None and final_fds is not None:
                assert final_fds <= baseline_fds, (
                    f"process-tree open FDs did not return to baseline "
                    f"({baseline_fds} -> {final_fds})"
                )
            assert growth_percent <= config.max_rss_growth_percent, (
                f"process-tree RSS grew {growth_percent:.2f}% "
                f"({baseline_rss} -> {final_rss} bytes), above the "
                f"{config.max_rss_growth_percent:.2f}% threshold"
            )
    finally:
        try:
            await app.state.sessions.close()
        finally:
            try:
                await app.state.pool.close()
            finally:
                server.close()
