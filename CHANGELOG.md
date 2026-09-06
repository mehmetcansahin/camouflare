# Changelog

All notable changes to Camouflare are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Breaking:** non-`User-Agent` target `headers` on `request.get` now select a
  stateless direct HTTP transport that removes those headers before any cross-origin
  redirect. They are no longer applied to browser navigation, so JavaScript rendering,
  browser challenge handling, and screenshots do not apply to such requests, and the
  request is rejected with `INVALID_REQUEST` when it names a session, proxy, or
  screenshot. `Referer` is one of those headers; it is no longer passed to `page.goto`.
  `User-Agent` still configures the browser context.
- **Breaking:** `request.post` always uses the browser context request transport with
  automatic redirects disabled and returns the first response; the hidden-form document
  navigation is removed. `returnScreenshot` is rejected for POST, and every failure after
  the POST was sent reports `requestOutcomeUnknown: true` with `retryable: false`.
- Direct HTTP GET follows at most ten redirects under one shared deadline, returns
  non-2xx responses instead of raising (the `ajax=true` preflight and the
  navigation-timeout fallback still accept only 2xx), imports the cookies it received
  into the browser context, and merges them into `solution.cookies`. Collected cookies
  now also carry Selenium's `expiry` spelling.
- Request cookies are validated and normalized: `name`/`value` must be storable by a
  browser, `url` and `domain` are exclusive, a cookie with neither is scoped to the
  target URL, `expiry` maps to `expires`, `sameSite` is case-insensitive, public-suffix
  domains and invalid `__Secure-`/`__Host-` scopes are rejected, and unknown
  browser-export fields are ignored.
- Request `proxy` values are validated: only `http`, `https`, `socks4`, `socks5`, and
  `socks5h` servers, no credentials with SOCKS4, and SOCKS5 credentials of at most 255
  UTF-8 bytes. `url`, `headers`, and `userAgent` reject control characters.
- Tokenless mode accepts only loopback peers and `Host` values, rejects cross-origin and
  cross-site browser requests, and requires a JSON content type for `POST /v1`.
  `camouflare.asgi:app` now requires `CAMOUFLARE_API_TOKEN`; use `python -m camouflare`
  for tokenless local development.
- A session request no longer falls back to direct HTTP after a browser transport
  failure, a session whose context can no longer open a page is evicted, and popup
  pages opened during a request are closed with it.
- `sessions.create` beyond `MAX_SESSIONS` returns HTTP 503 with `POOL_UNAVAILABLE` and
  `retryable: true` instead of an internal error.
- Challenge clearance polling keeps one second of the request budget in reserve so the
  error envelope and solution can still be collected before `maxTimeout` expires.
- Browser pool startup is shared by concurrent callers, keeps launched browsers
  accounted for until they are registered, and caps live plus closing browser
  generations at twice `POOL_MAX_BROWSERS`. Expired-session pruning no longer waits for
  physical cleanup.
- `returnOnlyCookies` responses no longer read or serialize page content; direct HTTP
  reads only a bounded prefix to classify challenge interstitials.

### Added

- Direct HTTP requests run on a bounded worker executor (four workers) owned by the
  application runtime; it stops accepting work and drains during shutdown.
- `scripts/camoufox-artifacts.json` pins the Camoufox release tag and per-platform
  SHA-256 digests. `scripts/fetch_camoufox.py` fetches only that tag, installs only
  listed archives, and verifies each download before extraction; CI and release
  workflows resolve the pinned tag instead of the latest release.

### Fixed

- Log redaction now covers `socks4://` URLs.

### Security

- Direct HTTP cookie handling rejects public-suffix domains, `Domain` cookies from IP
  literals, `Secure` cookies received over HTTP, `SameSite=None` without `Secure`,
  malformed `__Secure-`/`__Host-` cookies, and `Partitioned` cookies.

## [1.4.0] - 2026-08-27

### Added

- `scripts/benchmark_service.py`, a reusable service benchmark runner that writes schema 2
  JSON evidence with a machine-readable acceptance block, plus load and lifecycle-canary
  evidence for the published 1.3.3 image. Metric-derived acceptance checks fail instead of
  passing vacuously when `/metrics` exposes no Camouflare counters.

