"""Regression coverage for the live-confirmed Gemini findings schema rejection."""
from copy import deepcopy
import hashlib
import json
from json import dumps
from types import SimpleNamespace

import pytest

from opsfleet_assistant import model_gateway
from opsfleet_assistant.config import Settings
from opsfleet_assistant.contracts import ReportDraft
from opsfleet_assistant.identity import PolicyError
from opsfleet_assistant.model_gateway import GeminiGateway, ModelFailure, _strict_schema
from opsfleet_assistant.reporting import ground_report
from test_grounded_findings import fixture_evidence


# Complete schema accepted by Google in the controlled terminal comparison.
ACCEPTED_SCHEMA_SHA256 = '1c1490b5434e314783e437b2bdffc9de1fd9a279001b6df00be84e9b78c88440'
SETTINGS = Settings('fixture-project', 'US', 'unused-direct', gemini_api_key='fixture-direct',
                    openrouter_api_key='fixture-router', openrouter_model='google/fixture')
SELECTED = {'status':'selected', 'findings':[{'operation':'observed_value',
    'current':{'evidence_index':0, 'row_index':0, 'metric':'revenue'},
    'baseline':None, 'order':'descending'}], 'headline_index':0,
    'recommendations':[{'kind':'review_performance', 'finding_index':0}], 'hypotheses':[]}


def fingerprint(schema):
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def router_transport(monkeypatch, reply):
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['timeout'] <= 30
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, *, headers, json):
            calls.append(json)
            # Model the actual provider rejection, rather than always accepting schemas.
            if 'maxItems' in json['response_format']['json_schema']['schema']['properties']['findings']:
                return SimpleNamespace(status_code=400)
            return SimpleNamespace(status_code=200, json=lambda: {
                'choices':[{'message':{'content':dumps(reply)}}]})
    monkeypatch.setattr(model_gateway.httpx, 'Client', Client)
    monkeypatch.setattr(model_gateway.genai, 'Client', lambda **kwargs: pytest.fail('unexpected direct call'))
    return calls


@pytest.mark.parametrize('route', ['direct', 'openrouter'])
@pytest.mark.parametrize('report_requested', [False, True])
def test_actual_report_routes_send_live_accepted_schema_and_ground_selection(monkeypatch, route, report_requested):
    calls = []
    if route == 'openrouter':
        bodies = router_transport(monkeypatch, SELECTED)
        gateway = GeminiGateway(SETTINGS, provider_mode='openrouter_only')
    else:
        class Client:
            def __init__(self, **kwargs): self.models = self
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def generate_content(self, **kwargs):
                calls.append(kwargs['config'])
                return SimpleNamespace(text=json.dumps(SELECTED))
        monkeypatch.setattr(model_gateway.genai, 'Client', Client)
        monkeypatch.setattr(model_gateway.httpx, 'Client', lambda **kwargs: pytest.fail('unexpected fallback'))
        gateway = GeminiGateway(SETTINGS)
    evidence = (fixture_evidence(),)
    draft = gateway.report(tuple(item.model_dump(mode='json', exclude={'execution_scope'}) for item in evidence),
                           report_requested=report_requested)
    grounded = ground_report(draft, evidence)
    schema = bodies[0]['response_format']['json_schema']['schema'] if route == 'openrouter' else calls[0].response_json_schema
    assert fingerprint(schema) == ACCEPTED_SCHEMA_SHA256
    assert draft.status == 'selected' and len(grounded.findings) == len(grounded.recommendations) == 1
    assert grounded.findings[0].value == 200
    assert gateway.budget.requests == 1
    if route == 'openrouter':
        assert bodies[0]['max_tokens'] == 2000 and bodies[0]['reasoning'] == {'effort':'low'}
        assert bodies[0]['provider'] == {'require_parameters':True,'zdr':True,'data_collection':'deny'}
        assert bodies[0]['response_format']['json_schema']['strict'] is True
    else:
        assert calls[0].max_output_tokens == 2000


@pytest.mark.parametrize('count', [20, 21])
def test_twenty_finding_local_limit_still_applies_after_provider_relaxation(monkeypatch, count):
    reply = deepcopy(SELECTED)
    reply['findings'] *= count
    calls = router_transport(monkeypatch, reply)
    gateway = GeminiGateway(SETTINGS, provider_mode='openrouter_only')
    if count == 20:
        draft = gateway.report((), report_requested=False)
        assert len(draft.findings) == len(ground_report(draft, (fixture_evidence(),)).findings) == 20
    else:
        with pytest.raises(ModelFailure, match='invalid_report_contract'):
            gateway.report((), report_requested=False)
    assert len(calls) == gateway.budget.requests == 1
    assert ReportDraft.model_json_schema()['properties']['findings']['maxItems'] == 20


@pytest.mark.parametrize('mutation', ['operation', 'metric', 'action', 'row_bounds', 'recommendation_count', 'hypothesis_count'])
def test_relaxed_provider_schema_still_rejects_invalid_local_contract(monkeypatch, mutation):
    reply = deepcopy(SELECTED)
    if mutation == 'operation': reply['findings'][0]['operation'] = 'invented'
    elif mutation == 'metric': reply['findings'][0]['current']['metric'] = 'invented'
    elif mutation == 'action': reply['recommendations'][0]['kind'] = 'invented'
    elif mutation == 'row_bounds': reply['findings'][0]['current']['row_index'] = 20
    elif mutation == 'recommendation_count': reply['recommendations'] *= 4
    else: reply['hypotheses'] = [{'kind':'review_retention','finding_index':0}] * 4
    calls = router_transport(monkeypatch, reply)
    gateway = GeminiGateway(SETTINGS, provider_mode='openrouter_only')
    with pytest.raises(ModelFailure, match='invalid_report_contract'):
        gateway.report((), report_requested=False)
    assert len(calls) == 1


@pytest.mark.parametrize('mutation', ['absent_row', 'unreleased_metric', 'absent_finding', 'irrelevant_action'])
def test_grounding_remains_required_after_schema_relaxation(monkeypatch, mutation):
    reply = deepcopy(SELECTED)
    if mutation == 'absent_row': reply['findings'][0]['current']['row_index'] = 1
    elif mutation == 'unreleased_metric': reply['findings'][0]['current']['metric'] = 'churn_rate'
    elif mutation == 'absent_finding': reply['recommendations'][0]['finding_index'] = 19
    else: reply['recommendations'][0]['kind'] = 'review_retention'
    router_transport(monkeypatch, reply)
    draft = GeminiGateway(SETTINGS, provider_mode='openrouter_only').report((), report_requested=False)
    with pytest.raises(PolicyError):
        ground_report(draft, (fixture_evidence(),))


def test_provider_schema_calls_do_not_mutate_the_local_contract():
    before = ReportDraft.model_json_schema()
    assert fingerprint(_strict_schema(ReportDraft)) == ACCEPTED_SCHEMA_SHA256
    assert fingerprint(_strict_schema(ReportDraft)) == ACCEPTED_SCHEMA_SHA256
    assert ReportDraft.model_json_schema() == before
