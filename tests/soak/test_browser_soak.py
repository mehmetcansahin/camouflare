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
            requests=_positive_int("CAMOUFLARE_SOAK_REQUESTS", 100),
            duration_seconds=_non_negative_float("CAMOUFLARE_SOAK_DURATION_SECONDS", 300),
            # Firefox lazily initializes process-level caches and descriptors across
            # early contexts. Keep the established warmup before measuring resource
            # growth. The five-minute profile keeps five measured recycle cycles
            # while ending with an equally provisioned idle pool.
            warmup_requests=_positive_int("CAMOUFLARE_SOAK_WARMUP_REQUESTS", 100),
            browser_max_uses=_positive_int("CAMOUFLARE_SOAK_BROWSER_MAX_USES", 20),
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


def _process_parents() -> dict[int, int]:
    """Return a live PID-to-parent snapshot without counting the sampler itself."""

    proc_root = Path("/proc")
    if proc_root.is_dir():
        parents: dict[int, int] = {}
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
                fields = stat[stat.rfind(")") + 2 :].split()
                parents[int(entry.name)] = int(fields[1])
            except (FileNotFoundError, PermissionError, IndexError, ValueError):
                continue
        return parents

    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid="],
        check=True,
        capture_output=True,
        text=True,
    )
    parents = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        pid, parent_pid = (int(field) for field in fields)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # ``ps`` reports itself as a child of pytest, but it has exited before
            # subprocess.run returns. Exclude every process no longer alive now.
            continue
        except PermissionError:
            pass
        parents[pid] = parent_pid
    return parents


def _live_descendant_pids(root_pid: int | None = None) -> frozenset[int]:
    root_pid = root_pid or os.getpid()
    parents = _process_parents()
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent_pid in parents.items():
            if parent_pid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return frozenset(descendants)


