import asyncio
import io
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock, MagicMock

import httpx
import pytest

from tuneio import cli
from tuneio.console import timeseries
from tuneio.core.metrics import Metrics
from tuneio.product_config import ProductConfig
from tuneio.scenarios import Scenario,Result
from tuneio.session import ControlSession


def streaming(data=b'{}',headers=None):
    return httpx.Response(200,stream=httpx.ByteStream(data),headers=headers)


@pytest.fixture
def config(tmp_path):
    return ProductConfig(upstream='http://upstream',operations=[{'method':'GET','path':'/items'}],
        metrics={'storage_path':str(tmp_path/'metrics.sqlite3')},mode='cache',
        cache={'routes':[{'id':'items','match':'/items','policy':'cache'}]})


def test_assets_snapshot_playground_and_cache_isolation(config):
    async def run():
        received=[]
        async def upstream(request):
            received.append(request)
            return streaming(b'{"private":"secret"}',{'cache-control':'public,max-age=60'})
        session=ControlSession(config,transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session),base_url='http://local') as client:
            for path in ['dashboard','dashboard/','assets/product.css','assets/product_ui.js']:
                response=await client.get('/_autotuner/'+path)
                assert response.status_code==200 and response.headers['cache-control']=='no-store'
                assert "script-src 'self'" in response.headers['content-security-policy']
                if path=='dashboard':assert 'rel="icon" href="data:image/svg+xml,' in response.text
                assert (await client.post('/_autotuner/'+path)).status_code==405
            data=(await client.get('/_autotuner/dashboard/data')).json()
            assert data['metrics']['summary']['count']==0 and data['series'][0]['p95_ms'] is None
            assert len(data['policies'])==1 and 'dashboard' in data['status']['capabilities']
            assert 'cache' not in data['policies'][0] and 'decision_history' not in data['policies'][0]
            for _ in range(2):
                response=await client.post('/_autotuner/playground/request',json={'path':'/items'},headers={'Cookie':'ambient=secret','Cache-Control':'no-cache'})
                assert response.status_code==200 and response.json()['status']==200
                assert 'response_text' not in response.json()
            assert response.json()['cache']=='HIT' and len(received)==1
            assert 'cookie' not in received[0].headers and 'cache-control' not in received[0].headers
            data=(await client.get('/_autotuner/dashboard/data?window=300')).json()
            assert data['metrics']['summary']['count']==2 and data['status']['cache']['entries']==1
            assert 'secret' not in json.dumps(data) and sum(x['count'] for x in data['series'])==2
            inspected=(await client.post('/_autotuner/playground/request',json={'path':'/items','include_response':True})).json()
            assert 'secret' in inspected['response_text']
            presets=(await client.get('/_autotuner/dashboard/presets')).json()['presets']
            assert set(presets)=={'reads','invalidation','lifecycle'}
            assert (await client.get('/_autotuner/dashboard/data?window=0')).status_code==400
            assert (await client.get('/_autotuner/dashboard/data?window=bad')).status_code==400
            assert (await client.post('/_autotuner/dashboard/data')).status_code==405
            assert (await client.get('/_autotuner/scenarios/jobs/missing')).status_code==404
            assert (await client.get('/_autotuner/assets/missing')).status_code==404
        await session.proxy.close()
    asyncio.run(run())


@pytest.mark.parametrize('payload,status',[
    ({'path':'/_autotuner/status'},422),({'path':'//remote'},422),({'path':'/','headers':{'Host':'remote'}},422),
    ({'path':'/','extra':True},422),([] ,400),({'path':'/','body':'x'*65537},422)])
def test_console_validation(config,payload,status):
    async def run():
        session=ControlSession(config,transport=httpx.MockTransport(lambda r:httpx.Response(200)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session),base_url='http://local') as client:
            response=await client.post('/_autotuner/playground/request',json=payload)
            assert response.status_code==status and not session.proxy.metrics.records
        await session.proxy.close()
    asyncio.run(run())


def test_console_input_boundaries_and_errors(config,monkeypatch):
    async def run():
        session=ControlSession(config,transport=httpx.MockTransport(lambda r:httpx.Response(200)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session),base_url='http://local') as client:
            url='/_autotuner/playground/request'
            assert (await client.post(url,content='{}')).status_code==415
            assert (await client.post(url,content='{',headers={'Content-Type':'application/json'})).status_code==400
            assert (await client.post(url,content='x'*1048577,headers={'Content-Type':'application/json'})).status_code==413
            session.console.body_readers=4
            assert (await client.post(url,json={'path':'/'})).status_code==503
            session.console.body_readers=0
            import tuneio.console as console
            async def broken(*args):raise RuntimeError('secret')
            monkeypatch.setattr(console,'execute',broken)
            response=await client.post(url,json={'path':'/'})
            assert response.status_code==502 and 'secret' not in response.text
            async def timeout(*args):raise asyncio.TimeoutError()
            monkeypatch.setattr(console,'execute',timeout)
            assert (await client.post(url,json={'path':'/'})).status_code==504
            async def os_error(*args):raise OSError('secret')
            monkeypatch.setattr(console,'execute',os_error)
            assert (await client.post(url,json={'path':'/'})).status_code==502
            assert session.console.body_readers==0
        await session.proxy.close()
    asyncio.run(run())


