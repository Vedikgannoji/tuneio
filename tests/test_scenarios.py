import asyncio
import json
from pathlib import Path
import uuid

import httpx
import pytest
from pydantic import ValidationError

from tuneio.scenarios import (Scenario, RequestSpec, ScenarioRunner, ScenarioBusy,
    Result, execute, load_scenario, path_check, select, resolve, MAX_RESPONSE)
from tuneio.product_config import ProductConfig
from tuneio.session import ControlSession


def plan(**overrides):
    return Scenario.model_validate(dict(name='Review',steps=[dict(name='Read',path='/items')],**overrides))


def response(data=None,status=200,**overrides):
    payload=json.dumps(data).encode()
    return Result(status,1,[(b'x-autotuner-cache',b'MISS')],payload,len(payload),False,**overrides)


@pytest.mark.parametrize('path',['https://example.com','//example.com','/_autotuner','/_autotuner/status',
    '/x/../_autotuner/status','/%5fautotuner/status','/x#fragment','/a%00b','/a\\b','/a%0db'])
def test_reject_non_upstream_paths(path):
    with pytest.raises(ValueError):path_check(path)


@pytest.mark.parametrize('headers',[{'Host':'remote'},{'Content-Length':'1'},{'Proxy-Authorization':'secret'},
    {'x-test':'a\nb'},{'bad name':'x'},{'Connection':'keep-alive'},{'x-test':'é'}])
def test_unsupported_headers(headers):
    with pytest.raises(ValidationError):RequestSpec(path='/items',headers=headers)


@pytest.mark.parametrize('override',[
    {'cycles':0},{'cycles':True},{'cycles':11},{'delay_ms':5001},{'steps':[]},
    {'steps':[{'name':'Write','path':'/items','method':'POST','repeat':2}]},
    {'steps':[{'name':'Read','path':'/{{unknown}}'}]},
    {'steps':[{'name':'Read','path':'/','capture':{'run_id':'id'}}]},
    {'steps':[{'name':'Read','path':'/','capture':{'value':'__proto__'}}]},
    {'steps':[{'name':'Read','path':'/','checks':[{'field':'a.constructor','equals':1}]}]},
    {'steps':[{'name':'Read','path':'/','include_response':True}]},
    {'steps':[{'name':'Read','path':'/{{broken'}]},
    {'steps':[{'name':'Read','path':'/','body':{'{{key}}':1}}]},
    {'steps':[{'name':'Read','path':'/','body':float('nan')}]},
    {'steps':[{'name':'Read','path':'/','body':'x'*65537}]},
    {'steps':[{'name':'Read','path':'/','capture':{'value':'a'}}]*2},
    {'cycles':10,'steps':[{'name':'Read','path':'/','repeat':20}]*6},
])
def test_invalid_scenarios(override):
    value=dict(name='Review',steps=[{'name':'Read','path':'/'}]);value.update(override)
    with pytest.raises(ValidationError):Scenario.model_validate(value)


def test_nested_data_and_selectors(tmp_path):
    body=0
    for _ in range(14):body=[body]
    with pytest.raises(ValidationError):RequestSpec(path='/',body=body)
    assert select({'rows':[{'id':3}]},'rows.0.id')==3
    assert select(7,'$')==7
    with pytest.raises(KeyError):select(7,'id')
    assert resolve('/{{id}}',{'id':'a/b'},path=True)=='/a%2Fb'
    assert resolve({'id':'{{id}}','label':'item {{id}}'},{'id':7})=={'id':7,'label':'item 7'}
    file=tmp_path/'scenario.json';file.write_text(plan().model_dump_json(),encoding='utf-8-sig')
    assert load_scenario(file).preview()['requests']==1
    file.write_bytes(b'x'*1048577)
    with pytest.raises(ValueError):load_scenario(file)
    file.write_text('{')
    with pytest.raises(ValueError):load_scenario(file)


def test_preview_contains_no_secrets():
    value=Scenario(name='Preview',steps=[dict(name='Write',method='POST',path='/items?token=secret',
        headers={'Authorization':'secret'},body={'secret':'secret'})])
    preview=value.preview()
    assert preview['writes']==1 and preview['requests']==1
    assert 'secret' not in json.dumps(preview)


