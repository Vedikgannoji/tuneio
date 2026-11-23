import asyncio
from contextlib import closing
import csv
import io
import json
from pathlib import Path
import sqlite3
from threading import Event
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from tuneio import cli
from tuneio.product_config import ProductConfig
from tuneio.reporting import serialize_report
from tuneio.session import ControlSession
from tuneio.storage import TelemetryStorage, StorageUnavailable, config_identity, connect_readonly, history, report


class Bytes(httpx.AsyncByteStream):
    def __init__(self, content): self.content=content
    async def __aiter__(self): yield self.content


@pytest.fixture
def config(tmp_path):
    return ProductConfig(upstream='https://private.example', operations=[{'method':'GET','path':'/items/*'}],
                         metrics={'storage_path':str(tmp_path/'telemetry.sqlite3'), 'raw_capacity':4})


def observation(timestamp=1_800_000_000., duration=.125, status=200, route='/items/*', cache='BYPASS'):
    return dict(timestamp=timestamp, method='GET', route=route, status=status, duration_ms=duration, cache=cache)


def test_exact_aggregates_restart_and_offline_exports(config):
    async def scenario():
        storage = TelemetryStorage(config, 'first', clock=lambda:1_800_000_000.)
        await storage.start()
        values = [.125,.25,.25,1.5,10.25]
        for i, value in enumerate(values):
            storage.submit('proxy', observation(duration=value, status=500 if i==4 else 200, cache='HIT' if i==0 else 'BYPASS'))
        data = await storage.query('report', 'first')
        row = data['routes'][0]
        assert row['count']==5 and row['mean_ms']==sum(values)/5
        assert (row['p50_ms'],row['p95_ms'],row['p99_ms'])==(.25,10.25,10.25)
        assert row['errors']==row['server_errors']==row['cache_hits']==1
        assert row['error_rate']==.2 and row['cache_bypasses']==4 and row['cache_misses']==0
        assert data['effective_config']['upstream']=='https://{upstream}'
        assert data['effective_config']['metrics']['storage_path']=='{local_storage}'
        assert len(data['source_sha256'])==64 and len(data['config_digest'])==64
        assert 'private.example' not in json.dumps(data)
        assert not data['sampling']['sampling_incomplete']
        await storage.close()
        assert storage.health()['state']=='closed'
        second = TelemetryStorage(config,'second', clock=lambda:1_800_000_060.)
        await second.start()
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            assert len(history(db, storage.digest))==2
            recovered = report(db,storage.digest,'first')
            assert recovered['routes']==data['routes'] and recovered['shutdown_recorded']
            assert report(db,storage.digest)['session_id']=='second'
            assert not report(db,storage.digest)['routes']
        await second.close()
        csv_rows = list(csv.DictReader(io.StringIO(serialize_report(recovered,'csv'))))
        assert csv_rows[0]['count']=='5' and csv_rows[0]['p50_ms']=='0.25'
        assert csv_rows[0]['shutdown_recorded']=='True' and csv_rows[0]['sampling_may_be_incomplete']=='False'
        assert json.loads(serialize_report(recovered,'json'))['session_id']=='first'
        # Five observations collapse to four duration frequencies; no payload table.
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]==4
            with pytest.raises(sqlite3.OperationalError):
                db.execute('DELETE FROM samples')
    asyncio.run(scenario())


def test_queue_overflow_and_cancelled_export_are_visible(config, monkeypatch):
    config.metrics.writer_queue_capacity=1
    storage = TelemetryStorage(config,'pressure',clock=lambda:1_800_000_000.)
    entered, release = Event(), Event()
    original = storage._write
    def blocked(db,batch):
        entered.set()
        assert release.wait(5)
        return original(db,batch)
    monkeypatch.setattr(storage,'_write',blocked)
    async def scenario():
        await storage.start()
        storage.submit('proxy',observation())
        assert await asyncio.to_thread(entered.wait,5)
        try:
            storage.submit('proxy',observation())
            storage.submit('proxy',observation())
            assert storage.health()['queue_depth']==1 and storage.stats['dropped_queue']==1
            with pytest.raises(StorageUnavailable):
                await storage.query('report','pressure')
        finally:
            release.set()
        await storage.close()
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            data=report(db,storage.digest,'pressure')
        assert data['routes'][0]['count']==2
        assert data['sampling']['dropped_queue']==1 and data['sampling']['sampling_incomplete']
    asyncio.run(scenario())