def test_scenario_control_preview_run_stop_and_reconnect(config):
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        session=ControlSession(config,transport=httpx.MockTransport(lambda r:httpx.Response(200)))
        async def executor(spec):entered.set();await release.wait();return Result(201,1,[],b'{}',2,False)
        session.console.runner.executor=executor
        value=dict(name='Writes',steps=[dict(name='Create',method='POST',path='/items',expect_status=201)]*2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session),base_url='http://local') as client:
            preview=await client.post('/_autotuner/scenarios/preview',json={'scenario':value})
            assert preview.json()['writes']==2 and not entered.is_set()
            assert (await client.post('/_autotuner/scenarios/run',json={'scenario':value})).status_code==400
            response=await client.post('/_autotuner/scenarios/run',json={'scenario':value,'allow_writes':True})
            assert response.status_code==202
            identifier=response.json()['run_id'];await entered.wait()
            assert (await client.post('/_autotuner/scenarios/run',json={'scenario':value,'allow_writes':True})).status_code==409
            data=(await client.get('/_autotuner/dashboard/data')).json()
            assert data['scenario']['run_id']==identifier and 'results' not in data['scenario']
            assert (await client.post('/_autotuner/scenarios/stop',json={'run_id':'wrong'})).status_code==404
            assert (await client.post('/_autotuner/scenarios/stop',json={'run_id':identifier})).status_code==200
            release.set();await session.console.runner.close()
            report=(await client.get('/_autotuner/scenarios/jobs/'+identifier)).json()
            assert report['state']=='stopped' and report['completed']==1
        await session.proxy.close()
    asyncio.run(run())


def test_timeseries_boundaries_missing_latency_and_future():
    now=[100];metrics=Metrics(clock=lambda:now[0])
    metrics.record('GET','/',200,7,'MISS')
    now[0]=160;metrics.record('GET','/',200,2,'HIT')
    now[0]=161;metrics.record('GET','/',200,100,'MISS');now[0]=160
    series=timeseries(metrics,60)
    assert series[0]['count']==1 and series[-1]['count']==1 and series[-1]['cache_hits']==1
    assert sum(x['count'] for x in series)==2
    assert all(x['p95_ms'] is None for x in series[1:-1])


