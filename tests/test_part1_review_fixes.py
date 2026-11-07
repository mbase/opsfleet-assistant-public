"""Offline regression cases for R1–R5; no provider transport is used."""
from dataclasses import asdict, replace
from datetime import date
from io import StringIO
import json
from types import SimpleNamespace

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
from google.api_core import exceptions as ge
import pytest
from rich.console import Console

from opsfleet_assistant.bigquery_adapter import BigQueryTurn, ExecutionError
from opsfleet_assistant.compiler import compile_plan, semantic_fingerprint
from opsfleet_assistant.contracts import AnalysisPlan, Dimension, InterpretationResult, RequestSpec, QuerySpec, TemporalExpression, YearExpression, ReportDraft
from opsfleet_assistant.identity import resolve_demo_scope
from opsfleet_assistant.presentation import render_result
from opsfleet_assistant.telemetry import TurnTrace, agent_metrics
from opsfleet_assistant.workflow import TurnResult
from test_step3 import turn, plan, base_stages, Job
from test_step4 import SETTINGS, FakeGateway
from test_step6 import workflow
from test_recovery_1b import SchemaServer, approved_migration, run_workflow


@pytest.mark.parametrize('page_seconds,expected_calls,success', [(5,2,True),(20,2,False),(30,1,False)])
def test_sdk_lazy_pages_are_clamped_before_dispatch(page_seconds, expected_calls, success):
    client = bigquery.Client(project='offline-test', credentials=AnonymousCredentials())
    clock = [0.0]
    calls = []
    def request(**kwargs):
        remaining = 30 - clock[0]
        assert 0 < kwargs['timeout'] <= remaining  # SDK tries to pass 120 here.
        calls.append(kwargs['timeout'])
        clock[0] += page_seconds
        return {'rows':[{'f':[{'v':'1'}]}], 'totalRows':'2',
                **({'pageToken':'next'} if len(calls) == 1 else {})}
    client._connection.api_request = request
    job = Job((), (bigquery.SchemaField('n','INT64'),), job_id='page_job')
    job.result = lambda **kw: client._list_rows_from_query_results('page_job','US','offline-test',job.schema,
                                                                retry=None,timeout=kw['timeout'],max_results=2)
    client.query = lambda *a, **kw: SimpleNamespace(total_bytes_processed=10) if kw['job_config'].dry_run else job
    runner = BigQueryTurn(SETTINGS, client=client, clock=lambda:clock[0])
    if success:
        assert len(runner._run('fixture', (), max_rows=2, purpose='catalog').rows) == 2
        assert calls == [30,25]
    else:
        with pytest.raises(ExecutionError, match='warehouse_timeout') as caught:
            runner._run('fixture', (), max_rows=2, purpose='catalog')
        assert caught.value.submission_state == 'submitted' and caught.value.job_id == 'page_job'
        assert not job.cancelled  # Expired allowance cannot dispatch cancellation.
    assert len(calls) == expected_calls and client._connection.api_request is request


@pytest.mark.parametrize('which,expiry', [(0,31),(2,121)])
def test_iteration_cannot_complete_after_job_or_turn_deadline(which, expiry):
    runner, client = turn(base_stages())
    clock = [runner._started]
    runner._clock = lambda:clock[0]
    job = client.stages[which][1]
    class LateRows:
        def __iter__(self):
            clock[0] = runner._started + expiry
            return iter(job.rows)
    job.result = lambda **kw: LateRows()
    with pytest.raises(ExecutionError, match='warehouse_timeout'):
        runner.run(plan(), 'manager_apparel')
    assert runner.retries == 0 and not job.cancelled


def test_cancellation_uses_only_remaining_job_allowance():
    runner, client = turn(base_stages())
    clock = [runner._started]
    runner._clock = lambda:clock[0]
    job = client.stages[0][1]
    def result(**kw):
        clock[0] += 29.9
        raise TimeoutError('controlled')
    def cancel(**kw):
        assert 0 < kw['timeout'] <= 0.101
    job.result, job.cancel = result, cancel
    with pytest.raises(ExecutionError, match='timeout_cancellation_requested'):
        runner.run(plan(), 'manager_apparel')


def test_evidence_validation_crossing_deadline_cannot_release_or_count_success(tmp_path, monkeypatch):
    from opsfleet_assistant import bigquery_adapter as module
    runner, client = turn(base_stages())
    runner.telemetry = TurnTrace('c','t',tmp_path/'events')
    original = module.release_evidence
    def late(*a, **kw):
        evidence = original(*a, **kw)
        runner._clock = lambda: runner._started + 121
        return evidence
    monkeypatch.setattr(module, 'release_evidence', late)
    with pytest.raises(ExecutionError, match='warehouse_timeout') as caught:
        runner.run(plan(), 'manager_apparel')
    assert caught.value.analysis_job_id == 'analysis_job'
    assert not agent_metrics(tmp_path/'events')['counts'].get('recovery.outcome.success',0)


