"""Independent row populations through the real compiler, adapter and workflow."""
from datetime import date
from decimal import Decimal
import pytest
from opsfleet_assistant.bigquery_adapter import BigQueryTurn, QueryLimits, ExecutionError
from opsfleet_assistant.workflow import AssistantWorkflow
from opsfleet_assistant.store import SQLiteStore
from test_playbooks import (SETTINGS, PlaybookGateway, spec, regional_spec, churn_spec,
                           product_plan, regional_plan, churn_plan)
from opsfleet_assistant.compiler import compile_plan, semantic_fingerprint
from opsfleet_assistant.playbooks import investigate
from opsfleet_assistant.identity import resolve_demo_scope
from playbook_source import product_source,regional_source,churn_source

CASES=[(product_source,spec,product_plan,('400','550','-150')),
       (regional_source,regional_spec,regional_plan,('20','65','-45')),
       (churn_source,churn_spec,churn_plan,('.11','.01','.10'))]

@pytest.mark.parametrize('source,spec_factory,plan,expected',CASES)
def test_independent_sql_adapter_workflow(tmp_path,source,spec_factory,plan,expected):
    client=source();runners=[]
    class Gateway(PlaybookGateway):pass
    Gateway.spec=spec_factory(report=True)
    def factory(settings,*,limits):
        turn=BigQueryTurn(settings,client=client,limits=limits);runners.append(turn);return turn
    wf=AssistantWorkflow(SETTINGS,SQLiteStore(tmp_path/'source.sqlite'),gateway_factory=Gateway,turn_factory=factory,today=date(2026,11,6))
    result=wf.run('manager_apparel','Independent offline row fixture')
    assert result.report_id,result.text
    d=result.driver_finding
    assert d['complete'],d
    assert tuple(Decimal(v) for v in (d['total_difference'],*(c['value'] for c in d['contributions'])))==tuple(map(Decimal,expected))
    assert runners[0].executions==3 and runners[0].dry_runs==3
    assert len(client.calls)==6
    saved=wf.run('manager_apparel','save',conversation_id=result.conversation_id)
    assert saved.driver_finding==d and saved.report_id,saved.text
    # Exact save runs only the catalog authorization dependency, no new analysis.
    assert [stage for stage,dry,_ in client.calls[6:] if not dry]==['catalog']

@pytest.mark.parametrize('source,spec_factory,plan,expected',CASES)
@pytest.mark.parametrize('fault,stage',[('ambiguous','analysis'),('coverage','coverage'),('schema','analysis')])
def test_new_paths_inherit_no_replay_and_recovery_stop(tmp_path,source,spec_factory,plan,expected,fault,stage):
    client=source();client.fault=fault;client.fault_stage=stage
    class Gateway(PlaybookGateway):pass
    Gateway.spec=spec_factory()
    runners=[]
    def factory(settings,*,limits):
        t=BigQueryTurn(settings,client=client,limits=limits);runners.append(t);return t
    wf=AssistantWorkflow(SETTINGS,SQLiteStore(tmp_path/'fault.sqlite'),gateway_factory=Gateway,turn_factory=factory,today=date(2026,11,6))
    result=wf.run('manager_apparel','Injected offline failure')
    assert result.status=='error' and result.report_id is None,result.text
    assert sum(stage=='analysis' and not dry for stage,dry,_ in client.calls)<=1
    if fault=='coverage':assert result.analysis_submission_state=='submitted' and result.analysis_job_id
    if fault=='schema':assert len(client.metadata)==4

@pytest.mark.parametrize('source,spec_factory,plan,expected',CASES)
def test_execution_cap_and_full_plan_seal(source,spec_factory,plan,expected):
    client=source();turn=BigQueryTurn(SETTINGS,client=client,limits=QueryLimits(max_executions=2))
    catalog=turn.load_catalog();p=plan();q=compile_plan(p,resolve_demo_scope('manager_apparel',catalog),catalog)
    with pytest.raises(ExecutionError):turn.execute_compiled(q)
    assert turn.executions<=2
    assert semantic_fingerprint(q)!=semantic_fingerprint(compile_plan(type(p).model_validate({**p.model_dump(),'revenue_statuses':('Complete',)}),resolve_demo_scope('manager_apparel',catalog),catalog))

