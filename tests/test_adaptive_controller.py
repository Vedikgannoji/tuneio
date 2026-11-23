import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
from threading import Event

import httpx
import pytest
from pydantic import ValidationError

from tuneio.core.controller import AdaptiveController
from tuneio.product_config import ProductConfig
from tuneio.response_cache import ResponseCache
from tuneio.session import ControlSession
from tuneio.storage import TelemetryStorage,connect_readonly,report,config_identity


@pytest.fixture
def config(tmp_path):
    return ProductConfig(upstream='http://origin',mode='adaptive',cache={'routes':[dict(id='items',match='/items/*',policy='adaptive',
        ttl_min_seconds=2,ttl_max_seconds=30,invalidate_on=[dict(method='PATCH',path='/items/*')])]},
        metrics={'storage_path':str(tmp_path/'metrics.sqlite3')})


def controller(config):
    clock=[0.]
    c=AdaptiveController(config,clock=lambda:clock[0],wall_clock=lambda:1_800_000_000+clock[0],session_id='test-session')
    return c,config.cache.routes[0],clock


def feed(c,rule,values,key='same'):
    for value in values:result=c.observe(rule,key,value)
    return result


def test_minimum_samples_and_same_key_reuse(config):
    c,rule,clock=controller(config)
    assert not feed(c,rule,[10]*3)['enabled']
    assert feed(c,rule,[10])['enabled']
    assert c.evaluate(rule)['ttl_seconds']==10
    other,rule,_=controller(config)
    for i in range(4):result=other.observe(rule,str(i),10)
    assert not result['enabled'] and result['reason']=='insufficient_recent_reuse'
    assert other.observe(rule,'3',10)['enabled']


def test_hysteresis_cooldown_and_ttl_changes(config):
    config.tuning.sample_capacity=4
    c,rule,clock=controller(config)
    assert feed(c,rule,[10]*4)['enabled']
    result=feed(c,rule,[20]*4)
    assert result['ttl_seconds']==10 and result['candidate_ttl_seconds']==20 and result['cooldown_blocking']
    clock[0]=5
    assert c.evaluate(rule)['ttl_seconds']==20
    result=feed(c,rule,[1.75]*4)
    assert result['enabled'] and result['candidate_enabled']
    clock[0]=10
    assert c.evaluate(rule)['ttl_seconds']==2
    feed(c,rule,[1]*4)
    clock[0]=15
    assert not c.evaluate(rule)['enabled']
    fresh,rule,_=controller(config)
    assert not feed(fresh,rule,[1.75]*4)['enabled']


def test_failure_bypasses_cooldown_and_recovers_after_clean_cohort(config):
    c,rule,clock=controller(config)
    feed(c,rule,[10]*4)
    result=c.observe(rule,'same',0,failed=True,reason='upstream_failure')
    assert not result['enabled'] and result['reason']=='upstream_failure'
    assert not feed(c,rule,[10]*19)['enabled']
    assert not feed(c,rule,[10])['enabled']  # clean cohort, still inside cooldown
    clock[0]=5
    assert c.evaluate(rule)['enabled']
    assert any(d['action']=='disabled' and d['applied'] for d in c.history)


def test_reuse_and_learning_windows_expire(config):
    c,rule,clock=controller(config)
    feed(c,rule,[10]*4)
    clock[0]=61
    assert not c.evaluate(rule)['enabled'] and c.evaluate(rule)['reason']=='insufficient_recent_reuse'
    clock[0]=301
    result=c.evaluate(rule)
    assert result['inputs']['samples']==0 and result['reason']=='collecting_samples'


@pytest.mark.parametrize('mode',['recommend','observe','off','cache'])
def test_non_adaptive_modes_never_apply_controller_decisions(config,mode):
    config.mode=mode
    c,rule,_=controller(config)
    result=feed(c,rule,[10]*4)
    assert result['candidate_enabled'] and not result['enabled']
    assert all(not d['applied'] for d in c.history)


def test_hit_cost_is_excluded_and_hot_polling_does_not_flood_decisions(config):
    c,rule,_=controller(config)
    feed(c,rule,[10]*4)
    before=len(c.history)
    for _ in range(50):c.hit(rule,'same');c.evaluate(rule)
    result=c.evaluate(rule)
    assert result['inputs']['origin_mean_ms']==10 and result['inputs']['origin_observations']==4
    assert result['inputs']['cache_hits']==50
    assert len(c.history)==before


