"""Part 2 offline fixtures: injected interpretation, independent source rows."""
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from io import StringIO
from types import SimpleNamespace
import pytest
from rich.console import Console
from opsfleet_assistant.contracts import *
from opsfleet_assistant.compiler import compile_plan
from opsfleet_assistant.evidence import Coverage, release_evidence
from opsfleet_assistant.identity import Catalog, resolve_demo_scope, PolicyError
from opsfleet_assistant.reporting import ground_report
from opsfleet_assistant.report_artifact import reconstruct_grounded
from opsfleet_assistant.playbooks import investigate
from opsfleet_assistant.playbooks.base import Contribution
from opsfleet_assistant.store import SQLiteStore
from opsfleet_assistant.workflow import AssistantWorkflow
from opsfleet_assistant.presentation import render_result
from opsfleet_assistant.model_gateway import ModelFailure
from test_step4 import SETTINGS, FakeTurn

CATALOG=Catalog.from_products([{'id':1,'brand':'Calvin Klein'},{'id':2,'brand':'Carhartt'},{'id':3,'brand':'Hanes'}])
SCOPE=resolve_demo_scope('manager_apparel',CATALOG)
PERIOD=Period(start=date(2025,1,1),end=date(2025,4,1))

def product_plan(ids=(1,2)):
    return AnalysisPlan(version=2,operation='product_comparison',metric='revenue',product_ids=ids,period=PERIOD,
                        playbook=PlaybookChoice(id='product_revenue'))

def released(plan, rows, complete=True):
    query=compile_plan(plan,SCOPE,CATALOG)
    kinds={'product_id':'INT64','item_count':'INT64','order_count':'INT64','customers':'INT64',
           'eligible_customers':'INT64','inactive_customers':'INT64','demographic_value':'STRING',
           'cutoff_role':'STRING','segment_key':'STRING'}
    schema=[SimpleNamespace(name=k,field_type=kinds.get(k,'NUMERIC')) for k in query.result_contract.columns]
    private=plan.operation.value in {'regional_spend','churn_change'}
    item=release_evidence(query,rows,schema,job_id='offline_analysis',estimated_bytes=0,billed_bytes=0,
        query_as_of=datetime(2026,11,6,tzinfo=timezone.utc), coverage=Coverage(status='checked',
        source_rows=None if private else 1000,period_rows=None if private else 150 if rows else 0,
        job_id='offline_coverage',note='Synthetic independent fixture.'))
    return item if complete else item.model_copy(update={'complete':False})

def product_rows(ra='1000',na=100,rb='600',nb=50):
    return [dict(product_id=1,revenue=Decimal(ra),item_count=na,order_count=na,average_item_price=Decimal(ra)/na if na else Decimal(0)),
            dict(product_id=2,revenue=Decimal(rb),item_count=nb,order_count=nb,average_item_price=Decimal(rb)/nb if nb else Decimal(0))]

@pytest.mark.parametrize('ids,values,expected',[
    ((1,2),('1000',100,'600',50),('400','550','-150')),
    ((2,1),('1000',100,'600',50),('-400','-550','150')),
    ((1,2),('600',50,'600',50),('0','0','0')),
    ((1,2),('600',100,'600',50),('0','450','-450')),
])
def test_product_reference(ids,values,expected):
    driver=investigate((released(product_plan(ids),product_rows(*values)),))
    assert driver.complete
    assert (driver.total_difference,*(c.value for c in driver.contributions))==tuple(map(Decimal,expected))
    assert abs(driver.residual)<=driver.tolerance
    assert driver.actions and 'cause' in driver.conclusion
    if expected[0]=='0': assert all(c.percentage is None for c in driver.contributions)

def test_product_incomplete_zero_and_signed_validation():
    assert not investigate((released(product_plan(),product_rows(),False),)).complete
    assert investigate((released(product_plan(),product_rows(na=0)),)).availability_reason=='incomplete_evidence'
    assert Contribution(name='opposing',value=-150).value==-150
    with pytest.raises(ValueError): Contribution(name='bad',value='NaN')
    with pytest.raises(PolicyError): compile_plan(product_plan((1,3)),SCOPE,CATALOG)

