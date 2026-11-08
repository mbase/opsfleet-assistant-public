"""Post-full-run regressions: substituted transports, never live semantic proof."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from google.cloud import bigquery
import pytest

from opsfleet_assistant import evaluation_runner as runner
from opsfleet_assistant.bigquery_adapter import BigQueryTurn, QueryLimits
from opsfleet_assistant.compiler import BoundParameter
from opsfleet_assistant.config import ROOT
from opsfleet_assistant.contracts import InterpretationResult, RequestSpec, QuerySpec, TemporalExpression, YearExpression, ReportSpec, ReportDraft
from opsfleet_assistant.evaluation_contracts import assertions
from opsfleet_assistant.evaluation_fixtures import FixtureClient
from opsfleet_assistant.identity import resolve_demo_scope
from test_evaluation_preparation import SETTINGS, account, http_transport


def case(name):
    return deepcopy(next(c for c in json.loads((ROOT/'docs/remediation/SCENARIO_MANIFEST.json').read_text())['scenarios'] if c['id']==name))


@pytest.mark.parametrize('profile,start,end,source,period', [
    ('empty','2025-01-01','2026-01-01',0,0),
    ('standard','2030-01-01','2031-01-01',18,0),
    ('standard','2025-01-01','2026-01-01',18,10)])
def test_countif_preserves_other_empty_aggregate_semantics(profile,start,end,source,period):
    from opsfleet_assistant.bigquery_adapter import _COVERAGE_SQL
    client=FixtureClient(profile)
    params=[bigquery.ArrayQueryParameter('brands','STRING',['Calvin Klein','Carhartt']),
        bigquery.ArrayQueryParameter('statuses','STRING',['Complete']),
        bigquery.ScalarQueryParameter('start_ts','TIMESTAMP',datetime.fromisoformat(start+'T00:00:00+00:00')),
        bigquery.ScalarQueryParameter('end_ts','TIMESTAMP',datetime.fromisoformat(end+'T00:00:00+00:00'))]
    row=client.query(_COVERAGE_SQL,job_config=bigquery.QueryJobConfig(query_parameters=params)).rows[0]
    assert row['source_rows']==source and row['period_rows']==period
    nulls=client.query('SELECT SUM(sale_price) AS revenue, AVG(sale_price) AS average_item_price, MAX(created_at) AS latest_item_at FROM order_items WHERE 1=0',job_config=bigquery.QueryJobConfig()).rows[0]
    assert nulls=={'revenue':None,'average_item_price':None,'latest_item_at':None}
    client.db.close()


def test_empty_report_uses_real_fixture_adapter_and_saves_partial(tmp_path,monkeypatch):
    c=case('report-empty')
    req=RequestSpec(mode='report',analysis=TemporalExpression(kind='year',year=YearExpression(kind='absolute',value=2025)),queries=(QuerySpec(operation='aggregate',metric='revenue'),),report=ReportSpec())
    http_transport(monkeypatch,[InterpretationResult(status='ready',request=req).model_dump_json(),ReportDraft().model_dump_json()])
    a=account(tmp_path)
    row,=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    observed=row['attempts'][0]['observed']
    assert row['status']=='PASS' and observed['status']=='partial'
    assert observed['saved'] and observed['factual_findings']==[]
    assert observed['resolved_periods']['action'] is None
    assert observed['presentation']['evidence'][0]['coverage']['period_rows']==0
    assert observed['factual_findings']==[]  # Compiler's explicit COALESCE is never an observation.


def simple_manifest():
    def turn(id,deps=()):return {'id':id,'depends_on':list(deps),'assertions':[{'path':'status','op':'eq','value':'ok'}]}
    return {'scenarios':[{'id':'a','mode':'live_fixture','turns':[turn('a1'),turn('a2',['a1'])]},
        {'id':'b','mode':'live_fixture','turns':[turn('b1')]}]}


def test_skip_reasons_survive_later_batches_and_eligible_work_runs(tmp_path,monkeypatch):
    a=account(tmp_path); calls=[]
    def work(c,t,*args): calls.append(t['id']); return {'status':'bad' if t['id']=='a1' else 'ok'}
    monkeypatch.setattr(runner,'run_workflow',work)
    runner.execute(a,simple_manifest(),tmp_path,live=True,selection={'a'})
    rows=runner.execute(a,simple_manifest(),tmp_path,live=True,selection={'b'})
    assert calls==['a1','b1']
    assert rows[1]['reason']=='prerequisite_did_not_pass' and rows[1]['attempts']==[]
    assert rows[2]['status']=='PASS'
    runner.execute(a,simple_manifest(),tmp_path,live=True,selection={'a'})
    assert calls==['a1','b1'] and a.state['used']['turns']==2


def test_cap_skip_reason_survives_unrelated_batch(tmp_path,monkeypatch):
    a=account(tmp_path,turns=1)
    monkeypatch.setattr(runner,'run_workflow',lambda *args:{'status':'ok'})
    runner.execute(a,simple_manifest(),tmp_path,live=True,selection={'a'})
    assert a.state['turns'][1]['reason']=='turns_cap'
    runner.execute(a,simple_manifest(),tmp_path,live=True,selection={'b'})
    assert a.state['turns'][1]['reason']=='turns_cap'


@pytest.mark.parametrize('available',[True,False])
def test_product_discovery_is_period_status_price_and_authorization_scoped(tmp_path,monkeypatch,available):
    client=FixtureClient('empty')
    client.add(1,41001,'Complete','10','2025-01-10')
    client.add(2,41003,'Complete','90','2025-01-10')  # unauthorized
    client.add(3,41002,'Complete','20','2024-01-10') # outside period
    client.add(4,41002,'Cancelled','20','2025-01-10')
    client.add(5,41002,'Complete',None,'2025-01-10')
    if available: client.add(6,41002,'Complete','20','2025-01-10')
    a=account(tmp_path); a.begin_turn('discovery')
    adapter=BigQueryTurn(SETTINGS,client=runner.GuardedClient(client,a))
    catalog=adapter.load_catalog(); scope=resolve_demo_scope('manager_apparel',catalog)
    expected=case('live-products')['turns'][0]['semantic_contract']['queries'][0]
    if available:
        bindings,precondition=runner.discover_products(adapter,catalog,scope,expected)
        assert bindings=={'product_1':41001,'product_2':41002}
        assert precondition['status']=='met' and precondition['qualifying_products']==2
    else:
        with pytest.raises(runner.DataPreconditionError) as exc:runner.discover_products(adapter,catalog,scope,expected)
        assert exc.value.precondition['status']=='unmet'
    assert a.state['used']['warehouse_executions']==2 and adapter.executions==2
    assert all(e['billed_bytes']==0 for e in a.state['events'])
    client.db.close()

from test_report_completeness import harness, request, ReportTurn, OWNER


def test_table_presentation_delivers_required_table_even_when_model_omits_it(harness):
    script,_,store,flow,_=harness
    script['Comparison layout']=InterpretationResult(status='ready',request=request(report=ReportSpec(
        presentation='table',requested_sections=('summary','recommendations'))))
    result=flow().run(OWNER,'Comparison layout')
    assert result.report_completeness=='complete'
    sections={s['kind']:s['status'] for s in result.report_composition['sections']}
    assert sections=={'summary':'supported','performance_table':'supported','recommendations':'supported','limitations':'supported'}
    assert 'performance_table' not in result.report_composition['spec']['requested_sections']
    assert json.loads(store.report(OWNER,result.report_id)['report_composition_json'])==result.report_composition


def test_explicit_summary_only_stays_without_actions_or_table(harness):
    script,_,_,flow,_=harness
    script['Only summary']=InterpretationResult(status='ready',request=request(report=ReportSpec(
        requested_sections=('summary','limitations'),presentation='briefing')))
    result=flow().run(OWNER,'Only summary')
    assert [s['kind'] for s in result.report_composition['sections']]==['summary','limitations']
    assert result.suggestions==()


def test_empty_table_is_missing_and_partial(harness):
    script,_,_,flow,_=harness
    ReportTurn.state='empty'
    script['Empty table']=InterpretationResult(status='ready',request=request(report=ReportSpec(
        presentation='table',requested_sections=('summary','limitations'))))
    result=flow().run(OWNER,'Empty table')
    assert result.report_completeness=='partial'
    assert 'performance_table' in result.report_composition['missing']


@pytest.mark.parametrize('kind,code,finish,availability',[
    ('length','truncated_model_output','length','text'),
    ('empty','empty_model_output','stop','empty'),
    ('whitespace','empty_model_output','stop','empty'),
    ('missing','backup_failure','stop','missing'),
    ('nontext','empty_model_output','stop','non_text'),
    ('json','invalid_model_json','unknown','text'),
    ('schema','invalid_report_contract','stop','text')])
def test_safe_unusable_provider_diagnostics(tmp_path,monkeypatch,kind,code,finish,availability):
    from opsfleet_assistant.model_gateway import GeminiGateway, ModelFailure
    from opsfleet_assistant.telemetry import TurnTrace
    content={'length':'private-marker','empty':'','whitespace':'  ','missing':None,
        'nontext':[],'json':'private-marker','schema':'{"private-marker":true}'}[kind]
    message={} if kind=='missing' else {'content':content}
    payload={'model':SETTINGS.openrouter_model,'usage':{'total_tokens':17,'cost':0.1},
        'choices':[{'finish_reason':'length' if kind=='length' else 'private-marker' if kind=='json' else 'stop','message':message}]}
    http_transport(monkeypatch,[payload])
    a=account(tmp_path); a.begin_turn('diagnostic')
    gateway=GeminiGateway(SETTINGS,provider_mode='openrouter_only',observer=runner.ProviderObserver(a))
    gateway.budget.telemetry=TurnTrace('c','t',tmp_path/'telemetry.jsonl')
    with pytest.raises(ModelFailure,match=code):gateway.report((),report_requested=True)
    event=a.state['events'][0]
    assert event['outcome']=='failed' and event['usage']=={'total_tokens':17,'cost':0.1} and event['cost']==0.1
    assert event['response_metadata']['finish_category']==finish
    assert event['response_metadata']['content_availability']==availability
    assert event['diagnostics']['failure_code'] in {'truncated_output','empty_output','unusable_response','invalid_json','invalid_report_contract'}
    assert runner.outcome_status([],{},[event],None)==('FAIL' if kind in {'json','schema'} else 'PROVIDER_FAILURE')
    assert 'private-marker' not in json.dumps(a.state)+(tmp_path/'telemetry.jsonl').read_text()


def test_zero_baseline_report_fallback_does_not_hide_provider_failure(tmp_path,monkeypatch):
    c=case('novel-zero-save'); c['turns']=c['turns'][:1]
    def q(role):return QuerySpec(operation='aggregate',metric='revenue',temporal_role=role,revenue_statuses=('Complete',))
    def quarter(n):return TemporalExpression(kind='quarter',quarter=n,year=YearExpression(kind='absolute',value=2025))
    req=RequestSpec(mode='temporal_compare',analysis=quarter(3),comparison=quarter(2),queries=(q('analysis'),q('comparison')))
    http_transport(monkeypatch,[InterpretationResult(status='ready',request=req).model_dump_json(),
        {'model':SETTINGS.openrouter_model,'choices':[{'finish_reason':'length','message':{'content':'private-marker'}}]}])
    a=account(tmp_path)
    row,=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    assert row['status']=='PROVIDER_FAILURE'
    observed=row['attempts'][0]['observed']
    assert observed['status']=='ok' and observed['arithmetic_consistent']
    assert all(r['passed'] for r in row['attempts'][0]['assertions'])
    assert a.state['events'][-1]['diagnostics']['failure_code']=='truncated_output'


def test_facts_only_report_stays_action_free_after_save_replay_and_followup(harness):
    from opsfleet_assistant.contracts import ActionSelection
    script,selections,store,flow,_=harness
    selections[0]=ReportDraft(recommendations=(ActionSelection(kind='review_performance',finding_index=0),))
    script['Facts only']=InterpretationResult(status='ready',request=request(report=ReportSpec(requested_sections=('summary','limitations'))))
    script['Explain']=InterpretationResult(status='ready',context_action='continue',request=RequestSpec(intent='explain'))
    first=flow().run(OWNER,'Facts only',conversation_id='facts',turn_id='original')
    replay=flow().run(OWNER,'Facts only',conversation_id='facts',turn_id='original')
    followup=flow().run(OWNER,'Explain',conversation_id='facts',turn_id='followup')
    assert replay.reused_report and first.text==replay.text
    for result in (first,replay,followup):
        assert result.status=='ok' and result.suggestions==()
        assert 'Recommended actions' not in result.text and 'Hypothesis (unverified)' not in result.text
    saved=json.loads(store.report(OWNER,first.report_id)['grounded_report_json'])
    assert saved['recommendations']==saved['hypotheses']==[]


def defaults_reply(c):
    if c['status']!='ready':
        return {'status':c['status'],'issues':['unsupported_capability' if c['status']=='unsupported' else 'unresolved_interpretation']}
    analysis={'kind':'year','year':{'kind':'absolute','value':2025}}
    if c['id'] in {'explicit_q4','next_quarter'}:
        analysis={'kind':'quarter','quarter':3 if c['id']=='explicit_q4' else 4,'year':{'kind':'absolute','value':2025}}
    query={'operation':c['operation'],'metric':c['metric']}
    if c['id']=='explicit_average':query.update(dimension='state',demographic_values=['Texas','California'],limit=10)
    req={'analysis':analysis,'queries':[query]}
    if c.get('report'):req.update(mode='report',report=ReportSpec().model_dump(mode='json'))
    if c.get('action'):
        req['action']={'kind':'reference','reference':'analysis','unit':'quarter','offset':1}
    return {'status':'ready','request':req}


@pytest.mark.parametrize('mode',['pass','wrong','dns','dry'])
def test_defaults_probe_is_bounded_and_detects_wrong_meaning(tmp_path,monkeypatch,mode):
    import socket
    import httpx
    from test_full_validation_driver import load_script,copied_workspace,forbidden
    from opsfleet_assistant.config import Settings
    probe=load_script('probe_interpretation_defaults');copied_workspace(tmp_path)
    monkeypatch.setattr(probe,'ROOT',tmp_path)
    monkeypatch.setattr(probe,'__file__',str(tmp_path/'scripts/probe_interpretation_defaults.py'))
    replies=[json.dumps(defaults_reply(c)) for c in probe.cases()]
    if mode=='wrong':
        bad=defaults_reply(probe.cases()[4]);bad['request']['action']={'kind':'year','year':{'kind':'absolute','value':2026}}
        replies[4]=json.dumps(bad)
    if mode=='dns':
        error=httpx.ConnectError('private-marker');error.__cause__=socket.gaierror(-2,'private-marker');replies=[error]
    calls=http_transport(monkeypatch,replies)
    monkeypatch.setattr(Settings,'load',forbidden if mode=='dry' else lambda:SETTINGS)
    monkeypatch.setattr('socket.getaddrinfo',forbidden)
    monkeypatch.setattr('google.cloud.bigquery.Client',forbidden)
    monkeypatch.setattr('sys.argv',['probe','--run-id','defaults-control']+([] if mode=='dry' else ['--live']))
    assert probe.main()==(1 if mode in {'wrong','dns'} else 0)
    value=json.loads((tmp_path/'docs/remediation/runs/defaults-control/results.json').read_text())
    assert len(calls)==(0 if mode=='dry' else 1 if mode=='dns' else 8)
    assert value['runtime_unchanged'] and value['warehouse_operations']==0
    if mode=='wrong':assert value['results'][4]['status']=='FAIL'
    if mode=='dns':assert value['stop']=='provider_dns_resolution'
    assert 'private-marker' not in json.dumps(value)


def test_prompt_contrasts_reach_gateway_without_overriding_output(monkeypatch):
    from test_full_validation_driver import load_script
    from opsfleet_assistant.model_gateway import GeminiGateway
    probe=load_script('probe_interpretation_defaults')
    for c in probe.cases():
        gateway=GeminiGateway(SETTINGS)
        def generate(prompt,*args):
            assert len(prompt)<12000
            assert 'Unqualified regional spend => clarify total versus average' in prompt
            assert 'Unspecified recommendations/actions => action null' in prompt
            assert 'Q4 2025 becomes Q1 2026' in prompt
            return defaults_reply(c)
        monkeypatch.setattr(gateway,'_generate',generate)
        value=gateway.interpret(c['message'],previous_plan=None,reference_date='2026-11-05',analytical_timezone='UTC')
        checks,_=probe.check_interpretation(value,c)
        assert all(checks.values()),checks


@pytest.mark.parametrize('live',[False,True])
def test_targeted_driver_uses_complete_chains_and_shared_smaller_caps(tmp_path,monkeypatch,capsys,live):
    from test_full_validation_driver import load_script,copied_workspace,forbidden
    from opsfleet_assistant.config import Settings
    driver=load_script('run_live_evaluation');copied_workspace(tmp_path)
    monkeypatch.setattr(driver,'ROOT',tmp_path)
    monkeypatch.setattr(driver,'__file__',str(tmp_path/'scripts/run_live_evaluation.py'))
    monkeypatch.setattr(Settings,'load',lambda:SETTINGS if live else forbidden())
    monkeypatch.setattr('socket.getaddrinfo',forbidden)
    selected=['ambiguity-spend','report-empty','novel-leap-negation','novel-role-layout','novel-narrow-save',
        'novel-unauthorized-recovery','live-products','journey','novel-zero-save']
    calls=[]
    if live:
        def simulate(a,m,p,**kwargs):
            assert len(m['scenarios'])==9 and sum(len(c['turns']) for c in m['scenarios'])==20
            assert a.state['caps']=={'turns':20,'provider_attempts':40,'warehouse_executions':8,'billed_bytes':1024**3,'active_seconds':900.0}
            calls.append(kwargs['selection'])
        monkeypatch.setattr(driver,'execute',simulate)
    argv=['driver','--run-id','affected-control','--turns','20','--provider-attempts','40','--warehouse-executions','8',
        '--billed-bytes',str(1024**3),'--active-seconds','900','--select',*selected]+(['--live'] if live else [])
    monkeypatch.setattr('sys.argv',argv)
    assert driver.main()==0
    output=tmp_path/'docs/remediation/runs/affected-control'
    plan=json.loads((output/'plan.json').read_text())
    assert plan['planned_turns']==20 and plan['planned_scenarios']==9
    if live:
        assert len(calls)==2 and set.union(*calls)==set(selected)
        lines=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
        monkeypatch.setattr('sys.argv',argv+['--resume',lines[0]['private_resume_directory']])
        assert driver.main()==0 and len(calls)==2
    else:
        result=json.loads((output/'results.json').read_text())
        assert result['counts']['turns']['NOT_RUN']==20 and result['used']['provider_attempts']==0


def test_all_null_aggregates_remain_null_and_conditional_counts_zero():
    client=FixtureClient('empty');client.add(1,41001,'Complete',None,'2025-01-01')
    row=client.query('SELECT SUM(sale_price) AS revenue, AVG(sale_price) AS average_item_price, COUNTIF(sale_price IS NOT NULL) AS item_count FROM order_items',job_config=bigquery.QueryJobConfig()).rows[0]
    assert row=={'revenue':None,'average_item_price':None,'item_count':0}
    client.db.close()


def test_unmet_live_product_precondition_is_explicit_without_model_dispatch(tmp_path,monkeypatch):
    c=case('live-products')
    monkeypatch.setattr(runner,'_client_from_adc',lambda s:FixtureClient('empty'))
    calls=http_transport(monkeypatch,[])
    a=account(tmp_path)
    row,=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    assert row['status']=='FAIL' and row['reason']=='data_precondition_unmet'
    assert row['attempts'][0]['observed']['data_precondition']['status']=='unmet'
    assert a.state['used']['warehouse_executions']==2 and not calls