def test_ordered_captures_cycles_assertions_and_expected_errors():
    async def run():
        seen=[]
        async def executor(spec):
            seen.append(spec)
            if spec.method=='POST':return response({'id':len(seen)},201)
            return response({'id':int(spec.path.rsplit('/',1)[-1])},409)
        value=Scenario(name='Ordered',cycles=2,steps=[
            dict(name='Create',method='POST',path='/items',body={'name':'{{run_id}}-{{cycle}}'},expect_status=201,capture={'item':'id'}),
            dict(name='Check',path='/items/{{item}}',expect_status=409,checks=[{'field':'id','equals':'{{item}}'}])])
        runner=ScenarioRunner(executor)
        with pytest.raises(PermissionError):runner.start(value)
        result=runner.start(value,allow_writes=True)
        await runner.task
        assert result['state']=='completed' and result['completed']==4 and result['failed']==0
        assert [s.path for s in seen]==['/items','/items/1','/items','/items/3']
        assert seen[0].body!=seen[2].body
        exported=json.dumps(result)
        assert seen[0].body['name'] not in exported and '/items/1' not in exported
        assert runner.current() is result
        await runner.close()
    asyncio.run(run())


@pytest.mark.parametrize('data,step,expected',[
    ({'id':1},{'expect_status':201},'unexpected_status'),
    ({'id':1},{'checks':[{'field':'absent','equals':1}]},'check_field_missing'),
    ({'id':1},{'checks':[{'field':'id','equals':True}]},'response_assertion_failed'),
    ({'id':1},{'capture':{'item':'absent'}},'capture_field_missing'),
    ({'id':{}},{'capture':{'item':'id'}},'capture_not_scalar'),
    ({'id':'x'*257},{'capture':{'item':'id'}},'capture_not_scalar'),
    ({'id':float('nan')},{'capture':{'item':'id'}},'capture_not_scalar'),
])
def test_failure_stops_dependencies(data,step,expected):
    async def run():
        calls=[]
        async def executor(spec):calls.append(spec);return response(data)
        runner=ScenarioRunner(executor)
        value=Scenario(name='Failure',steps=[dict(name='First',path='/',**step),dict(name='Never',path='/never')])
        report=runner.start(value);await runner.task
        assert len(calls)==1 and report['failed']==1 and report['state']=='failed'
        assert report['results'][0]['reason']==expected
    asyncio.run(run())


@pytest.mark.parametrize('kind,expected',[('truncated','response_too_large'),('invalid','invalid_json'),('exception','request_failed')])
def test_response_errors(kind,expected):
    async def run():
        async def executor(spec):
            if kind=='exception':raise RuntimeError('secret exception')
            return Result(200,1,[],b'{invalid' if kind=='invalid' else b'{}',2,kind=='truncated')
        runner=ScenarioRunner(executor)
        report=runner.start(Scenario(name='Errors',steps=[dict(name='Read',path='/',capture={'item':'id'})]));await runner.task
        assert report['results'][0]['reason']==expected
        assert 'secret' not in json.dumps(report)
    asyncio.run(run())


def test_stop_after_current_without_retry_and_idempotency():
    async def run():
        entered,release=asyncio.Event(),asyncio.Event();calls=[]
        async def executor(spec):calls.append(spec);entered.set();await release.wait();return response({})
        runner=ScenarioRunner(executor);value=Scenario(name='Stop',steps=[dict(name='Read',path='/',repeat=3)])
        identifier=str(uuid.uuid4());report=runner.start(value,identifier=identifier)
        assert runner.start(value,identifier=identifier) is report
        with pytest.raises(ValueError):runner.start(plan(),identifier=identifier)
        with pytest.raises(ScenarioBusy):runner.start(value)
        with pytest.raises(KeyError):runner.stop('wrong')
        await entered.wait();runner.stop(identifier)
        assert not runner.task.done()
        release.set();await runner.close()
        assert report['state']=='stopped' and len(calls)==1 and report['completed']==1
        for _ in range(4):runner.start(plan());await runner.task
        assert len(runner.jobs)==3 and identifier not in runner.jobs
    asyncio.run(run())


def test_stop_during_pacing_and_runtime_limit():
    async def run():
        runner=None
        async def executor(spec):
            asyncio.get_running_loop().call_soon(runner.stop,runner.current()['run_id'])
            return response({})
        runner=ScenarioRunner(executor)
        result=runner.start(Scenario(name='Pacing',delay_ms=5000,steps=[dict(name='Read',path='/',repeat=2)]));await runner.task
        assert result['state']=='stopped' and result['completed']==1
        ticks=iter([0,601]);runner=ScenarioRunner(executor,clock=lambda:next(ticks))
        result=runner.start(plan());await runner.task
        assert result['state']=='runtime_limit' and result['completed']==0
    asyncio.run(run())


