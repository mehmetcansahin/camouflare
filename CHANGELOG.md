# Changelog

All notable changes to Camouflare are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/mehmetcansahin/camouflare/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mehmetcansahin/camouflare/releases/tag/v1.0.0
