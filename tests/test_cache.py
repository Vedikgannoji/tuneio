import asyncio
import pytest
from tuneio.cache import AdaptiveCache, CacheConfig


def test_policy_hysteresis_errors_and_recovery():
    now = [0]
    cache = AdaptiveCache(CacheConfig(minimum_samples=2, slow_ms=5, cooldown=10), lambda: now[0])
    cache.observe('/x', 'k', 10)
    assert not cache.policy('/x')['enabled']
    cache.observe('/x', 'k', 10)
    assert cache.policy('/x')['enabled']
    cache.observe('/x', 'k', 1)
    assert cache.policy('/x')['enabled']
    now[0] = 11
    cache.observe('/x', 'k', 20)
    assert cache.policy('/x')['ttl'] > 2
    cache.observe('/x', 'k', 10, failed=True)
    assert not cache.policy('/x')['enabled']
    now[0] += 11
    for _ in range(20):
        cache.observe('/x', 'k', 10)
    assert cache.policy('/x')['enabled']
    now[0] += 11
    for _ in range(20):
        cache.observe('/x', 'k', .01)
    now[0] += 11  # TTL adjustment also starts the cooldown.
    cache.observe('/x', 'k', .01)
    assert not cache.policy('/x')['enabled']


def test_low_reuse_and_bounds():
    cache = AdaptiveCache(CacheConfig(capacity=2, minimum_samples=2, slow_ms=1, cooldown=0))
    for k in range(10):
        cache.observe('/x', k, 1000)
    assert not cache.policy('/x')['enabled']
    assert len(cache.reuse) == 2
    cache.observe('/x', 9, 1000)
    assert cache.policy('/x')['ttl'] == 30


def test_expiry_lru_coalescing_and_failures():
    async def scenario():
        now = [0]
        cache = AdaptiveCache(CacheConfig(capacity=2, minimum_samples=1, slow_ms=1, cooldown=0), lambda: now[0])
        calls = [0]
        def loader():
            calls[0] += 1
            now[0] += .01
            return {'value': calls[0]}
        await cache.read('/x', 'a', loader)
        await cache.read('/x', 'a', loader)
        assert cache.policy('/x')['enabled']
        result = await asyncio.gather(*(cache.read('/x', 'a', loader) for _ in range(10)))
        assert all(r[1] == 'HIT' for r in result)
        assert calls[0] == 2
        cache.invalidate()
        cold = await asyncio.gather(*(cache.read('/x', 'a', loader) for _ in range(10)))
        assert calls[0] == 3
        assert [r[1] for r in cold].count('MISS') == 1
        assert [r[1] for r in cold].count('HIT') == 9
        now[0] += 40
        assert (await cache.read('/x', 'a', loader))[1] == 'MISS'
        for key in ('b', 'c'):
            await cache.read('/x', key, loader)
            await cache.read('/x', key, loader)
        assert len(cache.entries) <= 2
        assert 'a' not in cache.entries
        cache.mode = 'off'
        assert (await cache.read('/x', 'c', loader))[1] == 'BYPASS'
        def broken():
            raise RuntimeError('origin failure')
        with pytest.raises(RuntimeError):
            await cache.read('/x', 'c', broken)
        assert not cache.policy('/x')['enabled']
        cache.invalidate()
        assert not cache.entries
    asyncio.run(scenario())


def test_entry_capacity_without_policy_changes():
    async def scenario():
        now = [0]
        cache = AdaptiveCache(CacheConfig(capacity=2, minimum_samples=1, cooldown=100), lambda: now[0])
        cache.policy('/x').update(enabled=True, changed=0)
        for key in ('a', 'b', 'c'):
            await cache.read('/x', key, lambda: key)
        assert list(cache.entries) == ['b', 'c']
    asyncio.run(scenario())
