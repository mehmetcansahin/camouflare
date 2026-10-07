# Camouflare

[![CI](https://github.com/mehmetcansahin/camouflare/actions/workflows/ci.yml/badge.svg)](https://github.com/mehmetcansahin/camouflare/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/tag/mehmetcansahin/camouflare?sort=semver&label=release)](https://github.com/mehmetcansahin/camouflare/tags)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Camouflare is a FlareSolverr-compatible `/v1` service powered by Camoufox. It keeps a
small browser pool running and supports both isolated requests and persistent sessions.

- FlareSolverr-style GET, POST, and session commands
- Configurable browser and context limits
- Per-request cookies, headers, proxy, screenshots, and wait time
- Health, readiness, diagnostics, and optional Prometheus endpoints

Camouflare does not guarantee access to third-party sites or successful challenge
handling. Use Camouflare only on systems you own, administer, or have permission to
test. This project does not accept requests to bypass a specific third-party site's
access controls.

## Why Camouflare

Camouflare pairs a Camoufox runtime with the familiar FlareSolverr `/v1` API, but
its main focus is predictable operation:

- **Warm, bounded capacity.** Browser processes launch at startup, are replaced in
  the background when a recycle limit retires them, and stay warm between requests,
  while hard browser, context, session, payload, and timeout limits prevent unbounded
  growth.
- **Failure-aware responses.** Stable error codes report retryability, uncertain POST
  outcomes, and visible GET fallback use instead of reducing every failure to an opaque
  internal error. When no proxy is configured, GET can fall back to direct HTTP after a
  browser transport failure, and an `ajax=true` GET that asks for no cookies, wait time,
  or screenshot is tried over direct HTTP first; that preflight response omits
  `fallbackUsed` because no navigation was attempted. POST uses the browser context's
  request transport with automatic redirects disabled, so it is never replayed.
- **Self-healing lifecycle.** Browser liveness is checked before leasing capacity;
  unhealthy, old, or overused browsers retire through bounded cleanup that survives
  caller cancellation.
- **Useful production signals.** Browser-backed readiness, passive diagnostics,
  structured request logs, and low-cardinality Prometheus metrics expose pool,
  session, cleanup, timeout, and browser transport state.
- **Measured release profile.** The published 1.3.3 image completed all 45 load requests,
  and the published 2.0.0 image completed all 16 lifecycle-canary requests, including
  eight browser recycle cycles, with no cleanup backlog in the accepted profile.
  These runs used deterministic local targets and do not measure challenge success.
  See the [recorded evidence](docs/benchmarks/README.md) for versions, environments,
  and the scope of each measurement. A separate macOS source run on 2.0.2 also
  completed 45 load requests and 16 canary requests with eight browser recycle
  cycles; it does not measure Docker resource limits or third-party integrations.
- **Hardened, verifiable delivery.** The container runs as a non-root user; Compose
  binds to loopback, requires a token, drops Linux capabilities, and applies memory,
  shared-memory, and PID limits. The Camoufox browser archive and its default uBlock
  Origin add-on are pinned in a reviewed manifest and verified by SHA-256 before
  extraction. Multi-architecture releases include SBOM and provenance evidence.
- **Small deployment surface.** One container and no external state service keep
  setup simple. The tradeoff is explicit: one trusted user, one worker, and no shared
  sessions across replicas.

## FlareSolverr compatibility

Camouflare supports `request.get`, `request.post`, `sessions.create`,
`sessions.list`, and `sessions.destroy`, including the common proxy, cookie, header,
wait, screenshot, and persistent-session fields. It preserves the familiar response
envelope and adds optional machine-readable error metadata.

`download`, `returnRawHtml`, and `tabs_till_verify` are accepted but ignored.
Unknown fields are also ignored for compatibility.

No named third-party client integration is currently part of CI, so the project
does not claim verified drop-in compatibility with Prowlarr, Jackett, Sonarr, or
similar clients. See `/documentation` for the full field and behavior reference.

Before connecting a client, check that it supports the required authentication and
the GET/POST transport rules described below. The supplied Docker and Compose setup
requires `Authorization: Bearer <token>` or `X-API-Token: <token>` on `/v1`; configuring
only a service URL is insufficient for a client that cannot send either header.

## Run with Docker

```bash
export CAMOUFLARE_API_TOKEN="$(openssl rand -hex 32)"

docker run --detach --rm \
  --name camouflare \
  --publish 127.0.0.1:8191:8191 \
  --env CAMOUFLARE_API_TOKEN \
  --shm-size 2g \
  ghcr.io/mehmetcansahin/camouflare:2.0.2
```

Check that the service is ready:

```bash
curl --fail \
  --header "Authorization: Bearer ${CAMOUFLARE_API_TOKEN}" \
  http://127.0.0.1:8191/ready
```

Send a request:

```bash
curl --request POST http://127.0.0.1:8191/v1 \
  --header 'Content-Type: application/json' \
  --header "Authorization: Bearer ${CAMOUFLARE_API_TOKEN}" \
  --data '{
    "cmd": "request.get",
    "url": "https://example.com",
    "maxTimeout": 60000
  }'
```

To use Compose:

```bash
export CAMOUFLARE_API_TOKEN="$(openssl rand -hex 32)"
docker compose up -d
```

`compose.yaml` pins the same image and refuses to start when `CAMOUFLARE_API_TOKEN`
is unset. Use `docker compose up --build` to build the image locally instead of
pulling it.

The same 2.0.2 release is available on [Docker Hub](https://hub.docker.com/r/mehmetcansahin/camouflare)
as `mehmetcansahin/camouflare:2.0.2`. Use that image in `docker run`, or set
`CAMOUFLARE_IMAGE=docker.io/mehmetcansahin/camouflare:2.0.2` for Compose. The
release workflow copies the verified GHCR index, including both architectures and
the attached BuildKit SBOM/provenance, and checks that its digest is identical.
See the [Docker Hub setup and backfill instructions](docs/release-checklist.md#docker-hub).

The production profile keeps two warm browser processes with one isolated context
per browser, so two requests run at a time. `POOL_RESERVED_TRANSIENT_CONTEXTS` holds one
of those two slots for stateless traffic, which leaves room for exactly one concurrent
persistent session: once that session exists, the next `sessions.create` is rejected
immediately with HTTP 503 `POOL_UNAVAILABLE` instead of waiting, and `MAX_SESSIONS` only
caps the registry above that limit. Any other request, including a first
`sessions.create` while both slots are busy, waits for a free slot until the earlier of
`POOL_ACQUIRE_TIMEOUT_MS` (10 seconds in this profile, then the same 503) and its own
`maxTimeout` (then HTTP 500 `REQUEST_TIMEOUT`). To run more sessions at once, raise
`POOL_MAX_BROWSERS` only after measuring container memory and PID usage under your
intended load (for example with `docker stats`), because the project publishes no
per-browser figures. Alternatively set `POOL_RESERVED_TRANSIENT_CONTEXTS=0`; with no
reservation, two idle sessions leave nothing for stateless requests until they expire, and
`/ready` reports `saturated` for that whole time. Do not increase contexts per browser without
repeating the
[load evidence](docs/benchmarks/README.md) for that profile.

## Install from source

Camouflare requires Python 3.11-3.14 and a git checkout: the browser fetch script and
its pins (`scripts/fetch_camoufox.py` and `scripts/camoufox-artifacts.json`) are not part
of the installed package. It is not published to PyPI.

```bash
git clone https://github.com/mehmetcansahin/camouflare.git
cd camouflare
python -m venv .venv
. .venv/bin/activate
python -m pip install .
python scripts/fetch_camoufox.py
playwright install-deps firefox  # Linux only
camouflare
```

For development with the locked dependency set that CI uses (`pip install .` resolves
the ranges in `pyproject.toml` instead):

```bash
uv sync --frozen --group dev
uv run python scripts/fetch_camoufox.py
uv run playwright install-deps firefox  # Linux only
uv run python -m camouflare
```

`scripts/fetch_camoufox.py` installs the Camoufox release and uBlock Origin add-on
pinned in `scripts/camoufox-artifacts.json`, the same ones CI and the container image
use, verifies every download by SHA-256 before extracting it, and exits non-zero on any
failure. Pinned browser archives exist for Linux and macOS on x86_64 and arm64, and for
Windows on x86_64 and i686. The script needs HTTPS access to `api.github.com`,
`github.com`, `release-assets.githubusercontent.com`, and `addons.mozilla.org`. It reads
the pinned release's metadata from the GitHub API anonymously, which is rate limited; to
avoid that, save the metadata with the GitHub CLI and pass it in:

```bash
tag="$(python -c 'import json; print(json.load(open("scripts/camoufox-artifacts.json"))["release_tag"])')"
gh api "repos/daijro/camoufox/releases/tags/${tag}" > camoufox-release.json
CAMOUFLARE_CAMOUFOX_RELEASES_FILE=camoufox-release.json python scripts/fetch_camoufox.py
```

Camouflare never downloads the browser or its add-ons when it launches a browser: if
either is missing, the launch fails with an error that points to this script. Do not use
`camoufox fetch`: it installs the newest browser release the `camoufox` library supports
and the latest add-on without verifying either, and a failed add-on download leaves an
empty add-on directory.

## API

`POST /v1` supports:

- `request.get`
- `request.post`
- `sessions.create`
- `sessions.list`
- `sessions.destroy`

Common request fields are `url`, `maxTimeout`, `session`, `proxy`, `cookies`,
`headers`, `userAgent`, `postData`, `waitInSeconds`, `disableMedia`,
`returnOnlyCookies`, and `returnScreenshot`.

Caller-supplied non-`User-Agent` headers select transports with enforceable redirect
boundaries. A `request.get` carrying them uses direct HTTP, strips them before any
cross-origin redirect, and is supported only for stateless requests without a proxy or
screenshots. A `request.post` uses the browser context request transport and returns a
redirect response without following it. These transport paths return the buffered HTTP
response rather than a JavaScript-rendered page, so browser challenge handling does not
apply. `User-Agent` is the compatibility exception: whether supplied through `headers`
or `userAgent`, it defines browser identity for the whole context.

Direct HTTP GET decodes `gzip` and `deflate` before charset conversion, including
concatenated gzip members and up to four stacked encodings. The response byte limit
applies to the wire body and every decoded layer under the same request deadline.
Unsupported, malformed, or truncated encodings return `RESPONSE_DECODE_ERROR`;
oversized bodies return `RESOURCE_LIMIT_EXCEEDED`. Cookies-only GET inspects only a
bounded 64 KiB decoded prefix, without validating the unread remainder.

GET navigation shares the command's absolute `maxTimeout` with setup, challenge handling,
and collection. DOM readiness and commit grace each wait at most 15 seconds. Navigation
keeps up to one second for collection (at most half the remaining time for short budgets),
and eligible `ajax=true` timeout fallback keeps up to 15 seconds or half the navigation
budget left after preflight. These stage caps are not a fresh timeout: direct HTTP workers
use the shortened deadline too. An already-committed page remains on the browser path
with the original request budget for challenge clearance; sessions, proxies, screenshots,
and POST do not gain new direct HTTP eligibility.

Confirmed Cloudflare-style bot/WAF block documents return `TARGET_BLOCKED` with
`retryable: false`, without invoking the challenge solver. An unresolved challenge
still returns `CHALLENGE_FAILED`. HTTP 403/503 alone is not a block: ordinary target
errors remain successful fetches with the upstream status in `solution.status`.
`request.post` with `returnOnlyCookies` does not read its response body, so that mode
cannot classify body-based challenges or blocks.

Other endpoints:

- `GET /` returns service metadata.
- `GET /documentation` serves the full API and configuration reference.
- `GET /health` returns process liveness only and does not read browser state.
- `GET /ready` checks that the browser pool can create a page and evaluate JS. When
  every context slot is held by a browser that is still serving it returns 200 with
  `capacity_state: saturated` instead of queueing a probe behind live requests.
- `GET /diagnostics` returns a passive pool, session, and cleanup snapshot without
  leasing browser capacity.
- `GET /metrics` returns Prometheus metrics when `PROMETHEUS_ENABLED=true`.

Send either `Authorization: Bearer <token>` or `X-API-Token: <token>` on every
endpoint except `/health`. Binding `HOST` to a non-loopback address requires
`CAMOUFLARE_API_TOKEN`; loopback binds may run without one. Tokenless loopback mode
accepts only loopback peers and `Host` values, rejects cross-origin browser requests,
and requires an `application/json` or `application/*+json` content type on `POST /v1`.

Use the `camouflare` command or `python -m camouflare` for tokenless local development.
The reusable `camouflare.asgi:app` entry point always requires
`CAMOUFLARE_API_TOKEN`, because an external ASGI server can override the configured
bind address.

## Configuration

The most commonly used environment variables are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `8191` | Bind port |
| `CAMOUFLARE_API_TOKEN` | unset | API authentication token |
| `HEADLESS` | platform-dependent | `true`, `false`, or Linux-only `virtual` |
| `POOL_MIN_BROWSERS` | `1` | Browsers started on launch |
| `POOL_MAX_BROWSERS` | `2` | Maximum browser processes |
| `POOL_MAX_CONTEXTS_PER_BROWSER` | `1` | Concurrent contexts per browser |
| `POOL_RESERVED_TRANSIENT_CONTEXTS` | `1` | Context slots withheld from sessions |
| `POOL_ACQUIRE_TIMEOUT_MS` | `30000` | Wait for free capacity before HTTP 503 |
| `MAX_SESSIONS` | `32` | Session registry cap, above the context limit |
| `SESSION_TTL_MINUTES` | `60` | Default session lifetime |
| `PROXY_URL` | unset | Default proxy URL |
| `PROMETHEUS_ENABLED` | `false` | Enable `/metrics` |
| `CHALLENGE_SOLVER` | `none` | Set to `click` to enable click-based handling |
| `LOG_FORMAT` | `text` | `text` or `json`; both redact messages and structured fields |

See `/documentation` on a running instance for every option and request field.

Text logs append compact JSON as `fields={...}` when structured fields are present.
Request completion records expose `result`, `http_status`, `duration_ms`, and an
`error_code` when applicable. JSON logs retain the same redacted fields under `fields`;
URLs lose credentials, paths, and queries, and sensitive values are masked.

Active click-based challenge handling is disabled by default. Browser-side challenges
may still clear on their own within the request deadline; success depends on the target.
To enable the optional
[playwright-captcha](https://pypi.org/project/playwright-captcha/) ClickSolver:

```bash
CHALLENGE_SOLVER=click uv run python -m camouflare
```

## Deployment notes

Camouflare is designed for a single trusted user and a single application worker.
Browser and session state is kept in the process, so multiple workers do not share
sessions.

Do not expose the service directly to the public internet. For non-loopback use,
add network restrictions, access control, and rate limiting around it.

See [the deployment guide](docs/deployment.md) for container sizing, monitoring,
and production checks.

## Development

```bash
uv run python -m pytest tests -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Run the browser integration tests with:

```bash
CAMOUFLARE_RUN_BROWSER_TESTS=1 uv run python -m pytest tests/integration -q
```
