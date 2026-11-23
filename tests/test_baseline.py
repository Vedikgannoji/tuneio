import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from tuneio import baseline, cli
from tuneio.product_config import ProductConfig
from tuneio.session import ControlSession
from tuneio.storage import config_identity


@pytest.fixture
def target(tmp_path, monkeypatch):
    config = ProductConfig(upstream='http://127.0.0.1:8000', operations=[{'method': 'GET', 'path': '/items/*'}],
                           metrics={'storage_path': str(tmp_path / 'metrics.db')})
    monkeypatch.setattr(cli, 'resolved_config', lambda args: config)
    args = ['baseline', '--output', str(tmp_path / 'baseline.json'), '--json']
    return config, args, tmp_path / 'baseline.json'


def control_for(config, behavior='completed'):
    identity, _ = config_identity(config)

    def reply(config, method, path, payload=None):
        result = dict(schema_version=1, session_id='session', config_digest=identity)
        if path.endswith('/status'):
            return dict(result, running=True, tool_version='test', runtime={'in_flight': 0})
        if '/metrics?' in path:
            if behavior == 'changed':
                result['session_id'] = 'new-session'
            return dict(result, summary={'count': 5})
        if '/insights?' in path:
            return dict(result, findings=[{'code': 'slow_upstream'}], evaluations=[])
        if path.endswith('/run'):
            assert method == 'POST' and payload['expected_session_id'] == 'session' and not payload['allow_writes']
            step = payload['scenario']['steps'][0]
            assert step['method'] == 'GET' and step['repeat'] == 1 and 'body' not in step and 'headers' not in step
            reply.run_id = payload['run_id']
            if behavior == 'interrupted':
                raise KeyboardInterrupt
            return dict(run_id=reply.run_id, state='running')
        assert path.endswith('/stop') or path.endswith(reply.run_id)
        state = 'stopped' if path.endswith('/stop') else behavior
        rows = [] if state == 'stopped' else [dict(status=500 if state == 'failed' else 200, duration_ms=5,
            passed=state == 'completed', reason=None, path='/items/private-value', headers={'Authorization': 'secret'})]
        return dict(run_id=reply.run_id, state=state, results=rows, started_at=1, finished_at=2)

    return Mock(side_effect=reply)


def test_passive_capture_only_queries_control_and_preserves_findings(target, monkeypatch, capsys):
    config, args, output = target
    query = control_for(config)
    monkeypatch.setattr(cli, 'local_json', query)
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)['findings'] == 1
    data = json.loads(output.read_text())
    assert data['kind'] == 'passive' and data['state'] == 'completed'
    assert data['insights']['findings'][0]['code'] == 'slow_upstream'
    assert data['config']['upstream'] == 'http://{upstream}' and len(data['client_source_sha256']) == 64
    assert all(call.args[1] == 'GET' and call.args[2].startswith('/_autotuner/') for call in query.call_args_list)


@pytest.mark.parametrize('behavior, state, code', [('completed', 'completed', 0), ('failed', 'failed', 3),
                                                   ('interrupted', 'stopped', 3), ('changed', 'incomplete', 2)])
def test_active_review_and_partial_evidence(target, monkeypatch, capsys, behavior, state, code):
    config, args, output = target
    query = control_for(config, behavior if behavior != 'changed' else 'completed')
    if behavior == 'changed':
        original = query.side_effect
        def changed(*call):
            result = original(*call)
            if '/metrics?' in call[2]:
                result['session_id'] = 'replacement'
            return result
        query.side_effect = changed
    monkeypatch.setattr(cli, 'local_json', query)
    monkeypatch.setattr(baseline, 'sleep', lambda delay: None)
    active = args + ['--get', '/items/private-value', '--requests', '1', '--environment', 'local']
    assert cli.main(active) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['workload']['concurrency'] == 1 and not preview['written'] and not output.exists()
    query.assert_not_called()
    assert cli.main(active + ['--apply', 'wrong']) == 2
    query.assert_not_called()
    apply = ['--apply', preview['review_token']]
    assert cli.main(active + apply) == code
    record = output.read_text()
    data = json.loads(record)
    assert data['state'] == state and data['active_result']['completed'] == (0 if state == 'stopped' else 1)
    assert 'private-value' not in record and 'secret' not in record
    posts = [call for call in query.call_args_list if call.args[1] == 'POST']
    assert cli.main(active + apply) == 2
    assert [call for call in query.call_args_list if call.args[1] == 'POST'] == posts


@pytest.mark.parametrize('extra', [['--get','/items/x?token=secret'], ['--get','/items/..'], ['--get','/other'],
                                   ['--requests','21'], ['--timeout','6'], ['--delay-ms','1'], ['--window','0'],
                                   ['--requests','20','--timeout','5']])
def test_invalid_requests_do_not_contact_proxy(target, monkeypatch, extra):
    _, args, output = target
    query = Mock(side_effect=AssertionError('unexpected query'))
    monkeypatch.setattr(cli, 'local_json', query)
    active = args + ['--get', '/items/x', '--environment', 'local']
    assert cli.main(active + extra) == 2
    query.assert_not_called()
    assert not output.exists()


def test_session_mismatch_rejects_before_scenario_dispatch():
    async def check():
        proxy = ControlSession(ProductConfig(upstream='http://127.0.0.1:8000'))
        proxy.console.runner.start = Mock(side_effect=AssertionError('unexpected dispatch'))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url='http://proxy') as client:
                result = await client.post('/_autotuner/scenarios/run', json={
                    'scenario': {'name': 'baseline', 'steps': [{'name': 'Read', 'path': '/items/x'}]},
                    'expected_session_id': 'old-session'})
                assert result.status_code == 409
                proxy.console.runner.start.assert_not_called()
        finally:
            await proxy.proxy.close()
    asyncio.run(check())


def test_active_capture_through_proxy_and_scenario_runner(target, monkeypatch, capsys):
    config, args, output = target
    origin = Mock(return_value=httpx.Response(200, stream=httpx.ByteStream(b'body secret')))

    async def check():
        proxy = ControlSession(config, transport=httpx.MockTransport(origin))
        proxy.running = True
        loop = asyncio.get_running_loop()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url='http://proxy') as client:
                async def request(method, path, payload):
                    response = await client.request(method, path, json=payload)
                    response.raise_for_status()
                    return response.json()

                def query(config, method, path, payload=None):
                    return asyncio.run_coroutine_threadsafe(request(method, path, payload), loop).result(timeout=5)

                monkeypatch.setattr(cli, 'local_json', query)
                active = args + ['--get', '/items/42', '--requests', '1', '--environment', 'local']
                assert await asyncio.to_thread(cli.main, active) == 0
                token = json.loads(capsys.readouterr().out)['review_token']
                origin.assert_not_called()
                assert await asyncio.to_thread(cli.main, active + ['--apply', token]) == 0
        finally:
            await proxy.console.runner.close()
            await proxy.proxy.close()

    asyncio.run(check())
    origin.assert_called_once()
    request = origin.call_args.args[0]
    assert request.method == 'GET' and request.url.path == '/items/42'
    contents = output.read_text()
    result = json.loads(contents)
    assert result['active_result']['completed'] == 1
    assert result['metrics']['routes']['GET /items/*']['count'] == 1
    assert 'body secret' not in contents and '/items/42' not in contents
