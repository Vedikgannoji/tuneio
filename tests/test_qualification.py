"""Coordinator contracts use isolated ASGI/fake process boundaries, not timing claims."""
import asyncio
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from tuneio import benchmarking as b,cli,quality
from tuneio.qualification_fixture import QualificationFixture,seed,reference,encode
from tuneio.session import ControlSession


@pytest.mark.parametrize('values',[{'requests':19},{'requests':True},{'concurrency':0},{'concurrency':65},
    {'repeats':2},{'repeats':11},{'orders':7},{'rate':0},{'duration_seconds':0},{'duration_seconds':3601},
    {'profile':'remote'},{'upstream':'http://remote'},{'requests':10000,'repeats':10},
    {'requests':10000,'rate':1},{'profile':'soak','requests':1000,'rate':1}])
def test_workload_validation(values):
    with pytest.raises(ValidationError):b.Options(**values)


def test_reference_fixture_is_fixed_size_real_sql_and_refuses_overwrite(tmp_path):
    path=tmp_path/'data.sqlite3';seed(path,17);app=QualificationFixture(path)
    assert app.report()==reference(17) and len(app.report())==1024
    assert json.loads(app.mutate())=={'version':1}
    assert app.report()==reference(17,1)
    with pytest.raises(FileExistsError):seed(path,17)
    with pytest.raises(ValueError):encode({'oversize':'x'*1024})
    path.unlink()  # All SQL handles must close, including on Windows.


def test_fixture_protocol_and_lifecycle(tmp_path):
    path=tmp_path/'data.sqlite3';seed(path,8);app=QualificationFixture(path)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
            for target,status in [('/health',200),('/stats',200),('/payload',200),('/report',200),('/fault',503),('/missing',404)]:
                response=await client.get(target);assert response.status_code==status and len(response.content)==1024
            assert (await client.post('/mutate')).json()['version']==1
            assert (await client.get('/stats')).json()=={'reads':2,'writes':1,'failures':1}
        events=iter([{'type':'lifespan.startup'},{'type':'lifespan.shutdown'}]);sent=[]
        async def receive():return next(events)
        async def send(event):sent.append(event)
        await app({'type':'lifespan'},receive,send)
        assert sent==[{'type':'lifespan.startup.complete'},{'type':'lifespan.shutdown.complete'}]
        await app({'type':'websocket'},receive,send)
    asyncio.run(run())


@pytest.mark.parametrize('workload',(*b.WORKLOADS,'soak'))
def test_load_correctness_status_hashes_and_explicit_mutation_barriers(tmp_path,workload):
    async def run():
        path=tmp_path/'data.sqlite3';seed(path,8);app=QualificationFixture(path)
        result=await b.load('http://fixture',b.Options(requests=40,orders=8,rate=1000),workload,
                            transport=httpx.ASGITransport(app=app))
        assert result['requests']==40 and result['unexpected_failures']==0
        assert all(x['correct'] for x in result['samples'])
        assert len(result['samples'])==40
        assert app.writes==result['final_version']
        if workload=='soak':assert result['errors']>0  # Expected failures remain real HTTP errors.
        elif workload!='mixed':assert app.writes==0
    asyncio.run(run())


def test_load_retains_corrupt_status_and_network_failures_without_retry():
    async def run():
        calls=[]
        def handler(request):
            calls.append(request)
            if len(calls)==1:raise httpx.ConnectError('private endpoint')
            return httpx.Response(200,content=b'incorrect')
        result=await b.load('http://fixture',b.Options(requests=20,orders=8,rate=1000),'repeated',transport=httpx.MockTransport(handler))
        assert len(calls)==20 and result['unexpected_failures']==20 and result['errors']==1
        assert result['samples'][0]['response_sha256'] is None
        assert 'private endpoint' not in json.dumps(result)
        with pytest.raises(ValueError):await b.load('http://fixture',b.Options(),'unknown')
    asyncio.run(run())


