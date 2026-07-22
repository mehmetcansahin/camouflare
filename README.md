# Camouflare

Camouflare is a FlareSolverr-compatible `/v1` API backed by FastAPI and Camoufox.
It keeps browser processes warm in a bounded pool, creates isolated contexts for
stateless requests, and uses locked persistent contexts for FlareSolverr sessions.
Active Cloudflare interstitial and Turnstile handling is disabled by default. Set
`CHALLENGE_SOLVER=click` to opt in to clicking challenges through
[playwright-captcha](https://pypi.org/project/playwright-captcha/)'s ClickSolver.

## Why Camouflare?

[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) established the
widely used `/v1` integration contract, and
[Byparr](https://github.com/ThePhaseless/Byparr) provides a smaller FastAPI-based
alternative. Camouflare was developed to fill a different operational gap: keep
existing FlareSolverr clients while treating browser capacity as a bounded,
long-running resource. The goal is predictable local operation, isolated request
state, explicit backpressure, and enough diagnostics to understand why a request
or browser is unhealthy.

This is an architectural comparison, not a challenge-success benchmark. The table
reflects the documented behavior of
[FlareSolverr 3.5.0](https://github.com/FlareSolverr/FlareSolverr/tree/v3.5.0) and
[Byparr 2.1.0](https://github.com/ThePhaseless/Byparr/tree/v2.1.0); upstream projects
may change after those releases.

| Area | FlareSolverr 3.5.0 | Byparr 2.1.0 | Camouflare 1.x |
| --- | --- | --- | --- |
| Browser stack | Selenium, undetected-chromedriver, and Chrome | FastAPI with Camoufox through Playwright | FastAPI with Camoufox through Playwright |
| Stateless request lifecycle | Starts a new browser for each request | Opens a request-scoped browser and context | Leases a fresh, isolated context from a bounded pool of warm browser processes |
| Persistent sessions | Keeps a browser instance with explicit destruction and optional TTL rotation | Does not implement the FlareSolverr session commands | Keeps a locked persistent context, rotates it by TTL, and reaps expired sessions |
| `/v1` surface | Reference implementation for GET, POST, and session commands | GET-focused compatibility subset | GET, POST, and session commands with cookies, headers, screenshots, waits, and environment or per-request proxies |
| Active challenge handling | Core request behavior | Click handling is integrated into the request path | Disabled by default and enabled explicitly with `CHALLENGE_SOLVER=click` |

The design addresses several problems that show up in a long-running local service:

- **Browser startup cost and resource spikes.** Warm processes avoid paying the full
  browser-launch cost for every stateless request. Pool size and per-browser context
  limits prevent unrestricted browser fan-out; saturated requests wait for a bounded
  interval and then receive an explicit 503 error envelope.
- **State leakage versus useful persistence.** Stateless calls receive new browser
  contexts, so cookies and storage do not cross requests. Named sessions deliberately
  retain state, serialize access with a lock, and respect configurable capacity
  reserved for stateless work and readiness probes.
- **Stale and stuck resources.** Browsers are recycled by age and use count, sessions
  expire by TTL, and request, cleanup, reaping, readiness, and shutdown paths have hard
  deadlines. Cleanup remains tracked after caller cancellation so logical capacity is
  not silently pinned by an abandoned request.
- **Incomplete request semantics.** Camouflare handles FlareSolverr-style POST requests
  inside the browser, preserves raw JSON or form bodies and their content type, applies
  target headers before navigation, and supports user-agent, referer, cookie, proxy,
  wait, media-blocking, and screenshot options.
- **Opaque failures and unsafe defaults.** Separate liveness, readiness, diagnostics,
  and Prometheus endpoints distinguish a live API from a healthy or saturated browser
  pool. Request correlation, bounded payloads, a loopback default, and mandatory token
  authentication for non-loopback binds make unattended operation easier to inspect
  and safer to expose within a controlled network.

Camouflare is an alternative operating model, not a claim of complete feature parity
or a guaranteed bypass. Some deprecated FlareSolverr fields are intentional
compatibility no-ops, and challenge outcomes still depend on the target, network
reputation, proxy, and browser fingerprint.

Camouflare 1.x targets a single-user, single-worker local service. Linux and
macOS source installs are supported; release container builds target Linux
`amd64` and `arm64`. Windows and shared multi-tenant deployments are out of scope.

Use Camouflare only on systems you own, administer, or have permission to test.
You are responsible for following target-site terms of service and applicable
law. This project does not accept requests to bypass a specific third-party
site's access controls.

## Installation

Camouflare is installed from source or run from the immutable
`ghcr.io/mehmetcansahin/camouflare:1.3.0` image; it is not published to PyPI.
For a source installation, fetch the Camoufox browser runtime after installing the package:

```bash
git clone https://github.com/mehmetcansahin/camouflare.git
cd camouflare
python -m venv .venv
. .venv/bin/activate
python -m pip install .
camoufox fetch
playwright install-deps firefox  # Linux only
camouflare --version
camouflare
```

For a local container build, follow the [Docker](#docker) instructions below.

## API

- `GET /` returns service metadata.
- `GET /documentation` serves expanded API documentation with commands,
  examples, response shapes, session/proxy notes, and configuration details.
- `GET /health` returns process liveness only and does not read browser state.
- `GET /ready` checks that the browser pool can create a page and evaluate JS.
- `GET /diagnostics` returns a passive, token-protected pool/session/cleanup snapshot
  without leasing browser capacity. A successful snapshot returns HTTP 200 even when
  its `capacity_state` is `saturated`, `recovering`, or `unavailable`.
- `GET /metrics` returns Prometheus metrics when `PROMETHEUS_ENABLED=true`.
- `POST /v1` supports:
  - `sessions.create`
  - `sessions.list`
  - `sessions.destroy`
  - `request.get`
  - `request.post`

Supported request fields include `cmd`, `url`, `maxTimeout`, `session`,
`session_ttl_minutes`, `proxy`, `cookies`, `returnOnlyCookies`,
`returnScreenshot`, `waitInSeconds`, `disableMedia`, `postData`, `headers`,
and `userAgent`.
Deprecated or unsupported FlareSolverr fields such as `download`,
`returnRawHtml`, and `tabs_till_verify` are accepted as compatibility no-ops.

Headers are applied to the browser page before navigation. `User-Agent` is
handled as a browser context option via `userAgent` or `headers.User-Agent`;
`userAgent` takes precedence when both are supplied. `Referer`/`Referrer` is
passed to navigation as the browser referer.

`request.post` sends `postData` as URL-encoded form data by default. When the
target `Content-Type` header is `application/json` or another `+json` media type,
`postData` is sent as the raw request body.
The initial document request is converted to POST inside the browser, preserving
the exact body and content type while allowing JavaScript execution,
`waitInSeconds`, screenshots, and challenge handling to operate on the loaded page.

Example:

```bash
curl -L -X POST 'http://localhost:8191/v1' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  --data-raw '{
    "cmd": "request.get",
    "url": "https://example.com",
    "maxTimeout": 60000
  }'
```

When `CAMOUFLARE_API_TOKEN` is unset, no API token is required and Camouflare
only binds to loopback by default. Binding `HOST` to a non-loopback address
requires `CAMOUFLARE_API_TOKEN`. Send either `Authorization: Bearer <token>` or
`X-API-Token: <token>` on every endpoint except `/health`.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Bind host. A non-loopback value requires `CAMOUFLARE_API_TOKEN`. |
| `PORT` | `8191` | Bind port. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `CAMOUFLARE_API_TOKEN` | unset | API token required for every endpoint except `/health`; optional only for loopback binds. |
| `HEADLESS` | Linux: `virtual`; other OSes: `true` | Camoufox mode. Accepts `true`, `false`, or `virtual`; `virtual` uses Xvfb and is Linux-only. |
| `PROXY_URL` / `PROXY_SERVER` | unset | Default proxy URL. |
| `PROXY_USERNAME` | unset | Default proxy username. |
| `PROXY_PASSWORD` | unset | Default proxy password. |
| `POOL_MIN_BROWSERS` | `1` | Warm browser count on startup. |
| `POOL_MAX_BROWSERS` | `2` | Maximum browser processes. |
| `POOL_MAX_CONTEXTS_PER_BROWSER` | `1` | Per-browser context concurrency. |
| `POOL_RESERVED_TRANSIENT_CONTEXTS` | `1` | Context slots kept free for stateless requests and `/ready`; each live session holds one slot for its TTL, so the concurrent-session limit is `POOL_MAX_BROWSERS * POOL_MAX_CONTEXTS_PER_BROWSER - POOL_RESERVED_TRANSIENT_CONTEXTS`. Set to `0` to let sessions use the whole pool. |
| `POOL_ACQUIRE_TIMEOUT_MS` | `30000` | Maximum time a request waits for pool capacity before returning a 503 error envelope. |
| `MAX_SESSIONS` | `32` | Upper bound on session count (also capped by pool capacity above). |
| `SESSION_TTL_MINUTES` | `60` | Default session rotation TTL. |
| `BROWSER_MAX_USES` | `200` | Recycle browser after this many context leases. |
| `BROWSER_MAX_AGE_MINUTES` | `120` | Recycle browser after this age. |
| `MAX_REQUEST_BODY_BYTES` | `4194304` | Maximum JSON request body. Chunked bodies are counted while streaming. |
| `MAX_RESPONSE_BODY_BYTES` | `33554432` | Maximum returned HTML, JSON, XML, or text body. |
| `MAX_SCREENSHOT_BYTES` | `16777216` | Maximum raw PNG screenshot size before base64 encoding. |
| `MAX_SOLUTION_BYTES` | `67108864` | Maximum serialized FlareSolverr response envelope. |
| `MAX_TIMEOUT_MS` | `300000` | Upper bound accepted for `maxTimeout`. |
| `MAX_SESSION_TTL_MINUTES` | `1440` | Upper bound accepted for session TTL. |
| `SESSION_REAPER_INTERVAL_SECONDS` | `30` | Background expired-session cleanup interval. |
| `CLEANUP_TIMEOUT_SECONDS` | `10` | Shared hard deadline for a request or session cancellation unwind. Nested page, context, browser, captcha, and proxy cleanup uses the same absolute budget; logical capacity is released even if physical close ignores cancellation. |
| `READINESS_TIMEOUT_MS` | `15000` | Total hard deadline for the active `/ready` browser probe, including capacity acquisition and cleanup. |
| `SHUTDOWN_TIMEOUT_SECONDS` | `30` | Shared shutdown deadline for sessions and browsers. |
| `PROMETHEUS_ENABLED` | `false` | Enable `/metrics`. |
| `CHALLENGE_SOLVER` | `none` | Challenge solver. `none` disables active solving and returns the loaded page as-is. `click` opts in to active Cloudflare interstitial/Turnstile handling via playwright-captcha's ClickSolver (loads a Camoufox add-on at launch). |
| `LOG_FORMAT` | `text` | Log format: `text` or `json`. |

Limit violations return the existing HTTP 500 FlareSolverr error envelope.
Response bodies and screenshots are never silently truncated. Private and
loopback target URLs remain available because Camouflare is a trusted local tool.

## Proxy Usage

Camouflare already supports proxy configuration. Use `PROXY_URL` or
`PROXY_SERVER` for a service-wide default, with optional `PROXY_USERNAME` and
`PROXY_PASSWORD`. Per-request `proxy` overrides the environment default and
accepts either `url` or `server` plus optional credentials:

```json
{
  "cmd": "request.get",
  "url": "https://example.com",
  "proxy": {
    "url": "http://proxy.example:8080",
    "username": "user",
    "password": "pass"
  }
}
```

For persistent sessions, the proxy is fixed when the session context is created.
Later rotations preserve the session's existing proxy.

## Source Run

```bash
uv sync
uv run camoufox fetch
uv run playwright install-deps firefox  # Linux only
uv run camouflare --version
uv run python -m camouflare
```

To opt in to active challenge handling:

```bash
CHALLENGE_SOLVER=click uv run python -m camouflare
```

## Docker

```bash
docker build --tag camouflare:local .
export CAMOUFLARE_API_TOKEN="$(openssl rand -hex 32)"
docker run --rm \
  --publish 127.0.0.1:8191:8191 \
  --env CAMOUFLARE_API_TOKEN \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --shm-size 2g \
  --memory 4g \
  --pids-limit 512 \
  camouflare:local
```

For Compose, set `CAMOUFLARE_API_TOKEN` before running `docker compose pull` and
`docker compose up -d`. The example `compose.yaml` intentionally fails to start when this
variable is unset and defaults to the immutable `1.3.0` image. Its retained `build: .`
entry supports explicit local builds with `docker compose up --build`.

The Dockerfile pins Ubuntu 24.04, uses `dumb-init`, runs as a non-root user,
builds a single runtime image, and removes build-only tools after browser assets
are installed.

Do not expose Camouflare directly to the public internet. If you bind it outside
loopback, put it behind your own access control, network restrictions, and rate
limiting; the built-in API token is a basic application gate, not a full edge
security layer.

On Linux, the browser runtime requires a writable `/dev/shm`. The default Docker
shared-memory mount satisfies this; constrained OCI runtimes should mount a
writable shared-memory directory before starting the service.

## Development

```bash
uv sync --group dev
uv run python -m pytest tests -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python -m pytest --cov=camouflare --cov-report=term-missing tests -q
```

Deterministic real-browser tests use only a loopback target:

```bash
CAMOUFLARE_RUN_BROWSER_TESTS=1 uv run python -m pytest tests/integration -q
```

The manual browser smoke requires an explicit target and never defaults to a
third-party site:

```bash
SMOKE_URL=http://127.0.0.1:8000/ uv run camouflare-smoke
```

Live challenge handling still depends on target-site behavior, IP/proxy
reputation, and browser fingerprint quality. The test suite verifies service behavior,
compatibility envelopes, cleanup, pooling, and session isolation.
