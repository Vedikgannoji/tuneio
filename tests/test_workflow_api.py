"""Run the real JavaScript scenario against the real ASGI application over pipes."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required for browser workflow integration')
def test_browser_workflow_against_real_backend(app):
    script = r"""
const {run}=require(process.argv[1]);
const rl=require('node:readline').createInterface({input:process.stdin});
let answer;
rl.on('line',line=>answer(JSON.parse(line)));
run({reads:6,delayMs:0},(method,path,body)=>new Promise(resolve=>{
  answer=resolve; process.stdout.write(JSON.stringify({type:'request',method,path,body})+'\n');
})).then(report=>{process.stdout.write(JSON.stringify({type:'report',report})+'\n');rl.close();})
.catch(error=>{console.error(error);process.exitCode=1;rl.close();});
"""
    process = subprocess.Popen(['node', '-e', script, str(Path('tuneio/workflow.js').resolve())],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8')
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            while True:
                line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10)
                assert line, 'Workflow process exited without a report'
                message = json.loads(line)
                if message['type'] == 'report':
                    return message['report']
                options = {'json': message['body']} if message.get('body') is not None else {}
                response = await client.request(message['method'], message['path'], **options)
                process.stdin.write(json.dumps(dict(status=response.status_code, data=response.json(),
                                                   cache=response.headers.get('x-cache'))) + '\n')
                process.stdin.flush()
    try:
        report = asyncio.run(scenario())
        assert report['state'] == 'completed', report
        assert len(report['results']) == 34
        assert all(row['passed'] for row in report['results'])
        assert sum(row['cache'] == 'HIT' for row in report['results']) > 0
        assert len(app.state.metrics.snapshot()['routes']) == 13
        order = app.state.db.get('orders', report['contexts'][0]['order']['id'])
        assert order['status'] == 'shipped' and order['unit_price_cents'] == 2000
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