def test_retention_and_session_cap(config, monkeypatch):
    import tuneio.storage as module
    monkeypatch.setattr(module,'MAX_SESSIONS',3)
    clock=[1_800_000_000.]
    async def scenario():
        for i in range(5):
            store=TelemetryStorage(config,str(i),clock=lambda:clock[0])
            await store.start()
            store.submit('proxy',observation(clock[0]))
            await store.close()
            clock[0]+=60
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            assert len(history(db,store.digest))==3
        clock[0]+=8*86400
        latest=TelemetryStorage(config,'expired',clock=lambda:clock[0])
        await latest.start()
        assert latest.stats['retention_truncated']==3
        assert latest.stats['sessions_truncated']==3
        assert (await latest.query('sessions'))[0]['session_id']=='expired'
        with pytest.raises(StorageUnavailable):
            await latest.query('report','missing')
        assert latest.state=='ready'  # bad report selection is not a storage fault
        await latest.close()
    asyncio.run(scenario())


def test_disk_budget_and_truncation(config):
    config.metrics.storage_max_bytes=131072
    config.metrics.writer_queue_capacity=10000
    async def scenario():
        store=TelemetryStorage(config,'budget',clock=lambda:1_800_000_000.)
        await store.start()
        assert store.state=='ready'
        for i in range(2500):
            store.submit('proxy',observation(duration=i+.0125))
        data=await store.query('report','budget')
        await store.close()
        assert store.stats['budget_truncated']>0
        assert data['sampling']['sampling_incomplete']
        assert store.stats['dropped_queue']==0
        assert Path(config.metrics.storage_path).stat().st_size<=config.metrics.storage_max_bytes
        assert sum(p.stat().st_size for p in Path(config.metrics.storage_path).parent.glob('telemetry.sqlite3*'))<=config.metrics.storage_max_bytes
        assert sum(r['count'] for r in data['routes'])<2500
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',['corrupt','foreign','version','directory','oversized'])
def test_unavailable_database_does_not_overwrite_unowned_data(config, failure):
    path=Path(config.metrics.storage_path)
    if failure=='corrupt':
        path.write_bytes(b'not a sqlite database')
    elif failure=='directory':
        path.mkdir()
    else:
        with sqlite3.connect(path) as db:
            if failure=='foreign': db.execute('CREATE TABLE business_data(value)')
            elif failure=='version': db.execute('PRAGMA user_version=99')
            else:
                db.execute('CREATE TABLE filler(value)')
                db.execute('INSERT INTO filler VALUES(?)',(b'x'*200000,))
    before=path.read_bytes() if path.is_file() else None
    async def scenario():
        store=TelemetryStorage(config,'bad')
        await store.start()
        assert store.state=='degraded' and store.health()['last_error']=='storage_open_failed'
        store.submit('proxy',observation())
        assert store.stats['dropped_storage']==1
        with pytest.raises(StorageUnavailable): await store.query('report')
        await store.close()
    asyncio.run(scenario())
    if before is not None:
        assert path.read_bytes()==before


def test_storage_write_failure_then_recovery(config, monkeypatch):
    async def scenario():
        store=TelemetryStorage(config,'recover',clock=lambda:1_800_000_000.)
        original=store._write
        await store.start()
        monkeypatch.setattr(store,'_write',Mock(side_effect=sqlite3.OperationalError('secret-token')))
        store.submit('proxy',observation())
        await store.query('flush')
        assert store.health()['dropped_storage']==1 and store.state=='degraded'
        assert 'secret-token' not in json.dumps(store.health())
        monkeypatch.setattr(store,'_write',original)
        store.submit('proxy',observation())
        data=await store.query('report','recover')
        assert store.state=='ready' and data['routes'][0]['count']==1
        assert data['sampling']['sampling_incomplete']
        await store.close()
    asyncio.run(scenario())