@pytest.mark.parametrize('source,spec_factory,plan,expected',CASES)
def test_approved_binding_correction_and_reuse(tmp_path,monkeypatch,source,spec_factory,plan,expected):
    from dataclasses import replace
    from opsfleet_assistant import source_schema as ss
    migration=replace(ss.PUBLIC_BINDING,version='synthetic_part2_price_v2',fields=tuple(
        replace(f,physical='unit_price') if (f.table,f.logical)==('order_items','sale_price') else f for f in ss.PUBLIC_BINDING.fields))
    monkeypatch.setattr(ss,'APPROVED_BINDINGS',(ss.PUBLIC_BINDING,migration))
    client=source();client.db.execute('ALTER TABLE order_items RENAME COLUMN sale_price TO unit_price')
    # SQLite raises a real missing-column rejection; translate at the transport.
    original=client.query
    def query(*a,**kw):
        import sqlite3
        from google.api_core import exceptions as ge
        try:return original(*a,**kw)
        except sqlite3.OperationalError as exc:raise ge.BadRequest('Unrecognized name: controlled column migration') from exc
    client.query=query
    class Gateway(PlaybookGateway):pass
    Gateway.spec=spec_factory(report=True)
    turns=[]
    def factory(settings,*,limits):
        t=BigQueryTurn(settings,client=client,limits=limits);turns.append(t);return t
    wf=AssistantWorkflow(SETTINGS,SQLiteStore(tmp_path/'migration.sqlite'),gateway_factory=Gateway,turn_factory=factory,today=date(2026,11,6))
    result=wf.run('manager_apparel','Synthetic migration')
    assert result.report_id,result.text
    assert result.driver_finding['complete'] and len(client.metadata)==4
    assert tuple(map(Decimal,(result.driver_finding['total_difference'],*(c['value'] for c in result.driver_finding['contributions']))))==tuple(map(Decimal,expected))

@pytest.mark.parametrize('which',['regional','churn'])
def test_sql_privacy_suppression_and_unknown_reconciliation(tmp_path,which):
    if which=='regional':
        client=regional_source()
        client.db.execute("DELETE FROM users WHERE state='California' AND id>104")
        chosen=regional_spec(report=True)
    else:
        client=churn_source()
        # Move one purchaser to a small distinct country. No partial segments.
        client.db.execute("UPDATE users SET country='Tiny' WHERE id=1")
        chosen=churn_spec(report=True)
    class Gateway(PlaybookGateway):pass
    Gateway.spec=chosen
    wf=AssistantWorkflow(SETTINGS,SQLiteStore(tmp_path/'private.sqlite'),gateway_factory=Gateway,
        turn_factory=lambda settings,limits:BigQueryTurn(settings,client=client,limits=limits),today=date(2026,11,6))
    result=wf.run('manager_apparel','Privacy limited offline')
    assert result.report_completeness=='partial',result.text
    assert not result.driver_finding['contributions'] and result.driver_finding['residual'] is None
    evidence=result.presentation['evidence'][0]
    assert evidence['coverage']['period_rows'] is None and evidence['coverage']['source_rows'] is None
    assert 'Tiny' not in json_text(result)
    if which=='churn':assert all(row['segment_key']=='H:' for row in evidence['rows'])

def json_text(result):
    from dataclasses import asdict
    import json
    return json.dumps(asdict(result))

