# Production deployment profile

Camouflare supports a single user and a single application worker. Sessions and browser
pool state are process-local, so multiple workers do not provide session consistency. For
separate users, run separate container instances.

## Network and authentication

- Keep the published port on loopback unless remote access is required.
- `CAMOUFLARE_API_TOKEN` is mandatory for every non-loopback bind and should come from a
  secret store, not an image or Compose file.
- The reusable `camouflare.asgi:app` import always requires a token, even if an external
  Uvicorn command supplies a loopback bind. Use the `camouflare` command or
  `python -m camouflare` for tokenless local development so the validated `HOST` is also
  the address passed to Uvicorn.
- Tokenless loopback mode requires both a loopback network peer and `Host` header,
  rejects cross-origin or cross-site browser metadata, and accepts `POST /v1` only with an
  `application/json` or `application/*+json` content type. These checks prevent a web
  page from reaching the local API through cross-site requests or DNS rebinding; they
  are not a substitute for a token on any remotely reachable deployment.
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
| Stop grace period | 45 seconds | Covers the 10-second request drain plus the 30-second app cleanup deadline |

The profile keeps two browser processes warm with one context per browser, for two
physical context slots. `POOL_RESERVED_TRANSIENT_CONTEXTS` withholds one slot from
sessions, so this profile supports one concurrent persistent session; once it exists, the
next `sessions.create` is rejected immediately with HTTP 503 `POOL_UNAVAILABLE` rather
than waiting, and `MAX_SESSIONS` only caps the registry above that limit. Every other
request, including a first `sessions.create` while both slots are busy, waits for a free
slot until the earlier of `POOL_ACQUIRE_TIMEOUT_MS` (10 seconds here, then the same 503)
and its own `maxTimeout` (then HTTP 500 `REQUEST_TIMEOUT`). `/ready` does not join that
queue; see [operational checks](#operational-checks) for its saturation behavior.
Add concurrent sessions by raising
`POOL_MAX_BROWSERS`, which the sizing rule below gates on a load test, or by setting
`POOL_RESERVED_TRANSIENT_CONTEXTS=0`; with no reservation, two idle sessions leave no
capacity for stateless requests until they expire, and `/ready` reports `saturated` for
that whole time.

A browser retired by `BROWSER_MAX_USES` or `BROWSER_MAX_AGE_MINUTES` is replaced in the
background after it closes. The maintenance tick (`POOL_MAINTENANCE_INTERVAL_SECONDS`,
15 seconds by default) also retires browsers that age out between requests. Request-driven
launches can overlap a closing generation; allow memory and PID headroom for up to
another `POOL_MAX_BROWSERS` browser processes during a recycle.

Keep contexts isolated within each browser. The published 1.3.3
[load evidence](benchmarks/README.md) completed 45 of 45 requests with this profile,
while sharing two concurrent contexts in one Camoufox process timed out two requests in
one of five four-client rounds of a single recorded run. Increase browser count only
after load testing it against the memory and PID limits: run the intended load with the
higher `POOL_MAX_BROWSERS` while sampling the container with `docker stats` for memory
and PIDs, and keep headroom for one additional complete browser generation during a
recycle. The project publishes no per-browser memory or PID figures, and
`scripts/benchmark_service.py` records service-side counters only, so container resources
must be observed separately.

The profile drops all Linux capabilities and enables `no-new-privileges`. Preserve those
controls when translating the deployment to another runtime. Increase memory or PID limits
only after load testing the configured pool and payload limits.

## Graceful shutdown

The image runs `dumb-init` in single-child mode, so a stop signal reaches only the Python
process; the Playwright driver and Xvfb stay available while the application closes its
browsers. Uvicorn stops accepting connections, gives in-flight requests up to 10 seconds to
finish, and cancels any still running. The application then closes sessions and browsers
within `SHUTDOWN_TIMEOUT_SECONDS` (30 seconds by default). When Python exits, `dumb-init`
exits with it and the container runtime terminates anything cleanup left behind. The
10-second drain is set by the `camouflare` command; an external
`uvicorn camouflare.asgi:app` command must pass `--timeout-graceful-shutdown`, because
Uvicorn otherwise waits for in-flight requests without limit before cleanup starts.

Keep the runtime's stop timeout above the 10-second drain plus `SHUTDOWN_TIMEOUT_SECONDS`,
or it kills the container before browser cleanup finishes. The Compose profile allows
45 seconds. `docker run` and `docker stop` default to 10 seconds, so pass
`--stop-timeout 45` or `docker stop --time 45`; on Kubernetes set
`terminationGracePeriodSeconds: 45` or higher. Raise the stop timeout by the same amount
whenever `SHUTDOWN_TIMEOUT_SECONDS` is raised.

## Version and architecture policy

Releases use exact `MAJOR.MINOR.PATCH` image tags for linux/amd64 and linux/arm64;
there are no rolling `latest`, major, or major/minor tags. Compose defaults to the public
Docker Hub image. GHCR also hosts the release history; [the release record](releases.md)
identifies which versions were mirrored to Docker Hub and records their immutable digests.

Mirrored versions have identical index and platform digests, including attached BuildKit
SBOM/provenance manifests. GitHub's additional provenance attestation remains on GHCR.
Images contain the Camoufox browser and add-ons pinned in `scripts/camoufox-artifacts.json`,
with downloads verified by SHA-256 before extraction. Private mirrors require `docker login`.

See the [release checklist](release-checklist.md) for publication, pins, and registry setup,
and [rollback](rollback.md#interrupted-publication-recovery) for interrupted publication.

## Deploy or upgrade

1. Set `CAMOUFLARE_API_TOKEN` in the service secret store and configure every client
   to send the matching token.
2. Review the [2.0 upgrade guide](upgrade-to-2.0.md) and [changelog](../CHANGELOG.md),
   especially authentication, custom-header GETs, POST redirects, and error metadata.
3. Preserve the current live image and environment for rollback. Deploy the published
   index digest using the commands in [the release record](releases.md), then confirm
   the running container's revision matches the release commit. Deploy client changes
   separately from the Camouflare image.
4. Check authenticated `/ready` and `/diagnostics`, compressed content, a known terminal
   block, an ordinary upstream HTTP error, and an uncommitted GET timeout against targets
   you control. Confirm clients distinguish non-retryable API errors and target 503s from
   pool backpressure. For release observation, follow the
   [post-publication checks](release-checklist.md#after-publication).

## Operational checks

Use `/health` for liveness, authenticated `/ready` for browser-backed readiness, and
authenticated `/diagnostics` for a passive snapshot that never leases a browser. `/ready`
probes by leasing, and if needed launching, a browser context, unless every context slot
is held by a browser that is still serving; then it returns HTTP 200 with
`capacity_state: saturated` without waiting, even while such a browser is past a recycle
limit and finishing its work. A browser whose process has disconnected is retired before
that verdict, so a dead busy browser is probed rather than reported as saturated. A 503
therefore means the pool could not produce a working browser within
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
