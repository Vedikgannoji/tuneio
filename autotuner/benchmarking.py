"""Explicit isolated qualification runs. ASGI transports are test-only, never evidence."""
import asyncio
from collections import Counter,deque
from datetime import datetime,timezone
import hashlib
from importlib.metadata import version
import io
import csv
import json
import os
from pathlib import Path
import platform
from queue import Queue,Empty
import statistics
import subprocess
import sys
import tempfile
import threading
from time import perf_counter,get_clock_info

import httpx
from pydantic import Field,model_validator
from typing import Annotated,Literal

from .core.metrics import percentile
from .product_config import Contract,ProductConfig
from .qualification_fixture import seed,reference,encode,PAYLOAD_BYTES
from ._version import __version__


MODES=('direct','observe','off','adaptive')
WORKLOADS=('overhead','repeated','unique','mixed')


class Options(Contract):
    profile: Literal['quick','qualification','soak']='quick'
    requests: Annotated[int,Field(strict=True,ge=20,le=100000)]=40
    concurrency: Annotated[int,Field(strict=True,ge=1,le=64)]=8
    repeats: Annotated[int,Field(strict=True,ge=3,le=10)]=3
    orders: Annotated[int,Field(strict=True,ge=8,le=1000000)]=2000
    rate: Annotated[int,Field(strict=True,ge=1,le=1000)]=100
    duration_seconds: Annotated[int,Field(strict=True,ge=1,le=3600)]=60
    reference_host: bool=False

    @model_validator(mode='after')
    def bounded_run(self):
        if self.requests/self.rate>(60 if self.profile=='soak' else 600):
            raise ValueError('Paced workload exceeds the per-batch duration budget')
        if self.profile!='soak' and self.requests*self.repeats*len(MODES)*len(WORKLOADS)>200000:
            raise ValueError('Raw report exceeds the total observation budget')
        return self


def digest(content):return hashlib.sha256(content).hexdigest()


def source_hashes():
    root=Path(__file__).parent
    return {p.relative_to(root).as_posix():digest(p.read_bytes()) for p in sorted(root.rglob('*'))
            if p.is_file() and p.suffix in ('.py','.js','.html','.css','.json')}


def environment():
    result=dict(python=platform.python_version(),os=platform.platform(),machine=platform.machine(),
                logical_cpus=os.cpu_count(),processor=platform.processor(),tool_version=__version__,
                dependencies={name:version(name) for name in ('httpx','uvicorn','pydantic')},physical_cpus=None,ram_bytes=None)
    result['clocks']={name:dict(implementation=get_clock_info(name).implementation,resolution_seconds=get_clock_info(name).resolution)
                      for name in ('monotonic','perf_counter')}
    try:
        import psutil
        result.update(physical_cpus=psutil.cpu_count(logical=False),ram_bytes=psutil.virtual_memory().total)
    except ImportError:pass
    return result


def rss(pid):
    try:
        import psutil
    except ImportError:return None
    try:
        return psutil.Process(pid).memory_info().rss
    except (psutil.Error,OSError):return None


def config_for(mode,upstream,storage):
    return ProductConfig(upstream=upstream,mode=mode,metrics={'storage_path':str(storage)},
        operations=[{'method':'GET','path':'/report'},{'method':'GET','path':'/payload'},
                    {'method':'POST','path':'/mutate'},{'method':'GET','path':'/fault'}],
        cache={'routes':[{'id':name,'match':'/'+name,'policy':'adaptive','vary_headers':['Accept-Encoding'],
            'invalidate_on':[{'method':'POST','path':'/mutate'}]} for name in ('report','payload')]})


def canonical_config(config):
    data=config.model_dump()
    data['upstream']='http://{owned_fixture}'
    data['metrics']['storage_path']='{temporary_storage}'
    return data


