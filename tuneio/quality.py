"""Independent core statement and branch floors; supplements pytest's combined gate."""
import argparse
import json
from pathlib import Path


def check(data,minimum=90):
    if not 0<=minimum<=100:raise ValueError('Minimum must be 0–100')
    summaries=[value['summary'] for key,value in data['files'].items()
               if key.replace('\\','/').startswith('tuneio/core/')]
    if not summaries:raise ValueError('Core coverage records missing')
    totals={key:sum(s[key] for s in summaries) for key in ('num_statements','covered_lines','num_branches','covered_branches')}
    statements=100*totals['covered_lines']/totals['num_statements'] if totals['num_statements'] else 100.
    branches=100*totals['covered_branches']/totals['num_branches'] if totals['num_branches'] else 100.
    return dict(schema_version=1,statements_percent=statements,branches_percent=branches,minimum_percent=minimum,
                passed=statements>=minimum and branches>=minimum,totals=totals)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file',type=Path)
    args=parser.parse_args(argv)
    try:result=check(json.loads(args.file.read_text(encoding='utf-8')))
    except (OSError,ValueError,KeyError,TypeError):
        print('Invalid or missing branch coverage evidence');return 2
    print(json.dumps(result,indent=2));return 0 if result['passed'] else 3


if __name__=='__main__':raise SystemExit(main())
