"""Reproduce a clean wheel install in an owned venv, outside the repository import path."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import venv


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel',type=Path)
    parser.add_argument('--wheelhouse',type=Path,help='Resolve dependencies from an existing local wheel directory')
    parser.add_argument('--local-only',action='store_true',
                        help='Offline smoke using system site-packages and no real HTTP. Does not qualify a clean install.')
    args=parser.parse_args();wheel=args.wheel.resolve(strict=True)
    if wheel.is_dir():
        wheels=list(wheel.glob('*.whl'))
        if len(wheels)!=1:raise ValueError('Expected exactly one wheel in the distribution directory')
        wheel=wheels[0]
    wheelhouse = args.wheelhouse.resolve(strict=True) if args.wheelhouse else None
    scratch = Path('tmp').resolve()
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tuneio-clean-install-', dir=scratch) as temporary:
        root=Path(temporary);venv.create(root/'venv',with_pip=True,system_site_packages=args.local_only)
        python=root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        def run(*arguments):subprocess.run([str(python),*arguments],cwd=root,check=True,timeout=180)
        pip_flags = ['--no-index','--find-links',str(wheelhouse)] if wheelhouse else []
        if args.local_only:
            pip_flags += ['--no-deps']
        run('-m','pip','install',*pip_flags,str(wheel))
        run('-I','-m','tuneio','--version')
        run('-I','-m','tuneio','menu','--project',str(root),'--json')
        run('-I','-m','tuneio','init','--config',str(root/'config.json'),'--json')
        run('-I','-m','tuneio','serve','--config',str(root/'config.json'),'--check','--json')
        run('-I','-m','tuneio','benchmark','--help')
        for command in ('baseline','optimize','monitor','compare'):
            run('-I','-m','tuneio',command,'--help')
        run('-I','-c',"from importlib.resources import files; import tuneio; print(tuneio.__file__); "
            "assert all(files('tuneio').joinpath(p).is_file() for p in ('product_dashboard.html','product.css','product_ui.js','scenario_presets.json','benchmarking.py','qualification_server.py'))")
        if not args.local_only:
            run('-I',str(Path(__file__).with_name('smoke_product.py').resolve()))


if __name__=='__main__':main()
