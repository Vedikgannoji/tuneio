"""Bounded HTTP/1.1 data plane with opt-in fixed-TTL caching and no retries."""
import asyncio
from http.cookiejar import CookieJar, DefaultCookiePolicy
import json
import hashlib
import hmac
import re
import secrets
from time import perf_counter
from email.utils import formatdate

import httpx

from .core import Metrics
from .response_cache import ResponseCache, request_reason, representation_key

HOP_HEADERS = {b'connection', b'keep-alive', b'proxy-connection', b'proxy-authenticate',
               b'proxy-authorization', b'te', b'trailer', b'transfer-encoding', b'upgrade'}
METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}
ADVISORY_FRESHNESS = re.compile(r'(?:s-maxage|max-age)=[0-9]{1,10}')


class IncompleteResponse(RuntimeError):
    """Sanitized post-header failure; no second HTTP response can be sent."""


class RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


def filtered_headers(headers, *, request=False):
    named = {token.strip().lower() for key, value in headers if key.lower() == b'connection'
             for token in value.split(b',')}
    excluded = HOP_HEADERS | named
    if request:
        excluded |= {b'host', b'content-length', b'forwarded', b'x-real-ip'}
    return [(key.lower(), value) for key, value in headers
            if key.lower() not in excluded and not (request and key.lower().startswith(b'x-forwarded-'))]


def cache_review_reason(method, headers, response):
    """A conservative advisory screen, not shared-cache eligibility approval."""
    names = {key.lower() for key, _ in headers}
    if method != 'GET':
        return 'method'
    if names & {b'authorization', b'cookie'}:
        return 'credentials'
    if b'range' in names or any(name.startswith(b'if-') for name in names):
        return 'conditional_or_range'
    if names & {b'cache-control', b'pragma'}:
        return 'request_directives'
    if response.status_code != 200:
        return 'response_status'
    if 'set-cookie' in response.headers:
        return 'set_cookie'
    directives = [item.strip().lower() for value in response.headers.get_list('cache-control')
                  for item in value.split(',')]
    names = [item.split('=',1)[0] for item in directives]
    if ('pragma' in response.headers or len(names) != len(set(names))
            or any(item != 'public' and not ADVISORY_FRESHNESS.fullmatch(item) for item in directives)):
        return 'response_directives'
    vary = {part.strip().lower() for part in response.headers.get('vary', '').split(',') if part.strip()}
    if vary - {'accept', 'accept-encoding'}:
        return 'unsupported_vary'
    return 'review_required'


async def json_response(send, status, data):
    body = json.dumps(data).encode()
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json'), (b'cache-control', b'no-store'), (b'date',formatdate(usegmt=True).encode())]})
    await send({'type': 'http.response.body', 'body': body})


