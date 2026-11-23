"""Compare matched, completed read workloads from two proxy sessions."""
import re

from ._version import __version__
from .artifacts import read_json, write_new
from .core.metrics import percentile

HEX = re.compile(r'[0-9a-f]{64}')


def fingerprint(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def cohort(report, stage):
    workload = report.get('workload')
    result = report.get('active_result')
    if (type(report.get('schema_version')) is not int or report['schema_version'] != 1
            or report.get('kind') != 'active' or report.get('stage') != stage or report.get('state') != 'completed'
            or not isinstance(workload, dict) or not isinstance(result, dict)):
        return None
    rows = result.get('samples')
    count, warmup = workload.get('requests'), workload.get('warmup')
    if (type(count) is not int or type(warmup) is not int or not 0 <= warmup < 20 or not 1 <= count <= 20-warmup
            or workload.get('method') != 'GET' or workload.get('concurrency') != 1
            or workload.get('expected_status') != 200 or not fingerprint(workload.get('app_state_digest'))
            or result.get('state') != 'completed' or result.get('completed') != count or result.get('errors') != 0
            or result.get('warmup_completed') != warmup or not isinstance(rows, list) or len(rows) != count
            or type(result.get('elapsed_seconds')) not in (int, float) or not 0 < result['elapsed_seconds'] <= 120):
        return None
    if any(not isinstance(row, dict) or row.get('passed') is not True or row.get('status') != 200
           or row.get('cache') not in ('HIT', 'MISS', 'BYPASS') or not fingerprint(row.get('response_sha256'))
           or type(row.get('duration_ms')) not in (float, int) or not 0 <= row['duration_ms'] <= 120000
           for row in rows):
        return None
    return rows


def statistics(rows, elapsed):
    durations = [row['duration_ms'] for row in rows]
    return dict(requests=len(rows), errors=0, hits=sum(row['cache'] == 'HIT' for row in rows),
                elapsed_seconds=elapsed, requests_per_second=len(rows)/elapsed,
                mean_ms=sum(durations)/len(rows), p50_ms=percentile(durations, .5),
                p95_ms=percentile(durations, .95), p99_ms=percentile(durations, .99))


def compare(before, after):
    reasons = []
    baseline = cohort(before, 'baseline')
    optimized = cohort(after, 'optimized')
    if baseline is None:
        reasons.append('baseline_missing_completed_matched_workload')
    if optimized is None:
        reasons.append('optimized_missing_completed_matched_workload')
    for key in ('comparison_context', 'proxy_source_sha256', 'client_source_sha256', 'workload_digest'):
        if not fingerprint(before.get(key)) or before[key] != after.get(key):
            reasons.append('different_or_missing_' + key)
    if (not isinstance(before.get('session_id'), str) or not before['session_id']
            or not isinstance(after.get('session_id'), str) or not after['session_id']
            or before['session_id'] == after['session_id']):
        reasons.append('distinct_proxy_sessions_required')
    if before.get('workload') != after.get('workload'):
        reasons.append('request_plan_or_application_state_differs')
    if baseline and optimized:
        if len({row['response_sha256'] for row in baseline + optimized}) != 1:
            reasons.append('response_content_changed')
        if any(row['cache'] != 'BYPASS' for row in baseline):
            reasons.append('baseline_cache_was_active')
    output = dict(schema_version=1, tool_version=__version__, kind='matched-read-comparison',
                  comparable=not reasons, reasons=reasons, baseline_id=before.get('baseline_id'),
                  optimized_id=after.get('baseline_id'), boundary='explicit scenario through proxy completion',
                  limitations='Sequential repeated GETs. External traffic, upstream warm state and host load can affect timings. '
                              'Elapsed time includes pacing. Rolling telemetry is excluded.')
    if reasons:
        return output
    old, new = (statistics(rows, report['active_result']['elapsed_seconds'])
                for rows, report in ((baseline, before), (optimized, after)))
    output.update(workload=before['workload'], baseline=old, optimized=new,
                  optimized_cache_hit_observed=new['hits'] > 0,
                  latency_reduction_percent={key: 100 * (1-new[key]/old[key]) if old[key] > 0 else None
                                             for key in ('mean_ms','p50_ms','p95_ms','p99_ms')})
    return output


def compare_files(before, after, output=None):
    reports = [read_json(path, 32 * 1024 * 1024)[0] for path in (before, after)]
    result = compare(*reports)
    if output is not None:
        write_new(output, result)
    return result


def display_comparison(result):
    if not result['comparable']:
        return 'Captures are incomparable\n' + '\n'.join('  '+reason.replace('_',' ') for reason in result['reasons'])
    baseline, optimized = result['baseline'], result['optimized']
    lines = ['tuneio / MATCHED READ COMPARISON',
             f"{'Latency':<9} {'Baseline':>12} {'Optimized':>12} {'Reduction':>12}"]
    for label in ('mean','p50','p95','p99'):
        key = label+'_ms'
        reduction = result['latency_reduction_percent'][key]
        delta = f'{reduction:+.1f}%' if reduction is not None else 'unavailable'
        lines.append(f'{label:<9} {baseline[key]:>9.2f} ms {optimized[key]:>9.2f} ms {delta:>12}')
    lines.extend([f"{optimized['requests']} measured reads per capture. Optimized cache hits {optimized['hits']}.",
                  result['limitations']])
    return '\n'.join(lines)
