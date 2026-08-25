# Changelog

All notable changes to Camouflare are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/mehmetcansahin/camouflare/compare/v1.3.1...HEAD
[1.3.1]: https://github.com/mehmetcansahin/camouflare/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mehmetcansahin/camouflare/releases/tag/v1.0.0
