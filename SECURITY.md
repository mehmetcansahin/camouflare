# Security Policy

## Supported Versions

Security fixes are handled on the latest public release and the current `main`
branch. Older releases may be fixed when the affected code is still supported.

## Reporting a Vulnerability

Please report security issues privately before opening a public issue. If the
repository has GitHub private vulnerability reporting enabled, use that channel.
Otherwise, contact the maintainer through the repository owner profile and avoid
posting exploit details publicly.

Useful reports include:

- Affected version or commit.
- A short impact summary.
- Minimal reproduction steps.
- Logs or traces with secrets removed.

Please do not include real third-party credentials, private proxy credentials, or
target-specific bypass instructions in reports.

## Scope

In scope:

- Bugs that expose local files, environment variables, proxy credentials, or
  browser session state.
- Server-side request forgery or unsafe URL handling.
- Cross-session data leakage.
- Denial-of-service issues in the browser pool or session manager.

Out of scope:

- Requests to bypass a specific third-party site's access controls.
- Reports that rely on attacking systems you do not own or have permission to
  test.
- Issues caused only by intentionally exposing Camouflare to untrusted callers
  without authentication, network controls, or rate limits.

## Deployment Guidance

Do not expose Camouflare directly to the public internet without your own access
control, network restrictions, and rate limiting. Treat proxy credentials,
session identifiers, returned cookies, screenshots, and HTML responses as
sensitive data.

`CAMOUFLARE_API_TOKEN` provides a basic application-level gate for Camouflare
endpoints, but it is not a replacement for a hardened edge. Public or shared
deployments should still use a reverse proxy, TLS, network allowlists, logging,
and rate limiting appropriate for the environment.

Camouflare binds to `127.0.0.1` by default and refuses to start on a non-loopback
`HOST` unless `CAMOUFLARE_API_TOKEN` is configured.

The supported 1.0 deployment model is one trusted user and one worker per
instance. Private and loopback target URLs are intentionally available. Do not
share a session namespace between mutually untrusted users; use a separate
container or instance for each trust boundary.

Before making a GitHub repository public, enable GitHub Private Vulnerability
Reporting in the repository security settings so sensitive reports can be sent
privately.
