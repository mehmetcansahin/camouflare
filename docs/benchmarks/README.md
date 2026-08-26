# Benchmark results

Recorded load and lifecycle runs. Schema 2 files are produced by
`scripts/benchmark_service.py`; the 2026-08-11 schema 1 file predates the runner
and is retained as legacy evidence.

`method.minimum_recycles` and `acceptance.minimum_recycles_observed` appear only when a
run passes `--minimum-recycles`, so the two 2026-08-25 profile files omit them and the
canary file carries them. Every other schema 2 key is present in every schema 2 file.

These files are records of what was measured, not performance guarantees or CI
gates. Numbers depend on the host, network path, pool configuration, and Camoufox
build. Compare files only when those conditions match.

## Reproducing a run

Run a deterministic HTTP target reachable from Docker Desktop:

```bash
python3 -m http.server 18194 --bind 0.0.0.0
```

Start Camouflare with the profile being measured, then run:

```bash
export CAMOUFLARE_API_TOKEN="<benchmark-only token>"

uv run python scripts/benchmark_service.py \
  --base-url http://127.0.0.1:8191 \
  --target-url http://host.docker.internal:18194/ \
  --output docs/benchmarks/<date>-<environment>-results.json \
  --expected-version 1.3.3 \
  --environment-label "<host, image platform, limits, and pool profile>" \
  --image "<immutable image digest>" \
  --require-metrics
```

The runner excludes warmup requests, waits for an idle pool around every round,
uses a 70-second client deadline around the 60-second API deadline, records raw
successful latencies and every failure, and returns nonzero unless all acceptance
checks pass. A pool that is still busy before a round aborts the run, because the
next measurement would be invalid; a pool that has not settled 60 seconds after the last
round is recorded as `acceptance.idle_after` false with its final snapshot, and the run
still writes its evidence and exits nonzero. Canary runs add `--minimum-recycles N` with
`N` of at least one. The committed canary used a shorter profile so that each browser
recycled several times:

```bash
  --sequential-requests 4 --concurrency 4 --rounds 3 --warmup-requests 4 \
  --minimum-recycles 3
```

The metric-derived acceptance checks are only ever reported as passing when both the
opening and the closing scrape returned Camouflare counters. `--require-metrics` turns a
404 or counter-free `/metrics` into a hard error; without it, that run records those
checks as false and still exits nonzero. Either way an unmeasured invariant is never
published as a pass.

## File layout

| Key | Contents |
| --- | --- |
| `schema_version` | Evidence layout version. |
| `generated_at` | UTC timestamp. |
| `environment` | Client platform, immutable image, resource limits, and pool profile. |
| `service` | Base URL, target URL, and Camouflare version. |
| `method` | Command, deadlines, load levels, rounds, exclusions, and idle gate. |
| `diagnostics` | Full idle `/diagnostics` snapshots before and after measured traffic. |
| `aggregates` | Success, error, and nearest-rank latency summary for each load. |
| `successful_latency_samples_ms` | Raw client and internal latency samples. |
| `failures` | Every failed request with load, round, status, duration, and error. |
| `prometheus_delta` | Counter changes across measured traffic. |
| `acceptance` | Machine-readable pass/fail decision for reliability invariants. |

The legacy schema 1 file uses `concurrency_4_failures` and
`prometheus_retest` instead of the schema 2 failure, metric, and acceptance fields.
`client_*` latency includes network transit. `internal_*` is calculated from the
timestamps in Camouflare's response. Percentiles include successful requests only.

## Runs

### 2026-08-25, published 1.3.3 image

All runs used the published linux/arm64 image at index digest
`sha256:82cf3cc28a6da5cb89cbbef820e6cfd158c61fa39e30479b5f4238e090213e42`
on Docker Desktop on an Apple M1 against a deterministic host-local HTTP target. The
two profile runs record the Compose limits of 4 GiB memory, 2 GiB shared memory, and
1024 PIDs in their environment label; the canary label records only its pool and
recycle settings.

#### Isolated-context Compose profile

`2026-08-25-compose-profile-results.json`. Two browsers, one context per browser.
All 45 measured requests completed, including the four-client load. Extra requests at
concurrency four waited for one of the two physical context slots; the slowest
successful sample was 2670 ms, and no acquire timeout was recorded.

| Load | Requests | Successes | HTTP 500 | client p50 | client p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sequential baseline | 10 | 10 | 0 | 827 ms | 908 ms |
| 1 concurrent | 5 | 5 | 0 | 772 ms | 829 ms |
| 2 concurrent | 10 | 10 | 0 | 1324 ms | 1925 ms |
| 4 concurrent | 20 | 20 | 0 | 1466 ms | 2612 ms |

There were zero acquire timeouts, unhandled asyncio events, browser transport
errors, or `/v1` errors. The final snapshot had no active, waiting, creating, or
closing work and no cleanup backlog.

#### Shared-context comparison

`2026-08-25-shared-context-profile-results.json`. The previous Compose setting
used two browsers with two contexts per browser. It completed 43 of 45 measured
requests; two requests in one four-client round reached the 60-second API deadline.
Prometheus recorded two `REQUEST_TIMEOUT` errors and one error-driven browser
recycle. The pool returned fully idle afterward, with no acquire timeout, unhandled
asyncio event, or browser transport error.

Scope of this evidence: one run, and both failures fall in round 3 of the five
four-client rounds, so four of five rounds were clean and the single
`browser_recycle_total{reason="error"}` is consistent with one incident hitting two
in-flight requests. That is enough to prefer the isolated-context profile, which was
also faster at the concurrency-four tail, but it is not a demonstration that the
shared-context profile fails repeatably. Production Compose therefore uses one context
per browser.

#### Lifecycle canary

`2026-08-25-compose-canary-results.json`. The isolated-context profile ran with a
one-minute browser max age and a two-use browser limit. All 16 measured requests
completed across sequential and four-client traffic. The run observed eight
complete `max_uses` browser recycle operations, zero request or transport errors,
zero acquire timeouts, zero unhandled asyncio events, and an idle final snapshot
with no cleanup backlog.

This run predates background browser replacement: a retired browser was relaunched
only by the next request, which is why the recorded `idle_before` snapshot shows zero
ready browsers and `capacity_state` `unavailable` on a two-browser pool. The runner's
idle gate now also waits for that replacement to finish, so a rerun will show the pool
back at full strength between rounds.

### 2026-08-11, remote deployment

`2026-08-11-remote-results.json`. Camouflare 1.3.1 behind
`camouflare.ariel.ondokuzon.app`, driven with `request.get` against the service's
own `/health` endpoint. The pool allowed four contexts in each of four browsers.

| Load | Requests | Successes | Timeouts | HTTP 500 | client p50 | client p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Sequential baseline | 10 | 10 | 0 | 0 | 1184 ms | 1251 ms |
| 1 concurrent | 5 | 5 | 0 | 0 | 1250 ms | 1318 ms |
| 2 concurrent | 10 | 10 | 0 | 0 | 1144 ms | 1275 ms |
| 4 concurrent | 20 | 5 | 7 | 8 | 1763 ms | 3737 ms |

The 1.3.1 run degraded to a 25 percent success rate at concurrency four through
browser disconnections, closed contexts, and one renderer crash. Version 1.3.3 no
longer reproduced those browser transport errors, but the single 2026-08-25 comparison
run still timed out two requests when a browser process shared concurrent contexts.
The isolated-context Compose profile is the accepted configuration.
