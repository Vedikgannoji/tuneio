import asyncio
from contextlib import closing
import json

from tuneio.product_config import ProductConfig
from tuneio.storage import TelemetryStorage
from tuneio.benchmarking import config_for
from tuneio.core.controller import AdaptiveController


def test_minimum_disk_budget_reclaims_audit_rows_and_keeps_writer_healthy(tmp_path):
    async def run():
        config=ProductConfig(upstream='http://fixture',metrics={'storage_path':str(tmp_path/'pressure.sqlite3'),
            'storage_max_bytes':131072},cache={'routes':[{'id':'items','match':'/items','policy':'adaptive'}]})
        store=TelemetryStorage(config,'pressure',clock=lambda:1_800_000_000.);await store.start()
        try:
            for n in range(30):
                store.record_decision({'rule_id':'items','reason':'pressure-test','payload':'x'*3000,'sequence':n})
                await store.query('flush')
                assert store.state=='ready' and store.stats['write_errors']==0
            assert store.stats['persisted_tuning_decisions']==30
            assert store.stats['tuning_decisions_truncated']>0
            for n in range(10):
                for i in range(8):store.submit('proxy',dict(timestamp=1_800_000_000.,method='GET',route='/items',
                    status=200,duration_ms=n*8+i,cache='MISS'))
                await store.query('flush')
            report=await store.query('report','pressure')
            assert report['sampling']['sampling_incomplete']
            assert report['sampling']['write_errors']==0 and report['sampling']['state']=='ready'
            assert report['sampling']['persisted_observations']==80
            assert len(report['tuning_decisions'])<30
            assert sum(p.stat().st_size for p in tmp_path.glob('pressure.sqlite3*'))<=131072
        finally:await store.close()
        assert store.state=='closed'
    asyncio.run(run())


def test_reclaiming_previous_session_audit_marks_its_history_incomplete(tmp_path):
    config=ProductConfig(upstream='http://fixture',metrics={'storage_path':str(tmp_path/'history.sqlite3')})
    store=TelemetryStorage(config,'current')
    with closing(store._open()) as connection:
        with connection:
            connection.execute("INSERT INTO sessions SELECT 'prior',started,ended,config_digest,config,health,tool_version,source_digest FROM sessions WHERE id='current'")
            connection.execute('INSERT INTO decisions VALUES(?,?,?,?)',('prior',1,0,'{}'))
        with connection:assert store._reclaim_budget(connection)
        health=json.loads(connection.execute("SELECT health FROM sessions WHERE id='prior'").fetchone()[0])
        assert health['tuning_decisions_truncated']==1 and health['sampling_incomplete']
        assert health['tuning_history_may_be_incomplete']
        assert store.stats['tuning_decisions_truncated']==0
        assert not store._reclaim_budget(connection)


def test_small_audit_rows_interleaved_with_metrics_reclaim_page_space(tmp_path):
    # A single-row eviction can remove bytes without releasing a B-tree page.
    # This reproduces the remaining user-soak failure with actual controller records.
    config=config_for('adaptive','http://fixture',tmp_path/'mixed.sqlite3')
    config.metrics.storage_max_bytes=131072
    store=TelemetryStorage(config,'mixed',clock=lambda:1_800_000_000.)
    controller=AdaptiveController(config,clock=lambda:0,wall_clock=lambda:1_800_000_000.)
    with closing(store._open()) as connection:
        store.state='ready';store.accepting=True
        for n in range(80):
            controller.observe(config.cache.routes[0],'same',3+n%20)
            store._write_decision(connection,(n+1,1_800_000_000.,controller.history[-1]))
            store._write(connection,[(stream,dict(timestamp=1_800_000_000.,method='GET',route='/report',
                status=200,duration_ms=n*.131+1,cache='BYPASS')) for stream in ('upstream','proxy')])
            store._checkpoint(connection)
        assert store.stats['persisted_observations']==160
        assert store.stats['persisted_tuning_decisions']==80
        assert store.stats['tuning_decisions_truncated']>0
        assert store.stats['write_errors']==0 and store.state=='ready'
        assert store.path.stat().st_size<=131072