class PlaybookGateway:
    spec=None
    fail_draft=False
    def __init__(self,settings,*,budget): self.budget=budget
    def interpret(self,question,**kwargs):
        self.budget.charge('offline_interpretation')
        return InterpretationResult(status='ready',context_action='continue' if question=='save' else 'new',
            request=RequestSpec(intent='save') if question=='save' else self.spec)
    def report(self,*args,**kwargs):
        self.budget.charge('offline_drafting')
        if self.fail_draft: raise ModelFailure('direct_failure')
        return ReportDraft()

class PlaybookTurn(FakeTurn):
    rows=product_rows()
    def execute_compiled(self,query):
        self.executions+=2
        self.billed_bytes+=20
        return released(AnalysisPlan.model_validate_json(query.semantic_plan),self.rows)

def spec(id='product_revenue', report=False, **kwargs):
    return RequestSpec(mode='report' if report else 'analysis',playbook=PlaybookChoice(id=id,**kwargs),
        analysis=TemporalExpression(kind='quarter',quarter=1,year=YearExpression(kind='absolute',value=2025)),
        queries=(QuerySpec(operation='product_comparison',metric='revenue',product_ids=(1,2)),))

def run_workflow(tmp_path,request,rows=None,fail_draft=False):
    class Gateway(PlaybookGateway): pass
    Gateway.spec=request; Gateway.fail_draft=fail_draft
    class Turn(PlaybookTurn): pass
    Turn.rows=rows if rows is not None else product_rows()
    store=SQLiteStore(tmp_path/'synthetic.sqlite')
    wf=AssistantWorkflow(SETTINGS,store,gateway_factory=Gateway,turn_factory=Turn,today=date(2026,11,6))
    return wf,store,wf.run('manager_apparel','offline investigation')

@pytest.mark.parametrize('fail_draft',[False,True])
def test_product_workflow_report_exact_save_restart(tmp_path,fail_draft):
    wf,store,result=run_workflow(tmp_path,spec(),fail_draft=fail_draft)
    assert result.status=='ok',result.text
    assert result.driver_finding['total_difference']=='400'
    assert result.driver_finding['actions']
    saved=wf.run('manager_apparel','save',conversation_id=result.conversation_id)
    assert saved.report_id and saved.report_completeness=='complete',saved.text
    restarted=wf.run('manager_apparel','save',conversation_id=result.conversation_id)
    assert restarted.report_id and restarted.driver_finding==saved.driver_finding
    assert restarted.text==saved.text
    for width in (32,110):
        output=StringIO();render_result(Console(file=output,width=width,force_terminal=False),result)
        assert '-150.00' in output.getvalue() and '$' not in output.getvalue()
    context=store.load_continuity('manager_apparel',result.conversation_id)
    assert reconstruct_grounded(context.snapshot)==context.snapshot.grounded

def regional_plan(values=('Texas','California')):
    return AnalysisPlan(version=2,operation='regional_spend',metric='average_customer_spend',dimension='state',
        demographic_values=values,limit=2,period=PERIOD,
        playbook=PlaybookChoice(id='regional_spend',population='qualifying_purchasers'))

def regional_spec(report=False,values=('Texas','California')):
    return spec(report=report).model_copy(update={'playbook':PlaybookChoice(id='regional_spend',population='qualifying_purchasers'),
        'queries':(QuerySpec(operation='regional_spend',metric='average_customer_spend',dimension='state',demographic_values=values,limit=2),)})

def regional_rows():
    return [dict(demographic_value='Texas',customers=100,order_count=200,revenue=Decimal(10000),average_customer_spend=Decimal(100)),
            dict(demographic_value='California',customers=50,order_count=50,revenue=Decimal(4000),average_customer_spend=Decimal(80))]

@pytest.mark.parametrize('values,expected',[(('Texas','California'),('20','65','-45')),(('California','Texas'),('-20','-65','45'))])
def test_regional_reference(values,expected):
    driver=investigate((released(regional_plan(values),regional_rows()),))
    assert driver.complete
    assert (driver.total_difference,*(c.value for c in driver.contributions))==tuple(map(Decimal,expected))
    assert 'revenue difference' in driver.conclusion and 'denominator' in driver.conclusion

