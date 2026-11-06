"""Eight interpretation-only live probes for scope versus grouping; no warehouse calls."""
from datetime import date
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

from opsfleet_assistant.compiler import compile_plan
from opsfleet_assistant.config import Settings
from pydantic import ValidationError
from opsfleet_assistant.contracts import AnalysisPlan, InterpretationProposal, InterpretationResult, Period, RequestSpec
from opsfleet_assistant.evaluation_contracts import Accounting, Redactor, StopScheduling, atomic_json, digest, verify_source
from opsfleet_assistant.evaluation_runner import ProviderObserver
from opsfleet_assistant.identity import Catalog, PolicyError, resolve_demo_scope
from opsfleet_assistant.intent import analytical_plans, resolve_interpretation
from opsfleet_assistant.model_gateway import GeminiGateway, ModelBudget, ModelFailure, safe_interpretation_issues

REFERENCE_DATE = date(2026, 11, 5)
ATTEMPT_CAP = 8
SECONDS_CAP = 180


def cases():
    tail = ' in January 2025, only Complete items, across my authorized brands.'
    result = [
        {'id':'scope_original','message':'Revenue in January 2025, only Complete items, across my authorized brands.','dimension':None},
        {'id':'scope_total','message':'Total revenue in January 2025, only Complete items, within my brand scope.','dimension':None},
        {'id':'by_brand','message':'Revenue by brand'+tail,'dimension':'brand'},
        {'id':'each_brand','message':'Revenue for each brand'+tail,'dimension':'brand'},
        {'id':'brand_breakdown','message':'Give a brand breakdown of revenue'+tail,'dimension':'brand'},
        {'id':'by_product','message':'Revenue by product'+tail,'dimension':'product'},
    ]
    for dimension in (None, 'brand'):
        seed_message = 'Revenue'+(' by brand' if dimension else '')+tail
        seed = RequestSpec.model_validate({'analysis':{'kind':'month','month':1,'year':{'kind':'absolute','value':2025}},
            'queries':[{'operation':'aggregate','metric':'revenue','dimension':dimension,'revenue_statuses':['Complete']}]})
        previous = AnalysisPlan(operation='aggregate', metric='revenue', dimension=dimension,
            revenue_statuses=('Complete',), period=Period(start=date(2025,1,1), end=date(2025,2,1)))
        # Explicit synthetic context in the same shape supplied to the gateway.
        # This is an interpretation probe, not a persisted workflow snapshot.
        context = {'messages':[{'role':'user','content':seed_message},
                    {'role':'assistant','content':'January 2025 revenue checked.'}],
            'pending_request':None,'unresolved_fields':[],'pending_temporal_anchors':{},
            'last_request':seed.model_dump(mode='json', exclude_defaults=True, exclude={'sources'}),
            'released_evidence':[],'cohort_origin':'business'}
        result.append({'id':'followup_'+('brand' if dimension else 'scalar'),
            'message':'What about January 2024, with the same choices?', 'dimension':dimension,
            'previous_plan':previous,'context':context,'year':2024})
    return result


