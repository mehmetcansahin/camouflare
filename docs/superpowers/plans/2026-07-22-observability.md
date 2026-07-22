# Error Resilience Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit one safe structured completion event per `/v1` request and bounded error/transport metrics.

**Architecture:** The controller owns the request completion event because it has the final response, status, duration, and error metadata. Metrics helpers enforce allowlists before label creation, while browser lifecycle/navigation sites emit sanitized transport events through existing logging formatters.

**Tech Stack:** Python logging, JSON formatter/redaction, prometheus-client, pytest caplog

## Global Constraints

- Never log full URLs, queries, cookies, tokens, bodies, proxy credentials, or sensitive values.
- Target hostnames never become Prometheus labels.
- Metric labels are restricted to bounded command, error-code, and phase allowlists.
- Expected domain failures have no traceback; unexpected failures use `logger.exception`.

---

### Task 1: Add bounded resilience metrics

**Files:**
- Modify: `camouflare/metrics.py`
- Modify: `tests/test_observability.py`

**Interfaces:**
- Produces: `record_v1_error(command: str, error_code: str) -> None`.
- Produces: `record_browser_transport_error(phase: str) -> None`.

- [ ] **Step 1: Write failing bounded-label tests**

```python
metrics.record_v1_error("attacker-command", "attacker-code")
assert sample(metrics.V1_ERROR_COUNTER, command="unknown", error_code="INTERNAL_ERROR") == before + 1
metrics.record_browser_transport_error("attacker-phase")
assert sample(metrics.BROWSER_TRANSPORT_ERROR_COUNTER, phase="other") == phase_before + 1
```

- [ ] **Step 2: Run the focused test and confirm collectors are missing**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_observability.py -q -k resilience_metrics`

Expected: FAIL because the two collectors and hooks do not exist.

- [ ] **Step 3: Add allowlisted counters and recording helpers**

```python
V1_ERROR_COUNTER = _counter(
    "camouflare_v1_error_total", "Total /v1 errors by command and code.",
    ("command", "error_code"),
)
BROWSER_TRANSPORT_ERROR_COUNTER = _counter(
    "camouflare_browser_transport_error_total",
    "Browser transport errors by bounded phase.", ("phase",),
)
```

- [ ] **Step 4: Run observability tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_observability.py -q`

Expected: PASS and exported text includes both new metric names.

### Task 2: Structured completion and browser transport events

**Files:**
- Modify: `camouflare/app.py`
- Modify: `camouflare/navigation.py`
- Modify: `camouflare/pool.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_observability.py`

**Interfaces:**
- Produces one `V1 request completed.` record with `command`, `result`, `http_status`, `error_code`, `retryable`, `request_outcome_unknown`, `duration_ms`, `target_host`, and `fallback_used`.
- Produces browser records with `phase`, `error_type`, `browser_state`, `slot_uses`, `slot_active_contexts`, `retire_reason`, and `fallback_used` where applicable.

- [ ] **Step 1: Write failing completion-event and redaction tests**

```python
record = next(item for item in caplog.records if item.message == "V1 request completed.")
assert record.command == "request.get"
assert record.target_host == "example.com"
assert "secret=1" not in JsonLogFormatter().format(record)
assert record.http_status == response.status_code
```

- [ ] **Step 2: Run the completion tests and confirm no record is emitted**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_api.py tests/test_observability.py -q -k 'completion_event or new_field_redaction'`

Expected: FAIL because completion logging is absent.

- [ ] **Step 3: Emit final structured records and unexpected tracebacks**

```python
logger.info(
    "V1 request completed.",
    extra={"command": command, "result": result, "http_status": status_code,
           "error_code": error_code, "retryable": retryable,
           "request_outcome_unknown": request_outcome_unknown,
           "duration_ms": round((time.monotonic() - started) * 1000),
           "target_host": target_host, "fallback_used": fallback_used},
)
```

- [ ] **Step 4: Run API, pool, and observability tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_api.py tests/test_pool.py tests/test_observability.py -q`

Expected: PASS with expected errors logged without tracebacks and internal errors logged with tracebacks.
