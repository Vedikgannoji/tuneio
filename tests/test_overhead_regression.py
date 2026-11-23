"""Controlled clocks distinguish workload latency from client/setup/scheduler cost."""
import asyncio

import httpx
import pytest

from tuneio import benchmarking as benchmark
from tuneio.core.controller import AdaptiveController
from tuneio.product_config import ProductConfig
from tuneio.qualification_fixture import encode


def test_client_setup_and_teardown_do_not_consume_pacing_or_workload_budget(monkeypatch):
    async def run():
        now=[0.];sent=[]
        real_client=httpx.AsyncClient
        class Client(real_client):
            def __init__(self,*args,**kwargs):
                now[0]+=3
                super().__init__(*args,**kwargs)
            async def __aenter__(self):
                now[0]+=5
                return await super().__aenter__()
            async def __aexit__(self,*args):
                await super().__aexit__(*args)
                now[0]+=7
        def handler(request):
            sent.append(now[0]);now[0]+=.001
            return httpx.Response(200,content=encode({'message':'Reference payload'}))
        async def sleep(seconds):now[0]+=seconds
        monkeypatch.setattr(benchmark.httpx,'AsyncClient',Client)
        monkeypatch.setattr(benchmark,'perf_counter',lambda:now[0])
        monkeypatch.setattr(asyncio,'sleep',sleep)
        result=await benchmark.load('http://fixture',benchmark.Options(requests=20,rate=100),'overhead',
                                    transport=httpx.MockTransport(handler))
        assert sent==pytest.approx([8+i*.01 for i in range(20)])
        assert result['client_setup_ms']==pytest.approx(8000)
        assert result['client_close_ms']==pytest.approx(7000)
        assert result['elapsed_seconds']==pytest.approx(.191)
        assert result['scheduling_lag_p95_ms']==pytest.approx(0)
        assert result['mean_ms']==pytest.approx(1)
        assert result['unexpected_failures']==0
    asyncio.run(run())


def test_early_timer_wakeup_rechecks_deadline_before_dispatch():
    async def run():
        times=iter([0,.4,1]);waits=[]
        async def sleep(seconds):waits.append(seconds)
        await benchmark.pace(1,clock=lambda:next(times),sleep=sleep)
        assert waits==pytest.approx([1,.6])
        async def unexpected_sleep(seconds):pytest.fail('Past deadline must dispatch without delay')
        await benchmark.pace(1,clock=lambda:2,sleep=unexpected_sleep)
    asyncio.run(run())


def test_cancel_during_pacing_does_not_schedule_a_request():
    async def run():
        entered=asyncio.Event()
        async def sleep(seconds):entered.set();await asyncio.Event().wait()
        task=asyncio.create_task(benchmark.pace(1,clock=lambda:0,sleep=sleep))
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())


def test_unchanged_policy_checks_do_not_construct_duplicate_audit_records():
    config=ProductConfig(upstream='http://fixture',mode='adaptive',cache={'routes':[
        {'id':'items','match':'/items','policy':'adaptive'}]})
    wall_calls=[]
    def wall():wall_calls.append(True);return 1_800_000_000.
    controller=AdaptiveController(config,clock=lambda:0,wall_clock=wall)
    listener=[];controller.listener=listener.append;rule=config.cache.routes[0]
    first=controller.evaluate(rule)
    for _ in range(20):assert controller.evaluate(rule)==first
    assert len(wall_calls)==len(listener)==len(controller.history)==1
    for _ in range(4):controller.observe(rule,'same',10)
    assert controller.evaluate(rule)['enabled']
    assert len(wall_calls)==len(listener)==len(controller.history)==5
    # A caller must not be able to alter the retained audit's thresholds.
    result=controller.evaluate(rule);result['thresholds']['enable_ms']=999
    assert controller.history[-1]['thresholds']['enable_ms']==2
