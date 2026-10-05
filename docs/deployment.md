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
a retired generation is closing. The hard physical ceiling is two complete generations,
so allow memory and PID headroom for up to another `POOL_MAX_BROWSERS` browser processes
during a recycle.

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

GHCR images are released from `vMAJOR.MINOR.PATCH` tags. Images contain linux/amd64 and
linux/arm64 manifests and publish attached BuildKit SBOM/provenance. Only the exact immutable
version tag is published; rolling `latest`, major, and major/minor tags are intentionally
omitted to prevent a release rerun from moving an established channel backward. The official
GHCR package is intended to be public; private mirrors require `docker login` before Compose
or direct pulls.

The release workflow pushes the multi-arch candidate by digest only; no temporary tag is
created. It smokes, revision-checks, and scans the exact per-platform digests and uploads
the release evidence first. The promotion step then re-confirms through the authenticated
GitHub API that the release tag still names the workflow commit and, in the same step,
tags the scanned index digest. A run that fails before that step leaves only an untagged
package version, which no tag references and which maintainers may delete from the GHCR
package settings.

Docker and release jobs fetch the exact Camoufox tag declared in
`scripts/camoufox-artifacts.json`, install only archives listed there, and verify each
download against its reviewed SHA-256 digest before extraction. Camoufox's default browser
addons are pinned in the same manifest: each `addons` entry names an exact version, a
versioned XPI URL, and a reviewed SHA-256 digest, and a download, digest, or embedded
version mismatch fails the fetch and therefore the image build. New upstream releases are
ignored until their exact tag and independently verified platform digests are reviewed and
updated in the manifest; an addon update likewise requires its version, URL, and digest to
be re-verified and changed together.

## Preparing the 2.0.2 cutover

The local 2.0.2 package and release wording are prepared separately from publication.
A prepared wheel or a changed Compose version is not evidence that its GHCR image
exists. Do not switch the live service to 2.0.2 until the release workflow has completed
and its actual index digest has been recorded in `docs/releases.md`.

1. Rotate the previously shared API token in the service secret store and every client.
   For Events backend, update `CAMOUFLARE_TOKEN` together with the service's
   `CAMOUFLARE_API_TOKEN`, then reload deployed client configuration/workers through
   its existing deploy procedure. Verify the old token is rejected; do not log either
   credential.
2. Review `CHANGELOG.md` and `.github/release-notes/v2.0.2.md`, then follow
   `docs/release-checklist.md` for CI, annotated-tag publication, protected-environment
   approval, platform smoke, security scanning, and provenance. Local Python 3.14
   checks do not replace the required Python 3.11–3.14 and container gates.
3. Preserve the current live image/environment for rollback. Deploy the published
   index digest using the commands in `docs/releases.md`, and confirm the running
   container's revision matches the release commit. Deploy the Events backend changes
   separately; the Camouflare image does not contain them.
4. Check authenticated `/ready` and `/diagnostics`, gzip JSON content, a known terminal
   block, an ordinary upstream HTTP error, and an uncommitted GET timeout. Confirm the
   backend does not turn non-retryable API errors or target 503s into pool backpressure.
   Observe cleanup and lifecycle behavior for the window required by the checklist.

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
