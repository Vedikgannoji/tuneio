"""Single-process bounded cache with measured, explainable policy decisions."""
import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from contextlib import asynccontextmanager
from time import perf_counter


@dataclass(frozen=True)
class CacheConfig:
    capacity: int = 256
    minimum_samples: int = 4
    slow_ms: float = 2.0
    ttl_min: float = 2.0
    ttl_max: float = 30.0
    cooldown: float = 5.0
    origin_concurrency: int = 8
    reuse_window: float = 60.0

    def __post_init__(self):
        if min(self.capacity, self.minimum_samples, self.origin_concurrency, self.slow_ms,
               self.ttl_min, self.reuse_window) <= 0 or self.ttl_max < self.ttl_min or self.cooldown < 0:
            raise ValueError('Invalid cache limits or timing configuration')


class AdaptiveCache:
    def __init__(self, config=None, clock=perf_counter):
        self.config = config or CacheConfig()
        self.clock = clock
        self.mode = 'adaptive'
        self.entries = OrderedDict()
        self.policies = {}
        self.reuse = OrderedDict()
        self.decisions = deque(maxlen=100)
        # Writes serialize; independent origin reads do not hold this lock.
        self.lock = asyncio.Lock()
        self.origin_slots = asyncio.Semaphore(self.config.origin_concurrency)
        self.key_locks = {}
        self.generation = 0
        self.counters = dict(hits=0, origin_loads=0, origin_errors=0, coalesced=0,
                             queue_ms=0.0, origin_ms=0.0, estimated_saved_origin_ms=0.0,
                             eligible_reads=0, credential_bypasses=0, disabled_reads=0)

    def invalidate(self, resources=None):
        self.generation += 1
        if resources is None:
            self.entries.clear()
        else:
            for key, entry in list(self.entries.items()):
                if any(entry[2] == resource or entry[2].startswith(resource.rstrip('/') + '/') for resource in resources):
                    del self.entries[key]

    def invalidate_route(self, route):
        for key, entry in list(self.entries.items()):
            if entry[2] == route:
                del self.entries[key]

    def policy(self, route):
        return self.policies.setdefault(route, dict(enabled=False, ttl=self.config.ttl_min,
                                                    changed=float('-inf'), samples=deque(maxlen=20),
                                                    reason='Waiting for origin samples'))

    def observe(self, route, key, duration, failed=False):
        policy = self.policy(route)
        policy['samples'].append((duration, failed))
        now = self.clock()
        count, last = self.reuse.get(key, (0, now))
        self.reuse[key] = (count + 1 if now - last <= self.config.reuse_window else 1, now)
        self.reuse.move_to_end(key)
        while len(self.reuse) > self.config.capacity:
            self.reuse.popitem(last=False)
        samples = policy['samples']
        if len(samples) < self.config.minimum_samples:
            policy['reason'] = 'Collecting minimum origin samples'
            return
        mean = sum(s[0] for s in samples) / len(samples)
        errors = sum(s[1] for s in samples) / len(samples)
        threshold = self.config.slow_ms * (.75 if policy['enabled'] else 1)
        enabled = mean >= threshold and self.reuse[key][0] >= 2 and errors == 0
        reason = ('Origin failures in recent samples' if errors else
                  'Origin is below the latency threshold' if mean < threshold else
                  'Current request key has insufficient recent reuse' if self.reuse[key][0] < 2 else
                  'Slow origin with repeated request key')
        policy['reason'] = reason
        ttl = min(self.config.ttl_max, max(self.config.ttl_min, mean / self.config.slow_ms * self.config.ttl_min))
        if failed or self.clock() - policy['changed'] >= self.config.cooldown:
            if enabled != policy['enabled'] or (enabled and abs(ttl - policy['ttl']) >= 1):
                action = 'adjust_ttl' if enabled and policy['enabled'] else ('enable' if enabled else 'disable')
                policy.update(enabled=enabled, ttl=ttl, changed=self.clock())
                if not enabled:
                    self.invalidate_route(route)
                else:
                    for key, entry in list(self.entries.items()):
                        if entry[2] == route:
                            self.entries[key] = (min(entry[0], self.clock() + ttl), entry[1], route)
                self.decisions.append(dict(route=route, enabled=enabled, ttl=ttl, action=action,
                                           reason=reason,
                                           timestamp=self.clock(), automatic=True,
                                           origin_mean_ms=mean, error_rate=errors))
        elif enabled != policy['enabled']:
            policy['reason'] = 'Policy cooldown; latest evidence: ' + reason

    def hit(self, route, key):
        entry = self.entries.get(key)
        if self.mode == 'adaptive' and self.policy(route)['enabled'] and entry:
            if entry[0] > self.clock():
                self.entries.move_to_end(key)
                self.counters['hits'] += 1
                samples = self.policy(route)['samples']
                self.counters['estimated_saved_origin_ms'] += sum(s[0] for s in samples) / max(1, len(samples))
                return entry[1], 'HIT'
        self.entries.pop(key, None)
        return None

    @asynccontextmanager
    async def key_lock(self, key):
        lock, users = self.key_locks.get(key, (asyncio.Lock(), 0))
        self.key_locks[key] = (lock, users + 1)
        if lock.locked():
            self.counters['coalesced'] += 1
        try:
            async with lock:
                yield
        finally:
            _, users = self.key_locks[key]
            if users == 1:
                del self.key_locks[key]
            else:
                self.key_locks[key] = (lock, users - 1)

    async def measure_origin(self, loader):
        def measured():
            start = self.clock()
            try:
                return loader(), None, (self.clock() - start) * 1000
            except Exception as exc:
                return None, exc, (self.clock() - start) * 1000
        value, error, duration = await asyncio.to_thread(measured)
        self.counters['origin_loads'] += 1
        self.counters['origin_ms'] += duration
        if error is not None:
            self.counters['origin_errors'] += 1
        return value, error, duration

    async def bypass(self, loader):
        # Never train shared policies on potentially personalized responses, but
        # still account for their real database work and worker queue contention.
        self.counters['credential_bypasses'] += 1
        queued = self.clock()
        async with self.origin_slots:
            async with self.lock:
                pass
            self.counters['queue_ms'] += (self.clock() - queued) * 1000
            value, error, _ = await self.measure_origin(loader)
            if error is not None:
                raise error
            return value

    async def load(self, route, key, loader):
        # Wait for an ongoing write, then release coordination before origin I/O.
        async with self.lock:
            generation = self.generation
        value, error, duration = await self.measure_origin(loader)
        self.observe(route, key, duration, failed=error is not None)
        if error is not None:
            raise error
        # A read started before a write/clear may finish afterward. Never refill
        # its old snapshot. Writes invalidate both before and after committing.
        if self.mode == 'adaptive' and self.policy(route)['enabled'] and generation == self.generation:
            self.entries[key] = (self.clock() + self.policy(route)['ttl'], value, route)
            while len(self.entries) > self.config.capacity:
                self.entries.popitem(last=False)
        return value, 'MISS' if self.mode == 'adaptive' else 'BYPASS'

    async def read(self, route, key, loader):
        self.counters['eligible_reads'] += 1
        self.counters['disabled_reads'] += self.mode == 'off'
        queued = self.clock()
        async with self.lock:
            pass
        cached = self.hit(route, key)
        if cached is not None:
            return cached
        # Bound worker I/O and lock bookkeeping. Hot hits skip this queue.
        async with self.origin_slots:
            self.counters['queue_ms'] += (self.clock() - queued) * 1000
            if self.mode == 'off':
                return await self.load(route, key, loader)
            key_queued = self.clock()
            async with self.key_lock(key):
                async with self.lock:
                    pass
                self.counters['queue_ms'] += (self.clock() - key_queued) * 1000
                cached = self.hit(route, key)
                if cached is not None:
                    return cached
                return await self.load(route, key, loader)

    def status(self):
        for key, entry in list(self.entries.items()):
            if entry[0] <= self.clock():
                del self.entries[key]
        if self.mode == 'off':
            diagnostic = 'Caching is turned off. Enable adaptive mode to allow automatic cache decisions.'
        elif not self.policies and self.counters['credential_bypasses']:
            diagnostic = 'Credentialed reads bypass shared caching. Use anonymous playground requests to train policies.'
        elif not self.policies:
            diagnostic = 'No eligible anonymous GET requests observed yet. Writes do not train read-cache policies.'
        else:
            diagnostic = 'Policies learn from origin execution time. Fast reads can correctly remain uncached.'
        policies = {}
        for route, policy in self.policies.items():
            samples = policy['samples']
            mean = sum(s[0] for s in samples) / len(samples) if samples else None
            policies[route] = dict(enabled=policy['enabled'], ttl=policy['ttl'], origin_samples=len(samples),
                                   minimum_samples=self.config.minimum_samples, origin_mean_ms=mean,
                                   activation_ms=self.config.slow_ms,
                                   retention_ms=self.config.slow_ms * .75,
                                   reason=policy['reason'], failed_samples=sum(s[1] for s in samples))
        return dict(mode=self.mode, entries=len(self.entries), capacity=self.config.capacity,
                    origin_concurrency=self.config.origin_concurrency, active_keys=len(self.key_locks),
                    performance=dict(self.counters), diagnostic=diagnostic, policies=policies,
                    decisions=list(self.decisions))
