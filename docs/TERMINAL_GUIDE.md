# tuneio terminal guide

## Start the workspace

Run `tuneio` from your project or use `tuneio menu --project PATH --config FILE`. Entry is offline. It reads terminal input after you select a menu action. Use arrows and Enter, number keys, **b** to go back, **q** to quit and **h** for help.

Choose a listed number or move the highlight with **Up/Down** and press **Enter**. For matched captures, **1** selects **Baseline** or **Local**, and **2** selects **Optimized** or **Staging** on the corresponding screen. Approvals default to **No**. Press **1** for **Yes**. Paths and other custom values use the text box.

The terminal supplies the font. For the purple block-letter title and highlighted selection, use a true-color terminal such as Windows Terminal with Cascadia Mono or JetBrains Mono. On narrow terminals the title fits on one line. `--plain` and `NO_COLOR=1` use line prompts. Redirected scripts can use `tuneio menu --json` for the action list.

The keyboard-driven workspace holds menus, forms, reports, and the live monitor on the same screen. Use **Up/Down** and **PgUp/PgDn** to scroll reports. Press **d** where offered to see detailed metrics, then **Enter** to return. Use **Reports and history** to view stored telemetry, open a saved capture or comparison, or export full JSON. Scripting commands also support `--json` where shown in help.

Start the demo from **Setup and configuration → Start or stop demo API**. With a config selected, choose **Start or restart workspace proxy**. The rich workspace runs its proxy in the background, so you can open **Session and insights → Open live monitor** in the same window. After saving a policy revision, use the same proxy action to activate it. The workspace stops the processes it started when you quit. Plain mode also supports a foreground proxy with Ctrl+C to return.

## Run the demo in one tuneio window

From the checkout with tuneio installed, run `tuneio menu --config examples/adaptive-demo.json`. The demo uses port 3000 and the proxy uses port 8080. Free those ports before starting.

1. Open **Setup and configuration** and select **9 Start or stop demo API**. Confirm and press **Enter** to return to Setup.
2. Select **3 Start or restart workspace proxy**. Confirm, press **Enter** to return and press **b** for the workspace menu.
3. Open **Session and insights** and select **5 Capture matched GET workload**. With this sample config, choose **2 Optimized**. Enter `/api/products` and the application identifier `demo-seed-v1`, select **1 Local**, and enter eight measured requests and five warmup requests. Enter a new JSON filename and choose **1 Yes** on the preview. To make a matched pair, capture the baseline first with an observe config that defines the same operation and upstream, then switch to the optimized config and restart the proxy. Keep the demo running for both captures.
4. Select **6 Open live monitor** to see request volume, latency, errors, cache behavior, findings and resource usage. Press **q** to go back to the workspace. Open **Reports and history** to revisit saved captures or compare a matched pair.

## Scripted demo on Windows

Use three Command Prompt windows. Choose free ports if 3000 or 8080 are already occupied. The RAM upstream and the files under `tmp\beta-review` are disposable. Choose a new directory name for a second run.

Install once from the checkout. If you already have a development environment, activate it and install the current source.

```bat
cd /d E:\API-Performance-Auto-Tuner
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[test,benchmark,release]"
tuneio --version
```

**Terminal 1** starts the upstream. Keep it running for both captures so application state remains the same.

```bat
cd /d E:\API-Performance-Auto-Tuner
.venv\Scripts\activate
tuneio demo --listen 127.0.0.1:3000
```

**Terminal 2** describes the disposable API, previews a project config and runs the proxy. Copy the preview's `review_token` into the apply command.

```bat
cd /d E:\API-Performance-Auto-Tuner
.venv\Scripts\activate
mkdir tmp\beta-review
>tmp\beta-review\openapi.json echo {"openapi":"3.1.0","paths":{"/api/products":{"get":{}},"/api/products/{id}":{"patch":{}}}}
tuneio discover --project tmp\beta-review
tuneio setup --project tmp\beta-review --select 1 --select 2 --upstream http://127.0.0.1:3000 --output observe.json --json
tuneio setup --project tmp\beta-review --select 1 --select 2 --upstream http://127.0.0.1:3000 --output observe.json --apply SETUP_TOKEN --json
tuneio serve --config tmp\beta-review\observe.json
```

**Terminal 3** verifies forwarding, previews the baseline, then captures it. Replace `BASELINE_TOKEN` with the preview's token. The browser is optional.

```bat
cd /d E:\API-Performance-Auto-Tuner
.venv\Scripts\activate
curl.exe -i http://127.0.0.1:8080/api/products
tuneio baseline --config tmp\beta-review\observe.json --output tmp\beta-review\before.json --get /api/products --environment local --requests 8 --warmup 5 --app-state demo-seed-v1 --json
tuneio baseline --config tmp\beta-review\observe.json --output tmp\beta-review\before.json --get /api/products --environment local --requests 8 --warmup 5 --app-state demo-seed-v1 --apply BASELINE_TOKEN --json
tuneio optimize enable --config tmp\beta-review\observe.json --route /api/products --baseline tmp\beta-review\before.json --output tmp\beta-review\adaptive.json --json
tuneio optimize enable --config tmp\beta-review\observe.json --route /api/products --baseline tmp\beta-review\before.json --output tmp\beta-review\adaptive.json --apply POLICY_TOKEN --json
```

