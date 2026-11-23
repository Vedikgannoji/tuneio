"""Reviewed, immutable proxy configurations. Each revision takes effect at startup."""
from pathlib import Path

from .artifacts import read_json, write_new
from .baseline import digest
from .product_config import CacheRules, Mutation, ProductConfig, RoutePolicy
from .scenarios import path_check
from .storage import config_identity


KIND = 'autotuner-policy-revision'
CONFIG_LIMIT = 8 * 1024 * 1024


def unpack(document):
    if document.get('kind') != KIND:
        return document, None
    config = document.get('config')
    previous = document.get('previous_policy')
    if (type(document.get('schema_version')) is not int or document['schema_version'] != 1 or not isinstance(config, dict)
            or not isinstance(previous, dict) or set(previous) != {'mode', 'cache'}
            or digest([config, previous, document.get('action'), document.get('parent_digest')]) != document.get('revision')):
        raise ValueError('Invalid policy revision. Select the original reviewed file.')
    return config, document


def scope(config):
    return {'mode': config.mode, 'cache': config.cache.model_dump()}


def enabled_policy(args, config):
    route = path_check(args.route)
    rules = [rule.model_copy(deep=True) for rule in config.cache.routes]
    selected = next((rule for rule in rules if rule.match == route), None)
    if selected is None and not any(op.method == 'GET' and op.path == route for op in config.operations):
        raise ValueError('Choose a configured GET path or cache rule pattern.')
    writes = {(op.method, op.path) for op in config.operations if op.method in ('POST', 'PUT', 'PATCH', 'DELETE')}
    if selected:
        writes.update((op.method, op.path) for op in selected.invalidate_on)
    writes.update(tuple(item) for item in args.invalidate)
    if not writes and not args.read_only:
        raise ValueError('Declare a dependent write with --invalidate METHOD PATH or attest a read-only resource with --read-only.')
    mutations = [Mutation(method=method, path=path_check(path)) for method, path in sorted(writes)]
    # Moving from observation to adaptive must only activate the selected rule.
    for rule in rules:
        if rule.match != route and (config.mode in ('recommend', 'off', 'observe')
                                   or config.mode == 'cache' and rule.policy == 'adaptive'):
            rule.policy = 'observe'
    if selected is None:
        selected = RoutePolicy(id='public-' + digest(route)[:12], match=route,
                               vary_headers=['Accept', 'Accept-Encoding'])
        rules.append(selected)
    selected.policy = 'adaptive'
    selected.invalidate_on = mutations
    return {'mode': 'adaptive', 'cache': CacheRules(routes=rules).model_dump()}


def review(args, config, parent):
    action = args.action
    identity, _ = config_identity(config)
    old = scope(config)
    if action == 'inspect':
        return dict(schema_version=1, config_digest=identity, policy=old,
                    revision=parent['revision'] if parent else None,
                    rollback_policy=parent['previous_policy'] if parent else None)
    if not args.output:
        raise ValueError('Select a new revision filename with --output.')
    evidence = None
    if action == 'enable':
        policy = enabled_policy(args, config)
        if args.baseline:
            baseline, _ = read_json(args.baseline, 32 * 1024 * 1024)
            if (baseline.get('config_digest') != identity or baseline.get('state') != 'completed'
                    or baseline.get('kind') not in ('active', 'passive')):
                raise ValueError('Select a completed baseline from this configuration.')
            insights = baseline.get('insights')
            rows = insights.get('cache_suitability') if isinstance(insights, dict) else None
            if not isinstance(rows, list):
                raise ValueError('The baseline contains invalid cache suitability evidence.')
            evidence = next((row for row in rows if isinstance(row, dict)
                             and row.get('operation') == 'GET ' + args.route), None)
            if evidence is None:
                raise ValueError('The baseline contains no evidence for the selected GET.')
    elif action == 'rollback':
        if parent is None or config.model_dump() != parent['config']:
            raise ValueError('Rollback requires the reviewed revision with its original connection settings.')
        policy = parent['previous_policy']
    else:
        policy = dict(old, mode='recommend' if action == 'pause' else 'off')
    effective = ProductConfig.model_validate({**config.model_dump(), **policy}).model_dump()
    revision = digest([effective, old, action, identity])
    token = digest([revision, str(Path(args.output).resolve()), evidence])
    result = dict(schema_version=1, action=action, written=False, revision=revision,
                  upstream=config.upstream, listen=config.listen, policy=policy, previous_policy=old,
                  evidence=evidence, activation=config.tuning.model_dump(), limits=config.limits.model_dump(),
                  reason={'enable': 'Authorize the selected public GET to learn origin latency and reuse. Serving starts when both thresholds are met.',
                          'pause': 'Observe traffic and keep declared rules available for learning.',
                          'disable': 'Turn off cache serving and adaptive learning.',
                          'rollback': 'Restore the previous mode and cache rules.'}[action],
                  requirements='Verify public response semantics and route dependent writes through this proxy. External changes can remain cached until expiry.',
                  activation_boundary='Restart the proxy with the saved revision. Rollback restores the previous policy on restart.',
                  output=str(args.output), review_token=token)
    if args.apply is not None:
        if token != args.apply:
            raise ValueError('The reviewed configuration, evidence or destination changed. Preview again.')
        write_new(args.output, dict(kind=KIND, schema_version=1, action=action, revision=revision,
                                    parent_digest=identity, config=effective, previous_policy=old), limit=CONFIG_LIMIT)
        result['written'] = True
    return result
