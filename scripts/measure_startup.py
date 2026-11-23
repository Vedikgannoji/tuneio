"""Measure one installed CLI's cold process startup outside its checkout."""
from argparse import ArgumentParser
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=7)
    args = parser.parse_args()
    if not 5 <= args.samples <= 30:
        parser.error('Use 5–30 samples per command.')
    results = {}
    with TemporaryDirectory(prefix='autotuner-startup-') as project:
        prefix = [sys.executable, '-I', '-m', 'autotuner']
        tool = subprocess.check_output([*prefix,'--version'],cwd=project,text=True,timeout=15).strip()
        for command in (('--help',), ('--version',), ('menu','--json')):
            readings = []
            for _ in range(args.samples):
                started = perf_counter()
                subprocess.run([*prefix,*command],cwd=project,stdout=subprocess.DEVNULL,check=True,timeout=15)
                readings.append((perf_counter()-started)*1000)
            ordered = sorted(readings)
            p95 = ordered[(95*len(ordered)+99)//100-1]
            median = statistics.median(readings)
            results[' '.join(command)] = dict(trials_ms=readings,median_ms=median,p95_ms=p95,
                                             proposed_budget_met=median<=500 and p95<=1000)
        assert not list(Path(project).iterdir())
    print(json.dumps(dict(schema_version=1, tool=tool, os=platform.platform(), python=platform.python_version(),
                          scope='fresh processes on this host', commands=results),indent=2))


if __name__ == '__main__':
    main()
