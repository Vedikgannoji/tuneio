# Autotuner CLI Product Requirements

Version 2.0 | 15 November 2026 | Product owner Vedik

This is the current internal engineering contract for the terminal-first developer product. It supersedes the scope and development sequence in the original Word PRD, while retaining its tested HTTP/cache correctness requirements and qualification gates. The Word draft remains historical; it is not evidence that future features are implemented.

## Product decision

Build a lightweight, locally installed CLI that helps a developer discover supported project APIs, establish an explicit connection, analyze performance, approve suitable optimizations, monitor ongoing behavior, compare matched sessions and export evidence. Every essential operation must work through keyboard-operated terminal flows and scriptable commands. Browser use, a frontend server, Node.js and a hosted account are not runtime requirements.

The current monitoring proxy and cache controller are reusable foundations. The original business API remains a demonstration fixture. Installing Autotuner in a project does not intercept traffic or authorize executing project code; integration must be visible and explicit.

## Intended users and supported scope

- Backend developers investigating local or staging HTTP APIs.
- API maintainers reviewing caching and bounded request-policy changes.
- Contributors reproducing behavior, resource costs and performance evidence.

Initial discovery supports FastAPI project signals and local OpenAPI descriptions. A running OpenAPI description can be queried only after explicit connection selection. Generic HTTP APIs that satisfy the existing forwarding contract retain a manual upstream/operation configuration path. Static findings are candidates, not proof that all runtime routes were found.

The initial automatic integration is the existing single-worker loopback proxy. Any framework adapter requires a separately tested contract. Finite HTTP/1.x traffic and ordinary request/response bodies remain supported within configured limits. WebSockets, indefinite event streams, multi-worker shared state, distributed invalidation and public management listeners remain excluded.

## Developer journey

1. Install through a verified pip/venv, pipx or uv tool distribution.
2. Enter a project directory and launch `autotuner` in a terminal.
3. Review supported-project findings and select operations and connection setup.
4. Observe real traffic or approve a bounded test workload with explicit data.
5. Inspect baseline metrics and evidence-backed recommendations.
6. Review changes and approve applicable cache/request policies.
7. Monitor decisions, errors, resource use and policy effectiveness in the terminal.
8. Compare matched baseline/optimized sessions, export JSON and pause or roll back policies.

Steps are not implied by launching the tool. Discovery, startup, network connections, load generation, mutations and policy changes have separate explicit actions.

## Functional requirements and acceptance criteria

### Terminal interface

CLI01: In a terminal with interactive stdin and stdout, bare `autotuner` opens the workspace. With either stream redirected, it prints help and exits without reading stdin. `autotuner menu` explicitly requests interactive entry; redirected streams or `--no-input` cause exit 2 unless `--json` is requested.

CLI02: `autotuner menu --json` prints one schema-versioned JSON object describing available actions, effects, selected project/config paths and planned features. It never prompts, scans project files, opens a browser, connects to an API or changes configuration. Invalid input produces empty stdout, a sanitized stderr diagnostic and exit 2.

CLI03: The foundation uses numbers/letters followed by Enter; b/0 returns, q exits and h/? opens help. Raw arrow-key navigation and a live terminal viewport are later monitoring work. EOF closes successfully; Ctrl+C at a menu prompt exits 130. Existing direct command shutdown behavior is preserved, including foreground serve returning cleanly after Ctrl+C.

CLI04: Keep existing init, config check, serve, doctor, status, endpoints, insights, cache, sessions, report, scenario, benchmark, demo and optional dashboard commands compatible. Pass argv arrays directly; never construct a shell command from user input. `--no-input` is a global option placed before a command and is also accepted after menu.

Acceptance: Deterministic fake-terminal tests cover prompts, navigation, back/exit, JSON, redirected streams, errors, bounded input, confirmation and cancellation. Installed-wheel checks run the offline menu without a repository checkout. Help/version/menu imports must not load HTTPX, Uvicorn, FastAPI, Pydantic, SQLite or the proxy.

### Project discovery and configuration

DSC01: Inspect allowlisted project metadata and supported source patterns without importing project modules. Ignore dependency/build directories and bound file count, bytes, recursion and schema size. Explain ambiguous startup points, missing schemas and incomplete discovery. Symlink handling must be explicit and tested before scanning is shipped.

