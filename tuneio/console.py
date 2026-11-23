"""Bundled product console and explicit, bounded playground/scenario controls."""
import asyncio
from email.utils import formatdate
import json
from pathlib import Path
from time import time
from urllib.parse import parse_qs

from pydantic import StrictBool, ValidationError

from .core.insights import analyze
from .core.metrics import percentile
from .product_config import Contract
from .proxy import json_response
from .scenarios import Scenario,RequestSpec,ScenarioRunner,ScenarioBusy,execute,MAX_FILE


class RunInput(Contract):
    scenario: Scenario
    allow_writes: StrictBool=False
    run_id: str|None=None
    expected_session_id: str | None = None


ASSETS={'/_autotuner/dashboard':('product_dashboard.html','text/html; charset=utf-8'),
        '/_autotuner/dashboard/':('product_dashboard.html','text/html; charset=utf-8'),
        '/_autotuner/assets/product.css':('product.css','text/css; charset=utf-8'),
        '/_autotuner/assets/product_ui.js':('product_ui.js','application/javascript; charset=utf-8')}


def timeseries(metrics,window):
    now=metrics.clock();width=window/20
    result=[]
    rows=[r for r in metrics.records if now-window<=r['timestamp']<=now]
    for index in range(20):
        start=now-window+index*width;end=start+width
        items=[r for r in rows if start<=r['timestamp']<end or (index==19 and r['timestamp']==end)]
        values=[r['duration_ms'] for r in items]
        result.append(dict(timestamp=end,count=len(items),p95_ms=percentile(values,.95),cache_hits=sum(r['cache']=='HIT' for r in items)))
    return result


