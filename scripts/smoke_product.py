"""Run the installed CLI against owned loopback fixtures, outside the checkout."""
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory

import httpx

from autotuner.artifacts import write_new
from autotuner.benchmarking import Child
from autotuner.qualification_fixture import seed


@contextmanager
def server(kind, path, port=0):
    child = Child(kind, path, port=port)
    try:
        yield child.url
    finally:
        child.close()
    assert child.graceful, f'{kind} did not shut down gracefully'


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def verify(root, fixture, route, method, mutation, payload):
    def run(*args):
        command = [sys.executable, '-I', '-m', 'autotuner', '--no-input', *map(str, args), '--json']
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return json.loads(result.stdout)

    def reviewed(*args):
        preview = run(*args)
        assert preview['written'] is False
        outcome = run(*args, '--apply', preview['review_token'])
        assert outcome['written'] is True

    port = free_port()
    source = root/'fixture.db'
    if fixture == 'fixture':
        seed(source, 2000)
    write_new(root/'openapi.json', {'openapi':'3.1.0','paths':{route:{'get':{}},mutation:{method.lower():{}}}})
    inventory = run('discover', '--project', root)
    selections = [part for row in inventory['operations'] for part in ('--select', row['id'])]
    with server(fixture, source) as upstream:
        reviewed('setup','--project',root,'--upstream',upstream,'--listen',f'127.0.0.1:{port}',
                 '--output','observe.json',*selections)
        raw = json.loads((root/'observe.json').read_text())
        # Demo-only floor. CI checks the flow and correctness without host timing gates.
        raw['tuning'] = dict(enable_ms=.000001, retain_ms=.000001)
        write_new(root/'before-config.json', raw)
        capture = ['--get',route,'--environment','local','--requests',4,'--warmup',5,
                   '--app-state',f'{fixture}-seed-v1']
        with server('proxy', root/'before-config.json', port):
            reviewed('baseline','--config','before-config.json','--output','before.json',*capture)
        reviewed('optimize','enable','--config','before-config.json','--output','adaptive.json',
                 '--route',route,'--baseline','before.json')
        with server('proxy', root/'adaptive.json', port) as url:
            reviewed('baseline','--config','adaptive.json','--stage','optimized','--output','after.json',*capture)
            comparison = run('compare','before.json','after.json','--output','comparison.json')
            assert comparison['comparable'] and comparison['optimized']['hits'] >= 1
            with httpx.Client(base_url=url, trust_env=False, timeout=5) as client:
                client.headers.pop('Accept')
                client.headers.pop('Accept-Encoding')
                before = client.get(route).content
                assert client.get(route, headers={'Authorization':'Bearer fixture'}).headers['x-autotuner-cache-reason']=='credentials'
                assert client.request(method, mutation, json=payload).status_code==200
                fresh = client.get(route)
                assert fresh.content != before and fresh.headers['x-autotuner-cache'] != 'HIT'
            monitor = run('monitor','--config','adaptive.json','--once','--output','monitor.json')
            assert monitor['decisions'] and monitor['status']['running']
        assert run('sessions','--config','adaptive.json')['sessions']
        reviewed('optimize','rollback','--config','adaptive.json','--output','restored.json')
        with server('proxy', root/'restored.json', port) as url, httpx.Client(base_url=url,trust_env=False,timeout=5) as client:
            assert client.get(route).headers['x-autotuner-cache-reason']=='mode_observe'


if __name__ == '__main__':
    with TemporaryDirectory(prefix='autotuner-product-smoke-', dir=Path.cwd()) as tmp:
        root = Path(tmp)
        for name, path, method, mutation, body in (
                ('demo','/api/products','PATCH','/api/products/1',{'price_cents':7300}),
                ('fixture','/report','POST','/mutate',None)):
            project = root/name
            project.mkdir()
            verify(project, name, path, method, mutation, body)
            print(f'{name} installed CLI journey passed')
