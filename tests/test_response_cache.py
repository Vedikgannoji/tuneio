import asyncio
import gzip
import io
import json
from contextlib import nullcontext
from email.utils import formatdate
from unittest.mock import Mock

import httpx
import pytest

from tuneio import cli
from tuneio.product_config import ProductConfig
from tuneio.response_cache import ResponseCache, freshness, representation_key
from tuneio.session import ControlSession
from tuneio.proxy import IncompleteResponse

WALL=1_800_000_000


class Bytes(httpx.AsyncByteStream):
    def __init__(self,*chunks,error=None,gate=None):self.chunks,self.error,self.gate=chunks,error,gate
    async def __aiter__(self):
        if self.gate:await self.gate.wait()
        for chunk in self.chunks:yield chunk
        if self.error:raise self.error


@pytest.fixture
def config(tmp_path):
    return ProductConfig(upstream='http://upstream.example',mode='cache',metrics={'storage_path':str(tmp_path/'metrics.sqlite3')},
        cache={'routes':[{'id':'items','match':'/items/*','policy':'cache','vary_headers':['Accept','Accept-Encoding'],
                         'invalidate_on':[{'method':'PATCH','path':'/items/*'},{'method':'POST','path':'/write'}]}]})


def response(body=b'origin',*,headers=(),status=200,gate=None,error=None):
    return httpx.Response(status,headers=[('Date',formatdate(WALL,usegmt=True)),('Cache-Control','public, max-age=60'),*headers],
                          stream=Bytes(body,gate=gate,error=error))


def app_for(config,handler):
    app=ControlSession(config,transport=httpx.MockTransport(handler))
    clock=[0.]
    app.proxy.cache.clock=lambda:clock[0]
    app.proxy.cache.wall_clock=lambda:WALL
    return app,clock