def test_semantics_bind_grain_and_role_and_reject_changed_grouping(approved_migration):
    runner, _ = turn(base_stages())
    catalog = runner.load_catalog()
    scope = resolve_demo_scope('manager_apparel',catalog)
    state = AnalysisPlan(operation='demographic_comparison',metric='average_customer_spend',
                         period=plan().period,dimension='state')
    country = state.model_copy(update={'dimension':Dimension.COUNTRY})
    first = compile_plan(state,scope,catalog)
    changed = compile_plan(country,scope,catalog,binding=approved_migration)
    comparison = compile_plan(state,scope,catalog,temporal_role='comparison')
    assert semantic_fingerprint(first) != semantic_fingerprint(changed)
    assert semantic_fingerprint(first) != semantic_fingerprint(comparison)
    runner._schema_inspection_started, runner._inspected_binding = True, approved_migration
    with pytest.raises(ExecutionError,match='recovery_scope_changed'):
        runner.correct_schema_binding(first,country,'manager_apparel')
    assert semantic_fingerprint(first) == semantic_fingerprint(replace(first,_seal='different'))


def test_diagnostic_and_analysis_provenance_reach_json_and_terminal(tmp_path):
    server = SchemaServer(price='sale_price')
    server.fail_coverage = True
    result, _ = run_workflow(tmp_path,server)
    structured = json.loads(json.dumps(asdict(result)))
    assert structured['failure_stage'] == 'dry_run' and structured['failure_purpose'] == 'coverage'
    assert structured['failure_submission_state'] == 'not_submitted'
    assert structured['failure_job_id'] is None and structured['failure_billed_bytes'] is None
    assert structured['analysis_submission_state'] == 'submitted'
    assert structured['analysis_job_id'] == 'fixture_job' and structured['analysis_billed_bytes'] == 10
    output = StringIO()
    render_result(Console(file=output,color_system=None,width=200),result,verbose=True)
    text = output.getvalue()
    assert 'fixture_job' in text and 'dry_run' in text and 'not_submitted' in text
    assert 'Automatic replay stopped' in text and 'billed bytes unknown' in text


@pytest.mark.parametrize('state', ['submitted','unknown'])
def test_timeout_guidance_is_submission_aware_even_for_budget_category(state):
    result = TurnResult('error','Controlled timeout','c','t',error_category='budget',
                        failure_stage='execute',failure_submission_state=state,
                        failure_job_id='important_job' if state == 'submitted' else None,
                        failure_billed_bytes=None)
    output = StringIO()
    render_result(Console(file=output,color_system=None,width=200),result,verbose=True)
    text = output.getvalue()
    assert 'check warehouse job status' in text and 'submission '+state in text
    assert 'billed bytes unknown' in text and 'Narrow the request' not in text
    if state == 'submitted': assert 'important_job' in text


def test_two_plan_migration_reuses_binding_and_counts_one_recovered_turn(tmp_path, approved_migration):
    class Gateway(FakeGateway):
        def interpret(self,*a,**kw):
            self.budget.charge('injected_model')
            return InterpretationResult(status='ready',request=RequestSpec(mode='report',
                analysis=TemporalExpression(kind='year',year=YearExpression(kind='absolute',value=2025)),
                comparison=TemporalExpression(kind='year',year=YearExpression(kind='absolute',value=2024)),
                queries=(QuerySpec(operation='aggregate',metric='revenue'),
                         QuerySpec(operation='aggregate',metric='revenue',temporal_role='comparison'))))
        def report(self,*a,**kw):
            self.budget.charge('injected_model')
            return ReportDraft()
    server = SchemaServer()
    runners=[]
    def factory(settings,*,limits):
        runner=BigQueryTurn(settings,client=server,limits=limits)
        runners.append(runner)
        return runner
    result=workflow(tmp_path,gateway=Gateway,turn=factory).run('manager_apparel','Revenue report')
    assert result.status in {'ok','partial'}, result.text
    assert len(server.metadata)==4 and runners[0].dry_runs==6  # One rejection, not two.
    records=[json.loads(line) for line in (tmp_path/'workflow-events.jsonl').read_text().splitlines()]
    assert len([r for r in records if r.get('event')=='recovery' and r.get('outcome')=='success'])==1
    assert agent_metrics(tmp_path/'workflow-events.jsonl')['counts']['recovery.outcome.success']==1


