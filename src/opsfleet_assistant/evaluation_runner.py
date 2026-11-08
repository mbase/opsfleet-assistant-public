"""Reviewed, opt-in evaluation driver. Default dry mode has no external effects."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import date, datetime
import fcntl
from functools import partial
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from . import bigquery_adapter, identity, workflow, request_resolver, analysis_executor
from .bigquery_adapter import BigQueryTurn, ExecutionError, _client_from_adc, _classify
from .config import ROOT, Settings
from .evaluation_contracts import (Accounting, MAX_CAPS, Redactor, StopScheduling,
    assertions, atomic_json, digest, totals, verify_source)
from .evaluation_fixtures import FixtureClient
from .model_gateway import GeminiGateway, ModelFailure, response_details, OUTPUT_FAILURE_CODES
from .store import SQLiteStore
from .telemetry import TurnTrace

MANIFEST = ROOT/'docs/remediation/SCENARIO_MANIFEST.json'
SOURCE = ROOT/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json'
OWNER_SESSION = '01a1aade-98c8-71a3-8fd4-40200ad6bcb7'

WAREHOUSE_ENVIRONMENT_REASONS = frozenset({
    'missing_project','credentials_unavailable','permission_denied','provider_transient',
    'provider_failure','billing_unknown','timeout_cancellation_requested',
})


PLAN_FAILURE_CODES = frozenset({'invalid_plan_contract', 'plan_intent_mismatch', 'plan_period_mismatch', 'plan_limit_mismatch', 'plan_investigation_mismatch'})

def safe_failure(boundary, stage, reason):
    allowed = workflow.SAFE_ERROR_CODES | {'credentials_unavailable'}
    return {'boundary':boundary,'stage':stage,
            'reason':reason if reason in allowed else 'internal_failure'}


def outcome_status(checks, observed, events, stop):
    if any(e.get('diagnostics', {}).get('failure_kind') == 'dns_resolution' for e in events):
        return 'BLOCKED_ENVIRONMENT'
    # Failed model dispatches retain their provenance even when another stage
    # also fails or deterministic checked evidence is useful.
    if any(e['kind']=='provider' and e['outcome']=='failed' and
           e.get('diagnostics', {}).get('failure_code') not in OUTPUT_FAILURE_CODES for e in events):
        return 'PROVIDER_FAILURE'
    failure = observed.get('terminal_error') or {}
    if observed.get('error_category') in {'internal','syntax','schema'}:
        return 'FAIL'
    if observed.get('budget_denials'):
        return 'FAIL'
    if (failure.get('boundary')=='warehouse' and failure.get('reason') in WAREHOUSE_ENVIRONMENT_REASONS
            or failure.get('boundary')=='model' and failure.get('reason')=='backup_unconfigured'):
        return 'BLOCKED_ENVIRONMENT'
    if any(e['kind']=='provider' and e['outcome']=='failed' for e in events):
        return 'FAIL'
    if stop or failure:
        return 'FAIL'
    return 'PASS' if all(c['passed'] for c in checks) else 'FAIL'


class ProviderObserver:
    def __init__(self, accounting): self.a=accounting
    def begin(self, model):
        return self.a.reserve('provider',route='openrouter_gemini',requested_model=model,
                              actual_model=None,usage=None,cost=None)
    def result(self, event, payload):
        usage=payload.get('usage') if isinstance(payload,dict) else None
        numeric={}
        if isinstance(usage,dict):
            for key in ('prompt_tokens','completion_tokens','total_tokens','cost'):
                value=usage.get(key)
                if type(value) in (int,float) and value>=0 and value < 10**12:
                    import math
                    if math.isfinite(value): numeric[key]=value
        model=payload.get('model') if isinstance(payload,dict) else None
        model=model if isinstance(model,str) and re.fullmatch(r'[A-Za-z0-9_./:@+-]{1,160}',model) else None
        if model is not None and model != event['requested_model']:
            self.a.state['stop']='provider_reported_model_mismatch'
        self.a.finish(event,outcome='response_received',actual_model=model,
                      usage=numeric or None,cost=numeric.get('cost'),response_metadata=response_details(payload))
        if self.a.state['stop']=='provider_reported_model_mismatch':
            raise StopScheduling('provider_reported_model_mismatch')
    def failed(self,event,diagnostics=None):
        # Keep a received response's model/usage even if contract parsing fails.
        details = {'diagnostics':{**event.get('diagnostics',{}),**diagnostics}} if diagnostics else {}
        if diagnostics and diagnostics.get('failure_kind') == 'dns_resolution':
            self.a.state['stop'] = self.a.state['stop'] or 'provider_dns_resolution'
        self.a.finish(event,outcome='failed',**details)


class GuardedJob:
    def __init__(self, job, event, accounting):
        self.job,self.event,self.a=job,event,accounting
    def __getattr__(self,name): return getattr(self.job,name)
    def result(self,**kwargs):
        try: return self.job.result(**kwargs)
        finally:
            job_id=getattr(self.job,'job_id',None)
            self.a.finish(self.event,billed=getattr(self.job,'total_bytes_billed',None),outcome='completed_or_failed',
                job_id=job_id if isinstance(job_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}',job_id) else None)


class GuardedClient:
    """Intercept every actual BigQuery dispatch, including catalog and coverage."""
    def __init__(self, client, accounting, *, snapshot_timestamp=None):
        self.client,self.a=client,accounting
        self.snapshot_timestamp = snapshot_timestamp
    def query(self,sql,*,job_config,**kwargs):
        if self.snapshot_timestamp is not None:
            from .evaluation_part5 import snapshot_sql
            sql = snapshot_sql(sql, self.snapshot_timestamp)
        self.a.check()
        left=self.a.state['caps']['active_seconds']-self.a.state['used']['active_seconds']
        kwargs['timeout']=min(kwargs.get('timeout',30),left)
        if job_config.dry_run:
            # Dry runs have no billed execution, but do consume active time and
            # adapter dry-run limits. Retain counts independently of job counts.
            self.a.state['dry_runs']=self.a.state.get('dry_runs',0)+1; self.a.save()
            return self.client.query(sql,job_config=job_config,**kwargs)
        job_timeout=min(int(job_config.job_timeout_ms or 30000),max(1,int(left*1000)))
        event=self.a.reserve('warehouse',cap=int(job_config.maximum_bytes_billed),
                             purpose=(job_config.labels or {}).get('purpose','reference'))
        job_config.maximum_bytes_billed=event['cap']
        job_config.job_timeout_ms=job_timeout
        try: job=self.client.query(sql,job_config=job_config,**kwargs)
        except Exception:
            self.a.finish(event,billed=None,outcome='submission_failed_unknown'); raise
        return GuardedJob(job,event,self.a)


def validate_manifest(manifest):
    if manifest['schema_version']!=1 or manifest['timezone']!='UTC': raise ValueError('invalid_manifest')
    seen=set(); novel=set()
    for case in manifest['scenarios']:
        if case['id'] in seen or case['mode'] not in {'live_bigquery','live_fixture','controlled_regression'}:
            raise ValueError('invalid_scenario')
        if case.get('novel_combination'): novel.add(case['novel_combination'])
        for turn in case['turns']:
            if turn['id'] in seen or not turn['assertions']: raise ValueError('invalid_turn')
            if any(d not in seen for d in turn['depends_on']): raise ValueError('forward_dependency')
            if case['mode']!='controlled_regression':
                if turn.get('semantic_contract',{}).get('kind') not in {
                        'execution','reuse','clarification','refusal','late_slot','empty_comparison','bounded_modified_save'}:
                    raise ValueError('material_semantic_contract_missing')
                if turn.get('reference_scalar') and turn.get('reference_expected') != turn['semantic_contract']['queries'][0]:
                    raise ValueError('independent_reference_contract_mismatch')
            for rule in turn['assertions']:
                if rule['op'] not in {'eq','in','contains','absent','max','min','decimal','finding','set_eq'}:
                    raise ValueError('invalid_assertion')
            date.fromisoformat(turn['analytical_date']); dt=datetime.fromisoformat(turn['lifecycle_clock'])
            if dt.utcoffset().total_seconds()!=0: raise ValueError('invalid_clock')
            seen.add(turn['id'])
        seen.add(case['id'])
    if len(novel)<10: raise ValueError('novelty_coverage_missing')
    return manifest


def acceptance(root,path,source,scenario):
    value=json.loads(Path(path).read_text())
    if (value['verdict'] not in {'ACCEPTED','ACCEPTED_WITH_VERIFICATION_LIMITS'} or value['blocking_findings'] or
        value['source_revision']!=source or value['scenario_sha256']!=scenario or
        not value['reviewer_identity'] or value['reviewer_identity']==OWNER_SESSION):
        raise ValueError('independent_acceptance_required')
    artifact=(root/value['review_artifact']).resolve()
    if not artifact.is_relative_to(root.resolve()) or digest(artifact)!=value['review_sha256']:
        raise ValueError('review_artifact_mismatch')
    return value


def safe_private(path):
    resolved=Path(path).resolve()
    roots=(Path('/private/tmp').resolve(),Path(tempfile.gettempdir()).resolve())
    if not any(resolved.is_relative_to(r) and resolved!=r for r in roots) or resolved.is_relative_to(ROOT):
        raise ValueError('temporary_private_directory_required')
    if resolved.is_symlink() or not (resolved/'evaluation-private-marker').is_file():
        raise ValueError('invalid_private_state')
    os.chmod(resolved,0o700)
    return resolved


def observe_result(result, captured, proposals):
    value=asdict(result)
    match=re.fullmatch(r'Unable to complete this request: ([a-z_]+)\.',result.text)
    value['safe_error_code']=match.group(1) if match and match.group(1) in workflow.SAFE_ERROR_CODES else None
    value['compiled_queries']=captured
    value['interpretations']=proposals
    first = proposals[0].get('request') or {} if proposals else {}
    playbook = first.get('playbook') or {}
    value['interpretation_contract'] = {k: playbook.get(k) for k in ('id', 'population', 'segment')}
    value['analysis_queries']=len(captured)
    value['saved']=result.report_id is not None
    composition=result.report_composition or {}
    value['delivered_report_sections']=[s['kind'] for s in composition.get('sections',[])]
    value['supported_report_sections']=[s['kind'] for s in composition.get('sections',[]) if s['status']=='supported']
    evidence=(result.presentation or {}).get('evidence',[])
    value['evidence_count']=len(evidence)
    value['evidence_semantics']=[{
        'operation':e['operation'], 'metric':e['metric_definition']['metric'],
        'period':{'start':e['period_utc'].get('period_start'),'end':e['period_utc'].get('period_end')},
        'columns':e['columns'], 'brands':e['execution_scope']['brands'],
        'products':e['execution_scope']['product_ids'],
        'status_set':sorted(e['metric_definition']['revenue_statuses']),
        'group_ids':[r['product_id'] for r in e['rows'] if 'product_id' in r],
        'measured_groups':[r['product_id'] for r in e['rows'] if 'product_id' in r and r.get('item_count',1)>0],
    } for e in evidence]
    from decimal import Decimal, localcontext
    consistent=True
    for f in result.factual_findings:
        op=f['selection']['operation']; a=f.get('current_value'); b=f.get('baseline_value')
        if op in {'difference','percentage_change','percentage_point_change'}:
            with localcontext() as ctx:
                ctx.prec=100
                target=None if a is None or b is None else Decimal(a)-Decimal(b)
                if op=='percentage_change': target=None if target is None or Decimal(b)==0 else target/Decimal(b)*100
                elif op=='percentage_point_change' and target is not None: target*=100
                consistent &= (f['value'] is None if target is None else f['value'] is not None and abs(Decimal(f['value'])-target)<=Decimal('0.000000000001'))
    value['arithmetic_consistent']=bool(consistent)
    value['usage']=None; value['cost']=None
    return json.loads(json.dumps(value))


def saved_report_integrity(result, store, owner):
    """Read only this scenario's isolated saved row; validate the public persistence claim."""
    if result.report_id is None or result.report_completeness not in {'complete','partial'}:
        return False
    stored=store.report(owner,result.report_id)
    if stored is None: return False
    try:
        composition=json.loads(stored['report_composition_json'])
        grounded=json.loads(stored['grounded_report_json'])
        suggestions=[{'kind':kind,**item} for kind in ('recommendations','hypotheses')
                     for item in grounded[kind]]
        prefix=result.report_completeness.capitalize()+' report:'
        return (stored['content']==result.text and stored['content'].startswith(prefix)
            and store._title(stored['content']).startswith(prefix)
            and stored['report_completeness']==result.report_completeness
            and composition==result.report_composition
            and composition['completeness']==result.report_completeness
            and grounded['findings']==list(result.factual_findings)
            and suggestions==list(result.suggestions)
            and result.status==('partial' if result.report_completeness=='partial' else 'ok'))
    except (KeyError,TypeError,ValueError): return False