def test_cancellation_is_explicit():
    async def run():
        entered=asyncio.Event()
        async def executor(spec):entered.set();await asyncio.Event().wait()
        runner=ScenarioRunner(executor);result=runner.start(plan());await entered.wait();runner.task.cancel()
        with pytest.raises(asyncio.CancelledError):await runner.task
        assert result['state']=='interrupted' and result['finished_at'] is not None
    asyncio.run(run())


def test_pacing_occurs_between_requests_not_after_final_request(monkeypatch):
    async def run():
        async def executor(spec):return response({})
        async def no_pacing(*args):raise AssertionError('No delay after the last request')
        monkeypatch.setattr(asyncio,'wait_for',no_pacing)
        runner=ScenarioRunner(executor);report=runner.start(plan(delay_ms=5000));await runner.task
        assert report['state']=='completed'
    asyncio.run(run())


def test_bounded_executor_does_not_capture_by_default(tmp_path):
    async def run():
        seen=[]
        async def upstream(request):
            seen.append(request)
            return httpx.Response(200,stream=httpx.ByteStream(b'x'*(MAX_RESPONSE+200)),headers={'Set-Cookie':'secret'})
        config=ProductConfig(upstream='http://upstream.test',metrics={'storage_path':str(tmp_path/'metrics.sqlite3')})
        session=ControlSession(config,transport=httpx.MockTransport(upstream))
        result=await execute(session.proxy,RequestSpec(path='/items',method='POST',body={'id':2}))
        assert result.body_bytes==MAX_RESPONSE+200 and result.truncated and len(result.content)==MAX_RESPONSE
        assert 'response_text' not in result.public() and 'secret' not in json.dumps(result.public())
        assert 'secret' in json.dumps(result.public(True))
        assert json.loads(seen[0].content)=={'id':2} and 'cookie' not in seen[0].headers
        assert session.proxy.metrics.snapshot()['summary']['count']==1
        await session.proxy.close()
    asyncio.run(run())


def test_bundled_lifecycle_against_isolated_demo(app,tmp_path):
    async def run():
        config=ProductConfig(upstream='http://demo',metrics={'storage_path':str(tmp_path/'telemetry.sqlite3')})
        session=ControlSession(config,transport=httpx.ASGITransport(app=app))
        value=load_scenario(Path('examples/scenarios/lifecycle.json'));value.cycles=2
        runner=session.console.runner;report=runner.start(value,allow_writes=True);await runner.task
        assert report['state']=='completed',report
        assert report['completed']==22 and report['failed']==0
        summary=session.proxy.metrics.snapshot()['summary']
        assert summary['count']==22 and summary['error_rate']==pytest.approx(4/22)
        assert app.state.db.get('orders',1)['status']=='shipped'
        assert app.state.db.get('orders',2)['unit_price_cents']==2000
        await session.proxy.close()
    asyncio.run(run())


def test_fixture_presets_learn_and_invalidate_without_application_database(tmp_path):
    from examples.cache_upstream import CacheUpstream
    async def run():
        data=json.loads(Path('examples/adaptive-demo.json').read_text())
        data['metrics']['storage_path']=str(tmp_path/'telemetry.sqlite3')
        config=ProductConfig.model_validate(data);fixture=CacheUpstream()
        session=ControlSession(config,transport=httpx.ASGITransport(app=fixture))
        # Control origin measurements so activation doesn't depend on Windows timer granularity.
        from itertools import count
        ticks=count();session.proxy.cache.clock=lambda:next(ticks)/1000
        for name in ('reads','invalidation'):
            report=session.console.runner.start(load_scenario(Path('examples/scenarios')/(name+'.json')),allow_writes=True)
            await session.console.runner.task
            assert report['state']=='completed',report
        assert fixture.product['price_cents']==3500 and fixture.version==1
        assert session.proxy.cache.counters['hits']>0 and session.proxy.cache.counters['invalidations']>0
        assert session.proxy.metrics.snapshot()['summary']['count']==25
        await session.proxy.close()
    asyncio.run(run())
