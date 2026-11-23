"""Validated declarative scenarios and a bounded local request executor."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from pathlib import Path
import posixpath
import re
from time import perf_counter, time
from urllib.parse import quote, unquote, urlsplit
import uuid

from pydantic import Field, StrictBool, field_validator, model_validator
from typing import Annotated, Any, Literal
from .product_config import Contract

METHODS=Literal['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS']
TOKEN=re.compile(r'{{([A-Za-z_][A-Za-z0-9_]{0,39})}}')
FIELD=re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]{1,5})){0,7}$|^\$')
FORBIDDEN={'__proto__','prototype','constructor'}
MAX_FILE=1_048_576
MAX_RESPONSE=65_536


class ScenarioBusy(RuntimeError):
    """Only one local workflow may dispatch requests at a time."""


def path_check(value):
    parsed=urlsplit(value)
    decoded=unquote(parsed.path)
    canonical=posixpath.normpath(decoded)
    if (not value.startswith('/') or value.startswith('//') or parsed.scheme or parsed.netloc or parsed.fragment
        or any(ord(c)<32 or ord(c)==127 for c in unquote(value)) or '\\' in unquote(value)
        or canonical=='/_autotuner' or canonical.startswith('/_autotuner/')):
        raise ValueError('Expected an upstream-relative non-control path')
    return value


def field_check(value):
    if not FIELD.fullmatch(value) or any(part in FORBIDDEN for part in value.split('.')):raise ValueError('Invalid response field')
    return value


def walk(value,depth=0):
    if depth>12:raise ValueError('JSON nesting limit exceeded')
    if isinstance(value,dict):
        for key,item in value.items():
            if not isinstance(key,str) or '{{' in key:raise ValueError('JSON keys must be literal strings')
            yield from walk(item,depth+1)
    elif isinstance(value,list):
        for item in value:yield from walk(item,depth+1)
    elif isinstance(value,str):yield value
    elif value is not None and type(value) not in (int,float,bool):raise ValueError('Expected JSON data')
    elif isinstance(value,float) and not isfinite(value):raise ValueError('JSON numbers must be finite')


def references(value):
    found=set()
    for text in walk(value):
        found.update(TOKEN.findall(text))
        if '{{' in TOKEN.sub('',text) or '}}' in TOKEN.sub('',text):raise ValueError('Invalid variable syntax')
    return found


class RequestSpec(Contract):
    method: METHODS='GET'
    path: Annotated[str,Field(min_length=1,max_length=2048)]
    headers: dict[str,str]=Field(default_factory=dict,max_length=32)
    body: Any=None
    include_response: StrictBool=False
    fingerprint_response: StrictBool=False
    timeout_seconds: Annotated[int,Field(strict=True,ge=1,le=120)]=30

    _path=field_validator('path')(path_check)

    @field_validator('headers')
    @classmethod
    def valid_headers(cls,values):
        for name,value in values.items():
            if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+",name) or
                name.lower() in {'host','content-length','transfer-encoding','connection','upgrade','x-autotuner-proxy'} or
                name.lower().startswith('proxy-') or len(value)>8192 or any(ord(c)<32 or ord(c)>126 for c in value)):
                raise ValueError('Unsupported request header')
        return values

    @model_validator(mode='after')
    def bounded_body(self):
        list(walk(self.body))
        if len(json.dumps(self.body,allow_nan=False).encode())>65536:raise ValueError('Body exceeds console limit')
        return self


class Check(Contract):
    field: str
    equals: Any
    _field=field_validator('field')(field_check)


class Step(RequestSpec):
    name: Annotated[str,Field(min_length=1,max_length=80)]
    expect_status: Annotated[int,Field(strict=True,ge=100,le=599)]=200
    capture: dict[str,str]=Field(default_factory=dict,max_length=20)
    checks: list[Check]=Field(default_factory=list,max_length=20)
    repeat: Annotated[int,Field(strict=True,ge=1,le=20)]=1

    @field_validator('capture')
    @classmethod
    def valid_captures(cls,values):
        for name,path in values.items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,39}',name) or name in FORBIDDEN|{'run_id','cycle'}:raise ValueError('Invalid capture name')
            field_check(path)
        return values

    @model_validator(mode='after')
    def safe_repeat(self):
        if self.include_response:raise ValueError('Scenario reports do not capture response bodies')
        if self.repeat>1 and (self.capture or self.method not in ('GET','HEAD','OPTIONS')):raise ValueError('Repeat is read-only and cannot capture')
        return self


class Scenario(Contract):
    schema_version: Literal[1]=1
    name: Annotated[str,Field(min_length=1,max_length=80)]
    cycles: Annotated[int,Field(strict=True,ge=1,le=10)]=1
    delay_ms: Annotated[int,Field(strict=True,ge=0,le=5000)]=0
    steps: list[Step]=Field(min_length=1,max_length=100)

    @model_validator(mode='after')
    def dependencies(self):
        known={'run_id','cycle'}
        for step in self.steps:
            used=references([step.path,step.body,step.headers,[check.equals for check in step.checks]])
            if used-known:raise ValueError('A variable is referenced before capture')
            if known&step.capture.keys():raise ValueError('Capture names must be unique')
            known.update(step.capture)
        if len(known)>52 or self.cycles*sum(step.repeat for step in self.steps)>1000:raise ValueError('Scenario budget exceeded')
        return self

    def preview(self):
        return dict(schema_version=1,name=self.name,cycles=self.cycles,requests=self.cycles*sum(s.repeat for s in self.steps),
            writes=self.cycles*sum(s.repeat for s in self.steps if s.method not in ('GET','HEAD','OPTIONS')),
            delay_ms=self.delay_ms,steps=[dict(name=s.name,method=s.method,path_template=s.path.split('?',1)[0],repeat=s.repeat,
                expect_status=s.expect_status,captures=list(s.capture)) for s in self.steps])


def load_scenario(path):
    with Path(path).open('rb') as file:content=file.read(MAX_FILE+1)
    if len(content)>MAX_FILE:raise ValueError('Scenario file exceeds limit')
    try:return Scenario.model_validate(json.loads(content.decode('utf-8-sig')))
    except RecursionError:raise ValueError('Scenario nesting exceeds limit') from None


def resolve(value,variables,*,path=False):
    if isinstance(value,str):
        match=TOKEN.fullmatch(value)
        if match and not path:return variables[match[1]]
        return TOKEN.sub(lambda m:quote(str(variables[m[1]]),safe='') if path else str(variables[m[1]]),value)
    if isinstance(value,list):return [resolve(item,variables) for item in value]
    if isinstance(value,dict):return {key:resolve(item,variables) for key,item in value.items()}
    return value


def select(data,path):
    if path=='$':return data
    for part in path.split('.'):
        if isinstance(data,list) and part.isdigit():data=data[int(part)]
        elif isinstance(data,dict):data=data[part]
        else:raise KeyError(path)
    return data


@dataclass
class Result:
    status: int
    duration_ms: float
    headers: list
    content: bytes
    body_bytes: int
    truncated: bool
    response_sha256: str | None = None

    def data(self):return json.loads(self.content)

    def public(self,inspect=False):
        headers={k.decode('latin1'):v.decode('latin1') for k,v in self.headers}
        data=dict(status=self.status,duration_ms=self.duration_ms,body_bytes=self.body_bytes,truncated=self.truncated,
            cache=headers.get('x-autotuner-cache'),cache_reason=headers.get('x-autotuner-cache-reason'))
        if inspect:data.update(response_text=self.content.decode('utf-8',errors='replace'),headers=headers)
        return data


async def execute(proxy,spec):
    body=json.dumps(spec.body,allow_nan=False).encode() if 'body' in spec.model_fields_set else b''
    headers=[(k.lower().encode(),v.encode()) for k,v in spec.headers.items()]
    if body:
        if not any(k==b'content-type' for k,v in headers):headers.append((b'content-type',b'application/json'))
        headers.append((b'content-length',str(len(body)).encode()))
    status,response_headers=0,[]
    content=bytearray();body_bytes=0
    fingerprint = hashlib.sha256() if spec.fingerprint_response else None
    sent=False
    async def receive():
        nonlocal sent
        if sent:return {'type':'http.disconnect'}
        sent=True
        return {'type':'http.request','body':body,'more_body':False}
    async def send(message):
        nonlocal status,response_headers,body_bytes
        if message['type']=='http.response.start':status,response_headers=message['status'],message.get('headers',[])
        else:
            chunk=message.get('body',b'');body_bytes+=len(chunk)
            if fingerprint is not None:
                fingerprint.update(chunk)
            content.extend(chunk[:max(0,MAX_RESPONSE-len(content))])
    url=urlsplit(spec.path)
    scope=dict(type='http',asgi={'version':'3.0'},http_version='1.1',method=spec.method,scheme='http',
        path=unquote(url.path),raw_path=url.path.encode('utf-8'),query_string=url.query.encode('utf-8'),headers=headers)
    started=perf_counter()
    await asyncio.wait_for(proxy(scope,receive,send),spec.timeout_seconds)
    return Result(status,(perf_counter()-started)*1000,response_headers,bytes(content),body_bytes,body_bytes>MAX_RESPONSE,
                  fingerprint.hexdigest() if fingerprint is not None else None)


class ScenarioRunner:
    def __init__(self,executor,*,clock=perf_counter):
        self.executor,self.clock=executor,clock
        self.jobs=OrderedDict();self.task=None;self.stop_event=asyncio.Event()

    def current(self):return next(reversed(self.jobs.values())) if self.jobs else None

    def start(self,scenario,*,allow_writes=False,identifier=None):
        identifier=str(uuid.UUID(identifier)) if identifier is not None else str(uuid.uuid4())
        digest=hashlib.sha256(scenario.model_dump_json().encode()).hexdigest()
        if identifier in self.jobs:
            if self.jobs[identifier]["scenario_sha256"]!=digest:raise ValueError("Run identity mismatch")
            return self.jobs[identifier]
        if self.task is not None and not self.task.done():raise ScenarioBusy('A scenario is already running')
        preview=scenario.preview()
        if preview['writes'] and not allow_writes:raise PermissionError('Explicit write approval required')
        self.stop_event=asyncio.Event()
        report=dict(schema_version=1,run_id=identifier,name=scenario.name,state='running',preview=preview,completed=0,failed=0,
                    results=[],started_at=time(),finished_at=None,scenario_sha256=digest)
        self.jobs[identifier]=report
        while len(self.jobs)>3:self.jobs.popitem(last=False)
        self.task=asyncio.create_task(self._run(scenario,report))
        return report

    def stop(self,identifier):
        report=self.current()
        if report is None or report['run_id']!=identifier:raise KeyError(identifier)
        self.stop_event.set()
        return report

    async def close(self):
        self.stop_event.set()
        if self.task is not None:await self.task

    async def _run(self,scenario,report):
        started=self.clock()
        try:
            for cycle in range(1,scenario.cycles+1):
                variables=dict(run_id=report['run_id'].replace('-','')[:12],cycle=cycle)
                for step in scenario.steps:
                    for repeat in range(step.repeat):
                        if self.stop_event.is_set():report['state']='stopped';return
                        if self.clock()-started>=600:report['state']='runtime_limit';return
                        row=dict(index=len(report['results'])+1,cycle=cycle,name=step.name,method=step.method,
                                 path_template=step.path.split('?',1)[0],expected=step.expect_status,status=None,duration_ms=None,cache=None,passed=False,reason=None)
                        try:
                            path=path_check(resolve(step.path,variables,path=True))
                            payload=dict(method=step.method,path=path,headers=resolve(step.headers,variables),timeout_seconds=step.timeout_seconds)
                            if 'body' in step.model_fields_set:payload['body']=resolve(step.body,variables)
                            if step.fingerprint_response:
                                payload['fingerprint_response'] = True
                                start_offset = self.clock() - started
                            response=await self.executor(RequestSpec.model_validate(payload))
                            if step.fingerprint_response:
                                row.update(response_sha256=response.response_sha256, start_offset=start_offset,
                                           end_offset=self.clock()-started)
                            row.update(status=response.status,duration_ms=response.duration_ms,cache=response.public()['cache'])
                            if response.status!=step.expect_status:raise AssertionError('unexpected_status')
                            if step.checks or step.capture:
                                if response.truncated:raise AssertionError('response_too_large')
                                try:data=response.data()
                                except (ValueError,UnicodeError,RecursionError):raise AssertionError('invalid_json') from None
                                for check in step.checks:
                                    try:actual=select(data,check.field)
                                    except (KeyError,IndexError):raise AssertionError('check_field_missing') from None
                                    expected=resolve(check.equals,variables)
                                    if actual!=expected or type(actual) is not type(expected):raise AssertionError('response_assertion_failed')
                                for name,field in step.capture.items():
                                    try:value=select(data,field)
                                    except (KeyError,IndexError):raise AssertionError('capture_field_missing') from None
                                    if (type(value) not in (str,int,float,bool,type(None)) or
                                        (isinstance(value,str) and len(value)>256) or
                                        (isinstance(value,float) and not isfinite(value))):raise AssertionError('capture_not_scalar')
                                    variables[name]=value
                            row.update(passed=True,reason='expected_response')
                        except AssertionError as exc:row['reason']=str(exc)
                        except (ValueError,KeyError,UnicodeError):row['reason']='invalid_resolved_request'
                        except Exception:row['reason']='request_failed'
                        report['results'].append(row);report['completed']+=1
                        if not row['passed']:report['failed']+=1;report['state']='failed';return
                        if self.stop_event.is_set():report['state']='stopped';return
                        if scenario.delay_ms and report['completed']<report['preview']['requests']:
                            try:await asyncio.wait_for(self.stop_event.wait(),scenario.delay_ms/1000)
                            except asyncio.TimeoutError:pass
            report['state']='completed'
        except asyncio.CancelledError:
            report['state']='interrupted';raise
        finally:report['finished_at']=time()