def test_decision_history_is_bounded_redacted_and_has_identity(config):
    config.tuning.history_capacity=3
    c,rule,clock=controller(config)
    for i in range(8):
        clock[0]+=6;c.observe(rule,'never-export',10+i)
    assert len(c.history)==3
    assert 'never-export' not in json.dumps(list(c.history))
    decision=c.history[-1]
    assert decision['session_id']=='test-session' and decision['rule_id']=='items'
    assert decision['timestamp'].endswith('+00:00')
    assert {'previous_ttl_seconds','new_ttl_seconds','thresholds','inputs','reason','action'}<=decision.keys()
    assert len(c.states['items']['samples'])<=config.tuning.sample_capacity


@pytest.mark.parametrize('duration',[-1,float('nan'),float('inf')])
def test_invalid_origin_duration_is_rejected(config,duration):
    c,rule,_=controller(config)
    with pytest.raises(ValueError):c.observe(rule,'same',duration)


@pytest.mark.parametrize('fields',[{'sample_capacity':1},{'minimum_samples':21},{'retain_ms':3.0},
    {'enable_ms':True},{'enable_ms':float('inf')},{'cooldown_seconds':-1.0},{'history_capacity':0},
    {'reuse_window_seconds':0.0},{'learning_window_seconds':0.0},{'sample_capacity':101}])
def test_invalid_tuning_configuration(fields):
    with pytest.raises(ValidationError):ProductConfig(upstream='http://origin',tuning=fields)


def test_policy_ttl_is_capped_by_origin_and_decreases_shorten_existing_entries(config):
    config.tuning.sample_capacity=4;config.tuning.cooldown_seconds=0.
    clock=[0.]
    cache=ResponseCache(config,clock=lambda:clock[0],wall_clock=lambda:1_800_000_000)
    rule=config.cache.routes[0]
    feed(cache.controller,rule,[20]*4)
    fill,_=cache.begin_fill('key',rule,0,[(b'cache-control',b'max-age=60')],200,received=0,sent=0)
    cache.append(fill,b'body');assert cache.publish(fill)
    assert cache.entries['key'].expires==20
    clock[0]=1
    feed(cache.controller,rule,[3]*4)
    assert cache.entries['key'].expires==3
    feed(cache.controller,rule,[30]*4)
    assert cache.entries['key'].expires==3  # increases never extend an old response
    fresh,_=cache.begin_fill('next',rule,0,[(b'cache-control',b'max-age=5')],200,received=1,sent=1)
    assert fresh.expires==6
    cache.discard(fresh)
    cache.invalidate()
    assert cache.controller.evaluate(rule)['enabled'] and cache.used_bytes==0


class Bytes(httpx.AsyncByteStream):
    def __init__(self,data=b'body',error=None):self.data,self.error=data,error
    async def __aiter__(self):
        yield self.data
        if self.error:raise self.error


def integration(config,monkeypatch,*,duration=10):
    import tuneio.proxy as module
    clock=[0.];state=dict(duration=duration,status=200,headers={'cache-control':'public, max-age=60'},fail=False,calls=0)
    monkeypatch.setattr(module,'perf_counter',lambda:clock[0])
    def origin(req):
        state['calls']+=1
        clock[0]+=state['duration']/1000
        if state['fail']:raise httpx.ReadError('private transport details')
        return httpx.Response(state['status'],headers=state['headers'],stream=Bytes())
    app=ControlSession(config,transport=httpx.MockTransport(origin))
    app.proxy.cache.clock=lambda:clock[0]
    app.proxy.cache.wall_clock=lambda:1_800_000_000+clock[0]
    return app,clock,state


