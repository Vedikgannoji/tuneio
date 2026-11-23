"""A keyboard-operated view of the running loopback session."""
import sys
from time import monotonic

from .terminal_ui import Keyboard, Theme, clip, terminal_size

TABS = ('Endpoints', 'Cache', 'Findings', 'Resources')
INTERVALS = (1, 2, 5, 10)


def ms(value):
    return '—' if value is None else f'{value:.2f} ms'


def metrics_line(stats):
    count = stats['count']
    errors = f"{stats['error_rate']:.1%}" if count else '—'
    return (f"{count:,} requests  ·  {stats['requests_per_second']:.2f} req/s  ·  "
            f"p95 {ms(stats['p95_ms'])}  ·  {errors} errors  ·  {stats['cache_hits']:,} hits")


def tab_rows(data, tab, pattern):
    if tab == 0:
        return [
            operation + '  |  ' + metrics_line(stats)
            for operation, stats in sorted(data['metrics']['routes'].items(), key=lambda item: -item[1]['count'])
            if pattern in operation.casefold()
        ]
    if tab == 1:
        rules = data['policies']
        decisions = reversed(data['decisions'])
        return [
            f"{rule['id']} {rule['match']}   {rule['state']}   {rule['detail']}"
            for rule in rules if pattern in (rule['id']+' '+rule['match']).casefold()
        ] + [
            f"{event['rule_id']}   {event['action']}   {event['reason']}   TTL {event['new_ttl_seconds']:g}s"
            for event in decisions if pattern in event['rule_id'].casefold()
        ]
    if tab == 2:
        return [f"{row['operation'] or 'Session'}   {row['code']}   {row['recommendation']}"
                for row in data['findings'] if pattern in (row['operation'] or 'session').casefold()]
    status = data['status']
    return [f'{group} / {key.replace("_", " ")}   {value if value is not None else "unavailable"}'
            for group in ('runtime', 'transport', 'storage', 'limits') for key, value in status[group].items()]


