# Benchmark results

Recorded results from ad-hoc load runs against a deployed Camouflare instance.
Each run is stored as one JSON file named `<date>-<environment>-results.json`.

These files are a record of what was measured, not a performance guarantee and
not a regression gate. Nothing in CI reads them. Numbers depend on the host, the
network path, the pool configuration, and the Camoufox build in use, so treat a
file as comparable only with runs made under the same conditions.

## File layout

| Key | Contents |
| --- | --- |
| `schema_version` | Layout version of the file. |
| `generated_at` | UTC timestamp of the run. |
| `service` | Base URL, target URL, Camouflare version, Playwright version. |
| `method` | Command, timeouts, rounds, exclusions, percentile method. |
| `diagnostics` | `/diagnostics` snapshot taken while idle before and after the run. |
| `aggregates` | One row per load level: request count, successes, timeouts, server errors, and client/internal latency percentiles. |
| `successful_latency_samples_ms` | Raw per-request samples behind the percentiles. |
| `concurrency_4_failures` | Every individual failure at the level that degraded, with its error string. |
| `prometheus_retest` | Focused rerun of the failing level with `PROMETHEUS_ENABLED=true`, including the counter delta across the run. |

`client_*` latency is measured by the load client and includes network transit.
`internal_*` latency is the value Camouflare reports in its own response. The
percentiles cover successful requests only, so a row with a low success rate has
percentiles drawn from a small sample.

`process_metrics` describes the Python API process. It does not include the
resource usage of the child browser processes.

## Runs

### 2026-08-11, remote deployment

`2026-08-11-remote-results.json`. Camouflare 1.3.1 behind
`camouflare.ariel.ondokuzon.app`, driven with `request.get` against the
service's own `/health` endpoint so target-site behavior does not enter the
measurement. `/diagnostics` reports the pool as 4 browsers by 4 contexts, so
16 context slots.

| Load | Requests | Successes | Timeouts | HTTP 500 | client p50 | client p95 |
| --- | --- | --- | --- | --- | --- | --- |
| Sequential baseline | 10 | 10 | 0 | 0 | 1184 ms | 1251 ms |
| 1 concurrent | 5 | 5 | 0 | 0 | 1250 ms | 1318 ms |
| 2 concurrent | 10 | 10 | 0 | 0 | 1144 ms | 1275 ms |
| 4 concurrent | 20 | 5 | 7 | 8 | 1763 ms | 3737 ms |

Serial and 2-concurrent load is stable at roughly one second per request. Four
concurrent requests degrade to a 25 percent success rate: 7 client timeouts and
8 HTTP 500 responses. The 500s report
`BrowserContext.new_page: Target page, context or browser has been closed` (6),
`Browser.new_context: Target page, context or browser has been closed` (1), and
`Page.goto: Page crashed` (1).

The `prometheus_retest` section reproduces the failure with metrics enabled and
attributes it to the browser process rather than to pool exhaustion: three
`browser_event_disconnected`, three `browser_transport_error_context_create`,
one `browser_recycle_disconnected`, and zero `request_timeout`. Capacity was well
above the offered load. The idle `/diagnostics` snapshots on both sides of the
run report one ready browser slot and three slots still in `closing`.