class ObservationProxy:
    def __init__(self, config, session_id, *, transport=None):
        self.config, self.session_id = config, session_id
        self.base = httpx.URL(config.upstream)
        self.reuse_salt = secrets.token_bytes(32)
        self.metrics = Metrics(capacity=config.metrics.raw_capacity)
        self.upstream_metrics = Metrics(capacity=config.metrics.raw_capacity)
        self.slots = asyncio.Semaphore(config.limits.upstream_concurrency)
        self.in_flight = 0
        self.hot_reads = 0
        self.cache = ResponseCache(config,session_id=session_id)
        self.client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, verify=True,
            cookies=CookieJar(policy=RejectCookies()),
            timeout=httpx.Timeout(config.limits.read_timeout_seconds, connect=config.limits.connect_timeout_seconds),
            limits=httpx.Limits(max_connections=config.limits.upstream_concurrency,
                               max_keepalive_connections=config.limits.upstream_concurrency))
        self.counters = dict(upstream_requests=0, completed_requests=0, failed_requests=0,
                             incomplete_responses=0, cancelled_requests=0, overload_rejections=0, queue_ms=0.0, upstream_ms=0.0)

    def operation(self, path, method):
        parts = path.split('/')
        matches = [op.path for op in self.config.operations if op.method == method
                   and len(op.path.split('/')) == len(parts)
                   and all(a == b or a == '*' for a, b in zip(op.path.split('/'), parts))]
        if matches:
            return min(matches, key=lambda value: value.count('*'))
        matches = [route.match for route in self.config.cache.routes
                   if len(route.match.split('/')) == len(parts)
                   and all(a == b or a == '*' for a, b in zip(route.match.split('/'), parts))]
        return min(matches, key=lambda value: value.count('*')) if matches else '/{unmatched}'

    async def close(self):
        await self.client.aclose()

    async def __call__(self, scope, receive, send):
        start, response_started, completed, status = perf_counter(), False, False, 500
        upstream_complete = False
        upstream_start = None
        upstream_elapsed = 0.0
        admitted = acquired = hot = False
        response = fill = pinned = None
        key = rule = None
        coordination = lock_acquired = False
        generation = None
        write_ids = None
        method = scope['method']
        stage = 'request'
        queued = None
        queue_elapsed_ms = 0.0
        review_reason, request_key = 'not_observed', None
        outcome, reason = 'BYPASS', 'not_evaluated'
        tuning_read=tuning_recorded=False
        assessment=None
        assessment_reason='not_observed'
        delivered=0

        async def downstream(message):
            await asyncio.wait_for(send(message), self.config.limits.read_timeout_seconds)

        def diagnostics(headers):
            headers = [(k,v) for k,v in headers if k.lower() not in (b'x-autotuner-cache',b'x-autotuner-cache-reason')]
            if not any(k.lower()==b'date' for k,v in headers):
                headers.append((b'date',formatdate(self.cache.wall_clock(),usegmt=True).encode()))
            return headers+[(b'x-autotuner-cache',outcome.encode()),(b'x-autotuner-cache-reason',reason.encode())]

        async def reject(code, detail):
            nonlocal completed, status, response_started
            status = code
            response_started = True
            async def annotated(message):
                if message['type']=='http.response.start':message=dict(message,headers=diagnostics(message['headers']))
                await downstream(message)
            await json_response(annotated, code, {'detail': detail})
            completed = True

        def finish_queue():
            nonlocal queued, queue_elapsed_ms
            if queued is not None:
                elapsed=(perf_counter()-queued)*1000
                queue_elapsed_ms+=elapsed
                self.counters['queue_ms']+=elapsed
                queued=None

        async def hit(entry):
            nonlocal pinned, response_started, completed, status, outcome, reason
            pinned=entry
            outcome,reason='HIT','fresh'
            status=200
            age=int(entry.initial_age+max(0,self.cache.clock()-entry.received))
            headers=list(entry.headers)+[(b'age',str(age).encode()),(b'content-length',str(sum(map(len,entry.chunks))).encode())]
            response_started=True
            await downstream({'type':'http.response.start','status':200,'headers':diagnostics(headers)})
            for chunk in entry.chunks:
                await downstream({'type':'http.response.body','body':chunk,'more_body':True})
            await downstream({'type':'http.response.body','body':b'','more_body':False})
            completed=True

        try:
            headers = scope.get('headers', [])
            if sum(len(k) + len(v) + 4 for k, v in headers) > self.config.limits.header_bytes:
                return await reject(431, 'Request headers exceed configured limit')
            values = {}
            for name, value in headers:
                values.setdefault(name.lower(), []).append(value)
            if scope.get('http_version', '1.1') not in ('1.0', '1.1'):
                return await reject(505, 'Only HTTP/1.x downstream requests are supported')
            if method not in METHODS or b'upgrade' in values or any(b'text/event-stream' in v.lower() for v in values.get(b'accept', [])):
                return await reject(501, 'Method, upgrade or event-stream traffic is unsupported')
            hops = b','.join(values.get(b'x-autotuner-proxy', [])).split(b',')
            if self.session_id.encode() in [h.strip() for h in hops]:
                return await reject(508, 'Proxy forwarding loop detected')
            lengths, transfer = values.get(b'content-length', []), values.get(b'transfer-encoding', [])
            if (len(lengths) > 1 or len(values.get(b'host', [])) > 1 or
                (lengths and transfer) or (lengths and (not lengths[0].isdigit() or len(lengths[0]) > 20)) or
                (transfer and transfer != [b'chunked'])):
                return await reject(400, 'Ambiguous or invalid request framing')
            if lengths and int(lengths[0]) > self.config.limits.request_body_bytes:
                return await reject(413, 'Request body exceeds configured limit')
            raw_path = scope.get('raw_path', scope['path'].encode()).split(b'?', 1)[0]
            if not raw_path.startswith(b'/'):
                return await reject(400, 'An origin-form request path is required')
            try:
                target = self.base.copy_with(raw_path=raw_path + (b'?' + scope['query_string'] if scope.get('query_string') else b''))
            except httpx.InvalidURL:
                return await reject(400, 'Invalid request target')
            if self.config.mode in ('cache','adaptive','recommend') and any(token.strip().split(b'=',1)[0].lower()==b'only-if-cached'
                    for value in values.get(b'cache-control',[]) for token in value.split(b',')):
                reason='only_if_cached_unsupported'
                return await reject(504,'only-if-cached is unsupported; no upstream request was made')
            rule=self.cache.rule(scope['path'])
            reason=request_reason(self.config,rule,method,headers)
            candidate=reason=='eligible'
            if candidate and self.cache.writes[rule.id]:
                candidate,reason=False,'write_in_progress'
            if candidate:
                key=representation_key(self.config.upstream,method,target.raw_path,headers)
                hot=self.cache.contains(key,rule)
            limit=self.config.limits.upstream_concurrency+self.config.limits.queue_capacity
            if (self.hot_reads if hot else self.in_flight)>=limit:
                self.counters['overload_rejections']+=1
                return await reject(503,'Proxy admission capacity exceeded')
            if hot:self.hot_reads+=1
            else:self.in_flight+=1
            admitted=True
            # Non-cache requests retain admission-before-body buffering. Cache candidates have zero body framing.
            if not candidate:
                stage,queued='queue',perf_counter()
                await asyncio.wait_for(self.slots.acquire(),self.config.limits.connect_timeout_seconds)
                acquired=True
                finish_queue()
            stage='request'
            body_deadline=perf_counter()+self.config.limits.read_timeout_seconds
            body=bytearray()
            while True:
                message=await asyncio.wait_for(receive(),max(0,body_deadline-perf_counter()))
                if message['type']=='http.disconnect':
                    self.counters['cancelled_requests']+=1
                    return
                body.extend(message.get('body',b''))
                if len(body)>self.config.limits.request_body_bytes:
                    return await reject(413,'Request body exceeds configured limit')
                if not message.get('more_body'):break
            if (lengths and int(lengths[0])!=len(body)) or (candidate and body):
                return await reject(400,'Request length does not match body')
            if candidate:
                pinned=self.cache.acquire(key,rule)
                if pinned is not None:return await hit(pinned)
                if hot:
                    self.hot_reads-=1
                    hot=False
                    if self.in_flight>=limit:
                        admitted=False
                        self.counters['overload_rejections']+=1
                        return await reject(503,'Proxy admission capacity exceeded')
                    self.in_flight+=1
                coordination=True
                lock=self.cache.coordination(key)
                stage,queued='queue',perf_counter()
                await asyncio.wait_for(lock.acquire(),self.config.limits.connect_timeout_seconds)
                lock_acquired=True
                finish_queue()
                pinned=self.cache.acquire(key,rule)
                if pinned is not None:return await hit(pinned)
                generation=self.cache.generations[rule.id]
                if self.cache.writes[rule.id]:
                    candidate,reason=False,'write_in_progress'
                else:
                    outcome,reason='MISS','origin_fetch'
                    self.cache.counters['misses']+=1
                stage,queued='queue',perf_counter()
                await asyncio.wait_for(self.slots.acquire(),self.config.limits.connect_timeout_seconds)
                acquired=True
                finish_queue()
            tuning_read=bool(candidate and rule and rule.policy=='adaptive' and self.config.mode in ('adaptive','recommend'))
            outgoing=filtered_headers(headers,request=True)
            outgoing=[(k,v) for k,v in outgoing if k!=b'x-autotuner-proxy']
            outgoing.append((b'x-autotuner-proxy',b', '.join([h.strip() for h in hops if h.strip()]+[self.session_id.encode()])))
            request=httpx.Request(method,target,headers=outgoing,content=bytes(body))
            request.extensions['timeout']=self.client.timeout.as_dict()
            if method in ('POST','PUT','PATCH','DELETE'):
                write_ids=self.cache.begin_write(method,scope['path'])
            sent=self.cache.clock()
            upstream_start=perf_counter()
            stage='upstream'
            self.counters['upstream_requests']+=1
            response_deadline=perf_counter()+self.config.limits.response_timeout_seconds
            response=await asyncio.wait_for(self.client.send(request,stream=True),max(0,response_deadline-perf_counter()))
            received=self.cache.clock()
            upstream_elapsed+=perf_counter()-upstream_start
            upstream_start=None
            raw_headers=response.headers.raw
            if sum(len(k)+len(v)+4 for k,v in raw_headers)>self.config.limits.header_bytes:
                return await reject(502,'Upstream headers exceed configured limit')
            upstream_lengths=response.headers.get_list('content-length')
            if (len(upstream_lengths)>1 or (upstream_lengths and ('transfer-encoding' in response.headers or not upstream_lengths[0].isdigit()))):
                return await reject(502,'Ambiguous upstream response framing')
            if response.status_code==101 or response.headers.get('content-type','').lower().split(';')[0].strip()=='text/event-stream':
                return await reject(502,'Upstream returned an unsupported upgrade or event stream')
            review_reason=cache_review_reason(method,headers,response)
            if body and review_reason=='review_required':review_reason='get_body'
            if review_reason=='review_required':
                fields=[target.raw_path]+[b'\x00'.join(values.get(name,[])) for name in (b'accept',b'accept-encoding')]
                material=b''.join(len(value).to_bytes(8,'big')+value for value in fields)
                request_key=hmac.new(self.reuse_salt,material,hashlib.sha256).hexdigest()
            if tuning_read:
                assessment,assessment_reason=self.cache.assess(rule,raw_headers,response.status_code,received=received,sent=sent)
                if assessment is None and generation==self.cache.generations[rule.id] and not self.cache.writes[rule.id]:
                    self.cache.controller.observe(rule,key,upstream_elapsed*1000,failed=True,reason='uncacheable_'+assessment_reason)
                    tuning_recorded=True
            if candidate:
                if upstream_lengths and int(upstream_lengths[0])>self.config.limits.cache_entry_bytes:
                    reason='entry_too_large'
                elif 'content-range' in response.headers:
                    reason='partial_response'
                else:
                    fill,reason=self.cache.begin_fill(key,rule,generation,raw_headers,response.status_code,received=received,sent=sent)
                    if fill is not None and upstream_lengths and fill.cost+int(upstream_lengths[0])>self.config.limits.cache_entry_bytes:
                        self.cache.discard(fill)
                        fill,reason=None,'entry_too_large'
                if fill is None:outcome='BYPASS'
            if candidate and ((tuning_read and assessment is not None) or fill is not None):
                review_reason='review_required'
                request_key=hmac.new(self.reuse_salt,key.encode(),hashlib.sha256).hexdigest()
            status=response.status_code
            response_started=True
            await downstream({'type':'http.response.start','status':status,'headers':diagnostics(filtered_headers(raw_headers))})
            delivered=0
            if method!='HEAD' and status not in (204,304):
                chunks=response.aiter_raw(chunk_size=65536).__aiter__()
                while True:
                    upstream_start=perf_counter()
                    try:chunk=await asyncio.wait_for(anext(chunks),max(0,response_deadline-perf_counter()))
                    except StopAsyncIteration:break
                    finally:
                        upstream_elapsed+=perf_counter()-upstream_start
                        upstream_start=None
                    delivered+=len(chunk)
                    if fill is not None and fill.active and not self.cache.append(fill,chunk):
                        reason='entry_or_buffer_capacity'
                        review_reason,request_key='capacity',None
                    await downstream({'type':'http.response.body','body':chunk,'more_body':True})
                if upstream_lengths and delivered!=int(upstream_lengths[0]):
                    raise httpx.RemoteProtocolError('Incomplete upstream framing')
            upstream_complete=True
            await downstream({'type':'http.response.body','body':b'','more_body':False})
            completed=True
            if tuning_read and not tuning_recorded and generation==self.cache.generations[rule.id] and not self.cache.writes[rule.id]:
                metadata=256+len(key)+sum(len(k)+len(v) for k,v in assessment['headers']) if assessment else 0
                too_large=metadata+delivered>self.config.limits.cache_entry_bytes or metadata+delivered>self.config.limits.cache_bytes
                if too_large:review_reason,request_key='capacity',None
                self.cache.controller.observe(rule,key,upstream_elapsed*1000,failed=too_large,reason='capacity' if too_large else 'origin_sample')
                tuning_recorded=True
            if fill is not None and fill.active:
                reason='stored' if self.cache.publish(fill) else 'invalidated_or_expired_fill'
        except (httpx.TimeoutException,asyncio.TimeoutError):
            if tuning_read and not tuning_recorded and stage=='upstream' and generation==self.cache.generations[rule.id] and not self.cache.writes[rule.id]:
                self.cache.controller.observe(rule,key,0,failed=True,reason='upstream_failure')
                tuning_recorded=True
            self.counters['failed_requests']+=1
            if response_started:raise IncompleteResponse('Response interrupted after headers') from None
            if stage=='queue':self.counters['overload_rejections']+=1
            finish_queue()
            await reject(408 if stage=='request' else 503 if stage=='queue' else 504,
                         'Request body timed out' if stage=='request' else 'Proxy admission timed out' if stage=='queue' else 'Upstream timed out')
        except (httpx.HTTPError,OSError):
            if tuning_read and not tuning_recorded and generation==self.cache.generations[rule.id] and not self.cache.writes[rule.id]:
                self.cache.controller.observe(rule,key,0,failed=True,reason='upstream_failure')
                tuning_recorded=True
            self.counters['failed_requests']+=1
            if response_started:raise IncompleteResponse('Response interrupted after headers') from None
            await reject(502,'Upstream request failed')
        except asyncio.CancelledError:
            self.counters['cancelled_requests']+=1
            raise
        finally:
            finish_queue()
            if upstream_start is not None:upstream_elapsed+=perf_counter()-upstream_start
            self.counters['upstream_ms']+=upstream_elapsed*1000
            if write_ids is not None:self.cache.end_write(write_ids)
            self.cache.discard(fill)
            if pinned is not None:self.cache.release(pinned)
            if coordination:self.cache.release_coordination(key,lock_acquired)
            if acquired:self.slots.release()
            if admitted:
                if hot:self.hot_reads-=1
                else:self.in_flight-=1
            if response is not None:await response.aclose()
            if upstream_complete:
                self.upstream_metrics.record(method,self.operation(scope['path'],method),response.status_code,
                                             upstream_elapsed*1000,'BYPASS',cache_review_reason=review_reason,request_key=request_key)
            if response_started and not completed:self.counters['incomplete_responses']+=1
            if completed:
                self.counters['completed_requests']+=1
                if outcome=='BYPASS':self.cache.counters['bypasses']+=1
                self.cache.record_decision(rule,outcome,reason,upstream_complete)
                self.metrics.record(method if method in METHODS else 'OTHER',self.operation(scope['path'],method),status,
                                    (perf_counter()-start)*1000,outcome,queue_ms=queue_elapsed_ms,cache_reason=reason)
