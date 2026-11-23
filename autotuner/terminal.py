"""Small, line-oriented terminal workspace; all actions use explicit argv lists."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
import json
import logging
import os
from pathlib import Path
import sys

from ._version import __version__
from .terminal_reports import capture_view, details_table, report_view, review_lines, summary
from .terminal_ui import Keyboard, Panel, Theme, clip, terminal_size

MAX_INPUT=4096
PLANNED=()
SUMMARY_COMMANDS = {'config','doctor','status','endpoints','insights','cache','optimize','sessions','report','baseline'}


class InputTooLong(Exception):
    """Abort instead of interpreting the rest of an oversized line as commands."""


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    command: tuple[str,...]
    effect: str
    prompt: str=''


GROUPS={
    '1':('Setup and configuration',(
        Action('1','Validate selected configuration',('config','check'),'read_local'),
        Action('2','Create observe configuration',('init',),'write_config','create'),
        Action('3','Start or restart workspace proxy',('serve',),'listen','serve'),
        Action('4','Offline diagnostics',('doctor',),'read_local'),
        Action('5','Check upstream TCP connectivity',('doctor','--connect'),'connect_upstream'),
        Action('6','Discover project API operations',('discover',),'read_local','discover'),
        Action('7','Discover and set up this project',('setup',),'review_config','setup'),
        Action('8','Stop workspace proxy',('serve',),'stop_owned_proxy','stop_proxy'),
        Action('9','Start or stop demo API',('demo',),'control_demo','demo'),
    )),
    '2':('Session and insights',(
        Action('1','Current session status',('status',),'query_proxy'),
        Action('2','Endpoint metrics',('endpoints',),'query_proxy'),
        Action('3','Performance insights',('insights',),'query_proxy'),
        Action('4','Save passive baseline',('baseline',),'query_proxy_and_export','export'),
        Action('5','Capture matched GET workload',('baseline',),'review_read_workload','baseline'),
        Action('6','Open live monitor',('monitor',),'query_proxy','monitor'),
    )),
    '3':('Cache and policies',(
        Action('1','Explain a route policy',('cache','explain'),'query_proxy','route'),
        Action('2','Clear stored responses',('cache','clear'),'clear_cache','clear'),
        Action('3','Inspect current file policy',('optimize','inspect'),'read_local'),
        Action('4','Approve adaptive caching for a GET',('optimize','enable'),'review_config','policy'),
        Action('5','Pause cache serving',('optimize','pause'),'review_config','policy'),
        Action('6','Disable caching',('optimize','disable'),'review_config','policy'),
        Action('7','Roll back a policy revision',('optimize','rollback'),'review_config','policy'),
    )),
    '4':('Reports and history',(
        Action('1','List stored sessions',('sessions',),'read_local'),
        Action('2','View stored telemetry summary',('report','--format','json'),'read_local'),
        Action('3','Export stored telemetry JSON',('report','--format','json'),'write_export','export'),
        Action('4','Export live telemetry JSON',('report','--live','--format','json'),'query_proxy_and_export','export'),
        Action('5','Compare baseline and optimized captures',('compare',),'read_and_export','compare'),
        Action('6','Open a saved capture or comparison',('baseline',),'read_local','saved_capture'),
        Action('7','View a stored session by ID',('report','--format','json'),'read_local','stored_session'),
    )),
}


def display(value):
    """Keep project-controlled text from inserting terminal control sequences."""
    return ''.join(c if c.isprintable() else ' ' for c in str(value))[:1024]


def selected_path(value,root):
    if not value or len(value)>MAX_INPUT or any(not c.isprintable() for c in value):
        raise ValueError('Invalid path')
    path=Path(value).expanduser()
    return (path if path.is_absolute() else root/path).resolve()


def manifest(project,config):
    return dict(schema_version=1,tool_version=__version__,interactive=False,
        project=str(project),config=str(config),read_only_on_entry=True,
        groups=[dict(key=key,label=label,actions=[dict(key=a.key,label=a.label,command=list(a.command),effect=a.effect)
            for a in actions]) for key,(label,actions) in GROUPS.items()],planned_features=list(PLANNED))


class Workspace:
    def __init__(self,project,config,invoke,stdin,stdout,plain=False):
        self.project,self.config,self.invoke=project,config,invoke
        self.stdin,self.stdout=stdin,stdout
        self.theme = Theme(stdout, plain=plain)
        self.keyboard = Keyboard(stdin)
        self.panel = None
        self.proxy = self.demo = None

    def say(self,text=''):
        if self.panel is None:
            print(text,file=self.stdout)
        else:
            self.panel.add(text)

    def view(self):
        panel = self.panel
        with self.keyboard.active():
            while True:
                width, height = terminal_size()
                panel.draw(self.theme, width, height)
                key = self.keyboard.read(1)
                if key in ('enter','back','b','q'):
                    return
                if key == 'd' and panel.details:
                    panel.show_details = not panel.show_details
                    panel.offset = 0
                else:
                    panel.move(key, height)

    def ask(self,prompt):
        if self.panel is not None:
            panel = self.panel
            panel.offset = sys.maxsize
            value = ''
            with self.keyboard.active():
                while True:
                    width, height = terminal_size()
                    panel.draw(self.theme, width, height, prompt=prompt, value=value)
                    key = self.keyboard.read(1, text=True)
                    if key == 'enter':
                        panel.add(prompt.strip() + ' ' + value)
                        return value.strip()
                    if key == 'back':
                        return 'b'
                    if key == 'erase':
                        value = value[:-1]
                    elif key and len(key) == 1 and key.isprintable():
                        if len(value) >= MAX_INPUT:
                            raise InputTooLong()
                        value += key
                    else:
                        panel.move(key, height)
        self.stdout.write(prompt);self.stdout.flush()
        line=self.stdin.readline(MAX_INPUT+1)
        if not line:raise EOFError()
        if len(line)>MAX_INPUT:raise InputTooLong()
        return line.strip()

    def select(self, prompt, options, *, default=0):
        shortcuts = {str(index + 1): index for index in range(len(options))}
        if self.panel is None:
            self.say(prompt)
            for key, index in shortcuts.items():
                suffix = ' (default)' if index == default else ''
                self.say(f'{key}. {options[index]}{suffix}')
            while True:
                answer = self.ask('Select a number (b cancels): ').lower()
                if answer == 'b':
                    return None
                if not answer:
                    return default
                if answer in shortcuts:
                    return shortcuts[answer]
                self.say('Choose a listed number.')

        selected = default
        self.panel.offset = sys.maxsize
        with self.keyboard.active():
            while True:
                width, height = terminal_size()
                self.panel.draw(self.theme, width, height, prompt=prompt, choices=options, selected=selected)
                key = self.keyboard.read(1)
                if key in ('back', 'b'):
                    return None
                if key == 'enter' or key in shortcuts:
                    selected = shortcuts.get(key, selected)
                    self.say(f'{prompt}  {options[selected]}')
                    return selected
                if key in ('up', 'down'):
                    selected = (selected + (-1 if key == 'up' else 1)) % len(options)
                else:
                    self.panel.move(key, height)

    def confirm(self, prompt):
        return self.select(prompt, ('Yes', 'No'), default=1) == 0

    def help(self):
        self.say('Press a number or use arrows and Enter to choose. In plain mode, press Enter after the number.')
        self.say('b goes back. q or Ctrl+C exits.')
        self.say('Entry is offline. Session actions query the proxy running with the selected config.')
        self.say('Use Setup and configuration to start the proxy. The workspace stops its own services on exit.')
        self.say('The proxy routes traffic to your upstream; installing Autotuner alone does not connect your API.')
        self.say('Fast origins can correctly remain uncached. Cache rules and invalidation need explicit configuration.')
        self.say('Monitor shows live traffic and tuning. Use Reports to compare two matched read captures.')

    def run_json(self, args):
        output = StringIO()
        if self.panel is None:
            with redirect_stdout(output):
                code = self.invoke(['--no-input', *args, '--json'])
        else:
            code = self.invoke_in_panel(['--no-input', *args, '--json'], output)
        raw = output.getvalue()
        return code, json.loads(raw) if raw else None

    def invoke_in_panel(self, argv, output=None):
        if self.panel is None:
            return self.invoke(argv)
        diagnostic = StringIO()
        self.panel.draw(self.theme, *terminal_size())
        try:
            with redirect_stdout(output if output is not None else diagnostic), redirect_stderr(diagnostic):
                return self.invoke(argv)
        finally:
            # A foreground server can initialize logging while stderr is redirected.
            for handler in logging.getLogger().handlers[:]:
                if getattr(handler, 'stream', None) is diagnostic:
                    logging.getLogger().removeHandler(handler)
                    handler.close()
            if diagnostic.getvalue().strip():
                self.render([diagnostic.getvalue().strip()])

    def show_result(self, command, data):
        if self.panel is None:
            self.render(summary(command, data))
            return
        for item in report_view(command, data):
            self.panel.add(item)
        self.panel.add(details_table(data), detail=True)

    def show_capture(self, saved):
        self.render(summary('baseline', saved))
        if self.panel is not None and saved.get('output'):
            self.open_capture(Path(saved['output']))

    def open_capture(self, path):
        from .artifacts import read_json
        data, _ = read_json(path, 32*1024*1024)
        kind = data.get('kind')
        if data.get('schema_version') != 1 or (kind != 'matched-read-comparison' and
                (kind not in ('active','passive') or 'baseline_id' not in data)):
            self.say('Select a capture or comparison saved by Autotuner.')
            return
        try:
            lines = list(report_view('compare', data) if kind == 'matched-read-comparison' else capture_view(data))
        except (KeyError, TypeError, AttributeError, ValueError):
            self.say('The report is incomplete or has an invalid structure.')
            return
        for item in lines:
            if self.panel is not None:
                self.panel.add(item)
            elif isinstance(item, str):
                self.render([item])
            else:
                self.render(item.lines(terminal_size()[0]))
        if self.panel is not None:
            self.panel.add(details_table(data), detail=True)

    def stop_service(self, name):
        child = getattr(self, name)
        if child is not None:
            setattr(self, name, None)
            child.close()

    def start_proxy(self, args):
        from .benchmarking import Child

        checked = self.structured(['config','check',*args[1:]])
        if checked is None:
            return
        cfg = checked['config']
        self.say(f"Proxy {cfg['listen']} forwards to {cfg['upstream']} in {cfg['mode']} mode.")
        if not self.confirm('Start the proxy with this config?' if self.proxy is None else 'Restart the workspace proxy with this config?'):
            return
        self.stop_service('proxy')
        self.say('Starting the proxy...')
        self.panel.draw(self.theme, *terminal_size())
        try:
            self.proxy = Child('workspace-proxy', self.config)
        except (RuntimeError, OSError):
            self.say('Proxy startup failed. Check the selected config and whether its port is already in use.')
        else:
            self.say(f"Proxy running at {cfg['listen']}. Open Session and insights for live metrics.")

    def toggle_demo(self):
        if self.demo is not None:
            if self.confirm('Stop the demo and discard its in-memory data?'):
                self.stop_service('demo')
                self.say('Demo stopped.')
            return
        if not self.confirm('Start the disposable demo API on 127.0.0.1:3000?'):
            return
        from .benchmarking import Child

        self.say('Starting the demo API...')
        if self.panel is not None:
            self.panel.draw(self.theme, *terminal_size())
        try:
            self.demo = Child('demo', self.config, port=3000)
        except (RuntimeError, OSError):
            self.say('Demo startup failed. Check whether port 3000 is already in use.')
        else:
            self.say('Demo running on 127.0.0.1:3000. Start or restart the proxy with a config pointing there.')

    def structured(self, args):
        result, data = self.run_json(args)
        if result != 0:
            self.say(f'Action did not complete (exit {result}). Check the diagnostic above.')
            return None
        return data

    def render(self, lines):
        for line in lines:
            for part in line.split('\n'):
                safe = display(part)
                self.say(self.theme.glyph(safe, safe.encode('ascii', 'replace').decode('ascii')))

    def review(self, args, *, pick_config=False):
        preview = self.structured(args)
        if preview is None:
            return
        self.render(review_lines(preview))
        if not self.confirm('Apply this exact preview?'):
            return
        code, saved = self.run_json([*args, '--apply', preview['review_token']])
        if saved and args[0] == 'baseline':
            self.show_capture(saved)
        if code == 0:
            if pick_config:
                self.config = selected_path(preview['output'], self.project)
                self.render(['Saved and selected ' + str(self.config) + ' for the next proxy start.',
                             'Use Setup and configuration to start or restart the proxy.'])
        else:
            self.say('Apply did not complete. Check the diagnostic above.')

    def guided_setup(self):
        source = self.ask('OpenAPI JSON path (blank auto-discovers, b cancels): ')
        if source.lower() == 'b':
            return
        base = ['--project', str(self.project)]
        if source:
            base += ['--openapi='+source]
        data = self.structured(['discover', *base])
        if data is None:
            return
        for row in data['operations']:
            self.say(f"{row['id']:>4}  {display(row['method']+' '+row['path'])}")
        for warning in data['warnings']:
            self.say(display(warning))
        if not data['operations']:
            return
        entries = self.ask('Operation IDs separated by spaces (b cancels): ')
        if entries.lower() == 'b':
            return
        ids = entries.split()
        if not ids or any(not item.isascii() or not item.isdecimal() for item in ids):
            raise ValueError('Invalid operation IDs')
        upstream = self.ask('Upstream URL [http://127.0.0.1:3000] (b cancels): ')
        if upstream.lower() == 'b':
            return
        output = self.ask('New project-root config filename [autotuner.json] (b cancels): ')
        if output.lower() == 'b':
            return
        selection = ['--select='+item for item in ids]
        self.review(['setup', *base, *selection, '--upstream='+(upstream or 'http://127.0.0.1:3000'),
                     '--output='+(output or 'autotuner.json')], pick_config=True)

    def guided_policy(self, args, action):
        if action == 'enable':
            route = self.ask('Configured GET path/pattern (b cancels): ')
            if not route or route.lower() == 'b':
                return
            self.say('Configured write operations are included as invalidation dependencies.')
            methods = ('POST', 'PUT', 'PATCH', 'DELETE')
            while True:
                method = self.select('Add an invalidating write', (*methods, 'Done adding writes'), default=len(methods))
                if method is None:
                    return
                if method == len(methods):
                    break
                dependency = self.ask(f'{methods[method]} path (b cancels): ')
                if dependency.lower() == 'b':
                    return
                if not dependency.startswith('/') or any(char.isspace() for char in dependency):
                    raise ValueError('Expected a write path starting with /')
                args += ['--invalidate', methods[method], dependency]
            args += ['--route='+route]
            if '--invalidate' not in args and self.confirm('Attest that this resource has no dependent writes?'):
                args += ['--read-only']
            baseline = self.ask('Optional baseline JSON for review evidence (blank skips, b cancels): ')
            if baseline.lower() == 'b':
                return
            if baseline:
                args += ['--baseline='+str(selected_path(baseline, self.project))]
        output = self.ask('New revision JSON path (b cancels): ')
        if output and output.lower() != 'b':
            self.review([*args,'--output='+str(selected_path(output, self.project))], pick_config=True)

    def guided_baseline(self, args):
        stages = ('Baseline', 'Optimized')
        stage = self.select('Capture stage', stages)
        if stage is None:
            return
        route = self.ask('Concrete GET path (b cancels): ')
        if not route or route.lower() == 'b':
            return
        state = self.ask('Application build/test-data identifier (b cancels): ')
        if not state or state.lower() == 'b':
            return
        environments = ('Local', 'Staging')
        environment = self.select('Target environment', environments)
        if environment is None:
            return
        repeat = self.ask('Measured requests [10] (b cancels): ') or '10'
        if repeat.lower() == 'b':
            return
        warmup = self.ask('Warmup requests [0] (b cancels): ') or '0'
        if warmup.lower() == 'b':
            return
        output = self.ask('New capture JSON path (b cancels): ')
        if output and output.lower() != 'b':
            self.review([*args, '--stage='+stages[stage].lower(), '--get='+route, '--app-state='+state,
                         '--environment='+environments[environment].lower(), '--requests='+repeat, '--warmup='+warmup,
                         '--output='+str(selected_path(output,self.project))])

    def execute(self,action):
        if action.prompt == 'saved_capture':
            path = self.ask('Saved report path (b cancels): ')
            if path and path.lower() != 'b':
                self.open_capture(selected_path(path, self.project))
            return
        if action.prompt == 'demo':
            self.toggle_demo()
            return
        if action.prompt == 'stop_proxy':
            if self.proxy is None:
                self.say('There is no proxy started by this workspace.')
            elif self.confirm('Stop the workspace proxy?'):
                self.stop_service('proxy')
                self.say('Workspace proxy stopped.')
            return
        if action.prompt == 'setup':
            self.guided_setup()
            return
        if action.prompt == 'compare':
            before = self.ask('Baseline JSON (b cancels): ')
            if not before or before.lower() == 'b':
                return
            after = self.ask('Optimized JSON (b cancels): ')
            if not after or after.lower() == 'b':
                return
            output = self.ask('New comparison JSON path (blank prints only, b cancels): ')
            if output.lower() == 'b':
                return
            cmd = ['compare', str(selected_path(before,self.project)), str(selected_path(after,self.project))]
            if output:
                cmd += ['--output='+str(selected_path(output,self.project))]
            if self.panel is None:
                code = self.invoke(['--no-input', *cmd])
            else:
                code, result = self.run_json(cmd)
                if result:
                    self.show_result('compare', result)
            if code in (0, 3) and output:
                self.render([f'Comparison saved to {selected_path(output, self.project)}'])
            return
        if action.prompt == 'discover':
            code = self.invoke_in_panel(['--no-input', 'discover', '--project', str(self.project)])
            if code == 0:
                self.say('Use Discover and set up this project to preview an observe configuration.')
            else:
                self.say(f'Action did not complete (exit {code}). Check the diagnostic above.')
            return
        args=[*action.command,'--config',str(self.config)]
        if action.prompt == 'policy':
            self.guided_policy(args, action.command[-1])
            return
        if action.prompt == 'baseline':
            self.guided_baseline(args)
            return
        if action.prompt == 'monitor':
            self.invoke([*args] if self.theme.rich else [*args, '--plain'])
            return
        if action.prompt=='create':
            upstream=self.ask('Upstream URL [http://127.0.0.1:3000] (b cancels): ')
            if upstream.lower()=='b':return
            args+=['--upstream='+ (upstream or 'http://127.0.0.1:3000')]
            if not self.confirm('Create an observe configuration at '+display(self.config)+' without overwriting?'):return
        elif action.prompt=='serve':
            if self.panel is not None:
                self.start_proxy(args)
                return
            if not self.confirm('Run the proxy here using the selected config? Ctrl+C stops it and returns'):return
        elif action.prompt=='clear':
            if not self.confirm('Remove stored cache responses? Configured rules are retained'):return
        elif action.prompt=='route':
            route=self.ask('Route path, for example /api/products (b cancels): ')
            if route.lower()=='b':return
            if (not route.startswith('/') or len(route)>1024 or any(c in route for c in ('?','#'))
                    or any(not c.isprintable() for c in route)):
                self.say('Enter a route path without a query, fragment or control characters.');return
            args.append(route)
        elif action.prompt=='export':
            target=self.ask('Export file path (b cancels): ')
            if not target or target.lower()=='b':return
            args+=['--output',str(selected_path(target,self.project))]
        elif action.prompt == 'stored_session':
            session = self.ask('Session ID from List stored sessions (b cancels): ')
            if not session or session.lower() == 'b':
                return
            args += ['--session='+session]
        if args[0] in SUMMARY_COMMANDS and not (args[0] == 'report' and action.prompt == 'export'):
            code, data = self.run_json(args)
            if data:
                if args[0] == 'baseline':
                    self.show_capture(data)
                else:
                    self.show_result(args[0], data)
        else:
            code = self.invoke_in_panel(['--no-input', *args])
            data = None
        if code != 0:
            self.say(f'Action did not complete (exit {code}). Check the diagnostic above.')
        elif args[0] == 'report' and action.prompt == 'export':
            self.render([f'Report saved to {args[-1]}'])
        elif not data:
            self.say('Action completed.')

    def choose(self, group):
        if not self.theme.rich:
            return self.ask('Select: ').lower()
        options = ([(key, label) for key,(label,_) in GROUPS.items()] if group is None
                   else [(item.key, item.label) for item in GROUPS[group][1]])
        selected = 0
        displayed = None
        with self.theme.fullscreen(), self.keyboard.active():
            while True:
                width, height = terminal_size()
                state = width, height, selected
                if state != displayed:
                    self.draw_menu(group, options, selected, width, height)
                    displayed = state
                key = self.keyboard.read(1)
                if key in ('up','k','down','j'):
                    selected = (selected + (-1 if key in ('up','k') else 1)) % len(options)
                elif key == 'enter':
                    return options[selected][0]
                elif key == 'back':
                    return 'b'
                elif key and key in {item[0] for item in options} | {'b','q','h','?','p','c','0'}:
                    return key

    def draw_menu(self, group, options, selected, width, height):
        top = self.theme.banner(width, height)
        top += [self.theme.paint('muted', clip(f'API PERFORMANCE WORKSPACE  /  {__version__}', width)), '',
                self.theme.paint('text', clip(f'Project  {self.project}', width)),
                self.theme.paint('muted', clip(f'Config   {self.config}', width)), '',
                self.theme.paint('accent', clip(GROUPS[group][0] if group else 'WORKSPACE', width))]
        top = top[:max(0, height-3)]
        slots = min(len(options), max(1, height-len(top)-2))
        start = min(max(0, selected-slots+1), len(options)-slots)
        for index in range(start, start+slots):
            key, label = options[index]
            marker = self.theme.glyph('\u203a', '>') if index == selected else ' '
            choice = clip(f' {marker}  {key:>2}   {label}  ', width)
            top.append(self.theme.paint('selected' if index == selected else 'text', choice))
        help_text = 'p Project   c Config   h Help   q Quit' if group is None else 'b Back   h Help   q Quit'
        top.append(self.theme.paint('muted', clip(help_text, width)))
        self.theme.screen(top)

    def loop(self):
        try:
            with self.theme.fullscreen():
                return self.navigate()
        finally:
            try:
                self.stop_service('proxy')
            finally:
                self.stop_service('demo')

    def navigate(self):
        group=None
        if not self.theme.rich:
            self.say('Autotuner '+__version__+' | Terminal workspace')
            self.say('No project scanning, API traffic or configuration changes occur on entry.')
        while True:
            if not self.theme.rich:
                self.say('\nProject: '+display(self.project)+'\nConfig: '+display(self.config))
            if group is None and not self.theme.rich:
                for key,(label,_) in GROUPS.items():self.say(key+'. '+label)
                self.say('p. Select project   c. Select config   h. Quick start   q. Quit')
            elif not self.theme.rich:
                label,actions=GROUPS[group];self.say(label)
                for action in actions:self.say(action.key+'. '+action.label)
                self.say('b. Back   h. Help   q. Quit')
            choice=self.choose(group)
            if choice in ('q','quit','exit'):return 0
            if choice in ('h','?'):
                if self.theme.rich:
                    self.panel = Panel('Help')
                self.help()
                if self.theme.rich:
                    self.view()
                    self.panel = None
                continue
            if choice in ('b','0'):
                if group is None:return 0
                group=None;continue
            if group is None:
                if choice in GROUPS:group=choice;continue
                if choice in ('p','c'):
                    if self.theme.rich:
                        self.panel = Panel('Select project' if choice=='p' else 'Select config')
                    value=self.ask('Project directory (b cancels): ' if choice=='p' else 'Config path (b cancels): ')
                    if not value or value.lower()=='b':
                        self.panel = None
                        continue
                    try:
                        target=selected_path(value,self.project)
                        if choice=='p':
                            if not target.is_dir():raise ValueError()
                            self.project=target;self.config=target/'autotuner.json'
                        else:self.config=target
                    except (OSError,ValueError):
                        self.say('Cannot select that path. Check the directory or file name.')
                        if self.theme.rich:
                            self.view()
                    self.panel = None
                    continue
            else:
                action=next((a for a in GROUPS[group][1] if a.key==choice),None)
                if action is not None:
                    if self.theme.rich and action.prompt != 'monitor':
                        self.panel = Panel(action.label)
                    try:self.execute(action)
                    except (OSError,ValueError):self.say('Cannot use that input or path. No automatic retry was made.')
                    if self.panel is not None:
                        self.panel.offset = 0
                        self.view()
                        self.panel = None
                    continue
            self.say('Choose a listed key. Use h for help.')


def run(*,invoke,project='.',config=None,as_json=False,no_input=False,plain=False,stdin=None,stdout=None,stderr=None):
    stdin=sys.stdin if stdin is None else stdin
    stdout=sys.stdout if stdout is None else stdout
    stderr=sys.stderr if stderr is None else stderr
    try:
        root=selected_path(str(project),Path.cwd())
        if not root.is_dir():raise ValueError()
        path=selected_path(str(config or os.environ.get('AUTOTUNER_CONFIG') or 'autotuner.json'),root)
        if as_json:
            print(json.dumps(manifest(root,path),indent=2),file=stdout);return 0
        if no_input or not (stdin.isatty() and stdout.isatty()):
            print('Interactive menu requires terminal input and output. Use autotuner menu --json or autotuner --help.',file=stderr)
            return 2
        return Workspace(root,path,invoke,stdin,stdout,plain).loop()
    except EOFError:
        print('\nTerminal workspace closed.',file=stdout);return 0
    except KeyboardInterrupt:
        print('\nTerminal workspace cancelled.',file=stderr);return 130
    except (OSError,ValueError,InputTooLong):
        print('Cannot open the selected project or terminal input. Check the path and input length.',file=stderr);return 2
