"""Bounded repair of model-added save choices; scripted replies are not live evidence."""
import json

import pytest

from opsfleet_assistant.config import ROOT
from opsfleet_assistant.contracts import InterpretationResult as I, RequestSpec as R, ReportSpec, Mode, Metric, ItemStatus
from opsfleet_assistant.model_gateway import ModelFailure, build_interpretation_prompt
from test_conversation_flows import harness, analysis, goal, year, OWNER, OracleTurn


RECORDED = json.loads((ROOT / 'tests/fixtures/exact_save_interpretations.json').read_text())


@pytest.mark.parametrize('recorded', RECORDED, ids=lambda r: r['run_id'])
def test_recorded_expanded_exact_save_repairs_without_changing_snapshot(harness, recorded):
    script, seen, store, flow, _ = harness
    initial = I.model_validate(recorded['preceding_interpretation']).model_copy(
        update={'context_action': 'new', 'temporal_updates': ()})
    script['Initial'] = initial
    first = flow().run(OWNER, 'Initial', conversation_id='recorded', turn_id='first')
    assert first.status == 'ok'
    before = store.load_continuity(OWNER, 'recorded')
    expanded = I.model_validate(recorded['expanded_save'])
    question = recorded['question']

    def reply(kwargs):
        if not kwargs['feedback']:
            return expanded
        feedback = kwargs['feedback']
        assert 'explicitly' in feedback and 'Never discard' in feedback
        assert 'executive' in feedback and 'briefing' in feedback
        assert 'performance' in feedback and 'narrative' in feedback
        prompt, counts = build_interpretation_prompt(question, previous_plan=before.snapshot.plans[0],
            reference_date='2026-11-06', analytical_timezone='UTC', feedback=feedback,
            conversation_context=kwargs['conversation_context'])
        assert prompt and counts['prompt_chars'] <= 12000 and question in prompt
        return goal('save')

    script[question] = reply
    saved = flow().run(OWNER, question, conversation_id='recorded', turn_id='save')
    assert saved.status in {'ok', 'partial'} and saved.report_id is not None
    assert saved.model_requests == 2 and saved.query_executions == 1
    assert saved.factual_findings == first.factual_findings
    assert saved.suggestions == first.suggestions
    assert saved.resolved_periods == first.resolved_periods
    assert saved.evidence_job_ids == first.evidence_job_ids
    assert len(OracleTurn.queries) == 1 and len(store.list_reports(OWNER)) == 1
    after = store.load_continuity(OWNER, 'recorded')
    assert after.snapshot.request == before.snapshot.request
    report = store.report(OWNER, saved.report_id)
    assert report is not None and '2024-02-01' in report['content']
    assert saved.report_composition['spec']['purpose'] == 'executive'
    assert saved.report_composition['spec']['presentation'] == 'briefing'
    save_calls = [kwargs for q, kwargs in seen if q == question]
    assert len(save_calls) == 2
    assert save_calls[0]['conversation_context'] == save_calls[1]['conversation_context']
    replay = flow().run(OWNER, question, conversation_id='recorded', turn_id='save')
    assert replay.reused_report and replay.text == saved.text
    assert len([q for q, _ in seen if q == question]) == 2


@pytest.mark.parametrize('change', ['year', 'metric', 'filter', 'statuses', 'clear', 'refresh'])
def test_known_analytical_modification_never_repairs_away_constraints(harness, change):
    script, seen, store, flow, _ = harness
    script['Initial'] = analysis()
    flow().run(OWNER, 'Initial', conversation_id='changed', turn_id='first')
    before = store.load_continuity(OWNER, 'changed')
    original = before.snapshot.request.spec
    query = original.queries[0]
    req = original.model_copy(update={'intent': 'save', 'mode': Mode.REPORT,
        'report': ReportSpec(purpose='performance', presentation='narrative')})
    flags = {}
    if change == 'year':
        req = req.model_copy(update={'analysis': year(2024)})
    elif change == 'metric':
        req = req.model_copy(update={'queries': (query.model_copy(update={'metric': Metric.ORDER_COUNT}),)})
    elif change == 'filter':
        req = req.model_copy(update={'queries': (query.model_copy(update={'requested_brands': ('Calvin Klein',)}),)})
    elif change == 'statuses':
        req = req.model_copy(update={'queries': (query.model_copy(update={'revenue_statuses': (ItemStatus.COMPLETE,)}),)})
    elif change == 'clear':
        flags = {'clear_fields': ('report',)}
        req = req.model_copy(update={'report': None})
    else:
        flags = {'temporal_updates': ('analysis',)}
    script['Modify and save'] = I(status='ready', context_action='correct', request=req, **flags)
    result = flow().run(OWNER, 'Modify and save', conversation_id='changed', turn_id='save')
    assert result.status == 'needs_clarification' and result.model_requests == 1
    assert result.report_id is None and result.query_executions == 1
    assert len(OracleTurn.queries) == 1 and store.list_reports(OWNER) == []
    after = store.load_continuity(OWNER, 'changed')
    assert after.snapshot == before.snapshot and after.expires_at == before.expires_at
    assert after.pending is None