class Console:
    def __init__(self,proxy,status,storage):
        self.proxy,self.status,self.storage=proxy,status,storage
        self.runner=ScenarioRunner(lambda spec:execute(proxy,spec))
        self.body_readers=0

    def snapshot(self,window):
        metrics=self.proxy.metrics.snapshot(window)
        insights=analyze(self.proxy.config,self.proxy.metrics,self.proxy.upstream_metrics,
            transport=self.proxy.counters,storage=self.storage.health(),window=window)
        policies=[self.proxy.cache.explain(rule.match,include_session_details=False) for rule in self.proxy.config.cache.routes]
        current=self.runner.current()
        return dict(schema_version=1,status=self.status(),metrics=metrics,upstream=self.proxy.upstream_metrics.snapshot(window),
            insights=insights,policies=policies,decisions=list(self.proxy.cache.controller.history),
            series=timeseries(self.proxy.metrics,window),operations=[op.model_dump() for op in self.proxy.config.operations],
            scenario={k:v for k,v in current.items() if k!='results'} if current else None)

    def monitor(self, window):
        cache = self.proxy.cache
        policies = []
        for rule in self.proxy.config.cache.routes:
            tuning = cache.controller.evaluate(rule) if rule.policy == 'adaptive' else None
            state = 'serving' if cache.serving(rule) else 'bypass'
            detail = (f"{tuning['reason']} · {tuning['inputs']['successful_samples']} samples · TTL {tuning['ttl_seconds']:g}s"
                      if tuning else rule.policy)
            policies.append(dict(id=rule.id, match=rule.match, state=state, detail=detail))
        insights = analyze(self.proxy.config, self.proxy.metrics, self.proxy.upstream_metrics,
                           transport=self.proxy.counters, storage=self.storage.health(), window=window)
        return dict(schema_version=1, status=self.status(), metrics=self.proxy.metrics.snapshot(window),
                    findings=insights['findings'], evidence_quality=insights['evidence_quality'],
                    policies=policies, decisions=list(cache.controller.history))

    async def read_json(self,scope,receive):
        headers=dict(scope.get('headers',[]))
        if headers.get(b'content-type',b'').split(b';')[0].strip().lower()!=b'application/json':raise TypeError('JSON required')
        data=bytearray()
        deadline=asyncio.get_running_loop().time()+30
        while True:
            message=await asyncio.wait_for(receive(),max(0,deadline-asyncio.get_running_loop().time()))
            if message['type']=='http.disconnect':raise ValueError('Request disconnected')
            chunk=message.get('body',b'')
            if len(chunk)>MAX_FILE-len(data):raise OverflowError('Input too large')
            data.extend(chunk)
            if not message.get('more_body'):break
        payload=json.loads(data.decode('utf-8-sig'))
        if not isinstance(payload,dict):raise ValueError('Expected object')
        return payload

    async def handle(self,scope,receive,send):
        path,method=scope['path'],scope['method']
        if path in ASSETS:
            if method!='GET':await json_response(send,405,{'detail':'GET required'});return True
            filename,mime=ASSETS[path]
            content=Path(__file__).with_name(filename).read_bytes()
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',mime.encode()),
                (b'content-length',str(len(content)).encode()),(b'cache-control',b'no-store'),(b'date',formatdate(usegmt=True).encode()),
                (b'content-security-policy',b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
                (b'x-content-type-options',b'nosniff')]})
            await send({'type':'http.response.body','body':content});return True
        get_paths={'/_autotuner/dashboard/data','/_autotuner/dashboard/presets'}
        post_paths={'/_autotuner/playground/request','/_autotuner/scenarios/preview','/_autotuner/scenarios/run','/_autotuner/scenarios/stop'}
        job_path=path.startswith('/_autotuner/scenarios/jobs/')
        if path not in get_paths|post_paths and not job_path:return False
        if method!=('GET' if path in get_paths or job_path else 'POST'):
            await json_response(send,405,{'detail':'Unsupported method'});return True
        try:
            if path=='/_autotuner/dashboard/data':
                window=int(parse_qs(scope.get('query_string',b'').decode()).get('window',['60'])[-1])
                if not 1<=window<=3600:raise ValueError('Invalid window')
                await json_response(send,200,self.snapshot(window));return True
            if path=='/_autotuner/dashboard/presets':
                data=json.loads(Path(__file__).with_name('scenario_presets.json').read_text(encoding='utf-8'))
                await json_response(send,200,dict(schema_version=1,presets=data));return True
            if job_path:
                identifier=path.rsplit('/',1)[-1]
                data=self.runner.jobs.get(identifier)
                if data is None:raise KeyError(identifier)
                await json_response(send,200,data);return True
            if self.body_readers>=4:
                await json_response(send,503,{'detail':'Console input capacity exceeded'});return True
            self.body_readers+=1
            try:
                payload=await self.read_json(scope,receive)
                if path=='/_autotuner/playground/request':
                    spec=RequestSpec.model_validate(payload)
                    result=await execute(self.proxy,spec)
                    await json_response(send,200,result.public(spec.include_response))
                elif path=='/_autotuner/scenarios/stop':
                    result=self.runner.stop(payload.get('run_id'))
                    await json_response(send,200,result)
                else:
                    data=RunInput.model_validate(payload)
                    if data.expected_session_id is not None and data.expected_session_id != self.proxy.session_id:
                        await json_response(send,409,{'detail':'Proxy session changed. Review the workload again.'})
                        return True
                    if path.endswith('/preview'):await json_response(send,200,data.scenario.preview())
                    else:await json_response(send,202,self.runner.start(data.scenario,allow_writes=data.allow_writes,identifier=data.run_id))
            finally:self.body_readers-=1
        except KeyError:await json_response(send,404,{'detail':'Scenario not found'})
        except PermissionError:await json_response(send,400,{'detail':'Preview writes and explicitly allow them before running'})
        except ValidationError:await json_response(send,422,{'detail':'Invalid request or scenario definition; check fields, captures and bounds'})
        except TypeError:await json_response(send,415,{'detail':'application/json required'})
        except OverflowError:await json_response(send,413,{'detail':'Console input exceeds 1 MiB'})
        except (ValueError,UnicodeError,RecursionError):await json_response(send,400,{'detail':'Invalid JSON, path or window'})
        except asyncio.TimeoutError:await json_response(send,504,{'detail':'Console request timed out'})
        except ScenarioBusy:await json_response(send,409,{'detail':'A scenario is already running'})
        except RuntimeError:await json_response(send,502,{'detail':'The proxy response was interrupted; inspect upstream state before retrying writes'})
        except OSError:await json_response(send,502,{'detail':'Console request failed'})
        return True
