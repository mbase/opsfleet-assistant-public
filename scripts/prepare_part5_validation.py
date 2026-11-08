"""Freeze current public application inputs or project actual JUnit observations.

Does not install, dispatch providers, adopt uv.lock, or rewrite historical records.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from opsfleet_assistant.evaluation_contracts import canonical, digest, MAX_CAPS
from opsfleet_assistant.evaluation_inventory import POLICY, inventory, tracked_diff_hash

AREA = ROOT / 'docs/validation/part5-v1'


def lock_audit():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    lock = tomllib.loads((ROOT / 'uv.lock').read_text())
    packages = {p['name']: p for p in lock['package']}
    own = packages[project['name']]
    expected = sorted(project['dependencies'] + [dep for deps in project['optional-dependencies'].values() for dep in deps])
    actual = sorted(d['name'] + d['specifier'] for d in own['metadata']['requires-dist'])
    todo = [d['name'] for d in own['dependencies']] + ['pytest']
    closure = set()
    while todo:
        name = todo.pop()
        if name in closure:
            continue
        closure.add(name)
        todo.extend(d['name'] for d in packages[name].get('dependencies', []))
    pins = {name + '==' + packages[name]['version'] for name in closure}
    accepted = {line for line in (ROOT / 'requirements.part5.lock').read_text().splitlines()
                if line and not line.startswith('#')}
    return {'uv_sha256': digest(ROOT / 'uv.lock'), 'metadata_matches': actual == expected,
            'python_matches': lock['requires-python'].replace(' ', '') == project['requires-python'],
            'editable_source': own['source'] == {'editable': '.'}, 'version_matches': own['version'] == project['version'],
            'extras': sorted(own['optional-dependencies']), 'dependency_groups': lock.get('manifest', {}).get('dependency-groups', []),
            'resolved_packages': len(packages), 'runtime_test_closure_packages': len(closure),
            'only_uv_pins': sorted(pins - accepted), 'only_accepted_pins': sorted(accepted - pins),
            'decision': 'RETAIN_UNTOUCHED_NOT_ADOPTED',
            'reason': 'Valid direct metadata; different transitive resolution from accepted environment. Part 5 pins preserve accepted versions; registry/install validation is separate.'}


def freeze():
    files = inventory(ROOT)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=ROOT, text=True).strip()
    source = {'files': files, 'revision': canonical(files), 'application_head': head, 'application_tree': tree,
              'inventory_policy': POLICY,
              'tracked_application_diff_sha256': tracked_diff_hash(ROOT),
              'uncommitted': True, 'untracked_inputs_included': True,
              'scope': 'Public executable/config/test/fixture inputs. Narrative reports/results and this manifest excluded to avoid recursion. Not a committed Part 5 application.'}
    value = {'schema_version': 2, 'version': 'part5-v1', 'source': source,
             'scenario_file': 'docs/validation/part5-v1/scenarios.json',
             'fixture_snapshot_sha256': digest(AREA / 'source_rows.json'),
             'driver_sha256': digest(ROOT / 'src/opsfleet_assistant/evaluation_part5.py'),
             'dependency_lock_sha256': digest(ROOT / 'requirements.part5.lock'),
             'unadopted_uv_sha256': json.loads((AREA / 'lock_audit.json').read_text())['uv_sha256'],
             'metadata_sha256': digest(ROOT / 'pyproject.toml'),
             'versions': {'application': '0.1.0+part5.candidate', 'metric': 2, 'classification': 2, 'first_semantic': 2, 'field_policy': 1,
                          'compiler': 1, 'source_binding': 'public_v1', 'playbooks': 1,
                          'artifact': [1, 2], 'snapshot': [2, 3], 'grounded_report': [1, 2, 3],
                          'rendering': 1, 'presentation': [1, 2],
                          'prompt_sha256': digest(ROOT / 'src/opsfleet_assistant/model_gateway.py'),
                          'policy_sha256': digest(ROOT / 'src/opsfleet_assistant/privacy.py')},
             'prepared_interpreter': platform.python_version(),
             'command': 'python scripts/run_live_evaluation.py --manifest docs/validation/part5-v1/candidate_manifest.json --run-id OWNER_RUN_ID --layer LAYER --dry-run',
             'caps': dict(MAX_CAPS),
             'provider': {'route': 'openrouter_only', 'model': 'google/gemini-3.8-flash',
                          'estimated_cost_usd': None, 'cost_limit_usd': None, 'maximum_cost_per_call_usd': None},
             'warehouse': {'project': None, 'dataset': 'bigquery-public-data.thelook_ecommerce',
                           'snapshot_timestamp': None, 'same_snapshot_authorized': False},
             'review': {'owner_approved': False, 'paraphrases_independently_reviewed': False},
             'status': 'BLOCKED_ENVIRONMENT', 'acceptance_complete': False}
    (AREA / 'candidate_manifest.json').write_text(json.dumps(value, indent=2) + '\n')
    return source['revision']


def project_junit(source, output):
    cases = []
    for node in ET.parse(source).iter('testcase'):
        status = 'FAIL' if node.find('failure') is not None or node.find('error') is not None else 'NOT_RUN' if node.find('skipped') is not None else 'PASS'
        cases.append({'case_id': node.attrib['classname'] + '::' + node.attrib['name'],
                      'status': status, 'first_attempt': status, 'attempts': 1 if status != 'NOT_RUN' else 0,
                      'retries': 0, 'latency_seconds': float(node.attrib.get('time', 0)),
                      'provider_calls': 0, 'live_warehouse_jobs': 0, 'billed_bytes': 0, 'cost_usd': 0})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'layer': 'deterministic_offline', 'source_junit_sha256': digest(source),
                                 'cases': cases, 'counts': {s: sum(c['status'] == s for c in cases) for s in ('PASS', 'FAIL', 'NOT_RUN')},
                                 'limits': 'Test-instance observations, including injected transport tests. Not real model or warehouse evidence.'}, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--audit-lock', action='store_true')
    parser.add_argument('--junit', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.audit_lock:
        print(json.dumps(lock_audit(), indent=2))
    if args.junit:
        if args.output is None or args.output.exists():
            parser.error('new_output_required')
        project_junit(args.junit, args.output)
    if args.freeze:
        print(freeze())


if __name__ == '__main__':
    main()
