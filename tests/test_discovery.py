import io
import json
import asyncio
from unittest.mock import Mock

import httpx
import pytest

from tuneio import cli, discovery
from tuneio.product_config import load_config
from tuneio.session import ControlSession


def schema(paths):
    return json.dumps({'openapi': '3.1.0', 'paths': paths})


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'app.py').write_text(
        'from fastapi import FastAPI as Factory\napi = Factory()\n'
        'raise RuntimeError("discovery must never import this")\n'
        '@api.get("/items/{item_id}")\ndef item(): pass\n'
        '@api.post(path="/items")\nasync def create(): pass\n'
        '@api.get(dynamic_path)\ndef dynamic(): pass\n', encoding='utf-8')
    return tmp_path


def test_static_fastapi_is_offline_and_does_not_import(project, monkeypatch):
    monkeypatch.setattr(discovery, 'fetch_description', Mock(side_effect=AssertionError('unexpected network')))
    ignored = project / '.venv'
    ignored.mkdir()
    (ignored / 'app.py').write_bytes((project / 'app.py').read_bytes())
    (project / 'broken.py').write_text('def (')
    result = discovery.discover(project)
    assert result['entrypoint_candidates'] == ['app:api'] and not result['complete']
    assert [(row['method'], row['pattern']) for row in result['operations']] == [('GET', '/items/*'), ('POST', '/items')]
    assert all(row['confidence'] == 'candidate' for row in result['operations'])
    assert any('Dynamic' in message for message in result['warnings'])
    assert any('parsed' in message for message in result['warnings'])


def test_static_factory_is_discovered_before_generated_files(tmp_path, monkeypatch):
    scratch = tmp_path / 'tmp'
    scratch.mkdir()
    (scratch / 'fixture.py').write_text('raise RuntimeError("generated fixture")')
    source = tmp_path / 'service'
    source.mkdir()
    (source / 'main.py').write_text(
        'import fastapi as fa\n'
        'def create_app():\n'
        '    app = fa.FastAPI()\n'
        '    @app.get("/items/{id}")\n'
        '    def detail(): pass\n'
        '    raise RuntimeError("must never be called by discovery")\n'
        '    return app\n'
    )
    monkeypatch.setattr(discovery, 'MAX_FILES', 1)
    result = discovery.discover(tmp_path)
    assert result['factory_entrypoint_candidates'] == ['service.main:create_app']
    assert result['entrypoint_candidates'] == []
    assert [row['pattern'] for row in result['operations']] == ['/items/*']
    assert not any('Python-file limit' in item for item in result['warnings'])


def test_openapi_skips_unsupported_paths_and_references(project):
    (project / 'openapi.json').write_text(schema({
        '/': {'get': {}}, '/items/{id}': {'patch': {}}, '/file/{id}.json': {'get': {}},
        '/linked': {'$ref': 'outside.json'}, '/x/../y': {'get': {}}, '/trace': {'trace': {}},
    }))
    result = discovery.discover(project)
    assert result['source'] == 'local OpenAPI'
    assert [row['pattern'] for row in result['operations']] == ['/', '/items/*']
    assert len(result['warnings']) == 2


@pytest.mark.parametrize('body', ['{', '[]', '{"openapi":"3.1.0","paths":[]}', ' ' * (discovery.MAX_BYTES + 1)],
                         ids=['malformed', 'wrong-root', 'wrong-paths', 'oversized'])
def test_invalid_or_oversized_schema(project, body):
    (project / 'openapi.json').write_text(body)
    with pytest.raises(ValueError):
        discovery.discover(project)


def test_path_boundary(project):
    with pytest.raises(ValueError):
        discovery.discover(project, openapi='../openapi.json')
    with pytest.raises(ValueError):
        discovery.discover(project, openapi=project.parent / 'openapi.json')


def test_linked_schema_is_skipped(project):
    try:
        (project / 'linked.json').symlink_to(project / 'app.py')
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(ValueError, match='Linked'):
        discovery.discover(project, openapi='linked.json')


@pytest.mark.parametrize('url', ['http://example.com/openapi.json', 'http://localhost/openapi.json',
                                 'http://user@127.0.0.1/schema', 'http://127.0.0.1/schema?token=x'])
def test_live_source_limits_destination(project, monkeypatch, url):
    opener = Mock(side_effect=AssertionError('unexpected network'))
    monkeypatch.setattr(discovery.urllib.request, 'build_opener', opener)
    with pytest.raises(ValueError):
        discovery.discover(project, openapi_url=url)
    opener.assert_not_called()


