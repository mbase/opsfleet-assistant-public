"""Bounded spend/action-horizon interpretation contrasts; dry by default, no warehouse."""
import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from opsfleet_assistant.config import Settings
from opsfleet_assistant.evaluation_contracts import Accounting, Redactor, StopScheduling, atomic_json, digest, verify_source
from opsfleet_assistant.evaluation_runner import ProviderObserver
from opsfleet_assistant.intent import analytical_plans, resolve_interpretation
from opsfleet_assistant.identity import PolicyError
from opsfleet_assistant.model_gateway import GeminiGateway, ModelBudget, ModelFailure

SECONDS_CAP=180
REFERENCE_DATE=date(2026,11,5)


def cases():
    return [
        {'id':'ambiguous_spend','message':'Compare spend in Texas and California in 2025.','status':'needs_clarification'},
        {'id':'explicit_average','message':'Compare average customer spend in Texas and California in 2025.',
            'status':'ready','operation':'demographic_comparison','metric':'average_customer_spend'},
        {'id':'explicit_total','message':'Compare total spend in Texas and California in 2025.','status':'unsupported'},
        {'id':'explicit_revenue','message':'Total revenue across my authorized brands in 2025.',
            'status':'ready','operation':'aggregate','metric':'revenue','dimension':None},
        {'id':'generic_actions','message':'Create a 2025 revenue report with actions.',
            'status':'ready','operation':'aggregate','metric':'revenue','report':True,'action':None},
        {'id':'generic_recommendations','message':'Create a 2025 revenue report with recommendations.',
            'status':'ready','operation':'aggregate','metric':'revenue','report':True,'action':None},
        {'id':'explicit_q4','message':'Create a Q3 2025 revenue report with Q4 actions.',
            'status':'ready','operation':'aggregate','metric':'revenue','report':True,
            'analysis':{'start':'2025-07-01','end':'2025-10-01'},'action':{'start':'2025-10-01','end':'2026-01-01'}},
        {'id':'next_quarter','message':'Create a Q4 2025 revenue report with next-quarter actions.',
            'status':'ready','operation':'aggregate','metric':'revenue','report':True,
            'analysis':{'start':'2025-10-01','end':'2026-01-01'},'action':{'start':'2026-01-01','end':'2026-04-01'}},
    ]


def check_interpretation(value,case):
    outcome,resolved=resolve_interpretation(value,reference_date=REFERENCE_DATE,analytical_timezone='UTC')
    checks={'status':outcome.status==case['status']}
    observed={'status':outcome.status}
    if case['status']!='ready':
        checks['no_analytical_plan']=resolved is None
        return checks,observed
    checks['resolved']=resolved is not None
    if resolved is None:return checks,observed
    plans=analytical_plans(resolved)
    observed.update(operations=[p.operation.value for p in plans],metrics=[p.metric.value for p in plans],
        periods={role:getattr(resolved,role).model_dump(mode='json') if getattr(resolved,role) else None
            for role in ('analysis','comparison','action')})
    checks['query']=len(plans)==1 and plans[0].operation==case['operation'] and plans[0].metric==case['metric']
    checks['analysis']=observed['periods']['analysis']==case.get('analysis',{'start':'2025-01-01','end':'2026-01-01'})
    checks['comparison']=resolved.comparison is None
    checks['action']=observed['periods']['action']==case.get('action')
    if 'dimension' in case:checks['dimension']=all(p.dimension==case['dimension'] for p in plans)
    if case.get('report'):checks['report']=resolved.spec.mode=='report' or resolved.spec.report is not None
    if case['id']=='explicit_average':
        checks['demographic']=len(plans)==1 and plans[0].dimension=='state' and plans[0].demographic_values==('Texas','California')
    return checks,observed


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,79}',args.run_id):parser.error('invalid_run_id')
    source=json.loads((ROOT/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json').read_text())
    revision=verify_source(ROOT,source)
    output=ROOT/'docs/remediation/runs'/args.run_id
    output.mkdir(mode=0o700,exist_ok=False)
    before={str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'runtime').rglob('*') if p.is_file()}
    settings=Settings.load() if args.live else None
    redactor=Redactor((settings.gemini_api_key,settings.openrouter_api_key) if settings else ())
    identity={'run_id':args.run_id,'source_revision':revision,'driver_sha256':digest(Path(__file__)),
        'live':args.live,'review_status':'waived_by_user' if args.live else 'dry_plan'}
    tests=cases()
    atomic_json(output/'plan.json',redactor({'identity':identity,'cases':tests,'provider_attempt_cap':len(tests),
        'active_seconds_cap':SECONDS_CAP,'warehouse_operations_permitted':0,'provider_turn_retries':0,
        'runtime_before':before,'selected_model':settings.openrouter_model if settings else None}))
    rows=[{'case':c['id'],'status':'NOT_RUN','reason':'dry_plan_no_dispatch'} for c in tests]
    events=[];used={'provider_attempts':0,'active_seconds':0};stop=None
    if args.live:
        private=Path(tempfile.mkdtemp(prefix='opsfleet-defaults-probe-'));os.chmod(private,0o700)
        account=Accounting(private/'checkpoint.json',identity=identity,caps={
            'turns':len(tests),'provider_attempts':len(tests),'warehouse_executions':1,'billed_bytes':1,'active_seconds':SECONDS_CAP})
        for case,row in zip(tests,rows):
            if account.state['stop']:
                row['reason']=account.state['stop'];continue
            if not settings.openrouter_api_key:
                row['reason']='backup_unconfigured';continue
            try:
                account.begin_turn(case['id'])
                budget=ModelBudget(seconds=max(.001,SECONDS_CAP-account.state['used']['active_seconds']),max_requests=1)
                gateway=GeminiGateway(settings,budget=budget,provider_mode='openrouter_only',observer=ProviderObserver(account))
                value=gateway.interpret(case['message'],previous_plan=None,reference_date=REFERENCE_DATE.isoformat(),analytical_timezone='UTC')
                checks,observed=check_interpretation(value,case)
                row.update(status='PASS' if all(checks.values()) else 'FAIL',reason=None,checks=checks,observed=observed)
            except ModelFailure as exc:row.update(status='ERROR',reason=exc.code)
            except PolicyError as exc:row.update(status='FAIL',reason=exc.code)
            except StopScheduling:row.update(status='NOT_RUN',reason=account.state['stop'] or 'budget_stop')
            except Exception:
                row.update(status='ERROR',reason='diagnostic_internal_error');account.state['stop']='diagnostic_internal_error'
            finally:
                account.end_turn()
                atomic_json(output/'partial-results.json',redactor({'identity':identity,'results':rows,'events':account.state['events'],'used':account.state['used']}))
        events=account.state['events'];used=account.state['used'];stop=account.state['stop']
    verify_source(ROOT,source)
    after={str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'runtime').rglob('*') if p.is_file()}
    result=redactor({'identity':identity,'results':rows,'events':events,'used':used,'stop':stop,
        'warehouse_operations':0,'runtime_unchanged':before==after,
        'limits':['Interpretation and local resolution only; no warehouse/report execution or persisted journey.','No raw replies retained.']})
    atomic_json(output/'results.json',result)
    print(json.dumps({'output':str(output/'results.json'),'statuses':[r['status'] for r in rows],
        'provider_attempts':used['provider_attempts'],'runtime_unchanged':result['runtime_unchanged']}))
    return 0 if not args.live or all(r['status']=='PASS' for r in rows) else 1

if __name__=='__main__':raise SystemExit(main())
