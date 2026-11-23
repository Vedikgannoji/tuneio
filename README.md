# tuneio

tuneio is a terminal-first tool for observing HTTP APIs and applying reviewed adaptive caching. Discover FastAPI candidates or import OpenAPI, capture a baseline, approve a cache policy, and compare it with a matched run. tuneio 2.1.0 is distributed under the MIT license.

## Install a local wheel

Use Python 3.11, 3.12 or 3.13 and a wheel from this project.

| Installer | Command |
| --- | --- |
| pip in an activated virtual environment | `python -m pip install PATH_TO_WHEEL` |
| pipx | `pipx install PATH_TO_WHEEL` |
| uv | `uv tool install PATH_TO_WHEEL` |

Run `tuneio --version`, then start `tuneio` from your project. For a source checkout, install with `python -m pip install -e .`. Use the wheel's path until a registry release is verified and announced.

After the release is published to PyPI, install `tuneio` with `python -m pip install tuneio`, `pipx install tuneio`, or `uv tool install tuneio`. All three install the `tuneio` command.

## Work in the terminal

The workspace has a purple pixel-art title, highlighted selections, and arrow-key and number navigation. Use **Enter** to open a selection, **b** to go back, **h** for help and **q** to quit. If your terminal needs line prompts, use `tuneio menu --plain`. The CLI uses your terminal's font. Cascadia Mono and JetBrains Mono render the block characters clearly.

1. In **Setup and configuration**, discover operations and save a reviewed observe configuration. Start your API with its usual command, or choose **Start or stop demo API** for the bundled fixture. Choose **Start or restart workspace proxy** to connect it.
2. In **Session and insights**, inspect traffic, save a baseline, or open the live monitor. Switch monitor views with **Tab** or arrow keys and use **/** to filter, **Space** to pause, **r** to refresh, and **i** to change the refresh interval.
3. In **Cache and policies**, review a public GET rule and its dependent writes. Saving an approval selects the new config revision. Use **Start or restart workspace proxy** to apply it. The policy menu also provides pause, disable and rollback.
4. In **Reports and history**, compare matched reads or export JSON. Scriptable commands expose the same actions.

The [terminal guide](docs/TERMINAL_GUIDE.md) includes the complete disposable demo, matched capture walkthrough and command reference. For the original demo and optional browser interface, see the [demo guide](README_DEMO.md).

## Supported deployment

- A single-worker loopback HTTP/1.x proxy for finite requests. Point clients at the proxy to measure their traffic.
- Adaptive response caching for reviewed public GETs. HTTP freshness, request variants, credential bypass, memory limits and declared write invalidation govern cache serving.
- Static FastAPI discovery covers literal decorators on direct apps and simple factories. Use OpenAPI for assembled routers and dynamic routes, or configure operations manually.
- Tuning uses measured origin latency and reuse to activate caching and adjust TTL. Admission and concurrency limits are configured. Concurrency findings are advisory.
- Telemetry stays local in bounded SQLite storage. Keep the proxy on a trusted machine and avoid exposing its management interface to untrusted clients.

## Develop and qualify

```
python -m pip install -e ".[test,benchmark,release]"
python -m pytest
python -m tuneio.quality coverage.json
python -m build
python -m twine check dist/*
python scripts/smoke_wheel.py dist
```

The optional browser UI checks use `node --test tests/workflow.test.cjs tests/product_ui.test.cjs`. The managed Windows sandbox can use `python scripts/test_socketless.py` for in-process tests. The installed-wheel journey and performance qualification require real loopback HTTP.

See the [candidate checklist](docs/release/DEVELOPER_BETA.md), [contributing guide](CONTRIBUTING.md), [security policy](SECURITY.md), and [changelog](CHANGELOG.md).