def check_interpretation(value, case):
    outcome, resolved = resolve_interpretation(value, reference_date=REFERENCE_DATE,
        analytical_timezone='UTC', previous_plan=case.get('previous_plan'))
    year = case.get('year', 2025)
    checks = {'ready':outcome.status == 'ready' and resolved is not None,
        'one_analysis_query':bool(value.request and len(value.request.queries) == 1
            and value.request.intent == 'analysis' and value.request.mode == 'analysis'),
        'context_action':value.context_action == 'correct' if 'context' in case else value.context_action == 'new'}
    if 'context' in case:
        checks['temporal_updates'] = value.temporal_updates == ('analysis',)
    observed = {'status':outcome.status,'context_action':value.context_action,
        'temporal_updates':list(value.temporal_updates)}
    if resolved is not None:
        checks['period'] = resolved.analysis is not None and resolved.analysis.model_dump(mode='json') == {
            'start':f'{year}-01-01','end':f'{year}-02-01'} and resolved.comparison is None and resolved.action is None
        plans = analytical_plans(resolved)
        checks['one_plan'] = len(plans) == 1
        checks['grouping'] = len(plans) == 1 and plans[0].dimension == case['dimension']
        checks['metric_status'] = len(plans) == 1 and plans[0].operation == 'aggregate' and plans[0].metric == 'revenue' and set(plans[0].revenue_statuses) == {'Complete'}
        catalog = Catalog.from_products(({'id':1,'brand':'Calvin Klein'},{'id':2,'brand':'Carhartt'}))
        scope = resolve_demo_scope('manager_apparel', catalog)
        compiled = [compile_plan(plan, scope, catalog) for plan in plans]
        expected_columns = ['revenue'] if case['dimension'] is None else [
            'brand' if case['dimension'] == 'brand' else 'product_id','revenue']
        checks['compiled_shape'] = len(compiled) == 1 and list(compiled[0].result_contract.columns) == expected_columns
        checks['authorized_scope'] = all(tuple(query.parameter('brands').value) == ('Calvin Klein','Carhartt')
            and query.requested_product_ids is None for query in compiled)
        observed['dimensions'] = [plan.dimension for plan in plans]
    return checks, observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--followups-only', action='store_true', help='Only the two year-change cases; at most two requests.')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,79}', args.run_id): parser.error('invalid_run_id')
    source = json.loads((ROOT/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json').read_text())
    revision = verify_source(ROOT, source)
    settings = Settings.load()
    redactor = Redactor((settings.gemini_api_key,settings.openrouter_api_key))
    output = ROOT/'docs/remediation/runs/scope-grouping-prompt-20261105'/args.run_id
    output.mkdir(mode=0o700, exist_ok=False)
    private = Path(tempfile.mkdtemp(prefix='opsfleet-scope-grouping-probe-'))
    os.chmod(private, 0o700)
    before = {str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'runtime').rglob('*') if p.is_file()}
    identity = {'run_id':args.run_id,'source_revision':revision,'driver_sha256':digest(Path(__file__)),
        'review_status':'waived_by_user_for_live_testing'}
    tests = [case for case in cases() if 'context' in case] if args.followups_only else cases()
    attempt_cap = len(tests)
    atomic_json(output/'plan.json', redactor({**identity,'provider_attempt_cap':attempt_cap,
        'active_seconds_cap':SECONDS_CAP,'warehouse_operations_permitted':0,'selected_model':settings.openrouter_model,
        'synthetic_followup_context':True,'followups_only':args.followups_only,'runtime_before':before,
        'cases':[{**case,'previous_plan':case['previous_plan'].model_dump(mode='json')} if 'previous_plan' in case else case for case in tests]}))
    rows = []
    budget = ModelBudget(seconds=SECONDS_CAP, max_requests=attempt_cap)
    with (private/'runner.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        account = Accounting(private/'checkpoint.json', identity=identity, caps={
            'turns':attempt_cap,'provider_attempts':attempt_cap,'warehouse_executions':1,'billed_bytes':1,'active_seconds':SECONDS_CAP})

        class DiagnosticGateway(GeminiGateway):
            def _generate(self, prompt, schema, max_tokens):
                raw = super()._generate(prompt, schema, max_tokens)
                try:
                    proposal = InterpretationProposal.model_validate(raw)
                except ValidationError as exc:
                    row['wire_validation_issues'] = safe_interpretation_issues(exc)
                else:
                    # Only typed enums/role lists; never persist raw responses,
                    # rejected values, source phrases or validation messages.
                    row['proposal_metadata'] = {'status':proposal.status,
                        'context_action':proposal.context_action,'temporal_updates':list(proposal.temporal_updates),
                        'clear_fields':list(proposal.clear_fields),
                        'dimensions':[query.dimension for query in proposal.request.queries] if proposal.request else []}
                    try:
                        InterpretationResult.model_validate(proposal.model_dump())
                    except ValidationError as exc:
                        row['pre_reconciliation_issues'] = safe_interpretation_issues(exc)
                    else:
                        row['pre_reconciliation_issues'] = []
                return raw

        gateway = DiagnosticGateway(settings, budget=budget, provider_mode='openrouter_only', observer=ProviderObserver(account))
        for case in tests:
            row = {'case':case['id'],'status':'NOT_RUN','expected_dimension':case['dimension']}
            rows.append(row)
            if not settings.openrouter_api_key:
                row['reason'] = 'backup_unconfigured'; continue
            if account.state['stop']:
                row['reason'] = account.state['stop']; continue
            try:
                account.begin_turn(case['id'])
                value = gateway.interpret(case['message'], previous_plan=case.get('previous_plan'),
                    reference_date=REFERENCE_DATE.isoformat(), analytical_timezone='UTC',
                    conversation_context=case.get('context'))
                checks, observed = check_interpretation(value, case)
                row.update(status='PASS' if all(checks.values()) else 'FAIL', checks=checks, observed=observed)
            except ModelFailure as exc:
                row.update(status='ERROR',reason=exc.code,validation_issues=exc.validation_issues)
            except PolicyError as exc:
                row.update(status='FAIL',reason=exc.code)
            except StopScheduling:
                row.update(status='NOT_RUN',reason=account.state['stop'] or 'budget_stop')
            except Exception:
                row.update(status='ERROR',reason='diagnostic_internal_error')
                account.state['stop'] = 'diagnostic_internal_error'
            finally:
                account.end_turn()
                atomic_json(output/'partial-results.json',redactor({'identity':identity,'results':rows,
                    'events':account.state['events'],'used':account.state['used']}))
        after = {str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'runtime').rglob('*') if p.is_file()}
        verify_source(ROOT, source)
        result = redactor({'kind':'scope_grouping_interpretation_probe','identity':identity,'results':rows,
            'events':account.state['events'],'used':account.state['used'],'stop':account.state['stop'],
            'warehouse_operations':0,'runtime_unchanged':before == after,
            'limits':['Real model interpretation, local resolution and compilation only; no warehouse/report execution.',
                'Followup context is synthetic, not a persisted workflow journey. No raw replies retained.',
                'A passing finite probe does not guarantee every phrasing or complete live smoke.']})
        atomic_json(output/'results.json',result)
        print(json.dumps({'output':str(output/'results.json'),'statuses':[row['status'] for row in rows],
            'provider_attempts':account.state['used']['provider_attempts'],'stop':result['stop'],
            'runtime_unchanged':result['runtime_unchanged']}))
    return 0 if all(row['status'] == 'PASS' for row in rows) else 1


if __name__ == '__main__': raise SystemExit(main())