Replace `POLICY_TOKEN` with the optimization preview's token. Verify the preview includes the PATCH dependency and adaptive thresholds. Stop the proxy in Terminal 2 with **Ctrl+C**. Start the new revision in that terminal.

```bat
tuneio serve --config tmp\beta-review\adaptive.json
```

In **Terminal 3**, use the same application-state identifier and request options. Replace `OPTIMIZED_TOKEN` with the new capture preview's token.

```bat
tuneio baseline --config tmp\beta-review\adaptive.json --stage optimized --output tmp\beta-review\after.json --get /api/products --environment local --requests 8 --warmup 5 --app-state demo-seed-v1 --json
tuneio baseline --config tmp\beta-review\adaptive.json --stage optimized --output tmp\beta-review\after.json --get /api/products --environment local --requests 8 --warmup 5 --app-state demo-seed-v1 --apply OPTIMIZED_TOKEN --json
tuneio compare tmp\beta-review\before.json tmp\beta-review\after.json --output tmp\beta-review\comparison.json
tuneio monitor --config tmp\beta-review\adaptive.json --output tmp\beta-review\monitor.json
tuneio menu --project tmp\beta-review --config adaptive.json
```

The demo can run below the adaptive activation threshold. The capture reports the observed hit count and both latency distributions. In the menu, choose **Session and insights**, then **Open live monitor**. Use tabs to switch among endpoints, cache, findings and resources. Filter with **/**, scroll with **↑/↓** or **j/k**, pause with **Space**, refresh with **r**, choose the refresh interval with **i**, and return with **q**.

To verify rollback, exit the menu and run the following in Terminal 3. Replace `ROLLBACK_TOKEN` with the token printed by the first command.

```bat
tuneio optimize rollback --config tmp\beta-review\adaptive.json --output tmp\beta-review\restored.json --json
tuneio optimize rollback --config tmp\beta-review\adaptive.json --output tmp\beta-review\restored.json --apply ROLLBACK_TOKEN --json
```

Press **Ctrl+C** in Terminal 2 and restart it with `tuneio serve --config tmp\beta-review\restored.json`. In Terminal 3, run `curl.exe -i http://127.0.0.1:8080/api/products` and verify `x-autotuner-cache-reason: mode_observe`. Stop Terminal 2 and then Terminal 1 with **Ctrl+C**.

On Linux and macOS, use your checkout path, `source .venv/bin/activate`, and `curl`. Write the same OpenAPI JSON using your shell's file redirection.

## Use your own API

- **Discovery.** Select **Discover and set up this project** or run `tuneio discover --project .`. Select observed operations by ID and start the API using its usual command. An explicit `--openapi-url` fetches a live OpenAPI description from a loopback IP. Static candidates can omit mounted routers, dynamic endpoints or generated routes.
- **Analysis.** Use `tuneio insights --config FILE --json` or save a passive snapshot with `tuneio baseline --config FILE --output FILE.json`. Use a concrete GET with test data for active runs. Active runs are sequential, stop on failed responses, and allow up to 20 requests including warmup. Ctrl+C requests a stop after the current request.
- **Policy.** Approve only responses that are public across callers and safe for the reviewed TTL and variant rules. Include all relevant write dependencies. Configured writes are included conservatively, and `--invalidate METHOD PATH` adds dependencies. Use `--read-only` for resources with no dependent writes. Writes made outside the proxy rely on origin freshness and TTL. Cache failure handling can deactivate a rule until there is fresh, successful evidence.
- **Pause or restore.** Use `optimize pause` for recommendation mode, `optimize disable` to turn caching off, and `optimize rollback` to restore the preceding policy. Each action previews an immutable revision and takes effect on restarting the proxy with that file. Keep revision files in the project so you can inspect the history. The existing `cache clear` command evicts live entries.
- **Matching and privacy.** Keep route, environment, warmup, request count, pacing, timeout, proxy source, transport configuration and application/data revision the same. Matched captures compare SHA-256 fingerprints of the measured response bytes and can reject changing content. Use non-sensitive public test data. Capture JSON retains fingerprints, normalized operations and counters. Compare reports summarize latency, throughput and cache hits. Rolling observations can include unrelated traffic and have separate sampling indicators.
- **Export.** `tuneio monitor --once --json` returns a machine-readable snapshot. Use `--output FILE` to save it. `tuneio compare` supports the same export option. `tuneio sessions` lists retained sessions. `tuneio report --format json` retains tuning decisions and `--format csv` exports route metrics. Use `--live` to request a checkpoint from the running proxy.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Cannot reach proxy | Select **Start or restart workspace proxy** in **Setup and configuration**. Select the same configuration in the workspace and the monitor. For scripts, start `tuneio serve --config FILE`. |
| Upstream failure | Start the upstream and verify its listener with `tuneio doctor --config FILE --connect`. |
| Cache never serves | Check `tuneio cache explain PATH --config FILE --json` for training, privacy, freshness or mutation reasons. |
| Preview was rejected | Repeat the preview after edits and apply the current token to the same target file. An existing output needs a new filename. |
| Comparison exits 3 | Inspect the reason codes. Capture a fresh pair with matching options and stable response data. |
| Monitor is plain or truncated | Widen the terminal, check its font/ANSI support, or use `--once --json` for machine-readable output. |