def test_proxy_storage_integration_redaction_bounds_and_controls(config):
    seen=[]
    def upstream(req):
        seen.append(req)
        return httpx.Response(200,stream=Bytes(b'private response body'))
    app=ControlSession(config,transport=httpx.MockTransport(upstream))
    async def scenario():
        incoming=asyncio.Queue()
        started,stopped=asyncio.Event(),asyncio.Event()
        async def receive():return await incoming.get()
        async def send(message):
            if message['type']=='lifespan.startup.complete':started.set()
            else:stopped.set()
        life=asyncio.create_task(app({'type':'lifespan'},receive,send))
        await incoming.put({'type':'lifespan.startup'})
        await started.wait()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as client:
                for i in range(8):
                    response=await client.get('/items/'+str(i)+'?secret=query-secret',headers={'authorization':'Bearer header-secret','cookie':'cookie-secret'})
                    assert response.content==b'private response body'
                assert len(seen)==8
                assert sum(len(m.records) for m in (app.proxy.metrics,app.proxy.upstream_metrics))==4
                assert app.proxy.metrics.snapshot()['window_may_be_incomplete']
                result=(await client.get('/_autotuner/report')).json()
                assert {r['route'] for r in result['routes']}=={'/items/*'}
                assert all(r['count']==8 for r in result['routes'])
                assert result['sampling']['transport']['completed_requests']==8
                status=(await client.get('/_autotuner/status')).json()
                assert 'reports' in status['capabilities'] and status['storage']['persistent']
                assert not status['cache_serving_enabled']
                assert (await client.get('/_autotuner/sessions')).json()['sessions'][0]['session_id']==app.session_id
                assert (await client.get('/_autotuner/report?session=unknown')).status_code==503
                assert (await client.get('/_autotuner/report?x=%FF')).status_code==200
        finally:
            await incoming.put({'type':'lifespan.shutdown'})
            await stopped.wait()
            await life
        encoded=Path(config.metrics.storage_path).read_bytes()
        for secret in (b'query-secret',b'header-secret',b'cookie-secret',b'private response body'):
            assert secret not in encoded and secret.decode() not in json.dumps(result)
    asyncio.run(scenario())


def test_storage_failure_keeps_proxy_forwarding(config):
    Path(config.metrics.storage_path).mkdir()
    app=ControlSession(config,transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Bytes(b'ok'))))
    async def scenario():
        await app.storage.start()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as client:
            assert (await client.get('/items/1')).status_code==200
            assert (await client.get('/_autotuner/report')).status_code==503
            health=(await client.get('/_autotuner/status')).json()['storage']
            assert health['state']=='degraded' and health['dropped_storage']==2
        await app.proxy.close()
        await app.storage.close()
    asyncio.run(scenario())


def test_report_cli_offline_output_and_config_relative_paths(config,tmp_path,monkeypatch,capsys):
    for key in ('CONFIG','UPSTREAM','LISTEN','MODE'): monkeypatch.delenv('AUTOTUNER_'+key,raising=False)
    async def setup():
        store=TelemetryStorage(config,'cli',clock=lambda:1_800_000_000.)
        await store.start()
        store.submit('proxy',observation())
        await store.close()
    asyncio.run(setup())
    file=tmp_path/'config.json'
    payload=config.model_dump()
    payload['metrics']['storage_path']='telemetry.sqlite3'
    file.write_text(json.dumps(payload))
    monkeypatch.chdir(tmp_path.parent)
    base=['--config',str(file)]
    assert cli.main(['sessions',*base,'--json'])==0
    assert json.loads(capsys.readouterr().out)['sessions'][0]['session_id']=='cli'
    assert cli.main(['report',*base])==0
    assert json.loads(capsys.readouterr().out)['routes'][0]['count']==1
    output=tmp_path/'report.csv'
    assert cli.main(['report',*base,'--format','csv','--output',str(output)])==0
    assert not capsys.readouterr().out
    assert list(csv.DictReader(io.StringIO(output.read_text())))[0]['session_id']=='cli'
    assert cli.main(['report',*base,'--output',str(output)])==2
    assert cli.main(['report',*base,'--output',str(output),'--force'])==0
    assert cli.main(['report',*base,'--session','absent'])==1
    missing=tmp_path/'missing.json'
    payload['metrics']['storage_path']='absent.sqlite3'
    missing.write_text(json.dumps(payload))
    assert cli.main(['report','--config',str(missing)])==1
    assert not (tmp_path/'absent.sqlite3').exists()


