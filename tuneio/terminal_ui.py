"""Terminal palette, display sizing, and platform keyboard input."""
from contextlib import contextmanager
from contextvars import ContextVar
import codecs
from dataclasses import dataclass
from itertools import zip_longest
import os
from shutil import get_terminal_size
import sys
from textwrap import wrap
from time import monotonic, sleep
from unicodedata import east_asian_width, combining

PALETTE = {
    'title': '1;38;2;210;170;255',
    'accent': '1;38;2;191;139;255',
    'selected': '1;48;2;79;46;131;38;2;252;245;255',
    'text': '38;2;235;226;247',
    'muted': '38;2;183;168;205',
    'ok': '1;38;2;133;233;188',
    'warn': '1;38;2;255;198;136',
    'bad': '1;38;2;255;151;178',
    'border': '38;2;114;77;170',
}
GRADIENT = ((234,207,255), (221,183,255), (201,153,252), (177,123,245), (151,100,226))
PIXELS = {
    't': (8, 8, 30, 8, 7), 'u': (0, 0, 17, 17, 15), 'n': (0, 0, 30, 17, 17),
    'e': (0, 14, 31, 16, 14), 'i': (4, 0, 12, 4, 14), 'o': (0, 0, 14, 17, 14),
}
ACTIVE_SCREEN = ContextVar('active_terminal_screen', default=None)


def cell_width(value):
    return sum(0 if combining(char) else 2 if east_asian_width(char) in ('W', 'F') else 1 for char in value)


def wrapped(value, width):
    for part in wrap(value, width) or ['']:
        while cell_width(part) > width:
            prefix = clip(part, width)
            if not prefix:
                prefix, part = '?', part[1:]
            else:
                part = part[len(prefix):]
            yield prefix
        if part:
            yield part
        elif not value:
            yield ''


@dataclass(frozen=True)
class Table:
    headings: tuple[str, ...]
    rows: list[tuple[str, ...]]

    def lines(self, width):
        headings = [clip(label, width) for label in self.headings]
        rows = [tuple(clip(cell, 4096) for cell in row) for row in self.rows]
        gap = '  '
        sizes = [cell_width(label) for label in headings]
        for row in rows:
            sizes = [max(size, cell_width(cell)) for size, cell in zip(sizes, row)]
        available = width - len(gap) * (len(sizes)-1)
        minimum = [max(4, cell_width(label)) for label in headings]
        if sum(minimum) > available:
            for row in rows:
                for label, cell in zip(headings, row):
                    yield from wrapped(f'{label}  {cell}', width)
                yield ''
            return
        while sum(sizes) > available:
            index = max(range(len(sizes)), key=lambda i: sizes[i]-minimum[i])
            sizes[index] -= 1

        def line(cells):
            clipped = [clip(cell, size) for cell, size in zip(cells, sizes)]
            return gap.join(cell + ' '*(size-cell_width(cell)) for cell, size in zip(clipped, sizes)).rstrip()

        yield line(headings)
        yield line(tuple('-'*size for size in sizes))
        for row in rows:
            cells = [list(wrapped(cell, size)) or [''] for cell, size in zip(row, sizes)]
            for segment in zip_longest(*cells, fillvalue=''):
                yield line(segment)


class Panel:
    def __init__(self, title):
        self.title = title
        self.content = []
        self.details = []
        self.offset = 0
        self.show_details = False
        self._cache = None
        self._frame = None

    def add(self, item, *, detail=False):
        (self.details if detail else self.content).append(item)
        self._cache = None

    def draw(self, theme, width, height, *, prompt=None, value='', choices=(), selected=0):
        cache_key = width, self.show_details
        if self._cache is None or self._cache[0] != cache_key:
            rendered = []
            for item in self.details if self.show_details else self.content:
                if isinstance(item, Table):
                    rendered.extend((('accent' if i < 2 else 'text'), line)
                                    for i,line in enumerate(item.lines(width)))
                else:
                    for line in str(item).split('\n'):
                        rendered.extend(('text', part) for part in wrapped(clip(line, 4096), width))
            self._cache = cache_key, rendered
        body = self._cache[1]
        label = ' / DETAILS' if self.show_details else ''
        top = [('title', f'tuneio / {self.title}{label}'), ('border', '-'*width)]
        help_line = 'Up/Down scroll  PgUp/PgDn page'
        if prompt is None:
            footer = [('muted', help_line + ('  d details' if self.details else '') + '  Enter back')]
        elif choices:
            footer = [('accent', prompt)]
            slots = min(len(choices), max(1, height-4))
            start = min(max(0, selected-slots+1), len(choices)-slots)
            footer.extend(('selected' if index == selected else 'text', f'{index + 1}  {choices[index]}')
                          for index in range(start, start+slots))
            footer.append(('muted', f'1-{len(choices)} select  Up/Down move  Enter confirm  Esc cancel'))
        else:
            tail = max(1, width-2)
            suffix = value[-tail:]
            while cell_width(suffix) > tail:
                suffix = suffix[1:]
            footer = [('accent', prompt), ('selected', '> ' + suffix),
                      ('muted', help_line + '  Enter submit  Esc cancel')]
        page = max(0, height - len(top) - len(footer))
        self.offset = min(max(0, self.offset), max(0, len(body)-page))
        rows = top + body[self.offset:self.offset+page] + footer
        if len(footer) + len(top) > height:
            rows = rows[-height:]
        frame = []
        for style, line in rows[:height]:
            fitting = clip(line, width)
            frame.append(theme.paint(style, theme.glyph(fitting, fitting.encode('ascii','replace').decode('ascii'))))
        if frame != self._frame:
            theme.screen(frame)
            self._frame = frame

    def move(self, key, height):
        stride = max(1, height-4)
        self.offset += {'up': -1, 'down': 1, 'pageup': -stride, 'pagedown': stride}.get(key, 0)


