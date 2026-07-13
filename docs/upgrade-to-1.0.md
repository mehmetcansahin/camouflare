# Upgrading to Camouflare 1.0

Camouflare 1.0 formalizes the project as a single-user, single-worker local service. Linux
and macOS source installations are supported. Published containers support linux/amd64 and
linux/arm64; Windows is not supported.

## Before upgrading

1. Record the current image digest or installed package version and save the existing
   environment configuration.
2. Confirm that only one worker serves a given set of in-memory sessions.
3. Set `CAMOUFLARE_API_TOKEN` before using `HOST=0.0.0.0`, an interface address, or a
   non-loopback hostname. The service refuses an unauthenticated non-loopback bind.
4. Review the resource defaults below and increase them only for a measured workload.

| Setting | 1.0 default |
| --- | ---: |
| `MAX_REQUEST_BODY_BYTES` | 4 MiB |
| `MAX_RESPONSE_BODY_BYTES` | 32 MiB |
| `MAX_SCREENSHOT_BYTES` | 16 MiB |
| `MAX_SOLUTION_BYTES` | 64 MiB |
| `MAX_TIMEOUT_MS` | 300,000 |
| `MAX_SESSION_TTL_MINUTES` | 1,440 |
| `SESSION_REAPER_INTERVAL_SECONDS` | 30 |
| `SHUTDOWN_TIMEOUT_SECONDS` | 30 |

Limit violations continue to use the FlareSolverr-compatible HTTP 500 error envelope and
never return a truncated solution. Idle expired sessions are now closed by a background
reaper, including when no new requests arrive.

## Package upgrade

Install the exact release in a clean environment first, then switch the service:

```bash
python -m pip install --upgrade "camouflare==1.0.0"
camouflare --version
```

## Container upgrade

Prefer the immutable release tag or recorded digest. Compose requires an explicit token:

```bash
export CAMOUFLARE_API_TOKEN='replace-with-a-secret'
docker compose pull
docker compose up -d
```

The supplied Compose profile reserves 4 GiB of memory, 2 GiB of shared memory, and 512
process IDs. It also drops Linux capabilities and enables no-new-privileges. Tune resource
limits deliberately rather than removing the security options. It pulls
`ghcr.io/mehmetcan/camouflare:1.0.0` by default; set `CAMOUFLARE_IMAGE` to an immutable
digest for a pinned deployment. The retained `build: .` entry supports an explicit local
`docker compose build` without changing the production image default.

## Verification

```bash
curl --fail http://127.0.0.1:8191/health
curl --fail -H "Authorization: Bearer ${CAMOUFLARE_API_TOKEN}" \
  http://127.0.0.1:8191/ready
```

Then send one representative request to `/v1`, confirm session creation/destruction, and
observe memory and timeout metrics under expected concurrency. If verification fails, use
the [rollback procedure](rollback.md).
