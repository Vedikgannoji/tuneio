import asyncio
import gzip
import json

import httpx
import pytest

from tuneio.product_config import ProductConfig
from tuneio.session import ControlSession
from tuneio.proxy import IncompleteResponse


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks, error=None):
        self.chunks, self.error, self.closed = chunks, error, False
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error
    async def aclose(self):
        self.closed = True


def application(handler, **overrides):
    return ControlSession(ProductConfig.model_validate(dict(upstream='https://upstream.example', **overrides)),
                          transport=httpx.MockTransport(handler))


async def request(app, method='GET', path='/items', **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://proxy') as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def test_supported_methods_preserve_bytes_status_and_headers(method):
    calls, stream = [], Chunks(b'response')
    def origin(req):
        calls.append(req)
        return httpx.Response(418, headers=[('content-type', 'application/problem+json'),
                                          ('set-cookie', 'a=1'), ('set-cookie', 'b=2'),
                                          ('connection', 'x-private'), ('x-private', 'remove'),
                                          ('x-origin', 'keep')], stream=stream)
    app = application(origin)
    async def scenario():
        response = await request(app, method, '/items?x=2&x=1&encoded=%2F', content=b'{"name":"\xe2\x9c\x93"}',
                                 headers={'Host': 'attacker.example', 'Connection': 'x-hop', 'x-hop': 'remove',
                                          'Authorization': 'Bearer caller', 'Cookie': 'private=value',
                                          'X-Forwarded-Host': 'attacker.example'})
        assert response.status_code == 418
        assert response.content == (b'' if method == 'HEAD' else b'response')
        assert response.headers.get_list('set-cookie') == ['a=1', 'b=2']
        assert response.headers['x-origin'] == 'keep' and 'x-private' not in response.headers
        forwarded = calls[0]
        assert forwarded.url.host == 'upstream.example'
        assert forwarded.url.query == b'x=2&x=1&encoded=%2F'
        assert forwarded.headers['host'] == 'upstream.example'
        assert 'x-hop' not in forwarded.headers and 'x-forwarded-host' not in forwarded.headers
        assert forwarded.headers['authorization'] == 'Bearer caller'
        assert forwarded.headers['cookie'] == 'private=value'
        assert forwarded.content == b'{"name":"\xe2\x9c\x93"}'
        assert len(calls) == 1 and stream.closed
        await app.proxy.close()
    asyncio.run(scenario())


def test_encoded_paths_fixed_authority_and_gzip():
    payload = gzip.compress(b'{"hello":"world"}')
    calls = []
    def origin(req):
        calls.append(req)
        return httpx.Response(200, headers={'content-encoding': 'gzip', 'content-length': str(len(payload))}, stream=Chunks(payload))
    app = application(origin)
    async def scenario():
        response = await request(app, path='/objects/a%2Fb')
        assert response.json() == {'hello': 'world'}
        assert response.headers['content-length'] == str(len(payload))
        assert calls[0].url.raw_path == b'/objects/a%2Fb'
        await app.proxy.close()
    asyncio.run(scenario())


def test_redirects_and_upstream_cookies_do_not_cross_clients():
    calls = []
    def origin(req):
        calls.append(req)
        return httpx.Response(302, headers={'location': 'https://elsewhere.example', 'set-cookie': 'session=secret; Path=/'}, stream=Chunks(b''))
    app = application(origin)
    async def scenario():
        for _ in range(2):
            assert (await request(app)).status_code == 302
        assert len(calls) == 2
        assert all('cookie' not in req.headers for req in calls)
        assert not list(app.proxy.client.cookies.jar)
        await app.proxy.close()
    asyncio.run(scenario())


async def raw_request(app, *, headers=None, method='GET', body=b'', path='/items', raw_path=None, version='1.1', disconnect=False):
    messages = iter([{'type': 'http.disconnect'}] if disconnect else [{'type': 'http.request', 'body': body}])
    sent = []
    async def receive(): return next(messages)
    async def send(message): sent.append(message)
    await app({'type': 'http', 'method': method, 'path': path, 'raw_path': raw_path or path.encode(),
               'query_string': b'', 'headers': headers or [], 'http_version': version}, receive, send)
    return sent


@pytest.mark.parametrize('headers,method,body,expected', [
    ([(b'upgrade', b'websocket')], 'GET', b'', 501),
    ([(b'accept', b'text/event-stream')], 'GET', b'', 501),
    ([], 'CONNECT', b'', 501),
    ([(b'content-length', b'1'), (b'content-length', b'1')], 'POST', b'a', 400),
    ([(b'content-length', b'1'), (b'transfer-encoding', b'chunked')], 'POST', b'a', 400),
    ([(b'content-length', b'bad')], 'POST', b'', 400),
    ([(b'content-length', b'9' * 100)], 'POST', b'', 400),
    ([(b'content-length', b'2')], 'POST', b'a', 400),
    ([(b'host', b'a'), (b'host', b'b')], 'GET', b'', 400),
    ([(b'transfer-encoding', b'gzip')], 'POST', b'', 400),
])
def test_rejected_requests_never_reach_upstream(headers, method, body, expected):
    calls = []
    app = application(lambda req: calls.append(req))
    async def scenario():
        sent = await raw_request(app, headers=headers, method=method, body=body)
        assert sent[0]['status'] == expected and not calls
        assert app.proxy.metrics.snapshot()['summary']['count'] == 1
        await app.proxy.close()
    asyncio.run(scenario())


def test_limits_disconnect_loop_and_raw_path():
    app = application(lambda req: pytest.fail('Unexpected forwarding'), limits={'request_body_bytes': 2, 'header_bytes': 100})
    async def scenario():
        for kwargs, status in [({'headers': [(b'large', b'x' * 100)]}, 431),
                               ({'headers': [(b'content-length', b'3')]}, 413),
                               ({'body': b'abc'}, 413),
                               ({'version': '2'}, 505),
                               ({'raw_path': b'http://evil.example/'}, 400),
                               ({'raw_path': b'/bad\x00path'}, 400),
                               ({'headers': [(b'x-autotuner-proxy', app.session_id.encode())]}, 508)]:
            sent = await raw_request(app, **kwargs)
            assert sent[0]['status'] == status
        assert await raw_request(app, disconnect=True) == []
        assert app.proxy.counters['cancelled_requests'] == 1
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('error,status', [(httpx.ReadTimeout('timeout'), 504), (httpx.ConnectError('secret URL'), 502)])
def test_upstream_failures_are_redacted_and_not_retried(error, status):
    calls = []
    def origin(req):
        calls.append(req)
        raise error
    app = application(origin)
    async def scenario():
        response = await request(app, 'POST', content=b'write')
        assert response.status_code == status and 'secret URL' not in response.text
        assert len(calls) == 1
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('headers,status', [({'content-type': 'text/event-stream'}, 200), ({}, 101),
                                          ({'x-large': 'a' * 200}, 200),
                                          ({'content-length': '0', 'transfer-encoding': 'chunked'}, 200)])
def test_unsupported_or_ambiguous_upstream_response_is_closed(headers, status):
    stream = Chunks(b'')
    app = application(lambda req: httpx.Response(status, headers=headers, stream=stream), limits={'header_bytes': 128})
    async def scenario():
        assert (await request(app)).status_code == 502
        assert stream.closed
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('error', [httpx.ReadError('interrupted'), httpx.ReadTimeout('interrupted')])
def test_failed_response_stream_closes_and_is_not_counted_as_complete(error):
    stream = Chunks(b'partial', error=error)
    app = application(lambda req: httpx.Response(200, stream=stream))
    async def scenario():
        with pytest.raises(IncompleteResponse):
            await request(app)
        assert stream.closed
        assert app.proxy.counters['incomplete_responses'] == 1
        assert app.proxy.metrics.snapshot()['summary']['count'] == 0
        await app.proxy.close()
    asyncio.run(scenario())


def test_double_slash_path_cannot_replace_upstream_authority():
    calls = []
    app = application(lambda req: calls.append(req) or httpx.Response(200, stream=Chunks(b'ok')))
    async def scenario():
        sent = await raw_request(app, raw_path=b'//evil.example/path')
        assert sent[0]['status'] == 200
        assert calls[0].url.host == 'upstream.example'
        await app.proxy.close()
    asyncio.run(scenario())


def test_real_asgi_upstream_integration():
    calls = []
    async def backend(scope, receive, send):
        body = b''
        while True:
            message = await receive()
            body += message.get('body', b'')
            if not message.get('more_body'):
                break
        calls.append((scope['method'], scope['query_string'], body))
        await send({'type': 'http.response.start', 'status': 201,
                    'headers': [(b'content-type', b'application/json')]})
        await send({'type': 'http.response.body', 'body': b'{"created":true}'})
    app = ControlSession(ProductConfig(upstream='http://local-backend', operations=[{'method': 'POST', 'path': '/objects'}]),
                         transport=httpx.ASGITransport(app=backend))
    async def scenario():
        response = await request(app, 'POST', '/objects?a=1&a=2', json={'name': 'test'})
        assert response.status_code == 201 and response.json() == {'created': True}
        assert len(calls) == 1 and calls[0][1] == b'a=1&a=2'
        assert json.loads(calls[0][2]) == {'name': 'test'}
        data = (await request(app, path='/_autotuner/metrics')).json()
        assert data['upstream']['summary']['count'] == 1
        assert data['summary']['count'] == 1
        assert set(data['routes']) == {'POST /objects'}
        assert app.config.cache.routes == []
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['observe', 'recommend', 'adaptive'])
def test_phase_four_never_serves_a_shared_response_cache(mode):
    calls = []
    app = application(lambda req: calls.append(req) or httpx.Response(200, stream=Chunks(b'live')),
                      mode=mode, cache={'routes': [{'id': 'items', 'match': '/items', 'policy': 'adaptive'}]})
    async def scenario():
        for _ in range(2):
            assert (await request(app)).content == b'live'
        assert len(calls) == 2
        assert app.status()['effective_mode'] == mode
        assert app.proxy.metrics.snapshot()['summary']['cache_hits'] == 0
        await app.proxy.close()
    asyncio.run(scenario())


