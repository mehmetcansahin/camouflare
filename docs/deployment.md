# Production deployment profile

Camouflare 1.0 supports a single user and a single application worker. Sessions and browser
pool state are process-local, so multiple workers do not provide session consistency. For
separate users, run separate container instances.

## Network and authentication

- Keep the published port on loopback unless remote access is required.
- `CAMOUFLARE_API_TOKEN` is mandatory for every non-loopback bind and should come from a
  secret store, not an image or Compose file.
- `/health` is intentionally unauthenticated and reports process liveness only. `/ready`,
  `/metrics`, documentation, and `/v1` require the configured token.
- Private and loopback target URLs remain available because local-network automation is an
  intentional use case. Restrict network egress at the container or host boundary when the
  target set is narrower.

## Container resources

The supplied Compose profile uses these starting limits:

| Resource | Default | Purpose |
| --- | ---: | --- |
| Memory | 4 GiB | Browser processes and response/screenshot buffers |
| Shared memory | 2 GiB | Browser stability under concurrent pages |
| PIDs | 512 | Limits runaway browser process trees |
| Stop grace period | 45 seconds | Exceeds the 30-second app cleanup deadline |

The profile drops all Linux capabilities and enables `no-new-privileges`. Preserve those
controls when translating the deployment to another runtime. Increase memory or PID limits
only after load testing the configured pool and payload limits.

## Version and architecture policy

PyPI packages and GHCR images are released from the same `vMAJOR.MINOR.PATCH` tag. Images
contain linux/amd64 and linux/arm64 manifests and publish attached BuildKit SBOM/provenance.
Only the exact immutable version tag is published; rolling `latest`, major, and major/minor
tags are intentionally omitted to prevent a release rerun from moving an established
channel backward. The official GHCR package is intended to be public; private mirrors
require `docker login` before Compose or direct pulls.

## Operational checks

Use `/health` for liveness and authenticated `/ready` for browser-backed readiness. Alert on
pool acquisition timeouts, browser create/recycle errors, session rejections, and sustained
growth in active contexts or process memory. Request IDs may be returned to callers, but
URLs, tokens, cookies, bodies, and proxy credentials must not be copied into operational
logs.
