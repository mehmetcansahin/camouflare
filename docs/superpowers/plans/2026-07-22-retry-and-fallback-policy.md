# Retry and Fallback Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make browser-to-direct GET fallback visible, prevent POST replay/fallback, and keep cleanup recovery internal without masking valid solutions.

**Architecture:** Mark only `RawResponse` objects produced after a failed browser navigation as fallback responses; propagate the marker into `V1Response`. POST uses exactly one request path and classifies transport uncertainty without retry. Existing pool and cleanup supervisors continue quarantining failed owned resources.

**Tech Stack:** Python asyncio, solver/navigation modules, Pydantic response serialization, pytest fakes

## Global Constraints

- Do not add automatic GET or POST retries.
- Preserve stateless browser-to-direct GET fallback and existing direct-HTTP-first behavior.
- `fallbackUsed` is omitted for browser success and direct-HTTP-first success.
- POST transport uncertainty is non-retryable and reports `requestOutcomeUnknown=true`.
- Cleanup failure after a valid solution cannot replace that solution.

---

### Task 1: Visible GET fallback provenance

**Files:**
- Modify: `camouflare/navigation.py`
- Modify: `camouflare/solver.py`
- Modify: `tests/test_solver.py`

**Interfaces:**
- Produces: `RawResponse.fallback_used: bool`.
- Produces: `V1Response.fallback_used=True` only after browser-to-direct transition.

- [ ] **Step 1: Add failing provenance assertions**

```python
assert browser_transport_fallback_result.fallback_used is True
assert direct_http_first_result.fallback_used is None
assert browser_success_result.fallback_used is None
```

- [ ] **Step 2: Run focused fallback tests and confirm the field is absent**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_solver.py -q -k 'fallback or direct_http_before'`

Expected: FAIL on `fallback_used` provenance assertions.

- [ ] **Step 3: Mark fallback responses at the transition and serialize provenance**

```python
response = await fetch_direct(url, request, timer)
response.fallback_used = True
return response
```

Direct-first helpers leave the default `False`; solver maps only true values to the optional response field.

- [ ] **Step 4: Run all solver tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_solver.py -q`

Expected: PASS.

### Task 2: POST single-attempt and uncertainty semantics

**Files:**
- Modify: `camouflare/navigation.py`
- Modify: `camouflare/solver.py`
- Modify: `tests/test_solver.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `V1ErrorCode.BROWSER_TRANSPORT_CLOSED`.
- Produces: no context-request-to-hidden-form replay and POST transport metadata `{retryable: false, requestOutcomeUnknown: true}`.

- [ ] **Step 1: Change the challenge fallback test into a failing no-replay contract**

```python
assert context.request.calls == 1
assert page.posted_form is None
assert result.status == "error"
```

Add a transport failure case asserting `errorCode == "BROWSER_TRANSPORT_CLOSED"`, `retryable is False`, and `requestOutcomeUnknown is True`.

- [ ] **Step 2: Run POST-focused tests and confirm the replay/metadata failures**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_solver.py tests/test_api.py -q -k post`

Expected: FAIL because challenge fallback submits twice and transport metadata is absent.

- [ ] **Step 3: Return the first POST response and classify uncertain transport failures**

```python
if response is not None:
    return response
```

When browser transport closes during POST navigation, return or raise `BROWSER_TRANSPORT_CLOSED` with `retryable=False` and `request_outcome_unknown=True`; never call direct HTTP.

- [ ] **Step 4: Run POST and compatibility tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_solver.py tests/test_api.py -q -k 'post or fallback or cleanup'`

Expected: PASS.

### Task 3: Cleanup quarantine remains non-masking and observable

**Files:**
- Modify: `tests/test_api.py`
- Modify: `tests/test_pool.py`

**Interfaces:**
- Consumes: existing `CleanupSupervisor` and retiring-slot accounting.
- Produces: regression coverage that successful response wins while the failed slot is excluded from capacity.

- [ ] **Step 1: Extend cleanup tests with response and quarantine assertions**

```python
assert response.status_code == 200
assert response.json()["status"] == "ok"
assert original_browser.closed is True
assert replacement_lease.browser is not original_browser
```

- [ ] **Step 2: Run cleanup-focused tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_api.py tests/test_pool.py tests/test_cleanup.py -q -k cleanup`

Expected: PASS if the existing quarantine behavior already meets the approved design; otherwise first FAIL identifies the missing boundary.

- [ ] **Step 3: Run the complete unit suite**

Run: `PYTHONPATH=. .venv/bin/pytest -q`

Expected: all non-browser unit tests pass; environment-restricted socket tests may require an approved unsandboxed run.
