"""Exercise the complete CLI path with independently implemented disposable upstreams."""
import asyncio
import json

import httpx
import pytest

from tuneio import cli, proxy
from tuneio.demo import DemoAPI
from tuneio.product_config import load_config
from tuneio.qualification_fixture import QualificationFixture, seed
from tuneio.session import ControlSession


@pytest.mark.parametrize('fixture', ['ram','sqlite'])
def test_workflow_from_discovery_to_rollback(fixture, tmp_path, monkeypatch, capsys):
    if fixture == 'ram':
        origin = DemoAPI()
        route, mutation, method = '/api/products', '/api/products/1', 'PATCH'
        body = {'price_cents':7100}
    else:
        database = tmp_path/'orders.db'
        seed(database, 64)
        origin = QualificationFixture(database)
        route, mutation, method = '/report', '/mutate', 'POST'
        body = None
    spec = {'openapi':'3.1.0','paths':{route:{'get':{}}, mutation:{method.lower():{}}}}
    (tmp_path/'openapi.json').write_text(json.dumps(spec))
    base, tuned, reverted = [tmp_path/name for name in ('observe.json','tuned.json','rollback.json')]
    baseline, optimized = tmp_path/'before.json', tmp_path/'after.json'

    async def run():
        async def cli_json(*args):
            assert await asyncio.to_thread(cli.main, [*map(str,args), '--json']) == 0
            return json.loads(capsys.readouterr().out)

        async def reviewed(*args):
            preview = await cli_json(*args)
            assert not preview['written']
            saved = await cli_json(*args,'--apply',preview['review_token'])
            assert saved['written']

        inventory = await cli_json('discover','--project',tmp_path)
        options = [piece for row in inventory['operations'] for piece in ('--select',row['id'])]
        await reviewed('setup','--project',tmp_path,'--upstream','http://origin','--output',base.name,*options)
        loop = asyncio.get_running_loop()
        current = None
        clock = [0.]
        monkeypatch.setattr(proxy,'perf_counter',lambda:clock[0])

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=origin),base_url='http://origin') as upstream:
            async def forward(request):
                clock[0] += .010
                response = await upstream.send(request)
                return httpx.Response(response.status_code,headers=response.headers,stream=httpx.ByteStream(response.content))

            async def control(method, url, payload):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=current),base_url='http://proxy') as client:
                    response = await client.request(method,url,json=payload)
                    response.raise_for_status()
                    return response.json()

            def query(config, method, url, payload=None, **kwargs):
                return asyncio.run_coroutine_threadsafe(control(method,url,payload),loop).result(timeout=5)

            monkeypatch.setattr(cli,'local_json',query)
            inputs = ['--get',route,'--environment','local','--requests',4,'--warmup',5,'--app-state','isolated-seed']
            for config, artifact, stage in ((base,baseline,'baseline'), (tuned,optimized,'optimized'), (reverted,None,None)):
                current = ControlSession(load_config(config),transport=httpx.MockTransport(forward))
                current.proxy.cache.clock = lambda:clock[0]
                await current.storage.start()
                current.running = True
                try:
                    if artifact is not None:
                        await reviewed('baseline','--config',config,'--output',artifact,'--stage',stage,*inputs)
                    if config == base:
                        await reviewed('optimize','enable','--config',base,'--route',route,'--output',tuned,'--baseline',baseline)
                    elif config == tuned:
                        comparison = await cli_json('compare',baseline,optimized,'--output',tmp_path/'comparison.json')
                        assert comparison['comparable'] and comparison['optimized']['hits']==4
                        assert comparison['baseline']['hits']==0
                        monitor = await cli_json('monitor','--config',tuned,'--output',tmp_path/'monitor.json')
                        assert monitor['metrics']['summary']['cache_hits']==4 and monitor['policies'][0]['state']=='serving'
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=current),base_url='http://proxy') as client:
                            client.headers.pop('Accept')
                            client.headers.pop('Accept-Encoding')
                            hit = await client.get(route)
                            assert hit.headers['x-autotuner-cache']=='HIT'
                            for headers in ({'Authorization':'Bearer isolated'}, {'Cookie':'session=isolated'}):
                                private = await client.get(route,headers=headers)
                                assert private.headers['x-autotuner-cache-reason']=='credentials'
                            assert (await client.request(method,mutation,json=body)).status_code==200
                            fresh = await client.get(route)
                            assert fresh.headers['x-autotuner-cache']!='HIT' and fresh.content != hit.content
                        await reviewed('optimize','rollback','--config',tuned,'--output',reverted)
                    else:
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=current),base_url='http://proxy') as client:
                            assert (await client.get(route)).headers['x-autotuner-cache-reason']=='mode_observe'
                finally:
                    await current.console.runner.close()
                    await current.proxy.close()
                    await current.storage.close()
                if config == tuned:
                    tunings = await cli_json('report','--config',tuned,'--session',current.session_id)
                    assert any(row['applied'] for row in tunings['tuning_decisions'])

    asyncio.run(run())
