"""Bounded stress/fault regressions with controlled synchronization and temporary SQL."""
import asyncio
from contextlib import closing
from itertools import count

import httpx

from tuneio.benchmarking import config_for
from tuneio.qualification_fixture import QualificationFixture,reference,seed
from tuneio.session import ControlSession
from tuneio.storage import connect_readonly,report as stored_report


def test_unique_key_pressure_mutation_faults_and_resource_recovery(tmp_path):
    async def run():
        database=tmp_path/'fixture.sqlite3';seed(database,64);fixture=QualificationFixture(database)
        config=config_for('adaptive','http://fixture',tmp_path/'metrics.sqlite3')
        config.limits.cache_bytes=8192;config.limits.cache_entry_bytes=4096;config.limits.cache_entries=4
        config.limits.upstream_concurrency=4;config.limits.queue_capacity=8
        config.metrics.raw_capacity=64;config.metrics.writer_queue_capacity=4;config.metrics.storage_max_bytes=131072
        config.tuning.enable_ms=.0001;config.tuning.retain_ms=.00001
        session=ControlSession(config,transport=httpx.ASGITransport(app=fixture))
        ticks=count();session.proxy.cache.clock=lambda:next(ticks)/1000
        await session.storage.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session),base_url='http://proxy') as client:
                for cycle in range(6):
                    # Origin work is real SQL; timing is not asserted.
                    responses=await asyncio.gather(*(client.get('/report?key='+str(cycle*24+i)) for i in range(24)))
                    assert all(r.status_code in (200,503) for r in responses)
                    assert all(r.content==reference(64,cycle) for r in responses if r.status_code==200)
                    assert (await client.get('/fault')).status_code==503
                    assert (await client.post('/mutate')).json()['version']==cycle+1
                    assert (await client.get('/report')).content==reference(64,cycle+1)
                    status=session.status()
                    assert status['cache']['accounted_bytes']<=8192 and status['cache']['entries']<=4
                    assert 0<=status['cache']['accounted_bytes']<=status['cache']['peak_accounted_bytes']<=8192
                    assert status['cache']['coordination_keys']==0 and status['runtime']['in_flight']==0
                    assert status['runtime']['hot_reads']==0 and status['cache']['active_writes']==0
                    assert session.proxy.slots._value==4
                    assert len(session.proxy.metrics.records)+len(session.proxy.upstream_metrics.records)<=64
                # A busy export queue is expected under pressure. Shutdown drains accepted work.
                assert session.proxy.metrics.snapshot()['summary']['server_error_rate']>0
                await session.storage.close()
                with closing(connect_readonly(config.metrics.storage_path)) as connection:
                    report=stored_report(connection,session.storage.digest,session.session_id)
                assert report['sampling']['disk_bytes']<=config.metrics.storage_max_bytes
                assert report['shutdown_recorded']
                assert session.proxy.counters['overload_rejections']>0
        finally:
            await session.proxy.close();await session.storage.close()
    asyncio.run(run())


def test_cancellation_storm_closes_connections_without_retained_coordination(tmp_path):
    async def run():
        entered,release=asyncio.Event(),asyncio.Event();closed=[]
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set();await release.wait();yield b'{}'
            async def aclose(self):closed.append(True)
        def handler(request):return httpx.Response(200,stream=Stream(),headers={'Cache-Control':'public,max-age=60'})
        config=config_for('cache','http://fixture',tmp_path/'m.sqlite3')
        config.cache.routes[0].policy='cache'
        session=ControlSession(config,transport=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session),base_url='http://proxy') as client:
            leader=asyncio.create_task(client.get('/report'));await entered.wait()
            waiters=[asyncio.create_task(client.get('/report')) for _ in range(12)]
            await asyncio.sleep(0)  # Yield scheduling only; no elapsed-time assertion.
            leader.cancel()
            for task in waiters[::2]:task.cancel()
            release.set();results=await asyncio.gather(leader,*waiters,return_exceptions=True)
            assert isinstance(results[0],asyncio.CancelledError)
            assert any(isinstance(item,httpx.Response) and item.status_code==200 for item in results)
            status=session.status()
            assert status['cache']['coordination_keys']==0 and status['cache']['retired_pinned_entries']==0
            assert status['runtime']['in_flight']==status['runtime']['hot_reads']==0
            assert session.proxy.slots._value==8 and closed
        await session.proxy.close()
    asyncio.run(run())
