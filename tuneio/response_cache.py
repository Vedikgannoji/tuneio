"""Single-worker HTTP representation cache with strict accounting and generations."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime, formatdate
import hashlib
import json
import re
from time import perf_counter, time
from .core.controller import AdaptiveController


@dataclass
class Entry:
    key: str
    rule: str
    generation: int
    headers: tuple
    chunks: tuple
    cost: int
    received: float
    initial_age: float
    expires: float
    resident: bool = True
    pins: int = 0


@dataclass
class Fill:
    key: str
    rule: str
    generation: int
    headers: tuple
    received: float
    initial_age: float
    expires: float
    cost: int
    chunks: list = field(default_factory=list)
    active: bool = True


def matches(pattern, path):
    a,b=pattern.split('/'),path.split('/')
    return len(a)==len(b) and all(x==y or x=='*' for x,y in zip(a,b))


def effective_mode(config):
    return config.mode if config.mode in ('cache','adaptive','recommend','off') else 'observe'


def request_reason(config, rule, method, headers):
    if effective_mode(config) in ('observe','off'): return 'mode_'+effective_mode(config)
    if method!='GET': return 'method'
    if rule is None: return 'no_rule'
    if rule.policy=='observe' or (config.mode=='cache' and rule.policy!='cache'): return 'rule_observe'
    names={k.lower() for k,v in headers}
    if names & {b'authorization',b'cookie'}: return 'credentials'
    if b'range' in names or any(k.startswith(b'if-') for k in names): return 'conditional_or_range'
    if names & {b'cache-control',b'pragma'}: return 'request_directives'
    if b'transfer-encoding' in names or any(k.lower()==b'content-length' and v!=b'0' for k,v in headers): return 'get_body'
    return 'eligible'


def representation_key(upstream, method, target, headers):
    # A JSON list preserves missing, repeated and ordered raw header values unambiguously.
    variants=[[v.hex() for k,v in headers if k.lower()==name] for name in (b'accept',b'accept-encoding')]
    return hashlib.sha256(json.dumps([upstream,method,target.hex(),variants],separators=(',',':')).encode()).hexdigest()


def freshness(headers, rule, *, wall, received, sent, preferred_ttl=None):
    values={}
    for k,v in headers: values.setdefault(k.lower(),[]).append(v)
    if b'set-cookie' in values: return None,'set_cookie'
    if b'pragma' in values: return None,'response_directives'
    if b'trailer' in values: return None,'trailers'
    hops={b'connection',b'keep-alive',b'proxy-connection',b'proxy-authenticate',b'proxy-authorization',b'te',b'trailer',b'transfer-encoding',b'upgrade'}
    named={n.strip().lower() for value in values.get(b'connection',[]) for n in value.split(b',')}
    if (named-hops)&values.keys(): return None,'connection_metadata'
    vary={v.strip().lower() for value in values.get(b'vary',[]) for v in value.split(b',') if v.strip()}
    approved={v.lower().encode() for v in rule.vary_headers}
    if vary-approved: return None,'unsupported_vary'
    directives={}
    for value in values.get(b'cache-control',[]):
        for raw in value.decode('latin1').split(','):
            name,sep,argument=raw.strip().partition('=')
            name=name.lower()
            if name in ('no-store','private','no-cache'): return None,'response_privacy'
            if name not in ('public','max-age','s-maxage'): return None,'unsupported_directive'
            if name in directives: return None,'ambiguous_directive'
            if name=='public':
                if sep: return None,'invalid_directive'
                directives[name]=True
            else:
                if not sep or not re.fullmatch(r'\d{1,10}|"\d{1,10}"',argument): return None,'invalid_directive'
                directives[name]=int(argument.strip('"'))
    def date(name):
        entries=values.get(name,[])
        if not entries: return None
        if len(entries)!=1: raise ValueError()
        parsed=parsedate_to_datetime(entries[0].decode('ascii'))
        if parsed.tzinfo is None: raise ValueError()
        return parsed.timestamp()
    try:
        date_value=date(b'date')
        expires=date(b'expires')
        ages=values.get(b'age',[])
        if len(ages)>1 or (ages and not re.fullmatch(rb'\d{1,10}',ages[0])): raise ValueError()
        age=int(ages[0]) if ages else 0
    except (ValueError, TypeError, OverflowError, UnicodeError):
        return None,'invalid_age_or_date'
    lifetime=directives.get('s-maxage',directives.get('max-age'))
    if lifetime is None:
        if expires is None: return None,'missing_freshness'
        if date_value is None: return None,'expires_without_date'
        lifetime=expires-date_value
    initial_age=max(max(0,wall-date_value) if date_value is not None else 0,age+max(0,received-sent))
    remaining=lifetime-initial_age
    if remaining<rule.ttl_min_seconds: return None,'freshness_below_minimum'
    ttl=min(max(rule.ttl_min_seconds,preferred_ttl if preferred_ttl is not None else rule.ttl_min_seconds),rule.ttl_max_seconds,remaining)
    # Store raw representation headers, reconstruct Age and Content-Length on a hit.
    normalized=tuple((k.lower(),v) for k,v in headers if k.lower() not in hops|named|{b'age',b'content-length',b'x-autotuner-cache',b'x-autotuner-cache-reason'})
    if date_value is None: normalized+=((b'date',formatdate(wall,usegmt=True).encode()),)
    return dict(headers=normalized,received=received,initial_age=initial_age,expires=received+ttl),'cacheable'


class ResponseCache:
    def __init__(self,config,*,clock=perf_counter,wall_clock=time,session_id=None):
        self.config,self.clock,self.wall_clock=config,clock,wall_clock
        self.entries=OrderedDict()
        self.generations={r.id:0 for r in config.cache.routes}
        self.writes={r.id:0 for r in config.cache.routes}
        self.locks={}
        self.decisions={}
        self.controller=AdaptiveController(config,clock=lambda:self.clock(),wall_clock=lambda:self.wall_clock(),session_id=session_id,on_change=self._tuning_changed)
        self.used_bytes=0
        self.peak_bytes=0
        self.retired=0
        self.counters=dict(hits=0,misses=0,bypasses=0,stores=0,evictions=0,expirations=0,
                           invalidations=0,coalesced_waiters=0,generation_rejections=0)

    def _tuning_changed(self,rule,decision):
        if not decision['new_enabled']:
            self.invalidate([rule.id])
        elif decision['new_ttl_seconds']<decision['previous_ttl_seconds']:
            for entry in self.entries.values():
                if entry.rule==rule.id:entry.expires=min(entry.expires,entry.received+decision['new_ttl_seconds'])

    def serving(self,rule):
        if self.config.mode not in ('cache','adaptive'):return False
        if rule.policy=='cache':return True
        return rule.policy=='adaptive' and self.config.mode=='adaptive' and self.controller.evaluate(rule)['enabled']

    def assess(self,rule,headers,status,*,received,sent):
        if status!=200:return None,'response_status'
        if any(k.lower()==b'content-range' for k,v in headers):return None,'partial_response'
        preferred=self.controller.evaluate(rule)['ttl_seconds'] if rule.policy=='adaptive' else None
        return freshness(headers,rule,wall=self.wall_clock(),received=received,sent=sent,preferred_ttl=preferred)

    def rule(self,path):
        rules=[r for r in self.config.cache.routes if matches(r.match,path)]
        return min(rules,key=lambda r:r.match.count('*')) if rules else None

    def _remove(self,key,reason):
        entry=self.entries.pop(key)
        entry.resident=False
        if entry.pins: self.retired+=1
        else: self.used_bytes-=entry.cost
        self.counters[reason]+=1

    def contains(self,key,rule):
        if not self.serving(rule):return False
        entry=self.entries.get(key)
        if entry is None or self.writes[rule.id]: return False
        if entry.expires<=self.clock() or entry.generation!=self.generations[rule.id]:
            self._remove(key,'expirations')
            return False
        return True

    def acquire(self,key,rule):
        if not self.contains(key,rule):return None
        entry=self.entries[key]
        entry.pins+=1
        self.entries.move_to_end(key)
        self.counters['hits']+=1
        if rule.policy=='adaptive':self.controller.hit(rule,key)
        return entry

    def release(self,entry):
        entry.pins-=1
        if not entry.resident and entry.pins==0:
            self.retired-=1
            self.used_bytes-=entry.cost

    def invalidate(self,ids=None):
        ids=set(self.generations if ids is None else ids)
        for name in ids:
            self.generations[name]+=1
            rule=next(r for r in self.config.cache.routes if r.id==name)
            if rule.policy=="adaptive":self.controller.note_invalidation(rule)
        for key,entry in list(self.entries.items()):
            if entry.rule in ids:self._remove(key,'invalidations')

    def begin_write(self,method,path):
        ids=[r.id for r in self.config.cache.routes if any(m.method==method and matches(m.path,path) for m in r.invalidate_on)]
        self.invalidate(ids)
        for name in ids:self.writes[name]+=1
        return ids

    def end_write(self,ids):
        self.invalidate(ids)
        for name in ids:self.writes[name]-=1

    def _reserve(self,cost):
        while self.used_bytes+cost>self.config.limits.cache_bytes and self.entries:
            self._remove(next(iter(self.entries)),'evictions')
        if self.used_bytes+cost>self.config.limits.cache_bytes:return False
        self.used_bytes+=cost
        self.peak_bytes=max(self.peak_bytes,self.used_bytes)
        return True

    def begin_fill(self,key,rule,generation,headers,status,*,received,sent):
        info,reason=self.assess(rule,headers,status,received=received,sent=sent)
        if info is None:return None,reason
        if not self.serving(rule):
            return None,'recommend_only' if self.config.mode=='recommend' else 'learning_'+self.controller.evaluate(rule)['reason']
        cost=256+len(key)+sum(len(k)+len(v) for k,v in info['headers'])
        if cost>self.config.limits.cache_entry_bytes or not self._reserve(cost):return None,'capacity'
        return Fill(key=key,rule=rule.id,generation=generation,cost=cost,**info),'miss'

    def append(self,fill,chunk):
        if fill.cost+len(chunk)>self.config.limits.cache_entry_bytes or not self._reserve(len(chunk)):
            self.discard(fill)
            return False
        fill.chunks.append(chunk)
        fill.cost+=len(chunk)
        return True

    def discard(self,fill):
        if fill is not None and fill.active:
            self.used_bytes-=fill.cost
            fill.active=False
            fill.chunks.clear()

    def publish(self,fill):
        rule=next(r for r in self.config.cache.routes if r.id==fill.rule)
        active=self.serving(rule)
        if rule.policy=="adaptive":fill.expires=min(fill.expires,fill.received+self.controller.evaluate(rule)["ttl_seconds"])
        if (not active or fill.generation!=self.generations[fill.rule] or self.writes[fill.rule] or fill.expires<=self.clock()):
            self.counters['generation_rejections']+=1
            self.discard(fill)
            return False
        if fill.key in self.entries:self._remove(fill.key,'evictions')
        while len(self.entries)>=self.config.limits.cache_entries:self._remove(next(iter(self.entries)),'evictions')
        self.entries[fill.key]=Entry(key=fill.key,rule=fill.rule,generation=fill.generation,headers=fill.headers,
                                    chunks=tuple(fill.chunks),cost=fill.cost,received=fill.received,
                                    initial_age=fill.initial_age,expires=fill.expires)
        fill.active=False
        fill.chunks.clear()
        self.counters['stores']+=1
        return True

    def coordination(self,key):
        if key not in self.locks:self.locks[key]=[asyncio.Lock(),0]
        item=self.locks[key];item[1]+=1
        # Registrations include acquisitions queued by wait_for before the lock
        # coroutine runs (notably on Python 3.11). Lock state alone misses them.
        if item[1]>1:self.counters['coalesced_waiters']+=1
        return item[0]

    def release_coordination(self,key,acquired):
        lock,users=self.locks[key]
        if acquired:lock.release()
        if users==1:del self.locks[key]
        else:self.locks[key][1]-=1

    def record_decision(self,rule,outcome,reason,origin):
        if rule is None:return
        previous=self.decisions.get(rule.id,dict(origin_samples=0))
        self.decisions[rule.id]=dict(outcome=outcome,reason=reason,origin_samples=previous['origin_samples']+int(origin))

    def explain(self,path,*,include_session_details=True):
        rule=self.rule(path)
        result=dict(rule=rule.model_dump() if rule else None, effective_mode=effective_mode(self.config),
                    serving_enabled=bool(rule and self.serving(rule)),
                    tuning=self.controller.evaluate(rule) if rule and rule.policy=='adaptive' else None,
                    ttl_strategy='bounded adaptive preference; origin freshness cap enforced' if rule and rule.policy=='adaptive' else 'fixed policy minimum; origin freshness cap enforced',
                    write_in_progress=bool(rule and self.writes[rule.id]),
                    last_decision=self.decisions.get(rule.id) if rule else None)
        if include_session_details:
            result.update(decision_history=[d for d in self.controller.history if rule and d['rule_id']==rule.id],cache=self.status())
        return result

    def status(self):
        return dict(mode=effective_mode(self.config),rules_enabled=sum(self.serving(r) for r in self.config.cache.routes),entries=len(self.entries),accounted_bytes=self.used_bytes,
                    peak_accounted_bytes=self.peak_bytes,
                    byte_budget=self.config.limits.cache_bytes,retired_pinned_entries=self.retired,
                    coordination_keys=len(self.locks),active_writes=sum(self.writes.values()),**self.counters)