def test_measure_reconciles_actual_proxy_cache_and_persistence(tmp_path,monkeypatch):
    async def run():
        path=tmp_path/'data.sqlite3';seed(path,16);fixture=QualificationFixture(path)
        options=b.Options(requests=20,orders=16,rate=1000)
        config=b.config_for('adaptive','http://fixture',tmp_path/'metrics.sqlite3')
        config.tuning.enable_ms=.0001;config.tuning.retain_ms=.00001
        session=ControlSession(config,transport=httpx.ASGITransport(app=fixture))
        await session.storage.start()
        proxy=SimpleNamespace(process=SimpleNamespace(pid=123))
        monkeypatch.setattr(b,'rss',lambda pid:100)
        try:
            result=await b.measure('http://proxy','http://fixture',options,'repeated',proxy=proxy,
                transport=httpx.ASGITransport(app=session),origin_transport=httpx.ASGITransport(app=fixture))
            assert result['correctness_passed'] and result['monitoring_reconciled']
            assert result['cache']['hits']>0 and result['origin_loads']['reads']<20
            assert result['peak_proxy_rss_bytes']==100
            assert result['runtime']['in_flight']==0
            assert result['storage']['persisted_observations']>0
        finally:await session.proxy.close();await session.storage.close()
    asyncio.run(run())


def test_measure_direct_and_bad_warmup(tmp_path):
    async def run():
        path=tmp_path/'data.sqlite3';seed(path,8);fixture=QualificationFixture(path)
        transport=httpx.ASGITransport(app=fixture)
        result=await b.measure('http://fixture','http://fixture',b.Options(requests=20,orders=8,rate=1000),'unique',
            transport=transport,origin_transport=transport)
        assert result['correctness_passed'] and result['origin_loads']['reads']==20
        assert result['idle_proxy_rss_bytes'] is None
        with pytest.raises(RuntimeError,match='warmup'):
            await b.measure('http://fixture','http://fixture',b.Options(), 'unique',origin_transport=transport)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(500)),base_url='http://fixture') as client:
            with pytest.raises(RuntimeError):await b.control(client,'/stats')
    asyncio.run(run())


class FakeChild:
    all=[]
    def __init__(self,kind,path):
        self.kind=kind;self.path=Path(path);self.url='http://fixture';self.closed=False;self.graceful=True
        self.process=SimpleNamespace(pid=123)
        self.config=json.loads(self.path.read_text()) if kind=='proxy' else None
        self.all.append(self)
    def close(self):self.closed=True


async def fake_measure(base,origin,options,workload,*,proxy=None,fixture=None):
    mode=proxy.config['mode'] if proxy else 'direct'
    mean=5 if mode=='adaptive' else 10
    return dict(requests=options.requests,mean_ms=mean,p50_ms=mean,p95_ms=mean,throughput_rps=100,errors=0,
        unexpected_failures=0,correctness_passed=True,idle_proxy_rss_bytes=100,peak_proxy_rss_bytes=200,
        cache={'accounted_bytes':100,'peak_accounted_bytes':100,'byte_budget':200},samples=[],
        storage={'persistent':True,'state':'ready','write_errors':0})


