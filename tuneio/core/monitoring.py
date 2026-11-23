from time import perf_counter
from .contracts import MetricSink


class Monitoring:
    """Pure ASGI observation, with adapter-supplied filtering and route labels."""
    def __init__(self, app, metrics: MetricSink, include=None, route_label=None, clock=perf_counter):
        self.app, self.metrics, self.clock = app, metrics, clock
        self.include = include if include is not None else lambda scope: True
        self.route_label = route_label if route_label is not None else lambda scope: '/{unmatched}'

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not self.include(scope):
            return await self.app(scope, receive, send)
        start, status = self.clock(), 500
        async def capture(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)
        try:
            await self.app(scope, receive, capture)
        finally:
            self.metrics.record(scope['method'], self.route_label(scope), status,
                                (self.clock() - start) * 1000,
                                scope.get('state', {}).get('cache', 'NONE'))
