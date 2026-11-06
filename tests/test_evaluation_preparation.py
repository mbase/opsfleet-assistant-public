"""Preparation tests. Substituted transports are never live semantic evidence."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

from google.cloud import bigquery
import httpx
import pytest

from opsfleet_assistant import model_gateway
from opsfleet_assistant.bigquery_adapter import BigQueryTurn, ExecutionError, QueryLimits
from opsfleet_assistant.compiler import compile_plan
from opsfleet_assistant.config import ROOT, Settings
from opsfleet_assistant.contracts import (AnalysisPlan, InterpretationResult, RequestSpec, QuerySpec,
    TemporalExpression, YearExpression, Period, ReportDraft)
from opsfleet_assistant.evaluation_contracts import (Accounting, MAX_CAPS, Redactor, StopScheduling,
    assertions, canonical, digest, totals, verify_source)
from opsfleet_assistant.evaluation_fixtures import FixtureClient, FixtureJob
from opsfleet_assistant.evaluation_runner import (ProviderObserver, GuardedClient, acceptance,
    execute, run_workflow, validate_manifest, run_probe, safe_private)
from opsfleet_assistant.identity import resolve_demo_scope
from opsfleet_assistant.model_gateway import GeminiGateway, ModelBudget, ModelFailure

SETTINGS=Settings(project='fixture-project',location='US',model='unused-direct',gemini_api_key='secret-direct-test',
    openrouter_model='google/frozen-test',openrouter_api_key='secret-router-test')
IDENTITY={'run_id':'test','source_revision':'frozen','scenario_sha256':'fixed'}


def account(tmp_path,**caps):
    return Accounting(tmp_path/'checkpoint.json',identity=IDENTITY,caps={**MAX_CAPS,**caps})


def http_transport(monkeypatch,responses,requests=None):
    sent=requests if requests is not None else []
    class Client:
        def __init__(self,**kwargs): self.options=kwargs
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def post(self,url,*,headers,json):
            sent.append({'url':url,'headers':headers,'json':json,'options':self.options})
            value=responses.pop(0)
            if isinstance(value,Exception): raise value
            if isinstance(value,int): return SimpleNamespace(status_code=value)
            payload=value if isinstance(value,dict) else {'choices':[{'message':{'content':value}}],
                'model':'google/frozen-test','usage':{'total_tokens':19,'cost':0.001}}
            return SimpleNamespace(status_code=200,json=lambda:payload)
    monkeypatch.setattr(model_gateway.httpx,'Client',Client)
    monkeypatch.setattr(model_gateway.genai,'Client',lambda **kw: pytest.fail('DIRECT GEMINI CALLED'))
    return sent


def test_openrouter_only_real_transport(tmp_path,monkeypatch):
    a=account(tmp_path); a.begin_turn('t1')
    calls=http_transport(monkeypatch,[ReportDraft().model_dump_json()])
    gateway=GeminiGateway(SETTINGS,provider_mode='openrouter_only',observer=ProviderObserver(a))
    assert gateway.report((),report_requested=False)==ReportDraft()
    request=calls[0]
    assert request['url']=='https://openrouter.ai/api/v1/chat/completions'
    assert request['json']['model']=='google/frozen-test'
    assert request['json']['provider']=={'require_parameters':True,'zdr':True,'data_collection':'deny'}
    assert request['json']['response_format']['json_schema']['strict'] is True
    assert request['json']['max_tokens']==2000 and request['options']['follow_redirects'] is False
    assert 0<request['options']['timeout']<=30
    assert gateway.budget.requests==1 and gateway.budget.routes==['openrouter_gemini']
    event=a.state['events'][0]
    assert event['actual_model']=='google/frozen-test' and event['usage']['total_tokens']==19 and event['cost']==0.001
    with pytest.raises(ModelFailure,match='direct_route_forbidden'): gateway._direct('',{},1)


@pytest.mark.parametrize('response,code',[ (429,'backup_transient'),(httpx.ReadTimeout('secret-provider-body'),'backup_transient'),
    ('not JSON','invalid_model_json'),(403,'backup_failure')])
def test_openrouter_transport_failures(tmp_path,monkeypatch,response,code):
    a=account(tmp_path); a.begin_turn('fault')
    calls=http_transport(monkeypatch,[response])
    gateway=GeminiGateway(SETTINGS,provider_mode='openrouter_only',observer=ProviderObserver(a))
    with pytest.raises(ModelFailure,match=code): gateway.report((),report_requested=False)
    assert len(calls)==1 and a.state['used']['provider_attempts']==1
    assert gateway.budget.routes==['openrouter_gemini']
    assert 'secret-provider-body' not in json.dumps(a.state)
    assert a.state['events'][0]['outcome']=='failed'


def test_missing_openrouter_fails_closed_without_http_or_direct(monkeypatch):
    http_transport(monkeypatch,[])
    gateway=GeminiGateway(replace(SETTINGS,openrouter_api_key=None),provider_mode='openrouter_only')
    with pytest.raises(ModelFailure,match='backup_unconfigured'): gateway.report((),report_requested=True)
    assert gateway.budget.requests==0


def test_production_default_route_is_preserved(monkeypatch):
    gateway=GeminiGateway(SETTINGS)
    sent=[]
    def direct(*args): sent.append('direct'); raise ModelFailure('direct_transient')
    def backup(*args): sent.append('router'); return '{}'
    monkeypatch.setattr(gateway,'_direct',direct);monkeypatch.setattr(gateway,'_openrouter',backup)
    assert gateway._generate('',{},1)=={} and sent==['direct','router']


def test_predispatch_provider_reservation_and_six_request_cap(tmp_path,monkeypatch):
    a=account(tmp_path,provider_attempts=2); a.begin_turn('t1')
    calls=http_transport(monkeypatch,['{}','{}'])
    gateway=GeminiGateway(SETTINGS,provider_mode='openrouter_only',observer=ProviderObserver(a))
    gateway._generate('',{},1); gateway._generate('',{},1)
    assert json.loads(a.path.read_text())['used']['provider_attempts']==2
    with pytest.raises(StopScheduling):gateway._generate('',{},1)
    assert len(calls)==2 and a.state['used']['provider_attempts']==2
    other=GeminiGateway(SETTINGS,provider_mode='openrouter_only',budget=ModelBudget(max_requests=1))
    monkeypatch.setattr(other,'_openrouter',lambda *args: (other.budget.charge('openrouter_gemini'),'{}')[1])
    other._generate('',{},1)
    with pytest.raises(ModelFailure,match='model_request_limit'): other._generate('',{},1)


def test_checkpoint_no_reset_and_crash_never_replays(tmp_path):
    a=account(tmp_path,provider_attempts=2); a.begin_turn('one')
    ticket=a.reserve('provider',route='openrouter_gemini',requested_model='fixed')
    assert json.loads(a.path.read_text())['events'][0]['outcome']=='dispatched'
    b=Accounting(a.path,identity=IDENTITY,caps=a.state['caps'])
    assert b.state['used']['provider_attempts']==1 and b.state['used']['turns']==1
    assert b.state['stop']=='interrupted_dispatch_unknown_billing'
    assert b.state['used']['active_seconds']>=120
    with pytest.raises(StopScheduling): b.begin_turn('two')
    with pytest.raises(ValueError): Accounting(a.path,identity={**IDENTITY,'run_id':'replacement'})
    with pytest.raises(ValueError): Accounting(a.path,identity=IDENTITY,caps=MAX_CAPS)


def test_normal_resume_retains_completed_attempts_caps_and_elapsed(tmp_path):
    now=[0.0]
    a=Accounting(tmp_path/'checkpoint.json',identity=IDENTITY,clock=lambda:now[0])
    a.begin_turn('one'); t=a.reserve('provider'); a.finish(t,outcome='failed');now[0]=3;a.end_turn()
    b=Accounting(a.path,identity=IDENTITY,clock=lambda:1000.0)
    assert b.state['used']['turns']==1 and b.state['used']['provider_attempts']==1
    assert b.state['used']['active_seconds']==3
    assert b.state['events'][0]['outcome']=='failed' and b.state['stop'] is None


@pytest.mark.parametrize('caps',[{'turns':121},{'billed_bytes':5368709121},{'active_seconds':float('nan')},
                                  {'provider_attempts':True},{'turns':0}])
def test_caps_cannot_expand_or_be_invalid(tmp_path,caps):
    with pytest.raises(ValueError):account(tmp_path,**caps)


def test_warehouse_reserved_byte_cap_catalog_coverage_and_unknown(tmp_path):
    a=account(tmp_path,warehouse_executions=2,billed_bytes=15);a.begin_turn('t')
    class Client:
        def query(self,sql,*,job_config,**kwargs):
            assert json.loads(a.path.read_text())['used']['warehouse_executions']>=1
            job=FixtureJob();job.total_bytes_billed=10 if a.state['used']['warehouse_executions']==1 else None
            assert job_config.maximum_bytes_billed==(15 if a.state['used']['warehouse_executions']==1 else 5)
            return job
    client=GuardedClient(Client(),a)
    def config(purpose): return bigquery.QueryJobConfig(dry_run=False,maximum_bytes_billed=100,labels={'purpose':purpose})
    first=client.query('catalog',job_config=config('catalog')); first.result()
    assert a.state['used']['billed_bytes']==10
    second=client.query('coverage',job_config=config('coverage'));second.result()
    assert a.state['used']['warehouse_executions']==2 and a.state['stop']=='unknown_warehouse_billing'
    assert a.state['events'][1]['billed_bytes'] is None
    with pytest.raises(StopScheduling):client.query('analysis',job_config=config('analysis'))
    first.result();assert a.state['used']['billed_bytes']==10  # no double billing


def test_failed_submission_unknown_billing_and_no_hidden_retry(tmp_path):
    a=account(tmp_path); a.begin_turn('t')
    class Broken:
        def query(self,*args,**kwargs): raise TimeoutError('private provider text')
    with pytest.raises(TimeoutError):GuardedClient(Broken(),a).query('x',job_config=bigquery.QueryJobConfig(maximum_bytes_billed=10))
    assert a.state['used']['warehouse_executions']==1 and a.state['stop']=='unknown_warehouse_billing'
    assert 'private provider text' not in json.dumps(a.state)


def test_active_wall_cap_stops_before_dispatch(tmp_path):
    now=[0.0]; a=Accounting(tmp_path/'c.json',identity=IDENTITY,caps={**MAX_CAPS,'active_seconds':2},clock=lambda:now[0])
    a.begin_turn('t');now[0]=2
    with pytest.raises(StopScheduling):a.reserve('provider')
    assert a.state['used']['provider_attempts']==0


def test_redaction_credentials_tokens_prohibited_pii_and_errors():
    token='a'*43
    payload={'prompt':'Contact Synthetic Person Sentinel at a@example.com +12345678901 123-45-6789 123 Synthetic Street',
             'text':f'confirm {token}; Bearer verysecret; sk-secret-key', 'api_key':'value',
             'nested':[{'content':'actual-secret-value','raw_error':'provider exception with private URL'}]}
    value=json.dumps(Redactor(['actual-secret-value'])(payload))
    for banned in (token,'a@example.com','+12345678901','123-45-6789','Synthetic Person Sentinel','123 Synthetic Street',
                   'verysecret','sk-secret-key','actual-secret-value','provider exception'):
        assert banned not in value


def test_independent_oracle_detects_wrong_period_arithmetic_and_precision():
    expected=[{'path':'findings','op':'finding','value':{'operation':'difference','metric':'revenue','value':'19.875'}},
              {'path':'period','op':'eq','value':{'start':'2025-07-01','end':'2025-10-01'}},
              {'path':'amount','op':'decimal','value':'123.456789012345678901'}]
    observed={'period':{'start':'2025-07-01','end':'2025-10-01'},'amount':'123.456789012345678901',
        'findings':[{'selection':{'operation':'difference','current':{'metric':'revenue'}},'value':'19.875'}]}
    assert all(x['passed'] for x in assertions(expected,observed))
    observed['findings'][0]['value']='0'; observed['period']['start']='2025-10-01'
    assert [x['passed'] for x in assertions(expected,observed)]==[False,False,True]


def test_manifest_frozen_coverage_and_dependencies():
    m=validate_manifest(json.loads((ROOT/'docs/remediation/SCENARIO_MANIFEST.json').read_text()))
    assert len(m['scenarios'])==72 and sum(len(c['turns']) for c in m['scenarios'])==99
    assert len({c['novel_combination'] for c in m['scenarios'] if c['novel_combination']})==12
    required={'absolute_calendar','relative_time','temporal_roles','ambiguity','multi_turn','restart_context',
        'scope_identity','pii_malicious','numerical_findings','reports','destructive_operations','resilience',
        'golden_methodology','output_diagnostics','later_review_regressions','novel_combinations'}
    # Temporal roles covered explicitly in report/novel role assertions, label too.
    assert required <= set(f for c in m['scenarios'] for f in c['families'])
    assert sum(len(c['turns']) for c in m['scenarios'])<=MAX_CAPS['turns']


def test_dry_plan_has_no_factories_or_settings_access(tmp_path,monkeypatch):
    import opsfleet_assistant.evaluation_runner as runner
    monkeypatch.setattr(runner,'run_workflow',lambda *a,**k:pytest.fail('workflow dispatch'))
    monkeypatch.setattr(runner,'run_probe',lambda *a,**k:pytest.fail('subprocess dispatch'))
    m=validate_manifest(json.loads((ROOT/'docs/remediation/SCENARIO_MANIFEST.json').read_text()))
    a=account(tmp_path);rows=execute(a,m,tmp_path,live=False)
    assert len(rows)==99 and all(r['status']=='NOT_RUN' and r['attempts']==[] for r in rows)
    assert a.state['used']['turns']==0 and a.state['events']==[]


def test_dependency_failure_continues_independent_cases_and_totals(tmp_path,monkeypatch):
    import opsfleet_assistant.evaluation_runner as runner
    calls=[]
    def dispatch(c,t,*args): calls.append(t['id']);return {'status':'wrong' if t['id']=='a1' else 'ok'}
    monkeypatch.setattr(runner,'run_workflow',dispatch)
    turn=lambda id,deps: {'id':id,'depends_on':deps,'assertions':[{'path':'status','op':'eq','value':'ok'}]}
    m={'scenarios':[{'id':'a','mode':'live_fixture','turns':[turn('a1',[]),turn('a2',['a1'])]},
                    {'id':'b','mode':'live_fixture','turns':[turn('b1',[])]}]}
    a=account(tmp_path); rows=execute(a,m,tmp_path,live=True,settings=SETTINGS)
    assert calls==['a1','b1'] and [r['status'] for r in rows]==['FAIL','NOT_RUN','PASS']
    counts=totals(rows)
    assert counts['turns']['FAIL']==1 and counts['turns']['NOT_RUN']==1 and counts['attempted_turns']==2
    assert counts['scenarios']['FAIL']==1 and counts['scenarios']['PASS']==1
    execute(a,m,tmp_path,live=True,settings=SETTINGS)
    assert calls==['a1','b1']  # never replay failed attempts


def test_smaller_batch_resumes_same_counters_without_replaying(tmp_path,monkeypatch):
    import opsfleet_assistant.evaluation_runner as runner
    calls=[]
    monkeypatch.setattr(runner,'run_workflow',lambda c,t,*args: (calls.append(t['id']),{'status':'ok'})[1])
    m={'scenarios':[{'id':x,'mode':'live_fixture','turns':[{'id':x+'1','depends_on':[],
        'assertions':[{'path':'status','op':'eq','value':'ok'}]}]} for x in ('a','b')]}
    a=account(tmp_path,turns=1)
    execute(a,m,tmp_path,live=True,settings=SETTINGS,selection={'a'})
    execute(a,m,tmp_path,live=True,settings=SETTINGS,selection={'b'})
    assert calls==['a1'] and a.state['used']['turns']==1
    assert a.state['turns'][1]['status']=='NOT_RUN' and a.state['turns'][1]['reason']=='turns_cap'


def test_source_hash_path_order_and_acceptance_tied_to_written_artifact(tmp_path):
    (tmp_path/'a').write_text('a');(tmp_path/'b').write_text('b')
    files={p:digest(tmp_path/p) for p in ('a','b')}
    manifest={'revision':canonical(files),'files':files}
    assert verify_source(tmp_path,manifest)==canonical(files)
    (tmp_path/'review.md').write_text('Independent review: accepted with limits')
    r={'verdict':'ACCEPTED_WITH_VERIFICATION_LIMITS','blocking_findings':[],'source_revision':'source',
       'scenario_sha256':'scenario','reviewer_identity':'independent reviewer label','review_artifact':'review.md',
       'review_sha256':digest(tmp_path/'review.md')}
    path=tmp_path/'review.json';path.write_text(json.dumps(r))
    assert acceptance(tmp_path,path,'source','scenario')==r
    with pytest.raises(ValueError):acceptance(tmp_path,path,'changed','scenario')
    r['reviewer_identity']='01a1aade-98c8-71a3-8fd4-40200ad6bcb7';path.write_text(json.dumps(r))
    with pytest.raises(ValueError):acceptance(tmp_path,path,'source','scenario')
    (tmp_path/'a').write_text('changed')
    with pytest.raises(ValueError):verify_source(tmp_path,manifest)


@pytest.mark.parametrize('profile,expected',[('standard','30'),('increase','200'),('decline','100'),('equal','125.25'),
                                           ('zero','200'),('precision','123.456789012345678901')])
def test_sql_fixture_executes_real_scoped_compilation_and_release(profile,expected):
    client=FixtureClient(profile)
    adapter=BigQueryTurn(SETTINGS,client=client)
    catalog=adapter.load_catalog();scope=resolve_demo_scope('manager_apparel',catalog)
    query=compile_plan(AnalysisPlan(operation='aggregate',metric='revenue',
        period=Period(start='2025-07-01',end='2025-10-01'),revenue_statuses=('Complete',)),scope,catalog)
    evidence=adapter.execute_compiled(query)
    assert Decimal(evidence.rows[0]['revenue'])==Decimal(expected)
    assert evidence.execution_scope.brands==('Calvin Klein','Carhartt')
    assert adapter.executions==3 and evidence.coverage.period_rows>0
    client.db.close()


def test_sql_fixture_mixed_order_narrowing_and_exclusive_end():
    client=FixtureClient();adapter=BigQueryTurn(SETTINGS,client=client)
    catalog=adapter.load_catalog();scope=resolve_demo_scope('manager_apparel',catalog)
    def run(brands,end):return adapter.execute_compiled(compile_plan(AnalysisPlan(operation='aggregate',metric='revenue',
        period=Period(start='2025-01-01',end=end),requested_brands=brands,revenue_statuses=('Complete',)),scope,catalog))
    assert Decimal(run(None,'2025-02-01').rows[0]['revenue'])==30
    assert Decimal(run(('Calvin Klein',),'2025-02-01').rows[0]['revenue'])==10
    assert run(None,'2025-01-10').coverage.period_rows==0  # exact exclusive end
    client.db.close()


def test_real_gateway_fixture_workflow_and_exact_replay_offline(tmp_path,monkeypatch):
    from opsfleet_assistant.contracts import ActionSelection
    request=RequestSpec(mode='report',analysis=TemporalExpression(kind='quarter',quarter=3,
        year=YearExpression(kind='absolute',value=2025)),queries=(QuerySpec(operation='aggregate',metric='revenue',revenue_statuses=('Complete',)),))
    responses=[InterpretationResult(status='ready',request=request).model_dump_json(),
               ReportDraft(recommendations=(ActionSelection(kind='review_performance',finding_index=0),)).model_dump_json()]
    calls=http_transport(monkeypatch,responses)
    a=account(tmp_path);a.begin_turn('first')
    c={'id':'workflow-test','owner':'manager_apparel','mode':'live_fixture','fixture':'standard',
        'turns':[{'id':'first','message':'Make Q3 2025 Complete-only revenue report'}]}
    t={**c['turns'][0],'analytical_date':'2026-11-05','lifecycle_clock':'2026-11-05T12:00:00+00:00'}
    original=run_workflow(c,t,tmp_path,a,SETTINGS,{})
    assert original['status']=='ok' and Decimal(original['presentation']['evidence'][0]['rows'][0]['revenue'])==30
    assert original['model_requests']==2 and original['query_executions']==3 and original['arithmetic_consistent']
    a.end_turn();a.begin_turn('retry')
    replay=run_workflow(c,{**t,'id':'retry','replay_of':'first'},tmp_path,a,SETTINGS,{})
    assert replay['status']=='ok' and replay['reused_report'] and replay['model_requests']==0 and replay['query_executions']==1
    assert replay['text']==original['text'] and len(calls)==2
    assert a.state['used']['provider_attempts']==2 and a.state['used']['warehouse_executions']==0
    a.end_turn()


def test_controlled_subprocess_isolated_and_actual_exit_observed(tmp_path):
    a=account(tmp_path);a.begin_turn('probe')
    result=run_probe({'id':'probe','selectors':['tests/test_step5.py::test_literal_matching_is_casefolded_substring_not_sql_wildcard']},{},tmp_path,a)
    assert result['exit_code']==0 and result['real_model_attempts']==0
    assert (tmp_path/'probe.pytest.log').is_file()
    a.end_turn()


def test_live_warehouse_guard_path_with_substituted_transport_and_independent_reference(tmp_path,monkeypatch):
    """Offline only: real adapter/reference SQL; warehouse transport is SQLite."""
    import opsfleet_assistant.evaluation_runner as runner
    request=RequestSpec(analysis=TemporalExpression(kind='month',month=1,year=YearExpression(kind='absolute',value=2025)),
        queries=(QuerySpec(operation='aggregate',metric='revenue',revenue_statuses=('Complete',)),))
    http_transport(monkeypatch,[InterpretationResult(status='ready',request=request).model_dump_json(),ReportDraft().model_dump_json()])
    monkeypatch.setattr(runner,'_client_from_adc',lambda s:FixtureClient())
    c={'id':'reference-test','owner':'manager_apparel','mode':'live_bigquery'}
    t={'id':'reference-turn','message':'January 2025 Complete-only revenue','analytical_date':'2026-11-05',
       'lifecycle_clock':'2026-11-05T12:00:00+00:00','reference_scalar':True,
       'reference_expected':{'operation':'aggregate','metric':'revenue','columns':['revenue'],'products':None,
          'status_set':['Complete'],'brands':['Calvin Klein','Carhartt'],'period':{'start':'2025-01-01','end':'2025-02-01'}}}
    a=account(tmp_path);a.begin_turn(t['id'])
    observed=run_workflow(c,t,tmp_path,a,SETTINGS,{})
    assert observed['independent_reference']['matches']
    assert Decimal(observed['independent_reference']['value'])==30
    assert observed['query_executions']==3 and observed['total_query_executions_including_reference']==4
    jobs=[e for e in a.state['events'] if e['kind']=='warehouse']
    assert len(jobs)==4 and [e['purpose'] for e in jobs]==['opsfleet_catalog','opsfleet_analysis','opsfleet_coverage','opsfleet_reference']
    assert a.state['used']['warehouse_executions']==4 and a.state['used']['billed_bytes']==0
    a.end_turn()


def test_report_contract_failure_remains_provider_failure_even_with_checked_fallback(tmp_path,monkeypatch):
    request=RequestSpec(analysis=TemporalExpression(kind='month',month=1,year=YearExpression(kind='absolute',value=2025)),
        queries=(QuerySpec(operation='aggregate',metric='revenue',revenue_statuses=('Complete',)),))
    http_transport(monkeypatch,[InterpretationResult(status='ready',request=request).model_dump_json(),'malformed-report'])
    a=account(tmp_path)
    t={'id':'fall-t1','message':'January revenue 2025 Complete only','analytical_date':'2026-11-05',
       'lifecycle_clock':'2026-11-05T12:00:00+00:00','depends_on':[],'assertions':[{'path':'status','op':'eq','value':'ok'}]}
    c={'id':'fall','owner':'manager_apparel','mode':'live_fixture','turns':[t]}
    rows=execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    assert rows[0]['status']=='PROVIDER_FAILURE'
    assert rows[0]['attempts'][0]['observed']['status']=='ok'  # useful fallback retained
    assert a.state['used']['provider_attempts']==2
    assert a.state['events'][-1]['outcome']=='failed'


def test_reported_model_mismatch_is_visible_and_stops_scheduling(tmp_path,monkeypatch):
    a=account(tmp_path);a.begin_turn('t')
    http_transport(monkeypatch,[{'model':'other/unauthorized','usage':{},'choices':[{'message':{'content':'{}'}}]}])
    g=GeminiGateway(SETTINGS,provider_mode='openrouter_only',observer=ProviderObserver(a))
    with pytest.raises(ModelFailure):g._generate('',{},1)
    assert a.state['events'][0]['actual_model']=='other/unauthorized'
    assert a.state['stop']=='provider_reported_model_mismatch'
    with pytest.raises(StopScheduling):a.reserve('provider')


def test_scenario_selectors_are_real_local_test_nodes():
    import ast
    m=json.loads((ROOT/'docs/remediation/SCENARIO_MANIFEST.json').read_text())
    for c in m['scenarios']:
        for selector in c.get('selectors',[]):
            path,*node=selector.split('::')
            assert path.startswith('tests/') and (ROOT/path).is_file()
            if node:
                names={n.name for n in ast.parse((ROOT/path).read_text()).body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
                assert node[0] in names


def test_private_state_rejects_real_workspace_and_unmarked_directory(tmp_path):
    with pytest.raises(ValueError):safe_private(ROOT/'runtime')
    with pytest.raises(ValueError):safe_private(tmp_path)
    (tmp_path/'evaluation-private-marker').write_text('synthetic')
    assert safe_private(tmp_path)==tmp_path.resolve()


def test_cli_dry_end_to_end_and_live_gate_without_network(tmp_path,monkeypatch,capsys):
    import opsfleet_assistant.evaluation_runner as runner
    manifest=ROOT/'docs/remediation/SCENARIO_MANIFEST.json'
    docs=tmp_path/'docs/remediation';docs.mkdir(parents=True)
    target=docs/'SCENARIO_MANIFEST.json';target.write_bytes(manifest.read_bytes())
    files={'docs/remediation/SCENARIO_MANIFEST.json':digest(target)}
    source=docs/'EVALUATION_SOURCE_MANIFEST.json';source.write_text(json.dumps({'files':files,'revision':canonical(files)}))
    monkeypatch.setattr(runner,'ROOT',tmp_path);monkeypatch.setattr(runner,'MANIFEST',target);monkeypatch.setattr(runner,'SOURCE',source)
    monkeypatch.setattr(runner.Settings,'load',lambda:pytest.fail('dry loaded provider secrets'))
    assert runner.main(['--run-id','offline-cli-test'])==0
    info=json.loads(capsys.readouterr().out)
    results=json.loads((docs/'runs/offline-cli-test/results.json').read_text())
    assert results['kind']=='dry_plan_no_execution' and results['counts']['turns']['NOT_RUN']==99
    assert results['used']['provider_attempts']==0 and results['used']['turns']==0
    assert (docs/'runs/offline-cli-test/results-batch-001.json').is_file()
    assert runner.main(['--run-id','offline-cli-test','--resume',info['private_resume_directory']])==0
    assert (docs/'runs/offline-cli-test/results-batch-002.json').is_file()
    with pytest.raises(SystemExit) as exc:runner.main(['--run-id','forbidden-live','--live'])
    assert exc.value.code==2 and not (docs/'runs/forbidden-live').exists()


def test_unsettled_dispatch_at_end_of_failed_turn_stops_independent_work(tmp_path):
    a=account(tmp_path);a.begin_turn('broken')
    a.reserve('warehouse',cap=10,purpose='catalog')
    a.end_turn()  # unexpected SDK/harness exception before settlement
    assert a.state['stop']=='unsettled_dispatch_unknown_outcome'
    with pytest.raises(StopScheduling):a.begin_turn('independent')
    assert a.state['used']['warehouse_executions']==1