DSC02: Show discovered method/path operations, their origin and confidence before creating configuration. Do not infer permission to cache merely because a route is GET or exists in OpenAPI.

CFG01: Configuration stays project-local. Existing file/environment/explicit flag precedence is retained. The menu resolves relative config and export paths against the selected project without changing process cwd. An explicit menu config overrides AUTOTUNER_CONFIG, which overrides autotuner.json. Selecting a new project resets the menu's selected config to that project's autotuner.json.

CFG02: Entry creates no files. Creating configuration requires an explicit action and acknowledgement, defaults to observe mode and refuses to overwrite. The foundation delegates validated commands to the existing CLI. Future generated changes require a preview, validation and deliberate replacement behavior.

Acceptance: Disposable projects test multiple entry points, malformed inputs, unsafe paths, unsupported frameworks, permission failures and interrupted setup. No discovered project code is executed and no data-plane request is generated on entry or discovery.

### Startup and integration

INT01: Show the upstream, listener, selected operations, runtime limits and owned-process behavior before connecting or starting anything. User-selected upstreams remain authoritative; traffic cannot select a different destination.

INT02: Starting project code requires reviewing its exact command and effects. Track and stop only owned child processes. The terminal foundation invokes the existing foreground serve command only after confirmation; session queries need a proxy already running in another terminal. It does not supervise upstream application startup yet.

Acceptance: Test port conflicts, missing upstreams, early child exits, Ctrl+C, bounded shutdown, config mismatch and retained/uncertain writes. Do not terminate unrelated project processes.

### Performance analysis and baselines

ANA01: Separate passive observation from explicitly approved active testing. Bound requests, duration, rate, concurrency, body/header sizes and environment scope. No automatic mutation replay or implicit retries. Read methods require approved semantics and test data too.

ANA02: Record latency percentiles, throughput, raw errors, origin I/O, queueing, cache eligibility/reuse and resource usage. Distinguish proxy handling, upstream I/O and client round trip. Upstream slowness alone is not proof of SQL, application-code or database execution cost.

ANA03: Save source/tool/config/workload identity, sample counts and incomplete-sampling markers. Require enough recent evidence before emitting recommendations. Preserve expected scenario errors in raw API error rates.

Acceptance: Controlled fixtures establish known findings. Test failed/cancelled/partial sessions, redaction, insufficient samples, bounded retention, backpressure and stale data. An unavailable metric remains unavailable rather than becoming a fabricated zero.

### Approved optimization and ongoing tuning

OPT01: Preview proposed policy, scope, expected evidence, risks, limits and rollback before applying. Keep observations and recommendations separate from applied changes. No source rewrite, database/index change, infrastructure modification or autonomous application mutation is part of the first automatic integration.

OPT02: Reuse existing approved public-GET caching, representation isolation, freshness caps, miss sharing, byte budgets and dependent-write invalidation. Credentials, private/no-store responses, Set-Cookie, unsupported Vary/revalidation semantics and unknown reuse behavior remain conservative bypasses.

OPT03: Adaptive activation/TTL uses admitted origin cost and recent reuse with minimum cohorts, hysteresis, cooldown and immediate error handling. Do not feed hit latency or queue delay into origin-cost learning. Expiry and invalidation provide fresh origin samples without synthetic writes.

OPT04: Bounded concurrency auto-tuning is a separately reviewed opt-in controller deliverable. Establish safe min/max bounds, evidence windows, error/tail-latency guardrails, ownership and rollback before exposing it as supported. Until then, provide an evidence-backed recommendation rather than claiming concurrency is automatically tuned.

OPT05: Provide inspect, pause, disable and rollback operations, with bounded decision history and visible reason codes. Configuration changes initially apply through validated restart/revision boundaries; do not invent partial hot reload.

Acceptance: Privacy isolation, variants, expiry, stale in-flight fills, writes, cancellation, origin errors, low reuse and resource pressure must preserve response correctness. Fast/unique workloads can correctly produce no optimization. Measured benefit is workload-specific, never a guarantee of perfect API behavior.

### Monitoring and comparison

