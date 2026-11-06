"""Deadline scope: contradictory save proposals clarify without storing old results."""
from datetime import date,timedelta
import json

import pytest

from opsfleet_assistant.contracts import InterpretationResult as I,RequestSpec as R,QuerySpec as Q,ReportSpec,TemporalExpression as T,YearExpression as Y
from opsfleet_assistant.store import SQLiteStore
from test_conversation_flows import harness,analysis,goal,year,OWNER,OracleTurn,NOW


@pytest.mark.parametrize('change',['year','month','metric','brands','statuses','grouping','products','report','actions','clear','mode'])
def test_modified_save_clarifies_without_analysis_or_persistence(harness,change):
    script,_,store,flow,_=harness
    script['Initial']=analysis()
    first=flow().run(OWNER,'Initial',conversation_id='guard',turn_id='first')
    assert first.status=='ok'
    before=store.load_continuity(OWNER,'guard')
    req=R(intent='save')
    flags={}
    if change=='year':req=req.model_copy(update={'analysis':year(2024)})
    elif change=='month':req=req.model_copy(update={'analysis':T(kind='month',month=2,year=Y(kind='absolute',value=2025))})
    elif change=='metric':req=req.model_copy(update={'queries':(Q(operation='aggregate',metric='order_count'),)})
    elif change=='brands':req=req.model_copy(update={'queries':(Q(operation='aggregate',metric='revenue',requested_brands=('Calvin Klein',)),)})
    elif change=='statuses':req=req.model_copy(update={'queries':(Q(operation='aggregate',metric='revenue',revenue_statuses=('Complete',)),)})
    elif change=='grouping':req=req.model_copy(update={'queries':(Q(operation='aggregate',metric='revenue',dimension='brand'),)})
    elif change=='products':req=req.model_copy(update={'queries':(Q(operation='aggregate',metric='revenue',product_ids=(1,)),)})
    elif change=='report':req=req.model_copy(update={'report':ReportSpec(purpose='comparison',detail='concise',requested_sections=('summary','comparison','limitations'))})
    elif change=='actions':req=req.model_copy(update={'action':year(2026)})
    elif change=='mode':req=R(intent='save',mode='temporal_compare')
    else:flags={'clear_fields':('report',)}
    script['Save changed']=I(status='ready',context_action='correct',request=req,**flags)
    result=flow().run(OWNER,'Save changed',conversation_id='guard',turn_id='changed')
    assert result.status=='needs_clarification' and result.report_id is None
    assert result.query_executions==1 and len(OracleTurn.queries)==1
    assert 'No report was saved.' in result.text and 'separately' in result.text
    assert store.list_reports(OWNER)==[]
    after=store.load_continuity(OWNER,'guard')
    assert after.snapshot==before.snapshot and after.expires_at==before.expires_at
    assert after.pending is None  # A bare confirmation cannot arm the rejected save.
    # An explicit exact save remains possible after clarification.
    script['Save unchanged']=goal('save')
    saved=flow().run(OWNER,'Save unchanged',conversation_id='guard',turn_id='exact')
    assert saved.status in {'ok','partial'} and saved.report_id is not None
    assert saved.factual_findings==first.factual_findings and len(OracleTurn.queries)==1


@pytest.mark.parametrize('variant',['compact','expanded','equivalent_dates','status_order'])
def test_matching_exact_save_still_works_across_restart_and_replay(harness,variant):
    script,_,store,flow,path=harness
    script['Initial']=analysis()
    first=flow().run(OWNER,'Initial',conversation_id='exact',turn_id='first')
    old=store.load_continuity(OWNER,'exact').snapshot.request.spec
    req=R(intent='save') if variant=='compact' else R.model_validate({**old.model_dump(),'intent':'save','mode':'report'})
    if variant=='equivalent_dates':req=req.model_copy(update={'analysis':T(kind='interval',start='2025-01-01',end='2026-01-01')})
    if variant=='status_order':req=req.model_copy(update={'queries':(old.queries[0].model_copy(update={'revenue_statuses':tuple(reversed(old.queries[0].revenue_statuses))}),)})
    script['Save']=I(status='ready',context_action='continue',request=req)
    store.close();current=SQLiteStore(path,clock=lambda:NOW)
    try:
        saved=flow(current).run(OWNER,'Save',conversation_id='exact',turn_id='save')
        replay=flow(current).run(OWNER,'Save',conversation_id='exact',turn_id='save')
        assert saved.status in {'ok','partial'} and saved.report_id is not None
        assert saved.factual_findings==first.factual_findings
        assert replay.reused_report and replay.text==saved.text
        assert len(OracleTurn.queries)==1 and len(current.list_reports(OWNER))==1
    finally:current.close()


def test_separate_year_correction_then_exact_save_is_supported(harness):
    script,_,store,flow,_=harness
    script['Initial']=analysis();script['2024 instead']=analysis(2024,action='correct');script['Save']=goal('save')
    flow().run(OWNER,'Initial',conversation_id='two-turn',turn_id='first')
    changed=flow().run(OWNER,'2024 instead',conversation_id='two-turn',turn_id='change')
    saved=flow().run(OWNER,'Save',conversation_id='two-turn',turn_id='save')
    assert saved.report_id and saved.resolved_periods['analysis']=={'start':'2024-01-01','end':'2025-01-01'}
    assert saved.factual_findings==changed.factual_findings and len(OracleTurn.queries)==2


def test_rejected_modification_does_not_change_existing_report(harness):
    script,_,store,flow,_=harness
    script['Initial']=analysis();script['Exact']=goal('save')
    flow().run(OWNER,'Initial',conversation_id='existing',turn_id='first')
    first=flow().run(OWNER,'Exact',conversation_id='existing',turn_id='save')
    original=store.report(OWNER,first.report_id)
    script['Modify']=I(status='ready',context_action='correct',request=R(intent='save',
        report=ReportSpec(presentation='table',detail='concise',requested_sections=('summary','limitations'))))
    result=flow().run(OWNER,'Modify',conversation_id='existing',turn_id='modify')
    assert result.status=='needs_clarification' and result.report_id is None
    assert store.report(OWNER,first.report_id)==original and len(store.list_reports(OWNER))==1
    assert len(OracleTurn.queries)==1


def test_v5_allows_only_safe_clarification_or_original_correct_execution():
    from copy import deepcopy
    from opsfleet_assistant.evaluation_contracts import assertions
    from test_post_full_remediation import case
    for name in ('novel-narrow-save','novel-zero-save'):
        rules=case(name)['turns'][-1]['assertions']
        safe={'status':'needs_clarification','compiled_queries':[],'evidence_count':0,
            'factual_findings':[],'saved':False,'report_id':None,'report_composition':None,
            'pending_request':None,'query_executions':1,'text':'No report was saved.'}
        assert all(r['passed'] for r in assertions(rules,safe))
        for change in ({'saved':True},{'report_id':1},{'status':'partial'},{'query_executions':3},
                       {'compiled_queries':[{}]},{'pending_request':{'intent':'save'}},{'status':'ok'}):
            assert not all(r['passed'] for r in assertions(rules,{**deepcopy(safe),**change}))
