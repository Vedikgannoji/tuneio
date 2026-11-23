"""Human-readable views of the existing command results."""


def words(value):
    return 'Unavailable' if value is None else str(value).replace('_', ' ')


def milliseconds(value):
    return 'unavailable' if value is None else f'{value:.2f} ms'


def bytes_used(value):
    for scale, unit in ((1024**3, 'GiB'), (1024**2, 'MiB'), (1024, 'KiB')):
        if value >= scale:
            return f'{value/scale:.1f} {unit}'
    return f'{value:,} B'


def show_rows(rows, empty):
    yield from rows[:20] or [empty]
    if len(rows) > 20:
        yield f'{len(rows)-20} more. Use JSON for the full result.'


def rule_lines(rules):
    rows = []
    for rule in rules:
        ttl = f"{rule['ttl_min_seconds']:g} to {rule['ttl_max_seconds']:g} s"
        variants = ', '.join(rule['vary_headers']) or 'none'
        writes = ', '.join(f"{dep['method']} {dep['path']}" for dep in rule['invalidate_on'])
        methods = ', '.join(rule['methods'])
        rows += [f"  {methods} {rule['match']} | {rule['policy']} | TTL {ttl}",
                 f"    Vary by {variants} | Invalidate on {writes or 'no declared writes'}"]
    return rows


def review_lines(data):
    if 'config' in data:
        cfg = data['config']
        yield f"Observe configuration | Proxy {cfg['listen']} | Upstream {cfg['upstream']}"
    for key, label in (('upstream','Upstream'), ('listen','Proxy listener'), ('proxy','Proxy listener')):
        if key in data:
            yield f'{label}  {data[key]}'
    if 'selected' in data:
        yield 'Operations to monitor'
        for row in data['selected']:
            yield f"  {row['method']} {row['pattern']}"
    if 'workload' in data:
        workload = data['workload']
        yield f"{data.get('stage', 'baseline').capitalize()} capture | {workload['method']} {workload['path']}"
        yield f"  {workload.get('warmup',0)} warmup and {workload['requests']} measured requests ({workload['environment']})"
        yield (f"  {workload['delay_ms']} ms between requests | {workload['timeout_seconds']} s timeout | "
               f"Concurrency {workload['concurrency']} | Expected HTTP {workload['expected_status']}")
    if 'policy' in data:
        mode, old = data['policy']['mode'], data['previous_policy']['mode']
        yield f'Proxy mode  {old} -> {mode}'
        yield 'Proposed cache rules'
        yield from rule_lines(data['policy']['cache']['routes']) or ['  No cache rules']
        if data['evidence']:
            evidence = data['evidence']
            yield (f"Baseline evidence  {words(evidence['state'])} | {evidence['samples']} samples | "
                   f"mean upstream latency {milliseconds(evidence['origin_mean_ms'])}")
        if mode == 'adaptive':
            tuning = data['activation']
            yield (f"Adaptive activation  {tuning['minimum_samples']} samples | {tuning['reuse_minimum']} repeated reads | "
                   f"{tuning['enable_ms']:g} ms minimum upstream latency")
            limits = data['limits']
            yield f"Cache budget  {limits['cache_entries']:,} entries | {bytes_used(limits['cache_bytes'])}"
    for key in ('reason','requirements','activation_boundary'):
        if key in data:
            yield data[key]
    if 'output' in data:
        yield f"Save to  {data['output']}"


def route_stats(operation, row):
    p95 = milliseconds(row.get('p95_ms'))
    count, hits = row.get('count', 0), row.get('cache_hits', 0)
    errors = f"{row['error_rate']:.1%}" if count else 'unavailable'
    return (f"{operation} | {count:,} {'request' if count == 1 else 'requests'} | p95 {p95} | "
            f"{errors} errors | {hits:,} {'hit' if hits == 1 else 'hits'}")


def summary(command, data):
    if command == 'config':
        cfg = data['config']
        yield 'Configuration is valid.'
        yield f"Mode  {cfg['mode']} | Proxy  {cfg['listen']} | Upstream  {cfg['upstream']}"
        yield f"{len(cfg['operations'])} configured operations | {len(cfg['cache']['routes'])} cache rules"
    elif command == 'doctor':
        yield f"Configuration is valid. Python {data['python']}."
        yield f"Upstream TCP check  {words(data['upstream_check'])}"
    elif command == 'status':
        yield f"Proxy is {'running' if data['running'] else 'stopped'} in {data['effective_mode']} mode."
        yield f"Listening on {data['listen']} | Upstream {data['upstream']}"
        yield f"Cache serving  {'active' if data.get('cache_serving_enabled') else 'inactive'}"
        yield f"Telemetry storage  {words(data.get('storage', {}).get('state'))} | Session {data['session_id']}"
    elif command == 'endpoints':
        yield route_stats(f"Last {data['window_seconds']} s", data['summary'])
        if data.get('window_may_be_incomplete'):
            yield 'In-memory history reached capacity. Window counts may be incomplete.'
        yield from show_rows([route_stats(name, row) for name, row in data['routes'].items()],
                             'No endpoint traffic in this window.')
    elif command == 'insights':
        state = data['state']
        yield f"Last {data['window_seconds']} s | {words(state).capitalize()}"
        rows = [f"{row['operation'] or 'Session'} | {words(row['code']).capitalize()}\n  {row['recommendation']}"
                for row in data['findings']]
        yield from show_rows(rows, 'No findings in this window.')
    elif command == 'cache':
        if data.get('cleared'):
            yield 'Stored responses cleared.'
        else:
            yield f"Mode  {data['effective_mode']} | Serving from cache  {'yes' if data['serving_enabled'] else 'no'}"
            rule = data['rule']
            yield from rule_lines([rule]) if rule else ['No cache rule matches this route.']
            if data.get('last_decision'):
                decision = data['last_decision']
                yield f"Last decision  {decision['outcome']} | {words(decision['reason'])}"
            if data.get('tuning'):
                yield f"Tuning  {words(data['tuning'].get('reason'))}"
    elif command == 'optimize':
        policy = data['policy']
        yield f"Saved configuration mode  {policy['mode']}"
        yield from rule_lines(policy['cache']['routes']) or ['No configured cache rules.']
        if data.get('revision'):
            yield 'This revision has a previous policy available for rollback.'
    elif command == 'sessions':
        rows = [f"{row['started_at']} | {row['session_id']} | " +
                (f"ended {row['ended_at']}" if row.get('ended_at') else 'shutdown unrecorded')
                for row in data['sessions']]
        yield from show_rows(rows, 'No saved sessions for this configuration.')
    elif command == 'report':
        yield f"{data['effective_mode'].capitalize()} session {data['session_id']} | Started {data['started_at']}"
        if data['sampling'].get('sampling_may_be_incomplete'):
            yield 'Shows the last committed checkpoint. Some observations may be missing.'
        rows = [route_stats(f"{row['boundary']} | {row['method']} {row['route']}", row) for row in data['routes']]
        yield from show_rows(rows, 'This session has no stored route metrics.')
        yield f"Tuning decisions recorded  {len(data.get('tuning_decisions', []))}"
    elif command == 'baseline':
        label = 'Capture saved' if data['state'] == 'completed' else f"Capture {words(data['state'])}"
        yield f"{label} | {data['findings']} findings"
        yield f"Report  {data['output']}"