MON01: Provide a live keyboard-operated terminal view, endpoint filters, refresh controls, pause/resume, visible stale/disconnected/degraded states and a plain-text alternative. Optional browser assets are not on the essential path.

CMP01: Compare matched workload/config/application-state identities, request mix, concurrency, warmup and duration, with response correctness checked. Label observational cohorts separately. Reject or clearly explain incomparable sessions, missing baselines and sampling loss.

EXP01: Export schema-versioned JSON for analysis, baseline, optimized, comparison and tuning evidence; preserve useful CSV summaries. Export includes provenance, units and limitations. Do not retain credentials, cookies or response bodies by default. Reject or stream bounded large exports; never silently truncate evidence or overwrite without an explicit option.

Acceptance: A fresh developer can complete the essential journey without a browser. Test terminal resize/small windows, Ctrl+C, redirected output, large reports, missing/corrupt history, non-overwrite semantics and known comparison results.

## Command availability contract

| Surface | Current implementation in this foundation | Later deliverable |
| --- | --- | --- |
| Bare command and menu | Offline workspace, help, project/config selection | Rich live monitoring viewport |
| Setup | Explicit config creation/validation, foreground proxy and diagnostics | API discovery and reviewed upstream startup |
| Session and insights | Existing status, endpoint metrics and advisory insights | Guided analysis/baseline sessions |
| Cache and policies | Existing route explanation and confirmed cache clear | Guided policy approval and rollback lifecycle |
| Reports | Existing session history and stored/live JSON exports | Matched comparison and unified session exports |
| Script interfaces | Existing commands plus menu JSON and global --no-input | Proposed discover, analyze, optimize, monitor, compare and export commands |

Proposed commands are not enabled merely because they appear here. Public menus show only implemented actions; planned capabilities are informational help text without an executable action.

## Lightweight architecture and budgets

The terminal shell uses the Python standard library and a small argv dispatcher. It adds no runtime dependency, resident daemon, UI server, background refresh task or persistent menu history. Load the proxy/model/storage stack only after selecting an existing runtime operation. Runtime installation dependencies remain unchanged in this foundation; any later optional-dependency split requires installation and demo compatibility evidence.

| Area | Target or preserved limit | Evidence |
| --- | --- | --- |
| CLI startup | Proposed median <=500 ms and p95 <=1 s for help/version and offline menu JSON | At least five fresh process runs on declared reference hardware, retain all trials; qualify later |
| Terminal CPU | Proposed idle <=1% of one core over 60 s; live refresh <=5% at 1 Hz and <=100 visible operations | Reference-host CPU-time samples; live rendering is a later deliverable |
| Proxy CPU | Proposed <=10% CPU-seconds regression against the reviewed observe baseline per 1000 responses at the matched reference workload | Keep raw CPU and throughput measurements; review baseline before freezing this new gate |
| Idle proxy RSS | <=150 MiB | Existing reference-host qualification protocol |
| Observe overhead | Added median <=3 ms and p95 <=8 ms at 100 requests/s | Matched 1 KiB direct/proxy cohorts, concurrency 8, reference 4-physical-core/8-GiB/Python-3.12 host |
| Repeated-read benefit | >=20% median mean-latency reduction against cache-off proxy | Correct matched repeated workloads; no general guarantee |
| Stability | Current-source 30-minute sustained mixed workload and resource recovery | Short CI smoke does not qualify this requirement |
| Raw events | Default total capacity 10000 | Existing bounded proxy/upstream observation tests |
| Persistent telemetry | Default 7 days and 100 MiB | Existing storage budgets; disclose loss/truncation |
| Response cache | Default 64 MiB, 256 entries, 1 MiB per entry | Existing accounted-byte and concurrency tests |
| Terminal input | <=4096 characters per line | Reject overlong lines without interpreting the remainder as commands |

Startup and new CPU budgets are proposed acceptance targets for joint review, not results established by a single local comparison. Do not weaken existing proxy/performance gates to meet them. Streaming export/rendering limits and the new CPU baseline must be finalized with measurements before that surface's release; the current menu delegates existing bounded telemetry exports without claiming a new streaming implementation.

## Automation and output contracts

