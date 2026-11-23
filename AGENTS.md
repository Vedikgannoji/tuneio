# tuneio development instructions

This repository is evolving from a FastAPI demo into an installable API monitoring and adaptive caching product. The current terminal-first product contract is `docs/reference/tuneio_CLI_Product_PRD.md`. It supersedes the historical Word draft `docs/reference/Autotuner_Technical_PRD.docx`. Internal phase contracts are in `docs/product/`.

## Phase development and testing

- Implement one agreed phase at a time. After each phase, stop for testing and review with the user before beginning the next phase.
- After the user completes each phase review, provide a concise commit title covering the phase's final changes before proceeding to the next phase, so the user can commit and push. Do not assume a commit or push has occurred; wait for the user's instruction to proceed.
- Write pytest tests for each phase's new Python behavior. Test success cases, invalid input, errors and meaningful edge cases; include integration tests when modules interact.
- Keep regression tests for discovered bugs. Cover concurrency, cancellation, cache isolation and invalidation when the phase affects those behaviors.
- Use isolated temporary data and injectable clocks or controlled synchronization. Avoid sleep-based timing assertions, live external services and changes to the user's application database.
- For JavaScript behavior, add Node's built-in test runner checks and Python-to-JavaScript integration tests where appropriate. Document optional Node dependencies and skips.
- Before declaring a phase ready for review, run its focused tests and the full pytest suite. Run relevant JavaScript and packaging checks when those surfaces change. Fix failures before moving on.
- Normal verification is `python -m pytest`. In the managed Windows sandbox that blocks asyncio's TCP self-pipe, use `python scripts/test_socketless.py` and disclose this verification boundary.
- A focused pytest run may disable aggregate coverage options with `-o addopts=""`; it does not replace the full coverage-enforced suite.
- Maintain the repository's coverage gate. Never weaken tests, exclusions, assertions or thresholds merely to obtain a pass.
- Record the phase's test commands, results, limitations and remaining decisions in its review evidence. Do not claim unexecuted tests, proxy capabilities or performance targets as achieved.