@pytest.mark.parametrize('variant',['null','literal_unknown','new_segment','too_many','unsafe_headline','no_population'])
def test_country_full_population_or_safe_headline_only(variant):
    client=churn_source()
    if variant=='null':client.db.execute("UPDATE users SET country=NULL WHERE country='A'")
    elif variant=='literal_unknown':
        client.db.execute("UPDATE users SET country=NULL WHERE country='A'")
        client.db.execute("UPDATE users SET country='U:' WHERE country='B'")
    elif variant=='new_segment':client.db.execute("UPDATE users SET country='New' WHERE id>1400")
    elif variant=='too_many':client.db.execute("UPDATE users SET country=CAST(id % 10 AS TEXT)")
    elif variant=='unsafe_headline':client.db.execute("UPDATE order_items SET created_at='2025-03-01T00:00:00+00:00'")
    else:client.db.execute('DELETE FROM order_items')
    turn=BigQueryTurn(SETTINGS,client=client)
    catalog=turn.load_catalog();e=turn.execute_compiled(compile_plan(churn_plan(),resolve_demo_scope('manager_apparel',catalog),catalog))
    d=investigate((e,))
    if variant in {'null','literal_unknown'}:
        assert d.complete and d.total_difference==Decimal('.11')
        keys={r['segment_key'] for r in e.rows}
        assert 'U:' in keys
        if variant=='literal_unknown':assert 'C:U:' in keys
    else:
        assert not d.complete and d.residual is None and not d.contributions
        assert all(r['segment_key']=='H:' for r in e.rows)

@pytest.mark.parametrize('source,spec_factory,plan,expected',CASES)
@pytest.mark.parametrize('fault',['release_exception','release_deadline','coverage_deadline'])
def test_completed_analysis_provenance_and_deadline_at_release(tmp_path,monkeypatch,source,spec_factory,plan,expected,fault):
    from opsfleet_assistant import bigquery_adapter as module
    client=source();turns=[]
    class Gateway(PlaybookGateway):pass
    Gateway.spec=spec_factory(report=True)
    original=module.release_evidence
    def release(*args,**kw):
        if fault=='release_exception':raise RuntimeError('PRIVATE fake submitted job and secret operands')
        value=original(*args,**kw)
        turns[0]._clock=lambda:turns[0]._started+121
        return value
    if fault!='coverage_deadline':monkeypatch.setattr(module,'release_evidence',release)
    def factory(settings,*,limits):
        t=BigQueryTurn(settings,client=client,limits=limits);turns.append(t)
        if fault=='coverage_deadline':
            coverage=t._coverage
            def expired(q):
                result=coverage(q);t._clock=lambda:t._started+121;return result
            t._coverage=expired
        return t
    wf=AssistantWorkflow(SETTINGS,SQLiteStore(tmp_path/'deadline.sqlite'),gateway_factory=Gateway,turn_factory=factory,today=date(2026,11,6))
    result=wf.run('manager_apparel','Fault injection')
    assert result.status=='error' and result.report_id is None,result.text
    assert result.analysis_submission_state=='submitted' and result.analysis_job_id
    assert 'PRIVATE' not in json_text(result)
    assert sum(stage=='analysis' and not dry for stage,dry,_ in client.calls)==1

@pytest.mark.parametrize('source,spec_factory,plan,expected',CASES)
@pytest.mark.parametrize('stage',['analysis','coverage'])
def test_bounded_transient_dry_run_retry(source,spec_factory,plan,expected,stage):
    from google.api_core import exceptions as ge
    client=source();original=client.query;failed=[False]
    def transient(sql,**kwargs):
        is_stage=('scoped_items AS' in sql) if stage=='analysis' else ('source_rows' in sql)
        if is_stage and kwargs['job_config'].dry_run and not failed[0]:
            failed[0]=True
            raise ge.ServiceUnavailable('Controlled pre-submission transient')
        return original(sql,**kwargs)
    client.query=transient
    t=BigQueryTurn(SETTINGS,client=client);catalog=t.load_catalog()
    e=t.execute_compiled(compile_plan(plan(),resolve_demo_scope('manager_apparel',catalog),catalog))
    assert e.complete and failed[0] and t.executions==3 and t.dry_runs==4