@pytest.mark.parametrize('rows',[[],regional_rows()[:1]])
def test_regional_partial_no_hidden_arithmetic(tmp_path,rows):
    e=released(regional_plan(),rows)
    assert not e.complete and e.coverage.period_rows is None
    wf,store,result=run_workflow(tmp_path,regional_spec(report=True),rows)
    assert result.status=='partial',result.text
    assert result.driver_finding['total_difference'] is None
    assert not result.driver_finding['operands'] and not result.driver_finding['contributions']
    assert 'withheld' in result.text

@pytest.mark.parametrize('id',['product_revenue','regional_spend'])
def test_report_path(tmp_path,id):
    request=spec(report=True) if id=='product_revenue' else regional_spec(report=True)
    rows=product_rows() if id=='product_revenue' else regional_rows()
    wf,store,result=run_workflow(tmp_path,request,rows)
    assert result.report_id and result.report_completeness=='complete',result.text
    assert reconstruct_grounded(store.load_continuity('manager_apparel',result.conversation_id).snapshot).driver.complete

def churn_plan():
    return AnalysisPlan(version=2,operation='churn_change',metric='churn_rate',cutoff_date=date(2025,4,1),
        baseline_cutoff_date=date(2025,1,1),playbook=PlaybookChoice(id='churn_change',population='qualifying_purchasers',segment='country'))

def churn_spec(report=False):
    return spec(report=report).model_copy(update={'playbook':churn_plan().playbook,
        'comparison':TemporalExpression(kind='quarter',quarter=4,year=YearExpression(kind='absolute',value=2024)),
        'queries':(QuerySpec(operation='churn_change',metric='churn_rate'),)})

def churn_rows():
    return [dict(cutoff_role=role,segment_key=key,eligible_customers=c,inactive_customers=n,churn_rate=Decimal(n)/c)
        for role,key,c,n in [('current','H:',1000,250),('baseline','H:',1000,140),
            ('current','C:A',500,100),('baseline','C:A',600,60),
            ('current','C:B',500,150),('baseline','C:B',400,80)]]

def test_churn_reference():
    d=investigate((released(churn_plan(),churn_rows()),))
    assert d.complete
    assert (d.total_difference,*(c.value for c in d.contributions))==tuple(map(Decimal,('.11','.01','.10')))
    assert 'rose' in d.conclusion and 'newly churned' in d.conclusion

@pytest.mark.parametrize('partial',[False,True])
def test_churn_workflow_report_and_exact_save(tmp_path,partial):
    rows=churn_rows()[:2] if partial else churn_rows()
    wf,store,result=run_workflow(tmp_path,churn_spec(report=True),rows,fail_draft=True)
    assert result.report_id,result.text
    assert result.report_completeness==('partial' if partial else 'complete'),result.text
    assert result.driver_finding['complete'] is not partial
    saved=wf.run('manager_apparel','save',conversation_id=result.conversation_id)
    assert saved.report_id and saved.driver_finding==result.driver_finding,saved.text

@pytest.mark.parametrize('factory,rows',[(spec,product_rows),(regional_spec,regional_rows),(churn_spec,churn_rows)])
def test_restart_artifact_replay_and_semantic_tampering(tmp_path,factory,rows):
    from opsfleet_assistant.report_artifact import artifact_binding_digest
    wf,store,result=run_workflow(tmp_path,factory(report=True),rows())
    assert result.report_id,result.text
    gateway_factory,turn_factory=wf.gateway_factory,wf.turn_factory
    store.close()
    current=SQLiteStore(tmp_path/'synthetic.sqlite')
    restarted=AssistantWorkflow(SETTINGS,current,gateway_factory=gateway_factory,turn_factory=turn_factory,today=date(2026,11,6))
    replay=restarted.run('manager_apparel','offline investigation',conversation_id=result.conversation_id,turn_id=result.turn_id)
    assert replay.reused_report and replay.text==result.text and replay.driver_finding==result.driver_finding,replay.text
    assert replay.model_requests==0 and replay.query_executions==1
    fields=current.report('manager_apparel',result.report_id)
    artifact=json.loads(fields['report_support_json'])
    artifact['snapshot']['grounded']['driver']['contributions'][0]['value']='123456'
    fields['report_support_json']=json.dumps(artifact)
    # Independently recomputed checksum cannot bless a changed contribution.
    digest=artifact_binding_digest(fields)
    current.db.execute('UPDATE reports SET report_support_json=?,report_binding_digest=? WHERE id=?',
        (fields['report_support_json'],digest,result.report_id));current.db.commit()
    rejected=restarted.run('manager_apparel','offline investigation',conversation_id=result.conversation_id,turn_id=result.turn_id)
    assert rejected.status=='error' and rejected.report_id is None
    assert '123456' not in rejected.text
    assert current.report('manager_casual',result.report_id) is None

