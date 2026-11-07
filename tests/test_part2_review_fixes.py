"""R1–R4: adversarial selections, downgrade, privacy outcomes and SQL ordering.

All warehouse/model inputs are injected; no live interpretation claim.
"""
from datetime import date
from decimal import Decimal
from io import StringIO
import json

import pytest
from rich.console import Console
from opsfleet_assistant.bigquery_adapter import BigQueryTurn
from opsfleet_assistant.contracts import (ReportDraft, FindingSelection, ObservationRef)
from opsfleet_assistant.identity import PolicyError
from opsfleet_assistant.reporting import calculate, ground_report
from opsfleet_assistant.report_artifact import reconstruct_grounded
from opsfleet_assistant.presentation import render_result
from opsfleet_assistant.store import SQLiteStore
from opsfleet_assistant.workflow import AssistantWorkflow
from test_playbooks import (PlaybookGateway, SETTINGS, spec, regional_spec, churn_spec,
                           churn_rows, churn_plan, released, run_workflow, product_rows,
                           regional_rows)
from playbook_source import product_source, regional_source, churn_source


def flow(tmp_path, client, request_spec, draft=ReportDraft()):
    class Gateway(PlaybookGateway):
        spec=request_spec
        def report(self,*args,**kwargs):
            self.budget.charge('offline_drafting')
            return draft
    runners=[]
    def factory(settings,*,limits):
        t=BigQueryTurn(settings,client=client,limits=limits);runners.append(t);return t
    store=SQLiteStore(tmp_path/'review.sqlite')
    wf=AssistantWorkflow(SETTINGS,store,gateway_factory=Gateway,turn_factory=factory,today=date(2026,11,6))
    return wf,store,runners


def pair(a,b,metric='churn_rate',operation='percentage_point_change'):
    return FindingSelection(operation=operation,current=ObservationRef(evidence_index=0,row_index=a,metric=metric),
                            baseline=ObservationRef(evidence_index=0,row_index=b,metric=metric))


@pytest.mark.parametrize('a,b',[(3,1),(4,4),(5,4),(0,3)])
def test_r1_incompatible_churn_draft_falls_back_and_saves_true_premise(tmp_path,a,b):
    # Production SQL: A current/baseline, B current/baseline, H current/baseline.
    wf,store,_=flow(tmp_path,churn_source(),churn_spec(report=True),ReportDraft(findings=(pair(a,b),),headline_index=0))
    result=wf.run('manager_apparel','Investigate churn')
    assert result.report_id and result.report_completeness=='complete',result.text
    assert result.presentation['selection_status']=='fallback'
    assert 'rose by 11.00 percentage points' in result.presentation['headline']
    assert 'spike is not supported' not in result.text
    saved=wf.run('manager_apparel','save',conversation_id=result.conversation_id)
    assert saved.report_id and saved.presentation['headline']==result.presentation['headline']
    snapshot=store.load_continuity('manager_apparel',result.conversation_id).snapshot
    assert reconstruct_grounded(snapshot)==snapshot.grounded
    # Reconstruction independently rejects old saved incompatible references.
    bad=calculate(pair(4,5),(snapshot.evidence[0],))
    forged=bad.model_copy(update={'selection':pair(a,b)})
    snapshot=snapshot.model_copy(update={'grounded':snapshot.grounded.model_copy(update={'findings':(forged,), 'headline':forged.text})})
    with pytest.raises(PolicyError):reconstruct_grounded(snapshot)


def test_r1_same_country_temporal_finding_is_labeled_but_cannot_headline_spike(tmp_path):
    wf,store,_=flow(tmp_path,churn_source(),churn_spec(report=True),ReportDraft(findings=(pair(0,1),pair(4,5)),headline_index=1))
    result=wf.run('manager_apparel','Country temporal comparison')
    assert result.report_id and result.presentation['selection_status']=='selected',result.text
    assert 'country A' in result.factual_findings[0]['text']
    assert 'current cutoff' in result.factual_findings[0]['text'] and 'baseline cutoff' in result.factual_findings[0]['text']
    assert '11.00 percentage points' in result.presentation['headline']
    wf2,_,_=flow(tmp_path/'invalid',churn_source(),churn_spec(report=True),ReportDraft(findings=(pair(0,1),),headline_index=0))
    rejected=wf2.run('manager_apparel','Country-only headline')
    assert rejected.presentation['selection_status']=='fallback'
    assert '11.00 percentage points' in rejected.presentation['headline']


