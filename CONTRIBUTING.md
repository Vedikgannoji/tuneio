# Contributing

Follow AGENTS.md and the internal development contracts in docs/product. Install `.[test,benchmark,release]` in a Python virtual environment and Node 22 for UI checks.

Run `python -m pytest`, `python -m tuneio.quality coverage.json`, and `node --test tests/workflow.test.cjs tests/product_ui.test.cjs`. For packaging changes run `python -m build`, `python -m twine check dist/*` and `python scripts/smoke_wheel.py dist`. Keep dist limited to one candidate wheel.

Add isolated pytest coverage for behavior, errors and meaningful edge cases; preserve regression and concurrency tests. Use temporary data and controlled synchronization. Never weaken coverage gates or modify a user's application database. Do not commit credentials, private payloads or local telemetry.

Normal pytest is authoritative. scripts/test_socketless.py is a disclosed managed-sandbox fallback, not real TCP qualification. Record limits and unexecuted checks in review evidence. License and public contribution terms await owner approval.
