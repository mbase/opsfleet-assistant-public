"""Compact contextual intent prompt checks; substituted replies are not live semantics."""
import json

import pytest

from opsfleet_assistant.contracts import (InterpretationProposal, InterpretationResult, RequestSpec, QuerySpec,
    TemporalExpression, YearExpression, ReportDraft, ActionSelection)
from opsfleet_assistant.model_gateway import GeminiGateway,_strict_schema
from opsfleet_assistant import evaluation_runner as runner
from test_evaluation_preparation import SETTINGS,account,http_transport
from test_post_full_remediation import case


def contextual(intent):
    return InterpretationResult(status='ready',context_action='continue',request=RequestSpec(intent=intent))


@pytest.mark.parametrize('intent,message',[
    ('save','Save that exact analysis as a report.'),('explain','Explain those figures.'),
    ('why','Why did that happen?'),('summary','Summarize that.')])
def test_compact_prompt_example_and_context_reach_gateway(monkeypatch,intent,message):
    gateway=GeminiGateway(SETTINGS)
    context={'last_request':{'analysis':{'kind':'month','month':2,'year':{'kind':'absolute','value':2024}}},
        'released_evidence':[{'index':0,'as_of':'2026-11-05','rows':1}]}
    def generate(prompt,schema,max_tokens):
        assert 'New/modified analysis needs the full revised request.' in prompt
        assert 'For unchanged-snapshot intents explain, why, summary and save, return a compact intent only.' in prompt
        assert 'Do not reproduce the snapshot, evidence, report text or history.' in prompt
        assert 'Date/metric/filter/status/report changes need the full request' in prompt
        raw=json.JSONDecoder().raw_decode(prompt.split('Example unchanged save JSON: ',1)[1])[0]
        assert InterpretationProposal.model_validate(raw).model_dump()==contextual('save').model_dump()
        assert raw['request']['queries']==[] and raw['request']['report'] is None
        assert json.dumps(context,separators=(',',':')) in prompt
        assert schema==_strict_schema(InterpretationProposal) and max_tokens==5000
        assert len(prompt)<12000
        return contextual(intent).model_dump(mode='json')
    monkeypatch.setattr(gateway,'_generate',generate)
    value=gateway.interpret(message,previous_plan=None,reference_date='2026-11-05',analytical_timezone='UTC',conversation_context=context)
    assert value==contextual(intent)


@pytest.mark.parametrize('change',['period','metric','report'])
def test_prompt_does_not_collapse_modified_requests(monkeypatch,change):
    request=RequestSpec(mode='report',analysis=TemporalExpression(kind='month',month=2,
        year=YearExpression(kind='absolute',value=2025)),queries=(QuerySpec(operation='aggregate',
        metric='order_count' if change=='metric' else 'revenue',revenue_statuses=('Complete',)),))
    value=InterpretationResult(status='ready',context_action='correct',
        temporal_updates=('analysis',) if change=='period' else (),request=request)
    gateway=GeminiGateway(SETTINGS)
    monkeypatch.setattr(gateway,'_generate',lambda *args:value.model_dump(mode='json'))
    assert gateway.interpret('Change '+change+' and save the revised report.',previous_plan=None,
        reference_date='2026-11-05',analytical_timezone='UTC')==value


def test_compact_save_completes_full_persisted_journey_with_original_evidence(tmp_path,monkeypatch):
    def request(year,month):return RequestSpec(analysis=TemporalExpression(kind='month',month=month,
        year=YearExpression(kind='absolute',value=year)),queries=(QuerySpec(operation='aggregate',metric='revenue',revenue_statuses=('Complete',)),))
    draft=ReportDraft(recommendations=(ActionSelection(kind='review_performance',finding_index=0),))
    replies=[InterpretationResult(status='ready',request=request(2025,1)),draft,
        contextual('explain'),contextual('why'),contextual('summary'),
        InterpretationResult(status='ready',context_action='correct',temporal_updates=('analysis',),request=request(2024,2)),
        draft,contextual('save')]
    calls=http_transport(monkeypatch,[r.model_dump_json() for r in replies])
    a=account(tmp_path)
    rows=runner.execute(a,{'scenarios':[case('journey')]},tmp_path,live=True,settings=SETTINGS)
    assert [r['status'] for r in rows]==['PASS']*6
    before=rows[4]['attempts'][0]['observed'];saved=rows[5]['attempts'][0]['observed']
    assert saved['saved'] and saved['report_completeness']=='complete'
    assert saved['resolved_periods']==before['resolved_periods']
    assert saved['factual_findings']==before['factual_findings']
    assert saved['evidence_job_ids']==before['evidence_job_ids']
    assert saved['analysis_queries']==0 and saved['query_executions']==1
    assert len(calls)==8 and all(c['json']['max_tokens']==5000 for c in calls if c['json']['max_tokens']!=2000)


def test_compact_save_without_snapshot_still_clarifies(tmp_path,monkeypatch):
    c=case('journey');c['turns']=[{**c['turns'][-1],'depends_on':[],'assertions':[]}]
    c['turns'][0].pop('compare_to',None)  # Isolated no-snapshot case has no preceding turn.
    http_transport(monkeypatch,[contextual('save').model_dump_json()])
    a=account(tmp_path)
    row,=runner.execute(a,{'scenarios':[c]},tmp_path,live=True,settings=SETTINGS)
    observed=row['attempts'][0]['observed']
    assert observed['status']=='needs_clarification' and not observed['saved']
    assert observed['analysis_queries']==0 and not observed['factual_findings']
