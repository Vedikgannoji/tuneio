"""Installable observation-proxy command interface."""
from contextlib import contextmanager, ExitStack
import argparse
import json
import logging
import os
from pathlib import Path
import socket
import sys
import time
import uuid
import webbrowser
from urllib.parse import urlsplit
import urllib.request
import urllib.error

from ._version import __version__

VERSION = __version__


class ControlError(OSError):
    """A sanitized and actionable local-control diagnostic."""


class MissingConfiguration(ValueError):
    """The selected configuration file does not exist."""


@contextmanager
def control_open(opener, request, listen, timeout):
    try:
        with opener.open(request,timeout=timeout) as response:
            yield response
    except urllib.error.HTTPError as exc:
        raise ControlError(f'Local proxy returned HTTP {exc.code}; inspect the proxy terminal and confirm its version and port.') from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason,TimeoutError):
            raise ControlError('Local proxy request timed out; inspect the proxy terminal.') from None
        raise ControlError(f'Cannot reach the local proxy at {listen}. Start autotuner serve with the same config in a separate terminal and leave it running.') from None
    except TimeoutError:
        raise ControlError('Local proxy request timed out; inspect the proxy terminal.') from None
    except ConnectionError:
        raise ControlError(f'Cannot reach the local proxy at {listen}. Keep its serve terminal running.') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def parser():
    root = argparse.ArgumentParser(prog='autotuner', description='Local API proxy with opt-in static and adaptive caching; observe by default.')
    root.add_argument('--version', action='version', version='autotuner ' + VERSION)
    root.add_argument('--no-input',action='store_true',help='Disable interactive prompts')
    commands = root.add_subparsers(dest='command')
    menu=commands.add_parser('menu',help='Open the keyboard-operated terminal workspace')
    menu.add_argument('--project',default='.',help='Project directory; no discovery or startup on entry')
    menu.add_argument('--config',default=None)
    menu.add_argument('--json',action='store_true',help='Describe available menu actions without prompting')
    menu.add_argument('--plain',action='store_true',help='Use line prompts without drawing a screen')
    menu.add_argument('--no-input',action='store_true',default=argparse.SUPPRESS)
    for name, description in (
        ('discover', 'List FastAPI route candidates or read an OpenAPI description'),
        ('setup', 'Preview and save selected operations in an observe configuration'),
    ):
        discovery_command = commands.add_parser(name, help=description)
        discovery_command.add_argument('--project', default='.')
        source = discovery_command.add_mutually_exclusive_group()
        source.add_argument('--openapi', help='OpenAPI 3 JSON file inside the project')
        source.add_argument('--openapi-url', help='Explicit OpenAPI URL on a loopback IP')
        discovery_command.add_argument('--json', action='store_true')
    discovery_command.add_argument('--select', type=int, action='append', required=True, metavar='ID')
    discovery_command.add_argument('--upstream', required=True)
    discovery_command.add_argument('--listen', default='127.0.0.1:8080')
    discovery_command.add_argument('--output', default='autotuner.json')
    discovery_command.add_argument('--apply', metavar='REVIEW_TOKEN', help='Save the exact preview to a new file')
    init = commands.add_parser('init', help='Create an observe-mode JSON configuration')
    init.add_argument('--config', default=None)
    init.add_argument('--force', action='store_true')
    init.add_argument('--upstream', default=None)
    init.add_argument('--json', action='store_true')
    demo=commands.add_parser('demo',help='Run a disposable RAM-only example upstream; no user database')
    demo.add_argument('--listen',default='127.0.0.1:3000')
    release=commands.add_parser('release',help='Inspect local candidate evidence; never publishes')
    release_commands=release.add_subparsers(dest='release_command',required=True)
    ready=release_commands.add_parser('check')
    ready.add_argument('--evidence',required=True)
    ready.add_argument('--json',action='store_true')
    config = commands.add_parser('config', help='Validate resolved configuration')
    checks = config.add_subparsers(dest='config_command', required=True)
    check = checks.add_parser('check')
    serve = commands.add_parser('serve', help='Run the loopback observation proxy')
    serve.add_argument('--check', action='store_true', help='Validate without opening a listener')
    serve.add_argument('--shutdown-timeout', type=int, default=5)
    doctor = commands.add_parser('doctor', help='Offline checks; optional explicit TCP connectivity check')
    doctor.add_argument('--connect', action='store_true')
    doctor.add_argument('--session',action='store_true',help='Explicitly query the running local session and uptime')
    status = commands.add_parser('status', help='Query an already running local control session')
    endpoints = commands.add_parser('endpoints', help='Query observed route metrics')
    endpoints.add_argument('--window', type=int, default=60)
    insights = commands.add_parser('insights', help='Explain recent bottlenecks and caching suitability without applying changes')
    insights.add_argument('--window', type=int, default=300)
    baseline = commands.add_parser('baseline', help='Save passive observations or preview an explicit GET baseline')
    baseline.add_argument('--window', type=int, default=300, help='Rolling proxy observation window in seconds')
    baseline.add_argument('--output', required=True, help='New baseline JSON file')
    baseline.add_argument('--get', metavar='PATH', help='Concrete test-data path matching a configured GET operation')
    baseline.add_argument('--environment', choices=('local', 'staging'), help='Attest the active workload target environment')
    baseline.add_argument('--requests', type=int, default=10)
    baseline.add_argument('--delay-ms', type=int, default=100)
    baseline.add_argument('--timeout', type=int, default=3, help='Active per-request timeout in seconds')
    baseline.add_argument('--apply', metavar='REVIEW_TOKEN', help='Run the exact previewed active workload')
    baseline.add_argument('--stage', choices=('baseline','optimized'), default='baseline')
    baseline.add_argument('--app-state', help='Application build and test-data revision for a matched capture')
    baseline.add_argument('--warmup', type=int, default=0, help='Unmeasured GETs within the 20-request total budget')
    compare = commands.add_parser('compare', help='Compare two matched active captures')
    compare.add_argument('before')
    compare.add_argument('after')
    compare.add_argument('--output', help='Write a new comparison JSON file')
    compare.add_argument('--json', action='store_true')
    monitor = commands.add_parser('monitor', help='Open the live terminal monitor or print one snapshot')
    monitor.add_argument('--once', action='store_true')
    monitor.add_argument('--plain', action='store_true')
    monitor.add_argument('--window', type=int, default=60)
    monitor.add_argument('--interval', type=int, choices=(1, 2, 5, 10), default=2)
    monitor.add_argument('--filter', default='', metavar='TEXT')
    monitor.add_argument('--output', help='Write one monitor snapshot to a new JSON file')
    optimize = commands.add_parser('optimize', help='Review, save or roll back a proxy policy revision')
    actions = optimize.add_subparsers(dest='action', required=True)
    policy_commands = []
    for name in ('inspect', 'enable', 'pause', 'disable', 'rollback'):
        action = actions.add_parser(name)
        policy_commands.append(action)
        if name != 'inspect':
            action.add_argument('--output', required=True, help='New revision JSON file')
            action.add_argument('--apply', metavar='REVIEW_TOKEN', help='Save the previewed revision')
        if name == 'enable':
            action.add_argument('--route', required=True, help='Exact GET pattern from the selected configuration')
            action.add_argument('--invalidate', nargs=2, action='append', default=[], metavar=('METHOD', 'PATH'))
            action.add_argument('--read-only', action='store_true', help='Attest that the resource has no dependent writes')
            action.add_argument('--baseline', help='Include route evidence from a matching baseline JSON')
    sessions = commands.add_parser('sessions', help='List retained sessions, including after shutdown')
    report_command = commands.add_parser('report', help='Export retained endpoint telemetry')
    for sub in (sessions, report_command):
        sub.add_argument('--live', action='store_true', help='Flush and query the running proxy instead of reading disk')
    report_command.add_argument('--session', default=None)
    report_command.add_argument('--format', choices=['json', 'csv'], default='json')
    report_command.add_argument('--output', default=None)
    report_command.add_argument('--force', action='store_true')
    dashboard=commands.add_parser('dashboard',help='Open the product dashboard of a running proxy')
    dashboard.add_argument('--no-browser',action='store_true')
    scenario=commands.add_parser('scenario',help='Preview or explicitly run a declarative API scenario')
    scenario_commands=scenario.add_subparsers(dest='scenario_command',required=True)
    preview=scenario_commands.add_parser('preview')
    run=scenario_commands.add_parser('run')
    for sub in (preview,run):sub.add_argument('file')
    run.add_argument('--allow-writes',action='store_true')
    run.add_argument('--output')
    run.add_argument('--force',action='store_true')
    cache = commands.add_parser('cache',help='Inspect or clear the running response cache')
    cache_commands=cache.add_subparsers(dest='cache_command',required=True)
    explain=cache_commands.add_parser('explain')
    explain.add_argument('route')
    clear=cache_commands.add_parser('clear')
    benchmark=commands.add_parser('benchmark',help='Run explicit isolated HTTP qualification; never targets your upstream')
    benchmark.add_argument('--profile',choices=['quick','qualification','soak'],default='quick')
    for name in ('requests','concurrency','repeats','orders','rate','duration-seconds'):
        benchmark.add_argument('--'+name,type=int)
    benchmark.add_argument('--reference-host',action='store_true',help='Attest the PRD reference host is allocated for this run')
    benchmark.add_argument('--require-gates',action='store_true',help='Exit 3 for failed or missing qualification gates')
    benchmark.add_argument('--format',choices=['json','csv'],default='json')
    benchmark.add_argument('--output',required=True)
    benchmark.add_argument('--force',action='store_true')
    for sub in (check, serve, doctor, status, endpoints, insights, sessions, report_command, explain, clear, dashboard, preview, run, baseline, monitor, *policy_commands):
        sub.add_argument('--config', default=None)
        sub.add_argument('--upstream', default=None)
        sub.add_argument('--listen', default=None)
        sub.add_argument('--mode', choices=['observe', 'recommend', 'cache', 'adaptive', 'off'], default=None)
        sub.add_argument('--json', action='store_true')
    return root


