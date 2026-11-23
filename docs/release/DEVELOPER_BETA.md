# Developer beta candidate review

Candidate version 2.1.0b2 includes numbered choices, in-app demo and proxy controls, live terminal monitoring and matched comparisons. Use the [terminal guide](../TERMINAL_GUIDE.md) for the complete application workflow.

## Validate distributions

Run from the checkout in a clean Python 3.11, 3.12 or 3.13 environment. The build and installer commands use the normal package index for dependencies. Install uv separately on the host if needed.

```
python -m pip install -e ".[test,benchmark,release]" pipx
python -m pytest
python -m tuneio.quality coverage.json
node --test tests/workflow.test.cjs tests/product_ui.test.cjs
python -m build
python -m twine check dist/*
python scripts/smoke_wheel.py dist
python scripts/smoke_installers.py dist/tuneio-2.1.0b2-py3-none-any.whl
```

Start with an empty dist directory or build into a new directory. The wheel smoke installs in a new venv and runs the CLI outside the source tree. Its real HTTP journey exercises both disposable upstreams, including read analysis, approval, warmup, comparison, invalidation, credential bypass, telemetry and rollback. The installer script covers pip, pipx and uv. Supply `--previous PATH_TO_REVIEWED_PREVIEW_WHEEL` to test upgrading from an earlier distribution. The hosted **Backend quality** workflow runs the nine OS/Python combinations and the **Prepare beta distributions** workflow exercises the installer checks.

## Qualify current-source performance

Run the paired benchmark and soak against isolated fixtures on a reference host. The reference definition is four physical cores, at least 8 GiB RAM and Python 3.12. Record the actual host and confirm suitability before passing `--reference-host`.

```
tuneio benchmark --profile qualification --reference-host --require-gates --output tmp/qualification.json
tuneio benchmark --profile soak --duration-seconds 1800 --orders 50000 --requests 100 --require-gates --output tmp/soak.json
python scripts/measure_startup.py --samples 7
tuneio release check --evidence docs/release/candidate.json --json
```

A smoke test or in-process fixture run is separate evidence. Retain latency and resource results, raw startup trials, terminal refresh CPU, proxy CPU at the matched workload, and the full soak with clean shutdown. The pre-existing [qualification protocol](../product/PHASE_10_RELIABILITY_EVIDENCE.md) defines duration boundaries, cache accounting and acceptance thresholds.

## Review and distribute

1. Bind the final wheel and source distribution to a frozen source revision and record their SHA-256 hashes. Review both artifacts.
2. Collect current-source benchmark and soak reports, all hosted combinations, installer lifecycle results and at least two independent supported-API pilot reviews. Include privacy bypass, invalidation and rollback evidence from each pilot.
3. Confirm license and distribution ownership. Verify the approved TestPyPI release artifact through a clean install before attesting to its verification.
4. Copy the sanitized evidence under the directory containing the candidate manifest. Reference each proof by relative path and SHA-256. Use `review-receipt.example.json` for the reviewer attestations. The offline checker exits 3 while evidence is missing. Uploads require separately authorized registry access.

Use the [security policy](../../SECURITY.md) for vulnerability reporting and repository issues for installation or usability feedback. For local experiments, keep traffic on loopback and use public test data and isolated telemetry files.