def test_transient_dependency_plus_correction_counts_once(tmp_path, approved_migration):
    server=SchemaServer()
    original=server.query
    calls=[]
    def transient(*a,**kw):
        calls.append(kw['job_config'].dry_run)
        if len(calls)==1: raise ge.ServiceUnavailable('controlled transient catalog dry run')
        return original(*a,**kw)
    server.query=transient
    result,_=run_workflow(tmp_path,server)
    assert result.status=='ok',result.text
    events=[json.loads(line) for line in (tmp_path/'workflow-events.jsonl').read_text().splitlines()]
    assert len([e for e in events if e.get('event')=='recovery' and e.get('outcome')=='success'])==1


@pytest.mark.parametrize('second,reason,outcome', [(ge.Forbidden,'not_retryable','failed'),
                                                    (ge.ServiceUnavailable,'retry_limit','exhausted')])
def test_second_failure_uses_actual_decision_not_attempt_index(tmp_path, second, reason, outcome):
    runner,client=turn(base_stages())
    path=tmp_path/'events'
    runner.telemetry=TurnTrace('c','t',path)
    count=[0]
    def failing(*a,**kw):
        count[0]+=1
        raise (ge.ServiceUnavailable if count[0]==1 else second)('private')
    client.query=failing
    with pytest.raises(ExecutionError): runner.run(plan(),'manager_apparel')
    recoveries=[json.loads(line) for line in path.read_text().splitlines() if json.loads(line)['event']=='recovery']
    assert [(e['reason'],e['outcome']) for e in recoveries]==[('transient_before_submission','attempted'),(reason,outcome)]


def test_correction_unavailable_is_unsupported_not_submission_uncertainty(tmp_path, approved_migration):
    result,_=run_workflow(tmp_path,SchemaServer(price='unknown_price'))
    assert result.failure_stage=='metadata' and result.failure_submission_state=='not_submitted'
    events=[json.loads(line) for line in (tmp_path/'workflow-events.jsonl').read_text().splitlines()]
    terminal=[e for e in events if e.get('event')=='recovery' and e.get('outcome') in {'failed','exhausted','unsupported'}]
    assert [(e['reason'],e['outcome']) for e in terminal]==[('correction_unavailable','unsupported')]


def test_cli_json_retains_both_provenance_records(tmp_path,monkeypatch):
    from typer.testing import CliRunner
    from opsfleet_assistant import cli
    server=SchemaServer(price='sale_price')
    server.fail_coverage=True
    result,_=run_workflow(tmp_path,server)
    monkeypatch.setattr(cli,'AssistantWorkflow',lambda *args:SimpleNamespace(run=lambda *a,**kw:result))
    response=CliRunner().invoke(cli.app,['ask','Revenue in 2025','--user','manager_apparel','--db',str(tmp_path/'cli.sqlite'),'--json'])
    assert response.exit_code==1
    data=json.loads(response.output)
    assert data['failure_stage']=='dry_run' and data['failure_purpose']=='coverage'
    assert data['failure_submission_state']=='not_submitted' and data['failure_billed_bytes'] is None
    assert data['analysis_submission_state']=='submitted' and data['analysis_job_id']=='fixture_job'


def test_corrected_analysis_with_failed_diagnostic_never_counts_success(tmp_path, approved_migration):
    server=SchemaServer()
    server.fail_coverage=True
    result,_=run_workflow(tmp_path,server)
    assert result.status=='error' and result.failure_submission_state=='not_submitted'
    assert result.analysis_submission_state=='submitted'
    counts=agent_metrics(tmp_path/'workflow-events.jsonl')['counts']
    assert counts.get('recovery.outcome.success',0)==0
    assert counts.get('recovery.outcome.failed',0)==1
    assert len([sql for sql in server.submissions if 'scoped_items AS' in sql])==1


def test_failed_analysis_retry_is_not_attributed_to_later_unrelated_evidence(tmp_path):
    runner,client=turn(base_stages())
    runner.telemetry=TurnTrace('c','t',tmp_path/'events')
    original=client.query
    transient=[False]
    diagnostic=[False]
    def query(sql,**kw):
        if 'scoped_items AS' in sql and kw['job_config'].dry_run and not transient[0]:
            transient[0]=True
            raise ge.ServiceUnavailable('controlled analytical dry-run transient')
        if 'COUNTIF' in sql and not diagnostic[0]:
            diagnostic[0]=True
            raise ge.Forbidden('controlled diagnostic denial')
        return original(sql,**kw)
    client.query=query
    with pytest.raises(ExecutionError,match='permission_denied'): runner.run(plan(),'manager_apparel')
    assert not agent_metrics(tmp_path/'events')['counts'].get('recovery.outcome.success',0)
    # Independent second operation: new analytical/coverage fixture jobs, same cached catalog.
    client.stages=base_stages()[1:]
    assert runner.run(plan(),'manager_apparel').complete
    assert not agent_metrics(tmp_path/'events')['counts'].get('recovery.outcome.success',0)