def config_path(args):
    return Path(args.config or os.environ.get('AUTOTUNER_CONFIG', 'autotuner.json'))


def resolved_config(args):
    from .product_config import ProductConfig, config_document, resolve_storage
    path = config_path(args)
    try:
        data, args.policy_revision = config_document(path)
    except FileNotFoundError:
        raise MissingConfiguration(
            f'Configuration file not found: {str(path)!r}. '
            'Create it with autotuner init or select an existing file with --config. '
            'In the menu, use Setup > Create observe configuration or c to select a config.'
        ) from None
    data = dict(data)
    if not isinstance(data, dict):
        raise ValueError('Configuration must be a JSON object')
    for name in ('upstream', 'listen', 'mode'):
        value = getattr(args, name, None)
        if value is None:
            value = os.environ.get('AUTOTUNER_' + name.upper())
        if value is not None:
            data[name] = value
    return resolve_storage(ProductConfig.model_validate(data), config_path(args))


def emit(value, as_json):
    print(json.dumps(value, indent=2) if as_json else '\n'.join(f'{key}: {item}' for key, item in value.items()))


def supports(data, capability):
    """Prefer advertised features; recognize legacy servers without exposing build milestones."""
    if 'capabilities' in data:
        capabilities=data['capabilities']
        return isinstance(capabilities,list) and all(isinstance(item,str) for item in capabilities) and capability in capabilities
    milestone=data.get('phase')
    return type(milestone) is int and {'proxy':3,'dashboard':9}[capability]<=milestone<=11


