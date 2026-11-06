"""Exact snapshot saves permit honest partial reports and reject altered content."""
from copy import deepcopy
import json

import pytest

from opsfleet_assistant import evaluation_runner as runner
from opsfleet_assistant.contracts import InterpretationResult,RequestSpec,QuerySpec,TemporalExpression,YearExpression,ReportDraft,ActionSelection
from opsfleet_assistant.evaluation_contracts import assertions
from test_evaluation_preparation import SETTINGS,account,http_transport
from test_compact_intent_prompt import contextual
from test_post_full_remediation import case


@pytest.fixture(params=['selected','refused','neutral'])
def journey(tmp_path,monkeypatch,request):
    def spec(year,month):return RequestSpec(analysis=TemporalExpression(kind='month',month=month,
        year=YearExpression(kind='absolute',value=year)),queries=(QuerySpec(operation='aggregate',metric='revenue',revenue_statuses=('Complete',)),))
    draft={'selected':ReportDraft(recommendations=(ActionSelection(kind='review_performance',finding_index=0),)),
        'refused':ReportDraft(status='refused'),'neutral':ReportDraft()}[request.param]
    replies=[InterpretationResult(status='ready',request=spec(2025,1)),draft,contextual('explain'),
        contextual('why'),contextual('summary'),InterpretationResult(status='ready',context_action='correct',
        temporal_updates=('analysis',),request=spec(2024,2)),draft,contextual('save')]
    http_transport(monkeypatch,[r.model_dump_json() for r in replies])
    a=account(tmp_path);c=case('journey')
    rows=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    return c['turns'][-1],rows[-1]['attempts'][0]['observed'],rows[-2]['attempts'][0]['observed'],rows,tmp_path


def test_exact_save_preserves_original_content_with_honest_completeness(journey):
    turn,saved,previous,rows,_=journey
    assert turn.get('compare_to')=='journey-t05'
    assert all(c['passed'] for c in assertions(turn['assertions'],saved,previous))
    assert [r['status'] for r in rows]==['PASS']*6
    assert saved['saved_report_integrity'] and saved['saved'] and saved['analysis_queries']==0
    assert saved['factual_findings']==previous['factual_findings']
    assert saved['suggestions']==previous['suggestions']
    expected='complete' if previous['suggestions'] else 'partial'
    assert saved['report_completeness']==expected


@pytest.mark.parametrize('mutation',['figures','dates','scope','statuses','evidence','actions','unsaved','storage','analytics','missing_section','false_completeness'])
def test_exact_save_rejects_changed_content_and_false_labels(journey,mutation):
    turn,saved,previous,_,_=journey
    changed=deepcopy(saved)
    if mutation=='figures':changed['factual_findings'][0]['value']='999'
    elif mutation=='dates':changed['resolved_periods']['analysis']['start']='2023-02-01'
    elif mutation=='scope':changed['presentation']['evidence'][0]['execution_scope']['brands']=['Calvin Klein']
    elif mutation=='statuses':changed['presentation']['evidence'][0]['metric_definition']['revenue_statuses']=['Returned']
    elif mutation=='evidence':changed['evidence_job_ids']=['changed-job']
    elif mutation=='actions':changed['suggestions']=[] if previous['suggestions'] else [{'kind':'recommendations','selection':{'kind':'review_performance','finding_index':0}}]
    elif mutation=='unsaved':changed['saved']=False;changed['report_id']=None
    elif mutation=='storage':changed['saved_report_integrity']=False
    elif mutation=='analytics':changed['analysis_queries']=1
    elif mutation=='missing_section':changed['delivered_report_sections'].remove('recommendations')
    elif mutation=='false_completeness':
        if saved['report_completeness']=='partial':
            changed['report_completeness']='complete';changed['status']='ok';changed['report_composition']['missing']=[]
            changed['supported_report_sections'].append('recommendations')
            changed['report_composition']['sections'][-1]['status']='supported'
        else:
            changed['report_completeness']='partial';changed['status']='partial';changed['report_composition']['missing']=['recommendations']
            changed['supported_report_sections'].remove('recommendations')
            changed['report_composition']['sections'][-1]['status']='missing'
    assert not all(c['passed'] for c in assertions(turn['assertions'],changed,previous)),mutation


def test_new_report_still_requires_supported_recommendations():
    c=case('report-rollover');rules=c['turns'][0]['assertions']
    assert any(r['path']=='supported_report_sections' and r.get('value')=='recommendations' for r in rules)


def test_missing_original_snapshot_cannot_satisfy_exact_save(journey):
    turn,saved,_,_,_=journey
    assert not all(c['passed'] for c in assertions(turn['assertions'],saved,None))


@pytest.mark.parametrize('column',['content','report_completeness','report_composition_json','grounded_report_json'])
def test_persistence_projection_reads_actual_saved_fields(journey,column):
    from dataclasses import fields
    from opsfleet_assistant.workflow import TurnResult
    from opsfleet_assistant.store import SQLiteStore
    _,saved,_,_,directory=journey
    result=TurnResult(**{f.name:saved[f.name] for f in fields(TurnResult) if f.name in saved})
    store=SQLiteStore(directory/'journey/state.sqlite')
    try:
        assert runner.saved_report_integrity(result,store,'manager_apparel')
        invalid='wrong' if column in {'content','report_completeness'} else '{}'
        store.db.execute('UPDATE reports SET '+column+'=? WHERE id=?',(invalid,saved['report_id']))
        assert not runner.saved_report_integrity(result,store,'manager_apparel')
    finally:store.close()
