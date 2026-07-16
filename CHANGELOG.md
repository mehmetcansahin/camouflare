# Changelog

All notable changes to Camouflare are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/mehmetcansahin/camouflare/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/mehmetcansahin/camouflare/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mehmetcansahin/camouflare/releases/tag/v1.0.0