def test_live_source_reads_explicit_url(project, monkeypatch):
    response = io.BytesIO(schema({'/live': {'get': {}}}).encode())
    opener = Mock(open=Mock(return_value=response))
    monkeypatch.setattr(discovery.urllib.request, 'build_opener', Mock(return_value=opener))
    url = 'http://127.0.0.1:8000/openapi.json'
    result = discovery.discover(project, openapi_url=url)
    assert result['operations'][0]['path'] == '/live'
    opener.open.assert_called_once_with(url, timeout=3)
    assert discovery.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com') is None


@pytest.mark.parametrize('body', [b'x' * 40, b'{}'], ids=['oversize', 'deadline'])
def test_live_source_is_bounded(project, monkeypatch, body):
    opener = Mock(open=Mock(return_value=io.BytesIO(body)))
    monkeypatch.setattr(discovery.urllib.request, 'build_opener', Mock(return_value=opener))
    monkeypatch.setattr(discovery, 'MAX_BYTES', 32)
    if len(body) > 32:
        error = ValueError
    else:
        monkeypatch.setattr(discovery, 'monotonic', Mock(side_effect=[0, 4]))
        error = TimeoutError
    with pytest.raises(error):
        discovery.discover(project, openapi_url='http://127.0.0.1:8000/openapi.json')


def test_setup_preview_exact_apply_and_nonoverwrite(project, capsys):
    base = ['setup', '--project', str(project), '--select', '1', '--upstream', 'http://127.0.0.1:8000', '--json']
    assert cli.main(base) == 0
    preview = json.loads(capsys.readouterr().out)
    assert not preview['written'] and not (project / 'autotuner.json').exists()
    assert preview['config']['mode'] == 'observe' and not preview['config']['cache']['routes']
    apply = ['--apply', preview['review_token']]
    assert cli.main(base + ['--select', '2'] + apply) == 2
    assert not (project / 'autotuner.json').exists()
    assert cli.main(base + apply) == 0
    file = project / 'autotuner.json'
    original = file.read_bytes()
    assert json.loads(original)['operations'] == [{'method': 'GET', 'path': '/items/*'}]
    assert cli.main(base + apply) == 2 and file.read_bytes() == original


def test_setup_rejects_changed_inventory_before_writing(project, capsys):
    description = project / 'openapi.json'
    description.write_text(schema({'/items/{id}': {'get': {}}}))
    args = ['setup', '--project', str(project), '--select', '1', '--upstream', 'http://127.0.0.1:8000', '--json']
    assert cli.main(args) == 0
    token = json.loads(capsys.readouterr().out)['review_token']
    description.write_text(schema({'/items/{name}': {'get': {}}}))
    assert cli.main(args + ['--apply', token]) == 2
    assert 'Preview changed' in capsys.readouterr().err
    assert not (project / 'autotuner.json').exists()


def test_selected_openapi_operation_labels_forwarded_traffic(project):
    (project / 'openapi.json').write_text(schema({'/items/{id}': {'get': {}}}))
    inventory = discovery.discover(project)
    config = discovery.reviewed_configuration(inventory, [1], 'http://127.0.0.1:8000', '127.0.0.1:8080')
    config.metrics.storage_path = str(project / 'metrics.sqlite3')
    path = discovery.write_configuration(project, 'observe.json', config)

    async def scenario():
        origin = Mock(return_value=httpx.Response(200, stream=httpx.ByteStream(b'{"answer": 42}')))
        app = ControlSession(load_config(path), transport=httpx.MockTransport(origin))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://proxy') as client:
                response = await client.get('/items/42')
                assert response.json() == {'answer': 42}
                metrics = (await client.get('/_autotuner/metrics')).json()
                assert metrics['routes']['GET /items/*']['count'] == 1
                assert origin.call_count == 1
        finally:
            await app.proxy.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('extra', [['--select','1'], ['--select','99'], ['--output','../out.json'],
                                  ['--output','NUL.json'], ['--listen','0.0.0.0:8080']])
def test_setup_rejects_bad_selection_or_output(project, extra):
    assert cli.main(['setup', '--project', str(project), '--select', '1', '--upstream', 'http://127.0.0.1:8000', *extra]) == 2
    assert not (project / 'autotuner.json').exists()


def test_scan_and_operation_budgets(project, monkeypatch):
    monkeypatch.setattr(discovery, 'MAX_FILES', 0)
    result = discovery.discover(project)
    assert not result['operations'] and any('Python-file limit' in item for item in result['warnings'])
    monkeypatch.setattr(discovery, 'MAX_OPERATIONS', 1)
    (project / 'openapi.json').write_text(schema({'/a': {'get': {}, 'post': {}}}))
    result = discovery.discover(project)
    assert len(result['operations']) == 1 and any('operation limit' in item for item in result['warnings'])
