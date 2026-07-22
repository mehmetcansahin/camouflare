# Error Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add stable, optional machine-readable error metadata while preserving every existing `/v1` HTTP status.

**Architecture:** Define the bounded error vocabulary and domain exception in a dependency-light module. Serialize optional metadata through `V1Response`, classify expected command/solver failures at their source, and let the controller render `CamouflareError` through the existing response envelope and status mapping.

**Tech Stack:** Python 3.11+, Pydantic 2, FastAPI, pytest, HTTPX ASGI transport

## Global Constraints

- Preserve the current `/v1` HTTP status behavior for backward compatibility.
- Optional metadata must be omitted when it does not apply.
- Partial `Solution` objects must survive error mapping.
- Unexpected exceptions use `INTERNAL_ERROR` and retain a server-side traceback.

---

### Task 1: Error vocabulary and response serialization

**Files:**
- Create: `camouflare/errors.py`
- Modify: `camouflare/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `V1ErrorCode`, `CamouflareError`, and optional `V1Response.error_code`, `retryable`, `request_outcome_unknown`, and `fallback_used` fields.
- Consumes: `Solution` as an optional type-only exception payload.

- [ ] **Step 1: Write the failing serialization and exception tests**

```python
def test_v1_response_omits_unused_resilience_metadata() -> None:
    payload = V1Response(status="ok", version="test").model_dump(
        by_alias=True, exclude_none=True
    )
    assert "errorCode" not in payload
    assert "retryable" not in payload
    assert "requestOutcomeUnknown" not in payload
    assert "fallbackUsed" not in payload


def test_camouflare_error_carries_machine_readable_metadata() -> None:
    error = CamouflareError(
        "Browser transport closed.",
        error_code=V1ErrorCode.BROWSER_TRANSPORT_CLOSED,
        retryable=True,
    )
    assert str(error) == "Browser transport closed."
    assert error.error_code is V1ErrorCode.BROWSER_TRANSPORT_CLOSED
    assert error.retryable is True
    assert error.request_outcome_unknown is False
```
- [ ] **Step 2: Run the focused tests and confirm missing imports/fields fail**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_models.py -q`

Expected: FAIL because `camouflare.errors` and the response fields do not exist.

- [ ] **Step 3: Implement the bounded enum, exception, and aliased optional fields**

```python
class V1ErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"
    POOL_UNAVAILABLE = "POOL_UNAVAILABLE"
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
    NAVIGATION_TIMEOUT = "NAVIGATION_TIMEOUT"
    BROWSER_TRANSPORT_CLOSED = "BROWSER_TRANSPORT_CLOSED"
    CHALLENGE_FAILED = "CHALLENGE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CamouflareError(Exception):
    def __init__(self, message: str, *, error_code: V1ErrorCode,
                 retryable: bool = False, request_outcome_unknown: bool = False,
                 solution: Solution | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.request_outcome_unknown = request_outcome_unknown
        self.solution = solution
```

- [ ] **Step 4: Run the focused tests and confirm they pass**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_models.py -q`

Expected: PASS.

### Task 2: Controller mapping and expected domain failures

**Files:**
- Modify: `camouflare/commands.py`
- Modify: `camouflare/solver.py`
- Modify: `camouflare/app.py`
- Test: `tests/test_api.py`
- Test: `tests/test_solver.py`

**Interfaces:**
- Consumes: `CamouflareError(message, error_code, retryable, request_outcome_unknown, solution)`.
- Produces: `_error_status_code(error_code) -> int` and stable response metadata for all nine codes.

- [ ] **Step 1: Add failing API cases for the bounded categories and old status codes**

```python
@pytest.mark.anyio
async def test_v1_invalid_request_has_stable_error_code() -> None:
    response = await post_v1({"cmd": "nope"})
    assert response.status_code == 500
    assert response.json()["errorCode"] == "INVALID_REQUEST"


@pytest.mark.anyio
async def test_v1_pool_timeout_keeps_503_and_reports_pool_unavailable() -> None:
    response = await post_saturated_pool_request()
    assert response.status_code == 503
    assert response.json()["errorCode"] == "POOL_UNAVAILABLE"
    assert response.json()["retryable"] is True
```

- [ ] **Step 2: Run the new API cases and confirm metadata assertions fail**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_api.py -q -k 'error_code or pool_unavailable'`

Expected: FAIL because controller error responses contain only `status` and `message`.

- [ ] **Step 3: Raise typed expected failures and map them centrally**

```python
except CamouflareError as exc:
    response = V1Response.error(
        str(exc), version=settings.version, start_timestamp=start_timestamp,
        error_code=exc.error_code, retryable=exc.retryable,
        request_outcome_unknown=exc.request_outcome_unknown,
        solution=exc.solution,
    )
    status_code = 503 if exc.error_code is V1ErrorCode.POOL_UNAVAILABLE else 500
```

Use `INVALID_REQUEST` for validation/command/URL/proxy errors, `SESSION_NOT_FOUND` for a missing destroy target, `RESOURCE_LIMIT_EXCEEDED` for payload limits, `POOL_UNAVAILABLE` for acquisition/persistent capacity, `REQUEST_TIMEOUT` for the hard controller deadline, and solver classifications for navigation, transport, and challenge failures.

- [ ] **Step 4: Run model, solver, and API coverage**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_models.py tests/test_solver.py tests/test_api.py -q`

Expected: PASS with all pre-existing HTTP status assertions unchanged.
