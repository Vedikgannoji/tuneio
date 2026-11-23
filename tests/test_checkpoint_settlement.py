"""Client completion can precede the server's post-response accounting."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from tuneio import benchmarking as b


def status(*,count=60,in_flight=0,hot_reads=0,keys=0):
    return dict(transport=dict(completed_requests=count),runtime=dict(in_flight=in_flight,hot_reads=hot_reads),
                cache=dict(coordination_keys=keys))


def test_delayed_completion_is_observed_before_checkpoint(monkeypatch):
    async def run():
        control=AsyncMock(side_effect=[status(count=59,in_flight=1),status()])
        monkeypatch.setattr(b,'control',control)
        snapshot,settlement=await b.completion_status(None,60,clock=lambda:0)
        assert snapshot['transport']['completed_requests']==60
        assert settlement==dict(state='settled',polls=2,wait_ms=0)
        assert control.await_count==2
    asyncio.run(run())


@pytest.mark.parametrize('fields',[dict(count=59),dict(count=61),dict(in_flight=1),dict(hot_reads=1),dict(keys=1)])
def test_persistent_leaks_and_mismatches_remain_unsettled(monkeypatch,fields):
    async def run():
        control=AsyncMock(return_value=status(**fields));monkeypatch.setattr(b,'control',control)
        snapshot,settlement=await b.completion_status(None,60,clock=lambda:0,max_polls=3)
        assert snapshot==status(**fields) and settlement['state']=='unsettled'
        assert settlement['polls']==control.await_count==3
    asyncio.run(run())


def test_deadline_stops_polling_without_accepting_last_busy_sample(monkeypatch):
    async def run():
        ticks=iter([0,0,6,6])
        control=AsyncMock(return_value=status(in_flight=1));monkeypatch.setattr(b,'control',control)
        _,settlement=await b.completion_status(None,60,clock=lambda:next(ticks))
        assert settlement['state']=='unsettled' and control.await_count==1
    asyncio.run(run())


@pytest.mark.parametrize('has_snapshot',[False,True])
def test_control_timeout_is_bounded_and_never_qualifies(monkeypatch,has_snapshot):
    async def run():
        values=([status(in_flight=1)] if has_snapshot else [])+[asyncio.TimeoutError()]
        control=AsyncMock(side_effect=values);monkeypatch.setattr(b,'control',control)
        if has_snapshot:
            _,settlement=await b.completion_status(None,60,clock=lambda:0)
            assert settlement['state']=='unsettled'
        else:
            with pytest.raises(RuntimeError,match='completion status timed out'):
                await b.completion_status(None,60,clock=lambda:0)
    asyncio.run(run())


def test_control_errors_propagate(monkeypatch):
    async def run():
        monkeypatch.setattr(b,'control',AsyncMock(side_effect=RuntimeError('Control failed')))
        with pytest.raises(RuntimeError,match='Control failed'):
            await b.completion_status(None,60)
    asyncio.run(run())
