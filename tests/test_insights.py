from tuneio import __version__
import asyncio
from contextlib import nullcontext
import io
import json
import sqlite3
from unittest.mock import Mock

import httpx
import pytest

from tuneio import cli
from tuneio.core import Metrics
from tuneio.core.insights import analyze
from tuneio.product_config import ProductConfig
from tuneio.session import ControlSession


@pytest.fixture
def config(tmp_path):
    return ProductConfig(upstream='https://upstream.example',operations=[{'method':'GET','path':'/items/*'}],
                         metrics={'storage_path':str(tmp_path/'metrics.sqlite3')},
                         cache={'routes':[{'id':'items','match':'/items/*'}]})


def histories(values=(), *, baseline=1000, proxy_duration=10, queue=0, status=200, key='same'):
    clock=[baseline]
    proxy,upstream=Metrics(clock=lambda:clock[0]),Metrics(clock=lambda:clock[0])
    for value in values:
        upstream.record('GET','/items/*',status,value,'BYPASS',request_key=key,cache_review_reason='review_required')
        proxy.record('GET','/items/*',status,proxy_duration,'BYPASS',queue_ms=queue)
        clock[0]+=.01
    return proxy,upstream,clock


def inspect(config,p,u,clock,**kwargs):
    return analyze(config,p,u,clock=lambda:clock[0],**kwargs)


def codes(result):return {f['code'] for f in result['findings']}


@pytest.mark.parametrize('baseline,recent,expected',[(3.04,4.68,False),(10,20,True),(10,15,True),(10,14.99,False),(0,20,False)])
def test_regression_requires_both_thresholds(config,baseline,recent,expected):
    p,u,clock=histories([baseline]*5+[recent]*5)
    result=inspect(config,p,u,clock)
    assert ('upstream_regression' in codes(result))==expected
    if expected:
        finding=next(f for f in result['findings'] if f['code']=='upstream_regression')
        assert finding['evidence']['samples_per_cohort']==5
        assert finding['evidence']['baseline_ms']==pytest.approx(baseline)
        assert finding['evidence']['recent_ms']==pytest.approx(recent)
        assert not finding['applied'] and finding['boundary']=='upstream'


def test_regression_expiry_hits_errors_future_and_method_separation(config):
    p,u,clock=histories([10]*5+[20]*5)
    for method,status,cache in [('GET',200,'HIT'),('GET',500,'BYPASS'),('POST',200,'BYPASS')]:
        u.record(method,'/items/*',status,5000,cache)
    result=inspect(config,p,u,clock)
    assert 'upstream_regression' in codes(result)
    clock[0]+=302
    assert 'upstream_regression' not in codes(inspect(config,p,u,clock,window=3600))
    u.record('GET','/items/*',200,9000,'BYPASS')
    clock[0]-=1
    assert all(e['regression_samples']==0 for e in inspect(config,p,u,clock,window=3600)['evaluations'])


def test_minimum_samples_empty_and_fast_states(config):
    p,u,clock=histories()
    assert inspect(config,p,u,clock)['state']=='no_recent_traffic'
    for _ in range(3):u.record('GET','/items/*',200,1,'BYPASS',cache_review_reason='review_required',request_key='a')
    result=inspect(config,p,u,clock)
    assert result['state']=='collecting_samples'
    assert result['cache_suitability'][0]['state']=='insufficient_samples'
    for _ in range(2):u.record('GET','/items/*',200,1,'BYPASS',cache_review_reason='review_required',request_key='a')
    result=inspect(config,p,u,clock)
    assert result['state']=='no_thresholds_exceeded' and not result['findings']
    assert result['cache_suitability'][0]['state']=='fast_origin'


def test_latency_tail_queue_and_errors_have_separate_evidence(config):
    p,u,clock=histories([300]*20,proxy_duration=1000,queue=500,status=200)
    for i,row in enumerate(p.records):row['status']=422 if i==0 else 500 if i==1 else 200
    result=inspect(config,p,u,clock)
    assert {'slow_upstream','upstream_tail_latency','admission_pressure','operation_errors'}<=codes(result)
    error=next(f for f in result['findings'] if f['code']=='operation_errors')
    assert error['evidence']['error_rate']==.1 and error['evidence']['server_errors']==1
    queue=next(f for f in result['findings'] if f['code']=='admission_pressure')
    assert queue['evidence']['queue_fraction']==.5
    assert next(f for f in result['findings'] if f['code']=='slow_upstream')['evidence']['mean_ms']==300
    assert not result['recommendations_applied']


@pytest.mark.parametrize('count,wait,duration,expected',[(4,100,200,False),(5,4,8,False),(5,5,100,False),(5,5,20,True),(5,0,0,False)])
def test_queue_requires_volume_absolute_and_relative_wait(config,count,wait,duration,expected):
    p,u,clock=histories([1]*count,proxy_duration=duration,queue=wait)
    assert ('admission_pressure' in codes(inspect(config,p,u,clock)))==expected
    for row in p.records:row.pop('queue_ms')
    assert 'admission_pressure' not in codes(inspect(config,p,u,clock))