def test_console_disconnected_input_and_shutdown_waits_for_current(config):
    async def run():
        session=ControlSession(config,transport=httpx.MockTransport(lambda r:streaming()))
        entered,release=asyncio.Event(),asyncio.Event()
        async def executor(spec):entered.set();await release.wait();return Result(200,1,[],b'{}',2,False)
        session.console.runner.executor=executor
        async def disconnected():return {'type':'http.disconnect'}
        with pytest.raises(ValueError):await session.console.read_json({'headers':[(b'content-type',b'application/json')]},disconnected)
        async def idle():await asyncio.Event().wait()
        import tuneio.console as console
        original=console.asyncio.wait_for
        async def timeout(coro,seconds):coro.close();raise asyncio.TimeoutError()
        console.asyncio.wait_for=timeout
        try:
            with pytest.raises(asyncio.TimeoutError):await session.console.read_json({'headers':[(b'content-type',b'application/json')]},idle)
        finally:console.asyncio.wait_for=original
        report=session.console.runner.start(Scenario(name='Shutdown',steps=[{'name':'Read','path':'/','repeat':2}]))
        await entered.wait()
        closing=asyncio.create_task(session.console.runner.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set();await closing
        assert report['state']=='stopped' and report['completed']==1
        await session.proxy.close()
    asyncio.run(run())


@pytest.fixture
def cli_config(config,tmp_path,monkeypatch):
    file=tmp_path/'config.json';file.write_text(config.model_dump_json())
    monkeypatch.setattr(cli,'resolved_config',lambda _:config)
    return file


def test_dashboard_cli(cli_config,monkeypatch,capsys):
    open_browser=Mock(return_value=True);monkeypatch.setattr(cli.webbrowser,'open',open_browser)
    control=Mock(return_value={'phase':9,'running':True,'session_id':'test'});monkeypatch.setattr(cli,'local_json',control)
    assert cli.main(['dashboard','--json'])==0
    assert '/_autotuner/dashboard' in json.loads(capsys.readouterr().out)['url'];open_browser.assert_not_called()
    assert cli.main(['dashboard'])==0;open_browser.assert_called_once()
    assert cli.main(['dashboard','--no-browser'])==0
    open_browser.return_value=False
    assert cli.main(['dashboard'])==0
    control.return_value={'phase':8,'running':True}
    assert cli.main(['dashboard'])==1


def test_scenario_cli_preview_run_and_failures(cli_config,tmp_path,monkeypatch,capsys):
    file=tmp_path/'read.json';file.write_text(Scenario(name='CLI',steps=[{'name':'Read','path':'/items'}]).model_dump_json())
    assert cli.main(['scenario','preview',str(file),'--json'])==0
    assert json.loads(capsys.readouterr().out)['requests']==1
    result={}
    def control(config,method,path,payload=None):
        if method=='POST':result.update(schema_version=1,run_id=payload['run_id'],state='running')
        else:result['state']='completed'
        return dict(result)
    monkeypatch.setattr(cli,'local_json',control);monkeypatch.setattr(cli.time,'sleep',lambda _:None)
    output=tmp_path/'report.json'
    assert cli.main(['scenario','run',str(file),'--output',str(output)])==0
    assert json.loads(output.read_text())['state']=='completed'
    assert cli.main(['scenario','run',str(file),'--output',str(output)])==2
    assert cli.main(['scenario','run',str(file),'--output',str(output),'--force'])==0
    def fail(config,method,path,payload=None):return dict(schema_version=1,run_id=payload['run_id'],state='failed')
    monkeypatch.setattr(cli,'local_json',fail)
    assert cli.main(['scenario','run',str(file),'--json'])==3
    monkeypatch.setattr(cli,'local_json',lambda *a,**kw:dict(schema_version=1,state='unknown'))
    assert cli.main(['scenario','run',str(file)])==1
    file.write_text(Scenario(name='Write',steps=[{'name':'Write','method':'POST','path':'/items'}]).model_dump_json())
    assert cli.main(['scenario','run',str(file)])==2


def test_scenario_cli_ctrl_c_stops_after_current(cli_config,tmp_path,monkeypatch):
    file=tmp_path/'read.json';file.write_text(Scenario(name='CLI',steps=[{'name':'Read','path':'/items'}]).model_dump_json())
    calls=[];identifier=[None]
    def control(config,method,path,payload=None):
        calls.append(path)
        if path.endswith('/run'):identifier[0]=payload['run_id'];raise KeyboardInterrupt()
        return dict(schema_version=1,run_id=identifier[0],state='stopped')
    monkeypatch.setattr(cli,'local_json',control)
    assert cli.main(['scenario','run',str(file)])==0
    assert calls==['/_autotuner/scenarios/run','/_autotuner/scenarios/stop']


def test_local_json_validation(config,monkeypatch):
    opener=MagicMock();opener.open.return_value.__enter__.side_effect=lambda:io.StringIO('{"schema_version":1}')
    monkeypatch.setattr(cli.urllib.request,'build_opener',Mock(return_value=opener))
    assert cli.local_json(config,'POST','/control',{})=={'schema_version':1}
    for text in ('{bad','[]','{"schema_version":2}'):
        opener.open.return_value.__enter__.side_effect=lambda text=text:io.StringIO(text)
        with pytest.raises(cli.ControlError):cli.local_json(config,'GET','/control')


@pytest.mark.skipif(shutil.which('node') is None,reason='Optional Node is required for console JavaScript integration')
def test_console_json_contract_with_real_javascript(config):
    async def run():
        config.mode='adaptive';config.cache.routes[0].policy='adaptive'
        session=ControlSession(config,transport=httpx.MockTransport(lambda r:streaming(b'[]')))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session),base_url='http://local') as client:
            await client.post('/_autotuner/playground/request',json={'path':'/items'})
            data=(await client.get('/_autotuner/dashboard/data')).json()
        await session.proxy.close()
        return data
    data=asyncio.run(run())
    script="const ui=require(process.argv[1]);let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>{const data=JSON.parse(s);console.log(JSON.stringify({summary:ui.summary(data.metrics.summary),chart:ui.chartModel(data.series)}));});"
    output=subprocess.run(['node','-e',script,str(Path('tuneio/product_ui.js').resolve())],input=json.dumps(data),text=True,capture_output=True,check=True,timeout=10)
    result=json.loads(output.stdout)
    assert result['summary']['total']=='1' and len(result['chart'])==20
    assert sum(x['count'] for x in result['chart'])==1
    render_script="const {surface}=require(process.argv[1]);let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>{const data=JSON.parse(s);const dom=surface('#cache',null,data);setImmediate(()=>{if(dom.get('connection').textContent!=='CONNECTED')throw Error(dom.get('notice').textContent);if(dom.get('total').textContent!=='1')throw Error('Wrong rendered count');if(dom.get('policies').children.length!==1)throw Error('Missing rendered policy');console.log('real snapshot rendered');});});"
    rendered=subprocess.run(['node','-e',render_script,str(Path('tests/console_dom.cjs').resolve())],input=json.dumps(data),text=True,capture_output=True,check=True,timeout=10)
    assert 'real snapshot rendered' in rendered.stdout
