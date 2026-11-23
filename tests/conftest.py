import asyncio
from itertools import count
import httpx
import pytest
from tuneio.main import create_app
from tuneio.cache import CacheConfig


@pytest.fixture
def app(tmp_path):
    application = create_app(tmp_path / 'test.sqlite3',
                             cache_config=CacheConfig(slow_ms=0.00001, cooldown=0))
    # Fast Windows database reads can start/end within one monotonic clock tick.
    # Drive policy timing explicitly; API assertions must not depend on host speed
    # or on the polling delay used by the sandbox's socketless event loop.
    ticks = count()
    application.state.cache.clock = lambda: next(ticks) / 1000
    return application


@pytest.fixture
def client(app):
    with asyncio.Runner() as runner:
        transport = httpx.ASGITransport(app=app)
        async_client = httpx.AsyncClient(transport=transport, base_url='http://test')
        class Client:
            def request(self, method, path, **kwargs):
                return runner.run(async_client.request(method, path, **kwargs))

            def get(self, path, **kwargs):
                return self.request('GET', path, **kwargs)

            def post(self, path, **kwargs):
                return self.request('POST', path, **kwargs)

            def patch(self, path, **kwargs):
                return self.request('PATCH', path, **kwargs)
        yield Client()
        runner.run(async_client.aclose())