def test_suitability_requires_same_key_recent_reuse_and_rule(config):
    p,u,clock=histories([3]*4)
    result=inspect(config,p,u,clock)
    assert result['cache_suitability'][0]['state']=='policy_review_candidate'
    assert not result['cache_suitability'][0]['approved_for_caching']
    assert '"same"' not in json.dumps(result)
    for i,row in enumerate(u.records):row['request_key']=str(i)
    assert inspect(config,p,u,clock)['cache_suitability'][0]['state']=='insufficient_key_reuse'
    for row in u.records:row['request_key']='same'
    clock[0]+=61
    assert inspect(config,p,u,clock)['cache_suitability'][0]['state']=='insufficient_key_reuse'
    clock[0]-=61
    config.cache.routes=[]
    assert inspect(config,p,u,clock)['cache_suitability'][0]['state']=='explicit_rule_required'


def test_suitability_rejects_private_missing_and_failed_evidence(config):
    p,u,clock=histories([3]*5)
    for row in u.records:row['cache_review_reason']='credentials'
    result=inspect(config,p,u,clock)
    assert result['cache_suitability'][0]['state']=='no_screened_public_reads'
    assert result['cache_suitability'][0]['screening_reasons']=={'credentials':5}
    assert 'cache_policy_review' not in codes(result)
    for row in u.records:row['cache_review_reason']='review_required'
    u.record('GET','/items/*',500,50,'BYPASS',cache_review_reason='response_status')
    assert inspect(config,p,u,clock)['cache_suitability'][0]['state']=='upstream_errors'
    u.records.clear()
    assert inspect(config,p,u,clock)['cache_suitability'][0]['state']=='no_completed_upstream_samples'


def test_quality_and_lifetime_failures_remain_explicit(config):
    p,u,clock=histories()
    p.evicted=10
    result=inspect(config,p,u,clock,storage={'state':'degraded','dropped_queue':2,'sampling_incomplete':True},
                   transport={'failed_requests':1,'incomplete_responses':2,'overload_rejections':3})
    assert codes(result)=={'telemetry_quality','transport_failures'}
    assert result['evidence_quality']['window_may_be_incomplete']
    assert result['evidence_quality']['storage_sampling_incomplete']
    assert next(f for f in result['findings'] if f['code']=='transport_failures')['thresholds']['scope']=='lifetime since session start'


@pytest.mark.parametrize('window',[0,3601,True,'300',None])
def test_invalid_window(config,window):
    p,u,clock=histories()
    with pytest.raises(ValueError):inspect(config,p,u,clock,window=window)


class Bytes(httpx.AsyncByteStream):
    async def __aiter__(self):yield b'{"ok":true}'


@pytest.mark.parametrize('request_headers,response_headers,status,reason',[
    ({'Authorization':'Bearer secret'},{},200,'credentials'),
    ({'Cookie':'private=value'},{},200,'credentials'),
    ({'Range':'bytes=0-2'},{},200,'conditional_or_range'),
    ({'If-None-Match':'tag'},{},200,'conditional_or_range'),
    ({'Cache-Control':'no-cache'},{},200,'request_directives'),
    ({},{'Set-Cookie':'secret=value'},200,'set_cookie'),
    ({},{'Cache-Control':'private'},200,'response_directives'),
    ({},{'Vary':'X-Customer'},200,'unsupported_vary'),
    ({},{'Vary':'*'},200,'unsupported_vary'),
    ({},{},500,'response_status'),
    ({},{'Vary':'Accept, Accept-Encoding'},200,'review_required'),
])
def test_proxy_advisory_screen_preserves_forwarding(config,request_headers,response_headers,status,reason):
    calls=[]
    def upstream(req):
        calls.append(req)
        return httpx.Response(status,headers=response_headers,stream=Bytes())
    app=ControlSession(config,transport=httpx.MockTransport(upstream))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as client:
            response=await client.get('/items/1?secret=unretained',headers=request_headers)
            assert response.status_code==status and response.content==b'{"ok":true}'
            row=app.proxy.upstream_metrics.records[-1]
            assert row['cache_review_reason']==reason
            assert bool(row['request_key'])==(reason=='review_required')
            data=(await client.get('/_autotuner/insights')).json()
            assert data['tool_version']==__version__ and data['session_id']==app.session_id
            assert not data['recommendations_applied']
            assert 'unretained' not in json.dumps(data) and 'secret=value' not in json.dumps(data)
            assert (await client.get('/_autotuner/insights?window=0')).status_code==400
        assert len(calls)==1
        await app.proxy.close()
    asyncio.run(scenario())


