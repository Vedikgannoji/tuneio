"""Inventory supported API descriptions without importing project code."""
import ast
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from time import monotonic
from urllib.parse import urlsplit
import urllib.request

from .product_config import ProductConfig


METHODS = ('get', 'head', 'post', 'put', 'patch', 'delete', 'options')
IGNORED = {'venv', 'env', 'build', 'dist', 'node_modules', '__pycache__', 'site-packages', 'tmp', 'temp'}
MAX_BYTES = 1024 * 1024
MAX_ENTRIES = 4000
MAX_FILES = 200
MAX_DEPTH = 5
MAX_OPERATIONS = 1000
PARAMETER = re.compile(r'\{[a-zA-Z_][a-zA-Z0-9_]*\}')
LITERAL = re.compile(r'[a-zA-Z0-9_.~-]+')


def operation_path(path):
    if path == '/':
        return path
    if not path.startswith('/') or len(path) > 1024:
        return None
    segments = []
    for part in path[1:].split('/'):
        if PARAMETER.fullmatch(part):
            segments.append('*')
        elif LITERAL.fullmatch(part) and part not in ('.', '..'):
            segments.append(part)
        else:
            return None
    return '/' + '/'.join(segments)


def linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def read_project_file(root, path):
    candidate = path if path.is_absolute() else root / path
    relative = candidate.relative_to(root)
    if '..' in relative.parts or not relative.parts:
        raise ValueError('Expected a file inside the project')
    current = root
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        if linked(info):
            raise ValueError('Linked project paths are unsupported')
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise ValueError('Expected a regular project file at most 1 MiB')
    with current.open('rb') as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError('Project file exceeds 1 MiB')
    return content


def static_files(root, warnings):
    pending = deque([(root, 0)])
    entries_left = MAX_ENTRIES
    files_left = MAX_FILES
    while pending:
        directory, depth = pending.popleft()
        try:
            with os.scandir(directory) as entries:
                batch = []
                for entry in entries:
                    if entries_left == 0:
                        warnings.add('Project scan stopped at the directory-entry limit.')
                        break
                    entries_left -= 1
                    batch.append(entry)
            for entry in sorted(batch, key=lambda item: item.name):
                if entry.name.startswith('.') or entry.name.casefold() in IGNORED:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if linked(info):
                        continue
                    path = Path(entry.path)
                    if stat.S_ISDIR(info.st_mode):
                        if depth < MAX_DEPTH:
                            pending.append((path, depth + 1))
                        else:
                            warnings.add('Project scan reached the depth limit.')
                    elif stat.S_ISREG(info.st_mode) and path.suffix == '.py':
                        if files_left == 0:
                            warnings.add('Project scan stopped at the Python-file limit.')
                            return
                        files_left -= 1
                        yield path
                except OSError:
                    warnings.add('Some project entries could not be inspected.')
            if entries_left == 0:
                return
        except OSError:
            warnings.add('Some project directories could not be read.')


def fastapi_instances(body, constructors, modules):
    apps = set()
    for node in body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        direct = isinstance(func, ast.Name) and func.id in constructors
        qualified = (isinstance(func, ast.Attribute) and func.attr == 'FastAPI'
                     and isinstance(func.value, ast.Name) and func.value.id in modules)
        if direct or qualified:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            apps.update(target.id for target in targets if isinstance(target, ast.Name))
    return apps


