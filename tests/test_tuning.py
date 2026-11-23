"""Feedback-loop and contention tests; no wall-clock latency claims."""
import asyncio
from threading import Barrier, Event

import pytest

from tuneio.cache import AdaptiveCache, CacheConfig


def test_independent_reads_and_off_mode_are_parallel():
    async def scenario():
        for mode, keys in [('adaptive', ['a', 'b']), ('off', ['same', 'same'])]:
            cache = AdaptiveCache()
            cache.mode = mode
            barrier = Barrier(2, timeout=3)
            def loader():
                barrier.wait()
                return 'ok'
            results = await asyncio.gather(*(cache.read('/x', key, loader) for key in keys))
            assert all(value == 'ok' for value, _ in results)
            assert cache.counters['origin_loads'] == 2
            assert not cache.key_locks
    asyncio.run(scenario())


def test_hot_hit_does_not_queue_behind_slow_origin():
    async def scenario():
        cache = AdaptiveCache(CacheConfig(origin_concurrency=1))
        cache.policy('/hot')['enabled'] = True
        cache.entries['hot'] = (cache.clock() + 30, 'hot data', '/hot')
        started, release = Event(), Event()
        def loader():
            started.set()
            assert release.wait(3)
            return 'cold data'
        cold = asyncio.create_task(cache.read('/cold', 'cold', loader))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert await asyncio.wait_for(cache.read('/hot', 'hot', lambda: pytest.fail('unexpected origin')), .5) == ('hot data', 'HIT')
        finally:
            release.set()
            await cold
    asyncio.run(scenario())


def test_automatic_activation_ttl_reduction_and_disable():
    async def scenario():
        now = [0.0]
        cache = AdaptiveCache(CacheConfig(cooldown=0), lambda: now[0])
        def slow():
            now[0] += .02
            return 'same data'
        for _ in range(4):
            await cache.read('/x', 'key', slow)
        assert cache.policy('/x')['enabled']
        assert cache.policy('/x')['ttl'] == pytest.approx(20)
        assert (await cache.read('/x', 'key', slow))[1] == 'HIT'
        assert cache.counters['origin_loads'] == 4
        def fast():
            now[0] += .0001
            return 'same data'
        for _ in range(20):
            now[0] += 31  # expire and probe a genuinely faster origin
            await cache.read('/x', 'key', fast)
        assert not cache.policy('/x')['enabled']
        assert cache.policy('/x')['ttl'] == 2
        assert cache.decisions[-1]['enabled'] is False
        assert all(d['automatic'] for d in cache.decisions)
    asyncio.run(scenario())


def test_queue_delay_does_not_enable_fast_origin():
    async def scenario():
        now = [0.0]
        cache = AdaptiveCache(CacheConfig(minimum_samples=1, origin_concurrency=1), lambda: now[0])
        def fast():
            now[0] += .001
            return 'data'
        await cache.read('/x', 'k', fast)
        await cache.origin_slots.acquire()
        task = asyncio.create_task(cache.read('/x', 'k', fast))
        await asyncio.sleep(0)
        now[0] += 100
        cache.origin_slots.release()
        await task
        assert cache.counters['queue_ms'] >= 100000
        assert cache.counters['origin_ms'] == pytest.approx(2)
        assert not cache.policy('/x')['enabled']
    asyncio.run(scenario())


def test_generation_prevents_old_inflight_fill_and_preserves_other_routes():
    async def scenario():
        cache = AdaptiveCache()
        cache.policy('/api/products')['enabled'] = True
        cache.entries['users'] = (cache.clock() + 30, ['user'], '/api/users')
        started, release = Event(), Event()
        def old_read():
            started.set()
            assert release.wait(3)
            return 'old snapshot'
        read = asyncio.create_task(cache.read('/api/products', 'products', old_read))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            cache.invalidate(('/api/products', '/api/reports'))
        finally:
            release.set()
        await read
        assert 'products' not in cache.entries
        assert 'users' in cache.entries
        assert not cache.key_locks
    asyncio.run(scenario())


def test_route_failure_does_not_flush_another_route():
    cache = AdaptiveCache(CacheConfig(minimum_samples=1, cooldown=0))
    cache.policy('/x')['enabled'] = True
    cache.entries['x'] = (cache.clock() + 30, 'x', '/x')
    cache.entries['y'] = (cache.clock() + 30, 'y', '/y')
    cache.observe('/x', 'x', 10, failed=True)
    assert 'x' not in cache.entries and 'y' in cache.entries


def test_hysteresis_and_reuse_window():
    now = [0]
    cache = AdaptiveCache(CacheConfig(minimum_samples=1, cooldown=0), lambda: now[0])
    cache.observe('/x', 'k', 2.1)
    cache.observe('/x', 'k', 2.1)
    for _ in range(20):
        cache.observe('/x', 'k', 1.8)
    assert cache.policy('/x')['enabled']  # retained above 1.5 ms lower threshold
    now[0] = 61
    cache.observe('/x', 'k', 2.1)
    assert not cache.policy('/x')['enabled']  # old reuse is no longer evidence


@pytest.mark.parametrize('kwargs', [{'origin_concurrency': 0}, {'slow_ms': 0}, {'ttl_max': 1}])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        CacheConfig(**kwargs)


def test_ttl_adjustment_clamps_only_its_route_and_status_prunes_expiry():
    now = [0]
    cache = AdaptiveCache(CacheConfig(minimum_samples=1, cooldown=0), lambda: now[0])
    cache.observe('/x', 'x', 10)
    cache.observe('/x', 'x', 10)
    cache.entries['x'] = (100, 'x', '/x')
    cache.entries['y'] = (100, 'y', '/y')
    cache.observe('/x', 'x', 20)
    assert cache.entries['x'][0] == pytest.approx(40 / 3)
    assert cache.entries['y'][0] == 100
    assert cache.decisions[-1]['action'] == 'adjust_ttl'
    now[0] = 20
    assert cache.status()['entries'] == 1
    assert 'y' in cache.entries


def test_submillisecond_origin_timing_is_preserved():
    async def scenario():
        ticks = iter([100, 100.0001234])
        cache = AdaptiveCache(clock=lambda: next(ticks))
        value, error, duration = await cache.measure_origin(lambda: 'data')
        assert value == 'data' and error is None
        assert duration == pytest.approx(.1234)
    asyncio.run(scenario())


def test_policy_reasons_explain_state_and_cooldown():
    now = [0]
    cache = AdaptiveCache(CacheConfig(minimum_samples=2), lambda: now[0])
    cache.observe('/x', 'key', 10)
    assert 'minimum' in cache.status()['policies']['/x']['reason']
    cache.observe('/x', 'key', 10)
    assert 'Slow origin' in cache.status()['policies']['/x']['reason']
    cache.observe('/x', 'new-key', 10)
    assert 'cooldown' in cache.status()['policies']['/x']['reason']
    now[0] += 6
    cache.observe('/x', 'other-key', 10)
    assert 'reuse' in cache.status()['policies']['/x']['reason']
    cache.observe('/x', 'other-key', 10, failed=True)
    assert 'failures' in cache.status()['policies']['/x']['reason']
