import hashlib
import json
from unittest.mock import Mock

import httpx
import pytest

from autotuner import __version__, cli, release
from autotuner.demo import DemoAPI


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(release, 'source_hashes', lambda: {'example.py': 'abc'})
    digest = hashlib.sha256(json.dumps({'example.py': 'abc'}, sort_keys=True).encode()).hexdigest()
    manifest = dict(schema_version=1, version=__version__)
    def put(name, data):
        data = dict(tool_version=__version__, source_digest=digest, **data)
        data.setdefault('schema_version', 1)
        raw = json.dumps(data).encode(); (tmp_path / (name + '.json')).write_bytes(raw)
        manifest[name] = dict(path=name + '.json', sha256=hashlib.sha256(raw).hexdigest())
    put('qualification', dict(schema_version=1, measurement_revision=2,
        evidence_type='real_http_isolated_product_benchmark', options=dict(profile='qualification'),
        gates={key: dict(state='confirmed' if key == 'reference_environment' else 'passed') for key in release.QUALIFICATION_GATES}))
    put('soak', dict(schema_version=1, evidence_type='real_http_isolated_product_benchmark',
        options=dict(profile='soak', duration_seconds=1800), graceful_shutdown=True,
        gates={key: dict(state='passed') for key in ('telemetry_health', 'stability_30_minutes')}))
    put('review_receipt', dict(schema_version=1, compatibility=[dict(os=os, python=version, pytest=True, node=True,
        core_coverage=True, clean_wheel_install=True) for os, version in release.MATRIX],
        **dict.fromkeys(release.REVIEW_FLAGS,True)))
    path = tmp_path / 'candidate.json'
    def save(): path.write_text(json.dumps(manifest)); return path
    return manifest, put, save


def test_complete_synthetic_review_and_cli(evidence, capsys):
    _, _, save = evidence
    path = save()
    assert release.assess(path)['ready'] is True
    assert cli.main(['release', 'check', '--evidence', str(path), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['publication_performed'] is False


@pytest.mark.parametrize('kind', ['missing', 'hash', 'traversal', 'version', 'stale', 'bad_shape'])
def test_untrusted_or_stale_evidence_blocks(evidence, kind):
    manifest, put, save = evidence
    if kind == 'missing': del manifest['soak']
    if kind == 'hash': manifest['soak']['sha256'] = 'bad'
    if kind == 'traversal': manifest['soak']['path'] = '../candidate.json'
    if kind == 'version': manifest['version'] = 'old'
    if kind in ('stale', 'bad_shape'):
        path = save().parent / 'soak.json'; data = json.loads(path.read_text())
        if kind == 'stale': data['source_digest'] = 'old'
        else: data['gates'] = []
        raw = json.dumps(data).encode(); path.write_bytes(raw)
        manifest['soak']['sha256'] = hashlib.sha256(raw).hexdigest()
    assert release.assess(save())['ready'] is False


def test_failed_gates_and_incomplete_review(evidence):
    _, put, save = evidence
    put('qualification', dict(options=dict(profile='quick'), gates={}))
    put('soak', dict(options=dict(profile='soak', duration_seconds=60), gates={}))
    put('review_receipt', dict(compatibility=[]))
    result = release.assess(save())
    assert 'qualification:contract' in result['blockers']
    assert 'soak:contract' in result['blockers']
    assert 'compatibility_matrix' in result['blockers']
    assert 'publisher_identity_confirmed' in result['blockers']


@pytest.mark.parametrize('content', ['[]', '{', '{"schema_version":2}', ' ' * 262145], ids=['array','json','schema','oversize'])
def test_invalid_manifest(tmp_path, content, capsys):
    path = tmp_path / 'bad.json'; path.write_text(content)
    assert cli.main(['release', 'check', '--evidence', str(path), '--json']) == 2
    assert not capsys.readouterr().out


def test_pending_manifest_is_offline(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'pending.json'; path.write_text(json.dumps(dict(schema_version=1, version=__version__)))
    network = Mock(side_effect=AssertionError('network forbidden'))
    monkeypatch.setattr(cli.socket, 'create_connection', network)
    assert cli.main(['release', 'check', '--evidence', str(path), '--json']) == 3
    assert json.loads(capsys.readouterr().out)['ready'] is False
    network.assert_not_called()


@pytest.mark.parametrize('content', [
    '{"schema_version":true}',
    '{"schema_version":1,"schema_version":2}',
    '{"schema_version":1,"value":NaN}',
    '{"schema_version":1,"value":Infinity}',
    '{"schema_version":1,"value":1e999}',
    '{"value":' + '[' * 1500 + '0' + ']' * 1500 + '}',
], ids=['boolean-schema','duplicate','nan','infinity','overflow-float','deep'])
def test_ambiguous_or_deep_manifest_is_sanitized(tmp_path, content, capsys):
    path = tmp_path / 'bad.json'; path.write_text(content)
    assert cli.main(['release', 'check', '--evidence', str(path), '--json']) == 2
    assert 'Traceback' not in capsys.readouterr().err


@pytest.mark.parametrize('duration', [10**400, float('inf'), float('nan'), True, 1799, 3601])
def test_soak_duration_cannot_crash_or_qualify(evidence, duration):
    _, put, save = evidence
    put('soak', dict(evidence_type='real_http_isolated_product_benchmark',
        options=dict(profile='soak', duration_seconds=duration), graceful_shutdown=True,
        gates={key: dict(state='passed') for key in ('telemetry_health', 'stability_30_minutes')}))
    assert release.assess(save())['ready'] is False


def test_demo_cli_loopback_only(monkeypatch):
    import uvicorn
    run = Mock(); monkeypatch.setattr(uvicorn, 'run', run)
    assert cli.main(['demo']) == 0
    assert isinstance(run.call_args.args[0], DemoAPI)
    assert run.call_args.kwargs['port'] == 3000
    assert cli.main(['demo', '--listen', '0.0.0.0:3000']) == 2
    assert run.call_count == 1


def test_demo_disposable_mutation():
    import asyncio
    async def check():
        app = DemoAPI()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://demo') as client:
            assert (await client.get('/api/products')).headers['cache-control'] == 'public, max-age=60'
            response = await client.patch('/api/products/1', json={'price_cents': 3500})
            assert response.status_code == 200
            assert (await client.get('/api/reports/catalog')).json()['price_cents'] == 3500
            assert (await client.patch('/api/products/1', json={'price_cents': -1})).status_code == 422
        assert DemoAPI().product['price_cents'] == 2500
    asyncio.run(check())
