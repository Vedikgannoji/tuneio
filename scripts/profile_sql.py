"""Isolated SQL index comparison; never changes the user's running database."""
import json
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark import seed
from tuneio.database import Database, REPORT_SQL


def main():
    result = {'scope': 'SQL query only; not HTTP or cache performance', 'orders': 50000, 'repeats': 7}
    with tempfile.TemporaryDirectory(prefix='tuneio-sql-') as temp:
        path = Path(temp) / 'profile.sqlite3'
        seed(path, 50000)
        db = Database(path)
        with db.connect() as conn:
            conn.execute('DROP INDEX orders_sales_idx')
        expected = db.report()
        for name in ('before', 'after'):
            if name == 'after':
                Database(path)  # apply the normal startup index migration
            db.report()  # equal warmup outside the timed samples
            values = []
            for _ in range(7):
                start = perf_counter()
                actual = db.report()
                values.append((perf_counter() - start) * 1000)
                assert actual == expected
            with db.connect() as conn:
                plan = [dict(r) for r in conn.execute('EXPLAIN QUERY PLAN ' + REPORT_SQL)]
            result[name] = {'latencies_ms': values, 'median_ms': statistics.median(values), 'query_plan': plan}
    result['query_latency_reduction_percent'] = (1 - result['after']['median_ms'] / result['before']['median_ms']) * 100
    output = Path(__file__).resolve().parents[1] / 'docs/SQL_PROFILE.json'
    output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
