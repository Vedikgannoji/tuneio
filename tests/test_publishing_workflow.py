"""Regression checks for Windows-built release source verification."""
import ast
from pathlib import Path
import textwrap

import pytest


@pytest.fixture
def source_bytes():
    workflow = Path(__file__).resolve().parents[1] / '.github/workflows/publish.yml'
    block = workflow.read_text().split("python - <<'PY'\n", 1)[1].split('\n          PY', 1)[0]
    script = ast.parse(textwrap.dedent(block))
    function = next(node for node in script.body if isinstance(node, ast.FunctionDef) and node.name == 'source_bytes')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(workflow), 'exec'), namespace)
    return namespace['source_bytes']


@pytest.mark.parametrize('suffix', ['.py', '.html', '.css', '.js', '.json'])
def test_windows_line_endings_match_tagged_text(source_bytes, suffix):
    path = Path('source' + suffix)
    assert source_bytes(path, b'first\r\nsecond\n') == source_bytes(path, b'first\nsecond\n')
    assert source_bytes(path, b'changed\r\n') != source_bytes(path, b'original\n')
    assert source_bytes(path, b'a\rb') != source_bytes(path, b'a\nb')


def test_binary_bytes_remain_exact(source_bytes):
    assert source_bytes(Path('image.png'), b'\x00\r\n\xff') == b'\x00\r\n\xff'
