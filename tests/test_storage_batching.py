import asyncio
from queue import Queue,Empty
from unittest.mock import Mock

import pytest

from tuneio.product_config import ProductConfig
from tuneio.storage import collect_events,TelemetryStorage,BATCH_LIMIT


def test_export_is_a_fifo_barrier_and_preserves_following_events():
    queue=Queue();barrier=('report','session',object())
    queue.put(('event',2,None));queue.put(barrier);queue.put(('event',3,None))
    ticks=iter([100,100.005,100.010])
    batch,pending=collect_events(1,queue,clock=lambda:next(ticks))
    assert batch==[1,2] and pending is barrier
    assert queue.get_nowait()==('event',3,None)


def test_batch_count_and_deadline_are_independently_bounded():
    queue=Queue()
    for n in range(1,100):queue.put(('event',n,None))
    batch,pending=collect_events(0,queue,clock=lambda:0)
    assert len(batch)==BATCH_LIMIT and pending is None and queue.qsize()==36
    ticks=iter([0,.021]);queue=Mock()
    assert collect_events(1,queue,clock=lambda:next(ticks))==([1],None)
    queue.get.assert_not_called()


def test_empty_queue_uses_only_remaining_batch_window():
    queue=Mock();queue.get.side_effect=Empty
    ticks=iter([0,.005])
    assert collect_events(1,queue,clock=lambda:next(ticks))==([1],None)
    assert queue.get.call_args.kwargs['timeout']==pytest.approx(.015)


def test_batched_writer_flushes_every_accepted_event_before_export_and_close(tmp_path):
    async def run():
        config=ProductConfig(upstream='http://fixture',metrics={'storage_path':str(tmp_path/'metrics.sqlite3')})
        store=TelemetryStorage(config,'batching',clock=lambda:1_800_000_000.);await store.start()
        try:
            for i in range(20):store.submit('proxy',dict(timestamp=1_800_000_000.,method='GET',route='/items',status=200,
                duration_ms=i+1,cache='MISS'))
            data=await store.query('report','batching')
            assert data['sampling']['persisted_observations']==20
            assert 1<=store.health()['largest_batch_observations']<=BATCH_LIMIT
            assert 1<=store.health()['write_batches']<=20
            assert store.health()['writer_batch_window_ms']==20
        finally:await store.close()
        assert store.state=='closed' and store.stats['dropped_storage']==0
    asyncio.run(run())