def decorated_routes(body, apps, origin, warnings):
    for node in body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            func = decorator.func
            if not isinstance(func.value, ast.Name) or func.value.id not in apps or func.attr not in METHODS:
                continue
            arg = decorator.args[0] if decorator.args else next(
                (kw.value for kw in decorator.keywords if kw.arg == 'path'), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield func.attr, arg.value, origin
            else:
                warnings.add('Dynamic FastAPI paths were skipped.')


def static_operations(root, warnings, entrypoints, factories):
    for path in static_files(root, warnings):
        try:
            tree = ast.parse(read_project_file(root, path))
        except (OSError, ValueError, SyntaxError, RecursionError):
            warnings.add('Some Python files could not be parsed within the scan limits.')
            continue
        constructors = set()
        modules = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == 'fastapi' and node.level == 0:
                constructors.update(item.asname or item.name for item in node.names if item.name == 'FastAPI')
            elif isinstance(node, ast.Import):
                modules.update(item.asname or item.name for item in node.names if item.name == 'fastapi')

        relative = path.relative_to(root)
        origin = relative.as_posix()
        module_parts = relative.with_suffix('').parts
        if module_parts[-1] == '__init__':
            module_parts = module_parts[:-1]
        module = '.'.join(module_parts) if all(part.isidentifier() for part in module_parts) else ''
        apps = fastapi_instances(tree.body, constructors, modules)
        if module:
            entrypoints.update(module + ':' + app for app in apps)
        yield from decorated_routes(tree.body, apps, origin, warnings)

        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            last = node.body[-1]
            if not isinstance(last, ast.Return) or not isinstance(last.value, ast.Name):
                continue
            factory_apps = fastapi_instances(node.body[:-1], constructors, modules)
            if last.value.id not in factory_apps:
                continue
            if module:
                factories.add(module + ':' + node.name)
            yield from decorated_routes(node.body[:-1], {last.value.id}, origin, warnings)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_description(url):
    from ipaddress import ip_address

    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or parsed.username is not None or
            '@' in parsed.netloc or parsed.fragment or parsed.query or '\\' in url or
            any(not char.isprintable() or char.isspace() for char in url) or
            not ip_address(parsed.hostname or '').is_loopback):
        raise ValueError('Use an explicit HTTP(S) loopback IP URL without credentials, query or fragment')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = monotonic() + 3
    content = bytearray()
    with opener.open(url, timeout=3) as response:
        while len(content) <= MAX_BYTES:
            if monotonic() >= deadline:
                raise TimeoutError('OpenAPI read timed out')
            chunk = response.read1(min(65536, MAX_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
    if len(content) > MAX_BYTES:
        raise ValueError('OpenAPI description exceeds 1 MiB')
    return bytes(content)


def openapi_operations(content, source, warnings):
    try:
        document = json.loads(content)
    except (ValueError, RecursionError):
        raise ValueError('Expected a valid OpenAPI JSON document') from None
    if (not isinstance(document, dict) or not isinstance(document.get('openapi'), str) or
            not document['openapi'].startswith('3.') or not isinstance(document.get('paths'), dict)):
        raise ValueError('Expected OpenAPI 3 JSON with a paths object')
    for path, item in sorted(document['paths'].items()):
        if not isinstance(item, dict) or '$ref' in item:
            warnings.add('Invalid or referenced OpenAPI path items were skipped.')
            continue
        for method in METHODS:
            if isinstance(item.get(method), dict):
                yield method, path, source


def discover(project, *, openapi=None, openapi_url=None):
    root = Path(project).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Expected a project directory')
    warnings = set()
    entrypoints = set()
    factories = set()
    if openapi_url:
        source = 'live OpenAPI'
        candidates = openapi_operations(fetch_description(openapi_url), source, warnings)
    elif openapi or (root / 'openapi.json').is_file():
        source = 'local OpenAPI'
        candidates = openapi_operations(read_project_file(root, Path(openapi or 'openapi.json')), source, warnings)
    else:
        source = 'static FastAPI'
        candidates = static_operations(root, warnings, entrypoints, factories)
        warnings.add('Static candidates cover literal decorators on direct apps and simple factories. Routers, mounts and dynamic routes may be missing.')

    operations = {}
    for method, path, origin in candidates:
        pattern = operation_path(path)
        if pattern is None:
            warnings.add('Some paths cannot be represented as operation labels and were skipped.')
            continue
        key = (method.upper(), path, origin)
        if key not in operations and len(operations) == MAX_OPERATIONS:
            warnings.add('Discovery reached the operation limit.')
            break
        operations[key] = pattern
    rows = [dict(id=index, method=method, path=path, pattern=operations[method, path, origin],
                 origin=origin, confidence='candidate' if source == 'static FastAPI' else 'described')
            for index, (method, path, origin) in enumerate(sorted(operations), 1)]
    if not rows:
        warnings.add('No supported operations found. Supply an OpenAPI JSON description or use manual configuration.')
    return dict(schema_version=1, project=str(root), source=source, complete=False,
                entrypoint_candidates=sorted(entrypoints)[:MAX_FILES],
                factory_entrypoint_candidates=sorted(factories)[:MAX_FILES],
                operations=rows, warnings=sorted(warnings))


def reviewed_configuration(inventory, selected, upstream, listen):
    operations = {row['id']: row for row in inventory['operations']}
    if not selected or len(set(selected)) != len(selected) or any(i not in operations for i in selected):
        raise ValueError('Select unique operation IDs shown in the inventory')
    return ProductConfig(upstream=upstream, listen=listen, operations=[
        dict(method=operations[i]['method'], path=operations[i]['pattern']) for i in selected])


def configuration_path(root, name):
    if (not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,119}\.json', name) or
            re.fullmatch(r'CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]', name.split('.')[0], re.I)):
        raise ValueError('Use a project-root JSON filename')
    return Path(root) / name


def write_configuration(root, name, config):
    path = configuration_path(root, name)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(config.model_dump_json(indent=2) + '\n')
    return path


def review_token(config, root, output, selected):
    payload = json.dumps([root, output, config.model_dump(), selected], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
