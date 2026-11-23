"""Offline, fail-closed candidate review. Local receipts are maintainer attestations."""
import hashlib
import json
from pathlib import Path

from ._version import __version__
from .artifacts import read_json
from .benchmarking import source_hashes

QUALIFICATION_GATES = ('correctness', 'telemetry_health', 'shutdown', 'repeated_read_benefit',
    'observe_median_overhead', 'observe_p95_overhead', 'offered_rate',
    'reference_environment', 'idle_memory', 'cache_budget')
MATRIX = {(os, version) for os in ('linux', 'windows', 'macos') for version in ('3.11', '3.12', '3.13')}
REVIEW_FLAGS = (
    'license_owner_approved', 'license_file_and_metadata_reviewed', 'publisher_identity_confirmed',
    'wheel_and_sdist_reviewed', 'pilot_integrations_reviewed', 'installer_lifecycle_reviewed',
    'terminal_budgets_reviewed', 'testpypi_artifact_verified',
)


def assess(path):
    path = path.resolve(strict=True)
    manifest, _ = read_json(path, 262144)
    if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise ValueError('Unsupported evidence schema')
    digest = hashlib.sha256(json.dumps(source_hashes(), sort_keys=True).encode()).hexdigest()
    blockers = []
    if manifest.get('version') != __version__:
        blockers.append('candidate_version')

    def proof(name):
        ref = manifest.get(name)
        if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
            blockers.append(name + ':missing'); return None
        try:
            target = (path.parent / ref['path']).resolve(strict=True)
            if not target.is_relative_to(path.parent):
                raise ValueError('Evidence outside review directory')
            value, raw = read_json(target, 32 * 1024 * 1024)
            if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
                raise ValueError('Unsupported proof schema')
            for key in ('environment', 'options', 'gates'):
                if key in value and not isinstance(value[key], dict):
                    raise ValueError('Invalid evidence structure')
            if any(not isinstance(gate, dict) for gate in value.get('gates', {}).values()):
                raise ValueError('Invalid gates')
            if not isinstance(value.get('compatibility', []), list):
                raise ValueError('Invalid compatibility receipt')
            if hashlib.sha256(raw).hexdigest() != ref.get('sha256'):
                raise ValueError('Evidence hash mismatch')
            if value.get('source_digest') != digest or value.get('tool_version', value.get('environment', {}).get('tool_version')) != __version__:
                raise ValueError('Evidence from different candidate')
            return value
        except (OSError, ValueError, TypeError, AttributeError):
            blockers.append(name + ':invalid'); return None

    qualification = proof('qualification')
    if qualification is not None:
        if (qualification.get('schema_version') != 1 or qualification.get('evidence_type') != 'real_http_isolated_product_benchmark'
                or qualification.get('options', {}).get('profile') != 'qualification'
                or type(qualification.get('measurement_revision')) is not int
                or qualification['measurement_revision'] != 2):
            blockers.append('qualification:contract')
        for name in QUALIFICATION_GATES:
            expected = 'confirmed' if name == 'reference_environment' else 'passed'
            if qualification.get('gates', {}).get(name, {}).get('state') != expected:
                blockers.append('qualification:' + name)
    soak = proof('soak')
    if soak is not None:
        if (soak.get('schema_version') != 1 or soak.get('evidence_type') != 'real_http_isolated_product_benchmark'
                or soak.get('options', {}).get('profile') != 'soak'
                or type(soak.get('options', {}).get('duration_seconds')) not in (int, float)
                or not 1800 <= soak['options']['duration_seconds'] <= 3600
                or soak.get('graceful_shutdown') is not True):
            blockers.append('soak:contract')
        for name in ('telemetry_health', 'stability_30_minutes'):
            if soak.get('gates', {}).get(name, {}).get('state') != 'passed':
                blockers.append('soak:' + name)
    receipt = proof('review_receipt')
    if receipt is not None:
        if receipt.get('schema_version') != 1: blockers.append('review_receipt:contract')
        rows = receipt.get('compatibility', [])
        passed = {(row.get('os'), row.get('python')) for row in rows if isinstance(row, dict)
                  and isinstance(row.get('os'), str) and isinstance(row.get('python'), str)
                  and row.get('pytest') is True and row.get('node') is True
                  and row.get('core_coverage') is True and row.get('clean_wheel_install') is True}
        if not MATRIX.issubset(passed): blockers.append('compatibility_matrix')
        for name in REVIEW_FLAGS:
            if receipt.get(name) is not True: blockers.append(name)
    return dict(schema_version=1, version=__version__, source_digest=digest,
                ready=not blockers, publication_performed=False, blockers=blockers,
                boundary='Offline local evidence and maintainer attestations; no authenticity or registry ownership verification')
