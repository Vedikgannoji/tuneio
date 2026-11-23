"""Small, line-oriented terminal workspace; all actions use explicit argv lists."""
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
import json
import os
from pathlib import Path
import sys

from ._version import __version__
from .terminal_ui import Keyboard, Theme, clip, terminal_size

MAX_INPUT=4096
PLANNED=()


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
        Action('3','Run proxy in this terminal',('serve',),'listen','serve'),
        Action('4','Offline diagnostics',('doctor',),'read_local'),
        Action('5','Check upstream TCP connectivity',('doctor','--connect'),'connect_upstream'),
        Action('6','Discover project API operations',('discover',),'read_local','discover'),
        Action('7','Discover and set up this project',('setup',),'review_config','setup'),
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
        Action('2','Show stored telemetry JSON',('report','--format','json'),'read_local'),
        Action('3','Export stored telemetry JSON',('report','--format','json'),'write_export','export'),
        Action('4','Export live telemetry JSON',('report','--live','--format','json'),'query_proxy_and_export','export'),
        Action('5','Compare baseline and optimized captures',('compare',),'read_and_export','compare'),
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

    def say(self,text=''):
        print(text,file=self.stdout)

    def ask(self,prompt):
        self.stdout.write(prompt);self.stdout.flush()
        line=self.stdin.readline(MAX_INPUT+1)
        if not line:raise EOFError()
        if len(line)>MAX_INPUT:raise InputTooLong()
        return line.strip()

    def confirm(self,prompt):
        return self.ask(prompt+' [y/N]: ').lower() in ('y','yes')

    def help(self):
        self.say('Select a number or letter, then press Enter. b returns; q exits; Ctrl+C exits.')
        self.say('Entry is offline. Session actions query a proxy already running with the selected config.')
        self.say('Create config, then run autotuner serve --config YOUR_CONFIG in another terminal for session actions.')
        self.say('The proxy routes traffic to your upstream; installing Autotuner alone does not connect your API.')
        self.say('Fast origins can correctly remain uncached. Cache rules and invalidation need explicit configuration.')
        self.say('Monitor shows live traffic and tuning. Use Reports to compare two matched read captures.')

    def structured(self, args):
        output = StringIO()
        with redirect_stdout(output):
            result = self.invoke(['--no-input', *args, '--json'])
        if result != 0:
            self.say(f'Action did not complete (exit {result}). Check the diagnostic above.')
            return None
        return json.loads(output.getvalue())

    def review(self, args, *, pick_config=False):
        preview = self.structured(args)
        if preview is None:
            return
        for key in ('upstream','proxy','listen','selected','policy','previous_policy','workload','reason',
                    'requirements','evidence','activation','limits','activation_boundary','output'):
            if key in preview:
                item = preview[key]
                value = json.dumps(item, indent=2) if isinstance(item, (list, dict)) else display(item)
                self.say(self.theme.paint('accent', key.replace('_', ' ').capitalize()))
                self.say(value)
        if not self.confirm('Apply this exact preview?'):
            return
        if self.invoke(['--no-input', *args, '--apply', preview['review_token']]) == 0:
            if pick_config:
                self.config = selected_path(preview['output'], self.project)
                self.say('Selected ' + display(self.config) + ' for the next proxy start.')
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
            while True:
                dependency = self.ask('Additional write as METHOD /path (blank finishes, b cancels): ')
                if dependency.lower() == 'b':
                    return
                if not dependency:
                    break
                fields = dependency.split()
                if len(fields) != 2 or fields[0].upper() not in ('POST','PUT','PATCH','DELETE') or not fields[1].startswith('/'):
                    raise ValueError('Expected METHOD /path')
                args += ['--invalidate', fields[0].upper(), fields[1]]
            args += ['--route='+route]
            if self.confirm('Attest that this resource has no dependent writes?'):
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
        stage = self.ask('Stage [baseline] or optimized (b cancels): ').lower() or 'baseline'
        if stage == 'b':
            return
        route = self.ask('Concrete GET path (b cancels): ')
        if not route or route.lower() == 'b':
            return
        state = self.ask('Application build/test-data identifier (b cancels): ')
        if not state or state.lower() == 'b':
            return
        environment = self.ask('Target environment [local] or staging (b cancels): ').lower() or 'local'
        if environment == 'b':
            return
        repeat = self.ask('Measured requests [10] (b cancels): ') or '10'
        if repeat.lower() == 'b':
            return
        warmup = self.ask('Warmup requests [0] (b cancels): ') or '0'
        if warmup.lower() == 'b':
            return
        output = self.ask('New capture JSON path (b cancels): ')
        if output and output.lower() != 'b':
            self.review([*args, '--stage='+stage, '--get='+route, '--app-state='+state,
                         '--environment='+environment, '--requests='+repeat, '--warmup='+warmup,
                         '--output='+str(selected_path(output,self.project))])

    def execute(self,action):
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
            self.invoke(['--no-input', *cmd])
            return
        if action.prompt == 'discover':
            code = self.invoke(['--no-input', 'discover', '--project', str(self.project)])
            if code == 0:
                self.say('To preview an observe config for these operations, run autotuner setup --help.')
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
        code=self.invoke(['--no-input',*args])
        self.say('Action completed.' if code==0 else f'Action did not complete (exit {code}). Check the diagnostic above.')

    def choose(self, group):
        if not self.theme.rich:
            return self.ask('Select: ').lower()
        options = ([(key, label) for key,(label,_) in GROUPS.items()] if group is None
                   else [(item.key, item.label) for item in GROUPS[group][1]])
        selected = 0
        with self.keyboard.active():
            while True:
                width, height = terminal_size()
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
                    choice = clip(f' {"›" if index == selected else " "}  {key:>2}   {label}  ', width)
                    style = 'selected' if index == selected else 'text'
                    top.append(self.theme.paint(style, self.theme.glyph(choice, choice.replace('›','>'))))
                help_text = 'p Project   c Config   h Help   q Quit' if group is None else 'b Back   h Help   q Quit'
                top.append(self.theme.paint('muted', clip(help_text, width)))
                self.theme.screen(top)
                key = self.keyboard.read(1)
                if key in ('up','k','down','j'):
                    selected = (selected + (-1 if key in ('up','k') else 1)) % len(options)
                elif key == 'enter':
                    return options[selected][0]
                elif key == 'back':
                    return 'b'
                elif key and key in {item[0] for item in options} | {'b','q','h','?','p','c','0'}:
                    return key

    def loop(self):
        group=None
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
                self.help()
                if self.theme.rich:
                    self.ask('Press Enter to return: ')
                continue
            if choice in ('b','0'):
                if group is None:return 0
                group=None;continue
            if group is None:
                if choice in GROUPS:group=choice;continue
                if choice in ('p','c'):
                    value=self.ask('Project directory (b cancels): ' if choice=='p' else 'Config path (b cancels): ')
                    if not value or value.lower()=='b':continue
                    try:
                        target=selected_path(value,self.project)
                        if choice=='p':
                            if not target.is_dir():raise ValueError()
                            self.project=target;self.config=target/'autotuner.json'
                        else:self.config=target
                    except (OSError,ValueError):self.say('Cannot select that path. Check the directory or file name.')
                    continue
            else:
                action=next((a for a in GROUPS[group][1] if a.key==choice),None)
                if action is not None:
                    try:self.execute(action)
                    except (OSError,ValueError):self.say('Cannot use that input or path. No automatic retry was made.')
                    if self.theme.rich and action.prompt != 'monitor':
                        self.ask('Press Enter to return: ')
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
