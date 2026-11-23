# FastAPI test and benchmark guide

This guide applies to the Python rebuild. Legacy Node scripts and historical QA reports do not validate this backend. The original defects are recorded in PROJECT_AUDIT.md.

## 1. Install and run automated checks

From the repository root, in a Python 3.11+ virtual environment:

```powershell
python -m pip install -e ".[test]"
python -m pytest
```

Tests use isolated temporary SQLite databases and do not require a server. Coverage includes the complete `tuneio` Python package; no business modules are excluded. Statement and branch coverage are reported, and combined coverage below 90% fails the command. The GitHub Actions workflow runs the standard command on Python 3.12/Linux; that hosted CI job has not been run from this workspace.

The managed Windows execution sandbox blocks localhost socket connections (verified WinError 10013). Python 3.12 on Windows creates a TCP self-pipe when starting its normal event loop, which hangs under that restriction. For this environment only:

```powershell
python scripts/test_socketless.py
```

This runs the same suite through HTTPX ASGITransport, with event-loop wakeups replaced by short polling. It preserves application request processing, thread dispatch, locks, cancellation and HTTP validation. It does not test an HTTP listener, network transport, or browser rendering. Its timings are not benchmark evidence. On an unrestricted machine use the normal pytest command.

## 2. Automated acceptance scenarios

| Area | Assertions |
| --- | --- |
| All 13 operations | Successful persistence flow; exact set of monitored method/route pairs |
| Validation/errors | Malformed JSON, extra fields, null/empty patches, invalid email, strict positive prices, bad quantity, pagination, missing resources, duplicate email, foreign keys, database unavailability |
| Orders | Price snapshot survives product changes; state transitions are enforced; cancellations update sales reports |
| Monitoring | Correct counts, full-window RPS, 4xx/5xx denominators, nearest-rank percentiles, normalized dynamic routes, bounded retention, window expiry, cache hits included |
| Analytics | Insufficient comparison data returns null; disjoint origin cohorts detect 2x degradation; cache hits do not dilute regression measurements |
| Cache policies | Learning, minimum samples, low reuse, TTL bounds, cooldown, failure rollback, recovery, fast-origin disable |
| Cache behavior | Expiry, LRU capacity, concurrent cold misses produce one origin load, mutations invalidate lists and reports, credentialed requests bypass shared responses |
| Races | Concurrent reads/writes cannot retain a stale result; a cancelled write keeps coordination until its database worker finishes and invalidates the cache |
| Controls/UI | Token-free cache mode switching and clearing, health/monitoring exclusions, dashboard HTML served |
| Benchmark contract | Seeded sales data, response integrity checks, complete monitoring reconciliation; correctness failures reject the run |

Tests intentionally use a very small cache latency threshold where necessary to trigger policies deterministically. Production defaults remain 2 ms, four origin samples, a 5-second cooldown, and 2-30 second TTLs. No test timing result supports a performance claim.

## 3. Interactive demonstration

Create a new seeded database before the first server start:

```powershell
python scripts/seed_demo.py --orders 50000
python -m uvicorn tuneio.main:create_app --factory --host 127.0.0.1 --port 8000
```

If a database already exists, choose a new path using `--db demo.sqlite3` and set `AUTOTUNER_DB` to the same path before starting. The seed command refuses to overwrite files.

1. Open http://127.0.0.1:8000/docs and explore the 13 business operations.
2. Call `/api/reports/sales` repeatedly. On a workload whose measured origin duration exceeds 2 ms, policy learning should enable caching after four origin observations. Later responses show `X-Cache: HIT`.
3. Check `/metrics` and `/analysis`: hits must appear in the total request count and the cache decision must contain an origin mean and reason.
4. PATCH `/api/products/1` with a new name. The next sales report must contain the new name; old cached responses must not survive.
5. Submit malformed requests and request missing IDs. Confirm 4xx responses and a nonzero error rate.
6. Open the dashboard at the root URL. Compare route counts, p95, errors and cache hits against `/metrics`. The dashboard uses a fixed 60-second window and refreshes every five seconds.

A fast, empty dataset may correctly never enable caching. Do not inject sleeps into production handlers to manufacture a gain. Regression behavior is validated using deterministic observation cohorts in the automated suite, rather than adding a public slow/failure-injection endpoint.

## 4. Real HTTP load benchmark

Run on a machine that permits localhost connections, from the repository root:

```powershell
python scripts/benchmark.py --orders 50000 --requests 120 --concurrency 8 --repeats 3
```

The runner creates an isolated temporary database: one user, 100 products, 50,000 paid orders. It starts a fresh Uvicorn single-worker process per mode. Each process gets one identical origin warmup to warm database/OS pages, then 120 requests to the same real SQL sales aggregation with eight concurrent clients. Adaptive runs include cold policy learning. Baseline/adaptive order alternates across the three paired repeats.

The runner checks every response status and SHA-256, reconciles all observed requests/hits with server metrics, and checks identical response hashes across modes and repeats. It records every client latency, mean and p95 latency, achieved throughput, cache hits, decisions, environment versions, and median paired mean-latency improvement in `benchmarks/latest.json`. A result below 20% exits with failure. Startup and correctness failures also fail; do not treat them as performance results.

Acceptance: at least 20% median mean client-latency reduction, successful identical responses throughout, all requests monitored, and adaptive policy activation. Keep the raw JSON as evidence. This supports a claim about repeated sales-report reads on the specified dataset and host, not every API or a general production speedup. Rerun on the target machine; do not hardcode or edit the result.

**Current state:** the runner's data and correctness checks are tested in-process. A real HTTP benchmark could not run in the managed sandbox. There is no verified latency improvement percentage yet.

## 5. Delivery evidence

`docs/validation.json` records the verified local test counts, versions, source hashes, and coverage. `docs/IMPLEMENTATION_STATUS.md` maps completed work and remaining verification to the resume bullets. The wheel build and CLI checks are separate from application tests. Browser rendering, container deployment, hosted CI, and multi-worker behavior have not been validated.

## Tuning and contention verification

The suite now also checks independent-read concurrency, cache-off concurrency, hot hits while origin capacity is busy, same-key coalescing, stale in-flight generation rejection, scoped mutation invalidation, TTL reduction/disable when origin work becomes fast, reuse expiry, threshold hysteresis, and queue delay exclusion from origin policy samples. These are deterministic correctness tests, not latency benchmarks.

Run `python scripts/profile_sql.py` for an isolated 50,000-order query/index comparison. It writes `docs/SQL_PROFILE.json`; it never changes the running application's database. After restarting the application to pick up the new index and concurrency model, rerun the full HTTP benchmark. Prior-version performance percentages cannot validate the new version.

## Browser workflow runner

Restart the server after updating so `/assets/workflow.js` is registered. Open API playground and use Automated API workflows. Preview the scenario, then Run workflow; inspect individual results and optionally Export JSON report. The default full workflow performs 34 business requests and covers all 13 operations. No HIT is required for a scenario pass because a fast origin can correctly stay uncached. Expected 409/422 checks pass while contributing to real monitoring error rates.

Run `node --test tests/workflow.test.cjs` for dependency chaining, statuses/body checks, read-only mode, unique emails, stop/pacing, failure handling and input limits. Pytest's additional Node-to-ASGI integration runs the real JavaScript engine through the full lifecycle against a temporary database and verifies captured IDs, final order state and cache hits under deterministic test timing. Node is optional for application startup but required for these development checks.
