"""Exercise local wheel install, version transition and uninstall outside the checkout."""
import argparse
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import venv

PACKAGE = 'tuneio'
BIN = 'Scripts' if os.name == 'nt' else 'bin'
SUFFIX = '.exe' if os.name == 'nt' else ''


def verify(root, installer, wheel, previous, offline):
    env = dict(os.environ, UV_TOOL_DIR=str(root/'uv-tools'), UV_TOOL_BIN_DIR=str(root/'uv-bin'),
               UV_CACHE_DIR=str(root/'uv-cache'), PIPX_HOME=str(root/'pipx'), PIPX_BIN_DIR=str(root/'pipx-bin'),
               PIPX_MAN_DIR=str(root/'pipx-man'), PIP_DISABLE_PIP_VERSION_CHECK='1')

    def run(*args):
        result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=180)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout.strip()

    if installer == 'pip':
        venv.create(root/'venv', with_pip=True, system_site_packages=offline)
        binary = root/'venv'/BIN/'tuneio'
        runner = [str(binary.with_name('python'+SUFFIX)),'-m','pip']
        flags = ['--no-index','--no-deps'] if offline else []
        def install(path):
            run(*runner,'install','--upgrade',*flags,str(path))
        remove = [*runner,'uninstall','-y',PACKAGE]
    elif installer == 'uv':
        binary = root/'uv-bin'/'tuneio'
        def install(path):
            run('uv','tool','install','--force','--no-python-downloads','--python',sys.executable,str(path))
        remove = ['uv','tool','uninstall',PACKAGE]
    else:
        binary = root/'pipx-bin'/'tuneio'
        runner = [sys.executable,'-m','pipx']
        def install(path):
            run(*runner,'install','--force','--python',sys.executable,str(path))
        remove = [*runner,'uninstall',PACKAGE]
    entry = binary.with_suffix(SUFFIX)

    def smoke(path, *, candidate):
        installed_version = path.name.split('-')[1]
        assert run(str(entry),'--version') == 'tuneio '+installed_version
        run(str(entry), *(['menu','--json'] if candidate else ['--help']))
        if not offline:
            run(str(entry),'init','--config',str(root/f'{installed_version}.json'),'--json')
            run(str(entry),'serve','--check','--config',str(root/f'{installed_version}.json'),'--json')

    if previous:
        install(previous)
        smoke(previous, candidate=False)
    install(wheel)
    smoke(wheel, candidate=True)
    run(*remove)
    assert not entry.exists()
    transitions = 'install, upgrade, and uninstall' if previous else 'install and uninstall'
    scope = ' (existing system dependencies)' if offline else ''
    print(f'{installer}: {transitions} passed{scope}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel',type=Path)
    parser.add_argument('--previous',type=Path)
    parser.add_argument('--installer',choices=('pip','pipx','uv'),action='append')
    parser.add_argument('--offline-entrypoints',action='store_true',help='Exercise pip entry points using system dependencies')
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    previous = args.previous.resolve(strict=True) if args.previous is not None else None
    prefix = PACKAGE.replace('-', '_') + '-'
    if any(path.suffix != '.whl' or not path.name.startswith(prefix)
           for path in (wheel, previous) if path is not None):
        parser.error(f'Use {PACKAGE} wheels for both the candidate and any previous release')
    installers = args.installer or ['pip','pipx','uv']
    if args.offline_entrypoints and installers != ['pip']:
        parser.error('--offline-entrypoints requires --installer pip')
    if 'pipx' in installers:
        try:
            version('pipx')
        except PackageNotFoundError:
            parser.error('Install pipx in the runner environment or select another installer')
    scratch = Path('tmp').resolve()
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='tuneio-installer-smoke-',dir=scratch) as tmp:
        for installer in installers:
            root = Path(tmp)/installer
            root.mkdir()
            verify(root, installer, wheel, previous, args.offline_entrypoints)


if __name__ == '__main__':
    main()
