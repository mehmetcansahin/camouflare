# Upgrading to Camouflare 2.0

Camouflare 2.0 changes how caller-supplied headers and `request.post` reach the target,
validates request cookies and proxies, and hardens tokenless local mode. Successful
`request.get` responses without custom headers are unchanged.

## Before upgrading

1. Record the current image digest and the existing environment configuration.
2. List the clients that send `headers` other than `User-Agent`, that use `request.post`,
   or that forward cookies exported from another browser. Each group is affected below.
3. If an external ASGI server imports `camouflare.asgi:app`, set `CAMOUFLARE_API_TOKEN`;
   the import now refuses to start without one. `python -m camouflare` still supports
   tokenless loopback development.

## Behavior changes

### `request.get` with custom headers

Non-`User-Agent` `headers` (including `Referer`) no longer configure the browser page.
A stateless, proxyless `request.get` carrying them is served by direct HTTP: the headers
are removed before any cross-origin redirect, and JavaScript rendering, browser challenge
handling, and screenshots do not apply. An unresolved challenge returns `CHALLENGE_FAILED`;
a confirmed Cloudflare-style hard block returns `TARGET_BLOCKED` without a solving attempt.
Ordinary target 403/503 responses still pass through in `solution.status`. With a `session`,
`proxy`, or `returnScreenshot`, the request is rejected with `INVALID_REQUEST`.

Migration: drop headers the target does not need, or solve the challenge first with a
header-less `request.get` and reuse the returned cookies. `userAgent` and
`headers.User-Agent` keep configuring the browser identity.

GET timing in 2.0.2: DOM readiness and commit grace each wait at most 15 seconds under
the original command deadline. Navigation leaves collection time and a bounded share
for an already-eligible `ajax=true` timeout fallback. Short or setup-delayed requests
keep a proportional reserve, and committed pages retain the original budget for
challenge handling. No session, proxy, screenshot, or POST transport is relaxed.

### `request.post`

Every POST uses the browser context request transport with automatic redirects disabled.
The first response is returned as-is, including `3xx` responses, and the request is never
replayed. The response is not loaded into a browser page, so JavaScript rendering, browser
challenge handling, and `returnScreenshot` do not apply; `returnScreenshot` is rejected.
Any failure after the POST was sent reports `requestOutcomeUnknown: true` with
`retryable: false`.

Migration: for challenge-protected endpoints, clear the challenge with a `request.get` in
a session, then POST in the same session so the persistent cookies are sent.

### Cookies

Request cookies are validated: `name` must be an HTTP token, `value` must be printable
ASCII without `;`, `url` and `domain` are mutually exclusive, and a cookie with neither is
scoped to the target URL. `expiry` (Selenium) maps to `expires`, `sameSite` is accepted
case-insensitively, public-suffix domains and invalid `__Secure-`/`__Host-` scopes are
rejected, and unknown browser-export fields are ignored. Collected cookies now also
include `expiry`.

### Proxies

`proxy.url` / `proxy.server` must use `http`, `https`, `socks4`, `socks5`, or `socks5h`.
SOCKS4 proxies cannot carry credentials and SOCKS5 credentials are limited to 255 UTF-8
bytes. Malformed values return `INVALID_REQUEST` instead of a browser launch error.

The same validation now applies to the default `PROXY_URL` / `PROXY_SERVER` at startup.
Check those settings before restarting: an invalid default proxy prevents the service
from starting rather than leaving `/ready` healthy while every command fails.

### Tokenless mode

Without `CAMOUFLARE_API_TOKEN` the service accepts only loopback peers and `Host` values,
rejects cross-origin and cross-site browser requests, and requires an `application/json`
or `application/*+json` content type on `POST /v1`. Reverse proxies in front of a
tokenless instance must be replaced by a token.

### Sessions and capacity

`sessions.create` beyond `MAX_SESSIONS` now returns HTTP 503 with `POOL_UNAVAILABLE` and
`retryable: true` instead of an internal error. A session request no longer falls back to
direct HTTP after a browser transport failure, and a session whose context can no longer
open a page is evicted. Plan memory and PID headroom for up to another
`POOL_MAX_BROWSERS` browser processes during a recycle; see `docs/deployment.md`.

## Container image

The 2.0.2 image is available on [Docker Hub](https://hub.docker.com/r/mehmetcansahin/camouflare)
for `linux/amd64` and `linux/arm64`:

```bash
docker pull mehmetcansahin/camouflare:2.0.2
```

The same image digest is also available on GHCR:

```bash
docker pull ghcr.io/mehmetcansahin/camouflare:2.0.2
```

The Camoufox archive inside the image is selected from `scripts/camoufox-artifacts.json`
and verified by SHA-256 before extraction.
