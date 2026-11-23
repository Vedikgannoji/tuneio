import sqlite3
import pytest
from tuneio.main import create_app


def seed(client):
    user = client.post('/api/users', json={'name': 'Vedik', 'email': 'vedik@example.com'})
    product = client.post('/api/products', json={'name': 'Keyboard', 'price_cents': 1500})
    assert user.status_code == product.status_code == 201
    order = client.post('/api/orders', json={'user_id': user.json()['id'], 'product_id': product.json()['id'], 'quantity': 2})
    assert order.status_code == 201
    return user.json(), product.json(), order.json()


def test_complete_business_flow(client, app):
    u, p, o = seed(client)
    for collection in ('users', 'products', 'orders'):
        assert len(client.get(f'/api/{collection}').json()) == 1
        assert client.get(f'/api/{collection}/1').json()['id'] == 1
        assert client.get(f'/api/{collection}?offset=1').json() == []
    assert client.patch('/api/users/1', json={'name': 'Updated'}).json()['name'] == 'Updated'
    assert client.patch('/api/products/1', json={'price_cents': 3000}).status_code == 200
    # Existing order keeps its original unit price.
    assert client.get('/api/orders/1').json()['unit_price_cents'] == 1500
    assert client.patch('/api/orders/1/status', json={'status': 'paid'}).status_code == 200
    assert client.patch('/api/orders/1/status', json={'status': 'paid'}).status_code == 200
    assert client.get('/api/reports/sales').json()[0]['revenue_cents'] == 3000
    assert client.patch('/api/orders/1/status', json={'status': 'shipped'}).status_code == 200
    assert client.patch('/api/orders/1/status', json={'status': 'cancelled'}).status_code == 409
    operations = client.get('/metrics').json()['routes']
    assert len(operations) == 13
    assert all(v['count'] > 0 for v in operations.values())
    # A second factory opens the same persistent database.
    assert create_app(app.state.db.path).state.db.get('users', 1)['name'] == 'Updated'


@pytest.mark.parametrize('path,body', [
    ('users', {'name': '', 'email': 'invalid'}),
    ('users', {'name': 'A', 'email': 'a@b.com', 'extra': True}),
    ('products', {'name': 'P', 'price_cents': 0}),
    ('products', {'name': 'P', 'price_cents': True}),
    ('products', {'name': 'P', 'price_cents': 2**64}),
    ('orders', {'user_id': 1, 'product_id': 1, 'quantity': -1}),
    ('orders', {'user_id': 2**64, 'product_id': 1, 'quantity': 1}),
])
def test_invalid_create(client, path, body):
    assert client.post('/api/' + path, json=body).status_code == 422


@pytest.mark.parametrize('path', ['users', 'products'])
@pytest.mark.parametrize('body', [{}, {'name': None}, {'unknown': 1}])
def test_invalid_patch(client, path, body):
    assert client.patch(f'/api/{path}/1', json=body).status_code == 422


def test_errors_and_normalized_routes(client):
    seed(client)
    assert client.post('/api/users', json={'name': 'Again', 'email': 'vedik@example.com'}).status_code == 409
    assert client.post('/api/orders', json={'user_id': 99, 'product_id': 1, 'quantity': 1}).status_code == 409
    assert client.post('/api/orders', json={'user_id': 1, 'product_id': 99, 'quantity': 1}).status_code == 409
    for collection in ('users', 'products', 'orders'):
        assert client.get(f'/api/{collection}/999').status_code == 404
    assert client.patch('/api/users/999', json={'name': 'X'}).status_code == 404
    assert client.patch('/api/orders/999/status', json={'status': 'paid'}).status_code == 404
    assert client.get('/api/users/not-a-number').status_code == 422
    assert client.get('/api/users?limit=101').status_code == 422
    assert client.get('/api/users?offset=18446744073709551616').status_code == 422
    assert client.get('/api/users/18446744073709551616').status_code == 422
    assert client.post('/api/users', content='{bad', headers={'content-type': 'application/json'}).status_code == 422
    assert client.get('/api/does-not-exist').status_code == 404
    stats = client.get('/metrics').json()
    assert stats['summary']['error_rate'] > 0
    assert 'GET /api/{unmatched}' in stats['routes']
    assert 'GET /api/users/{item_id}' in stats['routes']