@pytest.mark.parametrize('repair', ['repeat', 'analysis', 'clarification', 'unsupported', 'invalid_json', 'invalid_contract'])
def test_unsuccessful_report_choice_repair_clarifies_once_without_side_effects(harness, repair):
    script, seen, store, flow, _ = harness
    script['Initial'] = analysis()
    flow().run(OWNER, 'Initial', conversation_id='unresolved', turn_id='first')
    before = store.load_continuity(OWNER, 'unresolved')
    req = R(intent='save', report=ReportSpec(presentation='table', detail='concise',
        requested_sections=('summary', 'limitations')))
    expanded = I(status='ready', context_action='continue', request=req)

    def reply(kwargs):
        if not kwargs['feedback'] or repair == 'repeat':
            return expanded
        if repair == 'analysis':
            return analysis(2024, action='correct').model_copy(update={
                'request': analysis(2024).request.model_copy(update={'mode': Mode.REPORT})})
        if repair == 'clarification':
            return I(status='needs_clarification', request=req, issues=('missing_period',))
        if repair == 'unsupported':
            return I(status='unsupported', issues=('unsupported_capability',))
        raise ModelFailure('invalid_model_json' if repair == 'invalid_json' else 'invalid_interpretation_contract')

    script['Save with a concise table and no recommendations'] = reply
    result = flow().run(OWNER, 'Save with a concise table and no recommendations',
        conversation_id='unresolved', turn_id='save')
    assert result.status == 'needs_clarification' and result.model_requests == 2
    assert 'No report was saved.' in result.text and result.report_id is None
    assert result.query_executions == 1 and len(OracleTurn.queries) == 1
    assert store.list_reports(OWNER) == []
    after = store.load_continuity(OWNER, 'unresolved')
    assert after.snapshot == before.snapshot and after.expires_at == before.expires_at
    assert after.pending is None and after.pending_resolution is None
    assert len([q for q, _ in seen if q.startswith('Save with')]) == 2


def test_report_choice_repair_cannot_extend_existing_three_interpretation_attempts(harness):
    script, seen, store, flow, _ = harness
    script['Initial'] = analysis()
    flow().run(OWNER, 'Initial', conversation_id='limit', turn_id='first')
    calls = []
    def reply(kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise ModelFailure('invalid_model_json')
        return I(status='ready', context_action='continue', request=R(intent='save',
            report=ReportSpec(presentation='narrative')))
    script['Save'] = reply
    result = flow().run(OWNER, 'Save', conversation_id='limit', turn_id='save')
    assert result.status == 'needs_clarification' and result.model_requests == 3
    assert len(calls) == 3 and store.list_reports(OWNER) == []
    assert len(OracleTurn.queries) == 1


@pytest.mark.parametrize('failure', ['model_request_limit', 'backup_transient'])
def test_save_repair_respects_dispatch_limits_and_provider_failure_provenance(harness, failure):
    script, _, store, flow, _ = harness
    script['Initial'] = analysis()
    flow().run(OWNER, 'Initial', conversation_id='budget', turn_id='first')
    before = store.load_continuity(OWNER, 'budget')
    calls = []
    class LimitedGateway:
        def __init__(self, settings, *, budget):
            self.budget = budget
            if failure == 'model_request_limit':
                budget.max_requests = 1
        def interpret(self, question, **kwargs):
            self.budget.charge('injected_model')
            calls.append(kwargs)
            if kwargs['feedback']:
                raise ModelFailure(failure)
            return I(status='ready', context_action='continue', request=R(intent='save',
                report=ReportSpec(presentation='narrative')))
    current = flow()
    current.gateway_factory = LimitedGateway
    result = current.run(OWNER, 'Save', conversation_id='budget', turn_id='save')
    assert result.status == 'error' and result.report_id is None
    assert result.model_requests == (1 if failure == 'model_request_limit' else 2)
    assert len(calls) == result.model_requests
    assert store.list_reports(OWNER) == [] and len(OracleTurn.queries) == 1
    after = store.load_continuity(OWNER, 'budget')
    assert after.snapshot == before.snapshot and after.expires_at == before.expires_at