class DataPreconditionError(Exception):
    def __init__(self, precondition):
        self.precondition = precondition
        super().__init__('data_precondition_unmet')


def discover_products(adapter, catalog, scope, expected):
    """Bind measurable catalog products using authored inputs and the same guard/budget."""
    from .compiler import BoundParameter, DATASET
    brands = tuple(expected['brands'])
    if not set(brands).issubset(scope.brands):
        raise ExecutionError('unauthorized_scope')
    interval = expected['period']
    params = (BoundParameter('brands','ARRAY<STRING>',brands),
        BoundParameter('statuses','ARRAY<STRING>',tuple(expected['status_set'])),
        BoundParameter('start_ts','TIMESTAMP',datetime.fromisoformat(interval['start']+'T00:00:00+00:00')),
        BoundParameter('end_ts','TIMESTAMP',datetime.fromisoformat(interval['end']+'T00:00:00+00:00')))
    sql = f"""SELECT p.id AS product_id, COUNT(*) AS item_count
        FROM `{DATASET}.order_items` i JOIN `{DATASET}.products` p ON p.id=i.product_id
        WHERE p.brand IN UNNEST(@brands) AND i.status IN UNNEST(@statuses)
          AND i.sale_price IS NOT NULL AND i.created_at >= @start_ts AND i.created_at < @end_ts
        GROUP BY p.id ORDER BY p.id LIMIT 2"""
    found = adapter._run(sql,params,max_rows=2,purpose='product_discovery')
    ids = [r.get('product_id') for r in found.rows]
    valid = (len(ids)==2 and len(set(ids))==2 and all(type(i) is int and
        catalog.product_brands.get(i) in brands for i in ids) and
        all(type(r.get('item_count')) is int and r['item_count']>0 for r in found.rows))
    precondition = {'status':'met' if valid else 'unmet','required_products':2,
        'qualifying_products':len(ids),'period':interval,'statuses':expected['status_set'],
        'brands':list(brands),'non_null_price':True,'catalog_checked':True,
        'job_id':found.job_id,'as_of':found.as_of.isoformat()}
    if not valid: raise DataPreconditionError(precondition)
    return dict(zip(('product_1','product_2'),ids)), precondition


