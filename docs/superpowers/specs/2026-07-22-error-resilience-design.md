# Error Resilience Design

**Date:** 2026-07-22

## Objective

Improve Camouflare's error contract, browser-pool acquisition behavior,
observability, and retry/fallback semantics without breaking existing `/v1`
consumers.

## Confirmed Decisions

- Preserve the current `/v1` HTTP status behavior for backward compatibility.
- Add optional machine-readable error metadata to error responses.
- Keep the existing stateless GET direct-HTTP fallback, but make its use visible
  in the response.
- Do not automatically retry or fall back for POST requests.
- Let consumers decide whether to retry failed business requests.
- Keep bounded browser/context cleanup recovery inside Camouflare because a
  consumer cannot release or quarantine Camouflare-owned resources.
- Do not convert a successful solve into a failed response solely because
  post-solve cleanup failed.

## Non-Goals

- Changing existing `/v1` HTTP status codes in the current API version.
- Adding automatic retries for failed GET or POST commands.
- Logging full URLs, query strings, cookies, tokens, request bodies, response
  bodies, proxy credentials, or other sensitive values.
- Adding target hostnames as Prometheus labels.
- Increasing pool capacity as a substitute for fixing acquisition behavior.

## Design 1: Backward-Compatible Error Contract

Add optional serialized fields to `V1Response`:

```json
{
  "status": "error",
  "message": "Browser transport closed.",
  "errorCode": "BROWSER_TRANSPORT_CLOSED",
  "retryable": true,
  "requestOutcomeUnknown": false
}
```

The fields are omitted when they do not apply, so successful responses and
existing error consumers retain their current shape.

Use a bounded error-code enum. The initial codes are:

- `INVALID_REQUEST`
- `SESSION_NOT_FOUND`
- `RESOURCE_LIMIT_EXCEEDED`
- `POOL_UNAVAILABLE`
- `REQUEST_TIMEOUT`
- `NAVIGATION_TIMEOUT`
- `BROWSER_TRANSPORT_CLOSED`
- `CHALLENGE_FAILED`
- `INTERNAL_ERROR`

Introduce a typed domain exception carrying:

```python
class CamouflareError(Exception):
    error_code: V1ErrorCode
    retryable: bool
    request_outcome_unknown: bool
    solution: Solution | None
```

Expected failures become `CamouflareError` instances. The `/v1` controller
maps them to the existing HTTP status behavior and existing response envelope.
Unexpected exceptions remain `INTERNAL_ERROR` and include a server-side
traceback in logs.

Partial solutions remain supported by carrying an optional `Solution` on the
domain exception.

## Design 2: Pool Acquisition Race

Browser creation must be owned by `BrowserPool`, not by the request that first
noticed missing capacity.

The acquisition loop will:

1. Check all ready and soft-retiring slots under the pool condition lock.
2. Start a bounded pool-owned browser creation task when additional browser
   capacity is allowed.
3. Wait on the shared pool condition rather than awaiting one specific browser
   creation task.
4. Re-evaluate every slot after a context release, browser creation completion,
   browser retirement, or browser close event.
5. Return as soon as any compatible slot becomes available.

A request timing out must not cancel a shared browser creation that can serve
other requests. Each browser launch has a separate pool-owned deadline and is
abandoned safely if it exceeds that deadline.

The regression scenario is:

1. Existing browser capacity is temporarily occupied.
2. A second browser launch starts and remains blocked.
3. Existing capacity is released before the acquisition deadline.
4. The waiting request acquires the released slot instead of returning 503.

## Design 3: Observability

Use the existing JSON formatter and redaction pipeline. Deployments should use:

```yaml
LOG_FORMAT: "json"
```

Emit one structured completion event for every `/v1` request with bounded or
sanitized fields:

- `command`
- `result`
- `http_status`
- `error_code`
- `retryable`
- `request_outcome_unknown`
- `duration_ms`
- `target_host`
- `fallback_used`

Expected domain failures are logged without tracebacks. Unexpected exceptions
are logged with `logger.exception` and `error_code=INTERNAL_ERROR`.

Browser transport events include:

- `phase`
- `error_type`
- `browser_state`
- `slot_uses`
- `slot_active_contexts`
- `retire_reason`
- `fallback_used`

Add bounded Prometheus counters:

```text
camouflare_v1_error_total{command,error_code}
camouflare_browser_transport_error_total{phase}
```

No metric may use URLs, target hostnames, request IDs, session IDs, exception
messages, or other unbounded labels.

## Design 4: Retry and Fallback Policy

Camouflare does not retry a completed or failed business command on behalf of
the consumer.

For stateless GET requests, preserve the existing browser-to-direct-HTTP
fallback. When that fallback is used, return the optional response field:

```json
{
  "fallbackUsed": true
}
```

The field is omitted when browser navigation succeeds normally. Existing
direct-HTTP-first behavior is not changed by this scope.

POST requests never receive an automatic request retry or direct-HTTP fallback.
When a POST transport failure leaves the target outcome uncertain, return:

```json
{
  "errorCode": "BROWSER_TRANSPORT_CLOSED",
  "retryable": false,
  "requestOutcomeUnknown": true
}
```

Internal cleanup recovery remains bounded and service-owned:

- Context and browser cleanup may be retried or completed in the background.
- Failed resources remain quarantined and are not returned to usable capacity.
- Cleanup failures produce logs and metrics.
- A cleanup failure after a valid solution does not replace that solution with
  an API error.

## Plan Boundaries and Order

Four implementation plans will be produced:

1. `error-contract`: error enum, typed domain failures, optional response
   metadata, controller mapping, and compatibility tests.
2. `pool-acquire-race`: pool-owned browser creation and acquisition regression
   coverage. This plan is independent and may execute in parallel with plan 1.
3. `observability`: structured completion/error events and bounded metrics. This
   consumes the error codes introduced by plan 1.
4. `retry-and-fallback-policy`: visible stateless GET fallback, explicit POST
   uncertainty semantics, and cleanup/request retry separation. This consumes
   the response metadata introduced by plan 1.

Recommended execution order is plan 1, plan 3, and plan 4, while plan 2 may run
independently after review.

## Test Strategy

- Preserve all existing HTTP status assertions to prove backward compatibility.
- Add serialization tests proving optional fields are omitted when unused.
- Add one API test per bounded error category.
- Add pool concurrency tests with deterministic events rather than sleeps.
- Add text and JSON log redaction tests for every new field.
- Add metric tests proving labels are bounded.
- Add GET fallback tests asserting `fallbackUsed=true` only after an actual
  browser-to-direct-HTTP transition.
- Add POST transport tests proving no retry/fallback occurs and uncertain outcome
  metadata is returned.
- Add cleanup tests proving a successful solve remains successful when cleanup
  fails and the failed resource is quarantined.

## Acceptance Criteria

- Existing `/v1` consumers continue receiving the same HTTP status codes.
- Error consumers can branch on stable `errorCode` values without parsing
  messages.
- A waiting acquire uses newly released capacity while another browser launch is
  still pending.
- Every `/v1` error category is observable in structured logs and bounded
  metrics.
- Stateless GET fallback is visible to the consumer.
- POST requests are never automatically retried or sent through direct HTTP.
- Cleanup recovery remains internal, bounded, observable, and unable to mask a
  valid solution.