async def get(app,path='/items/1',**kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
        return await c.get(path,**kwargs)


@pytest.mark.parametrize('headers,reason',[
    ([], 'missing_freshness'),
    ([(b'cache-control',b'private, max-age=60')],'response_privacy'),
    ([(b'cache-control',b'no-store')],'response_privacy'),
    ([(b'cache-control',b'no-cache="etag"')],'response_privacy'),
    ([(b'cache-control',b'must-revalidate, max-age=60')],'unsupported_directive'),
    ([(b'cache-control',b'max-age=60, max-age=70')],'ambiguous_directive'),
    ([(b'cache-control',b'max-age=-1')],'invalid_directive'),
    ([(b'cache-control',b'max-age=bad')],'invalid_directive'),
    ([(b'cache-control',b'public=bad')],'invalid_directive'),
    ([(b'cache-control',b'max-age=')],'invalid_directive'),
    ([(b'cache-control',b'max-age=60'),(b'age',b'-1')],'invalid_age_or_date'),
    ([(b'cache-control',b'max-age=60'),(b'age',b'1'),(b'age',b'2')],'invalid_age_or_date'),
    ([(b'cache-control',b'max-age=60'),(b'date',b'invalid')],'invalid_age_or_date'),
    ([(b'cache-control',b'max-age=60'),(b'date',b'Fri, 01 Jan 2027 00:00:00')],'invalid_age_or_date'),
    ([(b'cache-control',b'max-age=60'),(b'date',formatdate(WALL,usegmt=True).encode()),(b'date',formatdate(WALL,usegmt=True).encode())],'invalid_age_or_date'),
    ([(b'cache-control',b'max-age=60'),(b'pragma',b'no-cache')],'response_directives'),
    ([(b'cache-control',b'max-age=60'),(b'set-cookie',b'identity=secret')],'set_cookie'),
    ([(b'cache-control',b'max-age=60'),(b'vary',b'*')],'unsupported_vary'),
    ([(b'cache-control',b'max-age=60'),(b'vary',b'X-Identity')],'unsupported_vary'),
    ([(b'cache-control',b'max-age=60'),(b'trailer',b'checksum')],'trailers'),
    ([(b'cache-control',b'max-age=60'),(b'connection',b'vary'),(b'vary',b'Accept')],'connection_metadata'),
    ([(b'expires',formatdate(WALL+60,usegmt=True).encode())],'expires_without_date'),
    ([(b'cache-control',b'max-age=1')],'freshness_below_minimum'),
])
def test_freshness_rejects_unsupported_and_private_responses(config,headers,reason):
    info,actual=freshness(headers,config.cache.routes[0],wall=WALL,received=0,sent=0)
    assert info is None and actual==reason


@pytest.mark.parametrize('headers,initial,ttl',[
    ([(b'cache-control',b'max-age="60"')],0,2),
    ([(b'cache-control',b'max-age=0, s-maxage=60'),(b'age',b'5')],5,2),
    ([(b'date',formatdate(WALL-58,usegmt=True).encode()),(b'cache-control',b'max-age=60')],58,2),
    ([(b'date',formatdate(WALL,usegmt=True).encode()),(b'expires',formatdate(WALL+60,usegmt=True).encode())],0,2),
])
def test_freshness_respects_precedence_dates_and_age(config,headers,initial,ttl):
    info,reason=freshness(headers,config.cache.routes[0],wall=WALL,received=0,sent=0)
    assert reason=='cacheable' and info['initial_age']==initial and info['expires']==ttl
    assert any(k==b'date' for k,v in info['headers'])


def test_response_delay_and_old_date_cannot_extend_freshness(config):
    rule=config.cache.routes[0]
    assert freshness([(b'cache-control',b'max-age=3')],rule,wall=WALL,received=2,sent=0)[1]=='freshness_below_minimum'
    assert freshness([(b'cache-control',b'max-age=60'),(b'date',formatdate(WALL-59,usegmt=True).encode())],rule,wall=WALL,received=0,sent=0)[0] is None


def test_miss_hit_expiry_age_and_metrics(config):
    calls=[]
    app,clock=app_for(config,lambda req:(calls.append(req) or response(headers=[('Age','3')])))
    async def scenario():
        first=await get(app);second=await get(app)
        assert first.content==second.content==b'origin'
        assert first.headers['x-autotuner-cache']=='MISS' and second.headers['x-autotuner-cache']=='HIT'
        assert second.headers['age']=='3' and second.headers['content-length']=='6'
        clock[0]=1.25
        assert (await get(app)).headers['age']=='4'
        clock[0]=2
        assert (await get(app)).headers['x-autotuner-cache']=='MISS'
        assert len(calls)==2
        assert app.proxy.metrics.snapshot()['summary']['cache_hits']==2
        assert app.proxy.upstream_metrics.snapshot()['summary']['count']==2
        assert app.proxy.cache.status()['entries']==1
        await app.proxy.close()
    asyncio.run(scenario())


def test_variants_queries_and_upstream_identity_have_separate_keys(config):
    calls=[]
    app,_=app_for(config,lambda req:(calls.append(req) or response(headers=[('Vary','Accept, Accept-Encoding')])))
    async def scenario():
        for path,headers in [('/items/1?a=1&a=2',{}),('/items/1?a=2&a=1',{}),('/items/2?a=1&a=2',{}),
                             ('/items/1?a=1&a=2',{'Accept':'text/plain'}),('/items/1?a=1&a=2',{'Accept-Encoding':'identity'})]:
            assert (await get(app,path,headers=headers)).headers['x-autotuner-cache']=='MISS'
        assert (await get(app,'/items/1?a=1&a=2')).headers['x-autotuner-cache']=='HIT'
        assert len(calls)==5
        await app.proxy.close()
    asyncio.run(scenario())
    assert representation_key('http://a','GET',b'/items',[])!=representation_key('http://b','GET',b'/items',[])


def test_gzip_bytes_and_duplicate_headers_are_reused_exactly(config):
    compressed=gzip.compress(b'compressed body')
    app,_=app_for(config,lambda req:response(compressed,headers=[('Content-Encoding','gzip'),('Content-Length',str(len(compressed))),('X-Tag','one'),('X-Tag','two'),('Connection','keep-alive'),('Keep-Alive','timeout=5')]))
    async def scenario():
        first,second=await get(app),await get(app)
        assert first.content==second.content==b'compressed body'
        assert second.headers.get_list('x-tag')==['one','two']
        assert second.headers['content-length']==str(len(compressed)) and second.headers['content-encoding']=='gzip'
        assert 'keep-alive' not in second.headers and 'connection' not in second.headers
        assert second.headers['x-autotuner-cache']=='HIT'
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('headers,reason',[
    ({'Authorization':'Bearer Alice'},'credentials'),({'Cookie':'identity=Alice'},'credentials'),
    ({'Range':'bytes=0-1'},'conditional_or_range'),({'If-None-Match':'tag'},'conditional_or_range'),
    ({'Cache-Control':'max-age=0'},'request_directives'),({'Pragma':'no-cache'},'request_directives'),
])
def test_request_bypasses_never_use_or_replace_public_entries(config,headers,reason):
    calls=[]
    app,_=app_for(config,lambda req:(calls.append(req) or response(str(len(calls)).encode())))
    async def scenario():
        assert (await get(app)).content==b'1'
        private=await get(app,headers=headers)
        assert private.content==b'2' and private.headers['x-autotuner-cache']=='BYPASS'
        assert private.headers['x-autotuner-cache-reason']==reason
        assert (await get(app)).content==b'1'
        assert len(calls)==2
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode,policy,reason',[('observe','cache','mode_observe'),('off','cache','mode_off'),('recommend','cache','recommend_only')]+[('cache','observe','rule_observe'),('cache','adaptive','rule_observe')])
def test_both_global_and_route_opt_in_are_required(config,mode,policy,reason):
    config.mode=mode;config.cache.routes[0].policy=policy
    calls=[]
    app,_=app_for(config,lambda req:(calls.append(req) or response()))
    async def scenario():
        for _ in range(2):assert (await get(app)).headers['x-autotuner-cache-reason']==reason
        assert len(calls)==2 and not app.proxy.cache.entries
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('headers,status,reason',[
    ([('Cache-Control','private')],200,'response_privacy'),([('Set-Cookie','id=secret')],200,'set_cookie'),
    ([('Vary','*')],200,'unsupported_vary'),([('Content-Range','bytes 0-1/4')],200,'partial_response'),([],500,'response_status'),
])
def test_response_bypass_forwarding_still_works(config,headers,status,reason):
    app,_=app_for(config,lambda req:response(headers=headers,status=status))
    async def scenario():
        for _ in range(2):
            result=await get(app)
            assert result.content==b'origin' and result.status_code==status
            assert result.headers['x-autotuner-cache']=='BYPASS'
            assert result.headers['x-autotuner-cache-reason']==reason
        assert not app.proxy.cache.entries
        await app.proxy.close()
    asyncio.run(scenario())


def test_large_response_streams_without_cache_and_releases_staging(config):
    config.limits.cache_entry_bytes=1000
    payload=b'x'*150000
    app,_=app_for(config,lambda req:response(payload))
    async def scenario():
        assert (await get(app)).content==payload
        assert app.proxy.cache.used_bytes==0 and not app.proxy.cache.entries
        assert app.proxy.cache.explain('/items/1')['last_decision']['reason']=='entry_or_buffer_capacity'
        await app.proxy.close()
    asyncio.run(scenario())


def test_lru_entry_count_and_pinned_payload_byte_accounting(config):
    config.limits.cache_entries=2
    app,_=app_for(config,lambda req:response())
    async def scenario():
        await get(app,'/items/1');await get(app,'/items/2');await get(app,'/items/1');await get(app,'/items/3')
        assert (await get(app,'/items/2')).headers['x-autotuner-cache']=='MISS'
        cache=app.proxy.cache;rule=cache.rule('/items/2');key=next(reversed(cache.entries))
        pinned=cache.acquire(key,rule)
        cache.invalidate()
        assert cache.used_bytes==pinned.cost and cache.retired==1
        assert not cache._reserve(config.limits.cache_bytes)
        assert cache.used_bytes==pinned.cost
        cache.release(pinned)
        assert cache.used_bytes==0 and cache.retired==0
        await app.proxy.close()
    asyncio.run(scenario())


def test_coalesces_cold_key_and_releases_all_coordination(config):
    calls=[]
    gate,entered,all_waiting=asyncio.Event(),asyncio.Event(),asyncio.Event()
    def origin(req):calls.append(req);entered.set();return response(gate=gate)
    app,_=app_for(config,origin)
    coordination=app.proxy.cache.coordination
    def observed(key):
        lock=coordination(key)
        if app.proxy.cache.locks[key][1]==8:all_waiting.set()
        return lock
    app.proxy.cache.coordination=observed
    async def scenario():
        tasks=[asyncio.create_task(get(app)) for _ in range(8)]
        await entered.wait();await all_waiting.wait();gate.set()
        results=await asyncio.gather(*tasks)
        assert len(calls)==1 and sum(r.headers['x-autotuner-cache']=='HIT' for r in results)==7
        assert not app.proxy.cache.locks and app.proxy.in_flight==0
        assert app.proxy.cache.counters['coalesced_waiters']==7
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('acquire_first', [False, True])
def test_waiter_telemetry_counts_registrations_before_acquisition(config, acquire_first):
    """Deferred lock acquisition must produce the same telemetry as immediate acquisition."""
    async def scenario():
        cache=ResponseCache(config)
        leader=cache.coordination('same-key')
        if acquire_first:await leader.acquire()
        followers=[cache.coordination('same-key') for _ in range(7)]
        assert all(lock is leader for lock in followers)
        assert leader.locked() is acquire_first
        assert cache.counters['coalesced_waiters']==7
        # A different key has its own leader, without adding a coalesced waiter.
        other=cache.coordination('other-key')
        assert other is not leader and cache.counters['coalesced_waiters']==7
        cache.release_coordination('other-key',False)
        # Registrations that cancel before acquisition still release their references.
        for _ in followers:cache.release_coordination('same-key',False)
        cache.release_coordination('same-key',acquire_first)
        assert not cache.locks
        next_leader=cache.coordination('same-key')
        assert next_leader is not leader and cache.counters['coalesced_waiters']==7
        cache.release_coordination('same-key',False)
        assert not cache.locks
    asyncio.run(scenario())


def test_hot_hit_skips_saturated_upstream_and_cold_admission(config):
    config.limits.upstream_concurrency=1;config.limits.queue_capacity=1
    gate,entered,queued=asyncio.Event(),asyncio.Event(),asyncio.Event()
    def origin(req):
        if req.url.path=='/items/2':entered.set();return response(gate=gate)
        return response()
    app,_=app_for(config,origin)
    async def scenario():
        await get(app)
        slow=asyncio.create_task(get(app,'/items/2'));await entered.wait()
        acquire=app.proxy.slots.acquire
        async def observed_acquire():queued.set();return await acquire()
        app.proxy.slots.acquire=observed_acquire
        waiting=asyncio.create_task(get(app,'/items/3'));await queued.wait()
        assert app.proxy.in_flight==2
        assert (await get(app,'/items/4')).status_code==503
        assert (await get(app)).headers['x-autotuner-cache']=='HIT'
        gate.set();await asyncio.gather(slow,waiting)
        assert app.proxy.in_flight==app.proxy.hot_reads==0 and not app.proxy.cache.locks
        await app.proxy.close()
    asyncio.run(scenario())


def test_read_finishing_after_mutation_cannot_refill_old_value(config):
    gate,entered=asyncio.Event(),asyncio.Event()
    state={'version':0,'reads':0}
    def origin(req):
        if req.method=='PATCH':state['version']+=1;return response(b'updated')
        state['reads']+=1
        snapshot=str(state['version']).encode()
        if state['reads']==1:entered.set();return response(snapshot,gate=gate)
        return response(snapshot)
    app,_=app_for(config,origin)
    async def scenario():
        read=asyncio.create_task(get(app));await entered.wait()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
            assert (await c.patch('/items/1')).status_code==200
        gate.set();assert (await read).content==b'0'
        assert not app.proxy.cache.entries
        assert (await get(app)).content==b'1'
        assert (await get(app)).headers['x-autotuner-cache']=='HIT'
        assert app.proxy.cache.counters['generation_rejections']==1
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',['error','cancel','failed_status'])
def test_write_failure_or_cancellation_invalidates_without_retry(config,failure):
    entered,gate=asyncio.Event(),asyncio.Event()
    state={'version':0,'writes':0}
    async def origin(req):
        if req.method=='PATCH':
            state['version']+=1;state['writes']+=1;entered.set()
            if failure=='cancel':await gate.wait()
            if failure=='error':raise httpx.ReadError('uncertain secret write')
            return response(b'write outcome',status=500 if failure=='failed_status' else 200)
        return response(str(state['version']).encode())
    app,_=app_for(config,origin)
    async def scenario():
        assert (await get(app)).content==b'0'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
            mutation=asyncio.create_task(c.patch('/items/1'));await entered.wait()
            if failure=='cancel':
                assert (await get(app)).headers['x-autotuner-cache-reason']=='write_in_progress'
                mutation.cancel()
                with pytest.raises(asyncio.CancelledError):await mutation
            else:assert (await mutation).status_code in (500,502)
        assert state['writes']==1 and sum(app.proxy.cache.writes.values())==0
        assert not app.proxy.cache.entries
        assert (await get(app)).content==b'1'
        await app.proxy.close()
    asyncio.run(scenario())


def test_cancelled_leader_and_waiter_release_locks_and_buffers(config):
    entered,gate,waiter_entered=asyncio.Event(),asyncio.Event(),asyncio.Event()
    calls=[]
    def origin(req):calls.append(req);entered.set();return response(gate=gate if len(calls)==1 else None)
    app,_=app_for(config,origin)
    original=app.proxy.cache.coordination
    def coordination(key):
        result=original(key)
        if app.proxy.cache.locks[key][1]==2:waiter_entered.set()
        return result
    app.proxy.cache.coordination=coordination
    async def scenario():
        leader=asyncio.create_task(get(app));await entered.wait()
        waiter=asyncio.create_task(get(app));await waiter_entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):await waiter
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):await leader
        assert not app.proxy.cache.locks and not app.proxy.cache.entries
        assert app.proxy.cache.used_bytes==0 and app.proxy.in_flight==0
        assert (await get(app)).headers['x-autotuner-cache']=='MISS'
        await app.proxy.close()
    asyncio.run(scenario())