def test_admission_bounds_and_cancelled_upstream_release_capacity():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def origin(req):
            calls.append(req)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return httpx.Response(200, stream=Chunks(b'ok'))
        app = application(origin, limits={'upstream_concurrency': 1, 'queue_capacity': 1})
        first = asyncio.create_task(request(app))
        await started.wait()
        second = asyncio.create_task(request(app))
        while app.proxy.in_flight < 2:
            await asyncio.sleep(0)  # scheduling only, no latency assertion
        assert (await request(app)).status_code == 503
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await second).status_code == 200
        assert app.proxy.in_flight == 0
        assert app.proxy.counters['cancelled_requests'] == 1
        assert app.proxy.counters['overload_rejections'] == 1
        await app.proxy.close()
    asyncio.run(scenario())


def test_body_timeout_never_dispatches_upstream():
    app = application(lambda req: pytest.fail('Unexpected origin'))
    async def scenario():
        sent = []
        async def receive(): raise asyncio.TimeoutError()
        async def send(message): sent.append(message)
        await app({'type': 'http', 'method': 'POST', 'path': '/items', 'headers': []}, receive, send)
        assert sent[0]['status'] == 408
        assert app.proxy.in_flight == 0
        await app.proxy.close()
    asyncio.run(scenario())


def test_admission_timeout_is_a_service_unavailable_error(monkeypatch):
    app = application(lambda req: pytest.fail('Unexpected origin'))
    async def timed_out(): raise asyncio.TimeoutError()
    monkeypatch.setattr(app.proxy.slots, 'acquire', timed_out)
    async def scenario():
        assert (await request(app)).status_code == 503
        assert app.proxy.in_flight == 0 and app.proxy.counters['overload_rejections'] == 1
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [204, 304])
def test_bodyless_status_has_no_downstream_body(status):
    app = application(lambda req: httpx.Response(status, stream=Chunks(b'')))
    async def scenario():
        response = await request(app)
        assert response.status_code == status and response.content == b''
        await app.proxy.close()
    asyncio.run(scenario())


def test_websocket_is_closed_without_forwarding():
    app = application(lambda req: pytest.fail('Unexpected origin'))
    async def scenario():
        sent = []
        async def send(message): sent.append(message)
        await app({'type': 'websocket'}, None, send)
        assert sent == [{'type': 'websocket.close', 'code': 1003}]
        await app.proxy.close()
    asyncio.run(scenario())


def test_large_finite_response_and_template_metrics():
    data = b'x' * 150000
    app = application(lambda req: httpx.Response(200, stream=Chunks(data)),
                      limits={'cache_entry_bytes': 1}, cache={'routes': [{'id': 'items', 'match': '/items/*'}]})
    async def scenario():
        assert (await request(app, path='/items/one')).content == data
        metrics = (await request(app, path='/_autotuner/metrics?window=60')).json()
        assert metrics['summary']['count'] == 1
        assert set(metrics['routes']) == {'GET /items/*'}
        assert not app.status()['cache_serving_enabled']
        assert (await request(app, path='/_autotuner/metrics?window=0')).status_code == 400
        assert (await request(app, path='/_autotuner/unknown')).status_code == 404
        await app.proxy.close()
    asyncio.run(scenario())