def run_workflow(case,turn,private,account,settings,records):
    turn_started=time.monotonic()
    directory=private/case['id']; directory.mkdir(mode=0o700,exist_ok=True)
    synthetic=directory/'config'; synthetic.mkdir(exist_ok=True)
    users=ROOT/'config/demo_users.json'
    # Copy only nonsecret identity configuration. This is a disposable synthetic
    # identity file; its trusted resolver and fingerprint checks remain active.
    if not (synthetic/'demo_users.json').exists(): (synthetic/'demo_users.json').write_bytes(users.read_bytes())
    captured=[]; proposals=[]; failures=[]; interpretation_attempts=[]; plan_failures=[]
    bindings_path=directory/'catalog-bindings.json'
    catalog_bindings=json.loads(bindings_path.read_text()) if bindings_path.exists() else {}
    precondition_path=directory/'data-precondition.json'
    data_precondition=json.loads(precondition_path.read_text()) if precondition_path.exists() else None
    observer=ProviderObserver(account)
    class EvaluationGateway(GeminiGateway):
        def __init__(self,s,*,budget):
            account.check()
            budget.seconds=min(budget.seconds, max(0.001,120-(time.monotonic()-turn_started)), account.state['caps']['active_seconds']-account.state['used']['active_seconds'])
            super().__init__(s,budget=budget,provider_mode='openrouter_only',observer=observer)
        def _failed_contract(self):
            events=[e for e in account.state['events'] if e['turn']==account.state['active_turn'] and e['kind']=='provider']
            if events: observer.failed(events[-1])
        def interpret(self,*args,**kwargs):
            interpretation_attempts.append("failed")
            try: value=super().interpret(*args,**kwargs)
            except ModelFailure as exc:
                failures.append(safe_failure('model','interpretation',exc.code))
                self._failed_contract(); raise
            interpretation_attempts[-1] = value.status
            proposals.append(value.model_dump(mode='json',exclude_none=True))
            return value
        def report(self,*args,**kwargs):
            try: return super().report(*args,**kwargs)
            except ModelFailure as exc:
                failures.append(safe_failure('model','report',exc.code))
                self._failed_contract(); raise
    clients=[]; adapters=[]
    class EvaluationTurn(BigQueryTurn):
        def __init__(self,s,*,limits):
            account.check()
            left=account.state['caps']['active_seconds']-account.state['used']['active_seconds']
            limits=replace(limits,turn_seconds=min(limits.turn_seconds,left))
            if case['mode']=='live_fixture':
                s=replace(s,project=s.project or 'evaluation-fixture-only')
                client=FixtureClient(case.get('fixture','standard'), snapshot=case.get('fixture_snapshot'))
            else:
                if not s.project:
                    failures.append(safe_failure('warehouse','configuration','missing_project'))
                    raise ExecutionError('missing_project')
                try:
                    client=GuardedClient(_client_from_adc(s),account,
                                         snapshot_timestamp=case.get('snapshot_timestamp'))
                except Exception as exc:
                    from google.auth.exceptions import DefaultCredentialsError
                    reason='credentials_unavailable' if isinstance(exc,DefaultCredentialsError) else _classify(exc).category
                    failure=safe_failure('warehouse','configuration',reason)
                    failures.append(failure)
                    error=ExecutionError('provider_failure','provider')
                    error.evaluation_failure=failure
                    raise error from None
            clients.append(client)
            super().__init__(s,limits=limits,client=client)
            adapters.append(self)
        def _run(self,*args,**kwargs):
            try: return super()._run(*args,**kwargs)
            except ExecutionError as exc:
                failure=safe_failure('warehouse',kwargs.get('purpose','warehouse'),exc.category)
                failures.append(failure)
                exc.evaluation_failure=failure
                raise
        def execute_compiled(self,query):
            captured.append({'operation':query.operation.value,'metric':query.metric_definition['metric'],
                'parameters':{p.name: (p.value.isoformat() if isinstance(p.value,(date,datetime)) else p.value)
                              for p in query.parameters},'brands':query.effective_brands,
                'products':query.requested_product_ids,
                'columns':list(query.result_contract.columns),
                'status_set':sorted(query.parameter('statuses').value)})
            return super().execute_compiled(query)
    clock=datetime.fromisoformat(turn['lifecycle_clock'])
    cid=str(uuid5(NAMESPACE_URL,account.state['identity']['run_id']+'/'+case['id']))
    tid=str(uuid5(NAMESPACE_URL,cid+'/'+turn.get('replay_of',turn['id'])))
    message=turn['message']
    if turn.get('confirm_proposal_from'):
        prior = records[turn['confirm_proposal_from']]['attempts'][0]['observed']
        message = 'confirm ' + prior['confirmation']['token']
    if turn.get('replay_of'):
        message=next(t['message'] for t in case['turns'] if t['id']==turn['replay_of'])
    # Scoped catalog discovery is through this same guarded adapter and included
    # in this evaluation turn's ordinary eight executions. Reuse the instance.
    preloaded=None
    def turn_factory(s,*,limits):
        nonlocal preloaded
        if preloaded is not None:
            value=preloaded; preloaded=None
            # Keep discovery's original start/deadline, never reset its budget.
            return value
        return EvaluationTurn(s,limits=limits)
    class IsolatedTrace(TurnTrace):
        def __init__(self,c,t): super().__init__(c,t,directory/'telemetry.jsonl')
    resolver=partial(identity.resolve_demo_scope,path=synthetic/'demo_users.json')
    try:
        if '{product_' in message:
            preloaded=EvaluationTurn(settings,limits=bigquery_adapter.QueryLimits())
            catalog=preloaded.load_catalog()
            scope=identity.resolve_demo_scope(case['owner'],catalog,path=synthetic/'demo_users.json')
            bindings,precondition=discover_products(preloaded,catalog,scope,turn['semantic_contract']['queries'][0])
            catalog_bindings.update(bindings)
            data_precondition=precondition
            atomic_json(bindings_path,catalog_bindings)
            atomic_json(precondition_path,data_precondition)
            message=message.replace('{product_1}',str(bindings['product_1'])).replace('{product_2}',str(bindings['product_2']))
        original_plans = request_resolver.analytical_plans
        def checked_plans(*args, **kwargs):
            try:
                return original_plans(*args, **kwargs)
            except identity.PolicyError as exc:
                code = str(exc)
                if code in PLAN_FAILURE_CODES:
                    plan_failures.append({'stage':'plan_validation', 'code':code, 'issues':[]})
                raise
        with ExitStack() as stack:
            stack.enter_context(patch.object(request_resolver, 'analytical_plans', checked_plans))
            stack.enter_context(patch.object(analysis_executor, 'analytical_plans', checked_plans))
            stack.enter_context(patch.object(workflow,'ROOT',directory))
            stack.enter_context(patch.object(workflow,'TurnTrace',IsolatedTrace))
            stack.enter_context(patch.object(workflow,'resolve_demo_scope',resolver))
            stack.enter_context(patch.object(bigquery_adapter,'resolve_demo_scope',resolver))
            store=SQLiteStore(directory/'state.sqlite',clock=lambda:clock)
            try:
                flow=workflow.AssistantWorkflow(settings,store,gateway_factory=EvaluationGateway,
                    turn_factory=turn_factory,today=date.fromisoformat(turn['analytical_date']))
                if turn.get('navigation'):
                    from .report_service import ReportService
                    service = ReportService.from_workflow(flow)
                    prior = records[turn['report_from']]['attempts'][0]['observed']
                    nav = (service.read(case['owner'], prior['report_id']) if turn['navigation'] == 'read'
                           else service.list(case['owner'], limit=1))
                    value = asdict(nav)
                    value['same_saved_text'] = (nav.content is not None and nav.content['text'] == prior['text'])
                    value['model_requests'] = 0
                    return value
                result=flow.run(case['owner'],message,conversation_id=cid,turn_id=tid)
                observed=observe_result(result,captured,proposals)
                observed["interpretation_attempts"] = len(interpretation_attempts)
                observed["first_model_status"] = interpretation_attempts[0] if interpretation_attempts else None
                observed["plan_failures"] = plan_failures
                if case.get('layer') == 'interpretation' and proposals:
                    # A valid clarification/unsupported reply may have no
                    # request. Preserve that semantic failure for the rubric
                    # rather than raising while inspecting absent query slots.
                    observed.update(first_request=None, first_query_count=0,
                                    first_operations=[], first_query=None, first_periods=None)
                if case.get('layer') == 'interpretation' and proposals and proposals[0].get('request') is not None:
                    # Judge the first parsed interpretation as well as the final
                    # executed request; a later repair cannot hide substitution.
                    from .contracts import RequestSpec
                    from .periods import resolve_request, ResolutionFailure
                    initial = RequestSpec.model_validate(proposals[0].get('request'))
                    first_request = initial.model_dump(mode='json')
                    # Compare the complete operation/effect set, not query zero.
                    # Period syntax is judged by resolved roles below; sources
                    # are diagnostic provenance rather than execution choices.
                    for field in ('analysis', 'comparison', 'sources'):
                        first_request.pop(field)
                    first_request['comparison_present'] = initial.comparison is not None
                    for query in first_request['queries']:
                        query['revenue_statuses'] = sorted(query['revenue_statuses'])
                        brands = query['requested_brands']
                        query['requested_brands'] = sorted(brands) if brands is not None else case['semantic_scope_brands']
                        # These fixed-shape playbooks do not compile a LIMIT;
                        # non-churn operations do not use an inactivity window.
                        # Ignore only those inert defaults, never query/effect
                        # count, actual filters, or a churn inactivity choice.
                        if query['operation'] in {'product_comparison', 'regional_spend', 'churn_change'}:
                            query.pop('limit')
                        if query['operation'] in {'product_comparison', 'regional_spend'}:
                            query.pop('inactivity_days')
                    observed['first_request'] = first_request
                    observed['first_query_count'] = len(initial.queries)
                    observed['first_operations'] = [q.operation.value for q in initial.queries]
                    if initial.queries:
                        query = initial.queries[0].model_dump(mode='json')
                        query['revenue_statuses'] = sorted(query['revenue_statuses'])
                        if query.get('requested_brands'):
                            query['requested_brands'] = sorted(query['requested_brands'])
                        observed['first_query'] = query
                    try:
                        resolved = resolve_request(initial, reference_date=date.fromisoformat(turn['analytical_date']),
                                                   analytical_timezone='UTC')
                        observed['first_periods'] = {role: getattr(resolved, role).model_dump(mode='json')
                            if getattr(resolved, role) else None for role in ('analysis', 'comparison', 'action')}
                    except ResolutionFailure:
                        observed['first_periods'] = None
                observed['input']=message
                observed['saved_report_integrity']=saved_report_integrity(result,store,case['owner'])
                observed['catalog_bindings']=catalog_bindings
                observed['data_precondition']=data_precondition
                context=store.load_continuity(case['owner'],cid)
                observed['pending_request']=context.pending.model_dump(mode='json') if context and context.pending else None
                observed['pending_periods']=None
                if context and context.pending and context.pending_resolution:
                    from .periods import resolve_request, ResolutionFailure
                    try:
                        pending=resolve_request(context.pending,reference_date=date.fromisoformat(turn['analytical_date']),
                            analytical_timezone='UTC',role_anchors=context.pending_resolution.anchors)
                        observed['pending_periods']={role:getattr(pending,role).model_dump(mode='json') if getattr(pending,role) else None
                                                     for role in ('analysis','comparison','action')}
                    except ResolutionFailure:
                        pass  # Missing pending slots have no concrete interval yet.
                observed['response_excerpt']=result.text[:1800]
                # Independent live scalar reference uses separately authored SQL,
                # same scope/period/status parameters, guarded job/bytes accounting.
                if turn.get('reference_scalar') and result.presentation:
                    try: observed['independent_reference']=live_reference(settings,result,adapters,account,turn['reference_expected'])
                    except ExecutionError:
                        # Preserve the already observed workflow and counters;
                        # the independently accounted reference remains blocked.
                        pass
                if turn.get('reference_playbook') and result.driver_finding:
                    try:
                        from .evaluation_part5 import playbook_reference
                        observed['independent_reference'] = playbook_reference(
                            adapters[-1], turn['reference_playbook'], result.driver_finding, catalog_bindings)
                    except ExecutionError:
                        pass
                if failures and (result.status=='error' or (turn.get('reference_scalar') or turn.get('reference_playbook')) and
                                 'independent_reference' not in observed):
                    observed['terminal_error']=failures[-1]
                observed['total_query_executions_including_reference']=sum(x.executions for x in adapters)
                observed['total_billed_bytes_including_reference']=None if any(x.billing_unknown for x in adapters) else sum(x.billed_bytes for x in adapters)
                observed['warehouse_dry_runs']=sum(x.dry_runs for x in adapters)
                return observed
            finally: store.close()
    finally:
        for client in clients:
            transport=client.client if isinstance(client,GuardedClient) else client
            if isinstance(transport,FixtureClient): transport.db.close()
            elif hasattr(transport,'close'): transport.close()


