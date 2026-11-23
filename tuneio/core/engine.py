import asyncio
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from .cache import AdaptiveCache, CacheConfig
from .metrics import Metrics

T = TypeVar('T')


@dataclass(frozen=True)
class ReadResult(Generic[T]):
    value: T
    outcome: str
    reason: str


class PerformanceEngine:
    """Framework-independent coordination for synchronous origin callables.

    Adapters explicitly opt public reads in and supply canonical resource prefixes.
    This is not yet an HTTP representation cache or an async upstream proxy.
    """
    def __init__(self, cache_config: CacheConfig | None = None, *, cache=None, metrics=None):
        self.cache = cache if cache is not None else AdaptiveCache(cache_config)
        self.metrics = metrics if metrics is not None else Metrics()

    async def read(self, route: str, path: str, query, loader: Callable[[], T], *, credentialed=False) -> ReadResult[T]:
        if credentialed:
            return ReadResult(await self.cache.bypass(loader), 'BYPASS', 'request-credentials')
        # Stable sorting by name retains the relative order of duplicate values.
        key = (path, tuple(sorted(query, key=lambda pair: pair[0])))
        value, outcome = await self.cache.read(route, key, loader)
        reason = 'cache-hit' if outcome == 'HIT' else 'cache-disabled' if outcome == 'BYPASS' else 'origin-observation'
        return ReadResult(value, outcome, reason)

    async def write(self, loader: Callable[[], T], *, invalidate: tuple[str, ...]) -> T:
        """Serialize writes and invalidate even after failed or cancelled work."""
        async with self.cache.lock:
            self.cache.invalidate(invalidate)
            work = asyncio.create_task(asyncio.to_thread(loader))
            try:
                try:
                    return await asyncio.shield(work)
                except asyncio.CancelledError as cancelled:
                    try:
                        await work
                    except Exception:
                        pass  # Preserve cancellation after accounting for the worker.
                    raise cancelled
            finally:
                self.cache.invalidate(invalidate)

    def analyze(self):
        bottlenecks = []
        for route, stats in self.metrics.snapshot()['routes'].items():
            if stats['count'] >= 5:
                if stats['p95_ms'] >= 50:
                    bottlenecks.append(dict(route=route, kind='high_latency', p95_ms=stats['p95_ms'],
                                            action='Inspect origin cost and cache reuse; compare client load results'))
                if stats['server_error_rate'] >= .05:
                    bottlenecks.append(dict(route=route, kind='server_errors',
                                            action='Inspect origin availability and failures before caching'))
        return {'regressions': self.metrics.regressions(), 'bottlenecks': bottlenecks, 'cache': self.cache.status()}
