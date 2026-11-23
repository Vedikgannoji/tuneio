"""Bounded deterministic controller; learns admitted public-origin cost, never hit latency."""
from collections import Counter, deque
from datetime import datetime, timezone
from time import perf_counter, time
from math import isfinite


class AdaptiveController:
    def __init__(self,config,*,clock=perf_counter,wall_clock=time,session_id=None,on_change=None):
        self.config,self.clock,self.wall_clock=config,clock,wall_clock
        self.session_id,self.on_change=session_id,on_change
        self.listener=None
        self.states={}
        self.history=deque(maxlen=config.tuning.history_capacity)

    def state(self,rule):
        if rule.id not in self.states:
            self.states[rule.id]=dict(enabled=False,ttl=float(rule.ttl_min_seconds),changed=float('-inf'),
                samples=deque(maxlen=self.config.tuning.sample_capacity),reuse=deque(maxlen=self.config.tuning.sample_capacity),
                signature=None,hits=0,origin_observations=0,invalidations=0)
        return self.states[rule.id]

    def note_invalidation(self,rule):
        self.state(rule)['invalidations']+=1

    def hit(self,rule,key):
        state=self.state(rule)
        state['hits']+=1
        state['reuse'].append((self.clock(),key))

    def observe(self,rule,key,duration_ms,*,failed=False,reason='origin_sample'):
        if not isfinite(duration_ms) or duration_ms<0:raise ValueError('Origin duration must be finite and nonnegative')
        state=self.state(rule)
        now=self.clock()
        state['samples'].append((now,duration_ms,failed,reason))
        state['origin_observations']+=1
        if not failed:state['reuse'].append((now,key))
        return self.evaluate(rule)

    def evaluate(self,rule):
        state=self.state(rule);options=self.config.tuning;now=self.clock()
        while state['samples'] and state['samples'][0][0]<now-options.learning_window_seconds:state['samples'].popleft()
        while state['reuse'] and state['reuse'][0][0]<now-options.reuse_window_seconds:state['reuse'].popleft()
        samples=list(state['samples'])
        successes=[row[1] for row in samples if not row[2]]
        errors=[row for row in samples if row[2]]
        mean=sum(successes)/len(successes) if successes else None
        maximum_reuse=max(Counter(key for timestamp,key in state['reuse']).values(),default=0)
        threshold=options.retain_ms if state['enabled'] else options.enable_ms
        if errors:reason=errors[-1][3]
        elif len(successes)<options.minimum_samples:reason='collecting_samples'
        elif maximum_reuse<options.reuse_minimum:reason='insufficient_recent_reuse'
        elif mean<threshold:reason='fast_origin'
        else:reason='slow_reused_origin'
        candidate=reason=='slow_reused_origin'
        desired_ttl=min(float(rule.ttl_max_seconds),max(float(rule.ttl_min_seconds),mean if mean is not None else rule.ttl_min_seconds)) if candidate else float(rule.ttl_min_seconds)
        old_enabled,old_ttl=state['enabled'],state['ttl']
        remaining=max(0,options.cooldown_seconds-(now-state['changed']))
        applies=self.config.mode=='adaptive' and rule.policy=='adaptive'
        action='retained' if old_enabled else 'observing'
        if applies:
            changed=candidate!=old_enabled or desired_ttl!=old_ttl
            if errors or not remaining or not changed:
                state['enabled'],state['ttl']=candidate,desired_ttl
                if changed:
                    state['changed']=now
                    action='enabled' if candidate and not old_enabled else 'disabled' if old_enabled and not candidate else 'adjusted_ttl'
            else:action='cooldown'
        elif self.config.mode=='recommend':action='recommend_enable' if candidate else 'recommend_observe'
        else:action='mode_'+self.config.mode
        changed=old_enabled!=state['enabled'] or old_ttl!=state['ttl']
        inputs=dict(samples=len(samples),successful_samples=len(successes),failed_samples=len(errors),origin_mean_ms=mean,
                    maximum_same_key_observations=maximum_reuse,cache_hits=state['hits'],origin_observations=state['origin_observations'],
                    invalidation_boundaries=state['invalidations'])
        signature=(state['enabled'],state['ttl'],candidate,desired_ttl,reason,action=='cooldown',len(samples),len(errors),
                   state['origin_observations'],state['invalidations'],maximum_reuse>=options.reuse_minimum)
        if signature!=state['signature']:
            state['signature']=signature
            # Only materialize an audit record when an actual observation/decision changed.
            decision=dict(schema_version=1,session_id=self.session_id,rule_id=rule.id,
                timestamp=datetime.fromtimestamp(self.wall_clock(),timezone.utc).isoformat(),action=action,reason=reason,
                applied=changed and applies,previous_enabled=old_enabled,new_enabled=state['enabled'],
                previous_ttl_seconds=old_ttl,new_ttl_seconds=state['ttl'],candidate_enabled=candidate,candidate_ttl_seconds=desired_ttl,
                thresholds=options.model_dump(),inputs=inputs)
            self.history.append(decision)
            if changed and self.on_change:self.on_change(rule,decision)
            if self.listener:self.listener(decision)
        return dict(enabled=state['enabled'],ttl_seconds=state['ttl'],candidate_enabled=candidate,
                    candidate_ttl_seconds=desired_ttl,reason=reason,cooldown_remaining_seconds=max(0,options.cooldown_seconds-(now-state['changed'])),
                    cooldown_blocking=action=='cooldown',inputs=inputs,thresholds=options.model_dump())