def test_paired_orchestration_reverses_order_and_records_reproducible_identity(monkeypatch):
    FakeChild.all=[]
    result=b.run(b.Options(orders=8),child_factory=FakeChild,measure_fn=fake_measure)
    assert len(result['rounds'])==12 and all(p.closed for p in FakeChild.all)
    assert result['rounds'][0]['order']==list(b.MODES)
    assert result['rounds'][4]['order']==list(reversed(b.MODES))
    assert result['gates']['repeated_read_benefit']['values']==[50]*3
    assert result['gates']['correctness']['state']=='passed'
    assert result['gates']['stability_30_minutes']['state']=='not_measured'
    assert result['qualification_status']=='review_required'
    a=result['rounds'][0]['modes']['off'];other=result['rounds'][4]['modes']['off']
    assert a['config_sha256']==other['config_sha256']
    assert b.digest(json.dumps(a['effective_config'],sort_keys=True).encode())==a['config_sha256']
    assert b.exit_code(result)==0 and b.exit_code(result,True)==3
    assert 'source_digest' in b.report_csv(result)
    host=dict(result['environment'],python='3.12.9',physical_cpus=4,ram_bytes=8*1024**3)
    assert b.comparisons(result['rounds'],b.Options(reference_host=True),host)['reference_environment']['state']=='confirmed'
    host['python']='3.13.0'
    assert b.comparisons(result['rounds'],b.Options(reference_host=True),host)['reference_environment']['state']=='not_confirmed'
    baseline=result['rounds'][0]['modes']['direct']
    baseline['throughput_rps']=71;baseline['p95_ms']=1000
    invalid=b.comparisons(result['rounds'],b.Options(),host)
    assert invalid['correctness']['state']=='passed' and invalid['offered_rate']['state']=='failed'
    assert invalid['observe_p95_overhead']['state']=='invalid_workload'
    assert invalid['observe_p95_overhead']['threshold_passed'] is True
    assert len(invalid['observe_p95_overhead']['values'])==3  # No discarded pair or favorable rerun.
    baseline['throughput_rps']=100
    result['rounds'][0]['modes']['direct']['correctness_passed']=False
    gated=b.comparisons(result['rounds'],b.Options(),result['environment'])
    assert gated['correctness']['state']=='failed'
    assert gated['repeated_read_benefit']==dict(state='blocked_by_correctness',values=[])
    assert b.exit_code(dict(result,gates=gated))==3


def test_diagnostic_busy_retry_never_retries_load_requests(monkeypatch):
    async def run():
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(503) if len(calls)<3 else httpx.Response(200,json={'ready':True})
        async def yield_only(seconds):pass
        monkeypatch.setattr(asyncio,'sleep',yield_only)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://fixture') as client:
            assert await b.control(client,'/stats')=={'ready':True}
        assert len(calls)==3
    asyncio.run(run())


def test_orchestration_closes_only_owned_children_on_failure():
    async def fail(*args,**kwargs):raise httpx.ConnectError('hidden')
    FakeChild.all=[]
    with pytest.raises(RuntimeError):b.run(b.Options(orders=8),child_factory=FakeChild,measure_fn=fail)
    assert len(FakeChild.all)==1 and FakeChild.all[0].closed


@pytest.mark.parametrize('message',['{}','not-json','{"schema_version":1,"port":false}','{"schema_version":1,"port":70000}'])
def test_child_rejects_invalid_readiness_and_closes(message):
    process=Mock();process.stdout=io.StringIO(message);process.stdin=io.StringIO();process.poll.return_value=0;process.returncode=0
    with pytest.raises(RuntimeError):b.Child('fixture','unused',factory=Mock(return_value=process))
    assert process.stdout.closed and process.stdin.closed


def test_child_graceful_and_forced_cleanup():
    def process():
        child=Mock();child.stdout=io.StringIO('{"schema_version":1,"port":4321}');child.stdin=io.StringIO()
        child.poll.return_value=None;child.returncode=0
        return child
    owned=process();child=b.Child('fixture','unused',factory=Mock(return_value=owned));assert child.url=='http://127.0.0.1:4321'
    child.close();assert child.graceful;owned.kill.assert_not_called()
    owned=process();owned.wait.side_effect=[subprocess.TimeoutExpired('test',1),0]
    child=b.Child('proxy','unused',factory=Mock(return_value=owned));child.close();owned.kill.assert_called_once();assert not child.graceful