def test_cache_hits_invalidation_and_auth(client, app):
    seed(client)
    for _ in range(6):
        response = client.get('/api/products')
    assert response.headers['x-cache'] == 'HIT'
    assert client.get('/metrics').json()['summary']['cache_hits'] >= 1
    assert client.get('/metrics/comparison').json()['GET /api/products']['improvement_percent'] is not None
    assert client.patch('/api/products/1', json={'price_cents': 2000}).status_code == 200
    assert not app.state.cache.entries
    assert client.get('/api/products').json()[0]['price_cents'] == 2000
    for header in ({'authorization': 'Bearer user'}, {'cookie': 'session=private'}):
        bypassed = client.get('/api/products', headers=header)
        assert bypassed.status_code == 200
        assert bypassed.headers['x-cache-reason'] == 'request-credentials'
        assert app.state.metrics.records[-1]['cache'] == 'BYPASS'
    assert app.state.cache.counters['credential_bypasses'] == 2
    assert client.request('PUT', '/cache/mode', json={'mode': 'off'}).json()['mode'] == 'off'
    assert client.get('/api/products').headers['x-cache'] == 'BYPASS'
    assert client.request('DELETE', '/cache').json() == {'cleared': True}
    assert client.get('/analysis').json()['cache']['decisions']


def test_health_dashboard_exclusions(client, app):
    assert client.get('/health').json() == {'status': 'ok'}
    assert 'API Performance Monitor' in client.get('/').text
    asset = client.get('/assets/workflow.js')
    assert asset.status_code == 200
    assert 'tuneioWorkflow' in asset.text
    assert asset.headers['cache-control'] == 'no-store'
    assert client.get('/metrics').json()['summary']['count'] == 0
    assert client.get('/metrics?window=0').status_code == 422
    assert client.get('/metrics/comparison').json() == {}
    assert not app.state.metrics.records


def test_database_unavailable(client, app, monkeypatch):
    def unavailable(*args):
        raise sqlite3.OperationalError('locked')
    monkeypatch.setattr(app.state.db, 'list', unavailable)
    assert client.get('/api/users').status_code == 503
    assert client.get('/metrics').json()['summary']['server_error_rate'] == 1


def test_bottlenecks_expose_latency_and_errors(client, app):
    for _ in range(5):
        app.state.metrics.record('GET', '/api/reports/sales', 503, 75, 'MISS')
    kinds = {item['kind'] for item in client.get('/analysis').json()['bottlenecks']}
    assert kinds == {'high_latency', 'server_errors'}


def test_cookie_bypass_is_observable_without_training_shared_cache(client, app):
    for _ in range(6):
        response = client.get('/api/products', headers={'cookie': 'another_local_app=value'})
        assert response.headers['x-cache'] == 'BYPASS'
    status = client.get('/analysis').json()['cache']
    assert status['performance']['credential_bypasses'] == 6
    assert status['performance']['origin_loads'] == 6
    assert status['performance']['eligible_reads'] == 0
    assert status['policies'] == {} and status['entries'] == 0
    assert 'Credentialed reads' in status['diagnostic']
    assert client.get('/metrics').json()['summary']['count'] == 6
    for _ in range(6):
        response = client.get('/api/products')
    assert response.headers['x-cache'] == 'HIT'
    status = client.get('/analysis').json()['cache']
    assert status['performance']['eligible_reads'] == 6
    assert status['policies']['/api/products']['origin_samples'] >= 4
    assert status['policies']['/api/products']['origin_mean_ms'] is not None


