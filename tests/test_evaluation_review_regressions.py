"""Independent-expectation regressions for the returned preparation review.

All HTTP/ADC transports are substituted; real compiler/release/storage stay active.
No independent reviewer artifact is imported, modified or rewritten.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from google.api_core import exceptions as ge
import pytest

from opsfleet_assistant import evaluation_runner as runner
from opsfleet_assistant.contracts import (ActionSelection, InterpretationResult, QuerySpec, ReportDraft,
    ReportSpec, RequestSpec, TemporalExpression as T, YearExpression as Y)
from opsfleet_assistant.evaluation_contracts import Accounting, MAX_CAPS, assertions, totals
from opsfleet_assistant.evaluation_fixtures import FixtureClient
from test_evaluation_preparation import SETTINGS, account, http_transport

ROOT=Path(__file__).resolve().parents[1]

def cases(): return {c['id']:c for c in json.loads((ROOT/'docs/remediation/SCENARIO_MANIFEST.json').read_text())['scenarios']}
def quarter(n,year=2025):return T(kind='quarter',quarter=n,year=Y(kind='absolute',value=year))
def q(**kw):return QuerySpec(operation='aggregate',metric='revenue',revenue_statuses=('Complete',),**kw)
def report():return ReportDraft(recommendations=(ActionSelection(kind='review_performance',finding_index=0),))

def observed_case(tmp_path,monkeypatch,key,spec,*,draft=None,turn_index=0):
    c=copy.deepcopy(cases()[key]);t=c['turns'][turn_index]
    http_transport(monkeypatch,[InterpretationResult(status='ready',request=spec).model_dump_json(),
                              (draft or ReportDraft()).model_dump_json()])
    monkeypatch.setattr(runner,'_client_from_adc',lambda s:FixtureClient(c.get('fixture','standard')))
    a=account(tmp_path);a.begin_turn(t['id'])
    observed=runner.run_workflow(c,t,tmp_path,a,SETTINGS,{})
    a.end_turn()
    return observed,assertions(t['assertions'],observed)


@pytest.mark.parametrize('case_id,wrong',[
 ('live-products',RequestSpec(analysis=T(kind='year',year=Y(kind='absolute',value=2025)),queries=(q(),))),
 ('num-increase',RequestSpec(mode='temporal_compare',analysis=T(kind='interval',start='2025-07-01',end='2026-01-01'),
    comparison=T(kind='interval',start='2025-01-01',end='2025-07-01'),queries=tuple(
       QuerySpec(operation='aggregate',metric='revenue',temporal_role=r,requested_brands=('Calvin Klein',),
                 revenue_statuses=('Complete','Shipped')) for r in ('analysis','comparison')))),
 ('report-rollover',RequestSpec(mode='report',analysis=quarter(3),action=quarter(1,2026),queries=(q(),),
    report=ReportSpec(presentation='narrative',detail='concise'))),
])
def test_r1_original_wrong_interpretations_reject(tmp_path,monkeypatch,case_id,wrong):
    observed,checks=observed_case(tmp_path,monkeypatch,case_id,wrong,draft=report())
    assert observed['status']=='ok'  # the product legitimately answers its typed proposal
    assert not all(c['passed'] for c in checks)  # the evaluation must reject the different question


def turn_manifest():
    t={'id':'interrupted-t1','depends_on':[],'assertions':[{'path':'status','op':'eq','value':'ok'}]}
    return {'scenarios':[{'id':'interrupted','mode':'live_fixture','turns':[t]}]}


def test_r2_keyboard_interrupt_retains_one_started_attempt_and_never_replays(tmp_path,monkeypatch):
    a=account(tmp_path);m=turn_manifest();calls=[]
    def interrupted(*args):
        calls.append('start');a.reserve('provider',route='openrouter_gemini',requested_model='frozen')
        raise KeyboardInterrupt()
    monkeypatch.setattr(runner,'run_workflow',interrupted)
    with pytest.raises(KeyboardInterrupt):runner.execute(a,m,tmp_path,live=True,settings=SETTINGS)
    b=Accounting(a.path,identity=a.state['identity'],caps=a.state['caps'])
    rows=runner.execute(b,m,tmp_path,live=True,settings=SETTINGS)
    assert totals(rows)['attempted_turns']==1 and totals(rows)['attempted_scenarios']==1
    assert rows[0]['status']=='BLOCKED_ENVIRONMENT'
    assert len(rows[0]['attempts'])==1 and rows[0]['attempts'][0]['events']==[1]
    assert b.state['used']['provider_attempts']==1 and calls==['start'] and b.state['stop']


@pytest.mark.parametrize('existing_not_run',[False,True])
def test_r2_hard_stop_reconciles_prior_unselected_record(tmp_path,monkeypatch,existing_not_run):
    a=account(tmp_path);m=turn_manifest();key='interrupted-t1'
    if existing_not_run: runner.execute(a,m,tmp_path,live=False)
    a.begin_turn(key);a.reserve('warehouse',cap=25,purpose='catalog')
    persisted=a.path.read_bytes()  # emulate death without Python finally; no actual transport
    b=Accounting(a.path,identity=a.state['identity'],caps=a.state['caps'])
    monkeypatch.setattr(runner,'run_workflow',lambda *args:pytest.fail('interrupted dispatch replayed'))
    rows=runner.execute(b,m,tmp_path,live=True,settings=SETTINGS)
    assert totals(rows)['attempted_turns']==1 and len(rows[0]['attempts'])==1
    assert rows[0]['status']=='BLOCKED_ENVIRONMENT' and rows[0]['attempts'][0]['events']==[1]
    assert b.state['used']['warehouse_executions']==1 and b.state['used']['billed_bytes']==0
    assert b.state['inflight']['1']['cap']==25
    assert persisted  # original checkpoint was real, not only in-memory state


@pytest.mark.parametrize('where',['catalog','analysis'])
def test_r3_warehouse_permission_before_or_after_model_is_environment(tmp_path,monkeypatch,where):
    c=copy.deepcopy(cases()['live-journey']);c['turns']=c['turns'][:2]
    class Forbidden(FixtureClient):
        def query(self,sql,*,job_config,**kwargs):
            if where=='catalog' or 'order_items' in sql:raise ge.Forbidden('private warehouse sentinel')
            return super().query(sql,job_config=job_config,**kwargs)
    http_transport(monkeypatch,[InterpretationResult(status='ready',request=RequestSpec(
        analysis=T(kind='month',month=1,year=Y(kind='absolute',value=2025)),queries=(q(),))).model_dump_json()])
    monkeypatch.setattr(runner,'_client_from_adc',lambda s:Forbidden())
    a=account(tmp_path);rows=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    assert rows[0]['status']=='BLOCKED_ENVIRONMENT'
    assert rows[1]['status']=='NOT_RUN' and rows[1]['reason']=='prerequisite_did_not_pass'
    assert a.state['used']['provider_attempts']==(1 if where=='analysis' else 0)
    assert 'private warehouse sentinel' not in json.dumps(rows)


@pytest.mark.parametrize('case_id,spec',[
 ('live-products',RequestSpec(analysis=T(kind='year',year=Y(kind='absolute',value=2025)),queries=(
    QuerySpec(operation='product_comparison',metric='revenue',product_ids=(41001,41002),revenue_statuses=('Complete',)),))),
 ('num-increase',RequestSpec(mode='temporal_compare',analysis=quarter(3),comparison=quarter(2),queries=(q(),q(temporal_role='comparison')))),
 ('report-rollover',RequestSpec(mode='report',analysis=quarter(4),action=quarter(1,2026),queries=(q(),),
    report=ReportSpec(presentation='table',detail='detailed'))),
])
def test_r1_correct_counterparts_pass_same_trusted_boundaries(tmp_path,monkeypatch,case_id,spec):
    observed,checks=observed_case(tmp_path,monkeypatch,case_id,spec,draft=report())
    assert observed['status']=='ok'
    assert all(c['passed'] for c in checks),[c['rule'] for c in checks if not c['passed']]
    if case_id=='live-products':
        assert observed['total_query_executions_including_reference']==4  # catalog, discovery, analysis, coverage
        assert observed['data_precondition']['status']=='met'


@pytest.mark.parametrize('mutation,failed_suffix',[
    ('analysis','parameters.end_ts'),('comparison','parameters.start_ts'),('scope','brands'),
    ('statuses','status_set'),('product','products'),('metric','metric'),('grouping','columns'),
])
def test_r1_each_numeric_semantic_dimension_rejects_coincident_or_valid_numbers(tmp_path,monkeypatch,mutation,failed_suffix):
    analysis=quarter(3);comparison=quarter(2);options={}
    if mutation=='analysis':analysis=T(kind='interval',start='2025-07-01',end='2026-01-01')
    if mutation=='comparison':comparison=T(kind='interval',start='2025-01-01',end='2025-07-01')
    if mutation=='scope':options['requested_brands']=('Calvin Klein',)
    if mutation=='product':options['product_ids']=(41001,)
    if mutation=='grouping':options['dimension']='product'
    metric='order_count' if mutation=='metric' else 'revenue'
    statuses=('Complete','Shipped') if mutation=='statuses' else ('Complete',)
    qs=tuple(QuerySpec(operation='aggregate',metric=metric,revenue_statuses=statuses,temporal_role=role,**options)
             for role in ('analysis','comparison'))
    observed,checks=observed_case(tmp_path,monkeypatch,'num-increase',RequestSpec(mode='temporal_compare',analysis=analysis,
        comparison=comparison,queries=qs))
    assert observed['status']=='ok'
    assert any(not c['passed'] and c['rule']['path'].startswith('compiled_queries.') and
               c['rule']['path'].endswith(failed_suffix) for c in checks)


@pytest.mark.parametrize('mutation',['analysis','action','presentation','detail'])
def test_r1_report_period_layout_detail_sections_reject_independently(tmp_path,monkeypatch,mutation):
    kwargs={'presentation':'table','detail':'detailed'}
    if mutation=='presentation':kwargs['presentation']='narrative'
    if mutation=='detail':kwargs['detail']='concise'
    spec=RequestSpec(mode='report',analysis=quarter(3 if mutation=='analysis' else 4),
        action=quarter(2 if mutation=='action' else 1,2026),queries=(q(),),report=ReportSpec(**kwargs))
    _,checks=observed_case(tmp_path,monkeypatch,'report-rollover',spec,draft=report())
    assert not all(c['passed'] for c in checks)


def test_delivered_table_satisfies_layout_despite_shorter_proposal(tmp_path,monkeypatch):
    spec=RequestSpec(mode='report',analysis=quarter(4),action=quarter(1,2026),queries=(q(),),
        report=ReportSpec(presentation='table',detail='detailed',
            requested_sections=('summary','limitations','recommendations')))
    observed,checks=observed_case(tmp_path,monkeypatch,'report-rollover',spec,draft=report())
    assert all(c['passed'] for c in checks)
    assert 'performance_table' in observed['supported_report_sections']
    # Exercise the real observation boundary with absent and unsupported content.
    from opsfleet_assistant.workflow import TurnResult
    for absent in (True,False):
        composition=copy.deepcopy(observed['report_composition'])
        if absent:
            composition['sections']=[s for s in composition['sections'] if s['kind']!='performance_table']
        else:
            next(s for s in composition['sections'] if s['kind']=='performance_table')['status']='missing'
        forged=runner.observe_result(TurnResult('ok','', 'c','t',report_composition=composition),[],[])
        table_rules=[c['rule'] for c in checks if c['rule']['path']=='supported_report_sections']
        assert not all(c['passed'] for c in assertions(table_rules,forged))


@pytest.mark.parametrize('products,operation',[((41002,41001),'product_comparison'),((41001,),'aggregate')])
def test_r1_discovered_product_filter_order_and_population_are_binding(tmp_path,monkeypatch,products,operation):
    spec=RequestSpec(analysis=T(kind='year',year=Y(kind='absolute',value=2025)),queries=(QuerySpec(
        operation=operation,metric='revenue',product_ids=products,revenue_statuses=('Complete',)),))
    observed,checks=observed_case(tmp_path,monkeypatch,'live-products',spec)
    assert observed['catalog_bindings']=={'product_1':41001,'product_2':41002}
    assert any(not c['passed'] and c['rule']['path']=='compiled_queries.0.products' for c in checks)


def test_r1_product_reuse_preserves_catalog_bindings_measured_groups_and_operands(tmp_path,monkeypatch):
    c=copy.deepcopy(cases()['novel-products']);a=account(tmp_path)
    spec=RequestSpec(analysis=T(kind='month',month=1,year=Y(kind='absolute',value=2025)),queries=(QuerySpec(
        operation='product_comparison',metric='revenue',product_ids=(41001,41002),revenue_statuses=('Complete',)),))
    messages=[InterpretationResult(status='ready',request=spec),ReportDraft(),
              InterpretationResult(status='ready',context_action='continue',request=RequestSpec(intent='why'))]
    http_transport(monkeypatch,[x.model_dump_json() for x in messages])
    rows=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    assert [r['status'] for r in rows]==['PASS','PASS']
    obs=rows[1]['attempts'][0]['observed']
    assert obs['compiled_queries']==[] and obs['evidence_semantics'][0]['measured_groups']==[41001,41002]
    assert obs['catalog_bindings']=={'product_1':41001,'product_2':41002}
    assert any(f['selection']['operation']=='difference' and f['value']=='-10' for f in obs['factual_findings'])


def test_r1_novel_role_layout_retention_is_enforced_after_restart(tmp_path,monkeypatch):
    c=copy.deepcopy(cases()['novel-role-layout']);a=account(tmp_path)
    first=RequestSpec(mode='report',analysis=quarter(4),comparison=quarter(4,2024),action=quarter(1,2026),
        queries=(q(),q(temporal_role='comparison')),report=ReportSpec(presentation='table',detail='concise',
        requested_sections=('summary','performance_table','comparison','limitations','recommendations')))
    second=first.model_copy(update={'action':quarter(2,2026),'report':None})
    payloads=[InterpretationResult(status='ready',request=first),report(),
              InterpretationResult(status='ready',context_action='correct',request=second),report()]
    http_transport(monkeypatch,[p.model_dump_json() for p in payloads])
    rows=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    assert [r['status'] for r in rows]==['PASS','PASS']
    observed=rows[1]['attempts'][0]['observed']
    forged=copy.deepcopy(observed);forged['report_composition']['spec']['detail']='detailed'
    assert not all(x['passed'] for x in assertions(c['turns'][1]['assertions'],forged))


def test_r1_manifest_audit_all_workflow_turns_have_explicit_semantic_dimensions():
    for c in cases().values():
        for t in c['turns']:
            contract=t['semantic_contract']
            if c['mode']=='controlled_regression':continue
            assert contract['kind'] in {'execution','reuse','clarification','refusal','late_slot','empty_comparison','bounded_modified_save'}
            if contract['kind']=='bounded_modified_save':
                assert t['id'] in {'novel-narrow-save-t02','novel-zero-save-t02'}
                assert contract['ready']['kind'] in {'execution','reuse'}
                assert any(r['path']=='status' and r.get('value')==['ok','needs_clarification'] for r in t['assertions'])
            core=contract.get('ready',contract)
            if core['kind'] not in {'execution','reuse'}:continue
            for q_ in core['queries']:
                assert set(q_)=={'operation','metric','columns','brands','status_set','products','period'}
                assert set(q_['period'])=={'start','end'}
            assert set(core['periods'])=={'analysis','comparison','action'}
            assert isinstance(core['saved'],bool)


@pytest.mark.parametrize('field,value',[
    ('current_value','201'),('baseline_value','101'),
    ('current',{'evidence_index':1,'row_index':0,'metric':'revenue'}),
    ('baseline',{'evidence_index':0,'row_index':0,'metric':'revenue'}),
])
def test_r1_numeric_values_alone_do_not_replace_named_operands_and_roles(tmp_path,monkeypatch,field,value):
    spec=RequestSpec(mode='temporal_compare',analysis=quarter(3),comparison=quarter(2),queries=(q(),q(temporal_role='comparison')))
    obs,checks=observed_case(tmp_path,monkeypatch,'num-increase',spec)
    assert all(c['passed'] for c in checks)
    for f in obs['factual_findings']:
        if field in ('current','baseline'):f['selection'][field]=value
        else:f[field]=value
    assert not all(c['passed'] for c in assertions(cases()['num-increase']['turns'][0]['assertions'],obs))


def test_r1_reference_uses_authored_inputs_even_when_wrong_observed_period_coincides(tmp_path,monkeypatch):
    c=copy.deepcopy(cases()['live-journey']);t=c['turns'][0];inputs=[]
    class Capture(FixtureClient):
        def query(self,sql,*,job_config,**kwargs):
            if (job_config.labels or {}).get('purpose')=='opsfleet_reference':
                inputs.append({p.name:(list(p.values) if hasattr(p,'values') else p.value) for p in job_config.query_parameters})
            return super().query(sql,job_config=job_config,**kwargs)
    monkeypatch.setattr(runner,'_client_from_adc',lambda s:Capture())
    # February 2024 and January 2025 independently both have30 in the fixture.
    spec=RequestSpec(analysis=T(kind='month',month=2,year=Y(kind='absolute',value=2024)),queries=(q(),))
    http_transport(monkeypatch,[InterpretationResult(status='ready',request=spec).model_dump_json(),ReportDraft().model_dump_json()])
    a=account(tmp_path);a.begin_turn(t['id'])
    obs=runner.run_workflow(c,t,tmp_path,a,SETTINGS,{});a.end_turn()
    assert obs['independent_reference']['matches']  # coincidence is preserved, never the sole oracle
    assert inputs[0]['start_ts'].isoformat()=='2025-01-01T00:00:00+00:00'
    assert inputs[0]['end_ts'].isoformat()=='2025-02-01T00:00:00+00:00'
    assert not all(r['passed'] for r in assertions(t['assertions'],obs))


@pytest.mark.parametrize('existing_not_run',[False,True])
def test_r2_abrupt_child_exit_persists_attempt_before_dispatch_and_preserves_old_snapshot(tmp_path,monkeypatch,existing_not_run):
    import os, subprocess, sys
    a=account(tmp_path);m=turn_manifest()
    if existing_not_run:runner.execute(a,m,tmp_path,live=True,selection={'other'},settings=SETTINGS)
    old=json.dumps({'results':a.state['turns'],'used':a.state['used']},sort_keys=True).encode()
    snapshot=tmp_path/'results-batch-001.json';snapshot.write_bytes(old)
    code='''
import json,os,sys
from pathlib import Path
from opsfleet_assistant import evaluation_runner as runner
from opsfleet_assistant.evaluation_contracts import Accounting
private=Path(sys.argv[1]);state=json.loads((private/'checkpoint.json').read_text())
a=Accounting(private/'checkpoint.json',identity=state['identity'],caps=state['caps'])
m=json.loads(sys.argv[2])
def abrupt(*args):
    a.reserve('warehouse',cap=25,purpose='analysis')
    os._exit(23)
runner.run_workflow=abrupt
runner.execute(a,m,private,live=True)
'''
    proc=subprocess.run([sys.executable,'-c',code,str(tmp_path),json.dumps(m)],
        env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(ROOT/'src')},capture_output=True,timeout=5)
    assert proc.returncode==23
    checkpoint=json.loads(a.path.read_text())
    started=checkpoint['turns'][0]['attempts'][0]
    assert started['outcome']=='STARTED' and started['attempt_id'].endswith(':interrupted-t1:1')
    assert checkpoint['active_turn']=='interrupted-t1' and checkpoint['events'][0]['outcome']=='dispatched'
    b=Accounting(a.path,identity=a.state['identity'],caps=a.state['caps'])
    monkeypatch.setattr(runner,'run_workflow',lambda *args:pytest.fail('abrupt attempt replayed'))
    rows=runner.execute(b,m,tmp_path,live=True)
    assert rows[0]['attempts'][0]['outcome']=='INTERRUPTED' and rows[0]['attempts'][0]['events']==[1]
    assert totals(rows)['attempted_turns']==1 and totals(rows)['attempted_scenarios']==1
    assert b.state['used']['active_seconds']>=120
    assert b.state['used']['warehouse_executions']==1 and b.state['inflight']['1']['cap']==25
    assert snapshot.read_bytes()==old
    runner.execute(b,m,tmp_path,live=True)
    assert len(b.state['turns'][0]['attempts'])==1  # recovery is idempotent


@pytest.mark.parametrize('fault,reason,executions,billing_unknown',[
    ('missing-project','missing_project',0,False),('adc','credentials_unavailable',0,False),
    ('transient','provider_transient',1,False),('submitted-known','permission_denied',2,False),
    ('submitted-unknown','timeout_cancellation_requested',2,True),
])
def test_r3_configuration_transient_and_submitted_billing_keep_safe_boundary(tmp_path,monkeypatch,fault,reason,executions,billing_unknown):
    from dataclasses import replace
    from google.auth.exceptions import DefaultCredentialsError
    from opsfleet_assistant.evaluation_fixtures import FixtureJob
    class FaultClient(FixtureClient):
        def query(self,sql,*,job_config,**kwargs):
            if 'order_items' in sql:
                if fault=='transient':raise ge.ServiceUnavailable('private transient warehouse diagnostic')
                if fault.startswith('submitted') and not job_config.dry_run:
                    job=FixtureJob();job.total_bytes_billed=None if billing_unknown else 11
                    def failed(**kwargs):
                        if billing_unknown:raise TimeoutError('private job timeout')
                        raise ge.Forbidden('private job denial')
                    job.result=failed
                    return job
            return super().query(sql,job_config=job_config,**kwargs)
    def client(s):
        if fault=='adc':raise DefaultCredentialsError('private authentication diagnostic')
        return FaultClient()
    monkeypatch.setattr(runner,'_client_from_adc',client)
    c=copy.deepcopy(cases()['live-journey']);c['turns']=c['turns'][:2]
    spec=RequestSpec(analysis=T(kind='month',month=1,year=Y(kind='absolute',value=2025)),queries=(q(),))
    http_transport(monkeypatch,[InterpretationResult(status='ready',request=spec).model_dump_json()])
    settings=replace(SETTINGS,project=None) if fault=='missing-project' else SETTINGS
    a=account(tmp_path);rows=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=settings)
    assert [r['status'] for r in rows]==['BLOCKED_ENVIRONMENT','NOT_RUN']
    obs=rows[0]['attempts'][0]['observed'];assert obs['terminal_error']['reason']==reason
    assert obs['terminal_error']['boundary']=='warehouse'
    assert a.state['used']['warehouse_executions']==executions
    assert a.state['used']['provider_attempts']==(0 if fault in {'missing-project','adc'} else 1)
    if fault=='submitted-known':assert a.state['used']['billed_bytes']==11
    if billing_unknown:
        assert a.state['stop']=='unknown_warehouse_billing' and obs['billed_bytes'] is None
        assert a.state['events'][-1]['billed_bytes'] is None
    assert 'private ' not in json.dumps(rows)


def test_r3_actual_assertion_or_warehouse_contract_defect_is_fail_not_environment(tmp_path,monkeypatch):
    c=copy.deepcopy(cases()['live-journey']);c['turns']=c['turns'][:1]
    class BadSchema(FixtureClient):
        def query(self,sql,*,job_config,**kwargs):
            if 'order_items' in sql:raise ge.BadRequest('syntax error in controlled SQL')
            return super().query(sql,job_config=job_config,**kwargs)
    http_transport(monkeypatch,[InterpretationResult(status='ready',request=RequestSpec(
        analysis=T(kind='month',month=1,year=Y(kind='absolute',value=2025)),queries=(q(),))).model_dump_json()])
    monkeypatch.setattr(runner,'_client_from_adc',lambda s:BadSchema())
    a=account(tmp_path);rows=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    assert rows[0]['status']=='FAIL'
    assert rows[0]['attempts'][0]['observed']['terminal_error']['reason']=='query_syntax_error'