def test_recovery_bookkeeping_cannot_release_after_deadline(tmp_path,monkeypatch):
    from opsfleet_assistant import bigquery_adapter as module
    runner,_=turn(base_stages())
    runner.telemetry=TurnTrace('c','t',tmp_path/'events')
    runner._pending_corrections={'corrected'}
    def late_fingerprint(query):
        runner._clock=lambda:runner._started+121
        return 'corrected'
    monkeypatch.setattr(module,'execution_fingerprint',late_fingerprint)
    with pytest.raises(ExecutionError,match='warehouse_timeout'):
        runner.run(plan(),'manager_apparel')
    assert not agent_metrics(tmp_path/'events')['counts'].get('recovery.outcome.success',0)


@pytest.mark.parametrize('phase', ['coverage', 'customer_check', 'evidence_checks'])
def test_unexpected_post_analysis_failure_keeps_only_trusted_provenance(tmp_path, monkeypatch, phase):
    from typer.testing import CliRunner
    from opsfleet_assistant import bigquery_adapter as module, cli
    server=SchemaServer(price='sale_price')
    secret='controlled private provider detail'
    def failed(*a,**kw):
        error=RuntimeError(secret)
        error.job_id='forged_job'
        error.analysis_job_id='forged_analysis'
        error.submission_state='not_submitted'
        raise error
    if phase=='evidence_checks':
        monkeypatch.setattr(module,'release_evidence',failed)
    elif phase=='coverage':
        monkeypatch.setattr(BigQueryTurn,'_coverage',failed)
    else:
        monkeypatch.setattr(BigQueryTurn,'_check_customer_ids',failed)
        class RankGateway(FakeGateway):
            def interpret(self,*a,**kw):
                self.budget.charge('injected_model')
                return InterpretationResult(status='ready',request=RequestSpec(
                    analysis=TemporalExpression(kind='year',year=YearExpression(kind='absolute',value=2025)),
                    queries=(QuerySpec(operation='rank_customers',metric='customer_spend'),)))
        from decimal import Decimal
        from test_step3 import fields
        runner,client=turn(base_stages(rows=({'user_id':11,'customer_spend':Decimal('42.50')},),
            schema=fields(('user_id','INT64'),('customer_spend','NUMERIC'))))
        result=workflow(tmp_path,gateway=RankGateway,turn=lambda *a,**kw:runner).run('manager_apparel','Customer spend in 2025')
    if phase!='customer_check':
        result,runner=run_workflow(tmp_path,server)
    assert result.status=='error' and result.error_category=='internal'
    assert 'internal_failure' in result.text and result.report_id is None
    assert result.failure_stage=='evidence_checks' and result.failure_purpose==phase
    assert result.analysis_submission_state=='submitted'
    assert result.analysis_job_id==('analysis_job' if phase=='customer_check' else 'fixture_job')
    assert result.analysis_billed_bytes==10
    assert result.failure_submission_state=='unknown' and result.failure_job_id is None
    assert runner.executions==(3 if phase=='evidence_checks' else 2)
    assert not agent_metrics(tmp_path/'workflow-events.jsonl')['counts'].get('recovery.outcome.success',0)
    monkeypatch.setattr(cli,'AssistantWorkflow',lambda *a:SimpleNamespace(run=lambda *a,**kw:result))
    response=CliRunner().invoke(cli.app,['ask','Revenue in 2025','--user','manager_apparel','--db',str(tmp_path/'cli.sqlite'),'--json'])
    data=json.loads(response.output)
    assert response.exit_code==1 and data['analysis_job_id']==result.analysis_job_id
    assert data['failure_stage']=='evidence_checks' and data['analysis_submission_state']=='submitted'
    assert data['failure_billed_bytes'] is None
    output=StringIO()
    render_result(Console(file=output,width=200,color_system=None),result,verbose=True)
    assert 'Analysis completed' in output.getvalue() and result.analysis_job_id in output.getvalue()
    assert 'Automatic replay stopped' in output.getvalue()
    assert secret not in response.output+output.getvalue()+(tmp_path/'workflow-events.jsonl').read_text()
    assert 'forged_' not in response.output+output.getvalue()