@pytest.mark.parametrize('factory,rows',[(spec,product_rows),(regional_spec,regional_rows),(churn_spec,churn_rows)])
def test_all_playbooks_modified_save_expiry_and_drafting_fallback(tmp_path,factory,rows):
    from datetime import timedelta
    wf,store,result=run_workflow(tmp_path,factory(),rows(),fail_draft=True)
    assert result.driver_finding['complete'] and result.driver_finding['actions'],result.text
    # Full save with omitted playbook must not turn into an unchanged save.
    changed=factory().model_copy(update={'intent':'save','playbook':None})
    class ChangedGateway(PlaybookGateway):
        def interpret(self,*a,**kw):
            self.budget.charge('offline_interpretation')
            return InterpretationResult(status='ready',context_action='continue',request=changed)
    wf.gateway_factory=ChangedGateway
    refused=wf.run('manager_apparel','modified save',conversation_id=result.conversation_id)
    assert refused.status=='needs_clarification' and refused.report_id is None,refused.text
    wf.gateway_factory=PlaybookGateway
    context=store.load_continuity('manager_apparel',result.conversation_id)
    store._now=lambda:context.expires_at+timedelta(seconds=1)
    expired=wf.run('manager_apparel','save',conversation_id=result.conversation_id)
    assert expired.status=='needs_clarification' and expired.report_id is None

@pytest.mark.parametrize('operation',['product','regional','churn'])
def test_ordinary_routes_do_not_gain_playbook_methods(operation):
    from opsfleet_assistant.intent import analytical_plans
    from opsfleet_assistant.periods import resolve_request
    if operation=='product':request=spec().model_copy(update={'playbook':None})
    elif operation=='regional':
        request=regional_spec().model_copy(update={'playbook':None,'queries':(QuerySpec(operation='demographic_comparison',metric='average_customer_spend',dimension='state',demographic_values=('Texas','California'),limit=2),)})
    else:
        request=churn_spec().model_copy(update={'playbook':None,'comparison':None,'queries':(QuerySpec(operation='churn_snapshot',metric='churn_rate'),)})
    plans=analytical_plans(resolve_request(request,reference_date=date(2026,11,6),analytical_timezone='UTC'))
    assert all(p.playbook is None and p.version==1 for p in plans)

def test_missing_product_and_purchaser_choices_clarify():
    from opsfleet_assistant.intent import resolve_interpretation
    requests=[spec().model_copy(update={'queries':(QuerySpec(operation='product_comparison',metric='revenue'),)}),
              regional_spec().model_copy(update={'playbook':PlaybookChoice(id='regional_spend')})]
    for request in requests:
        result,resolved=resolve_interpretation(InterpretationResult(status='ready',request=request),reference_date=date(2026,11,6),analytical_timezone='UTC')
        assert result.status=='needs_clarification' and resolved is None

@pytest.mark.parametrize('which',['equal','offsetting'])
def test_regional_zero_net_components(which):
    rows=regional_rows()
    if which=='equal':
        rows[1].update(customers=100,order_count=200,revenue=Decimal(10000),average_customer_spend=Decimal(100))
    else:rows[1].update(revenue=Decimal(5000),average_customer_spend=Decimal(100))
    d=investigate((released(regional_plan(),rows),))
    assert d.complete and d.total_difference==0 and all(c.percentage is None for c in d.contributions)
    assert [c.value for c in d.contributions]==([Decimal(0),Decimal(0)] if which=='equal' else [Decimal(75),Decimal(-75)])

