# Phases 15–17 implementation and verification

## Policy review and tuning

The optimize command saves immutable policy revisions. Enable is scoped to a chosen public GET. The preview shows invalidation dependencies, adaptive thresholds, limits and optional matching baseline evidence. Pause selects recommendation mode, disable selects off mode, and rollback restores the preceding mode and cache rules. CLI and menu approval bind a digest to the revision and destination. Proxy restart applies the new revision. Existing response screening, coalescing, generation invalidation, resource limits, hysteresis and error handling govern cache serving. Changing request concurrency remains advisory.

## Terminal and comparisons

The standard-library terminal workspace uses a purple pixel-art wordmark, responsive selection layout, arrow keys, shortcuts and a line-prompt alternative. The live monitor has endpoint, cache, findings and resource views. It shows stale and disconnected data explicitly and can save one versioned JSON snapshot. Matched reports require the same source, request parameters, application-state identifier and comparable connection configuration. The paced sequential read cohorts check response fingerprints. The comparison refuses failed or incompatible runs. An exact response fingerprint is intended for stable public test data.

## Local test evidence

- Policy, baseline, terminal, insights, release, scenario and adapter-focused pytest runs covered revision integrity, non-overwrite writes, approval, keyboard routing, disconnects, data mismatches and cache behavior.
- `python scripts/test_socketless.py --basetemp tmp/at-proof-temp --junitxml=tmp/autotuner-beta-final.xml -ra` passed 643 tests with 93.69% combined coverage. Windows symlink creation prevented one discovery test.
- `python -m autotuner.quality coverage.json` passed the separate core gate with 100% statement and branch coverage.
- `node --test tests/workflow.test.cjs tests/product_ui.test.cjs` passed 19 checks.
- The end-to-end pytest journey passes against the disposable RAM and SQLite fixtures using in-process ASGI and temporary telemetry. It checks approval, warmup, cache hits, byte-matched comparison, credential bypass, write invalidation, a persisted tuning audit and rollback.
- Wheel and source distribution metadata passed `twine check`. The wheel's runtime sources matched the workspace. The installed wheel smoke passed script entry and config checks in a separate venv with existing system dependencies. Both ASGI journeys also passed against the built wheel modules outside checkout imports.
- The local pip lifecycle check installed the previous preview, upgraded to the candidate and uninstalled it in an owned environment with existing dependency packages.

## Qualification status

Windows rejected real loopback connections with WinError 10013. The installed real-HTTP smoke runs on the hosted matrix through the wheel smoke coordinator. A clean resolver install requires access to the dependency index or wheels. The uv attempt encountered an unavailable FastAPI dependency in the offline cache and pipx is unavailable in this sandbox. The two local fixtures establish in-process workflow behavior. Independent developer pilots, the hosted matrix, reference-host performance and a complete 30-minute soak remain release evidence.

Seven installed CLI startup trials per command on the managed host recorded medians of 790 ms for help, 513 ms for version and 480 ms for menu JSON. Help and version exceeded the proposed 500 ms median budget on this host. Reference-host measurements and terminal refresh CPU qualification are pending.

The candidate checker still requires qualification, soak and reviewer proofs. Owner-approved licensing and publisher identity, TestPyPI artifact verification, and public registry authorization are release decisions. See [developer beta candidate review](../release/DEVELOPER_BETA.md) for commands and evidence requirements.