@pytest.mark.parametrize('request_factory,rows',[(spec,product_rows),(regional_spec,regional_rows),(churn_spec,churn_rows)])
@pytest.mark.parametrize('tamper',[False,True])
def test_r2_missing_support_cannot_downgrade_playbooks(tmp_path,request_factory,rows,tamper):
    wf,store,result=run_workflow(tmp_path,request_factory(report=True),rows())
    fields=store.report('manager_apparel',result.report_id)
    grounded=json.loads(fields['grounded_report_json'])
    if tamper:grounded['driver']['contributions'][0]['value']='123456'
    store.db.execute('UPDATE reports SET report_support_json=?,report_binding_digest=?,grounded_report_json=? WHERE id=?',
        ('','',json.dumps(grounded),result.report_id));store.db.commit()
    replay=wf.run('manager_apparel','offline investigation',conversation_id=result.conversation_id,turn_id=result.turn_id)
    assert replay.status=='error' and not replay.reused_report and replay.report_id is None
    assert replay.driver_finding is None and replay.presentation is None
    assert '123456' not in replay.text


def suppressed_source(which):
    if which=='regional':
        client=regional_source()
        client.db.execute('DELETE FROM users WHERE id NOT IN (1,2,101,102)')
        request=regional_spec(report=True)
    else:
        client=churn_source()
        client.db.execute("UPDATE order_items SET created_at='2025-03-01T00:00:00+00:00'")
        request=churn_spec(report=True)
    return client,request


@pytest.mark.parametrize('which',['regional','churn'])
@pytest.mark.parametrize('retry',[False,True])
def test_r3_fully_protected_results_stay_unavailable_through_save_restart_and_counters(tmp_path,which,retry):
    client,request=suppressed_source(which)
    if retry:
        from google.api_core import exceptions as ge
        original=client.query;failed=[False]
        def once(sql,**kwargs):
            if 'scoped_items AS' in sql and kwargs['job_config'].dry_run and not failed[0]:
                failed[0]=True
                raise ge.ServiceUnavailable('Synthetic pre-submission transient')
            return original(sql,**kwargs)
        client.query=once
    wf,store,runners=flow(tmp_path,client,request)
    first=wf.run('manager_apparel','Withheld populations')
    assert first.report_id and first.status=='partial',first.text
    assert first.presentation['empty_diagnosis']=='unavailable'
    assert first.presentation['evidence'][0]['rows']==[]
    saved=wf.run('manager_apparel','save',conversation_id=first.conversation_id)
    replay=wf.run('manager_apparel','save',conversation_id=first.conversation_id,turn_id=saved.turn_id)
    assert replay.reused_report and replay.text==saved.text
    for result in (first,saved,replay):
        assert 'No safely releasable observations' in result.text
        assert not any(s in result.text for s in ('No matching data','No qualifying rows','No matching scoped source data'))
        output=StringIO();render_result(Console(file=output,width=45),result)
        text=output.getvalue()
        assert 'No qualifying rows' not in text and 'No matching' not in text
    events=[json.loads(line) for line in (tmp_path/'workflow-events.jsonl').read_text().splitlines()]
    assert not any(e['event']=='empty_result' and e.get('decision')=='valid_empty' for e in events)
    assert not any(e['event']=='recovery' and e.get('outcome')=='success' for e in events)
    if retry:assert failed[0] and runners[0].dry_runs==4


@pytest.mark.parametrize('which',['product','regional'])
@pytest.mark.parametrize('draft_status',['selected','refused'])
def test_r4_sql_sorted_rows_follow_requested_default_direction(tmp_path,which,draft_status):
    if which=='product':
        client=product_source();request=spec(report=True)
        request=request.model_copy(update={'queries':(request.queries[0].model_copy(update={'product_ids':(2,1)}),)})
        expected=Decimal('-400');key='product_id';first_choice=2
    else:
        client=regional_source();request=regional_spec(report=True)
        expected=Decimal('20');key='demographic_value';first_choice='Texas'
    wf,store,_=flow(tmp_path,client,request,ReportDraft(status=draft_status))
    result=wf.run('manager_apparel','Ordered comparison')
    assert result.report_id,result.text
    evidence=result.presentation['evidence'][0]
    assert evidence['rows'][result.factual_findings[0]['selection']['current']['row_index']][key]==first_choice
    deltas=[f for f in result.factual_findings if f['selection']['operation']=='difference']
    assert Decimal(deltas[0]['value'])==expected==Decimal(result.driver_finding['total_difference'])
    saved=wf.run('manager_apparel','save',conversation_id=result.conversation_id)
    assert saved.report_id and saved.factual_findings==result.factual_findings


