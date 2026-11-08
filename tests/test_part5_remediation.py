"""Observed live failures replayed offline; these are not model-quality measurements."""
from datetime import date
import json

import pytest

from opsfleet_assistant.config import ROOT
from opsfleet_assistant.contracts import InterpretationProposal, InterpretationResult, TemporalExpression
from opsfleet_assistant import evaluation_part5 as p5, evaluation_runner as runner
from opsfleet_assistant.evaluation_contracts import Accounting
from opsfleet_assistant.intent import analytical_plans
from opsfleet_assistant.periods import resolve_request
from opsfleet_assistant.provider_contracts import encode_interpretation
from opsfleet_assistant.model_gateway import GeminiGateway, ModelFailure
from test_evaluation_preparation import http_transport, SETTINGS
from test_part5_validation import specification
from test_playbooks import spec, regional_spec, churn_spec

OBSERVATIONS = json.loads((ROOT / 'tests/fixtures/part5_observed_interpretations.json').read_text())['cases']
FACTORIES = {'product': spec, 'regional': regional_spec, 'churn': churn_spec}


@pytest.mark.parametrize('observation', OBSERVATIONS, ids=lambda o: o['scenario'])
def test_observed_first_choices_keep_first_attempt_failures(tmp_path, monkeypatch, observation):
    case = next(c for c in specification()['scenarios'] if c['id'] == observation['scenario'])
    proposal = InterpretationProposal.model_validate(observation['parsed_local_proposals_without_sources'][0])
    good = FACTORIES[case['fixture']]()
    # Repair is permitted, but cannot promote the first interpretation.
    from opsfleet_assistant.contracts import ReportDraft
    replies = [json.dumps(encode_interpretation(proposal)),
               json.dumps(encode_interpretation(InterpretationProposal(status='ready', request=good))),
               ReportDraft().model_dump_json()]
    http_transport(monkeypatch, replies)
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'observed-replay'})
    row = runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)[0]
    assert row['attempts'][0]['first_semantic_status'] == ('PASS' if observation['observed_status'] == 'PASS' else 'FAIL')
    if proposal.status != 'ready':
        assert row['attempts'][0]['observed']['analysis_queries'] == 0


@pytest.mark.parametrize('profile', FACTORIES)
def test_corrected_contracts_compile_against_frozen_oracle(tmp_path, monkeypatch, profile):
    from opsfleet_assistant.contracts import ReportDraft
    case = next(c for c in specification()['scenarios'] if c['layer'] == 'interpretation' and c['fixture'] == profile)
    good = InterpretationProposal(status='ready', request=FACTORIES[profile]())
    http_transport(monkeypatch, [json.dumps(encode_interpretation(good)), ReportDraft().model_dump_json()])
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'corrected-replay'})
    row = runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)[0]
    assert row['status'] == 'PASS', row


@pytest.mark.parametrize('factory,change', [(spec, 'dimension'), (regional_spec, 'segment'),
    (churn_spec, 'dimension'), (churn_spec, 'mode'), (churn_spec, 'population')])
def test_invalid_choices_are_rejected_before_compilation(factory, change):
    request = factory().model_dump(mode='json')
    if change == 'dimension': request['queries'][0]['dimension'] = 'product' if factory == spec else 'country'
    elif change == 'mode': request['mode'] = 'churn_compare'
    elif change == 'segment': request['playbook']['segment'] = 'country'
    else: request['playbook']['population'] = None
    from opsfleet_assistant.contracts import RequestSpec
    from opsfleet_assistant.identity import PolicyError
    with pytest.raises((ValueError, PolicyError)):
        analytical_plans(resolve_request(RequestSpec.model_validate(request), reference_date=date(2026,11,8), analytical_timezone='UTC'))


@pytest.mark.parametrize('start,end,earlier_start,earlier_end', [
    ('2025-01-01','2025-04-01','2024-10-01','2025-01-01'),
    ('2024-02-01','2024-02-29','2023-12-01','2024-01-01'),
    ('2024-02-01','2024-03-01','2023-12-01','2024-01-01'),
    ('2025-12-01','2026-01-01','2024-12-01','2025-01-01')])
def test_exclusive_cutoffs_are_preserved_without_date_rewriting(start,end,earlier_start,earlier_end):
    request = churn_spec().model_copy(update={
        'analysis':TemporalExpression(kind='interval',start=start,end=end),
        'comparison':TemporalExpression(kind='interval',start=earlier_start,end=earlier_end)})
    plan, = analytical_plans(resolve_request(request, reference_date=date(2026,11,8), analytical_timezone='UTC'))
    assert plan.cutoff_date == date.fromisoformat(end)
    assert plan.baseline_cutoff_date == date.fromisoformat(earlier_end)