def test_clear_during_fill_and_incomplete_stream_never_store(config):
    gate,entered=asyncio.Event(),asyncio.Event()
    def origin(req):entered.set();return response(gate=gate)
    app,_=app_for(config,origin)
    async def scenario():
        pending=asyncio.create_task(get(app));await entered.wait()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
            assert (await c.post('/_autotuner/cache/clear')).json()['cleared']
        gate.set();await pending
        assert not app.proxy.cache.entries and app.proxy.cache.used_bytes==0
        await app.proxy.close()
    asyncio.run(scenario())
    app,_=app_for(config,lambda req:response(b'partial',error=httpx.ReadError('secret error')))
    async def incomplete():
        with pytest.raises(IncompleteResponse):await get(app)
        assert app.proxy.cache.used_bytes==0 and not app.proxy.cache.entries
        await app.proxy.close()
    asyncio.run(incomplete())


def test_cache_controls_and_cli(config,tmp_path,monkeypatch,capsys):
    app,_=app_for(config,lambda req:response())
    async def scenario():
        await get(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
            result=(await c.get('/_autotuner/cache/explain?path=/items/1')).json()
            assert result['rule']['id']=='items' and result['last_decision']['reason']=='stored'
            assert 'cache' in (await c.get('/_autotuner/status')).json()['capabilities']
            assert (await c.get('/_autotuner/cache/explain?path=bad')).status_code==400
            assert (await c.get('/_autotuner/cache/clear')).status_code==404
            assert (await c.post('/_autotuner/cache/clear')).json()['cache']['entries']==0
        await app.proxy.close()
        return result
    result=asyncio.run(scenario())
    file=tmp_path/'config.json';file.write_text(config.model_dump_json())
    for name in ('CONFIG','UPSTREAM','LISTEN','MODE'):monkeypatch.delenv('AUTOTUNER_'+name,raising=False)
    opener=Mock(open=Mock(return_value=nullcontext(io.StringIO(json.dumps(result)))))
    monkeypatch.setattr(cli.urllib.request,'build_opener',Mock(return_value=opener))
    assert cli.main(['cache','explain','/items/1','--config',str(file),'--json'])==0
    assert json.loads(capsys.readouterr().out)['serving_enabled']
    assert opener.open.call_args.args[0].method=='GET'
    opener.open.return_value=nullcontext(io.StringIO(json.dumps(dict(schema_version=1,session_id='id',cache={}))))
    assert cli.main(['cache','clear','--config',str(file),'--json'])==0
    assert opener.open.call_args.args[0].method=='POST'
    assert cli.main(['cache','explain','/items?secret=x','--config',str(file)])==2
    opener.open.return_value=nullcontext(io.StringIO('{}'))
    assert cli.main(['cache','clear','--config',str(file)])==1
    opener.open.return_value=nullcontext(io.StringIO('bad json'))
    assert cli.main(['cache','clear','--config',str(file)])==1


def test_declared_report_dependencies_invalidate_without_evicting_unrelated_rule(config):
    payload=config.model_dump()
    payload['cache']['routes'] += [dict(id='report',match='/report',policy='cache',invalidate_on=[dict(method='PATCH',path='/items/*')]),
                                  dict(id='users',match='/users/*',policy='cache')]
    config=ProductConfig.model_validate(payload)
    state={'version':0}
    def origin(req):
        if req.method=='PATCH':state['version']+=1
        return response(b'user' if req.url.path.startswith('/users') else str(state['version']).encode())
    app,_=app_for(config,origin)
    async def scenario():
        for path in ('/items/1','/report','/users/1'):await get(app,path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:await c.patch('/items/1')
        assert (await get(app,'/users/1')).headers['x-autotuner-cache']=='HIT'
        for path in ('/items/1','/report'):
            result=await get(app,path)
            assert result.content==b'1' and result.headers['x-autotuner-cache']=='MISS'
        await app.proxy.close()
    asyncio.run(scenario())


def test_parallel_writes_keep_rule_blocked_until_last_completion(config):
    gates=[asyncio.Event(),asyncio.Event()];both=asyncio.Event()
    state={'writes':0}
    async def origin(req):
        if req.method=='PATCH':
            index=state['writes'];state['writes']+=1
            if state['writes']==2:both.set()
            await gates[index].wait()
        return response(str(state['writes']).encode())
    app,_=app_for(config,origin)
    async def scenario():
        await get(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
            tasks=[asyncio.create_task(c.patch('/items/1')) for _ in range(2)]
            await both.wait();assert app.proxy.cache.writes['items']==2
            gates[0].set();await tasks[0]
            assert app.proxy.cache.writes['items']==1
            assert (await get(app)).headers['x-autotuner-cache-reason']=='write_in_progress'
            assert not app.proxy.cache.entries
            gates[1].set();await tasks[1]
        assert app.proxy.cache.writes['items']==0
        assert (await get(app)).content==b'2'
        await app.proxy.close()
    asyncio.run(scenario())


def test_expiry_during_fill_and_too_small_entry_budget(config):
    cache=ResponseCache(config,clock=lambda:0,wall_clock=lambda:WALL)
    rule=config.cache.routes[0]
    fill,reason=cache.begin_fill('key',rule,0,[(b'cache-control',b'max-age=60')],200,received=0,sent=0)
    assert fill is not None
    cache.append(fill,b'payload')
    cache.clock=lambda:2
    assert not cache.publish(fill) and cache.used_bytes==0
    config.limits.cache_entry_bytes=1
    assert cache.begin_fill('key',rule,0,[(b'cache-control',b'max-age=60')],200,received=0,sent=0)==(None,'capacity')


def test_shared_staging_budget_bounds_parallel_streams(config):
    config.limits.cache_bytes=6000;config.limits.cache_entry_bytes=4000
    gate,both=asyncio.Event(),asyncio.Event();calls=[]
    def origin(req):
        calls.append(req)
        if len(calls)==2:both.set()
        return response(b'x'*3000,gate=gate)
    app,_=app_for(config,origin)
    original=app.proxy.cache._reserve
    def checked(cost):
        result=original(cost)
        assert app.proxy.cache.used_bytes<=6000
        return result
    app.proxy.cache._reserve=checked
    async def scenario():
        tasks=[asyncio.create_task(get(app,'/items/'+str(i))) for i in (1,2)]
        await both.wait();gate.set()
        results=await asyncio.gather(*tasks)
        assert all(r.content==b'x'*3000 for r in results)
        assert len(app.proxy.cache.entries)<=1 and app.proxy.cache.used_bytes<=6000
        app.proxy.cache.invalidate();assert app.proxy.cache.used_bytes==0
        await app.proxy.close()
    asyncio.run(scenario())


def test_hit_cancellation_releases_pin_and_hot_lane(config):
    app,_=app_for(config,lambda req:response())
    sent,gate=asyncio.Event(),asyncio.Event()
    async def scenario():
        await get(app,headers={'Accept':'application/json','Accept-Encoding':'identity'})
        scope=dict(type='http',method='GET',path='/items/1',raw_path=b'/items/1',query_string=b'',http_version='1.1',
                   headers=[(b'accept',b'application/json'),(b'accept-encoding',b'identity')])
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):
            if message['type']=='http.response.body' and message.get('more_body'):
                sent.set();await gate.wait()
        task=asyncio.create_task(app(scope,receive,send));await sent.wait()
        assert app.proxy.hot_reads==1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert app.proxy.hot_reads==0 and all(e.pins==0 for e in app.proxy.cache.entries.values())
        app.proxy.cache.invalidate();assert app.proxy.cache.used_bytes==0
        await app.proxy.close()
    asyncio.run(scenario())


def test_get_body_head_unmatched_and_no_freshness_are_bypasses(config):
    app,_=app_for(config,lambda req:response())
    async def scenario():
        await get(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
            assert (await c.request('GET','/items/1',content=b'body')).headers['x-autotuner-cache-reason']=='get_body'
            assert (await c.head('/items/1')).headers['x-autotuner-cache-reason']=='method'
            assert (await c.get('/other')).headers['x-autotuner-cache-reason']=='no_rule'
        await app.proxy.close()
    asyncio.run(scenario())


def test_cache_remains_correct_with_degraded_telemetry(config):
    from pathlib import Path
    Path(config.metrics.storage_path).mkdir()
    app,_=app_for(config,lambda req:response())
    async def scenario():
        await app.storage.start()
        assert app.storage.state=='degraded'
        assert (await get(app)).headers['x-autotuner-cache']=='MISS'
        assert (await get(app)).headers['x-autotuner-cache']=='HIT'
        assert app.storage.stats['dropped_storage']==3
        await app.storage.close();await app.proxy.close()
    asyncio.run(scenario())


def test_ram_only_fixture_and_persisted_cache_outcomes(config):
    from examples.cache_upstream import CacheUpstream
    fixture=CacheUpstream()
    payload=config.model_dump()
    payload.update(upstream='http://fixture',cache={'routes':[
        dict(id='products',match='/api/products',policy='cache',invalidate_on=[dict(method='PATCH',path='/api/products/*')]),
        dict(id='report',match='/api/reports/catalog',policy='cache',invalidate_on=[dict(method='PATCH',path='/api/products/*')])
    ]})
    cfg=ProductConfig.model_validate(payload)
    app=ControlSession(cfg,transport=httpx.ASGITransport(app=fixture))
    async def scenario():
        await app.storage.start()
        first=await get(app,'/api/products');second=await get(app,'/api/products')
        assert first.json()==second.json() and second.headers['x-autotuner-cache']=='HIT'
        assert (await get(app,'/api/reports/catalog')).json()['version']==0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
            assert (await c.patch('/api/products/1',json={'price_cents':3500})).status_code==200
            assert (await c.patch('/api/products/1',json={'price_cents':True})).status_code==422
            saved=(await c.get('/_autotuner/report')).json()
            assert saved['effective_mode']=='cache' and sum(r['cache_hits'] for r in saved['routes'])==1
            assert (await c.get('/_autotuner/insights')).json()['effective_mode']=='cache'
        assert (await get(app,'/api/products')).json()[0]['price_cents']==3500
        assert (await get(app,'/api/reports/catalog')).json()['version']==1
        await app.storage.close();await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('saturated',[False,True])
def test_hot_entry_expiring_during_body_validation_never_serves_stale(config,saturated):
    config.limits.upstream_concurrency=1;config.limits.queue_capacity=1
    block,origin_entered,queued=asyncio.Event(),asyncio.Event(),asyncio.Event()
    body_entered,body_release=asyncio.Event(),asyncio.Event()
    def origin(req):
        if req.url.path=='/items/2':origin_entered.set();return response(gate=block)
        return response()
    app,clock=app_for(config,origin)
    async def scenario():
        await get(app,headers={'Accept':'application/json','Accept-Encoding':'identity'})
        tasks=[]
        if saturated:
            tasks.append(asyncio.create_task(get(app,'/items/2')));await origin_entered.wait()
            acquire=app.proxy.slots.acquire
            async def observed():queued.set();return await acquire()
            app.proxy.slots.acquire=observed
            tasks.append(asyncio.create_task(get(app,'/items/3')));await queued.wait()
        scope=dict(type='http',method='GET',path='/items/1',raw_path=b'/items/1',query_string=b'',http_version='1.1',
                   headers=[(b'accept',b'application/json'),(b'accept-encoding',b'identity')])
        sent=[]
        async def receive():body_entered.set();await body_release.wait();return {'type':'http.request','body':b''}
        async def send(message):sent.append(message)
        pending=asyncio.create_task(app(scope,receive,send));await body_entered.wait()
        assert app.proxy.hot_reads==1
        clock[0]=3;body_release.set();await pending
        assert sent[0]['status']==(503 if saturated else 200)
        assert dict(sent[0]['headers'])[b'x-autotuner-cache']!=b'HIT'
        assert app.proxy.hot_reads==0
        block.set()
        if tasks:await asyncio.gather(*tasks)
        assert app.proxy.in_flight==0 and not app.proxy.cache.locks
        await app.proxy.close()
    asyncio.run(scenario())


def test_write_starting_during_cache_coordination_suppresses_fill(config):
    entered,gate=asyncio.Event(),asyncio.Event()
    app,_=app_for(config,lambda req:response())
    coordination=app.proxy.cache.coordination
    class Lock:
        async def acquire(self):entered.set();await gate.wait();return await real.acquire()
    real=None
    def blocked(key):
        nonlocal real
        real=coordination(key)
        return Lock()
    app.proxy.cache.coordination=blocked
    async def scenario():
        pending=asyncio.create_task(get(app));await entered.wait()
        ids=app.proxy.cache.begin_write('PATCH','/items/1')
        gate.set();result=await pending
        assert result.headers['x-autotuner-cache-reason']=='write_in_progress'
        assert not app.proxy.cache.entries
        app.proxy.cache.end_write(ids)
        assert not app.proxy.cache.locks and app.proxy.cache.used_bytes==0
        await app.proxy.close()
    asyncio.run(scenario())


def test_incomplete_length_and_known_oversize_do_not_store(config):
    app,_=app_for(config,lambda req:response(b'three',headers=[('Content-Length','20')]))
    async def incomplete():
        with pytest.raises(IncompleteResponse):await get(app)
        assert not app.proxy.cache.entries and app.proxy.cache.used_bytes==0
        await app.proxy.close()
    asyncio.run(incomplete())
    config.limits.cache_entry_bytes=500
    app,_=app_for(config,lambda req:response(b'x'*1000,headers=[('Content-Length','1000')]))
    async def oversized():
        result=await get(app)
        assert len(result.content)==1000 and result.headers['x-autotuner-cache-reason']=='entry_too_large'
        assert app.proxy.cache.used_bytes==0
        await app.proxy.close()
    asyncio.run(oversized())


def test_only_if_cached_never_dispatches_upstream(config):
    calls=[]
    app,_=app_for(config,lambda req:(calls.append(req) or response()))
    async def scenario():
        result=await get(app,headers={'Cache-Control':'only-if-cached'})
        assert result.status_code==504 and result.headers['x-autotuner-cache-reason']=='only_if_cached_unsupported'
        assert not calls
        await app.proxy.close()
    asyncio.run(scenario())


def test_origin_date_and_server_are_preserved_without_proxy_duplicates(config):
    app,_=app_for(config,lambda req:response(headers=[('Server','review-upstream')]))
    async def scenario():
        for _ in range(2):
            result=await get(app)
            assert result.headers.get_list('date')==[formatdate(WALL,usegmt=True)]
            assert result.headers.get_list('server')==['review-upstream']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
            assert len((await c.get('/_autotuner/status')).headers.get_list('date'))==1
        await app.proxy.close()
    asyncio.run(scenario())
    app,_=app_for(config,lambda req:httpx.Response(200,stream=Bytes(b'no date')))
    async def missing_date():
        result=await get(app)
        assert result.headers.get_list('date')==[formatdate(WALL,usegmt=True)]
        await app.proxy.close()
    asyncio.run(missing_date())


def test_uvicorn_proxy_configuration_adds_no_automatic_date_or_server():
    import uvicorn
    configuration=uvicorn.Config(lambda:None,date_header=False,server_header=False,log_config=None,access_log=False)
    configuration.load()
    server=uvicorn.Server(configuration)
    async def scenario():
        await server.on_tick(0)
        assert server.server_state.default_headers==[]
    asyncio.run(scenario())
