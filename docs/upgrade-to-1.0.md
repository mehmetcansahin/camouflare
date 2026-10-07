# Upgrading to Camouflare 1.0

Historical migration notes for 1.0. For current installation commands and container
settings, use the [README](../README.md) and [deployment guide](deployment.md). Also
review the [2.0 upgrade guide](upgrade-to-2.0.md) when upgrading from a pre-1.0 version
to the current release.

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

## Verification

Check `/health`, authenticated `/ready`, a representative `/v1` request, and session
creation/destruction using the current README examples. Observe memory and timeout
metrics under expected concurrency. If verification fails, use the
[rollback procedure](rollback.md).