def clip(value, width):
    result = []
    size = 0
    for char in str(value)[:4096]:
        char = char if char.isprintable() else ' '
        cells = 0 if combining(char) else (2 if east_asian_width(char) in ('W', 'F') else 1)
        if size + cells > width:
            break
        result.append(char)
        size += cells
    return ''.join(result)


def terminal_size():
    size = get_terminal_size((100, 28))
    return min(132, max(1, size.columns-1)), max(3, size.lines)


def windows_vt(stdout):
    import ctypes
    import msvcrt
    handle = ctypes.c_void_p(msvcrt.get_osfhandle(stdout.fileno()))
    mode = ctypes.c_uint()
    kernel = ctypes.windll.kernel32
    return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, mode.value | 4))


class Theme:
    def __init__(self, stdout, *, plain=False):
        self.stdout = stdout
        self.rich = False
        if plain or stdout is not sys.stdout or not stdout.isatty() or os.getenv('TERM') == 'dumb' or 'NO_COLOR' in os.environ:
            return
        try:
            stdout.fileno()
            self.rich = os.name != 'nt' or windows_vt(stdout)
        except (OSError, AttributeError, ValueError):
            pass

    def glyph(self, preferred, fallback):
        try:
            preferred.encode(self.stdout.encoding or 'ascii')
        except UnicodeEncodeError:
            return fallback
        return preferred

    def paint(self, style, text):
        return f'\x1b[{PALETTE[style]}m{text}\x1b[0m' if self.rich else text

    def banner(self, width, height):
        if not self.rich or width < 35 or height < 21:
            return [self.paint('title', clip('tuneio', width))]
        pixel = self.glyph('█', '#')
        return [f'\x1b[38;2;{red};{green};{blue}m' +
                ' '.join(''.join(pixel if PIXELS[letter][row] & (1 << column) else ' '
                                 for column in range(4, -1, -1)) for letter in 'tuneio') + '\x1b[0m'
                for row, (red, green, blue) in enumerate(GRADIENT)]

    def screen(self, lines):
        # A newline after the last viewport row scrolls the whole frame.
        frame = '\r\n'.join(lines) if self.rich else '\n'.join(lines) + '\n'
        self.stdout.write(('\x1b[H\x1b[J' if self.rich else '') + frame)
        self.stdout.flush()

    @contextmanager
    def fullscreen(self):
        if not self.rich or ACTIVE_SCREEN.get() is self.stdout:
            yield
            return
        token = ACTIVE_SCREEN.set(self.stdout)
        try:
            self.stdout.write('\x1b[?1049h')
            self.stdout.flush()
            yield
        finally:
            ACTIVE_SCREEN.reset(token)
            self.stdout.write('\x1b[?1049l')
            self.stdout.flush()


class Keyboard:
    def __init__(self, stdin):
        self.stdin = stdin
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

    @contextmanager
    def active(self):
        if os.name == 'nt':
            yield
        else:
            import termios
            import tty
            fd = self.stdin.fileno()
            original = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            try:
                yield
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, original)

    def read(self, seconds, *, text=False):
        if os.name == 'nt':
            import msvcrt
            deadline = monotonic() + seconds
            while not msvcrt.kbhit():
                remaining = deadline-monotonic()
                if remaining <= 0:
                    return None
                sleep(min(.05, remaining))
            key = msvcrt.getwch()
            if key in ('\x00', '\xe0'):
                return {'H':'up', 'P':'down', 'K':'left', 'M':'right', 'I':'pageup', 'Q':'pagedown'}.get(msvcrt.getwch(), '')
        else:
            from select import select
            fd = self.stdin.fileno()
            if not select([fd], [], [], seconds)[0]:
                return None
            raw = os.read(fd, 1)
            if not raw:
                raise EOFError()
            if raw == b'\x1b':
                for _ in range(3):
                    if not select([fd], [], [], .02)[0]:
                        break
                    raw += os.read(fd, 1)
                    if raw[-1:] in b'ABCD~':
                        break
                return {b'\x1b[A':'up', b'\x1b[B':'down', b'\x1b[C':'right',
                        b'\x1b[D':'left', b'\x1b[5~':'pageup', b'\x1b[6~':'pagedown',
                        b'\x1b':'back'}.get(raw, '')
            key = self.decoder.decode(raw) if text else raw.decode('ascii', errors='ignore')
        if key == '\x03':
            raise KeyboardInterrupt()
        if key in ('\x04', '\x1a'):
            raise EOFError()
        return {'\r':'enter', '\n':'enter', '\x1b':'back', '\t':'tab', '\x08':'erase',
                '\x7f':'erase'}.get(key, key if text else key.lower())
