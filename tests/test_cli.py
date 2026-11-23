import asyncio
import io
import json
import sys
from unittest.mock import Mock

import httpx
import pytest

from tuneio import cli
from tuneio.product_config import ProductConfig
from tuneio.session import ControlSession


@pytest.fixture
def config(tmp_path, monkeypatch):
    for name in ('CONFIG', 'UPSTREAM', 'LISTEN', 'MODE'):
        monkeypatch.delenv('AUTOTUNER_' + name, raising=False)
    path = tmp_path / 'autotuner.json'
    path.write_text('{"upstream":"http://127.0.0.1:3000"}')
    monkeypatch.chdir(tmp_path)
    return path


def test_init_and_overwrite(config, capsys):
    assert cli.main(['init']) == 2
    assert cli.main(['init', '--force', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'observe'
    assert json.loads(config.read_text())['cache']['routes'] == []


def test_precedence_and_offline_checks(config, monkeypatch, capsys):
    monkeypatch.setenv('AUTOTUNER_CONFIG', str(config))
    monkeypatch.setenv('AUTOTUNER_UPSTREAM', 'http://localhost:4000')
    monkeypatch.setenv('AUTOTUNER_MODE', 'recommend')
    assert cli.main(['config', 'check', '--json', '--upstream', 'https://example.com']) == 0
    result = json.loads(capsys.readouterr().out)['config']
    assert result['upstream'] == 'https://example.com' and result['mode'] == 'recommend'
    network = Mock(side_effect=AssertionError('unexpected network'))
    monkeypatch.setattr(cli.socket, 'create_connection', network)
    assert cli.main(['doctor', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['upstream_check'] == 'not_requested'
    assert cli.main(['serve', '--check', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['forwarding_enabled'] is False
    network.assert_not_called()


@pytest.mark.parametrize('text', ['{bad', '[]', '{"upstream":"http://user:super-secret@localhost"}',
                                  '{"upstream":"http://localhost","extra":"super-secret"}'])
def test_invalid_config_is_redacted(config, capsys, text):
    config.write_text(text)
    assert cli.main(['config', 'check', '--json']) == 2
    captured = capsys.readouterr()
    assert not captured.out and 'super-secret' not in captured.err


def test_missing_file_and_io_failure(config, monkeypatch):
    assert cli.main(['config', 'check', '--config', 'missing.json']) == 2
    monkeypatch.setattr(cli, 'resolved_config', Mock(side_effect=PermissionError()))
    assert cli.main(['doctor']) == 1


def test_serve_runner_and_interruption(config, monkeypatch, capsys):
    import uvicorn
    runner = Mock()
    monkeypatch.setattr(uvicorn, 'run', runner)
    assert cli.main(['serve', '--listen', '[::1]:8082']) == 0
    assert runner.call_args.kwargs['host'] == '::1'
    assert runner.call_args.kwargs['server_header'] is False
    assert runner.call_args.kwargs['date_header'] is False
    assert runner.call_args.args[0].status()['forwarding_enabled'] is True
    assert not capsys.readouterr().out
    assert cli.main(['serve', '--json']) == 2
    assert cli.main(['serve', '--shutdown-timeout', '0']) == 2
    runner.side_effect = KeyboardInterrupt()
    assert cli.main(['serve']) == 0


def test_explicit_connect_and_status(config, monkeypatch, capsys):
    from contextlib import nullcontext
    connection = Mock(return_value=nullcontext())
    monkeypatch.setattr(cli.socket, 'create_connection', connection)
    assert cli.main(['doctor', '--connect', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['upstream_check'].startswith('tcp_reachable')
    assert connection.call_args.kwargs['timeout'] == 2
    config.write_text('{"upstream":"https://example.com"}')
    assert cli.main(['doctor', '--connect']) == 0
    assert connection.call_args.args[0][1] == 443
    monkeypatch.setattr(cli.urllib.request, 'build_opener', Mock(return_value=Mock(open=Mock(return_value=nullcontext(io.StringIO('{"schema_version":1,"phase":3,"session_id":"abc"}'))))))
    capsys.readouterr()
    assert cli.main(['status', '--json']) == 0
    result=json.loads(capsys.readouterr().out)
    assert result['session_id'] == 'abc' and 'phase' not in result
    monkeypatch.setattr(cli.urllib.request, 'build_opener', Mock(return_value=Mock(open=Mock(return_value=nullcontext(io.StringIO('{}'))))))
    assert cli.main(['status']) == 1
    monkeypatch.setattr(cli.urllib.request, 'build_opener', Mock(return_value=Mock(open=Mock(return_value=nullcontext(io.StringIO('not-json'))))))
    assert cli.main(['status']) == 1


def test_help_version_and_entrypoint(config, monkeypatch, capsys):
    for args in (['--help'], ['--version']):
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 0
    assert 'tuneio '+cli.VERSION in capsys.readouterr().out
    monkeypatch.setattr(sys, 'argv', ['tuneio', 'doctor'])
    with pytest.raises(SystemExit) as exc:
        cli.entrypoint()
    assert exc.value.code == 0


def test_module_entrypoint_and_redirect_policy(monkeypatch, capsys):
    import runpy
    monkeypatch.setattr(sys, 'argv', ['tuneio', '--version'])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module('tuneio', run_name='__main__')
    assert exc.value.code == 0
    assert 'tuneio '+cli.VERSION in capsys.readouterr().out
    assert cli.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com') is None


def test_endpoint_metrics_command(config, monkeypatch, capsys):
    from contextlib import nullcontext
    opener = Mock(open=Mock(return_value=nullcontext(io.StringIO('{"schema_version":1,"session_id":"abc","routes":{}}'))))
    monkeypatch.setattr(cli.urllib.request, 'build_opener', Mock(return_value=opener))
    assert cli.main(['endpoints', '--window', '300', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['routes'] == {}
    assert opener.open.call_args.args[0].endswith('/_autotuner/metrics?window=300')
    assert cli.main(['endpoints', '--window', '0']) == 2


def test_session_lifecycle_and_unsupported_forwarding(tmp_path):
    app = ControlSession(ProductConfig(upstream='http://localhost:3000', mode='adaptive', metrics={'storage_path': str(tmp_path/'metrics.sqlite3')}), transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'ok')))
    async def scenario():
        messages = iter([{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])
        sent = []
        async def receive(): return next(messages)
        async def send(message): sent.append((message['type'], app.running))
        await app({'type': 'lifespan'}, receive, send)
        assert sent == [('lifespan.startup.complete', True), ('lifespan.shutdown.complete', False)]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/_autotuner/status')).json()['forwarding_enabled'] is True
            assert (await client.post('/_autotuner/health')).status_code == 405
            assert (await client.options('/_autotuner/status')).status_code == 405
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,expected',[
    ('refused','Cannot reach the local proxy at 127.0.0.1:8080'),
    ('http','Local proxy returned HTTP 500'),
    ('timeout','Local proxy request timed out'),
    ('wrapped_timeout','Local proxy request timed out'),
    ('reset','Cannot reach the local proxy'),
])
def test_local_control_failures_are_actionable_and_redacted(config,monkeypatch,capsys,failure,expected):
    import urllib.error
    error=(urllib.error.URLError(ConnectionRefusedError('sensitive reason')) if failure=='refused' else
           urllib.error.HTTPError('http://127.0.0.1:8080/secret',500,'sensitive reason',{},io.BytesIO(b'sensitive body')) if failure=='http' else TimeoutError('sensitive reason'))
    if failure=='wrapped_timeout':error=urllib.error.URLError(TimeoutError('sensitive reason'))
    if failure=='reset':error=ConnectionResetError('sensitive reason')
    opener=Mock(open=Mock(side_effect=error))
    monkeypatch.setattr(cli.urllib.request,'build_opener',Mock(return_value=opener))
    assert cli.main(['cache','explain','/api/products','--json'])==1
    captured=capsys.readouterr()
    assert expected in captured.err and 'sensitive' not in captured.err and not captured.out
