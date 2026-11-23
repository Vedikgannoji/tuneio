"""Proxy session and loopback control endpoints."""
import asyncio
from time import perf_counter
import uuid
from urllib.parse import parse_qs

from .proxy import ObservationProxy, json_response
from .storage import TelemetryStorage, StorageUnavailable, source_digest
from .core.insights import analyze
from .response_cache import effective_mode
from .console import Console
from ._version import __version__


class ControlSession:
    def __init__(self, config, *, transport=None):
        self.config = config
        self.session_id = str(uuid.uuid4())
        self.running = False
        self.started_monotonic = perf_counter()
        self.proxy = ObservationProxy(config, self.session_id, transport=transport)
        self.storage = TelemetryStorage(config, self.session_id, transport_counters=self.proxy.counters)
        self.proxy.cache.controller.listener=self.storage.record_decision
        self.proxy.metrics.observer = lambda row: self.observe("proxy", row)
        self.proxy.upstream_metrics.observer = lambda row: self.observe("upstream", row)
        self.console=Console(self.proxy,self.status,self.storage)

    def observe(self, boundary, row):
        histories = (self.proxy.upstream_metrics, self.proxy.metrics)
        while sum(len(m.records) for m in histories) > self.config.metrics.raw_capacity:
            oldest = min((m for m in histories if m.records), key=lambda m: m.records[0]["timestamp"])
            oldest.records.popleft()
            oldest.evicted += 1
        self.storage.submit(boundary, row)

    def status(self):
        try:active_tasks=len(asyncio.all_tasks())
        except RuntimeError:active_tasks=None
        return dict(schema_version=1, session_id=self.session_id, tool_version=__version__, config_digest=self.storage.digest,
                    source_sha256=source_digest(), capabilities=['proxy','dashboard','scenarios','insights','cache','reports','monitor'],
                    uptime_seconds=max(0,perf_counter()-self.started_monotonic),
                    runtime=dict(in_flight=self.proxy.in_flight,hot_reads=self.proxy.hot_reads,active_tasks=active_tasks),
                    mode=self.config.mode, effective_mode=effective_mode(self.config), running=self.running,
                    upstream=self.config.upstream, listen=self.config.listen,
                    dashboard_url='http://'+self.config.listen+'/_autotuner/dashboard',
                    forwarding_enabled=True, cache_serving_enabled=any(self.proxy.cache.serving(r) for r in self.config.cache.routes),
                    storage=self.storage.health(),
                    limits=self.config.limits.model_dump(), transport=dict(self.proxy.counters),cache=self.proxy.cache.status())

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                message = await receive()
                if message['type'] == 'lifespan.startup':
                    await self.storage.start()
                    self.running = True
                    await send({'type': 'lifespan.startup.complete'})
                elif message['type'] == 'lifespan.shutdown':
                    self.running = False
                    await self.console.runner.close()
                    await self.proxy.close()
                    await self.storage.close()
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        elif scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1003})
        elif scope['type'] == 'http':
            path = scope['path']
            if path == '/_autotuner' or path.startswith('/_autotuner/'):
                if await self.console.handle(scope,receive,send):return
                if path=='/_autotuner/cache/clear' and scope['method']=='POST':
                    self.proxy.cache.invalidate()
                    return await json_response(send,200,dict(schema_version=1,session_id=self.session_id,cleared=True,cache=self.proxy.cache.status()))
                if scope['method'] != 'GET':
                    return await json_response(send, 405, {'detail': 'GET required'})
                if path=='/_autotuner/cache/explain':
                    try:
                        target=parse_qs(scope.get('query_string',b'').decode()).get('path',['/'])[-1]
                        if not target.startswith('/') or len(target)>1024 or any(c in target for c in ('?','#','\x00')):raise ValueError()
                    except (ValueError,UnicodeError):
                        return await json_response(send,400,{'detail':'Expected a route path without query or fragment'})
                    return await json_response(send,200,dict(schema_version=1,session_id=self.session_id,**self.proxy.cache.explain(target)))
                if path in ('/_autotuner/status', '/_autotuner/health'):
                    return await json_response(send, 200, self.status())
                if path in ('/_autotuner/report', '/_autotuner/sessions'):
                    try:
                        params = parse_qs(scope.get('query_string', b'').decode())
                        session = params.get('session', [self.session_id])[-1]
                        result = await self.storage.query('sessions' if path.endswith('/sessions') else 'report', session)
                    except (ValueError, UnicodeError):
                        return await json_response(send, 400, {'detail': 'Invalid report parameters'})
                    except StorageUnavailable:
                        return await json_response(send, 503, {'detail': 'Telemetry storage unavailable or busy'})
                    if path.endswith('/sessions'):
                        result = dict(schema_version=1, sessions=result)
                    return await json_response(send, 200, result)
                if path in ('/_autotuner/metrics', '/_autotuner/insights', '/_autotuner/monitor'):
                    try:
                        window = int(parse_qs(scope.get('query_string', b'').decode()).get('window', ['300' if path.endswith('/insights') else '60'])[-1])
                        if not 1 <= window <= 3600:
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        return await json_response(send, 400, {'detail': 'Window must be 1-3600 seconds'})
                    if path.endswith('/monitor'):
                        return await json_response(send, 200, self.console.monitor(window))
                    if path.endswith('/insights'):
                        result = analyze(self.config, self.proxy.metrics, self.proxy.upstream_metrics,
                                         transport=self.proxy.counters, storage=self.storage.health(), window=window)
                        result.update(session_id=self.session_id, config_digest=self.storage.digest, tool_version=__version__,cache=self.proxy.cache.status())
                        for item in result['cache_suitability']:
                            policy=self.proxy.cache.rule(item['operation'].split(' ',1)[1])
                            if policy is not None:
                                item['response_cache_serving_enabled']=self.proxy.cache.serving(policy)
                                if policy.policy=='adaptive':item['tuning']=self.proxy.cache.controller.evaluate(policy)
                        return await json_response(send, 200, result)
                    result = self.proxy.metrics.snapshot(window)
                    result.update(schema_version=1, session_id=self.session_id,
                                  duration_boundary='proxy handling through downstream completion',
                                  transport=dict(self.proxy.counters), storage=self.storage.health(),
                                  upstream=self.proxy.upstream_metrics.snapshot(window),
                                  upstream_duration_boundary='active upstream I/O excluding admission and downstream send waits')
                    return await json_response(send, 200, result)
                return await json_response(send, 404, {'detail': 'Unknown local control endpoint'})
            return await self.proxy(scope, receive, send)
