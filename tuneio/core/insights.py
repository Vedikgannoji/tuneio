"""Deterministic advisory analysis over bounded, recent observation evidence."""
from collections import Counter
from datetime import datetime, timezone
from time import time

from .metrics import percentile

THRESHOLDS = dict(minimum_samples=5, slow_mean_ms=100, tail_minimum_samples=20, tail_p95_ms=250,
                  regression_cohort_samples=5, regression_ratio=1.5, regression_increase_ms=5,
                  regression_window_seconds=300, error_rate=.1, queue_p95_ms=5, queue_fraction=.25,
                  cache_minimum_samples=4, cache_origin_mean_ms=2, reuse_window_seconds=60,
                  reuse_minimum=2, cache_cohort_samples=20)


def grouped(rows):
    result = {}
    for row in rows:
        result.setdefault((row['method'], row['route']), []).append(row)
    return result


def successful_origin(row):
    return row['cache'] != 'HIT' and 200 <= row['status'] < 400


def analyze(config, proxy_metrics, upstream_metrics, *, transport=None, storage=None, window=300, clock=time):
    if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= 3600:
        raise ValueError('Window must be 1-3600 seconds')
    now = clock()
    thresholds = dict(THRESHOLDS)
    proxy_rows = [r for r in proxy_metrics.records if now-window <= r['timestamp'] <= now]
    upstream_rows = [r for r in upstream_metrics.records if now-window <= r['timestamp'] <= now]
    proxy_groups, upstream_groups = grouped(proxy_rows), grouped(upstream_rows)
    findings, evaluations, suitability = [], [], []

    def finding(code, operation, boundary, evidence, limits, recommendation, severity='warning'):
        findings.append(dict(code=code, severity=severity, operation=operation, boundary=boundary,
                             evidence=evidence, thresholds=limits, recommendation=recommendation,
                             applied=False))

    for method, route in sorted(proxy_groups.keys() | upstream_groups.keys()):
        operation = method+' '+route
        local = proxy_groups.get((method, route), [])
        origin = upstream_groups.get((method, route), [])
        success = sorted((r for r in origin if successful_origin(r)), key=lambda r:r['timestamp'])
        values = [r['duration_ms'] for r in success]
        mean = sum(values)/len(values) if values else None
        p95 = percentile(values, .95)
        recent = [r for r in success if r['timestamp'] >= now-min(window, thresholds['regression_window_seconds'])]
        cohorts = thresholds['regression_cohort_samples']
        evaluations.append(dict(operation=operation, proxy_samples=len(local), upstream_samples=len(origin),
                                successful_origin_samples=len(success), origin_mean_ms=mean, origin_p95_ms=p95,
                                regression_samples=len(recent), regression_required_samples=cohorts*2,
                                latency_minimum_samples=thresholds['minimum_samples']))
        if len(values) >= thresholds['minimum_samples'] and mean >= thresholds['slow_mean_ms']:
            finding('slow_upstream', operation, 'upstream', dict(samples=len(values), mean_ms=mean, p95_ms=p95),
                    dict(minimum_samples=thresholds['minimum_samples'], mean_ms=thresholds['slow_mean_ms']),
                    'Profile this upstream operation for database, CPU or I/O work; timing alone does not identify its cause.')
        if len(values) >= thresholds['tail_minimum_samples'] and p95 >= thresholds['tail_p95_ms']:
            finding('upstream_tail_latency', operation, 'upstream', dict(samples=len(values), p95_ms=p95),
                    dict(minimum_samples=thresholds['tail_minimum_samples'], p95_ms=thresholds['tail_p95_ms']),
                    'Inspect slow upstream requests and dependency variability with tracing before changing limits.')
        if len(recent) >= cohorts*2:
            baseline = sum(r['duration_ms'] for r in recent[-cohorts*2:-cohorts])/cohorts
            current = sum(r['duration_ms'] for r in recent[-cohorts:])/cohorts
            if baseline > 0 and current >= baseline*thresholds['regression_ratio'] and current-baseline >= thresholds['regression_increase_ms']:
                finding('upstream_regression', operation, 'upstream',
                        dict(baseline_ms=baseline, recent_ms=current, ratio=current/baseline,
                             increase_ms=current-baseline, samples_per_cohort=cohorts,
                             window_seconds=min(window, thresholds['regression_window_seconds'])),
                        dict(ratio=thresholds['regression_ratio'], increase_ms=thresholds['regression_increase_ms'], samples_per_cohort=cohorts),
                        'Compare recent upstream changes and request variants; disjoint observational cohorts do not establish causation.')
        if len(local) >= thresholds['minimum_samples']:
            errors = sum(400 <= r['status'] < 600 for r in local)
            if errors/len(local) >= thresholds['error_rate']:
                finding('operation_errors', operation, 'proxy', dict(samples=len(local), errors=errors, error_rate=errors/len(local),
                         server_errors=sum(500 <= r['status'] < 600 for r in local)),
                        dict(minimum_samples=thresholds['minimum_samples'], error_rate=thresholds['error_rate']),
                        'Inspect API validation, conflicts and upstream availability. Expected workflow errors remain included.')
            waits = [r['queue_ms'] for r in local if 'queue_ms' in r]
            if len(waits) == len(local):
                duration = sum(r['duration_ms'] for r in local)
                fraction = sum(waits)/duration if duration else 0
                queue_p95 = percentile(waits, .95)
                if queue_p95 >= thresholds['queue_p95_ms'] and fraction >= thresholds['queue_fraction']:
                    finding('admission_pressure', operation, 'queue', dict(samples=len(waits), p95_queue_ms=queue_p95, queue_fraction=fraction),
                            dict(minimum_samples=thresholds['minimum_samples'], p95_queue_ms=thresholds['queue_p95_ms'], queue_fraction=thresholds['queue_fraction']),
                            'Inspect upstream capacity and latency before adjusting the concurrency or admission limits.')
        if method == 'GET':
            eligible = [r for r in origin if r.get('cache_review_reason') == 'review_required' and r['status']==200 and r['cache']!='HIT']
            cohort = sorted(eligible, key=lambda r:r['timestamp'])[-thresholds['cache_cohort_samples']:]
            cost = sum(r['duration_ms'] for r in cohort)/len(cohort) if cohort else None
            keys = Counter(r['request_key'] for r in cohort if r['timestamp'] >= now-min(window, thresholds['reuse_window_seconds']) and r.get('request_key'))
            reuse = max(keys.values(), default=0)
            parts = route.split('/')
            rules = [rule for rule in config.cache.routes if len(rule.match.split('/')) == len(parts)
                     and all(a == b or a == '*' for a,b in zip(rule.match.split('/'),parts))]
            rule = min(rules,key=lambda r:r.match.count('*')) if rules else None
            reasons = Counter(r.get('cache_review_reason','not_measured') for r in origin)
            if not origin:
                state = 'no_completed_upstream_samples'
            elif not eligible:
                state = 'no_screened_public_reads'
            elif any(r['status'] >= 400 for r in origin[-thresholds['cache_cohort_samples']:]):
                state = 'upstream_errors'
            elif len(cohort) < thresholds['cache_minimum_samples']:
                state = 'insufficient_samples'
            elif cost < thresholds['cache_origin_mean_ms']:
                state = 'fast_origin'
            elif reuse < thresholds['reuse_minimum']:
                state = 'insufficient_key_reuse'
            elif rule is None:
                state = 'explicit_rule_required'
            else:
                state = 'policy_review_candidate'
            suitability.append(dict(operation=operation, state=state, samples=len(cohort), origin_mean_ms=cost,
                                    maximum_same_key_observations=reuse, reuse_window_seconds=min(window,thresholds['reuse_window_seconds']),
                                    screening_reasons=dict(sorted(reasons.items())), rule_id=rule.id if rule else None,
                                    approved_for_caching=False, response_cache_serving_enabled=bool(config.mode in ('cache','adaptive') and rule and rule.policy=='cache')))
            if state in ('explicit_rule_required','policy_review_candidate'):
                finding('cache_policy_review', operation, 'upstream', dict(samples=len(cohort), mean_ms=cost, same_key_observations=reuse, state=state),
                        dict(minimum_samples=thresholds['cache_minimum_samples'], mean_ms=thresholds['cache_origin_mean_ms'],
                             same_key_observations=thresholds['reuse_minimum'], reuse_window_seconds=min(window,thresholds['reuse_window_seconds'])),
                        'Review public-data eligibility, HTTP freshness, variants and mutation invalidation before approving a cache rule. No cache change has been applied.', 'info')
    health = storage or {}
    losses = {key:health.get(key,0) for key in ('dropped_queue','dropped_storage','retention_truncated','budget_truncated','dropped_tuning_decisions')}
    evictions = proxy_metrics.evicted + upstream_metrics.evicted
    if evictions or any(losses.values()) or health.get('state') == 'degraded':
        finding('telemetry_quality', None, 'session', dict(raw_evictions=evictions, storage_state=health.get('state','unknown'), **losses),
                dict(any_loss_or_degraded_state=True), 'Treat findings as partial evidence; inspect retention, writer queue and disk health.', 'info')
    counters = transport or {}
    if counters.get('failed_requests',0) or counters.get('incomplete_responses',0) or counters.get('overload_rejections',0):
        finding('transport_failures', None, 'session',
                {key:counters.get(key,0) for key in ('failed_requests','incomplete_responses','overload_rejections')},
                dict(minimum_events=1, scope='lifetime since session start'),
                'Check upstream connectivity and proxy capacity. Lifetime counters do not imply a current regression.')
    findings.sort(key=lambda f:(f['severity']!='warning', f['operation'] or '', f['code']))
    state = ('findings_available' if findings else 'no_recent_traffic' if not proxy_rows and not upstream_rows
             else 'collecting_samples' if all(e['successful_origin_samples']<thresholds['minimum_samples'] and e['proxy_samples']<thresholds['minimum_samples'] for e in evaluations)
             else 'no_thresholds_exceeded')
    return dict(schema_version=1, generated_at=datetime.fromtimestamp(now,timezone.utc).isoformat(), window_seconds=window,
                state=state, effective_mode=config.mode, recommendations_applied=False, thresholds=thresholds,
                evidence_quality=dict(proxy_samples=len(proxy_rows), upstream_samples=len(upstream_rows),
                    raw_evictions=evictions, window_may_be_incomplete=bool(evictions),
                    storage_sampling_incomplete=bool(health.get('sampling_incomplete')), history='live bounded raw observations; reset on restart'),
                findings=findings, evaluations=evaluations, cache_suitability=suitability)
