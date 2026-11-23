"""Bounded rolling observations and matched-route analysis."""
from collections import deque
from math import ceil
from time import time
from .contracts import ObservationStore


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, ceil(len(ordered) * fraction) - 1)] if ordered else None


class Metrics:
    def __init__(self, capacity=10000, clock=time, *, store: ObservationStore | None = None, observer=None):
        self.records = store if store is not None else deque(maxlen=capacity)
        if self.records.maxlen is None or self.records.maxlen <= 0:
            raise ValueError('Observation storage must have a positive finite capacity')
        self.clock = clock
        self.observer = observer
        self.evicted = 0
        self.total_recorded = 0

    def record(self, method, route, status, duration_ms, cache, **evidence):
        row = dict(timestamp=self.clock(), method=method, route=route,
                   status=status, duration_ms=duration_ms, cache=cache, **evidence)
        self.evicted += int(len(self.records) == self.records.maxlen)
        self.total_recorded += 1
        self.records.append(row)
        if self.observer is not None:
            self.observer(row)

    def snapshot(self, window=60):
        rows = [r for r in self.records if r['timestamp'] >= self.clock() - window]
        groups = {}
        for row in rows:
            groups.setdefault(f"{row['method']} {row['route']}", []).append(row)
        def summarize(items):
            values = [r['duration_ms'] for r in items]
            count = len(items)
            return dict(count=count, requests_per_second=count / window,
                        error_rate=sum(r['status'] >= 400 for r in items) / count if count else 0,
                        server_error_rate=sum(r['status'] >= 500 for r in items) / count if count else 0,
                        mean_ms=sum(values) / count if count else None,
                        p50_ms=percentile(values, .5), p95_ms=percentile(values, .95),
                        p99_ms=percentile(values, .99), cache_hits=sum(r['cache'] == 'HIT' for r in items))
        return dict(window_seconds=window, retained_samples=len(self.records),
                    capacity=self.records.maxlen, total_recorded=self.total_recorded,
                    evicted_samples=self.evicted, window_may_be_incomplete=bool(self.evicted),
                    summary=summarize(rows),
                    routes={key: summarize(value) for key, value in groups.items()})

    def comparison(self):
        groups = {}
        for row in self.records:
            if row['status'] == 200 and row['cache'] in ('HIT', 'MISS', 'BYPASS'):
                groups.setdefault(f"{row['method']} {row['route']}", []).append(row)
        result = {}
        for key, rows in groups.items():
            hits = [r['duration_ms'] for r in rows if r['cache'] == 'HIT']
            origin = [r['duration_ms'] for r in rows if r['cache'] != 'HIT']
            result[key] = dict(cached_samples=len(hits), origin_samples=len(origin),
                               improvement_percent=(1 - (sum(hits) / len(hits)) / (sum(origin) / len(origin))) * 100
                               if hits and origin and sum(origin) > 0 else None)
        return result

    def regressions(self, minimum=5, ratio=1.5, minimum_increase_ms=5, window=300):
        # Disjoint consecutive origin cohorts avoid cache-hit dilution.
        groups = {}
        cutoff = self.clock() - window
        for row in self.records:
            if row['timestamp'] >= cutoff and row['cache'] != 'HIT' and row['status'] < 400:
                groups.setdefault(f"{row['method']} {row['route']}", []).append(row['duration_ms'])
        result = []
        for route, values in groups.items():
            if len(values) >= minimum * 2:
                baseline = sum(values[-minimum * 2:-minimum]) / minimum
                recent = sum(values[-minimum:]) / minimum
                if baseline > 0 and recent >= baseline * ratio and recent - baseline >= minimum_increase_ms:
                    result.append(dict(route=route, baseline_ms=baseline, recent_ms=recent,
                                       ratio=recent / baseline, increase_ms=recent - baseline,
                                       minimum_increase_ms=minimum_increase_ms, window_seconds=window,
                                       samples_per_cohort=minimum))
        return result
