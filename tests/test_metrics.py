import pytest
from tuneio.metrics import Metrics, percentile


def test_exact_aggregates_and_windows():
    now = [100]
    metrics = Metrics(capacity=5, clock=lambda: now[0])
    assert metrics.snapshot()['summary']['p95_ms'] is None
    for duration, status in [(1, 200), (2, 200), (3, 404), (4, 500)]:
        metrics.record('GET', '/x', status, duration, 'MISS')
    summary = metrics.snapshot(10)['summary']
    assert summary == dict(count=4, requests_per_second=.4, error_rate=.5, server_error_rate=.25,
                           mean_ms=2.5, p50_ms=2, p95_ms=4, p99_ms=4, cache_hits=0)
    assert metrics.comparison()['GET /x']['improvement_percent'] is None
    metrics.record('GET', '/x', 200, .5, 'HIT')
    assert metrics.comparison()['GET /x']['improvement_percent'] == pytest.approx(66.6666667)
    metrics.record('GET', '/x', 200, 1, 'HIT')
    assert len(metrics.records) == 5
    now[0] += 11
    assert metrics.snapshot(10)['summary']['count'] == 0
    assert percentile([], .5) is None


def test_regression_requires_disjoint_origin_samples():
    metrics = Metrics()
    for _ in range(5):
        metrics.record('GET', '/x', 200, 10, 'MISS')
    assert metrics.regressions() == []
    for _ in range(5):
        metrics.record('GET', '/x', 200, 20, 'MISS')
        metrics.record('GET', '/x', 200, .1, 'HIT')
    assert metrics.regressions()[0]['ratio'] == 2
    for _ in range(5):
        metrics.record('GET', '/x', 200, 10, 'MISS')
    assert not metrics.regressions()


def test_zero_duration_comparison():
    metrics = Metrics()
    metrics.record('GET', '/x', 200, 0, 'MISS')
    metrics.record('GET', '/x', 200, 0, 'HIT')
    assert metrics.comparison()['GET /x']['improvement_percent'] is None


@pytest.mark.parametrize('baseline,recent,alert', [(3.04, 4.68, False), (1, 5, False),
                                                  (10, 15, True), (100, 110, False)])
def test_regression_requires_relative_and_absolute_increase(baseline, recent, alert):
    metrics = Metrics()
    for value in [baseline] * 5 + [recent] * 5:
        metrics.record('GET', '/x', 200, value, 'MISS')
    assert bool(metrics.regressions()) is alert


def test_regression_alerts_expire():
    now = [100]
    metrics = Metrics(clock=lambda: now[0])
    for value in [10] * 5 + [20] * 5:
        metrics.record('GET', '/x', 200, value, 'MISS')
    assert metrics.regressions()[0]['increase_ms'] == 10
    now[0] += 301
    assert not metrics.regressions()