def test_reuse_fingerprints_isolate_queries_variants_and_sessions(config):
    app=ControlSession(config,transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Bytes())))
    other=ControlSession(config,transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Bytes())))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as client:
            for path,headers in [('/items/1?a=1&a=2',{}),('/items/1?a=1&a=2',{}),('/items/1?a=2&a=1',{}),
                                 ('/items/2?a=1&a=2',{}),('/items/1?a=1&a=2',{'Accept':'text/plain'})]:
                await client.get(path,headers=headers)
            await client.request('GET','/items/1',content=b'body')
            await client.post('/items/1')
        rows=list(app.proxy.upstream_metrics.records)
        assert rows[0]['request_key']==rows[1]['request_key']
        assert len({r['request_key'] for r in rows[:5]})==4
        assert rows[-2]['cache_review_reason']=='get_body' and rows[-1]['cache_review_reason']=='method'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other),base_url='http://proxy') as client:
            await client.get('/items/1?a=1&a=2')
        assert other.proxy.upstream_metrics.records[-1]['request_key']!=rows[0]['request_key']
        await app.proxy.close();await other.proxy.close()
    asyncio.run(scenario())


def test_advisory_metadata_never_persists_to_sqlite(config):
    app=ControlSession(config,transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Bytes())))
    async def scenario():
        await app.storage.start()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as client:
            await client.get('/items/1?token=never-store-this')
            fingerprint=app.proxy.upstream_metrics.records[-1]['request_key']
            saved=(await client.get('/_autotuner/report')).json()
            assert 'request_key' not in json.dumps(saved)
        await app.storage.close();await app.proxy.close()
        data=open(config.metrics.storage_path,'rb').read()
        assert fingerprint.encode() not in data and b'never-store-this' not in data
    asyncio.run(scenario())


def test_insights_cli_validates_response_and_window(config,tmp_path,monkeypatch,capsys):
    path=tmp_path/'config.json';path.write_text(config.model_dump_json())
    for name in ('CONFIG','UPSTREAM','LISTEN','MODE'):monkeypatch.delenv('AUTOTUNER_'+name,raising=False)
    opener=Mock()
    monkeypatch.setattr(cli.urllib.request,'build_opener',Mock(return_value=opener))
    def answer(data):opener.open.return_value=nullcontext(io.StringIO(json.dumps(data)))
    payload=dict(schema_version=1,session_id='id',recommendations_applied=False,findings=[],cache_suitability=[])
    answer(payload)
    assert cli.main(['insights','--config',str(path),'--json'])==0
    assert json.loads(capsys.readouterr().out)==payload
    assert opener.open.call_args.args[0].endswith('/_autotuner/insights?window=300')
    assert cli.main(['insights','--config',str(path),'--window','0'])==2
    for bad in ({},dict(payload,findings={}),dict(payload,recommendations_applied=True)):
        answer(bad)
        assert cli.main(['insights','--config',str(path),'--json'])==1


def test_timeout_does_not_count_queue_wait_twice(config,monkeypatch):
    import tuneio.proxy as module
    clock=[10.]
    monkeypatch.setattr(module,'perf_counter',lambda:clock[0])
    def upstream(req):raise httpx.ReadTimeout('private failure')
    app=ControlSession(config,transport=httpx.MockTransport(upstream))
    acquire=app.proxy.slots.acquire
    async def measured_acquire():
        await acquire();clock[0]+=.005
    monkeypatch.setattr(app.proxy.slots,'acquire',measured_acquire)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as client:
            assert (await client.get('/items/1')).status_code==504
        assert app.proxy.counters['queue_ms']==pytest.approx(5.)
        assert app.proxy.metrics.records[-1]['queue_ms']==pytest.approx(5.)
        await app.proxy.close()
    asyncio.run(scenario())


def test_tail_uses_nearest_rank_and_regression_needs_ten_samples(config):
    p,u,clock=histories([1]*19+[1000])
    assert 'upstream_tail_latency' not in codes(inspect(config,p,u,clock))
    p,u,clock=histories([1]*18+[1000]*2)
    assert 'upstream_tail_latency' in codes(inspect(config,p,u,clock))
    p,u,clock=histories([10]*4+[20]*5)
    assert 'upstream_regression' not in codes(inspect(config,p,u,clock))


def test_rule_covering_literal_operation_is_found_and_analysis_does_not_mutate(config):
    p,u,clock=histories([3]*4)
    for row in list(p.records)+list(u.records):row['route']='/items/42'
    before=config.model_dump()
    result=inspect(config,p,u,clock)
    assert result['cache_suitability'][0]['rule_id']=='items'
    assert result['cache_suitability'][0]['state']=='policy_review_candidate'
    assert config.model_dump()==before
    result['thresholds']['slow_mean_ms']=1
    assert inspect(config,p,u,clock)['thresholds']['slow_mean_ms']==100