async def request(app,path='/items/1',headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as c:
        return await c.get(path,headers=headers)


def test_learning_activates_without_hit_or_queue_dilution(config,monkeypatch):
    app,clock,state=integration(config,monkeypatch)
    acquire=app.proxy.slots.acquire
    async def queued():clock[0]+=1;return await acquire()
    app.proxy.slots.acquire=queued
    async def scenario():
        for _ in range(4):assert (await request(app)).headers['x-autotuner-cache']=='BYPASS'
        tuning=app.proxy.cache.explain('/items/1')['tuning']
        assert tuning['enabled'] and tuning['inputs']['origin_mean_ms']==pytest.approx(10.)
        assert tuning['inputs']['origin_mean_ms']<1000
        assert (await request(app)).headers['x-autotuner-cache']=='MISS'
        for _ in range(8):assert (await request(app)).headers['x-autotuner-cache']=='HIT'
        assert state['calls']==5
        assert app.proxy.cache.controller.evaluate(config.cache.routes[0])['inputs']['origin_observations']==5
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('headers',[{'Authorization':'Bearer private'},{'Cookie':'private=value'},{'Range':'bytes=0-1'}])
def test_credential_and_conditional_bypasses_never_train(config,monkeypatch,headers):
    app,clock,state=integration(config,monkeypatch)
    async def scenario():
        for _ in range(6):await request(app,headers=headers)
        result=app.proxy.cache.controller.evaluate(config.cache.routes[0])
        assert result['inputs']['samples']==0 and not app.proxy.cache.entries
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',['transport','status','privacy','no_freshness','oversize'])
def test_failure_guardrails_disable_and_evict_immediately(config,monkeypatch,failure):
    app,clock,state=integration(config,monkeypatch)
    async def scenario():
        for _ in range(5):await request(app)
        assert app.proxy.cache.entries
        if failure=='transport':state['fail']=True
        elif failure=='status':state['status']=500
        elif failure=='privacy':state['headers']={'cache-control':'private'}
        elif failure=='no_freshness':state['headers']={}
        else:config.limits.cache_entry_bytes=1
        await request(app,'/items/2')
        result=app.proxy.cache.controller.evaluate(config.cache.routes[0])
        assert not result['enabled'] and not app.proxy.cache.entries and app.proxy.cache.used_bytes==0
        assert result['inputs']['failed_samples']==1
        assert not result['cooldown_blocking']
        await app.proxy.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['recommend','observe','off'])
def test_manual_modes_never_cache(config,monkeypatch,mode):
    config.mode=mode
    app,clock,state=integration(config,monkeypatch)
    async def scenario():
        for _ in range(8):assert (await request(app)).headers['x-autotuner-cache']=='BYPASS'
        assert not app.proxy.cache.entries and state['calls']==8
        result=app.proxy.cache.explain('/items/1')['tuning']
        assert result['candidate_enabled']==(mode=='recommend') and not result['enabled']
        assert result['inputs']['origin_observations']==(8 if mode=='recommend' else 0)
        await app.proxy.close()
    asyncio.run(scenario())


def test_unique_request_keys_and_fast_origins_remain_observing(config,monkeypatch):
    app,clock,state=integration(config,monkeypatch)
    async def scenario():
        for i in range(8):await request(app,'/items/'+str(i))
        assert not app.proxy.cache.controller.evaluate(config.cache.routes[0])['enabled']
        state['duration']=1
        for _ in range(25):
            clock[0]+=31
            await request(app)
        result=app.proxy.cache.controller.evaluate(config.cache.routes[0])
        assert not result['enabled'] and result['reason']=='fast_origin'
        await app.proxy.close()
    asyncio.run(scenario())


def test_decisions_persist_bounded_without_request_keys_and_survive_restart(config,monkeypatch):
    config.tuning.history_capacity=3
    app,clock,state=integration(config,monkeypatch)
    async def scenario():
        await app.storage.start()
        for _ in range(6):await request(app)
        saved=await app.storage.query('report',app.session_id)
        assert saved['effective_mode']=='adaptive' and 1<=len(saved['tuning_decisions'])<=3
        assert all(d['session_id']==app.session_id for d in saved['tuning_decisions'])
        assert saved['sampling']['tuning_decisions_truncated']>0
        await app.storage.close();await app.proxy.close()
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            assert report(db,app.storage.digest,app.session_id)['tuning_decisions']==saved['tuning_decisions']
        new,_,_=integration(config,monkeypatch)
        assert not new.proxy.cache.entries and new.proxy.cache.controller.evaluate(config.cache.routes[0])['inputs']['samples']==0
        await new.proxy.close()
    asyncio.run(scenario())


