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

## Run with Docker

```bash
export CAMOUFLARE_API_TOKEN="$(openssl rand -hex 32)"

docker run --detach --rm \
  --name camouflare \
  --publish 127.0.0.1:8191:8191 \
  --env CAMOUFLARE_API_TOKEN \
  --shm-size 2g \
  ghcr.io/mehmetcansahin/camouflare:1.3.2
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

## Install from source

Camouflare requires Python 3.11-3.14. It is not published to PyPI.

```bash
git clone https://github.com/mehmetcansahin/camouflare.git
cd camouflare
python -m venv .venv
. .venv/bin/activate
python -m pip install .
camoufox fetch
playwright install-deps firefox  # Linux only
camouflare
```

For development with `uv`:

```bash
uv sync --group dev
uv run camoufox fetch
uv run playwright install-deps firefox  # Linux only
uv run python -m camouflare
```

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

Other endpoints:

- `GET /` returns service metadata.
- `GET /documentation` serves the full API and configuration reference.
- `GET /health` returns process liveness only and does not read browser state.
- `GET /ready` checks that the browser pool can create a page and evaluate JS.
- `GET /diagnostics` returns a passive pool, session, and cleanup snapshot without
  leasing browser capacity.
- `GET /metrics` returns Prometheus metrics when `PROMETHEUS_ENABLED=true`.

Send either `Authorization: Bearer <token>` or `X-API-Token: <token>` on every
endpoint except `/health`. Binding `HOST` to a non-loopback address requires
`CAMOUFLARE_API_TOKEN`; loopback binds may run without one.

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
| `POOL_ACQUIRE_TIMEOUT_MS` | `30000` | Wait for free browser capacity |
| `SESSION_TTL_MINUTES` | `60` | Default session lifetime |
| `PROXY_URL` | unset | Default proxy URL |
| `PROMETHEUS_ENABLED` | `false` | Enable `/metrics` |
| `CHALLENGE_SOLVER` | `none` | Set to `click` to enable click-based handling |

See `/documentation` on a running instance for every option and request field.

Challenge handling is disabled by default. To enable the optional
[playwright-captcha](https://pypi.org/project/playwright-captcha/) ClickSolver:

```bash
CHALLENGE_SOLVER=click uv run python -m camouflare
```

## Deployment notes

Camouflare 1.x is designed for a single user and a single application worker.
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