- Background pool maintenance. A browser retired by `BROWSER_MAX_USES` or
  `BROWSER_MAX_AGE_MINUTES` is now replaced as soon as it closes (a request that is
  already waiting launches for itself instead, and the next tick fills any shortfall),
  and a periodic tick (`POOL_MAINTENANCE_INTERVAL_SECONDS`, default 15) retires idle
  browsers past their max age and relaunches to `POOL_MIN_BROWSERS` without waiting for
  a request. Background launches wait for closing browsers so the tick never runs more
  than `POOL_MAX_BROWSERS` processes.

### Changed

- Production Compose now limits each browser process to one concurrent context. A 1.3.3
  load run completed 45 of 45 requests with isolated contexts, while the previous
  two-context profile timed out two requests in one of five four-client rounds.
- The isolated-context profile lowers concurrent persistent sessions from three to one,
  because `POOL_RESERVED_TRANSIENT_CONTEXTS` withholds one of the two context slots from
  sessions. Raise `POOL_MAX_BROWSERS` after load testing memory and PID usage as
  described in `docs/deployment.md`, or set `POOL_RESERVED_TRANSIENT_CONTEXTS=0`, to
  restore session capacity.
- README positioning now defines Camouflare's deliberately narrow operational focus,
  tested FlareSolverr protocol surface, compatibility no-ops, and named-client
  verification status.
- `/documentation` now lists the pool context, reserved-context, acquire-timeout, and
  browser recycle settings, states the real concurrent-session limit next to
  `MAX_SESSIONS`, and documents the direct-HTTP GET preflight path.
- `/ready` no longer queues a probe context behind live requests. When every context
  slot is held by a browser that is still serving, including one past a recycle limit
  that is finishing its work, it returns HTTP 200 with `capacity_state: saturated`
  from the pool's own view, after first retiring any browser whose process has
  disconnected; the readiness metric records the probe as `saturated`, and a failure
  while taking that view maps to 503 like every other readiness failure. The OpenAPI
  schema declares the optional `capacity_state` and `message` fields of that body. A
  503 now means the pool could not produce a working browser.

## [1.3.3] - 2026-08-25

### Fixed

- Malformed JSON request bodies now return a stable `INVALID_REQUEST` envelope without
  being logged as unexpected internal failures.
- Release verification now rejects missing or stale changelog comparison links, and
  the 1.3.2 references point to the correct release ranges.

## [1.3.2] - 2026-08-25

### Fixed

- Camoufox launches are serialized, so two browsers can no longer be started onto the
  same virtual display. Camoufox resolves the display by mutating the shared process
  environment from a worker thread and by reading `/tmp` lock files without a lock of
  its own, so overlapping launches handed one Xvfb to two Firefox processes and the
  first of the pair to exit or fail killed the display under the other.
- The pool now checks browser liveness before reserving a context slot. A browser that
  died was previously only discovered when a request failed on it, so every request
  arriving in between was handed the same dead browser; a lease that failed after its
  context was created also returned that browser to the pool as healthy, because the
  context closed cleanly. Concurrent requests turned a single browser death into one
  failure per in-flight request.
- A browser whose physical close fails is now retried on a later acquisition, bounded
  and backed off, instead of only at shutdown. A wedged browser process previously
  survived for the life of the service while its slot kept `closing_slots` above zero.
- A renderer crash (`Page crashed`, `Target crashed`) is classified as a browser
  transport failure rather than an internal error, so a crashed GET can use the direct
  HTTP fallback and is reported as retryable, and a crashed POST reports an uncertain
  outcome. The browser itself is not retired, because a content-process crash does not
  imply the browser process died.
- A single waiting request now starts at most one browser launch. The create gate does
  not see how many requests are waiting, so any unrelated wake-up during an in-flight
  launch started another one, letting one request grow the pool to `POOL_MAX_BROWSERS`
  and consume a whole abandoned-launch generation.

## [1.3.1] - 2026-07-26

### Changed