def test_default_tuning_preserves_phase_seven_config_identity(config):
    data=config.model_dump();data.pop('tuning');data['metrics']['storage_path']=str(Path(config.metrics.storage_path).resolve())
    previous=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert config_identity(config)[0]==previous
    config.tuning.enable_ms=3.
    assert config_identity(config)[0]!=previous


def test_decision_queue_pressure_and_legacy_readonly_report(config,monkeypatch):
    config.metrics.writer_queue_capacity=1
    storage=TelemetryStorage(config,'pressure')
    entered,release=Event(),Event()
    original=storage._write
    def blocked(db,batch):entered.set();assert release.wait(5);return original(db,batch)
    monkeypatch.setattr(storage,'_write',blocked)
    async def scenario():
        await storage.start()
        storage.submit('proxy',dict(timestamp=storage.clock(),method='GET',route='/items/*',status=200,duration_ms=10,cache='BYPASS'))
        assert await asyncio.to_thread(entered.wait,5)
        try:
            storage.record_decision({'action':'observing'});storage.record_decision({'action':'observing'})
            assert storage.stats['dropped_tuning_decisions']==1
        finally:release.set()
        await storage.close()
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            saved=report(db,storage.digest,'pressure')
            assert len(saved['tuning_decisions'])==1 and saved['sampling']['tuning_history_may_be_incomplete']
        import sqlite3
        with sqlite3.connect(config.metrics.storage_path) as db:
            db.execute('DROP TABLE decisions')
            old_config=json.loads(db.execute('SELECT config FROM sessions').fetchone()[0]);old_config.pop('tuning')
            db.execute('UPDATE sessions SET config=?',(json.dumps(old_config),))
        with closing(connect_readonly(config.metrics.storage_path)) as db:
            historical=report(db,storage.digest,'pressure')
            assert historical['tuning_decisions']==[] and historical['effective_mode']=='observe'
    asyncio.run(scenario())


def test_cross_generation_read_does_not_train_or_refill(config,monkeypatch):
    import tuneio.proxy as module
    clock=[0.];entered,gate=asyncio.Event(),asyncio.Event();hold={'on':False}
    monkeypatch.setattr(module,'perf_counter',lambda:clock[0])
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if hold['on']:entered.set();await gate.wait()
            yield b'body'
    def origin(req):
        clock[0]+=.01
        return httpx.Response(200,headers={'cache-control':'max-age=60'},stream=Stream())
    app=ControlSession(config,transport=httpx.MockTransport(origin));app.proxy.cache.clock=lambda:clock[0]
    app.proxy.cache.wall_clock=lambda:1_800_000_000+clock[0]
    async def scenario():
        for _ in range(4):await request(app)
        assert app.proxy.cache.controller.evaluate(config.cache.routes[0])['enabled']
        hold['on']=True
        pending=asyncio.create_task(request(app));await entered.wait()
        ids=app.proxy.cache.begin_write('PATCH','/items/1');app.proxy.cache.end_write(ids)
        hold['on']=False;gate.set();await pending
        assert not app.proxy.cache.entries
        assert app.proxy.cache.controller.evaluate(config.cache.routes[0])['inputs']['origin_observations']==4
        assert app.proxy.cache.used_bytes==0
        await app.proxy.close()
    asyncio.run(scenario())


def test_incomplete_stream_rolls_back_adaptive_state_and_reservation(config,monkeypatch):
    import tuneio.proxy as module
    from tuneio.proxy import IncompleteResponse
    clock=[0.];bad={'on':False}
    monkeypatch.setattr(module,'perf_counter',lambda:clock[0])
    def origin(req):
        clock[0]+=.01
        return httpx.Response(200,headers={'cache-control':'max-age=60'},stream=Bytes(error=httpx.ReadError('sensitive') if bad['on'] else None))
    app=ControlSession(config,transport=httpx.MockTransport(origin));app.proxy.cache.clock=lambda:clock[0]
    app.proxy.cache.wall_clock=lambda:1_800_000_000+clock[0]
    async def scenario():
        for _ in range(5):await request(app)
        assert app.proxy.cache.entries
        bad['on']=True
        with pytest.raises(IncompleteResponse):await request(app,'/items/2')
        result=app.proxy.cache.controller.evaluate(config.cache.routes[0])
        assert not result['enabled'] and result['reason']=='upstream_failure'
        assert not app.proxy.cache.entries and app.proxy.cache.used_bytes==0 and not app.proxy.cache.locks
        assert 'sensitive' not in json.dumps(list(app.proxy.cache.controller.history))
        await app.proxy.close()
    asyncio.run(scenario())


