# Camouflare

Camouflare is a FlareSolverr-compatible `/v1` API backed by FastAPI and Camoufox.
It keeps browser processes warm in a bounded pool, creates isolated contexts for
stateless requests, and uses locked persistent contexts for FlareSolverr sessions.
Active Cloudflare interstitial and Turnstile handling is disabled by default. Set
`CHALLENGE_SOLVER=click` to opt in to clicking challenges through
[playwright-captcha](https://pypi.org/project/playwright-captcha/)'s ClickSolver.

Camouflare 1.0 targets a single-user, single-worker local service. Linux and
macOS source installs are supported; published Docker images target Linux
`amd64` and `arm64`. Windows and shared multi-tenant deployments are out of scope.

Use Camouflare only on systems you own, administer, or have permission to test.
You are responsible for following target-site terms of service and applicable
law. This project does not accept requests to bypass a specific third-party
site's access controls.

## Installation

Install the supported PyPI package on Linux or macOS, then fetch the Camoufox
browser runtime:

```bash
python -m pip install "camouflare==1.0.0"
camoufox fetch
playwright install-deps firefox  # Linux only
camouflare --version
camouflare
```

The official multi-architecture container is published from the same release
tag. A non-loopback container bind requires a token:

```bash
export CAMOUFLARE_API_TOKEN="$(openssl rand -hex 32)"
docker run --rm \
  --name camouflare \
  --publish 127.0.0.1:8191:8191 \
  --env CAMOUFLARE_API_TOKEN \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --shm-size 2g \
  --memory 4g \
  --pids-limit 512 \
  ghcr.io/mehmetcan/camouflare:1.0.0
```

## API

- `GET /` returns service metadata.
- `GET /documentation` serves expanded API documentation with commands,
  examples, response shapes, session/proxy notes, and configuration details.
- `GET /health` returns `{"status":"ok"}` without leasing a browser.
- `GET /ready` checks that the browser pool can create a page and evaluate JS.
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
| `CAMOUFOX_GEOIP` | `false` | Enable Camoufox IP geolocation discovery. Disabled by default so local and CI browser startup has no external-IP lookup dependency. |
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
docker build -t camouflare .
export CAMOUFLARE_API_TOKEN="$(openssl rand -hex 32)"
docker run --rm \
  --publish 127.0.0.1:8191:8191 \
  --env CAMOUFLARE_API_TOKEN \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --shm-size 2g \
  --memory 4g \
  --pids-limit 512 \
  camouflare
```

For Compose, set `CAMOUFLARE_API_TOKEN` before running `docker compose up`.
The example `compose.yaml` intentionally fails to start when this variable is
unset.

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