def test_environment_rss_optional_and_permission_failure(monkeypatch):
    monkeypatch.setitem(sys.modules,'psutil',None)
    assert b.rss(1) is None and b.environment()['physical_cpus'] is None
    fake=SimpleNamespace(Error=RuntimeError,cpu_count=lambda logical:4,virtual_memory=lambda:SimpleNamespace(total=8*1024**3),
        Process=lambda pid:SimpleNamespace(memory_info=lambda:SimpleNamespace(rss=500)))
    monkeypatch.setitem(sys.modules,'psutil',fake)
    assert b.rss(1)==500 and b.environment()['physical_cpus']==4
    fake.Process=Mock(side_effect=RuntimeError('Access denied'))
    assert b.rss(1) is None


def test_core_statement_and_branch_gates_are_independent(tmp_path,capsys):
    data={'files':{'tuneio\\core\\cache.py':{'summary':dict(num_statements=100,covered_lines=100,num_branches=100,covered_branches=89)}}}
    assert not quality.check(data)['passed']
    data['files']['tuneio\\core\\cache.py']['summary'].update(covered_lines=89,covered_branches=100)
    assert not quality.check(data)['passed']
    data['files']['tuneio\\core\\cache.py']['summary']['covered_lines']=100
    assert quality.check(data)['passed']
    file=tmp_path/'coverage.json';file.write_text(json.dumps(data));assert quality.main([str(file)])==0
    data['files']['tuneio\\core\\cache.py']['summary']['covered_lines']=0
    file.write_text(json.dumps(data));assert quality.main([str(file)])==3
    file.write_text('{}');assert quality.main([str(file)])==2
    with pytest.raises(ValueError):quality.check(data,101)


def test_benchmark_cli_has_no_user_upstream_and_reserves_output_before_traffic(tmp_path,monkeypatch,capsys):
    report=dict(schema_version=1,options={'profile':'quick'},gates={'correctness':{'state':'passed'}},rounds=[],source_digest='test')
    runner=Mock(return_value=report);monkeypatch.setattr(b,'run',runner)
    file=tmp_path/'report.json'
    assert cli.main(['benchmark','--output',str(file)])==0
    assert json.loads(file.read_text())['schema_version']==1
    assert capsys.readouterr().out==''
    runner.reset_mock();assert cli.main(['benchmark','--output',str(file)])==2;runner.assert_not_called()
    assert cli.main(['benchmark','--output',str(file),'--force','--format','csv'])==0
    assert file.read_text().startswith('schema_version,')
    assert cli.main(['benchmark','--output',str(file),'--force','--requests','1'])==2
    runner.side_effect=RuntimeError('secret failure')
    assert cli.main(['benchmark','--output',str(file),'--force'])==1
    assert json.loads(file.read_text())['qualification_complete'] is False
    assert 'secret failure' not in capsys.readouterr().err
    runner.side_effect=KeyboardInterrupt()
    assert cli.main(['benchmark','--output',str(file),'--force'])==3
    assert json.loads(file.read_text())['state']=='interrupted'
    runner.side_effect=None
    assert cli.main(['benchmark','--output',str(file),'--force','--profile','qualification'])==0
    assert runner.call_args.args[0].orders==50000
    assert cli.main(['benchmark','--output',str(file),'--force','--profile','soak'])==0
    assert runner.call_args.args[0].duration_seconds==1800