def test_live_report_cli_validation(config,tmp_path,monkeypatch,capsys):
    from contextlib import nullcontext
    file=tmp_path/'config.json'
    file.write_text(config.model_dump_json())
    digest,_=config_identity(config)
    payload=dict(schema_version=1,tool_version='2.0.0',source_sha256='hash',session_id='id',config_digest=digest,sampling={},routes=[])
    opener=Mock()
    monkeypatch.setattr(cli.urllib.request,'build_opener',Mock(return_value=opener))
    def answer(value):opener.open.return_value=nullcontext(io.StringIO(json.dumps(value)))
    answer(payload)
    assert cli.main(['report','--config',str(file),'--live','--session','id & /'])==0
    assert 'session=id+%26+%2F' in opener.open.call_args.args[0]
    assert json.loads(capsys.readouterr().out)['session_id']=='id'
    answer(dict(schema_version=1,sessions=[]))
    assert cli.main(['sessions','--config',str(file),'--live','--json'])==0
    for value in ({},dict(payload,config_digest='wrong'),dict(payload,routes={})):
        answer(value)
        assert cli.main(['report','--config',str(file),'--live'])==1
    answer(dict(schema_version=1,sessions={}))
    assert cli.main(['sessions','--config',str(file),'--live'])==1
    opener.open.return_value=nullcontext(io.StringIO('not json'))
    assert cli.main(['report','--config',str(file),'--live'])==1


@pytest.mark.parametrize('fields',[{'storage_max_bytes':1},{'storage_path':':memory:'},{'storage_path':'file:abc'},
                                  {'storage_path':'bad\x00path'},{'writer_queue_capacity':0}, {'writer_queue_capacity':10001}])
def test_invalid_storage_config(fields):
    with pytest.raises(ValidationError):ProductConfig(upstream='http://localhost',metrics=fields)


def test_single_writer_lease_and_readonly_reports(config):
    async def scenario():
        first=TelemetryStorage(config,'one')
        await first.start()
        second=TelemetryStorage(config,'two')
        await second.start()
        assert first.state=='ready' and second.state=='degraded'
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            assert report(db,first.digest)['session_id']=='one'
        await second.close()
        await first.close()
        third=TelemetryStorage(config,'three')
        await third.start()
        assert third.state=='ready'
        await third.close()
    asyncio.run(scenario())


