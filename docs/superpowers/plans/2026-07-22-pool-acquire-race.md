# Pool Acquire Race Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an acquisition use any newly available compatible slot while pool-owned browser creation continues independently.

**Architecture:** Requests only reserve slots or wait on the pool condition. Browser launch supervision owns registration, launch timeout, abandonment, and notification; request cancellation or timeout never owns or cancels shared creation.

**Tech Stack:** Python asyncio, condition variables, pytest-asyncio deterministic events

## Global Constraints

- Pool size and abandoned-launch limits remain bounded.
- Acquisition tests use events, never timing sleeps, to establish ordering.
- Existing startup, shutdown, cancellation, and quarantine behavior remains leak-free.

---

### Task 1: Reproduce the acquisition race

**Files:**
- Modify: `tests/test_pool.py`

**Interfaces:**
- Consumes: `BrowserPool.lease_context()` and pool condition notifications.
- Produces: a regression test proving released capacity wins over a pending launch.

- [ ] **Step 1: Write the deterministic failing regression test**

```python
@pytest.mark.anyio
async def test_waiter_uses_released_slot_while_shared_launch_is_pending() -> None:
    second_launch_started = asyncio.Event()
    finish_second_launch = asyncio.Event()
    factory = FakeBrowserFactory()

    async def delayed_second_factory() -> FakeBrowser:
        if factory.created:
            second_launch_started.set()
            await finish_second_launch.wait()
        return await factory()

    pool = BrowserPool(browser_factory=delayed_second_factory, min_browsers=1,
                       max_browsers=2, max_contexts_per_browser=1,
                       acquire_timeout_seconds=0.5)
    await pool.start()
    held = pool.lease_context()
    first = await held.__aenter__()
    waiter = asyncio.create_task(pool.lease_context().__aenter__())
    await second_launch_started.wait()
    await held.__aexit__(None, None, None)
    acquired = await asyncio.wait_for(waiter, timeout=0.1)
    assert acquired.browser is first.browser
    finish_second_launch.set()
```

- [ ] **Step 2: Run the regression and confirm it times out on the pending launch**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_pool.py::test_waiter_uses_released_slot_while_shared_launch_is_pending -q`

Expected: FAIL because the requester awaits the second browser task.

### Task 2: Move launch ownership into BrowserPool

**Files:**
- Modify: `camouflare/pool.py`
- Modify: `tests/test_pool.py`

**Interfaces:**
- Produces: `_supervise_create_task(task: asyncio.Task[BrowserSlot]) -> None`.
- Consumes: `_start_create_task_unlocked(supervise=True)` from `_acquire_slot`.

- [ ] **Step 1: Replace request-owned task awaiting with shared condition waiting**

```python
async with self._condition:
    self._refresh_recycling_unlocked()
    reservation = self._first_available_reservation_unlocked(kind)
    if reservation is not None:
        return reservation
    if self._can_start_creation_unlocked():
        self._start_create_task_unlocked(supervise=True)
    self._waiting_requests += 1
    try:
        await asyncio.wait_for(self._condition.wait(), timeout=remaining)
    finally:
        self._waiting_requests -= 1
```

The supervisor waits on the browser factory with the pool-owned launch deadline, registers successful slots under the condition, quarantines timed-out launches, consumes failures, and calls `notify_all()` for every terminal transition.

- [ ] **Step 2: Run the regression and all pool lifecycle tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_pool.py -q`

Expected: PASS, including cancellation-resistant launch and shutdown coverage.

- [ ] **Step 3: Run API saturation tests to preserve 503 behavior**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_api.py -q -k 'pool or capacity or timeout'`

Expected: PASS with existing acquisition HTTP statuses unchanged.
