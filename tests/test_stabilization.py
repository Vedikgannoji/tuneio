"""No scenario writes should occur when its output cannot be reserved."""
import json
from unittest.mock import Mock

import pytest

from tuneio import cli
from tuneio.product_config import ProductConfig


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    path = tmp_path / 'scenario.json'
    path.write_text(json.dumps(dict(name='Mutation',steps=[dict(name='Write',method='POST',path='/items',body={'name':'demo'})])))
    monkeypatch.setattr(cli,'resolved_config',lambda args:ProductConfig(upstream='http://fixture'))
    return path


@pytest.mark.parametrize('kind', ['exists', 'parent_missing', 'directory'])
def test_report_preflight_prevents_mutations(scenario, tmp_path, monkeypatch, kind):
    output = tmp_path / 'report.json'
    if kind == 'exists': output.write_text('retained report')
    elif kind == 'directory': output.mkdir()
    else: output = tmp_path / 'missing' / 'report.json'
    network=Mock(side_effect=AssertionError('No scenario must be submitted'))
    monkeypatch.setattr(cli,'local_json',network)
    assert cli.main(['scenario','run',str(scenario),'--allow-writes','--output',str(output)]) in (1,2)
    network.assert_not_called()
    if kind == 'exists': assert output.read_text() == 'retained report'


@pytest.mark.parametrize('force', [False, True])
def test_reserved_report_records_completion(scenario, tmp_path, monkeypatch, force):
    output = tmp_path / 'report.json'
    if force: output.write_text('old report much longer than new')
    def execute(config, method, path, payload):
        assert json.loads(output.read_text())['state'] == 'incomplete'
        return dict(schema_version=1,run_id=payload['run_id'],state='completed')
    monkeypatch.setattr(cli,'local_json',execute)
    assert cli.main(['scenario','run',str(scenario),'--allow-writes','--output',str(output)] + (['--force'] if force else [])) == 0
    assert json.loads(output.read_text())['state'] == 'completed'


def test_uncertain_execution_retains_marker(scenario, tmp_path, monkeypatch):
    output=tmp_path/'report.json'
    monkeypatch.setattr(cli,'local_json',Mock(side_effect=cli.ControlError('Connection lost')))
    assert cli.main(['scenario','run',str(scenario),'--allow-writes','--output',str(output)]) == 1
    marker=json.loads(output.read_text())
    assert marker['state']=='incomplete' and 'before retrying' in marker['note']
    import uuid
    assert str(uuid.UUID(marker['run_id'])) == marker['run_id']
