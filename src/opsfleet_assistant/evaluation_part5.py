"""Version-bound Part 5 orchestration using the existing execution/accounting engine.

Default and dry paths never load settings, credentials, ADC or SDK clients.
Public records are an allowlisted projection; checkpoints stay outside the repo.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .config import ROOT, Settings
from . import config
from .evaluation_contracts import Accounting, MAX_CAPS, atomic_json, digest, totals
from . import evaluation_runner as runner
from .evaluation_inventory import verify_inventory, part5_parser

AREA = ROOT / 'docs/validation/part5-v1'
ID = re.compile(r'[a-z0-9][a-z0-9-]{2,79}')
MODEL = re.compile(r'[A-Za-z0-9_./:@+-]{1,160}')


class LiveConfigurationError(ValueError):
    """Fixed diagnostic codes only; never include file content or secret values."""
    pass


def load_live_settings(manifest, layer, env_file=None):
    names = ('OPENROUTER_API_KEY', 'OPENROUTER_MODEL', 'GOOGLE_CLOUD_PROJECT', 'BQ_LOCATION')
    values = {name: os.environ.get(name) for name in names}
    if env_file is not None:
        path = Path(env_file).expanduser()
        try:
            if not path.is_file():
                raise LiveConfigurationError('env_file_unavailable')
            supplied = config._read_dotenv(path)
        except (OSError, UnicodeError):
            raise LiveConfigurationError('env_file_unreadable') from None
        # Explicit file selection wins over stale shell exports. Never source
        # shell code, interpolate variables, or switch to a Gemini API key.
        values.update({name: supplied[name] for name in names if name in supplied})
    if not values['OPENROUTER_API_KEY']:
        raise LiveConfigurationError('openrouter_api_key_missing')
    model = values['OPENROUTER_MODEL'] or manifest['provider']['model']
    if model != manifest['provider']['model']:
        raise LiveConfigurationError('openrouter_model_mismatch')
    location = values['BQ_LOCATION'] or 'US'
    if layer == 'warehouse' and (values['GOOGLE_CLOUD_PROJECT'] != manifest['warehouse']['project'] or location != 'US'):
        raise LiveConfigurationError('warehouse_environment_incomplete')
    return Settings(project=manifest['warehouse']['project'] if layer == 'warehouse' else None,
                    location='US', model='unused-direct', openrouter_model=model,
                    openrouter_api_key=values['OPENROUTER_API_KEY'])


def playbook_reference(adapter, contract, driver, bindings):
    """Independently authored aggregate SQL and arithmetic; never production calculators.

    Only aggregate operands enter the private checkpoint. The public projection
    contains pass/fail checks, no rows or numerical business values.
    """
    from decimal import Decimal, localcontext
    from .compiler import BoundParameter
    dataset = 'bigquery-public-data.thelook_ecommerce'
    common = [BoundParameter('brands', 'ARRAY<STRING>', tuple(contract['brands'])),
              BoundParameter('statuses', 'ARRAY<STRING>', tuple(contract['statuses']))]
    source = f'''FROM `{dataset}.order_items` i
        JOIN `{dataset}.products` p ON p.id=i.product_id
        JOIN `{dataset}.orders` o ON o.order_id=i.order_id'''
    filters = '''p.brand IN UNNEST(@brands) AND i.status IN UNNEST(@statuses) AND i.sale_price IS NOT NULL'''
    method = contract['method']
    def run(sql, parameters):
        return adapter._run(sql, tuple(parameters), max_rows=20, purpose='reference').rows
    with localcontext() as ctx:
        ctx.prec = 80
        if method in {'product_revenue', 'regional_spend'}:
            common += [BoundParameter('start_ts', 'TIMESTAMP', datetime.fromisoformat(contract['start'])),
                       BoundParameter('end_ts', 'TIMESTAMP', datetime.fromisoformat(contract['end']))]
            filters += ' AND i.created_at>=@start_ts AND i.created_at<@end_ts'
            if method == 'product_revenue':
                ids = [bindings['product_1'], bindings['product_2']]
                common += [BoundParameter('products', 'ARRAY<INT64>', tuple(ids))]
                rows = run('SELECT i.product_id AS key, SUM(i.sale_price) AS r, COUNT(*) AS n ' + source +
                           ' WHERE '+filters+' AND i.product_id IN UNNEST(@products) GROUP BY i.product_id', common)
                values = {r['key']: (Decimal(str(r['r'])), Decimal(r['n'])) for r in rows}
                (r1, n1), (r2, n2) = (values[k] for k in ids)
                if n1 == 0 or n2 == 0:
                    return {'matches': False, 'reason': 'undefined_oracle_denominator'}
                p1, p2 = r1/n1, r2/n2
                expected = (r1-r2, (n1-n2)*(p1+p2)/2, (p1-p2)*(n1+n2)/2)
            else:
                # Orders are counted distinctly after qualifying item selection.
                rows = run('SELECT u.state AS key, SUM(i.sale_price) AS r, COUNT(DISTINCT o.order_id) AS n, '
                           'COUNT(DISTINCT o.user_id) AS c '+source+f' JOIN `{dataset}.users` u ON u.id=o.user_id '
                           'WHERE '+filters+" AND u.state IN ('Texas','California') GROUP BY u.state", common)
                values = {r['key']: tuple(Decimal(str(r[k])) for k in ('r','n','c')) for r in rows}
                (r1, n1, c1), (r2, n2, c2) = (values[k] for k in ('Texas','California'))
                if min(n1, n2, c1, c2) <= 0:
                    return {'matches': False, 'reason': 'undefined_oracle_denominator'}
                f1, f2, v1, v2 = n1/c1, n2/c2, r1/n1, r2/n2
                expected = (r1/c1-r2/c2, (f1-f2)*(v1+v2)/2, (v1-v2)*(f1+f2)/2)
        elif method == 'churn_change':
            from datetime import timedelta
            populations = []
            for cutoff in (contract['baseline_cutoff'], contract['cutoff']):
                instant = datetime.fromisoformat(cutoff)
                params = common + [BoundParameter('cutoff', 'TIMESTAMP', instant),
                                   BoundParameter('threshold', 'TIMESTAMP', instant-timedelta(days=90))]
                rows = run('''WITH last_purchase AS (SELECT o.user_id, MAX(i.created_at) AS last_at '''+
                           source+' WHERE '+filters+''' AND i.created_at<@cutoff GROUP BY o.user_id)
                           SELECT u.country AS country, COUNT(*) AS c,
                           COUNTIF(last_at<@threshold) AS inactive
                           FROM last_purchase l LEFT JOIN `'''+dataset+'''.users` u ON u.id=l.user_id
                           GROUP BY u.country''', params)
                populations.append({r['country']: (Decimal(r['c']), Decimal(r['inactive'])) for r in rows})
            before, after = populations
            if set(before) != set(after) or not before:
                return {'matches': False, 'reason': 'unaligned_oracle_population'}
            c0, c1 = sum(c for c,n in before.values()), sum(c for c,n in after.values())
            change = sum(n for c,n in after.values())/c1 - sum(n for c,n in before.values())/c0
            mix = sum((after[k][0]/c1-before[k][0]/c0)*(after[k][1]/after[k][0]+before[k][1]/before[k][0])/2 for k in before)
            within = sum((after[k][1]/after[k][0]-before[k][1]/before[k][0])*(after[k][0]/c1+before[k][0]/c0)/2 for k in before)
            expected = change, mix, within
        else:
            raise ValueError('unsupported_oracle_method')
        actual = [driver['total_difference'], *(c['value'] for c in driver['contributions'])]
        return {'matches': len(actual) == 3 and all(a is not None and abs(Decimal(a)-b) <= Decimal('1e-9')
                                                  for a,b in zip(actual, expected)),
                'snapshot_pinned': bool(getattr(adapter.client, 'snapshot_timestamp', None)),
                'oracle_version': 'independent-aggregate-v1'}


def snapshot_sql(sql, timestamp):
    """Pin catalog, analytics, coverage and independent oracle to the same instant."""
    instant = datetime.fromisoformat(timestamp)
    if instant.tzinfo is None or instant.utcoffset().total_seconds() != 0:
        raise ValueError('utc_snapshot_required')
    stamp = instant.astimezone(timezone.utc).isoformat()
    from sqlglot import parse_one, exp
    # BigQuery's table grammar is table [AS alias] [FOR SYSTEM_TIME ...].
    # Appending directly to the table token puts the clause before its alias.
    tree = parse_one(sql, read='bigquery')
    tables = list(tree.find_all(exp.Table))
    sources = [table for table in tables if table.catalog == 'bigquery-public-data'
               and table.db == 'thelook_ecommerce'
               and table.name in {'products', 'orders', 'users', 'order_items'}]
    if not sources or 'FOR SYSTEM_TIME' in sql.upper() or any(table.args.get('version') for table in tables):
        raise ValueError('unsupported_snapshot_query')
    version = exp.Version(this='TIMESTAMP', kind='AS OF',
        expression=exp.Timestamp(this=exp.Literal.string(stamp), with_tz=True))
    for table in sources:
        table.set('version', version.copy())
    return tree.sql(dialect='bigquery')


def validate(manifest, *, verify=True):
    if manifest.get('schema_version') != 2 or manifest.get('version') != 'part5-v1':
        raise ValueError('manifest_version')
    if (ROOT / '.git').exists() and manifest['source']['application_head'] != subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip():
        raise ValueError('application_head_changed')
    if verify:
        verify_inventory(ROOT, manifest['source'])
        for field, path in (('fixture_snapshot_sha256', AREA / 'source_rows.json'),
                            ('dependency_lock_sha256', ROOT / 'requirements.part5.lock'),
                            ('metadata_sha256', ROOT / 'pyproject.toml'),
                            ('driver_sha256', Path(__file__))):
            if manifest[field] != digest(path):
                raise ValueError('frozen_field_mismatch')
    if not MODEL.fullmatch(manifest['provider']['model']) or manifest['provider']['route'] != 'openrouter_only':
        raise ValueError('provider_configuration')
    if manifest['warehouse']['dataset'] != 'bigquery-public-data.thelook_ecommerce':
        raise ValueError('unsupported_dataset')
    if manifest['warehouse']['project'] is not None and not re.fullmatch(r'[a-z][a-z0-9-]{4,62}', manifest['warehouse']['project']):
        raise ValueError('invalid_project')
    for key in ('estimated_cost_usd', 'cost_limit_usd', 'maximum_cost_per_call_usd'):
        value = manifest['provider'][key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise ValueError('invalid_cost')
    if any(type(manifest['review'][k]) is not bool for k in ('owner_approved', 'paraphrases_independently_reviewed')):
        raise ValueError('invalid_review')
    if type(manifest['warehouse']['same_snapshot_authorized']) is not bool:
        raise ValueError('invalid_snapshot_authorization')
    if manifest['warehouse']['snapshot_timestamp'] is not None:
        snapshot_sql('SELECT * FROM `bigquery-public-data.thelook_ecommerce.products`', manifest['warehouse']['snapshot_timestamp'])
    caps = manifest['caps']
    if set(caps) != set(MAX_CAPS) or any(type(caps[k]) not in (int, float) or
            not math.isfinite(caps[k]) or not 0 < caps[k] <= MAX_CAPS[k] or
            (k != 'active_seconds' and type(caps[k]) is not int) for k in MAX_CAPS):
        raise ValueError('invalid_caps')
    specification = json.loads((ROOT / manifest['scenario_file']).read_text())
    if manifest['scenario_file'] not in manifest['source']['files']:
        raise ValueError('unfrozen_scenarios')
    ids = set()
    for case in specification['scenarios']:
        if not ID.fullmatch(case['id']) or case['id'] in ids:
            raise ValueError('scenario_id')
        ids.add(case['id'])
        if case['layer'] not in {'interpretation', 'warehouse'} or not case['turns']:
            raise ValueError('scenario_layer')
        if case['layer'] == 'interpretation' and case['mode'] != 'live_fixture':
            raise ValueError('fixture_mode_required')
        if case['layer'] == 'warehouse' and case['mode'] != 'live_bigquery':
            raise ValueError('warehouse_mode_required')
        for turn in case['turns']:
            if not ID.fullmatch(turn['id']) or turn['id'] in ids or not turn['assertions']:
                raise ValueError('turn_contract')
            if not set(turn['depends_on']).issubset(ids):
                raise ValueError('forward_dependency')
            ids.add(turn['id'])
        if case['layer'] == 'interpretation':
            path = case['fixture_snapshot']
            if path not in manifest['source']['files']:
                raise ValueError('unfrozen_fixture')
            case['fixture_snapshot'] = str(ROOT / path)
    return specification


def plan(manifest, specification, layer, selection=None):
    cases = [c for c in specification['scenarios'] if c['layer'] == layer]
    if selection:
        if not set(selection).issubset({c['id'] for c in cases}):
            raise ValueError('unknown_scenario_selection')
        cases = [c for c in cases if c['id'] in selection]
    turns = sum(len(c['turns']) for c in cases)
    # Production ceilings: six model dispatches, eight jobs and twelve dry runs per turn.
    # No extra unaccounted discovery or oracle dispatch is allowed.
    caps = manifest['caps']
    return {'layer': layer, 'scenario_ids': [c['id'] for c in cases], 'turns': turns,
            'maximum_provider_calls': min(turns * 6, caps['provider_attempts']),
            'maximum_warehouse_jobs': min(turns * 8, caps['warehouse_executions']) if layer == 'warehouse' else 0,
            'maximum_dry_runs': turns * 12 if layer == 'warehouse' else 0,
            'maximum_billed_bytes': caps['billed_bytes'] if layer == 'warehouse' else 0,
            'maximum_active_seconds': caps['active_seconds'], 'per_job_seconds': 30,
            'per_job_bytes': 512 * 1024**2, 'provider': 'openrouter_only',
            'model': manifest['provider']['model'], 'project': manifest['warehouse']['project'],
            'dataset': manifest['warehouse']['dataset'] if layer == 'warehouse' else 'synthetic_fixture_only',
            'snapshot_timestamp': manifest['warehouse']['snapshot_timestamp'] if layer == 'warehouse' else None,
            'estimated_cost_usd': manifest['provider']['estimated_cost_usd'],
            'cost_limit_usd': manifest['provider']['cost_limit_usd'],
            'required_environment_names': ['OPENROUTER_API_KEY'] +
                (['GOOGLE_CLOUD_PROJECT', 'GOOGLE_APPLICATION_CREDENTIALS (or ADC)'] if layer == 'warehouse' else []),
            'optional_environment_names': ['OPENROUTER_MODEL', 'BQ_LOCATION'],
            'cleanup': 'Retain sanitized records; remove only the declared disposable temporary state directory after review. No warehouse writes.',
            'dispatch': False}, cases


def environment_blocks(manifest, layer):
    blocks = []
    provider = manifest['provider']
    if not manifest['review']['paraphrases_independently_reviewed'] and layer == 'interpretation':
        blocks.append('paraphrase_review_missing')
    if not manifest['review']['owner_approved']:
        blocks.append('owner_plan_approval_missing')
    # Positive per-call reservation is reviewed against price/token ceilings outside Codex.
    for key in ('estimated_cost_usd', 'cost_limit_usd', 'maximum_cost_per_call_usd'):
        if type(provider[key]) not in (int, float) or not math.isfinite(provider[key]) or provider[key] <= 0:
            blocks.append('provider_cost_plan_missing')
            break
    if layer == 'warehouse':
        if not manifest['warehouse']['project']:
            blocks.append('project_missing')
        timestamp = manifest['warehouse']['snapshot_timestamp']
        if not timestamp or not manifest['warehouse']['same_snapshot_authorized']:
            blocks.append('same_snapshot_oracle_unavailable')
        else:
            snapshot_sql('SELECT * FROM `bigquery-public-data.thelook_ecommerce.products`', timestamp)
    return blocks


class CostObserver(runner.ProviderObserver):
    def __init__(self, account, provider):
        super().__init__(account)
        self.provider = provider

    def begin(self, model):
        from .evaluation_contracts import StopScheduling
        self.a.check()
        reserve = Decimal(str(self.provider['maximum_cost_per_call_usd']))
        settled = Decimal(0)
        pending = 0
        for event in self.a.state['events']:
            if event['kind'] != 'provider':
                continue
            if str(event['id']) in self.a.state['inflight']:
                pending += 1
                continue
            cost = event.get('cost')
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                # A failed/finished dispatch without known billing cannot release
                # its reservation. Do not send another request with unknown spend.
                self.a.state['stop'] = 'provider_cost_unknown_or_over_reservation'
                self.a.save()
                raise StopScheduling(self.a.state['stop'])
            settled += Decimal(str(cost))
        # Retain reservations for all in-flight calls and the proposed next call;
        # release the unused portion only after actual cost has been reported.
        if settled + (pending + 1) * reserve > Decimal(str(self.provider['cost_limit_usd'])):
            self.a.state['stop'] = 'provider_cost_reservation_cap'
            self.a.save()
            raise StopScheduling('provider_cost_reservation_cap')
        return super().begin(model)

    def result(self, event, payload):
        super().result(event, payload)
        cost = event.get('cost')
        if cost is None or cost > self.provider['maximum_cost_per_call_usd']:
            self.a.state['stop'] = 'provider_cost_unknown_or_over_reservation'
            self.a.save()
            from .evaluation_contracts import StopScheduling
            raise StopScheduling('provider_cost_unknown_or_over_reservation')


def public_failure_diagnostics(events):
    """Project only bounded gateway vocabulary; never arbitrary checkpoint text."""
    from .model_gateway import FAILURE_STAGES, FAILURE_CODES
    result = []
    for event in events:
        d = event.get('diagnostics', {})
        stage, code = d.get('failure_stage'), d.get('failure_code')
        if stage in FAILURE_STAGES and code in FAILURE_CODES:
            # Gateway issues are already sanitized; validate their shape again.
            issues = d.get('validation_issues', [])
            from .contracts import InterpretationProposal
            fields = set()
            def collect(v):
                if isinstance(v, dict):
                    fields.update(v.get('properties', {}))
                    for child in v.values(): collect(child)
                elif isinstance(v, list):
                    for child in v: collect(child)
            collect(InterpretationProposal.model_json_schema())
            from .model_gateway import SAFE_VALIDATION_RULES
            safe = []
            for issue in issues[:3] if isinstance(issues, list) else []:
                if not isinstance(issue, dict): continue
                field, rule = issue.get('field'), issue.get('rule')
                if (isinstance(field, str) and len(field) <= 180 and
                    all(part in fields | {'<field>', 'interpretation'} or part in {str(i) for i in range(21)}
                        for part in field.split('.')) and rule in SAFE_VALIDATION_RULES):
                    safe.append({'field':field, 'rule':rule})
            result.append({'stage':stage, 'code':code, 'issues':safe})
    return result


def public_result(account):
    """No transcripts, response excerpts, tokens, row values or exception strings."""
    rows = []
    for row in account.state['turns']:
        events = [e for e in account.state['events'] if e['turn'] == row['turn_id']]
        provider = [e for e in events if e['kind'] == 'provider']
        warehouse = [e for e in events if e['kind'] == 'warehouse']
        rows.append({'scenario': row['scenario'], 'case_id': row['turn_id'], 'status': row['status'],
                     'attempts': [{'ordinal': a['ordinal'], 'outcome': a['outcome'],
                                  'first_semantic_status': a.get('first_semantic_status', 'NOT_RUN'),
                                  'workflow_status': a.get('workflow_status') if a.get('workflow_status') in
                                      {'ok', 'partial', 'error', 'unsupported', 'clarification', 'needs_clarification', 'confirmation_required'} else None,
                                  'latency_seconds': a.get('elapsed_seconds'),
                                  'repair_count': max(0, a.get('observed', {}).get('interpretation_attempts', 0) - 1),
                                  'failures': public_failure_diagnostics([e for e in events if e['id'] in a.get('events', [])]) +
                                      [{'stage':'plan_validation', 'code':d['code'], 'issues':[]}
                                       for d in a.get('observed', {}).get('plan_failures', [])
                                       if d.get('code') in runner.PLAN_FAILURE_CODES],
                                  'checks': [{'passed': c['passed']} for c in a.get('assertions', [])],
                                  'provider_calls': sum(e['kind'] == 'provider' for e in account.state['events'] if e['id'] in a.get('events', [])),
                                  'warehouse_jobs': sum(e['kind'] == 'warehouse' for e in account.state['events'] if e['id'] in a.get('events', []))}
                                 for a in row['attempts']],
                     'retries': max(0, len(row['attempts']) - 1),
                     'cost_usd': sum(e['cost'] for e in provider) if all(e.get('cost') is not None for e in provider) else None,
                     'billed_bytes': sum(e['billed_bytes'] for e in warehouse) if all(e.get('billed_bytes') is not None for e in warehouse) else None})
    return {'schema_version': 2, 'classification_version': 2, 'first_semantic_version': 2, 'identity': account.state['identity'], 'cases': rows,
            'counts': totals(account.state['turns']), 'used': account.state['used'],
            'stop': account.state['stop'], 'acceptance_complete': False,
            'limits': 'Developer execution is not independent acceptance. Unknown usage remains null.'}


def main(argv=None):
    parser = part5_parser()
    args = parser.parse_args(argv)
    try:
        if not ID.fullmatch(args.run_id):
            raise ValueError('run_id')
        manifest = json.loads(args.manifest.read_text())
        specification = validate(manifest)
        summary, cases = plan(manifest, specification, args.layer, args.select)
        blocks = environment_blocks(manifest, args.layer)
        summary['blocks'] = blocks
        print(json.dumps(summary, allow_nan=False), flush=True)
        if args.dry_run or not args.live:
            return 0
        if not args.confirm_owner_run:
            raise ValueError('explicit_owner_confirmation_required')
        if blocks:
            raise ValueError('blocked_environment_review_plan')
        if args.state is None or (args.resume and not args.select):
            raise ValueError('explicit_disposable_state_and_resume_selection_required')
        state = args.state.absolute()
        if state.is_symlink() or state.resolve() != state or state.is_relative_to(ROOT.resolve()) or not state.is_relative_to(Path(tempfile.gettempdir()).resolve()) or state == Path(tempfile.gettempdir()).resolve():
            raise ValueError('private_temporary_state_required')
        settings = load_live_settings(manifest, args.layer, args.env_file)
        output = AREA / 'runs' / args.run_id
        if args.resume:
            runner.safe_private(state)
            if not output.is_dir():
                raise ValueError('resume_output_missing')
        else:
            state.mkdir(mode=0o700, exist_ok=False)
            (state / 'evaluation-private-marker').write_text('isolated Part 5 evaluation\n')
            output.mkdir(parents=True, exist_ok=False)
        identity = {'run_id': args.run_id, 'manifest_sha256': digest(args.manifest), 'layer': args.layer,
                    'source_revision': manifest['source']['revision'], 'scenario_sha256': digest(ROOT / manifest['scenario_file'])}
        with (state / 'runner.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            account = Accounting(state / 'checkpoint.json', identity=identity, caps=manifest['caps'])
            # Retain all layer cases so resume does not lose failures outside its selection.
            all_cases = deepcopy([c for c in specification['scenarios'] if c['layer'] == args.layer])
            for case in all_cases:
                if args.layer == 'warehouse':
                    case['snapshot_timestamp'] = manifest['warehouse']['snapshot_timestamp']
            from unittest.mock import patch
            try:
                with patch.object(runner, 'ProviderObserver', lambda a: CostObserver(a, manifest['provider'])):
                    runner.execute(account, {'scenarios': all_cases}, state, live=True, settings=settings,
                                   selection={c['id'] for c in cases})
            finally:
                verify_inventory(ROOT, manifest['source'])
                number = len(list(output.glob('results-*.json'))) + 1
                result_path = output / f'results-{number:03}.json'
                atomic_json(result_path, public_result(account))
                print(json.dumps({'phase': 'results_saved', 'results_path': str(result_path),
                    'selected_statuses': {r['turn_id']: r['status'] for r in account.state['turns']
                                          if r['scenario'] in {c['id'] for c in cases}},
                    'provider_attempts': account.state['used']['provider_attempts'],
                    'warehouse_executions': account.state['used']['warehouse_executions']}), flush=True)
            return 0 if all(r['status'] == 'PASS' for r in account.state['turns'] if r['scenario'] in {c['id'] for c in cases}) else 1
    except LiveConfigurationError as exc:
        parser.error(str(exc))
    except (ValueError, KeyError, OSError, json.JSONDecodeError, subprocess.CalledProcessError):
        parser.error('invalid_or_blocked_plan; inspect sanitized preflight and validation instructions')


if __name__ == '__main__':
    raise SystemExit(main())
