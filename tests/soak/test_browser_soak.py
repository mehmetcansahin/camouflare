from __future__ import annotations

import asyncio
import gc
import inspect
import os
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response

from camouflare.app import create_app
from camouflare.runtime import shutdown_runtime
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
    browser_max_uses: int
    max_rss_growth_percent: float
    settle_seconds: float
    request_timeout_ms: int
    lifecycle_max_age_seconds: float

    @classmethod
    def from_env(cls) -> SoakConfig:
        return cls(
            requests=_positive_int("CAMOUFLARE_SOAK_REQUESTS", 1_000),
            duration_seconds=_non_negative_float("CAMOUFLARE_SOAK_DURATION_SECONDS", 3_600),
            # Firefox lazily initializes process-level caches and descriptors across
            # early contexts. Establish a steady-state baseline before measuring
            # long-run resource growth.
            warmup_requests=_positive_int("CAMOUFLARE_SOAK_WARMUP_REQUESTS", 100),
            browser_max_uses=_positive_int("CAMOUFLARE_SOAK_BROWSER_MAX_USES", 200),
            max_rss_growth_percent=_non_negative_float(
                "CAMOUFLARE_SOAK_MAX_RSS_GROWTH_PERCENT", 15
            ),
            settle_seconds=_non_negative_float("CAMOUFLARE_SOAK_SETTLE_SECONDS", 5),
            # Browser recycling includes a full process shutdown and relaunch. Use
            # the public API default so the resource gate does not become a stricter
            # startup-latency test at lifecycle boundaries.
            request_timeout_ms=_positive_int("CAMOUFLARE_SOAK_REQUEST_TIMEOUT_MS", 60_000),
            lifecycle_max_age_seconds=max(
                0.1,
                _non_negative_float("CAMOUFLARE_SOAK_LIFECYCLE_MAX_AGE_SECONDS", 1),
            ),
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


@dataclass(frozen=True)
class _RuntimeSettleState:
    pool: Any
    sessions: Any
    cleanup: Any
    pending_pool_tasks: tuple[str, ...]

    @property
    def settled(self) -> bool:
        return (
            self.pool.creating_slots == 0
            and self.pool.closing_slots == 0
            and self.pool.retiring_browser_slots == 0
            and self.pool.active_contexts == 0
            and self.pool.transient_contexts == 0
            and self.pool.persistent_contexts == 0
            and self.pool.waiting_requests == 0
            and self.sessions.active == 0
            and self.sessions.in_use == 0
            and self.sessions.closing == 0
            and self.cleanup.in_flight == 0
            and not self.pending_pool_tasks
        )

    def describe(self) -> str:
        return (
            "pool("
            f"ready={self.pool.ready_browser_slots}, "
            f"retiring={self.pool.retiring_browser_slots}, "
            f"creating={self.pool.creating_slots}, "
            f"closing={self.pool.closing_slots}, "
            f"active={self.pool.active_contexts}, "
            f"transient={self.pool.transient_contexts}, "
            f"persistent={self.pool.persistent_contexts}, "
            f"waiting={self.pool.waiting_requests}"
            "); sessions("
            f"active={self.sessions.active}, "
            f"in_use={self.sessions.in_use}, "
            f"closing={self.sessions.closing}"
            "); cleanup("
            f"in_flight={self.cleanup.in_flight}, "
            f"by_kind={self.cleanup.by_kind}"
            f"); pending_pool_tasks={list(self.pending_pool_tasks)!r}"
        )


def _pending_pool_task_descriptions(pool: Any) -> tuple[str, ...]:
    """Describe live pool-owned work not fully represented by public counters."""

    pending: dict[asyncio.Future[Any], set[str]] = {}

    def include(owner: str, future: Any) -> None:
        if not isinstance(future, asyncio.Future) or future.done():
            return
        pending.setdefault(future, set()).add(owner)

    for attribute in (
        "_create_tasks",
        "_accounting_tasks",
        "_cleanup_tasks",
        "_watched_physical_close_tasks",
    ):
        for future in getattr(pool, attribute, ()):
            include(attribute, future)

    for attribute in ("_create_watchers", "_close_tasks"):
        owned = getattr(pool, attribute, {})
        for future in owned:
            include(attribute, future)
        for future in owned.values():
            include(attribute, future)

    descriptions: list[str] = []
    for future, owners in pending.items():
        get_name = getattr(future, "get_name", None)
        name = get_name() if callable(get_name) else type(future).__name__
        descriptions.append(f"{'/'.join(sorted(owners))}:{name}")
    return tuple(sorted(descriptions))


def _runtime_settle_state(app: Any) -> _RuntimeSettleState:
    return _RuntimeSettleState(
        pool=app.state.pool.snapshot(),
        sessions=app.state.sessions.snapshot(),
        cleanup=app.state.cleanup.snapshot(),
        pending_pool_tasks=_pending_pool_task_descriptions(app.state.pool),
    )


async def _settle_runtime(
    app: Any,
    *,
    timeout_seconds: float,
    phase: str,
) -> _RuntimeSettleState:
    """Wait for two consecutive quiescent observations before sampling resources."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    settled_observations = 0
    state = _runtime_settle_state(app)

    while True:
        if state.settled:
            settled_observations += 1
            if settled_observations >= 2:
                return state
            # Let task callbacks remove completed ownership records before the
            # second observation. A zero timeout still permits this deterministic
            # confirmation when the runtime was already quiet.
            await asyncio.sleep(0)
        else:
            settled_observations = 0
            remaining = deadline - loop.time()
            if remaining <= 0:
                pytest.fail(
                    f"runtime did not settle during {phase} within "
                    f"{timeout_seconds:.3f}s: {state.describe()}"
                )
            await asyncio.sleep(min(0.05, remaining))
        state = _runtime_settle_state(app)


def _assert_success(response: Response) -> None:
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "ok", body
    assert body["solution"]["status"] == 200, body
    assert 'id="get-result">get-ok' in body["solution"]["response"], body


def _assert_ready(response: Response) -> None:
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "ok", body


async def test_real_browser_memory_and_contexts_stay_bounded() -> None:
    config = SoakConfig.from_env()
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    initial_tasks = set(asyncio.all_tasks())
    unexpected_asyncio_events: list[str] = []

    def capture_asyncio_event(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        message = str(context.get("message", ""))
        normalized = message.lower()
        if (
            any(
                marker in normalized
                for marker in (
                    "exception was never retrieved",
                    "destroyed but it is pending",
                    "async generator",
                    "async_generator",
                    "asynchronous generator",
                )
            )
            or context.get("asyncgen") is not None
        ):
            exception = context.get("exception")
            unexpected_asyncio_events.append(
                f"{message}: {exception!r}" if exception is not None else message
            )
            return
        if previous_exception_handler is not None:
            previous_exception_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(capture_asyncio_event)
    server = LocalHttpServer.start()
    browser_factory = make_offline_camoufox_factory()
    browser_launches = 0

    async def counted_browser_factory() -> Any:
        nonlocal browser_launches
        browser_launches += 1
        return await browser_factory()

    settings = replace(
        make_browser_test_settings(),
        browser_max_uses=config.browser_max_uses,
    )
    app = create_app(
        settings=settings,
        browser_factory=counted_browser_factory,
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
            pre_lifecycle_state = await _settle_runtime(
                app,
                timeout_seconds=config.settle_seconds,
                phase="pre-lifecycle baseline",
            )
            pre_lifecycle_pool = pre_lifecycle_state.pool
            pre_lifecycle_fds = _process_tree_open_fds()
            _, pre_lifecycle_processes = _process_tree()
            pre_lifecycle_task_count = len(asyncio.all_tasks())
            assert pre_lifecycle_pool.active_contexts == 0

            # Two idle max-age cycles surround an aged persistent session cycle,
            # and readiness competes with normal traffic in each.
            lifecycle_launches = browser_launches
            pool = app.state.pool
            original_max_age = pool._browser_max_age_seconds
            pool._browser_max_age_seconds = config.lifecycle_max_age_seconds
            lifecycle_pause = config.lifecycle_max_age_seconds + 0.1
            session_id = "soak-lifecycle-session"
            try:
                await asyncio.sleep(lifecycle_pause)
                ready_response, request_response = await asyncio.gather(
                    client.get("/ready"),
                    client.post("/v1", json=payload),
                )
                _assert_ready(ready_response)
                _assert_success(request_response)

                created = await client.post(
                    "/v1",
                    json={
                        "cmd": "sessions.create",
                        "session": session_id,
                        "maxTimeout": config.request_timeout_ms,
                    },
                )
                assert created.status_code == 200, created.json()
                await asyncio.sleep(lifecycle_pause)
                ready_response, session_response = await asyncio.gather(
                    client.get("/ready"),
                    client.post(
                        "/v1",
                        json={**payload, "session": session_id},
                    ),
                )
                _assert_ready(ready_response)
                _assert_success(session_response)
                destroyed = await client.post(
                    "/v1",
                    json={
                        "cmd": "sessions.destroy",
                        "session": session_id,
                        "maxTimeout": config.request_timeout_ms,
                    },
                )
                assert destroyed.status_code == 200, destroyed.json()

                # Seed the replacement left by the active-slot retirement, then
                # age it while idle for the second explicit idle recycle.
                _assert_ready(await client.get("/ready"))
                await asyncio.sleep(lifecycle_pause)
                ready_response, request_response = await asyncio.gather(
                    client.get("/ready"),
                    client.post("/v1", json=payload),
                )
                _assert_ready(ready_response)
                _assert_success(request_response)
            finally:
                pool._browser_max_age_seconds = original_max_age
                if app.state.sessions.get(session_id) is not None:
                    await client.post(
                        "/v1",
                        json={
                            "cmd": "sessions.destroy",
                            "session": session_id,
                            "maxTimeout": config.request_timeout_ms,
                        },
                    )

            assert browser_launches >= lifecycle_launches + 3, (
                "lifecycle gate did not replace both idle-aged browsers and "
                "the aged persistent-session browser"
            )

            # The synthetic lifecycle max-age can be shorter than one real-browser
            # request. In that case the final replacement legitimately retires on
            # release and leaves the demand-driven pool empty. Reseed only after
            # verifying the lifecycle launch count and restoring the normal max-age,
            # so resource baselines compare equally provisioned idle pools instead of
            # browser-present versus browser-empty.
            _assert_ready(await client.get("/ready"))

            gc.collect()
            post_lifecycle_state = await _settle_runtime(
                app,
                timeout_seconds=config.settle_seconds,
                phase="post-lifecycle baseline",
            )
            post_lifecycle_pool = post_lifecycle_state.pool
            post_lifecycle_fds = _process_tree_open_fds()
            _, post_lifecycle_processes = _process_tree()
            post_lifecycle_task_count = len(asyncio.all_tasks())
            assert post_lifecycle_pool.browser_slots == pre_lifecycle_pool.browser_slots
            assert post_lifecycle_pool.active_contexts == pre_lifecycle_pool.active_contexts
            assert len(post_lifecycle_processes) <= len(pre_lifecycle_processes), (
                "lifecycle process count did not return to baseline "
                f"({len(pre_lifecycle_processes)} -> {len(post_lifecycle_processes)})"
            )
            assert post_lifecycle_task_count <= pre_lifecycle_task_count, (
                "lifecycle task count did not return to baseline "
                f"({pre_lifecycle_task_count} -> {post_lifecycle_task_count})"
            )
            assert app.state.sessions.snapshot().active == 0
            assert app.state.sessions.snapshot().closing == 0
            assert app.state.cleanup.snapshot().in_flight == 0
            if pre_lifecycle_fds is not None and post_lifecycle_fds is not None:
                assert post_lifecycle_fds <= pre_lifecycle_fds, (
                    "lifecycle process-tree open FDs did not return to baseline "
                    f"({pre_lifecycle_fds} -> {post_lifecycle_fds})"
                )

            baseline_rss = _process_tree_rss_bytes()
            baseline_fds = post_lifecycle_fds
            baseline_pool = post_lifecycle_pool
            baseline_browser_launches = browser_launches
            baseline_processes = post_lifecycle_processes
            baseline_task_count = post_lifecycle_task_count
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
            final_state = await _settle_runtime(
                app,
                timeout_seconds=config.settle_seconds,
                phase="final measurement",
            )
            final_rss = _process_tree_rss_bytes()
            final_fds = _process_tree_open_fds()
            final_pool = final_state.pool
            _, final_processes = _process_tree()
            final_task_count = len(asyncio.all_tasks())
            growth_percent = max(0.0, (final_rss - baseline_rss) / baseline_rss * 100)

            assert elapsed >= config.duration_seconds
            assert server.request_count >= config.warmup_requests + config.requests
            assert browser_launches > baseline_browser_launches, (
                "the soak measurement did not exercise browser recycling "
                f"(browser_max_uses={config.browser_max_uses})"
            )
            assert await _active_context_count(app.state.pool) == 0
            assert final_pool.browser_slots == baseline_pool.browser_slots
            assert final_pool.active_contexts == baseline_pool.active_contexts
            assert len(final_processes) <= len(baseline_processes), (
                "process count did not return to baseline "
                f"({len(baseline_processes)} -> {len(final_processes)})"
            )
            assert final_task_count <= baseline_task_count, (
                "asyncio task count did not return to baseline "
                f"({baseline_task_count} -> {final_task_count})"
            )
            assert app.state.sessions.snapshot().active == 0
            assert app.state.sessions.snapshot().closing == 0
            assert app.state.cleanup.snapshot().in_flight == 0
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
            await shutdown_runtime(
                sessions=app.state.sessions,
                pool=app.state.pool,
                cleanup=app.state.cleanup,
                timeout_seconds=settings.cleanup_timeout_seconds,
            )
        finally:
            server.close()

        gc.collect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        current_task = asyncio.current_task()
        pending_tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not current_task and task not in initial_tasks and not task.done()
        ]
        pending_descriptions = [task.get_name() for task in pending_tasks]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.wait(pending_tasks, timeout=0.5)
            await asyncio.sleep(0)
        loop.set_exception_handler(previous_exception_handler)

        assert not unexpected_asyncio_events, "unexpected asyncio events: " + "; ".join(
            unexpected_asyncio_events
        )
        assert not pending_descriptions, "pending asyncio tasks after soak teardown: " + ", ".join(
            pending_descriptions
        )