def test_tuning_decision_storage_fault_does_not_break_cache(config,monkeypatch):
    import sqlite3
    from tuneio.storage import StorageUnavailable
    app,clock,state=integration(config,monkeypatch)
    async def scenario():
        await app.storage.start()
        with sqlite3.connect(config.metrics.storage_path) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            for _ in range(6):await request(app)
            with pytest.raises(StorageUnavailable):await app.storage.query('flush')
            assert app.proxy.cache.entries and app.storage.stats['dropped_tuning_decisions']>0
            blocker.rollback()
        assert (await request(app)).headers['x-autotuner-cache']=='HIT'
        saved=await app.storage.query('report',app.session_id)
        assert saved['sampling']['tuning_history_may_be_incomplete']
        await app.storage.close();await app.proxy.close()
    asyncio.run(scenario())


def test_reuse_threshold_is_configurable(config):
    config.tuning.reuse_minimum=5
    c,rule,_=controller(config)
    assert not feed(c,rule,[10]*4)['enabled']
    assert feed(c,rule,[10])['enabled']


def test_fixed_policy_override_and_observe_rule_in_adaptive_mode(config,monkeypatch):
    config.cache.routes[0].policy='cache'
    app,clock,state=integration(config,monkeypatch)
    async def fixed():
        assert (await request(app)).headers['x-autotuner-cache']=='MISS'
        assert (await request(app)).headers['x-autotuner-cache']=='HIT'
        assert not app.proxy.cache.controller.states
        await app.proxy.close()
    asyncio.run(fixed())
    config.cache.routes[0].policy='observe'
    app,clock,state=integration(config,monkeypatch)
    async def observing():
        for _ in range(6):assert (await request(app)).headers['x-autotuner-cache-reason']=='rule_observe'
        assert not app.proxy.cache.controller.states and not app.proxy.cache.entries
        await app.proxy.close()
    asyncio.run(observing())


def test_ttl_cannot_overflow_controller_numeric_state():
    with pytest.raises(ValidationError):
        ProductConfig(upstream='http://origin',cache={'routes':[dict(id='large',match='/items',policy='adaptive',ttl_max_seconds=10**309)]})


def test_cache_explain_control_and_cli_after_sixteen_adaptive_reads(config,monkeypatch,tmp_path,capsys):
    from contextlib import nullcontext
    from unittest.mock import Mock
    import io
    from tuneio import cli
    payload=config.model_dump()
    payload['cache']['routes'] += [dict(id='other',match='/other',policy='adaptive'),dict(id='third',match='/third',policy='adaptive')]
    config=ProductConfig.model_validate(payload)
    app,clock,state=integration(config,monkeypatch)
    async def scenario():
        for _ in range(16):assert (await request(app)).status_code==200
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as client:
            result=await client.get('/_autotuner/cache/explain?path=/items/1')
            assert result.status_code==200
            data=result.json()
            assert data['serving_enabled'] and data['tuning']['enabled'] and data['cache']['entries']==1
            assert data['tuning']['inputs']['origin_observations']==5
        await app.proxy.close()
        return result.text
    body=asyncio.run(scenario())
    file=tmp_path/'config.json';file.write_text(config.model_dump_json())
    for name in ('CONFIG','UPSTREAM','LISTEN','MODE'):monkeypatch.delenv('AUTOTUNER_'+name,raising=False)
    opener=Mock(open=Mock(return_value=nullcontext(io.StringIO(body))))
    monkeypatch.setattr(cli.urllib.request,'build_opener',Mock(return_value=opener))
    assert cli.main(['cache','explain','/items/1','--config',str(file),'--json'])==0
    assert json.loads(capsys.readouterr().out)['tuning']['enabled']
