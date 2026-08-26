# Production deployment profile

Camouflare supports a single user and a single application worker. Sessions and browser
pool state are process-local, so multiple workers do not provide session consistency. For
separate users, run separate container instances.

## Network and authentication

- Keep the published port on loopback unless remote access is required.
- `CAMOUFLARE_API_TOKEN` is mandatory for every non-loopback bind and should come from a
  secret store, not an image or Compose file.
- `/health` is intentionally unauthenticated and reports only process liveness without
  reading browser state. `/ready`, `/diagnostics`, `/metrics`, documentation, and `/v1`
  require the configured token.
- Private and loopback target URLs remain available because local-network automation is an
  intentional use case. Restrict network egress at the container or host boundary when the
  target set is narrower.

## Container resources

The supplied Compose profile uses these starting limits:

| Resource | Default | Purpose |
| --- | ---: | --- |
| Memory | 4 GiB | Browser processes and response/screenshot buffers |
| Shared memory | 2 GiB | Browser stability under concurrent pages |
| PIDs | 1024 | Browser process and thread headroom within a bounded process tree |
| Stop grace period | 45 seconds | Exceeds the 30-second app cleanup deadline |

The profile keeps two browser processes warm with one context per browser, for two
physical context slots. `POOL_RESERVED_TRANSIENT_CONTEXTS` withholds one slot from
sessions, so this profile supports one concurrent persistent session; once it exists, the
next `sessions.create` is rejected immediately with HTTP 503 `POOL_UNAVAILABLE` rather
than waiting, and `MAX_SESSIONS` only caps the registry above that limit. Every other
request, including a first `sessions.create` while both slots are busy, waits for a free
slot until the earlier of `POOL_ACQUIRE_TIMEOUT_MS` (10 seconds here, then the same 503)
and its own `maxTimeout` (then HTTP 500 `REQUEST_TIMEOUT`). `/ready` does not join that
queue: while both slots are busy on a healthy pool it returns HTTP 200 with
`capacity_state: saturated` from the passive snapshot. Add concurrent sessions by raising
`POOL_MAX_BROWSERS`, which the sizing rule below gates on a load test, or by setting
`POOL_RESERVED_TRANSIENT_CONTEXTS=0`; with no reservation, two idle sessions leave no
capacity for stateless requests until they expire, and `/ready` reports `saturated` for
that whole time.

A browser retired by `BROWSER_MAX_USES` or `BROWSER_MAX_AGE_MINUTES` is replaced in the
background as soon as it closes, so the pool returns to `POOL_MIN_BROWSERS` warm browsers
without waiting for the next request; if a request is already waiting, that request's own
launch replaces it and the next tick fills any remaining shortfall. The periodic tick
(`POOL_MAINTENANCE_INTERVAL_SECONDS`, 15 seconds by default) also retires browsers that
age out between requests. Background launches wait for closing browsers, so the tick never
runs more than `POOL_MAX_BROWSERS` processes; a request-driven launch may still start while
a retired browser is closing, so allow memory for one extra browser process during a
recycle.

Keep contexts isolated within each browser. The published 1.3.3
[load evidence](benchmarks/README.md) completed 45 of 45 requests with this profile,
while sharing two concurrent contexts in one Camoufox process timed out two requests in
one of five four-client rounds of a single recorded run. Increase browser count only
after load testing it against the memory and PID limits: run the intended load with the
higher `POOL_MAX_BROWSERS` while sampling the container with `docker stats` for memory
and PIDs, and keep headroom for one extra browser process during a recycle. The project
publishes no per-browser memory or PID figures, and `scripts/benchmark_service.py`
records service-side counters only, so container resources must be observed separately.

The profile drops all Linux capabilities and enables `no-new-privileges`. Preserve those
controls when translating the deployment to another runtime. Increase memory or PID limits
only after load testing the configured pool and payload limits.

## Version and architecture policy

GHCR images are released from `vMAJOR.MINOR.PATCH` tags. Images contain linux/amd64 and
linux/arm64 manifests and publish attached BuildKit SBOM/provenance. Only the exact immutable
version tag is published; rolling `latest`, major, and major/minor tags are intentionally
omitted to prevent a release rerun from moving an established channel backward. The official
GHCR package is intended to be public; private mirrors require `docker login` before Compose
or direct pulls.

## Operational checks

Use `/health` for liveness, authenticated `/ready` for browser-backed readiness, and
authenticated `/diagnostics` for a passive snapshot that never leases a browser. `/ready`
probes by leasing, and if needed launching, a browser context, unless every context slot
is held by a browser that is still serving; then it returns HTTP 200 with
`capacity_state: saturated` without waiting, even while such a browser is past a recycle
limit and finishing its work. A browser whose process has disconnected is retired before
that verdict, so a
dead busy browser is probed rather than reported as saturated. A 503 therefore means the
pool could not produce a working browser within
`POOL_ACQUIRE_TIMEOUT_MS` and `READINESS_TIMEOUT_MS`, not that it is merely full. Give
external probes a client timeout above `READINESS_TIMEOUT_MS`. Diagnostics returns HTTP 200
when the snapshot succeeds; alert from `capacity_state` and its counters, not from the
endpoint status alone.

Enable Prometheus in production. Alert on two consecutive readiness failures,
`active_contexts == 0` with `usable_context_slots == 0`, any cleanup timeout, and sustained
browser-process growth. Canary releases should use a one-minute browser max age and a low
max-use limit for at least three lifecycle cycles, then remain under observation for one full
production max-age window. Request IDs may be returned to callers, but URLs, tokens, cookies,
bodies, session ids, and proxy credentials must not be copied into operational logs or
diagnostics.
