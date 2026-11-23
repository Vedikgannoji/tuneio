import asyncio
from itertools import count
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, Response
import httpx
import pytest

from tuneio.core import AdaptiveCache, CacheConfig, Metrics, Monitoring, PerformanceEngine


@pytest.fixture(params=['fastapi_catalog', 'plain_asgi_inventory'])
def independent_backend(request):
    engine = PerformanceEngine(CacheConfig(slow_ms=.00001, cooldown=0))
    ticks = count()
    engine.cache.clock = lambda: next(ticks) / 1000
    state = {'value': 'original'}
    if request.param == 'fastapi_catalog':
        backend = FastAPI()
        prefix = '/catalog'

        @backend.get('/catalog/{item}')
        async def read(item: str, request: Request, response: Response):
            result = await engine.read('/catalog/{item}', request.url.path, request.query_params.multi_items(),
                                       lambda: dict(state), credentialed=bool(request.headers.get('cookie')))
            request.state.cache = result.outcome
            response.headers['X-Cache'] = result.outcome
            return result.value

        @backend.post('/catalog/{item}')
        async def write(item: str, body: dict):
            return await engine.write(lambda: state.update(body) or dict(state), invalidate=('/catalog',))

        app = Monitoring(backend, engine.metrics,
                         route_label=lambda scope: scope['route'].path if scope.get('route') else '/{unmatched}')
    else:
        prefix = '/inventory'

        async def backend(scope, receive, send):
            scope['state'] = {}
            scope['operation'] = '/inventory/{item}'
            headers = dict(scope['headers'])
            if scope['method'] == 'POST':
                raw = b''
                while True:
                    message = await receive()
                    raw += message.get('body', b'')
                    if not message.get('more_body'):
                        break
                body = json.loads(raw)
                value = await engine.write(lambda: state.update(body) or dict(state), invalidate=('/inventory',))
                outcome = 'NONE'
            else:
                result = await engine.read('/inventory/{item}', scope['path'],
                                           parse_qsl(scope['query_string'].decode(), keep_blank_values=True),
                                           lambda: dict(state), credentialed=b'cookie' in headers)
                value, outcome = result.value, result.outcome
            scope['state']['cache'] = outcome
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'application/json'), (b'x-cache', outcome.encode())]})
            await send({'type': 'http.response.body', 'body': json.dumps(value).encode()})

        app = Monitoring(backend, engine.metrics, route_label=lambda scope: scope['operation'])
    return app, engine, prefix


def test_same_core_observes_and_caches_independent_backends(independent_backend):
    app, engine, prefix = independent_backend
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for _ in range(6):
                response = await client.get(prefix + '/widget')
            assert response.headers['x-cache'] == 'HIT'
            assert engine.metrics.snapshot()['summary']['count'] == 6
            assert (await client.post(prefix + '/widget', json={'value': 'updated'})).status_code == 200
            fresh = await client.get(prefix + '/widget')
            assert fresh.json() == {'value': 'updated'}
            assert fresh.headers['x-cache'] == 'MISS'
            private = await client.get(prefix + '/widget', headers={'cookie': 'private=value'})
            assert private.headers['x-cache'] == 'BYPASS'
            assert engine.cache.counters['credential_bypasses'] == 1
            assert set(engine.metrics.snapshot()['routes']) == {f'GET {prefix}/{{item}}', f'POST {prefix}/{{item}}'}
    asyncio.run(scenario())


def test_core_imports_without_demo_or_web_framework():
    script = "import sys; import tuneio.core; assert not {'fastapi','starlette','tuneio.main','tuneio.database','tuneio.schemas'} & set(sys.modules)"
    subprocess.run([sys.executable, '-c', script], check=True, cwd=Path(__file__).resolve().parents[1])


def test_generic_invalidation_respects_path_boundaries():
    cache = AdaptiveCache()
    for path in ['/catalog', '/catalog/item', '/catalog-extra', '/other']:
        cache.entries[path] = (cache.clock() + 30, path, path)
    cache.invalidate(('/catalog',))
    assert set(cache.entries) == {'/catalog-extra', '/other'}


def test_injected_engine_services_and_disabled_outcome():
    cache, metrics = AdaptiveCache(), Metrics()
    engine = PerformanceEngine(cache=cache, metrics=metrics)
    assert engine.cache is cache and engine.metrics is metrics
    cache.mode = 'off'
    async def scenario():
        result = await engine.read('/other/{id}', '/other/4', [('a', '2'), ('a', '1')], lambda: 42)
        assert (result.value, result.outcome, result.reason) == (42, 'BYPASS', 'cache-disabled')
    asyncio.run(scenario())


def test_failed_write_invalidates_and_preserves_exception():
    engine = PerformanceEngine()
    engine.cache.entries['key'] = (engine.cache.clock() + 30, 'old', '/other')
    def broken():
        raise ValueError('write failed')
    async def scenario():
        with pytest.raises(ValueError, match='write failed'):
            await engine.write(broken, invalidate=('/other',))
        assert not engine.cache.entries and not engine.cache.lock.locked()
    asyncio.run(scenario())


def test_cancelled_failing_worker_preserves_cancellation():
    engine = PerformanceEngine()
    started, release = Event(), Event()
    def broken():
        started.set()
        assert release.wait(3)
        raise ValueError('worker failed after cancellation')
    async def scenario():
        task = asyncio.create_task(engine.write(broken, invalidate=('/other',)))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)  # scheduling checkpoint, not an elapsed-time assertion
            assert engine.cache.lock.locked()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not engine.cache.lock.locked()
    asyncio.run(scenario())


def test_monitoring_defaults_custom_sink_and_failure():
    class Sink:
        def __init__(self): self.rows = []
        def record(self, *args): self.rows.append(args)
    sink = Sink()
    async def broken(scope, receive, send):
        raise RuntimeError('origin failed')
    ticks = iter([1, 1.002])
    monitor = Monitoring(broken, sink, clock=lambda: next(ticks))
    async def scenario():
        with pytest.raises(RuntimeError):
            await monitor({'type': 'http', 'method': 'GET', 'path': '/random-id'}, None, None)
    asyncio.run(scenario())
    assert sink.rows[0][:3] == ('GET', '/{unmatched}', 500)
    assert sink.rows[0][3] == pytest.approx(2)


def test_bounded_observation_storage_injection():
    from collections import deque
    store = deque(maxlen=2)
    metrics = Metrics(store=store)
    for _ in range(3):
        metrics.record('GET', '/other', 200, 2, 'MISS')
    assert metrics.records is store
    assert metrics.snapshot()['retained_samples'] == metrics.snapshot()['capacity'] == 2
    with pytest.raises(ValueError, match='finite capacity'):
        Metrics(store=deque())
    with pytest.raises(ValueError):
        Metrics(capacity=0)