@pytest.fixture
def approved_region_migration(monkeypatch):
    from opsfleet_assistant import source_schema as schema
    binding=replace(schema.PUBLIC_BINDING,version='synthetic_region_v2',fields=tuple(
        replace(f,physical='region') if (f.table,f.logical)==('users','state') else f
        for f in schema.PUBLIC_BINDING.fields))
    monkeypatch.setattr(schema,'APPROVED_BINDINGS',(schema.PUBLIC_BINDING,binding))
    return binding


def test_unaffected_binding_adoption_is_not_a_correction(approved_region_migration):
    from opsfleet_assistant.compiler import execution_fingerprint, is_trusted_compiled
    runner,client=turn(base_stages())
    catalog=runner.load_catalog()
    scope=resolve_demo_scope('manager_apparel',catalog)
    query=compile_plan(plan(),scope,catalog)
    runner._inspected_binding=approved_region_migration
    runner._schema_inspection_started=True
    before=len(client.calls)
    adopted=runner.adopt_inspected_binding(query,plan(),'manager_apparel')
    assert is_trusted_compiled(adopted) and adopted.source_binding_version=='synthetic_region_v2'
    assert semantic_fingerprint(query)==semantic_fingerprint(adopted)
    assert execution_fingerprint(query)==execution_fingerprint(adopted)
    assert len(client.calls)==before and not runner._pending_corrections
    assert not runner._recovery_reported and runner.schema_metadata_requests==0
    with pytest.raises(ExecutionError,match='query_schema_error'):
        runner.correct_schema_binding(query,plan(),'manager_apparel')
    with pytest.raises(ExecutionError,match='recovery_scope_changed'):
        runner.adopt_inspected_binding(query,plan(product_ids=(1,)),'manager_apparel')
    with pytest.raises(ExecutionError):
        runner.adopt_inspected_binding(query,plan(),'ceo')
    assert len(client.calls)==before and not runner._pending_corrections


def test_mixed_report_executes_affected_and_unaffected_queries(tmp_path,approved_region_migration):
    server=SchemaServer(price='sale_price',empty=True)
    server.db.execute('ALTER TABLE users RENAME COLUMN state TO region')
    server.db.executemany('INSERT INTO users(id,region) VALUES (?,?)',[(n,'A') for n in range(1,9)])
    server.db.execute('DELETE FROM orders')
    server.db.executemany('INSERT INTO orders VALUES (?,?)',[(n,n) for n in range(1,9)])
    server.db.executemany('INSERT INTO order_items VALUES (?,?,?,?,?)',
        [(n,1,'2025-02-01',20,'Complete') for n in range(1,6)]+
        [(6,3,'2025-02-01',900,'Complete'),(7,2,'2024-02-01',800,'Complete'),(8,1,'2025-02-01',700,'Cancelled')])
    class Gateway(FakeGateway):
        def interpret(self,*a,**kw):
            self.budget.charge('injected_model')
            return InterpretationResult(status='ready',request=RequestSpec(mode='report',
                analysis=TemporalExpression(kind='year',year=YearExpression(kind='absolute',value=2025)),
                queries=(QuerySpec(operation='demographic_comparison',metric='average_customer_spend',dimension='state',limit=10),
                         QuerySpec(operation='aggregate',metric='revenue'))))
        def report(self,*a,**kw):
            self.budget.charge('injected_model')
            return ReportDraft()
    runners=[]
    def factory(settings,*,limits):
        runner=BigQueryTurn(settings,client=server,limits=limits)
        runners.append(runner)
        return runner
    result=workflow(tmp_path,gateway=Gateway,turn=factory).run('manager_apparel','Regional spend and revenue report in 2025')
    assert result.status in {'ok','partial'},result.text
    demographic=[rows for sql,rows in server.results if 'demographic_customers' in sql]
    aggregate=[rows for sql,rows in server.results if 'scoped_items AS' in sql and 'demographic_customers' not in sql]
    assert demographic==[[{'demographic_value':'A','customers':5,'revenue':100.0,'average_customer_spend':20.0}]]
    assert aggregate==[[{'revenue':100.0}]]
    assert runners[0].executions==5 and runners[0].dry_runs==6
    assert runners[0].schema_metadata_requests==len(server.metadata)==4
    assert len([sql for sql in server.submissions if 'scoped_items AS' in sql])==2
    records=[json.loads(line) for line in (tmp_path/'workflow-events.jsonl').read_text().splitlines()]
    assert len([r for r in records if r.get('action')=='recompile_supported_binding' and r.get('outcome')=='attempted'])==1
    assert len([r for r in records if r.get('event')=='recovery' and r.get('outcome')=='success'])==1