class Monitor:
    def __init__(self, fetch, errors, theme, window, interval, pattern, *, clock=monotonic):
        self.fetch, self.errors, self.theme, self.clock = fetch, errors, theme, clock
        self.window, self.interval, self.pattern = window, interval, pattern.casefold()
        self.tab = self.index = 0
        self.data = self.last_success = None
        self.error = False
        self.paused = False

    def update(self):
        try:
            self.data = self.fetch(self.window)
        except self.errors:
            self.error = True
            return
        self.error = False
        self.last_success = self.clock()

    def lines(self, width, height):
        theme = self.theme
        draw = lambda style, message: theme.paint(style, theme.glyph(clip(message, width), clip(
            message.translate(str.maketrans({'—':'-', '–':'-', '·':'|', '─':'-', '█':'#', '░':'.'})), width)))
        age = self.clock() - self.last_success if self.last_success is not None else None
        stale = age is not None and age > max(3, 2*self.interval)
        if self.error:
            text = 'DISCONNECTED' if age is None else 'DISCONNECTED / LAST SNAPSHOT'
        elif self.paused:
            text = 'PAUSED' + (' / STALE' if stale else '')
        else:
            text = 'STALE' if stale else 'LIVE'
        if self.data and self.data['status']['storage'].get('state') == 'degraded':
            text += ' / STORAGE DEGRADED'
        if self.data and self.data['metrics'].get('window_may_be_incomplete'):
            text += ' / PARTIAL WINDOW'
        if age is not None:
            text += f' · {age:.1f}s ago'
        header = [draw('title', f'AUTOTUNER / LIVE MONITOR   {self.window}s window'),
                  draw('warn' if self.error or stale or self.paused else 'ok', text)]
        footer = draw('muted', 'Tab tabs  j/k scroll  / filter  Space pause  r refresh  i interval  q exit')
        if height < 8 or width < 26:
            return header[:max(1, height-1)] + [draw('muted', 'Resize · q exits')]
        header.append(' '.join(draw('selected' if i==self.tab else 'accent', f' {i+1} {label} ')
                               for i,label in enumerate(TABS)) if width >= 57 else draw('accent', TABS[self.tab]))
        if self.data is None:
            header.append(draw('muted', 'Start the proxy with the selected config, then press r.'))
            return header + [footer]
        status, metrics = self.data['status'], self.data['metrics']
        header.extend([
            draw('text', f"{status['effective_mode'].upper()} · {status['session_id'][:8]} · refresh {self.interval:g}s"),
            draw('text', metrics_line(metrics['summary'])),
        ])
        if self.tab == 1:
            cache = status['cache']
            bar_length = min(26, width//3)
            occupied = min(bar_length, int(bar_length*cache['accounted_bytes']/cache['byte_budget']))
            header.append(draw('accent', f"{'█'*occupied}{'░'*(bar_length-occupied)}   "
                               f"{cache['entries']} entries · {cache['accounted_bytes']:,} B"))
        rows = tab_rows(self.data, self.tab, self.pattern)
        page = max(1, min(100, height-len(header)-3))
        self.index = min(self.index, max(0, len(rows)-page))
        header.append(draw('border', '─'*width))
        visible = rows[self.index:self.index+page]
        body = [draw('text', row) for row in visible] or [draw('muted', 'No matching observations.')]
        label = f'{self.index+1}–{self.index+len(visible)} of {len(rows)}' if visible else '0 rows'
        return (header + body + [draw('muted', label + (' · filter ' + self.pattern if self.pattern else '')), footer])[:height]

    def input(self, key):
        if key in ('tab','right','left'):
            self.tab = (self.tab + (-1 if key=='left' else 1)) % len(TABS)
            self.index = 0
        elif key in ('1','2','3','4'):
            self.tab, self.index = int(key)-1, 0
        elif key in ('j','down','k','up'):
            self.index = max(0, self.index + (1 if key in ('j','down') else -1))
        elif key in ('space',' '):
            self.paused = not self.paused
        elif key == 'i':
            self.interval = next((item for item in INTERVALS if item > self.interval), INTERVALS[0])
        elif key == 'r':
            self.update()

    def run(self, stdin, stdout, keyboard):
        due = 0
        while True:
            if not self.paused and self.clock() >= due:
                self.update()
                due = self.clock() + self.interval
            if self.theme.rich:
                self.theme.screen(self.lines(*terminal_size()))
                with keyboard.active():
                    key = keyboard.read(1 if self.paused else max(0, min(1, due-self.clock())))
            else:
                self.theme.screen(self.lines(*terminal_size()))
                stdout.write('Monitor key (Enter refreshes): ')
                stdout.flush()
                line = stdin.readline(4097)
                if not line or len(line) > 4096:
                    return
                key = line.strip().lower() or 'r'
            if key in ('q','back'):
                return
            if key == '/':
                stdout.write('Operation or rule filter (blank clears): ')
                stdout.flush()
                line = stdin.readline(4097)
                if not line or len(line) > 4096:
                    return
                self.pattern, self.index = line.strip().casefold(), 0
            self.input(key)
            if key in (' ', 'space', 'i', 'r'):
                due = self.clock() + (0 if key in (' ', 'space') and not self.paused else self.interval)


def run(fetch, args, errors, *, stdin=None, stdout=None):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if args.output or args.json or args.once or not stdin.isatty() or not stdout.isatty():
        data = fetch(args.window)
        if args.output:
            from .artifacts import write_new
            write_new(args.output, data)
        if args.json:
            import json
            print(json.dumps(data, indent=2), file=stdout)
        elif not args.output:
            print(metrics_line(data['metrics']['summary']), file=stdout)
            for row in tab_rows(data, 0, args.filter.casefold()):
                print(clip(row, 1200), file=stdout)
        return 0
    view = Monitor(fetch, errors, Theme(stdout, plain=args.plain), args.window, args.interval, args.filter)
    try:
        view.run(stdin, stdout, Keyboard(stdin))
    except EOFError:
        return 0
    except KeyboardInterrupt:
        return 130
    return 0