Successful structured output has schema_version and tool_version where applicable. Diagnostic/progress messages remain on stderr. Interactive menu prompts occur only in interactive mode. Scripted commands do not read stdin. Menu action failure returns the user to the menu with a diagnostic; the action's existing exit code is not converted into a success claim. Bare non-interactive entry is informational help, exit 0.

Exit codes remain 0 success, 1 runtime/I/O failure, 2 invalid input/configuration/non-interactive misuse and 3 assertion/qualification failure, with 130 for cancellation at a menu prompt. Preserve documented existing direct-command interruption semantics until reviewed separately.

Local control remains loopback-only with no mandatory management token. Do not expose/tunnel it to untrusted clients. No configuration, API inventory or telemetry is uploaded automatically. Strip terminal control characters from project-controlled display text; sanitize errors and avoid shell evaluation.

## Development and release acceptance

The implemented detailed contracts remain enforceable alongside this PRD: docs/product/config.schema.json and scenario.schema.json; PHASE_04_OBSERVATION_PROXY.md for supported forwarding; PHASE_05_STORAGE_REPORTING.md for retention/provenance; PHASE_07_SAFE_CACHING.md for privacy, variants and invalidation; PHASE_08_ADAPTIVE_TUNING.md for controller limits; and PHASE_10_RELIABILITY_EVIDENCE.md for benchmark qualification. These internal documents retain their historical identifiers. This revision changes the product journey and future work, not the tested HTTP/cache guarantees.

Internal sequence: 12 contract/menu foundation; 13 discovery/setup; 14 guided analysis; 15 approved optimization/tuning; 16 live terminal monitoring/comparison; 17 end-to-end developer beta. Stop for joint review and record actual tests after each step. Public copy uses feature names and versions, never these internal numbers.

Preserve passing pytest regressions and >=90% coverage gates, with independent core statement/branch floors. Add controlled integration, cancellation, concurrency and invalidation tests when affected. Use temporary project/data fixtures and injected clocks; avoid live external services and sleep-based correctness assertions. Normal local/CI verification is python -m pytest; the socketless runner is a disclosed sandbox boundary.

Developer beta requires clean pip/venv, pipx and uv tool installation outside the checkout; Windows/Linux/macOS and Python 3.11-3.13 compatibility; current-source performance/stability evidence; and at least two independently reviewed supported-API integrations. Preserve the original five-external-developer/three-framework adoption targets as broader validation goals, not claims fulfilled by two initial pilots. Framework coverage must not be overstated from manually configured proxy support.

Resolve licensing and distribution ownership before a public registry upload. Verify TestPyPI installation and provenance, review all evidence, then obtain explicit publication authorization. No permanent API token or online account is required for local operation.

## Decision register

| Decision | Current assignment | Deadline |
| --- | --- | --- |
| Initial discovery | FastAPI plus OpenAPI descriptions; manual fallback for supported HTTP | Frozen for discovery implementation |
| Terminal foundation | Standard-library numbered/lettered menus with Enter; no UI dependency | Implemented for joint review |
| First integration | Explicit single-worker local proxy | Retained; adapters require a separate contract |
| License choice and grant | Vedik; pending owner choice, no license assigned | Before registry candidate publication |
| Distribution name and publisher | Working name api-performance-auto-tuner; Vedik verifies registry ownership | Before TestPyPI/PyPI rehearsal |
| Reference host allocation | Vedik/release reviewer; not satisfied by local sandbox tests | Before performance qualification |
| Concurrency controller | Reviewed design and tests required; current support is fixed configured bounds | Before optimization capability is enabled |
| Version line | 2.1.0b1 terminal workflow candidate | Before the registry release |

## Foundation acceptance checklist

- Root/menu help is useful before a project has configuration.
- Menu startup is offline, read-only and does not import runtime dependencies.
- Existing script commands and configuration precedence remain tested.
- Config creation, listening and cache clear require explicit actions/acknowledgement; exports never overwrite silently.
- Invalid input, EOF, Ctrl+C, missing paths and redirected streams produce bounded, clear outcomes.
- Installed wheel contains the terminal module and can describe actions outside the checkout.
- Future discovery, live terminal monitoring, guided comparisons and unsupported tuning are not represented as implemented.