def _describe_processes(processes: frozenset[int]) -> list[str]:
    if not processes:
        return []
    result = subprocess.run(
        [
            "ps",
            "-o",
            "pid=,ppid=,state=,command=",
            "-p",
            ",".join(str(pid) for pid in sorted(processes)),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _process_command(pid: int) -> str:
    command_path = Path("/proc") / str(pid) / "cmdline"
    if command_path.is_file():
        try:
            return command_path.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except (FileNotFoundError, PermissionError):
            return ""
    result = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _unexpected_drained_processes(
    processes: frozenset[int],
    *,
    root_pid: int,
) -> frozenset[int]:
    resource_tracker_seen = False
    unexpected: set[int] = set()
    for pid in processes:
        if pid == root_pid:
            continue
        command = _process_command(pid)
        if (
            not resource_tracker_seen
            and "from multiprocessing.resource_tracker import main" in command
        ):
            resource_tracker_seen = True
            continue
        unexpected.add(pid)
    return frozenset(unexpected)


def _process_rss_bytes(pid: int) -> int:
    status_path = Path("/proc") / str(pid) / "status"
    if status_path.is_file():
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
        raise RuntimeError(f"{status_path} does not expose VmRSS")

    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip()) * 1024


def _processes_rss_bytes(processes: frozenset[int]) -> int:
    return sum(_process_rss_bytes(pid) for pid in processes)


def _process_open_fd_signature(pid: int) -> frozenset[str] | None:
    """Return stable identities for numeric descriptors owned by one process."""

    fd_root = Path("/proc") / str(pid) / "fd"
    if fd_root.is_dir():
        signature: set[str] = set()
        try:
            with os.scandir(fd_root) as entries:
                for entry in entries:
                    if not entry.name.isdigit():
                        continue
                    try:
                        target = os.readlink(entry.path)
                    except FileNotFoundError:
                        continue
                    # Scanning /proc/<pid>/fd briefly opens that directory in the
                    # target process. It is a measurement artifact, not an app FD.
                    if target == str(fd_root):
                        continue
                    signature.add(f"{entry.name}:{target}")
        except (FileNotFoundError, PermissionError):
            return None
        return frozenset(signature)

    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-F", "f"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    # lsof also reports cwd/txt/memory mappings as pseudo descriptors. Only numeric
    # entries correspond to descriptors and remain stable across sampler processes.
    return frozenset(
        line[1:]
        for line in result.stdout.splitlines()
        if line.startswith("f") and line[1:].isdigit()
    )


def _processes_open_fd_signature(processes: frozenset[int]) -> frozenset[str] | None:
    signature: set[str] = set()
    for pid in processes:
        process_signature = _process_open_fd_signature(pid)
        if process_signature is None:
            return None
        signature.update(f"{pid}:{descriptor}" for descriptor in process_signature)
    return frozenset(signature)


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


@dataclass(frozen=True)
class _DrainedResourceSnapshot:
    runtime: _RuntimeSettleState
    rss_bytes: int
    fd_signature: frozenset[str] | None
    processes: frozenset[int]
    task_count: int

    @property
    def open_fds(self) -> int | None:
        if self.fd_signature is None:
            return None
        return len(self.fd_signature)


async def _settle_drained_resources(
    app: Any,
    *,
    timeout_seconds: float,
    phase: str,
    expected_processes: frozenset[int] | None = None,
) -> _DrainedResourceSnapshot:
    """Require a quiescent runtime, no browser children, and stable process FDs."""

    await _settle_runtime(
        app,
        timeout_seconds=timeout_seconds,
        phase=phase,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    root_pid = os.getpid()
    previous_signature: frozenset[str] | None = None
    previous_processes: frozenset[int] | None = None
    have_previous_signature = False
    stable_observations = 0
    runtime = _runtime_settle_state(app)
    processes = _live_descendant_pids(root_pid)
    fd_signature = _processes_open_fd_signature(processes)

    while True:
        unexpected_processes = _unexpected_drained_processes(processes, root_pid=root_pid)
        process_tree_matches = (
            processes == expected_processes
            if expected_processes is not None
            else not unexpected_processes
        )
        if runtime.settled and process_tree_matches:
            if (
                have_previous_signature
                and processes == previous_processes
                and fd_signature == previous_signature
            ):
                stable_observations += 1
            else:
                stable_observations = 1
            previous_signature = fd_signature
            previous_processes = processes
            have_previous_signature = True
            if stable_observations >= 3:
                return _DrainedResourceSnapshot(
                    runtime=runtime,
                    rss_bytes=_processes_rss_bytes(processes),
                    fd_signature=fd_signature,
                    processes=processes,
                    task_count=len(asyncio.all_tasks()),
                )
        else:
            stable_observations = 0
            previous_signature = None
            previous_processes = None
            have_previous_signature = False

        remaining = deadline - loop.time()
        if remaining <= 0:
            fd_count = None if fd_signature is None else len(fd_signature)
            pytest.fail(
                f"drained resources did not stabilize during {phase} within "
                f"{timeout_seconds:.3f}s: processes={sorted(processes)!r}, "
                f"unexpected={sorted(unexpected_processes)!r}, "
                f"process_details={_describe_processes(processes)!r}, "
                f"root_fds={fd_count}, runtime={runtime.describe()}"
            )
        await asyncio.sleep(min(0.25, remaining))
        runtime = _runtime_settle_state(app)
        processes = _live_descendant_pids(root_pid)
        fd_signature = _processes_open_fd_signature(processes)


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


async def _exercise_persistent_session(
    client: AsyncClient,
    payload: dict[str, Any],
    session_id: str,
) -> None:
    created = await client.post(
        "/v1",
        json={
            "cmd": "sessions.create",
            "session": session_id,
            "maxTimeout": payload["maxTimeout"],
        },
    )
    assert created.status_code == 200, created.json()

    _assert_success(
        await client.post(
            "/v1",
            json={**payload, "session": session_id},
        )
    )

    destroyed = await client.post(
        "/v1",
        json={
            "cmd": "sessions.destroy",
            "session": session_id,
            "maxTimeout": payload["maxTimeout"],
        },
    )
    assert destroyed.status_code == 200, destroyed.json()


async def _drain_browser_for_resource_baseline(
    app: Any,
    client: AsyncClient,
    payload: dict[str, Any],
) -> None:
    """Retire the warmed browser without creating replacement capacity."""

    pool = app.state.pool
    assert len(pool._slots) == 1
    previous_slot = pool._slots[0]
    assert previous_slot.state == "ready"
    assert previous_slot.active_contexts == 0

    original_max_uses = pool._browser_max_uses
    pool._browser_max_uses = previous_slot.uses + 1
    try:
        _assert_success(await client.post("/v1", json=payload))
    finally:
        pool._browser_max_uses = original_max_uses

    assert previous_slot not in pool._slots
    assert not pool._slots


async def test_real_browser_memory_and_contexts_stay_bounded() -> None:
    config = SoakConfig.from_env()
    assert config.requests % config.browser_max_uses == 0, (
        "the soak request count must be an exact multiple of browser_max_uses so "
        "the measured process tree ends fully drained"
    )
    expected_measurement_launches = config.requests // config.browser_max_uses
    assert expected_measurement_launches > 0, (
        "the soak profile must include at least one measured browser recycle"
    )
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

            # A warmup count may land exactly on a max-use boundary and leave the
            # demand-driven pool empty. Seed one normal lease for the persistent
            # first-use warmup, then drain it before sampling resources.
            _assert_ready(await client.get("/ready"))

            # Persistent contexts lazily initialize browser and Playwright
            # descriptors that transient warmup does not exercise. Run the same
            # persistent path on both sides of the lifecycle gate so the strict FD
            # comparison measures accumulation instead of first-use initialization.
            await _exercise_persistent_session(
                client,
                payload,
                "soak-pre-lifecycle-baseline",
            )
            await _drain_browser_for_resource_baseline(app, client, payload)

            gc.collect()
            pre_lifecycle_resources = await _settle_drained_resources(
                app,
                timeout_seconds=config.settle_seconds,
                phase="pre-lifecycle baseline",
            )
            pre_lifecycle_state = pre_lifecycle_resources.runtime
            pre_lifecycle_pool = pre_lifecycle_state.pool
            pre_lifecycle_fds = pre_lifecycle_resources.open_fds
            pre_lifecycle_processes = pre_lifecycle_resources.processes
            pre_lifecycle_task_count = pre_lifecycle_resources.task_count
            assert pre_lifecycle_pool.browser_slots == 0
            assert pre_lifecycle_pool.active_contexts == 0

            # Two idle max-age cycles surround an aged persistent session cycle,
            # and readiness competes with normal traffic in each.
            _assert_ready(await client.get("/ready"))
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
            # warm the same persistent path, then fully drain both sides before
            # comparing process resources.
            _assert_ready(await client.get("/ready"))
            await _exercise_persistent_session(
                client,
                payload,
                "soak-post-lifecycle-baseline",
            )
            await _drain_browser_for_resource_baseline(app, client, payload)

            gc.collect()
            post_lifecycle_resources = await _settle_drained_resources(
                app,
                timeout_seconds=config.settle_seconds,
                phase="post-lifecycle baseline",
                expected_processes=pre_lifecycle_processes,
            )
            post_lifecycle_state = post_lifecycle_resources.runtime
            post_lifecycle_pool = post_lifecycle_state.pool
            post_lifecycle_fds = post_lifecycle_resources.open_fds
            post_lifecycle_processes = post_lifecycle_resources.processes
            post_lifecycle_task_count = post_lifecycle_resources.task_count
            assert post_lifecycle_pool.browser_slots == pre_lifecycle_pool.browser_slots
            assert post_lifecycle_pool.browser_slots == 0
            assert post_lifecycle_pool.active_contexts == pre_lifecycle_pool.active_contexts
            assert post_lifecycle_processes == pre_lifecycle_processes, (
                "lifecycle process tree did not return to the drained baseline "
                f"({sorted(pre_lifecycle_processes)!r} -> "
                f"{sorted(post_lifecycle_processes)!r})"
            )
            assert post_lifecycle_task_count <= pre_lifecycle_task_count, (
                "lifecycle task count did not return to baseline "
                f"({pre_lifecycle_task_count} -> {post_lifecycle_task_count})"
            )
            assert app.state.sessions.snapshot().active == 0
            assert app.state.sessions.snapshot().closing == 0
            assert app.state.cleanup.snapshot().in_flight == 0
            if pre_lifecycle_fds is not None and post_lifecycle_fds is not None:
                pre_lifecycle_signature = pre_lifecycle_resources.fd_signature or frozenset()
                post_lifecycle_signature = post_lifecycle_resources.fd_signature or frozenset()
                added_lifecycle_fds = sorted(post_lifecycle_signature - pre_lifecycle_signature)
                assert post_lifecycle_fds <= pre_lifecycle_fds, (
                    "lifecycle process-tree open FDs did not return to baseline "
                    f"({pre_lifecycle_fds} -> {post_lifecycle_fds}); "
                    f"added={added_lifecycle_fds!r}"
                )

            baseline_rss = post_lifecycle_resources.rss_bytes
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
            final_resources = await _settle_drained_resources(
                app,
                timeout_seconds=config.settle_seconds,
                phase="final measurement",
                expected_processes=baseline_processes,
            )
            final_state = final_resources.runtime
            final_rss = final_resources.rss_bytes
            final_fds = final_resources.open_fds
            final_pool = final_state.pool
            final_processes = final_resources.processes
            final_task_count = final_resources.task_count
            growth_percent = max(0.0, (final_rss - baseline_rss) / baseline_rss * 100)
            actual_measurement_launches = browser_launches - baseline_browser_launches

            assert elapsed >= config.duration_seconds
            assert server.request_count >= config.warmup_requests + config.requests
            assert actual_measurement_launches == expected_measurement_launches, (
                "the soak measurement did not exercise the expected browser recycles "
                f"(expected_launches={expected_measurement_launches}, "
                f"actual_launches={actual_measurement_launches}, "
                f"browser_max_uses={config.browser_max_uses})"
            )
            assert await _active_context_count(app.state.pool) == 0
            assert final_pool.browser_slots == baseline_pool.browser_slots
            assert final_pool.browser_slots == 0
            assert final_pool.active_contexts == baseline_pool.active_contexts
            assert final_processes == baseline_processes, (
                "process tree did not return to the drained baseline "
                f"({sorted(baseline_processes)!r} -> {sorted(final_processes)!r})"
            )
            assert final_task_count <= baseline_task_count, (
                "asyncio task count did not return to baseline "
                f"({baseline_task_count} -> {final_task_count})"
            )
            assert app.state.sessions.snapshot().active == 0
            assert app.state.sessions.snapshot().closing == 0
            assert app.state.cleanup.snapshot().in_flight == 0
            if baseline_fds is not None and final_fds is not None:
                baseline_signature = post_lifecycle_resources.fd_signature or frozenset()
                final_signature = final_resources.fd_signature or frozenset()
                added_fds = sorted(final_signature - baseline_signature)
                assert final_fds <= baseline_fds, (
                    f"process-tree open FDs did not return to baseline "
                    f"({baseline_fds} -> {final_fds}); added={added_fds!r}"
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
