"""Real HTTP benchmark; no sleeps or simulated latency in application handlers."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tuneio.database import Database


def seed(path, orders):
    db = Database(path)
    with db.connect() as conn:
        conn.execute("INSERT INTO users VALUES(1,'Benchmark user','bench@example.com')")
        conn.executemany('INSERT INTO products VALUES(?,?,?)', [(i, f'Product {i}', i * 100) for i in range(1, 101)])
        conn.executemany('INSERT INTO orders(user_id,product_id,quantity,unit_price_cents,status) VALUES(?,?,?,?,?)',
                         [(1, i % 100 + 1, i % 5 + 1, (i % 100 + 1) * 100, 'paid') for i in range(orders)])


async def load(base, requests, concurrency, expected, transport=None):
    durations, hashes, statuses = [], [], []
    hits = 0
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=base, timeout=60, trust_env=False, transport=transport) as client:
        async def one():
            nonlocal hits
            async with semaphore:
                started = time.perf_counter()
                response = await client.get('/api/reports/sales')
                durations.append((time.perf_counter() - started) * 1000)
                statuses.append(response.status_code)
                hashes.append(hashlib.sha256(response.content).hexdigest())
                hits += response.headers.get('x-cache') == 'HIT'
        start = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(requests)))
        elapsed = time.perf_counter() - start
        analysis = (await client.get('/analysis')).json()
        metrics = (await client.get('/metrics')).json()
    if any(s != 200 for s in statuses) or any(h != expected for h in hashes):
        raise RuntimeError('Benchmark correctness/status check failed')
    if metrics['summary']['count'] != requests + 1 or metrics['summary']['cache_hits'] != hits:
        raise RuntimeError('Benchmark monitoring reconciliation failed')
    return dict(mean_ms=statistics.mean(durations), p95_ms=sorted(durations)[int(.95 * (len(durations) - 1))],
                throughput_rps=requests / elapsed, elapsed_seconds=elapsed, errors=0, cache_hits=hits,
                response_sha256=expected, latencies_ms=durations,
                monitored_requests=metrics['summary']['count'], decisions=analysis['cache']['decisions'],
                origin_performance=analysis['cache'].get('performance', {}))


def run_round(db_path, mode, args):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, AUTOTUNER_DB=str(db_path))
    command = [sys.executable, '-m', 'uvicorn', 'tuneio.main:create_app', '--factory', '--host', '127.0.0.1',
               '--port', str(port), '--log-level', 'error', '--no-access-log']
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    base = f'http://127.0.0.1:{port}'
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(base + '/health', timeout=.5):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Server failed to start')
                time.sleep(.05)
        with httpx.Client(base_url=base, trust_env=False) as client:
            # Same single origin request warms SQLite/OS pages in both modes.
            expected = hashlib.sha256(client.get('/api/reports/sales').content).hexdigest()
            client.put('/cache/mode', json={'mode': mode}).raise_for_status()
        return asyncio.run(load(base, args.requests, args.concurrency, expected))
    finally:
        process.terminate()
        process.communicate(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--orders', type=int, default=50000)
    parser.add_argument('--requests', type=int, default=120)
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, default=ROOT / 'benchmarks/latest.json')
    args = parser.parse_args()
    if min(args.orders, args.requests, args.concurrency, args.repeats) < 1:
        parser.error('All workload sizes must be positive')
    rounds = []
    with tempfile.TemporaryDirectory(prefix='tuneio-bench-') as temp:
        db_path = Path(temp) / 'bench.sqlite3'
        seed(db_path, args.orders)
        for repeat in range(args.repeats):
            results = {}
            for mode in (('off', 'adaptive') if repeat % 2 == 0 else ('adaptive', 'off')):
                results[mode] = run_round(db_path, mode, args)
                print(f"Round {repeat + 1} {mode}: {results[mode]['mean_ms']:.2f} ms", flush=True)
            results['improvement_percent'] = (1 - results['adaptive']['mean_ms'] / results['off']['mean_ms']) * 100
            rounds.append(results)
    improvement = statistics.median(r['improvement_percent'] for r in rounds)
    if len({r[mode]['response_sha256'] for r in rounds for mode in ('off', 'adaptive')}) != 1:
        raise RuntimeError('Baseline and adaptive response bodies differ')
    result = dict(timestamp_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  source_sha256={str(p.relative_to(ROOT / 'tuneio')).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted((ROOT / 'tuneio').rglob('*.py'))},
                  environment=dict(python=platform.python_version(), os=platform.platform(),
                                   fastapi=__import__('fastapi').__version__, httpx=httpx.__version__,
                                   uvicorn=__import__('uvicorn').__version__),
                  workload=dict(orders=args.orders, products=100, requests_per_mode=args.requests,
                                concurrency=args.concurrency, repeats=args.repeats,
                                endpoint='GET /api/reports/sales', workers=1, warmup_requests=1),
                  median_improvement_percent=improvement, meets_20_percent=improvement >= 20, rounds=rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f'Median mean-latency improvement: {improvement:.2f}% -> {args.output}', flush=True)
    if improvement < 20:
        raise SystemExit('Measured improvement below 20%; inspect workload and results')


if __name__ == '__main__':
    main()