class Child:
    """Only owned subprocesses, OS-assigned ports, stdin-driven graceful shutdown."""
    def __init__(self,kind,path,*,factory=subprocess.Popen,start_timeout=30,port=0):
        self.graceful=False
        self.process=factory([sys.executable,'-m','autotuner.qualification_server','--kind',kind,'--path',str(path),'--port',str(port)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        ready=Queue(maxsize=1)
        def read():
            try:ready.put(self.process.stdout.readline())
            except (OSError,ValueError):ready.put('')
        threading.Thread(target=read,daemon=True).start()
        try:
            message=json.loads(ready.get(timeout=start_timeout))
            if message.get('schema_version')!=1 or type(message.get('port')) is not int or not 1<=message['port']<=65535:
                raise ValueError('Invalid readiness response')
            self.url='http://127.0.0.1:'+str(message['port'])
        except (Empty,ValueError,TypeError,AttributeError):
            self.close();raise RuntimeError('Isolated benchmark server failed to start; check local socket permissions and package installation') from None
        except KeyboardInterrupt:
            self.close()
            raise

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write('stop\n');self.process.stdin.flush()
                    self.process.wait(timeout=15);self.graceful=self.process.returncode==0
                except (OSError,ValueError,subprocess.TimeoutExpired):
                    self.process.kill();self.process.wait(timeout=5)
            else:self.graceful=self.process.returncode==0
        finally:
            for stream in (self.process.stdin,self.process.stdout):stream.close()


def summarize(records,elapsed):
    latencies=[row['latency_ms'] for row in records]
    failed=[r['index'] for r in records if not r['correct']]
    return dict(requests=len(records),elapsed_seconds=elapsed,throughput_rps=len(records)/elapsed if elapsed else None,
        mean_ms=statistics.mean(latencies) if latencies else None,p50_ms=percentile(latencies,.5),p95_ms=percentile(latencies,.95),
        errors=sum(r['status'] is None or r['status']>=400 for r in records),unexpected_failures=len(failed),
        failed_indexes=failed[:100],cache_outcomes=dict(Counter(r['cache'] for r in records)),
        scheduling_lag_p95_ms=percentile([r['scheduling_lag_ms'] for r in records],.95),samples=records)


async def load(base,options,workload,*,transport=None,start_version=0):
    """Bounded workers, paced dispatch, epoch barriers around explicit mutations."""
    if workload not in (*WORKLOADS,'soak'):raise ValueError('Unknown workload')
    records=[];setup_started=perf_counter();ticket=0;state_version=start_version
    semaphore=asyncio.Semaphore(options.concurrency)
    async with httpx.AsyncClient(base_url=base,transport=transport,trust_env=False,follow_redirects=False,timeout=60,
        headers={'accept-encoding':'identity'},limits=httpx.Limits(max_connections=options.concurrency,
        max_keepalive_connections=options.concurrency)) as client:
        client_setup_ms=(perf_counter()-setup_started)*1000
        started=perf_counter()
        async def one(method,path,expected,expected_status=200):
            nonlocal ticket
            index=ticket;ticket+=1
            scheduled=started+index/options.rate
            await pace(scheduled)
            active=perf_counter();status=None;actual=None;cache='UNAVAILABLE'
            try:
                async with semaphore:
                    response=await client.request(method,path)
                    status=response.status_code;actual=digest(response.content)
                    cache=response.headers.get('x-autotuner-cache','DIRECT')
            except httpx.HTTPError:pass  # No retry. Preserve the unsuccessful observation.
            records.append(dict(index=index,method=method,path_template=path.split('?',1)[0],status=status,
                expected_status=expected_status,response_sha256=actual,expected_sha256=digest(expected),
                correct=status==expected_status and actual==digest(expected),cache=cache,
                latency_ms=(perf_counter()-active)*1000,scheduling_lag_ms=max(0,active-scheduled)*1000))
        async def reads(count,offset,expected):
            counter=0
            async def worker():
                nonlocal counter
                while counter<count:
                    item=counter;counter+=1
                    unique=workload=='unique' or workload=='soak' and item%2==1
                    path='/payload' if workload=='overhead' else '/report'+('?key='+str(start_version*options.requests+offset+item) if unique else '')
                    await one('GET',path,expected)
            await asyncio.gather(*(worker() for _ in range(min(count,options.concurrency))))
        while ticket<options.requests:
            expected=encode({'message':'Reference payload'}) if workload=='overhead' else reference(options.orders,state_version)
            remaining=options.requests-ticket
            batch=min(19,remaining) if workload in ('mixed','soak') else remaining
            await reads(batch,ticket,expected)
            if workload=='soak' and ticket<options.requests:
                await one('GET','/fault',encode({'detail':'Injected fixture failure'}),503)
            if workload in ('mixed','soak') and ticket<options.requests:
                state_version+=1
                await one('POST','/mutate',encode({'version':state_version}))
        elapsed=perf_counter()-started
        close_started=perf_counter()
    result=summarize(sorted(records,key=lambda r:r['index']),elapsed)
    result.update(client_setup_ms=client_setup_ms,client_close_ms=(perf_counter()-close_started)*1000,
                  duration_boundary='First scheduled dispatch through final response; client setup/close excluded')
    result['final_version']=state_version
    return result


async def pace(deadline,*,clock=None,sleep=None):
    """Recheck absolute dispatch time when an event-loop timer fires early."""
    clock=clock or perf_counter
    sleep=sleep or asyncio.sleep
    while (remaining:=deadline-clock())>0:
        await sleep(remaining)


async def control(client,path):
    try:
        # Only read-only diagnostic exports retry a busy writer, outside timed traffic.
        for attempt in range(20):
            response=await client.get(path)
            if response.status_code!=503 or attempt==19:break
            await asyncio.sleep(.1)
        response.raise_for_status();return response.json()
    except (httpx.HTTPError,ValueError):raise RuntimeError('Benchmark control response failed validation') from None


async def completion_status(client,expected,*,clock=None,timeout=5,max_polls=100):
    """Observe post-response cleanup outside timed load; never accept an unsettled snapshot."""
    clock=clock or perf_counter
    started=clock();deadline=started+timeout;status=None;polls=0
    for _ in range(max_polls):
        remaining=deadline-clock()
        if remaining<=0:break
        try:status=await asyncio.wait_for(control(client,'/_autotuner/status'),remaining)
        except asyncio.TimeoutError:break
        polls+=1
        if (status['transport']['completed_requests']==expected and status['runtime']['in_flight']==0
                and status['runtime'].get('hot_reads',0)==0 and status['cache']['coordination_keys']==0):
            return status,dict(state='settled',polls=polls,wait_ms=max(0,clock()-started)*1000)
        await asyncio.sleep(0)  # Scheduling yield only; no time-based settling assumption.
    if status is None:raise RuntimeError('Benchmark completion status timed out')
    return status,dict(state='unsettled',polls=polls,wait_ms=max(0,clock()-started)*1000)


async def measure(base,origin,options,workload,*,proxy=None,fixture=None,transport=None,origin_transport=None):
    """Warm SQL outside the proxy, leaving policy learning inside measured costs."""
    async with httpx.AsyncClient(base_url=origin,transport=origin_transport,trust_env=False,timeout=60) as origin_client:
        warm=await origin_client.get('/report')
        if warm.status_code!=200 or warm.content!=reference(options.orders):raise RuntimeError('Fixture warmup correctness failed')
        before=await control(origin_client,'/stats')
        idle=rss(proxy.process.pid) if proxy else None
        peak=idle
        stop=asyncio.Event()
        async def sample():
            nonlocal peak
            while not stop.is_set():
                if proxy:
                    value=rss(proxy.process.pid)
                    if value is not None:peak=value if peak is None else max(peak,value)
                try:await asyncio.wait_for(stop.wait(),.2)
                except asyncio.TimeoutError:pass
        sampler=asyncio.create_task(sample())
        try:result=await load(base,options,workload,transport=transport)
        finally:stop.set();await sampler
        after=await control(origin_client,'/stats')
    result.update(origin_loads={key:after[key]-before[key] for key in ('reads','writes','failures')},idle_proxy_rss_bytes=idle,
                  peak_proxy_rss_bytes=peak)
    if proxy:
        async with httpx.AsyncClient(base_url=base,trust_env=False,timeout=60,transport=transport) as client:
            status,settlement=await completion_status(client,result['requests'])
            persisted=await control(client,'/_autotuner/report')
        result.update(cache=status['cache'],transport=status['transport'],storage=persisted['sampling'],runtime=status['runtime'],settlement=settlement)
        result['monitoring_reconciled']=status['transport']['completed_requests']==result['requests']
    else:result['monitoring_reconciled']=True
    result['correctness_passed']=not result['unexpected_failures'] and result['monitoring_reconciled'] and (not proxy or settlement['state']=='settled')
    return result


def comparisons(rounds,options,host):
    correct=all(r['correctness_passed'] for row in rounds for r in row['modes'].values())
    benefit=[];median_added=[];tail_added=[];overhead_rates=[]
    for round_ in rounds:
        if round_['workload']=='repeated':
            off,adaptive=(round_['modes'][key]['mean_ms'] for key in ('off','adaptive'))
            benefit.append((1-adaptive/off)*100 if off else None)
        if round_['workload']=='overhead':
            direct,observe=(round_['modes'][key] for key in ('direct','observe'))
            median_added.append(observe['p50_ms']-direct['p50_ms']);tail_added.append(observe['p95_ms']-direct['p95_ms'])
            overhead_rates.extend([direct['throughput_rps'],observe['throughput_rps']])
    rate_valid=options.rate==100 and bool(overhead_rates) and all(r is not None and 95<=r<=105 for r in overhead_rates)
    def gate(values,test,*,needs_rate=False):
        if not correct:return dict(state='blocked_by_correctness',values=[])
        if needs_rate and not rate_valid:
            return dict(state='invalid_workload',values=values,threshold_passed=bool(values) and test(values),
                        reason='Paired overhead cohorts must all achieve 95–105 requests/s at configured 100/s; no cohorts were discarded')
        return dict(state='not_measured' if not values or any(v is None for v in values) else 'passed' if test(values) else 'failed',values=values)
    return dict(correctness=dict(state='passed' if correct else 'failed'),
        telemetry_health=dict(state='passed' if all(r.get('storage',{}).get('persistent') and
            r['storage'].get('state') in ('ready','closed') and not r['storage'].get('write_errors')
            for row in rounds for mode,r in row['modes'].items() if mode!='direct') else 'failed'),
        shutdown=dict(state='passed' if all(r['graceful_shutdown'] for row in rounds for r in row['modes'].values()) else 'failed'),
        repeated_read_benefit=gate(benefit,lambda v:statistics.median(v)>=20),
        observe_median_overhead=gate(median_added,lambda v:statistics.median(v)<=3,needs_rate=True),
        observe_p95_overhead=gate(tail_added,lambda v:statistics.median(v)<=8,needs_rate=True),
        offered_rate=gate(overhead_rates,lambda v:options.rate==100 and all(95<=r<=105 for r in v)),
        reference_environment=dict(state='confirmed' if options.reference_host and host['python'].startswith('3.12.') and host['physical_cpus']==4 and host['ram_bytes'] is not None and 7*1024**3<=host['ram_bytes']<=9*1024**3 and options.concurrency==8 else 'not_confirmed'),
        idle_memory=gate([r['idle_proxy_rss_bytes'] for row in rounds for mode,r in row['modes'].items() if mode!='direct'],lambda v:max(v)<=150*1024**2),
        cache_budget=dict(state='passed' if all(r['cache']['peak_accounted_bytes']<=r['cache']['byte_budget'] for row in rounds for mode,r in row['modes'].items() if mode!='direct') else 'failed'),
        stability_30_minutes=dict(state='not_measured'),compatibility_matrix=dict(state='pending_hosted_ci'))


def report_csv(report):
    stream=io.StringIO(newline='');fields=['schema_version','source_digest','profile','round','workload','mode','requests',
        'mean_ms','p50_ms','p95_ms','throughput_rps','errors','unexpected_failures','correctness_passed','idle_proxy_rss_bytes','peak_proxy_rss_bytes',
        'config_sha256','gate_states','qualification_status']
    writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
    gates=json.dumps({key:value['state'] for key,value in report['gates'].items()},sort_keys=True)
    for row in report['rounds']:
        for mode,result in row['modes'].items():
            writer.writerow(dict(schema_version=1,source_digest=report['source_digest'],profile=report['options']['profile'],
                round=row['round'],workload=row['workload'],mode=mode,**{key:result.get(key) for key in fields[6:-2]},
                gate_states=gates,qualification_status=report.get('qualification_status','review_required')))
    if 'soak' in report:
        result=report['soak']
        writer.writerow(dict(schema_version=1,source_digest=report['source_digest'],profile='soak',round=1,workload='soak',
            mode='adaptive',requests=result['requests'],unexpected_failures=result['unexpected_failures'],
            correctness_passed=result['unexpected_failures']==0,config_sha256=report['soak_config_sha256'],
            gate_states=gates,qualification_status=report['qualification_status']))
    return stream.getvalue()


def run(options,*,progress=lambda message:None,child_factory=Child,measure_fn=measure):
    host=environment();sources=source_hashes()
    report=dict(schema_version=1,evidence_type='real_http_isolated_product_benchmark',
        measurement_revision=2,
        generated_at=datetime.now(timezone.utc).isoformat(),options=options.model_dump(),environment=host,
        source_sha256=sources,source_digest=digest(json.dumps(sources,sort_keys=True).encode()),rounds=[],
        qualification_status='review_required',warmup='One direct /report request per cohort; cold proxy/policy start included',
        payload_bytes=PAYLOAD_BYTES,workers=1,traffic_scope='Owned temporary fixture only; no user upstream, replay or database',
        listener_override='Private server uses an OS-assigned loopback port; config.listen is not bound',
        limitations=['No general API speedup claim','Hosted CI and 30-minute stability require separate evidence'])
    if options.profile=='soak':return run_soak(options,report,progress=progress,child_factory=child_factory)
    with tempfile.TemporaryDirectory(prefix='autotuner-qualification-') as temp:
        root=Path(temp)
        for repeat in range(options.repeats):
            for workload in WORKLOADS:
                row=dict(round=repeat+1,workload=workload,order=list(MODES if repeat%2==0 else reversed(MODES)),modes={})
                report['rounds'].append(row)
                for mode in row['order']:
                    origin=proxy=None
                    folder=root/f'{repeat}-{workload}-{mode}';folder.mkdir()
                    database=folder/'fixture.sqlite3';seed(database,options.orders)
                    try:
                        origin=child_factory('fixture',database)
                        config=config_for(mode if mode!='direct' else 'off',origin.url,folder/'telemetry.sqlite3')
                        config_path=folder/'config.json';config_path.write_text(config.model_dump_json(),encoding='utf-8')
                        canonical=canonical_config(config)
                        config_digest=digest(json.dumps(canonical,sort_keys=True).encode())
                        if mode!='direct':proxy=child_factory('proxy',config_path)
                        progress(f'Round {repeat+1}: {workload} / {mode}')
                        result=asyncio.run(measure_fn(proxy.url if proxy else origin.url,origin.url,options,workload,proxy=proxy,fixture=origin))
                        result['config_sha256']=config_digest
                        # No ephemeral path/port or sensitive response payload is retained.
                        result['effective_config']=canonical
                        row['modes'][mode]=result
                    except httpx.HTTPError:raise RuntimeError('Isolated benchmark HTTP connection failed') from None
                    finally:
                        if proxy:proxy.close()
                        if origin:origin.close()
                    result['graceful_shutdown']=bool(origin.graceful and (proxy is None or proxy.graceful))
    report['gates']=comparisons(report['rounds'],options,host)
    return report


def run_soak(options,report,*,progress,child_factory):
    """Sustained bounded checkpoints, mutations and expected faults; no comparison claim."""
    async def sustain(proxy,origin):
        checkpoints=deque(maxlen=3601);checkpoint_count=0;total=unexpected=expected_errors=0;state_version=0;started=perf_counter()
        async with httpx.AsyncClient(base_url=proxy.url,trust_env=False,timeout=60) as client:
            while perf_counter()-started<options.duration_seconds:
                result=await load(proxy.url,options,'soak',start_version=state_version)
                state_version=result['final_version'];total+=result['requests'];unexpected+=result['unexpected_failures']
                expected_errors+=result['errors']
                status,settlement=await completion_status(client,total)
                checkpoints.append(dict(elapsed_seconds=perf_counter()-started,requests=total,unexpected_failures=unexpected,rss_bytes=rss(proxy.process.pid),
                    cache=status['cache'],runtime=status['runtime'],storage=status['storage'],transport=status['transport'],settlement=settlement))
                checkpoint_count+=1
                progress(f'Soak: {int(perf_counter()-started)} s, {total} requests, {unexpected} unexpected failures')
            persisted=await control(client,'/_autotuner/report')
        return dict(elapsed_seconds=perf_counter()-started,requests=total,unexpected_failures=unexpected,http_errors=expected_errors,
                    checkpoints=list(checkpoints),checkpoint_count=checkpoint_count,checkpoints_truncated=checkpoint_count>len(checkpoints),
                    persisted_sampling=persisted['sampling'])
    with tempfile.TemporaryDirectory(prefix='autotuner-soak-') as temp:
        root=Path(temp);database=root/'fixture.sqlite3';seed(database,options.orders)
        origin=proxy=None
        try:
            origin=child_factory('fixture',database)
            config=config_for('adaptive',origin.url,root/'telemetry.sqlite3')
            config.limits.cache_bytes=65536;config.limits.cache_entry_bytes=8192;config.limits.cache_entries=16
            config.metrics.raw_capacity=128;config.metrics.writer_queue_capacity=32;config.metrics.storage_max_bytes=131072
            report.update(soak_effective_config=canonical_config(config),
                soak_config_sha256=digest(json.dumps(canonical_config(config),sort_keys=True).encode()),
                warmup='None; cold start included in soak')
            path=root/'config.json';path.write_text(config.model_dump_json(),encoding='utf-8')
            proxy=child_factory('proxy',path)
            result=asyncio.run(sustain(proxy,origin))
        except httpx.HTTPError:raise RuntimeError('Isolated soak HTTP connection failed') from None
        finally:
            if proxy:proxy.close()
            if origin:origin.close()
    points=result['checkpoints'];late=points[len(points)//2:]
    values=[p['rss_bytes'] for p in late]
    growth=max(values)-values[0] if values and all(v is not None for v in values) else None
    settled=all(p['settlement']['state']=='settled' and p['runtime']['in_flight']==0 and p['cache']['coordination_keys']==0 and
        p['cache']['peak_accounted_bytes']<=p['cache']['byte_budget'] for p in points)
    task_growth=max(p['runtime']['active_tasks'] for p in late)-late[0]['runtime']['active_tasks'] if late else None
    observed_mixture=bool(points and points[-1]['cache']['hits'] and points[-1]['cache']['misses'] and
                          points[-1]['cache']['invalidations'] and result['http_errors'])
    reconciled=bool(points and points[-1]['transport']['completed_requests']==result['requests'])
    health=result['persisted_sampling']
    persistence_ok=bool(health.get('persistent') and health.get('state') in ('ready','closed') and not health.get('write_errors'))
    report.update(soak=result,gates=dict(telemetry_health=dict(state='passed' if persistence_ok else 'failed',
        storage_state=health.get('state'),write_errors=health.get('write_errors'),
        note='Retention/queue truncation remains disclosed; unrecovered writer faults cannot qualify persistence.'),stability_30_minutes=dict(
        state='failed' if result['unexpected_failures'] or not settled or not reconciled or not proxy.graceful or not origin.graceful else
            'not_qualified' if options.duration_seconds<1800 or growth is None or len(points)<30 or result['checkpoints_truncated'] or not observed_mixture else
            'passed' if growth<=16*1024**2 and task_growth<=2 else 'failed',
        late_half_rss_growth_bytes=growth,late_half_task_growth=task_growth,
        observed_hits_misses_mutations_and_faults=observed_mixture,
        monitoring_reconciled=reconciled,
        diagnostic_growth_budget_bytes=16*1024**2,diagnostic_task_growth_budget=2,
        note='Diagnostic growth bounds supplement, not replace, review of full checkpoints.')),
        graceful_shutdown=proxy.graceful and origin.graceful)
    return report


def exit_code(report,require_gates=False):
    gates=report['gates']
    if any(g['state']=='failed' for key,g in gates.items() if key in ('correctness','shutdown','cache_budget','telemetry_health') or require_gates or report['options']['profile']!='quick'):return 3
    if require_gates and any(g['state'] not in ('passed','confirmed') for g in gates.values()):return 3
    return 0