def live_reference(settings,result,adapters,account,expected):
    from google.cloud import bigquery
    from .compiler import DATASET
    evidence=result.presentation['evidence'][0]
    # References intentionally avoid the production compiler. They verify scalar
    # revenue only, with explicit Complete-only brand-scoped synthetic requests.
    if (expected['operation']!='aggregate' or expected['metric']!='revenue' or expected['products'] is not None
            or expected['status_set']!=['Complete'] or expected['columns']!=['revenue']):
        raise ValueError('unsupported_independent_reference_contract')
    interval=expected['period']
    sql=f'''SELECT SUM(i.sale_price) AS reference_revenue FROM `{DATASET}.order_items` i
      JOIN `{DATASET}.products` p ON p.id=i.product_id
      WHERE p.brand IN UNNEST(@brands) AND i.status='Complete' AND i.sale_price IS NOT NULL
      AND i.created_at >= @start_ts AND i.created_at < @end_ts'''
    # Keep references within the SAME production turn's execution/byte deadline.
    # Adapter _run accounts dry-run and job, including unknown billing policy.
    adapter=adapters[-1]
    # Parameters are supplied using the same typed translation, without compiler.
    from .compiler import BoundParameter
    bound=(BoundParameter('brands','ARRAY<STRING>',tuple(expected['brands'])),
           BoundParameter('start_ts','TIMESTAMP',datetime.fromisoformat(interval['start']+'T00:00:00+00:00')),
           BoundParameter('end_ts','TIMESTAMP',datetime.fromisoformat(interval['end']+'T00:00:00+00:00')))
    completed=adapter._run(sql,bound,max_rows=1,purpose='reference')
    value=completed.rows[0]['reference_revenue']
    source=evidence['rows'][0].get('revenue') if evidence['rows'] else None
    from decimal import Decimal
    return {'value':str(value) if value is not None else None,'matches':value is not None and source is not None and
            abs(Decimal(str(value))-Decimal(str(source)))<=Decimal('0.000001'),
            'job_id':completed.job_id,'as_of':completed.as_of.isoformat(),'expected_inputs':expected,
            'limitation':('fixed-time source reads; availability/retention must be verified by the owner'
                          if getattr(adapters[-1].client, 'snapshot_timestamp', None) else
                          'mutable public source; separate jobs are not an atomic snapshot')}


