"""The real gateway must reconcile safe slot completion before final validation."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json

import pytest

from opsfleet_assistant.config import Settings
from opsfleet_assistant.contracts import InterpretationResult, RequestSpec, ReportDraft
from opsfleet_assistant.model_gateway import GeminiGateway, ModelFailure, _strict_schema
from opsfleet_assistant.store import SQLiteStore
from opsfleet_assistant.workflow import AssistantWorkflow
from test_request_interpretation import RecordingTurn

SETTINGS = Settings(None, 'US', 'unused')


def pending(kind='month', role='analysis'):
    expression = {'kind': kind}
    if kind == 'month': expression['month'] = 1
    if kind == 'quarter': expression['quarter'] = 1
    data = {'analysis': {'kind':'month','month':1,'year':{'kind':'absolute','value':2025}},
            'queries':[{'operation':'aggregate','metric':'revenue','revenue_statuses':['Complete']}]}
    data[role] = expression
    return RequestSpec.model_validate(data)


def completed(old, role='analysis', *, context_action='continue'):
    data = old.model_dump(mode='json')
    data[role]['year'] = {'kind':'absolute','value':2025,'offset':None}
    return {'status':'ready','context_action':context_action,'temporal_updates':[role],
            'clear_fields':[],'issues':[],'request':data}


def interpret(raw, old, **kwargs):
    class WireGateway(GeminiGateway):
        def _generate(self, prompt, schema, max_tokens): return deepcopy(raw)
    return WireGateway(SETTINGS).interpret('Use 2025 for that period.', previous_plan=None,
        reference_date='2026-11-05', analytical_timezone='UTC', pending_request=old, **kwargs)


@pytest.mark.parametrize('message', ['2025', 'Use 2025 for that January analysis.'])
def test_real_gateway_completes_year_with_history_and_one_interpretation(tmp_path, message):
    old = pending()
    first = {'status':'needs_clarification','request':old.model_dump(mode='json'),
             'issues':['missing_year']}
    responses = [first, completed(old)]
    prompts = []
    class WireGateway(GeminiGateway):
        def _direct(self, prompt, schema, max_tokens):
            self.budget.charge('injected_model')
            prompts.append(prompt)
            return json.dumps(responses.pop(0))
        def report(self, evidence, *, report_requested):
            self.budget.charge('injected_model')
            return ReportDraft()
    path = tmp_path/'state.sqlite'
    store = SQLiteStore(path, clock=lambda:datetime(2026,11,5,tzinfo=timezone.utc))
    RecordingTurn.queries = []
    flow = AssistantWorkflow(SETTINGS,store,gateway_factory=WireGateway,
                            turn_factory=RecordingTurn,today=date(2026,11,5))
    question = 'Revenue in January, Complete items only.'
    result = flow.run('manager_apparel',question,conversation_id='pending')
    assert result.status == 'needs_clarification'
    store.close()
    store = SQLiteStore(path, clock=lambda:datetime(2026,11,5,tzinfo=timezone.utc))
    flow = AssistantWorkflow(SETTINGS,store,gateway_factory=WireGateway,
                            turn_factory=RecordingTurn,today=date(2026,11,5))
    result = flow.run('manager_apparel',message,conversation_id='pending')
    assert result.status == 'ok'
    assert result.model_requests == 2  # one interpretation plus deterministic test report selector
    assert len(prompts) == 2
    assert question in prompts[1] and 'Which year should I use for that period?' in prompts[1]
    assert 'pending_request' in prompts[1] and 'missing_year' in prompts[1]
    assert result.resolved_periods['analysis'] == {'start':'2025-01-01','end':'2025-02-01'}
    query, = RecordingTurn.queries
    assert query.parameter('statuses').value == ('Complete',)
    assert query.parameter('brands').value == ('Calvin Klein','Carhartt')
    store.close()


@pytest.mark.parametrize('kind', ['month','quarter','year'])
@pytest.mark.parametrize('role', ['analysis','comparison','action'])
def test_missing_calendar_year_is_normalized_for_each_temporal_role(kind, role):
    old = pending(kind,role)
    value = interpret(completed(old,role),old)
    assert isinstance(value,InterpretationResult)
    assert value.context_action == 'continue' and value.temporal_updates == ()
    assert getattr(value.request,role).year.value == 2025
    assert value.request.queries == old.queries


@pytest.mark.parametrize('change', [
    'month','metric','brands','products','statuses','limit','intent','mode','report','action',
])
def test_normalization_never_hides_an_unrelated_change(change):
    old = pending()
    raw = completed(old)
    query = raw['request']['queries'][0]
    if change == 'month': raw['request']['analysis']['month'] = 2
    elif change == 'metric': query['metric'] = 'order_count'
    elif change == 'brands': query['requested_brands'] = ['Hanes']
    elif change == 'products': query['product_ids'] = [1]
    elif change == 'statuses': query['revenue_statuses'] = ['Returned']
    elif change == 'limit': query['limit'] = 1
    elif change == 'intent': raw['request']['intent'] = 'save'
    elif change == 'mode': raw['request']['mode'] = 'report'
    elif change == 'report': raw['request']['report'] = {'requested_sections':['summary']}
    elif change == 'action': raw['request']['action'] = {'kind':'quarter','quarter':2,'year':{'kind':'absolute','value':2025}}
    with pytest.raises(ModelFailure,match='invalid_interpretation_contract'):
        interpret(raw,old)


def test_no_pending_state_cannot_be_supplied_by_history_or_model():
    old = pending()
    with pytest.raises(ModelFailure,match='invalid_interpretation_contract'):
        interpret(completed(old),None,conversation_context={'pending_request':old.model_dump(mode='json')})


def test_duplicate_flags_still_fail_final_validation():
    old = pending(); raw = completed(old)
    raw['temporal_updates'] = ['analysis','analysis']
    with pytest.raises(ModelFailure,match='invalid_interpretation_contract'):
        interpret(raw,old)


def test_explicit_correction_preserves_its_refresh_flags():
    old = pending().model_copy(update={'analysis':RequestSpec.model_validate({
        'analysis':{'kind':'month','month':1,'year':{'kind':'absolute','value':2024}}}).analysis})
    result = interpret(completed(old,context_action='correct'),old)
    assert result.context_action == 'correct' and result.temporal_updates == ('analysis',)
    assert result.request.analysis.year.value == 2025


def test_completed_period_cannot_be_refreshed_as_a_continuation():
    old = pending().model_copy(update={'analysis':RequestSpec.model_validate({
        'analysis':{'kind':'window','unit':'month','count':1,'offset':0,'include_current':False}}).analysis})
    raw = {'status':'ready','context_action':'continue','temporal_updates':['analysis'],
           'request':old.model_dump(mode='json')}
    with pytest.raises(ModelFailure,match='invalid_interpretation_contract'):
        interpret(raw,old)


def test_result_contract_remains_strict_without_context_reconciliation():
    from pydantic import ValidationError
    with pytest.raises(ValidationError,match='invalid_temporal_updates'):
        InterpretationResult.model_validate(completed(pending()))


def test_safe_repair_feedback_identifies_rule_without_raw_fields_or_values(tmp_path):
    old = pending(); invalid = completed(old)
    invalid['request']['analysis']['month'] = 2  # a period change cannot be normalized
    secret = 'synthetic-private-marker'
    malformed = {'status':'ready','request':{'analysis':{'kind':'month','month':secret},
                 secret:secret},'issues':[]}
    for raw in [invalid, malformed]:
        with pytest.raises(ModelFailure) as caught:
            interpret(raw,old)
        feedback = caught.value.repair_feedback
        assert feedback and secret not in feedback
        assert caught.value.validation_issues
    with pytest.raises(ModelFailure) as caught:
        interpret(invalid,old)
    assert 'invalid_temporal_updates' in caught.value.repair_feedback
    assert 'temporal_updates' in caught.value.repair_feedback


def test_invalid_wire_shape_is_not_normalized():
    old = pending(); raw = completed(old)
    raw['request']['sql'] = 'synthetic-private-marker'
    with pytest.raises(ModelFailure,match='invalid_interpretation_contract'):
        interpret(raw,old)


def test_repair_receives_specific_feedback_with_original_budget(tmp_path):
    old = pending()
    bad = completed(old); bad['request']['analysis']['month'] = 2
    responses = [bad,completed(old)]
    prompts = []
    class Gateway(GeminiGateway):
        def _direct(self,prompt,schema,max_tokens):
            self.budget.charge('injected_model'); prompts.append(prompt)
            return json.dumps(responses.pop(0))
        def report(self,evidence,*,report_requested):
            self.budget.charge('injected_model'); return ReportDraft()
    # Seed the real pending state through a normal clarification turn first.
    class Seed(Gateway):
        def _direct(self,*args):
            self.budget.charge('injected_model')
            return json.dumps({'status':'needs_clarification','request':old.model_dump(mode='json'),
                               'issues':['missing_year']})
    store = SQLiteStore(tmp_path/'state.sqlite')
    RecordingTurn.queries = []
    result = AssistantWorkflow(SETTINGS,store,gateway_factory=Seed,turn_factory=RecordingTurn,
        today=date(2026,11,5)).run('manager_apparel','Revenue in January, Complete only.',conversation_id='repair')
    assert result.status == 'needs_clarification'
    result = AssistantWorkflow(SETTINGS,store,gateway_factory=Gateway,turn_factory=RecordingTurn,
        today=date(2026,11,5)).run('manager_apparel','Use 2025.',conversation_id='repair')
    assert result.status == 'ok' and result.model_requests == 3
    assert 'invalid_temporal_updates' in prompts[1]
    assert 'context_action=continue' in prompts[1]
    assert len(RecordingTurn.queries) == 1
    store.close()


def test_wire_and_result_share_the_existing_provider_schema():
    from opsfleet_assistant.contracts import InterpretationProposal
    assert _strict_schema(InterpretationProposal) == _strict_schema(InterpretationResult)


def test_filling_baseline_year_preserves_unrelated_relative_analysis_anchor():
    from opsfleet_assistant.contracts import TemporalAnchor
    from opsfleet_assistant.conversation import request_role_anchors
    from opsfleet_assistant.periods import resolve_request
    old = RequestSpec.model_validate({
        'mode':'temporal_compare',
        'analysis':{'kind':'window','unit':'month','count':1,'offset':0,'include_current':False},
        'comparison':{'kind':'month','month':1},
        'queries':[{'operation':'aggregate','metric':'revenue','revenue_statuses':['Complete'],
                    'temporal_role':role} for role in ['analysis','comparison']]})
    raw = completed(old,'comparison')
    result = interpret(raw,old)
    original = TemporalAnchor(reference_date=date(2026,1,31))
    anchors = request_role_anchors(result.request,previous_spec=old,
        previous_anchors={'analysis':original,'comparison':original},
        context_action=result.context_action,reference_date=date(2026,2,1),previous_plan=None,
        temporal_updates=result.temporal_updates)
    resolved = resolve_request(result.request,reference_date=date(2026,2,1),
                               analytical_timezone='UTC',role_anchors=anchors)
    assert resolved.analysis.model_dump(mode='json') == {'start':'2025-12-01','end':'2026-01-01'}
    assert resolved.comparison.model_dump(mode='json') == {'start':'2025-01-01','end':'2025-02-01'}
    assert anchors['analysis'] == original