def test_bypassed_failure_counts_database_work(client, app, monkeypatch):
    def unavailable(*args):
        raise sqlite3.OperationalError('locked')
    monkeypatch.setattr(app.state.db, 'list', unavailable)
    assert client.get('/api/users', headers={'cookie': 'session=private'}).status_code == 503
    status = client.get('/analysis').json()['cache']
    assert status['performance']['origin_errors'] == 1
    assert status['performance']['origin_loads'] == 1
    assert status['performance']['credential_bypasses'] == 1


def test_management_without_token(client, monkeypatch, tmp_path):
    import asyncio
    import httpx
    monkeypatch.delenv('AUTOTUNER_ADMIN_TOKEN', raising=False)
    monkeypatch.setenv('AUTOTUNER_DB', str(tmp_path / 'env.sqlite3'))
    app = create_app()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            assert (await c.delete('/cache')).status_code == 200
            assert (await c.put('/cache/mode', json={'mode': 'off'})).json()['mode'] == 'off'
            assert (await c.put('/cache/mode', json={'mode': 'adaptive'})).json()['mode'] == 'adaptive'
    asyncio.run(run())


def test_duplicate_query_parameters_do_not_collide(client):
    seed(client)
    client.post('/api/products', json={'name': 'Mouse', 'price_cents': 100})
    for _ in range(6):
        assert len(client.get('/api/products?limit=1&limit=2').json()) == 2
    assert len(client.get('/api/products?limit=2&limit=1').json()) == 1


def test_cancelled_order_report_invalidation(client):
    seed(client)
    for _ in range(6):
        assert client.get('/api/reports/sales').json()[0]['revenue_cents'] == 3000
    assert client.patch('/api/orders/1/status', json={'status': 'cancelled'}).status_code == 200
    assert client.get('/api/reports/sales').json()[0]['revenue_cents'] == 0


def test_mutations_keep_unrelated_responses_cached(client):
    seed(client)
    for path in ('/api/users', '/api/products', '/api/reports/sales'):
        for _ in range(6):
            client.get(path)
    assert client.patch('/api/users/1', json={'name': 'New name'}).status_code == 200
    assert client.get('/api/products').headers['x-cache'] == 'HIT'
    assert client.get('/api/reports/sales').headers['x-cache'] == 'HIT'
    assert client.get('/api/users').headers['x-cache'] == 'MISS'
    assert client.patch('/api/products/1', json={'name': 'New product'}).status_code == 200
    assert client.get('/api/products').headers['x-cache'] == 'MISS'
    assert client.get('/api/reports/sales').headers['x-cache'] == 'MISS'
    assert client.get('/api/users').headers['x-cache'] == 'HIT'


def test_concurrent_read_and_write_cannot_leave_stale_cache(app):
    import asyncio
    import httpx
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            await c.post('/api/products', json={'name': 'P', 'price_cents': 100})
            for _ in range(6):
                await c.get('/api/products')
            app.state.cache.invalidate()
            responses = await asyncio.gather(
                c.get('/api/products'),
                c.patch('/api/products/1', json={'price_cents': 200}),
                c.get('/api/products'))
            assert all(r.status_code == 200 for r in responses)
            assert (await c.get('/api/products')).json()[0]['price_cents'] == 200
    asyncio.run(scenario())


def test_cancelled_write_finishes_before_releasing_cache_lock(app, monkeypatch):
    import asyncio
    import httpx
    from threading import Event
    started, release = Event(), Event()
    original = app.state.db.create
    def delayed(*args):
        started.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(app.state.db, 'create', delayed)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            for _ in range(6):
                await c.get('/api/products')
            writer = asyncio.create_task(c.post('/api/products', json={'name': 'P', 'price_cents': 100}))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                writer.cancel()
                await asyncio.sleep(.02)
                assert app.state.cache.lock.locked()
                assert not writer.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await writer
            assert not app.state.cache.entries
            assert (await c.get('/api/products')).json()[0]['name'] == 'P'
    asyncio.run(scenario())