@pytest.mark.parametrize('mode',['equal','falling'])
def test_churn_rejects_false_spike(mode):
    rows=churn_rows()
    # Swap the complete released populations to reverse direction, or duplicate baseline.
    for row in rows:
        if mode=='falling':row['cutoff_role']='baseline' if row['cutoff_role']=='current' else 'current'
    if mode=='equal':
        rows=[r for r in rows if r['cutoff_role']=='baseline']
        rows += [{**r,'cutoff_role':'current'} for r in rows]
    d=investigate((released(churn_plan(),rows),))
    assert d.complete and d.total_difference<=0 and 'spike is not supported' in d.conclusion
    if mode=='falling':assert [c.value for c in d.contributions]==[Decimal('-.01'),Decimal('-.10')]

@pytest.mark.parametrize('rows',[[],product_rows()[:1]])
def test_missing_products_release_partial_without_zero(tmp_path,rows):
    wf,store,result=run_workflow(tmp_path,spec(report=True),rows)
    assert result.report_completeness=='partial' and not result.driver_finding['complete'],result.text
    assert result.driver_finding['total_difference'] is None
    assert len(result.presentation['evidence'][0]['rows'])==len(rows)

@pytest.mark.parametrize('factory,rows',[(spec,product_rows),(regional_spec,regional_rows),(churn_spec,churn_rows)])
def test_literal_markup_facts_only_and_json(tmp_path,factory,rows):
    request=factory(report=True).model_copy(update={'report':ReportSpec(requested_sections=('summary','performance_table','limitations'))})
    wf,store,result=run_workflow(tmp_path,request,rows())
    assert result.report_id and not result.driver_finding['actions'],result.text
    assert reconstruct_grounded(store.load_continuity('manager_apparel',result.conversation_id).snapshot).driver.actions==()
    from dataclasses import replace,asdict
    from opsfleet_assistant.cli import _show
    literal='[red]literal markup[/red]'
    result=replace(result,presentation={**result.presentation,'headline':literal})
    for width in (28,100):
        output=StringIO();render_result(Console(file=output,width=width,force_terminal=False),result)
        assert '[red]' in output.getvalue() and '[/red]' in output.getvalue()
    assert json.loads(json.dumps(asdict(result)))['driver_finding']['method_version']==1

@pytest.mark.parametrize('factory,rows',[(spec,product_rows),(regional_spec,regional_rows),(churn_spec,churn_rows)])
def test_gateway_injected_paraphrases_and_full_correction(tmp_path,monkeypatch,factory,rows):
    from opsfleet_assistant.model_gateway import GeminiGateway
    from opsfleet_assistant.intent import analytical_plans
    from opsfleet_assistant.periods import resolve_request
    request=factory()
    prompts=[]
    # These are substituted interpretation responses, not real-model accuracy.
    for phrase in ('Explain the measured difference in Q1 2025','Investigate the components of that comparison'):
        gateway=GeminiGateway(SETTINGS)
        def generate(prompt,*args):
            prompts.append(prompt)
            return InterpretationResult(status='ready',request=request).model_dump(mode='json')
        monkeypatch.setattr(gateway,'_generate',generate)
        parsed=gateway.interpret(phrase,previous_plan=None,reference_date='2026-11-06',analytical_timezone='UTC')
        assert parsed.request.playbook==request.playbook
        assert phrase in prompts[-1] and 'qualifying_purchasers' in prompts[-1]
    wf,store,result=run_workflow(tmp_path,request,rows())
    # A supported direction correction preserves period, status and population.
    queries=list(request.queries)
    if request.playbook.id=='product_revenue':queries[0]=queries[0].model_copy(update={'product_ids':(2,1)})
    elif request.playbook.id=='regional_spend':queries[0]=queries[0].model_copy(update={'demographic_values':('California','Texas')})
    else:queries[0]=queries[0].model_copy(update={'requested_brands':('Calvin Klein',)})
    revised=request.model_copy(update={'queries':tuple(queries)})
    class Correction(PlaybookGateway):
        def interpret(self,*a,**kw):
            self.budget.charge('offline_interpretation')
            return InterpretationResult(status='ready',context_action='correct',request=revised)
    wf.gateway_factory=Correction
    corrected=wf.run('manager_apparel','Typed correction',conversation_id=result.conversation_id)
    assert corrected.status=='ok',corrected.text
    snapshot=store.load_continuity('manager_apparel',result.conversation_id).snapshot
    assert snapshot.request.spec.playbook==request.playbook
    assert snapshot.request.spec.analysis==request.analysis
    assert snapshot.plans[0].revenue_statuses==DEFAULT_STATUSES

