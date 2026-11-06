"""Report-schema fix smoke validation; existing user review waiver applies.

Reuses production evaluation execution, isolation, assertions and accounting.
The standard reviewed CLI and its acceptance checks are left unchanged.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from opsfleet_assistant.contracts import ReportDraft
from opsfleet_assistant.model_gateway import _strict_schema
from opsfleet_assistant.config import Settings
from opsfleet_assistant.evaluation_contracts import Accounting, Redactor, atomic_json, digest, totals, verify_source
from opsfleet_assistant.evaluation_runner import execute, validate_manifest

REPORT_SCHEMA_SHA256 = '1c1490b5434e314783e437b2bdffc9de1fd9a279001b6df00be84e9b78c88440'
SELECTED = ('cal-jan', 'ambiguity-year', 'num-increase', 'report-rollover', 'live-journey')
# Actual live journey: 4 initial + 1 explain + 5 comparison + 4 report jobs.
CAPS = {'turns':10, 'provider_attempts':20, 'warehouse_executions':14,
        'billed_bytes':1024**3, 'active_seconds':600}


def runtime_fingerprints():
    return {str(p.relative_to(ROOT)): {'sha256':digest(p), 'bytes':p.stat().st_size}
            for p in (ROOT / 'runtime').rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,79}', args.run_id):
        parser.error('invalid_run_id')
    source_path = ROOT / 'docs/remediation/EVALUATION_SOURCE_MANIFEST.json'
    source = json.loads(source_path.read_text())
    revision = verify_source(ROOT, source)
    report_schema_hash = hashlib.sha256(json.dumps(_strict_schema(ReportDraft),
        sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if report_schema_hash != REPORT_SCHEMA_SHA256:
        parser.error('report_schema_not_live_confirmed')
    manifest_path = ROOT / 'docs/remediation/SCENARIO_MANIFEST.json'
    if digest(manifest_path) != source['scenario_sha256']:
        parser.error('scenario_hash_mismatch')
    manifest = validate_manifest(json.loads(manifest_path.read_text()))
    selected = {**manifest, 'scenarios':[c for c in manifest['scenarios'] if c['id'] in SELECTED]}
    if len(selected['scenarios']) != len(SELECTED):
        parser.error('missing_selected_scenario')
    output = ROOT / 'docs/remediation/runs/report-schema-fix-20261105' / args.run_id
    output.mkdir(mode=0o700, exist_ok=False)
    settings = Settings.load()
    redactor = Redactor((settings.gemini_api_key, settings.openrouter_api_key))
    before = runtime_fingerprints()
    private = Path(tempfile.mkdtemp(prefix='opsfleet-user-live-smoke-'))
    os.chmod(private, 0o700)
    (private / 'evaluation-private-marker').write_text('isolated synthetic evaluation state\n')
    identity = {'run_id':args.run_id, 'source_revision':revision, 'report_schema_sha256':report_schema_hash,
                'scenario_sha256':source['scenario_sha256'], 'scenario_version':manifest['version'],
                'live':True, 'review_status':'waived_by_user', 'review_sha256':None,
                'operator_driver_sha256':digest(Path(__file__))}
    plan = {**identity, 'authorization':'User explicitly requested skipping review and starting live testing.',
            'independent_acceptance':False, 'selected_scenarios':list(SELECTED),
            'planned_scenarios':len(selected['scenarios']),
            'planned_turns':sum(len(c['turns']) for c in selected['scenarios']),
            'caps':CAPS, 'selected_model':settings.openrouter_model,
            'configuration_available':{'openrouter':bool(settings.openrouter_api_key),
                                       'bigquery_project':bool(settings.project)},
            'created_at_utc':datetime.now(timezone.utc).isoformat(),
            'runtime_before':before, 'provider_turn_retries':0}
    # Persist the operator waiver, source/scenario/driver identity and caps before dispatch.
    atomic_json(output / 'plan.json', redactor(plan))
    with (private / 'runner.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        account = Accounting(private / 'checkpoint.json', identity=identity, caps=CAPS)
        account.state['selected_model'] = settings.openrouter_model
        account.save()
        try:
            rows = execute(account, selected, private, live=True, settings=settings)
        finally:
            # Preserve partial checkpoints even when interruption prevents final results.
            account.save()
        after = runtime_fingerprints()
        verify_source(ROOT, source)
        result = redactor({'kind':'user_authorized_live_smoke_without_independent_review',
                          'identity':identity, 'caps':CAPS, 'used':account.state['used'],
                          'events':account.state['events'], 'stop':account.state['stop'],
                          'denials':account.state.get('denials',[]),
                          'dry_runs':account.state.get('dry_runs',0), 'results':rows,
                          'counts':totals(rows), 'selected_model':settings.openrouter_model,
                          'cost':None, 'runtime_after':after, 'runtime_unchanged':before == after,
                          'limits':['Review explicitly waived by user; no independent acceptance claimed.',
                                    'Fixture warehouse journeys test real model behavior, not live BigQuery.',
                                    'Unknown token usage/cost remains unknown.']})
        atomic_json(output / 'results-batch-001.json', result)
        atomic_json(output / 'results.json', result)
        print(json.dumps({'output':str(output / 'results.json'), 'private_resume_directory':str(private),
                          'counts':result['counts'], 'used':result['used'], 'stop':result['stop'],
                          'denials':result['denials'],
                          'runtime_unchanged':result['runtime_unchanged']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