@pytest.mark.parametrize('which',['product','regional','churn'])
def test_saved_v2_valid_selections_reconstruct_without_new_defaults_or_labels(tmp_path,which):
    from opsfleet_assistant.reporting import GroundedReport
    from opsfleet_assistant.contracts import FindingSelection
    # Independently describe pre-revision SQL-order defaults, without depending
    # on the new default generator to encode the original saved ordering.
    if which=='product':
        client=product_source();request=spec(report=True)
        request=request.model_copy(update={'queries':(request.queries[0].model_copy(update={'product_ids':(2,1)}),)})
    elif which=='regional':client=regional_source();request=regional_spec(report=True)
    else:client=churn_source();request=churn_spec(report=True)
    wf,store,_=flow(tmp_path,client,request)
    result=wf.run('manager_apparel','Old supported report')
    snapshot=store.load_continuity('manager_apparel',result.conversation_id).snapshot
    evidence=snapshot.evidence
    if which=='churn':
        selections=[FindingSelection(operation='observed_value',current=ObservationRef(evidence_index=0,row_index=i,metric='churn_rate')) for i in (4,5)]
        selections += [pair(4,5)]
    else:
        metric='revenue' if which=='product' else 'average_customer_spend'
        selections=[FindingSelection(operation='observed_value',current=ObservationRef(evidence_index=0,row_index=i,metric=metric)) for i in (0,1)]
        selections += [pair(0,1,metric,'difference')]
        if which=='product':selections += [pair(0,1,m,'difference') for m in ('item_count','average_item_price')]
    findings=tuple(calculate(s,evidence,legacy_churn_labels=True) for s in selections)
    old=GroundedReport(schema_version=2,driver=snapshot.grounded.driver,factual_status='grounded',selection_status='selected',
        headline=findings[0].text,findings=findings)
    prior=snapshot.model_copy(update={'grounded':old})
    assert reconstruct_grounded(prior)==old
    if which=='product':assert old.findings[2].value==Decimal('400') # old explicit opposite pair preserved
    elif which=='regional':assert old.findings[2].value==Decimal('-20')
    else:assert 'overall prior purchasers' not in old.headline


@pytest.mark.parametrize('which',['product','regional'])
def test_r4_new_model_selected_reverse_pair_cannot_override_requested_direction(tmp_path,which):
    if which=='product':
        client=product_source();request=spec(report=True)
        request=request.model_copy(update={'queries':(request.queries[0].model_copy(update={'product_ids':(2,1)}),)})
        metric='revenue';a,b=0,1;expected=Decimal('-400')
    else:
        client=regional_source();request=regional_spec(report=True)
        metric='average_customer_spend';a,b=0,1;expected=Decimal('20')
    wf,_,_=flow(tmp_path,client,request,ReportDraft(findings=(pair(a,b,metric,'difference'),),headline_index=0))
    result=wf.run('manager_apparel','Wrong model comparison order')
    assert result.report_id and result.presentation['selection_status']=='fallback',result.text
    assert any(Decimal(f['value'])==expected for f in result.factual_findings if f['selection']['operation']=='difference')


def test_safe_headline_only_after_retry_counts_recovered_observations(tmp_path):
    from google.api_core import exceptions as ge
    client=churn_source();client.db.execute("UPDATE users SET country='Tiny' WHERE id=1")
    original=client.query;failed=[False]
    def once(sql,**kwargs):
        if 'scoped_items AS' in sql and kwargs['job_config'].dry_run and not failed[0]:
            failed[0]=True
            raise ge.ServiceUnavailable('Synthetic transient')
        return original(sql,**kwargs)
    client.query=once
    wf,_,_=flow(tmp_path,client,churn_spec(report=True))
    result=wf.run('manager_apparel','Recover safe headlines')
    assert result.status=='partial' and len(result.presentation['evidence'][0]['rows'])==2
    events=[json.loads(line) for line in (tmp_path/'workflow-events.jsonl').read_text().splitlines()]
    assert sum(e['event']=='recovery' and e.get('outcome')=='success' for e in events)==1