@pytest.mark.parametrize('factory,rows',[(spec,product_rows),(regional_spec,regional_rows),(churn_spec,churn_rows)])
def test_reauthorization_revocation_before_artifact_release(tmp_path,monkeypatch,factory,rows):
    from opsfleet_assistant import workflow as module
    wf,store,result=run_workflow(tmp_path,factory(report=True),rows())
    original=module.resolve_demo_scope
    monkeypatch.setattr(module,'resolve_demo_scope',lambda owner,catalog:original(owner,catalog).model_copy(update={'authorization_version':999}))
    revoked=wf.run('manager_apparel','offline investigation',conversation_id=result.conversation_id,turn_id=result.turn_id)
    assert revoked.status=='error' and revoked.report_id is None and revoked.driver_finding is None
    assert revoked.presentation is None and revoked.factual_findings==()

def test_v1_artifact_v2_snapshot_compatibility_without_new_defaults(tmp_path):
    from test_step4 import FakeGateway,FakeTurn
    from opsfleet_assistant.report_artifact import artifact_binding_digest
    store=SQLiteStore(tmp_path/'legacy.sqlite')
    wf=AssistantWorkflow(SETTINGS,store,gateway_factory=FakeGateway,turn_factory=FakeTurn,today=date(2026,11,6))
    result=wf.run('manager_apparel','Q1 report')
    assert result.report_id,result.text
    fields=store.report('manager_apparel',result.report_id)
    support=json.loads(fields['report_support_json'])
    assert support['schema_version']==1 and support['snapshot']['schema_version']==2
    support['snapshot']['grounded'].pop('driver',None)
    support['snapshot']['request']['spec'].pop('playbook',None)
    for plan in support['snapshot']['plans']:
        plan.pop('playbook',None);plan.pop('baseline_cutoff_date',None)
    plans=json.loads(fields['plans_json'])
    for plan in plans:plan.pop('playbook',None);plan.pop('baseline_cutoff_date',None)
    grounded=json.loads(fields['grounded_report_json']);grounded.pop('driver',None)
    composition=json.loads(fields['report_composition_json']);composition['request']['spec'].pop('playbook',None)
    for key,value in [('report_support_json',support),('plans_json',plans),('grounded_report_json',grounded),('report_composition_json',composition)]:fields[key]=json.dumps(value)
    fields['report_binding_digest']=artifact_binding_digest(fields)
    store.db.execute('UPDATE reports SET report_support_json=?,plans_json=?,grounded_report_json=?,report_composition_json=?,report_binding_digest=? WHERE id=?',
        (*(fields[k] for k in ('report_support_json','plans_json','grounded_report_json','report_composition_json','report_binding_digest')),result.report_id));store.db.commit()
    replay=wf.run('manager_apparel','Q1 report',conversation_id=result.conversation_id,turn_id=result.turn_id)
    assert replay.reused_report and replay.text==result.text and replay.driver_finding is None,replay.text


def test_churn_zero_rate_change_with_opposing_components():
    rows=churn_rows()
    for row in rows:
        if row['cutoff_role']=='current':
            row['inactive_customers']={'H:':140,'C:A':40,'C:B':100}[row['segment_key']]
            row['churn_rate']=Decimal(row['inactive_customers'])/row['eligible_customers']
    d=investigate((released(churn_plan(),rows),))
    assert d.complete and d.total_difference==0
    assert [c.value for c in d.contributions]==[Decimal('.011'),Decimal('-.011')]
    assert all(c.percentage is None for c in d.contributions)
    assert 'spike is not supported' in d.conclusion

@pytest.mark.parametrize('fault',['unmatched','inconsistent_total','out_of_range_rate','negative_count'])
def test_churn_rejects_incompatible_released_segment_shapes(fault):
    rows=churn_rows()
    if fault=='unmatched':rows[-1]['segment_key']='C:changed'
    elif fault=='inconsistent_total':rows[0]['eligible_customers']=1001
    elif fault=='out_of_range_rate':rows[0]['churn_rate']=Decimal('1.1')
    else:rows[0]['inactive_customers']=-1
    with pytest.raises(PolicyError):released(churn_plan(),rows)
