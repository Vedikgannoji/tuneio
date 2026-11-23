"""Portable JSON and CSV exports from retained, redacted telemetry."""
import csv
import io
import json

CSV_FIELDS = ['schema_version', 'tool_version', 'source_sha256', 'session_id', 'config_digest', 'sampling_incomplete', 'sampling_may_be_incomplete', 'shutdown_recorded',
              'dropped_queue', 'dropped_storage', 'retention_truncated', 'budget_truncated',
              'boundary', 'method', 'route', 'count', 'mean_ms', 'p50_ms', 'p95_ms', 'p99_ms',
              'errors', 'server_errors', 'error_rate', 'cache_hits', 'cache_misses', 'cache_bypasses', 'first_bucket_utc', 'last_bucket_utc']


def serialize_report(data, format):
    if format == 'json':
        return json.dumps(data, indent=2) + '\n'
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator='\n')
    writer.writeheader()
    metadata = {key: data[key] for key in ('schema_version', 'tool_version', 'source_sha256', 'session_id', 'config_digest')}
    for key in ('sampling_incomplete', 'sampling_may_be_incomplete', 'dropped_queue', 'dropped_storage', 'retention_truncated', 'budget_truncated'):
        metadata[key] = data['sampling'].get(key, False if key.startswith('sampling') else 0)
    metadata['shutdown_recorded'] = data.get('shutdown_recorded', False)
    for route in data['routes']:
        writer.writerow(dict(metadata, **route))
    return output.getvalue()