def local_json(config,method,path,payload=None,*,timeout=10):
    data=json.dumps(payload).encode() if payload is not None else None
    request=urllib.request.Request('http://'+config.listen+path,data=data,method=method,
                                  headers={'Content-Type':'application/json'} if data is not None else {})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with control_open(opener,request,config.listen,timeout) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ControlError('Local control response exceeds the supported size')
        try:result=json.loads(raw)
        except (ValueError, RecursionError):raise ControlError('Invalid local control JSON') from None
    if not isinstance(result,dict) or result.get('schema_version')!=1:raise ControlError('Incompatible local control response')
    return result


def main(argv=None):
    root=parser()
    args = root.parse_args(argv)
    if args.command is None:
        if args.no_input or not (sys.stdin.isatty() and sys.stdout.isatty()):
            root.print_help()
            return 0
        from .terminal import run
        return run(invoke=main)
    if args.command=='menu':
        from .terminal import run
        return run(invoke=main,project=args.project,config=args.config,as_json=args.json,no_input=args.no_input,plain=args.plain)
    # Help, version and menu entry do not import the proxy, database or HTTP client.
    from pydantic import ValidationError
    from .product_config import ProductConfig
    from .session import ControlSession
    from .storage import config_identity, connect_readonly, history, report
    from .reporting import serialize_report
    from .scenarios import load_scenario
    try:
        if args.command in ('discover', 'setup'):
            from .discovery import configuration_path, discover, reviewed_configuration, review_token, write_configuration

            try:
                inventory = discover(args.project, openapi=args.openapi, openapi_url=args.openapi_url)
                if args.command == 'discover':
                    if args.json:
                        emit(inventory, True)
                    else:
                        print(f"{inventory['source']} | {len(inventory['operations'])} supported operations")
                        for entrypoint in inventory['entrypoint_candidates']:
                            print('Possible entry point ' + ascii(entrypoint))
                        for factory in inventory['factory_entrypoint_candidates']:
                            print('Possible app factory ' + ascii(factory))
                        for row in inventory['operations']:
                            print(f"{row['id']}. {row['method']} {row['path']} [{row['confidence']}, {ascii(row['origin'])}]")
                        for warning in inventory['warnings']:
                            print(warning)
                    return 0
                config = reviewed_configuration(inventory, args.select, args.upstream, args.listen)
                output = configuration_path(inventory['project'], args.output)
                selected = [row for row in inventory['operations'] if row['id'] in args.select]
                token = review_token(config, inventory['project'], args.output, selected)
                result = dict(schema_version=1, config=config.model_dump(), review_token=token,
                              selected=selected, output=str(output), written=False)
                if args.apply is not None:
                    if args.apply != token:
                        raise ValueError('Preview changed. Run setup without --apply and review it again')
                    write_configuration(inventory['project'], args.output, config)
                    result['written'] = True
                emit(result, args.json)
                if result['written'] and not args.json:
                    print('Start your API separately, then run autotuner serve --config with the saved file.')
                return 0
            except FileExistsError:
                print('Configuration already exists. Choose a new --output filename.', file=sys.stderr)
                return 2
            except ValidationError:
                print('Invalid upstream, listener or ambiguous operation labels. Review the selections.', file=sys.stderr)
                return 2
            except ValueError as exc:
                # Discovery errors can include filesystem names or URL parser input.
                print('Discovery or setup rejected the input. ' + ascii(str(exc)), file=sys.stderr)
                return 2
            except OSError:
                print('Could not read the project or OpenAPI source, or save the configuration.', file=sys.stderr)
                return 1
        if args.command=='demo':
            from .demo import DemoAPI
            import uvicorn
            config=ProductConfig(upstream='http://127.0.0.1:3000',listen=args.listen)
            host,port=config.listen.rsplit(':',1)
            print('Disposable RAM-only upstream: http://'+config.listen+'; Ctrl+C stops and resets it.',file=sys.stderr)
            uvicorn.run(DemoAPI(),host=host.strip('[]'),port=int(port),access_log=False)
            return 0
        if args.command=='release':
            from .release import assess
            result=assess(Path(args.evidence))
            emit(result,args.json)
            return 0 if result['ready'] else 3
        if args.command == 'compare':
            from .comparison import compare_files, display_comparison
            result = compare_files(args.before, args.after, args.output)
            if args.json:
                emit(result, True)
            else:
                print(display_comparison(result))
            return 0 if result['comparable'] else 3
        if args.command == 'init':
            config = ProductConfig(upstream=args.upstream or os.environ.get('AUTOTUNER_UPSTREAM', 'http://127.0.0.1:3000'))
            path = config_path(args)
            with path.open('w' if args.force else 'x', encoding='utf-8') as file:
                file.write(config.model_dump_json(indent=2) + '\n')
            emit({'schema_version': 1, 'created': str(path), 'mode': 'observe'}, args.json)
            return 0
        if args.command=='benchmark':
            from .benchmarking import Options,run,report_csv,exit_code
            values={name:getattr(args,name) for name in ('profile','requests','concurrency','repeats','orders','rate','duration_seconds','reference_host') if getattr(args,name) is not None}
            if args.profile=='qualification':
                values.setdefault('requests',120);values.setdefault('orders',50000)
            if args.profile=='soak':
                values.setdefault('duration_seconds',1800);values.setdefault('orders',50000);values.setdefault('requests',100)
            options=Options.model_validate(values)
            output=Path(args.output)
            # Reserve before sending any traffic, and never overwrite without --force.
            with output.open('w' if args.force else 'x',encoding='utf-8',newline='') as file:
                try:
                    data=run(options,progress=lambda message:print(message,file=sys.stderr,flush=True))
                except KeyboardInterrupt:
                    file.write(json.dumps(dict(schema_version=1,state='interrupted',qualification_complete=False,
                        note='Owned servers stopped; this file is not performance evidence.'))+'\n')
                    return 3
                except (RuntimeError,OSError):
                    file.write(json.dumps(dict(schema_version=1,state='interrupted_or_runtime_failure',qualification_complete=False,
                        note='Owned servers are stopped; this file is not performance evidence.'))+'\n')
                    raise
                file.write(report_csv(data) if args.format=='csv' else json.dumps(data,indent=2)+'\n')
            print('Benchmark report: '+str(output)+'; qualification requires review of all gates.',file=sys.stderr)
            return exit_code(data,args.require_gates)
        config = resolved_config(args)
        if args.command == 'monitor':
            from .monitor import run
            if not 1 <= args.window <= 3600 or len(args.filter) > 1024:
                raise ValueError('Invalid monitor window or filter')
            identity, _ = config_identity(config)
            def fetch(window):
                result = local_json(config, 'GET', f'/_autotuner/monitor?window={window}', timeout=1)
                status = result.get('status')
                if not isinstance(status, dict) or status.get('config_digest') != identity or status.get('running') is not True:
                    raise ControlError('Start the proxy with the selected configuration to monitor it')
                return result
            if args.no_input:
                args.once = True
            return run(fetch, args, (OSError,))
        if args.command == 'optimize':
            from .optimization import review
            try:
                result = review(args, config, args.policy_revision)
            except FileExistsError:
                print('Revision already exists. Choose a new output filename.', file=sys.stderr)
                return 2
            except ValueError as exc:
                print(ascii(str(exc)), file=sys.stderr)
                return 2
            emit(result, args.json)
            return 0
        if args.command == 'baseline':
            from .baseline import capture

            try:
                result, code = capture(args, config, lambda *call: local_json(config, *call))
            except FileExistsError:
                print('Baseline output already exists. Choose a new filename.', file=sys.stderr)
                return 2
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            except KeyboardInterrupt:
                print('Baseline capture interrupted. Inspect the output and any running scenario.', file=sys.stderr)
                return 130
            emit(result, args.json)
            if not result['written'] and not args.json:
                print('To run this GET workload, repeat the command with --apply followed by the review token.')
            return code
        elif args.command == 'config':
            emit({'schema_version': 1, 'valid': True, 'config': config.model_dump()}, args.json)
        elif args.command == 'doctor':
            result = {'schema_version': 1, 'config_valid': True, 'python': sys.version.split()[0],
                      'forwarding_available': True, 'upstream_check': 'not_requested','limits':config.limits.model_dump(),
                      'session_check':'not_requested'}
            if args.session:
                session=local_json(config,'GET','/_autotuner/status')
                result.update(session_check='reachable',session_uptime_seconds=session.get('uptime_seconds'),
                              session_running=session.get('running'),session_limits=session.get('limits'))
            if args.connect:
                upstream = urlsplit(config.upstream)
                with socket.create_connection((upstream.hostname, upstream.port or (443 if upstream.scheme == 'https' else 80)), timeout=2):
                    pass
                result['upstream_check'] = 'tcp_reachable_tls_not_tested'
            emit(result, args.json)
        elif args.command=='dashboard':
            data=local_json(config,'GET','/_autotuner/status')
            if not supports(data,'dashboard') or not data.get('running'):
                raise ControlError('Start the updated proxy before opening its dashboard')
            url='http://'+config.listen+'/_autotuner/dashboard'
            if not args.no_browser and not args.json:
                if not webbrowser.open(url):print('Open the dashboard URL manually.',file=sys.stderr)
            emit(dict(schema_version=1,url=url,session_id=data.get('session_id')),args.json)
        elif args.command=='scenario':
            plan=load_scenario(args.file)
            if args.scenario_command=='preview':
                emit(plan.preview(),args.json)
                return 0
            if plan.preview()['writes'] and not args.allow_writes:
                raise ValueError('Use --allow-writes after reviewing scenario preview')
            identifier=str(uuid.uuid4())
            with ExitStack() as stack:
                output = stack.enter_context(Path(args.output).open('w' if args.force else 'x', encoding='utf-8')) if args.output else None
                if output is not None:
                    output.write(json.dumps(dict(schema_version=1,run_id=identifier,state='incomplete',note='Execution may have begun; inspect the proxy before retrying.'))+'\n')
                    output.flush()
                print(f"Scenario preview: {plan.preview()['requests']} requests, {plan.preview()['writes']} writes.",file=sys.stderr)
                print('Run ID: '+identifier+' (no automatic retry if the connection is lost).',file=sys.stderr)
                endpoint='/_autotuner/scenarios/jobs/'+identifier
                try:
                    report_data=local_json(config,'POST','/_autotuner/scenarios/run',
                        dict(scenario=plan.model_dump(exclude_unset=True),allow_writes=args.allow_writes,run_id=identifier))
                    while report_data.get('state')=='running':
                        time.sleep(.25)
                        report_data=local_json(config,'GET',endpoint)
                except KeyboardInterrupt:
                    print('Stopping after the current request; completed writes are retained.',file=sys.stderr)
                    report_data=local_json(config,'POST','/_autotuner/scenarios/stop',dict(run_id=identifier))
                    while report_data.get('state')=='running':
                        time.sleep(.25)
                        report_data=local_json(config,'GET',endpoint)
                if report_data.get('run_id')!=identifier or report_data.get('state') not in ('completed','stopped','failed','runtime_limit','interrupted'):
                    raise ControlError('Invalid scenario report')
                if output is not None:
                    output.seek(0)
                    output.write(json.dumps(report_data,indent=2)+'\n')
                    output.truncate()
                    print('Scenario report saved.',file=sys.stderr)
                else:emit(report_data,args.json)
                return 0 if report_data['state'] in ('completed','stopped') else 3
        elif args.command in ('report', 'sessions'):
            digest, _ = config_identity(config)
            if args.live:
                from urllib.parse import urlencode
                endpoint = '/_autotuner/' + args.command
                if args.command == 'report' and args.session:
                    endpoint += '?' + urlencode({'session': args.session})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                with control_open(opener,'http://' + config.listen + endpoint,config.listen,10) as response:
                    try:
                        data = json.load(response)
                    except ValueError:
                        raise ControlError('Invalid report response') from None
                if not isinstance(data, dict) or data.get('schema_version') != 1:
                    raise ControlError('Incompatible telemetry report')
                if args.command == 'report' and (data.get('config_digest') != digest or not isinstance(data.get('routes'), list) or
                    not isinstance(data.get('sampling'), dict) or
                    any(not isinstance(data.get(key), str) for key in ('tool_version', 'source_sha256', 'session_id'))):
                    raise ControlError('Report configuration mismatch')
                if args.command == 'sessions' and not isinstance(data.get('sessions'), list):
                    raise ControlError('Invalid sessions response')
            else:
                from contextlib import closing
                with closing(connect_readonly(config.metrics.storage_path)) as connection:
                    data = report(connection, digest, args.session) if args.command == 'report' else dict(schema_version=1, sessions=history(connection, digest))
            if args.command == 'sessions':
                emit(data, args.json)
            else:
                content = serialize_report(data, args.format)
                if args.output:
                    with Path(args.output).open('w' if args.force else 'x', encoding='utf-8', newline='') as output:
                        output.write(content)
                    print('Report saved.', file=sys.stderr)
                else:
                    sys.stdout.write(content)
        elif args.command=='cache':
            from urllib.parse import urlencode
            endpoint='/_autotuner/cache/'+args.cache_command
            if args.cache_command=='explain':
                if not args.route.startswith('/') or len(args.route)>1024 or any(c in args.route for c in ('?','#','\x00')):
                    raise ValueError('Expected a route path')
                endpoint+='?'+urlencode({'path':args.route})
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
            request=urllib.request.Request('http://'+config.listen+endpoint,method='POST' if args.cache_command=='clear' else 'GET')
            with control_open(opener,request,config.listen,2) as response:
                try:data=json.load(response)
                except ValueError:raise ControlError('Invalid cache control response') from None
            if not isinstance(data,dict) or data.get('schema_version')!=1 or not isinstance(data.get('session_id'),str) or not isinstance(data.get('cache'),dict):
                raise ControlError('Incompatible cache control session')
            emit(data,args.json)
        elif args.command in ('status', 'endpoints', 'insights'):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            endpoint = '/_autotuner/status'
            if args.command in ('endpoints', 'insights'):
                if not 1 <= args.window <= 3600:
                    raise ValueError('Invalid metrics window')
                endpoint = ('/_autotuner/insights' if args.command == 'insights' else '/_autotuner/metrics') + '?window=' + str(args.window)
            with control_open(opener,'http://' + config.listen + endpoint,config.listen,2) as response:
                try:
                    data = json.load(response)
                except ValueError as exc:
                    raise ControlError('Invalid local session response') from exc
            if (not isinstance(data, dict) or data.get('schema_version') != 1 or
                not isinstance(data.get('session_id'), str) or
                (args.command == 'status' and not supports(data,'proxy')) or
                (args.command == 'endpoints' and not isinstance(data.get('routes'), dict)) or
                (args.command == 'insights' and (not isinstance(data.get('findings'), list) or not isinstance(data.get('cache_suitability'), list) or data.get('recommendations_applied') is not False))):
                raise ControlError('Listener did not return a compatible control session')
            data.pop('phase',None)
            emit(data, args.json)
        else:
            if args.shutdown_timeout < 1:
                raise ValueError('Shutdown timeout must be positive')
            if args.check:
                emit({'schema_version': 1, 'valid': True, 'forwarding_enabled': False, 'forwarding_available': True, 'config': config.model_dump()}, args.json)
                return 0
            if args.json:
                raise ValueError('Use serve --check --json or status --json; foreground serve emits diagnostics on stderr')
            import uvicorn
            host, port = config.listen.rsplit(':', 1)
            print('Proxy enabled; fixed-TTL caching is opt-in through cache mode and explicit cache rules. Adaptive tuning requires adaptive mode and approved adaptive rules.',file=sys.stderr)
            logging.basicConfig(level=logging.INFO, stream=sys.stderr)
            logging.getLogger('httpx').setLevel(logging.WARNING)
            logging.getLogger('httpcore').setLevel(logging.WARNING)
            print('Open the terminal monitor with autotuner monitor using this configuration.',file=sys.stderr)
            uvicorn.run(ControlSession(config), host=host.strip('[]'), port=int(port),
                        log_config=None, access_log=False, server_header=False, date_header=False, timeout_graceful_shutdown=args.shutdown_timeout)
        return 0
    except ControlError as exc:
        print(str(exc),file=sys.stderr)
        return 1
    except RuntimeError:
        print('Isolated benchmark failed; check local socket permissions and the installed package. No qualification result was obtained.',file=sys.stderr)
        return 1
    except ValidationError as exc:
        locations = ['.'.join(map(str, item['loc'])) or 'configuration' for item in exc.errors(include_input=False)]
        print('Invalid configuration fields: ' + ', '.join(locations), file=sys.stderr)
        return 2
    except MissingConfiguration as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, FileExistsError, FileNotFoundError, UnicodeError):
        print('Invalid configuration or path; check the file, arguments and overwrite option.', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except OSError:
        print('Operation failed: check permissions, upstream connectivity or the running local session.', file=sys.stderr)
        return 1


def entrypoint():
    raise SystemExit(main())