@pytest.mark.parametrize('mode',('short','full','failure','growth','storage_fault','delayed_completion','unsettled'))
def test_soak_orchestration_checkpoints_limits_and_qualification_states(tmp_path,monkeypatch,mode):
    clock=[0.];FakeChild.all=[];pending=[False]
    async def load(*args,**kwargs):
        clock[0]+=60;pending[0]=mode=='delayed_completion'
        return dict(final_version=1,requests=20,errors=1,unexpected_failures=int(mode=='failure'))
    async def control(client,path):
        if path.endswith('report'):return {'sampling':{'state':'degraded' if mode=='storage_fault' else 'ready',
            'persistent':mode!='storage_fault','write_errors':int(mode=='storage_fault')}}
        busy=mode=='unsettled' or pending[0];pending[0]=False
        return dict(cache=dict(coordination_keys=0,accounted_bytes=100,peak_accounted_bytes=100,byte_budget=1000,hits=10,misses=10,invalidations=1),
            runtime=dict(in_flight=int(busy),active_tasks=2 if mode!='growth' else int(clock[0])),storage={},transport={'completed_requests':int(clock[0]/60)*20-int(busy)})
    monkeypatch.setattr(b,'load',load);monkeypatch.setattr(b,'control',control)
    monkeypatch.setattr(b,'perf_counter',lambda:clock[0]);monkeypatch.setattr(b,'rss',lambda pid:100)
    report=b.run(b.Options(profile='soak',orders=8,duration_seconds=1 if mode=='short' else 1800),child_factory=FakeChild)
    assert report['graceful_shutdown'] and all(p.closed for p in FakeChild.all)
    state=report['gates']['stability_30_minutes']['state']
    assert state==('not_qualified' if mode=='short' else 'passed' if mode in ('full','storage_fault','delayed_completion') else 'failed')
    assert report['gates']['telemetry_health']['state']==('failed' if mode=='storage_fault' else 'passed')
    if mode=='storage_fault':assert b.exit_code(report)==3
    if mode=='unsettled':assert b.exit_code(report)==3
    if mode=='delayed_completion':
        assert all(p['settlement']['polls']==2 and p['settlement']['state']=='settled' for p in report['soak']['checkpoints'])
    assert report['soak']['requests']>0 and 'soak,1,soak,adaptive' in b.report_csv(report)
    proxy=next(p for p in FakeChild.all if p.kind=='proxy')
    assert proxy.config['limits']['cache_bytes']==65536 and proxy.config['metrics']['raw_capacity']==128


def test_qualification_server_announces_bound_port_and_stops_on_stdin(tmp_path,monkeypatch,capsys):
    import uvicorn
    from tuneio import qualification_server as server
    path=tmp_path/'fixture.sqlite3';seed(path,8)
    async def startup(self,sockets=None):
        self.started=True;self.servers=[SimpleNamespace(sockets=[SimpleNamespace(getsockname=lambda:('127.0.0.1',4567))])]
    async def serve(self,sockets=None):await self.startup(sockets)
    monkeypatch.setattr(uvicorn.Server,'startup',startup);monkeypatch.setattr(uvicorn.Server,'serve',serve)
    monkeypatch.setattr(uvicorn.Server,'run',lambda self:asyncio.run(self.serve()))
    monkeypatch.setattr(sys,'stdin',io.StringIO('stop\n'))
    monkeypatch.setattr(sys,'argv',['qualification_server','--kind','fixture','--path',str(path)])
    server.main();assert json.loads(capsys.readouterr().out)['port']==4567
    config=tmp_path/'config.json';config.write_text(b.config_for('off','http://fixture',tmp_path/'m.sqlite3').model_dump_json())
    monkeypatch.setattr(sys,'stdin',io.StringIO('stop\n'))
    monkeypatch.setattr(sys,'argv',['qualification_server','--kind','proxy','--path',str(config)])
    server.main();assert json.loads(capsys.readouterr().out)['schema_version']==1


def test_doctor_session_and_uptime(tmp_path,monkeypatch,capsys):
    config=b.config_for('observe','http://fixture',tmp_path/'m.sqlite3')
    session=ControlSession(config);assert session.status()['uptime_seconds']>=0
    assert session.status()['runtime']['active_tasks'] is None
    monkeypatch.setattr(cli,'resolved_config',lambda _:config)
    monkeypatch.setattr(cli,'local_json',lambda *a:dict(uptime_seconds=30,running=True,limits={'upstream_concurrency':8}))
    assert cli.main(['doctor','--session','--json'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['session_uptime_seconds']==30 and result['session_limits']['upstream_concurrency']==8
    asyncio.run(session.proxy.close())