def test_retention_marks_previous_session_and_cancelled_flush(config, monkeypatch):
    clock=[1_800_000_000.]
    entered,release=Event(),Event()
    async def scenario():
        first=TelemetryStorage(config,'first',clock=lambda:clock[0])
        await first.start()
        # Last session start stays recent, but its earlier bucket has just expired.
        first.submit('proxy',observation(timestamp=clock[0]-7*86400+60))
        await first.close()
        clock[0]+=180
        second=TelemetryStorage(config,'second',clock=lambda:clock[0])
        await second.start()
        data=await second.query('report','first')
        assert not data['routes'] and data['sampling']['retention_truncated']==1
        original=second._write
        def blocked(db,batch):
            entered.set()
            assert release.wait(5)
            return original(db,batch)
        monkeypatch.setattr(second,'_write',blocked)
        second.submit('proxy',observation(timestamp=clock[0]))
        assert await asyncio.to_thread(entered.wait,5)
        task=asyncio.create_task(second.query('report','second'))
        # Synchronize scheduling without a timing-dependent sleep.
        await asyncio.get_running_loop().run_in_executor(None,lambda:None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        release.set()
        await second.close()
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            assert report(db,second.digest,'second')['routes'][0]['count']==1
            assert report(db,second.digest,'second')['shutdown_recorded']
            assert report(db,second.digest,'second')['sampling']['sampling_may_be_incomplete']  # retention loss is disclosed
        await second.close()  # close is idempotent
    asyncio.run(scenario())


def test_real_database_lock_drops_telemetry_and_recovers(config):
    async def scenario():
        store=TelemetryStorage(config,'lock',clock=lambda:1_800_000_000.)
        await store.start()
        with sqlite3.connect(config.metrics.storage_path) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            store.submit('proxy',observation())
            with pytest.raises(StorageUnavailable):await store.query('report','lock')
            assert store.stats['dropped_storage']==1
            blocker.rollback()
        store.submit('proxy',observation())
        data=await store.query('report','lock')
        assert data['routes'][0]['count']==1 and data['sampling']['sampling_incomplete']
        await store.close()
    asyncio.run(scenario())


def test_start_permission_and_shutdown_flush_failures(config, monkeypatch):
    async def scenario():
        unopened=TelemetryStorage(config,'permission')
        monkeypatch.setattr(unopened,'_open',Mock(side_effect=PermissionError('private/path')))
        await unopened.start()
        assert unopened.state=='degraded'
        await unopened.close()
        store=TelemetryStorage(config,'shutdown')
        await store.start()
        monkeypatch.setattr(store,'_checkpoint',Mock(side_effect=sqlite3.OperationalError('private/path')))
        await store.close()
        assert store.state=='degraded' and store.last_error=='storage_write_or_read_failed'
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            assert not report(db,store.digest,'shutdown')['shutdown_recorded']
    asyncio.run(scenario())


def test_readonly_validation_and_config_isolation(config):
    async def setup():
        store=TelemetryStorage(config,'source')
        await store.start()
        await store.close()
    asyncio.run(setup())
    digest,_=config_identity(config)
    with closing(connect_readonly(config.metrics.storage_path)) as db:
        with pytest.raises(StorageUnavailable):report(db,'different')
        with pytest.raises(StorageUnavailable):report(db,'different','source')
    with sqlite3.connect(config.metrics.storage_path) as db:db.execute('PRAGMA user_version=99')
    with pytest.raises(StorageUnavailable):connect_readonly(config.metrics.storage_path)


def test_lowered_budget_refuses_existing_store(config):
    async def scenario():
        store=TelemetryStorage(config,'large',clock=lambda:1_800_000_000.)
        await store.start()
        # Keep a single transaction's accepted events below the default writer queue.
        for i in range(800):store.submit('proxy',observation(duration=i+.0001))
        await store.close()
        assert Path(config.metrics.storage_path).stat().st_size>65536
        before=Path(config.metrics.storage_path).read_bytes()
        config.metrics.storage_max_bytes=131072
        smaller=TelemetryStorage(config,'small')
        await smaller.start()
        assert smaller.state=='degraded'
        await smaller.close()
        assert Path(config.metrics.storage_path).read_bytes()==before
    asyncio.run(scenario())


def test_budget_prunes_session_metadata(config):
    config.metrics.storage_max_bytes=131072
    # Large but valid config makes metadata pressure reproducible with only a few sessions.
    config.operations=[type(config.operations[0])(method='GET',path='/items/'+('x'*600)+str(i)) for i in range(15)]
    async def scenario():
        last=None
        for i in range(6):
            last=TelemetryStorage(config,str(i),clock=lambda:1_800_000_000.+i)
            await last.start()
            assert last.state=='ready'
            await last.close()
        assert last.stats['sessions_truncated']>0
        assert Path(config.metrics.storage_path).stat().st_size<=config.metrics.storage_max_bytes
    asyncio.run(scenario())


def test_unknown_methods_collapse_to_bounded_label(config):
    app=ControlSession(config,transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Bytes(b'ok'))))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as client:
            for method in ('RANDOMONE','RANDOMTWO','RANDOMTHREE'):
                assert (await client.request(method,'/random-id')).status_code==501
        assert set(app.proxy.metrics.snapshot()['routes'])=={'OTHER /{unmatched}'}
        await app.proxy.close()
    asyncio.run(scenario())


def test_start_cancellation_closes_writer(config, monkeypatch):
    store=TelemetryStorage(config,'cancel-start')
    original=store._open
    entered,release=Event(),Event()
    def delayed_open():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(store,'_open',delayed_open)
    async def scenario():
        task=asyncio.create_task(store.start())
        assert await asyncio.to_thread(entered.wait,5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert store.thread is None and not store.accepting
        assert store.state=='closed'
    asyncio.run(scenario())
