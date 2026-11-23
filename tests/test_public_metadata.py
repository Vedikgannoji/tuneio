"""Public interfaces describe product capabilities, not internal development milestones."""
import asyncio
import json
import re
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from tuneio import __version__, cli
from tuneio.product_config import ProductConfig
from tuneio.session import ControlSession


def test_session_metadata_is_user_facing(tmp_path):
    async def run():
        session=ControlSession(ProductConfig(upstream='http://fixture',metrics={'storage_path':str(tmp_path/'metrics.sqlite3')}))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=session),base_url='http://local') as client:
                for endpoint in ('status','health','insights'):
                    response=await client.get('/_autotuner/'+endpoint)
                    assert response.status_code==200
                    data=response.json()
                    assert data['tool_version']==__version__ and 'phase' not in data
                status=(await client.get('/_autotuner/status')).json()
                assert {'proxy','dashboard','scenarios','insights','cache','reports'}<=set(status['capabilities'])
                dashboard=(await client.get('/_autotuner/dashboard/data')).json()
                assert 'phase' not in dashboard['status'] and 'dashboard' in dashboard['status']['capabilities']
        finally:
            await session.proxy.close()
            await session.storage.close()
    asyncio.run(run())


@pytest.mark.parametrize('capabilities', [None, 'dashboard', [1], [], ['proxy']])
def test_advertised_capabilities_take_precedence_over_legacy(capabilities):
    assert not cli.supports({'capabilities':capabilities,'phase':11},'dashboard')


def test_dashboard_uses_capabilities_without_milestones(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(cli,'resolved_config',lambda _:ProductConfig(upstream='http://fixture'))
    control=Mock(return_value=dict(schema_version=1,running=True,session_id='demo',tool_version=__version__,capabilities=['proxy','dashboard']))
    monkeypatch.setattr(cli,'local_json',control)
    assert cli.main(['dashboard','--json'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['url'].endswith('/_autotuner/dashboard') and 'phase' not in result
    control.return_value['capabilities']=['proxy']
    assert cli.main(['dashboard','--json'])==1


@pytest.mark.parametrize('filename', ['README.md','README_DEMO.md','CHANGELOG.md','CONTRIBUTING.md','docs/release/v2.0.0b2.md','docs/TERMINAL_GUIDE.md'])
def test_public_documentation_keeps_internal_milestones_out(filename):
    text=Path(filename).read_text(encoding='utf-8')
    assert not re.search(r'\bphases?\b',text,re.I)
    assert 'docs/product/PHASE_' not in text