@pytest.mark.parametrize('mutation,rule', [('type','string_type'),('enum','enum'),('extra','extra_forbidden'),('cross','invalid_ready_interpretation')])
def test_invalid_repairs_have_safe_wire_diagnostics(tmp_path, monkeypatch, mutation, rule):
    payload = encode_interpretation(InterpretationProposal(status='ready', request=spec()))
    if mutation == 'type': payload['request']['queries'][0]['requested_brands'] = [123]
    elif mutation == 'enum': payload['request']['queries'][0]['operation'] = 'private-sentinel'
    elif mutation == 'extra': payload['private-sentinel'] = 'private-sentinel'
    else: payload['request'] = None
    http_transport(monkeypatch, [json.dumps(payload)])
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id':'invalid-repair'})
    account.begin_turn('synthetic')
    gateway = GeminiGateway(SETTINGS, provider_mode='openrouter_only', observer=runner.ProviderObserver(account))
    with pytest.raises(ModelFailure) as error:
        gateway.interpret('Synthetic', previous_plan=None, reference_date='2026-11-08', analytical_timezone='UTC')
    stage = 'reconciliation' if mutation == 'cross' else 'wire_validation'
    assert error.value.stage == stage
    assert rule in {i['rule'] for i in error.value.validation_issues}
    diagnostics = p5.public_failure_diagnostics(account.state['events'])
    assert diagnostics[0]['stage'] == stage and diagnostics[0]['issues']
    assert 'private-sentinel' not in json.dumps(diagnostics)
    assert runner.outcome_status([], {}, account.state['events'], None) == 'FAIL'


@pytest.mark.parametrize('code,stage,status', [('invalid_json','json_parsing','FAIL'),
    ('invalid_interpretation_contract','local_validation','FAIL'),
    ('invalid_interpretation_contract','reconciliation','FAIL'),
    ('truncated_output','generation','PROVIDER_FAILURE'),
    ('provider_transient','transport','PROVIDER_FAILURE'),('provider_failure','transport','PROVIDER_FAILURE')])
def test_versioned_classification(code, stage, status):
    events = [{'kind':'provider','outcome':'failed','diagnostics':{'failure_stage':stage,'failure_code':code}}]
    assert runner.outcome_status([], {}, events, None) == status


def test_public_diagnostics_reject_arbitrary_checkpoint_fields():
    events = [{'diagnostics':{'failure_stage':'wire_validation','failure_code':'invalid_interpretation_contract',
        'validation_issues':[{'field':'credential','rule':'private-sentinel'}, {'field':'request','rule':'private-sentinel'}]}}]
    assert p5.public_failure_diagnostics(events)[0]['issues'] == []


@pytest.mark.parametrize('response,stage,status', [
    (503, 'transport', 'PROVIDER_FAILURE'),
    (__import__('httpx').ReadTimeout('private-sentinel'), 'transport', 'PROVIDER_FAILURE'),
    ('not-json-private-sentinel', 'json_parsing', 'FAIL'),
    ('[]', 'json_parsing', 'FAIL'),
    ({'model':SETTINGS.openrouter_model, 'usage':{'cost':0.001}, 'choices':[{
        'finish_reason':'length','message':{'content':'private-sentinel'}}]}, 'generation', 'PROVIDER_FAILURE')])
def test_transport_and_generation_boundaries(tmp_path, monkeypatch, response, stage, status):
    http_transport(monkeypatch, [response])
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id':'boundary'})
    account.begin_turn('synthetic')
    gateway = GeminiGateway(SETTINGS, provider_mode='openrouter_only', observer=runner.ProviderObserver(account))
    with pytest.raises(ModelFailure):
        gateway.interpret('Synthetic', previous_plan=None, reference_date='2026-11-08', analytical_timezone='UTC')
    diagnostics = p5.public_failure_diagnostics(account.state['events'])
    assert diagnostics[0]['stage'] == stage
    assert runner.outcome_status([], {}, account.state['events'], None) == status
    assert 'private-sentinel' not in json.dumps(diagnostics)


def test_invalid_first_response_cannot_be_promoted_by_valid_repair(tmp_path, monkeypatch):
    from opsfleet_assistant.contracts import ReportDraft
    case = next(c for c in specification()['scenarios'] if c['id'] == 'product-p1-r1')
    good = encode_interpretation(InterpretationProposal(status='ready', request=spec()))
    bad = json.loads(json.dumps(good))
    bad['request']['playbook'] = 'private-sentinel'
    http_transport(monkeypatch, [json.dumps(bad), json.dumps(good), ReportDraft().model_dump_json()])
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id':'invalid-first'})
    row = runner.execute(account, {'scenarios':[case]}, tmp_path, live=True, settings=SETTINGS)[0]
    assert row['status'] == 'FAIL'
    assert row['attempts'][0]['first_semantic_status'] == 'FAIL'
    public = p5.public_result(account)['cases'][0]['attempts'][0]
    assert public['repair_count'] == 1 and public['failures'][0]['stage'] == 'wire_validation'


def test_invalid_provider_envelope_is_generation_failure(tmp_path, monkeypatch):
    from opsfleet_assistant import model_gateway
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs):
            class Response:
                status_code = 200
                def json(self): raise ValueError('private-sentinel')
            return Response()
    monkeypatch.setattr(model_gateway.httpx, 'Client', Client)
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id':'bad-envelope'})
    account.begin_turn('synthetic')
    gateway = GeminiGateway(SETTINGS, provider_mode='openrouter_only', observer=runner.ProviderObserver(account))
    with pytest.raises(ModelFailure): gateway._generate('Synthetic', {}, 64)
    diagnostics = p5.public_failure_diagnostics(account.state['events'])
    assert diagnostics == [{'stage':'generation','code':'provider_failure','issues':[]}]
    assert runner.outcome_status([], {}, account.state['events'], None) == 'PROVIDER_FAILURE'