def run_probe(case,turn,private,account):
    account.check()
    timeout=min(120,account.state['caps']['active_seconds']-account.state['used']['active_seconds'])
    cmd=[sys.executable,'-m','pytest','-q','-p','no:cacheprovider',*case['selectors']]
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(ROOT/'src')}
    # Explicitly suppress existing opt-in integration flags in the CHILD ONLY;
    # model credentials/routes are never altered to obtain evaluation routing.
    env['OPSFLEET_LIVE_TESTS']='0'
    start=time.monotonic()
    try:
        proc=subprocess.run(cmd,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
        (private/(case['id']+'.pytest.log')).write_bytes(proc.stdout)
        summary=proc.stdout.decode('utf-8',errors='replace')
        test_counts={word:sum(int(n) for n in re.findall(r'(\d+) '+word+r'\b',summary)) for word in ('passed','failed','skipped','error')}
        return {'exit_code':proc.returncode,'test_instance_counts':test_counts,'elapsed_seconds':time.monotonic()-start,
                'category':'controlled_regression','real_model_attempts':0,'live_warehouse_executions':0,
                'injected_model_requests':None,'injected_warehouse_executions':None,
                'assertion_evidence':'existing test assertions executed; injected model/warehouse, not live semantics'}
    except subprocess.TimeoutExpired:
        return {'exit_code':124,'category':'controlled_regression_timeout'}


def execute(account,manifest,private,*,live=False,settings=None,selection=None):
    account.reconcile_interrupted(manifest)
    records={r['turn_id']:r for r in account.state['turns']}
    for case in manifest['scenarios']:
        for turn in case['turns']:
            key=turn['id']
            if key in records and records[key]['attempts']: continue
            row=records.setdefault(key,{'scenario':case['id'],'turn_id':key,'mode':case['mode'],'status':'NOT_RUN',
                 'reason':None,'attempts':[],'expected':turn['assertions']})
            if not live:
                if row['reason'] is None: row['reason']='dry_plan_no_dispatch'
                continue
            if selection and case['id'] not in selection:
                if row['reason'] in (None,'dry_plan_no_dispatch','outside_selected_batch'):
                    row['reason']='outside_selected_batch'
                continue
            if any(records[d]['status']!='PASS' for d in turn['depends_on']):
                row['reason']='prerequisite_did_not_pass'; continue
            # The STARTED attempt, active marker and turn counter commit together
            # before workflow/subprocess work or any external reservation.
            account.state['turns']=list(records.values())
            try: account.begin_turn(key,record=row)
            except StopScheduling as exc: row['reason']=str(exc); continue
            started=time.monotonic()
            attempt=row['attempts'][0]
            try:
                observed=run_probe(case,turn,private,account) if case['mode']=='controlled_regression' else run_workflow(case,turn,private,account,settings,records)
                previous=records[turn['compare_to']]['attempts'][-1]['observed'] if turn.get('compare_to') else None
                checks=assertions(turn['assertions'],observed,previous)
                first_checks = [c for c in checks if c['rule']['path'].startswith(('first_', 'interpretation_contract.'))]
                if case.get('layer') == 'interpretation':
                    attempt['first_semantic_status'] = ('NOT_RUN' if not observed.get('interpretation_attempts') else
                        'PASS' if observed.get('first_model_status') == 'ready' and first_checks and all(c['passed'] for c in first_checks) else 'FAIL')
                    attempt['workflow_status'] = observed.get('status')
                events=[e for e in account.state['events'] if e['turn']==key]
                row.update(status=outcome_status(checks,observed,events,account.state['stop']),reason=account.state['stop'])
                attempt.update(outcome='COMPLETED',observed=observed,assertions=checks)
            except StopScheduling as exc:
                row.update(status='FAIL',reason=str(exc))
                attempt.update(outcome='COMPLETED',observed={'category':'cap_stop'},assertions=[])
            except Exception as exc:
                # Only typed safe reason/stage metadata, never reprs or text.
                observed={'category':'safe_execution_exception'}
                if isinstance(exc,DataPreconditionError):
                    observed.update(category='data_precondition_unmet',data_precondition=exc.precondition)
                failure=getattr(exc,'evaluation_failure',None)
                if failure is None and isinstance(exc,ExecutionError):
                    failure=safe_failure('warehouse','warehouse',exc.category)
                elif failure is None and isinstance(exc,ModelFailure):
                    failure=safe_failure('model','model',exc.code)
                elif failure is None and isinstance(exc,OSError):
                    failure={'boundary':'environment','stage':'local_state','reason':'local_state_unavailable'}
                if failure: observed['terminal_error']=failure
                events=[e for e in account.state['events'] if e['turn']==key]
                status=outcome_status([],observed,events,account.state['stop']) if failure else 'FAIL'
                if isinstance(exc,OSError): status='BLOCKED_ENVIRONMENT'
                row.update(status=status,reason='data_precondition_unmet' if isinstance(exc,DataPreconditionError) else 'safe_execution_exception')
                attempt.update(outcome='COMPLETED',observed=observed,assertions=[])
            except BaseException:
                row.update(status='BLOCKED_ENVIRONMENT',reason='interrupted_turn_not_replayed')
                attempt.update(outcome='INTERRUPTED',observed={
                    'category':'interrupted_unknown_outcome','usage':None,'cost':None},assertions=[])
                account.state['stop']=account.state['stop'] or 'interrupted_turn_requires_report'
                raise
            finally:
                # Adapter exception handling can turn a pre-dispatch guard
                # denial into a generic provider failure. Retain that observed
                # error while restoring its authoritative evaluation provenance.
                denials=[dict(d) for d in account.state.get('denials',[]) if d['turn']==key]
                if denials:
                    observed=attempt.setdefault('observed',{})
                    observed['budget_denials']=denials
                    events=[e for e in account.state['events'] if e['turn']==key]
                    if attempt.get('outcome')!='INTERRUPTED':
                        row.update(status=outcome_status(attempt.get('assertions',[]),observed,events,account.state['stop']),
                                   reason=denials[-1]['reason'])
                attempt['elapsed_seconds']=time.monotonic()-started
                attempt['events']=[e['id'] for e in account.state['events'] if e['turn']==key]
                account.state['turns']=list(records.values()); account.end_turn()
    account.state['turns']=list(records.values()); account.save()
    return account.state['turns']


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id',required=True)
    p.add_argument('--live',action='store_true',help='Requires returned written independent preparation acceptance')
    p.add_argument('--acceptance',type=Path)
    p.add_argument('--resume',type=Path,help='Private temporary run directory printed by first invocation')
    p.add_argument('--select',nargs='+',help='Scenario IDs; unselected scenarios remain NOT_RUN')
    for name,maximum in MAX_CAPS.items(): p.add_argument('--'+name.replace('_','-'),type=float if name=='active_seconds' else int,default=maximum)
    args=p.parse_args(argv)
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,79}',args.run_id): p.error('invalid_run_id')
    manifest=validate_manifest(json.loads(MANIFEST.read_text()))
    source=json.loads(SOURCE.read_text()); revision=verify_source(ROOT,source)
    scenario=digest(MANIFEST)
    if args.select and not set(args.select).issubset({c['id'] for c in manifest['scenarios']}): p.error('unknown_scenario')
    review=None
    if args.live:
        if not args.acceptance: p.error('independent_acceptance_required')
        review=acceptance(ROOT,args.acceptance,revision,scenario)
    output=ROOT/'docs/remediation/runs'/args.run_id
    if args.resume: private=safe_private(args.resume)
    else:
        if output.exists(): p.error('run_id_already_exists')
        private=Path(tempfile.mkdtemp(prefix='opsfleet-eval-')); os.chmod(private,0o700)
        (private/'evaluation-private-marker').write_text('private synthetic evaluation state\n')
    with (private/'runner.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        caps={k:getattr(args,k) for k in MAX_CAPS}
        run_identity={'run_id':args.run_id,'source_revision':revision,'scenario_sha256':scenario,
                      'scenario_version':manifest['version'],'live':args.live,
                      'review_sha256':digest(args.acceptance) if review else None}
        account=Accounting(private/'checkpoint.json',identity=run_identity,caps=caps)
        settings=Settings.load() if args.live else None
        # Fixture warehouses need a project label but never instantiate ADC.
        redactor=Redactor((settings.gemini_api_key,settings.openrouter_api_key) if settings else ())
        if settings:
            if account.state.get('selected_model',settings.openrouter_model)!=settings.openrouter_model:
                raise ValueError('model_changed_on_resume')
            account.state['selected_model']=settings.openrouter_model
            account.state['configuration_available']={'openrouter':bool(settings.openrouter_api_key),'bigquery_project':bool(settings.project)}
        account.state.setdefault('batches',[]).append({'selection':sorted(args.select) if args.select else None})
        account.save()
        rows=execute(account,manifest,private,live=args.live,settings=settings,selection=set(args.select or ()))
        output.mkdir(parents=True,exist_ok=True)
        result=redactor({'kind':'bounded_evaluation' if args.live else 'dry_plan_no_execution',
            'identity':run_identity,'caps':caps,'used':account.state['used'],'events':account.state['events'],
            'denials':account.state.get('denials',[]),'stop':account.state['stop'],'dry_runs':account.state.get('dry_runs',0),
            'configuration_available':account.state.get('configuration_available'),
            'selected_model':account.state.get('selected_model'),'batches':account.state['batches'],
            'results':rows,'counts':totals(rows),'cost':None,'cost_note':'Only provider-reported per-attempt cost is known; no warehouse dollar estimate.',
            'limits':['No concrete user-authored cases supplied.','Human UX unverified.',
                      'Controlled regressions are not live semantic or BigQuery proof.']})
        version=len(account.state['batches'])
        snapshot=output/f'results-batch-{version:03}.json'
        if snapshot.exists(): raise ValueError('immutable_result_snapshot_exists')
        atomic_json(snapshot,result)
        atomic_json(output/'results.json',result)
        print(json.dumps({'output':str(output/'results.json'),'private_resume_directory':str(private),
                          'kind':result['kind'],'counts':result['counts']}))
    return 0

if __name__=='__main__': raise SystemExit(main())
