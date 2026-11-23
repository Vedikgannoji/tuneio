"""Bounded JSON artifacts, published only after all bytes are written."""
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def read_json(path, limit):
    with Path(path).open('rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Evidence exceeds size limit')

    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('Duplicate evidence field')
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Non-finite evidence number')
        return number

    try:
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=finite, parse_float=finite)
    except RecursionError:
        raise ValueError('Evidence nesting exceeds parser limits') from None
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object')
    return data, raw


def write_new(path, data, *, limit=32 * 1024 * 1024):
    path = Path(path)
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            for part in json.JSONEncoder(indent=2, allow_nan=False).iterencode(data):
                stream.write(part.encode('utf-8'))
                if stream.tell() >= limit:
                    raise ValueError('Export exceeds size limit')
            stream.write(b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            os.rename(temp, path)  # On Windows this refuses an existing destination.
        else:
            os.link(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