- Production Compose, Docker example, and container smoke profiles now use a 1024 PID
  limit, preserving browser process and thread headroom for the two-browser pool.

### Fixed

- GET navigations that time out before `domcontentloaded` now accept only a committed
  HTTP(S) URL, preventing an untouched `about:blank` page from being returned as a
  successful empty solution.
- Playwright URL matcher callbacks are normalized across string and URL-object forms so
  timeout handling returns retryable timeout errors instead of an internal type error.

## [1.3.0] - 2026-07-22

### Added

- Optional machine-readable `/v1` error metadata for stable error codes, retryability,
  uncertain POST outcomes, and visible GET fallback use.
- Structured request-completion and browser-transport events, plus bounded Prometheus
  counters for `/v1` errors and browser transport failures.

### Changed

- Browser launches are now pool-owned so acquisition timeouts and caller cancellation do
  not cancel shared capacity creation, while waiters can immediately reuse released slots.
- Stateless GET transport fallback remains available and is now reported to consumers;
  POST requests are never automatically replayed or sent through direct HTTP fallback.
- The production Compose profile now emits JSON logs by default.

### Fixed

- Waiting requests could return a pool-unavailable response while reusable capacity had
  already been released and another browser launch was still pending.
- Browser transport and cleanup failures could obscure retry semantics or mask a valid
  solution after the business request had completed.

## [1.2.0] - 2026-07-17

### Added

- A token-protected, passive `/diagnostics` endpoint with browser-pool, session,
  cleanup, capacity-state, and guarded Playwright workaround status.
- Low-cardinality capacity, cleanup, readiness, acquire-timeout, and unhandled
  asyncio metrics, plus actionable pool-timeout log fields.

### Changed

- Browser slots now follow explicit ready, retiring, creating, and closing lifecycle
  states. Idle aged slots are replaced without pinning pool capacity, while active
  slots cross recycle limits softly and retire after their final lease.
- Request, readiness, cleanup, session reaping, and shutdown paths now use hard
  deadlines and runtime-owned tasks so caller cancellation cannot orphan capacity.
- `/ready` remains browser-backed but now has an independent 15-second total deadline;
  `/health` is now a minimal HTTP 200 process-liveness response, with browser capacity
  available from `/diagnostics` instead.
- The nightly real-browser soak now uses a five-minute, 100-request profile while
  preserving five measured browser recycle cycles.

### Fixed

- Idle max-age browsers could remain counted but permanently unusable, eventually
  producing `Timed out waiting for browser context capacity` with no active contexts.
- Cancelled session, context, captcha, proxy, and browser cleanup could leak resources
  or produce unhandled task/future errors.
- Playwright 1.61.0 protocol futures are cancelled during `_inner_send` cancellation
  when a guarded version-and-source fingerprint matches.

## [1.1.0] - 2026-07-16

### Added

- A non-invasive browser-pool snapshot in `/health`, covering browser slots,
  context usage, waiting requests, and configured capacity.

## [1.0.0] - 2026-07-11

### Added

- Deterministic real-Camoufox integration tests and a one-hour browser soak test.
- Configurable request, response, screenshot, solution, timeout, session, and shutdown limits.
- Request correlation, structured logging, pool/session snapshots, and low-cardinality metrics.
- GHCR release automation for linux/amd64 and linux/arm64 images with SBOMs and provenance.

### Changed

- Established the supported deployment model as a single-user, single-worker local service.
- Hardened cancellation, session expiry, shutdown cleanup, and POST body preservation.
- Split application, navigation, challenge, response, and lifecycle responsibilities into typed modules.
- Hardened the Compose profile with dropped capabilities, no-new-privileges, and resource limits.

### Security

- Default binding is loopback; a token is mandatory when binding to a non-loopback address.
- High and critical dependency or container findings block releases unless covered by a
  reasoned, time-bounded exception.

[Unreleased]: https://github.com/mehmetcansahin/camouflare/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.3.3...v1.4.0
[1.3.3]: https://github.com/mehmetcansahin/camouflare/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/mehmetcansahin/camouflare/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/mehmetcansahin/camouflare/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mehmetcansahin/camouflare/releases/tag/v1.0.0
