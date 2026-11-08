"""Offline wire conversion and local rejection checks; no live acceptance claim."""
from copy import deepcopy
from itertools import product
import json
from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import ValidationError

from opsfleet_assistant import model_gateway
from opsfleet_assistant.contracts import (
    InterpretationProposal, InterpretationResult, PlaybookChoice, QuerySpec, RequestSpec,
)
from opsfleet_assistant.model_gateway import GeminiGateway, ModelFailure, _strict_schema
from opsfleet_assistant.provider_contracts import (
    PlaybookToken, decode_interpretation, encode_interpretation,
)
from test_evaluation_preparation import SETTINGS
from test_playbooks import spec, regional_spec, churn_spec


CHOICES = [None] + [PlaybookChoice(id=i, population=p, segment=s) for i, p, s in product(
    ('product_revenue', 'regional_spend', 'churn_change'),
    (None, 'qualifying_purchasers'), (None, 'country'))]


@pytest.mark.parametrize('choice', CHOICES)
def test_every_local_playbook_choice_round_trips_without_inference(choice):
    proposal = InterpretationProposal(status='ready', request=RequestSpec(playbook=choice))
    original = proposal.model_dump(mode='json')
    encoded = encode_interpretation(proposal)
    before = deepcopy(encoded)
    assert decode_interpretation(encoded) == proposal
    assert proposal.model_dump(mode='json') == original and encoded == before
    if choice:
        assert encoded['request']['playbook'] in get_args(PlaybookToken)
        assert len(get_args(PlaybookToken)) == len(CHOICES) - 1 == 12
    else:
        assert encoded['request']['playbook'] is None


@pytest.mark.parametrize('status', ['needs_clarification', 'unsupported'])
def test_no_request_and_nonready_outcomes_are_preserved(status):
    proposal = InterpretationProposal(status=status, issues=('missing_period',))
    assert decode_interpretation(encode_interpretation(proposal)) == proposal


@pytest.mark.parametrize('token', ['invented', 'regional_spend',
    'regional_spend|all_users|unspecified', 'churn_change|qualifying_purchasers|city',
    'churn_change|qualifying_purchasers|country|extra',
    {'id': 'product_revenue', 'version': 2}, 1, [], True])
def test_wire_rejects_unknown_or_malformed_playbooks(token):
    payload = encode_interpretation(InterpretationProposal(status='ready', request=RequestSpec()))
    payload['request']['playbook'] = token
    with pytest.raises(ValidationError):
        decode_interpretation(payload)


def test_wire_schema_retains_shape_types_and_choices_with_fewer_branches():
    schema = _strict_schema(InterpretationProposal)
    assert schema == _strict_schema(InterpretationResult)
    def walk(value):
        if isinstance(value, dict):
            assert 'maxItems' not in value and 'const' not in value
            if value.get('type') == 'object':
                assert value['additionalProperties'] is False
                assert set(value['required']) == set(value['properties'])
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(schema)
    assert 'PlaybookChoice' not in schema['$defs']
    playbook = schema['$defs']['ProviderRequestSpec']['properties']['playbook']
    assert playbook['anyOf'] == [{'enum': list(get_args(PlaybookToken)), 'type': 'string'}, {'type': 'null'}]
    assert schema['$defs']['QuerySpec']['properties']['limit']['maximum'] == 20
    assert 'regional_spend' in schema['$defs']['Operation']['enum']
    assert 'churn_change' in schema['$defs']['Operation']['enum']
    # Never mutate Pydantic's application/persistence contracts.
    local = InterpretationProposal.model_json_schema()
    assert local['$defs']['RequestSpec']['properties']['queries']['maxItems'] == 2
    assert local['$defs']['PlaybookChoice']['properties']['version']['const'] == 1


def reply():
    return encode_interpretation(InterpretationProposal(status='ready', request=RequestSpec(
        queries=(QuerySpec(operation='aggregate', metric='revenue'),))))


@pytest.mark.parametrize('field,count', [('queries', 3), ('sources', 11),
    ('requested_brands', 21), ('product_ids', 11), ('demographic_values', 3),
    ('requested_sections', 7), ('clear_fields', 3), ('temporal_updates', 4), ('issues', 6)])
def test_all_nine_collection_limits_remain_enforced_locally(field, count):
    payload = reply()
    request = payload['request']
    if field == 'queries': request[field] *= count
    elif field == 'sources': request[field] = [{'field': 'metric', 'origin': 'explicit', 'phrase': None}] * count
    elif field in {'requested_brands', 'product_ids', 'demographic_values'}:
        request['queries'][0][field] = ([1] if field == 'product_ids' else ['value']) * count
    elif field == 'requested_sections':
        request['report'] = {'requested_sections': ['summary'] * count}
    elif field == 'clear_fields': payload[field] = ['report'] * count
    elif field == 'temporal_updates': payload[field] = ['analysis'] * count
    else: payload[field] = ['missing_period'] * count
    with pytest.raises(ValidationError) as exc:
        decode_interpretation(payload)
    assert any(e['type'] == 'too_long' for e in exc.value.errors())


@pytest.mark.parametrize('route', ['direct', 'openrouter'])
@pytest.mark.parametrize('factory', [spec, regional_spec, churn_spec])
def test_both_actual_gateway_routes_decode_playbooks(monkeypatch, route, factory):
    request = factory()
    encoded = encode_interpretation(InterpretationProposal(status='ready', request=request))
    calls = []
    class Client:
        def __init__(self, **kwargs): self.models = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def generate_content(self, **kwargs):
            calls.append(kwargs['config'].response_json_schema)
            assert kwargs['config'].max_output_tokens == 5000
            return SimpleNamespace(text=json.dumps(encoded))
        def post(self, url, *, headers, json):
            calls.append(json['response_format']['json_schema']['schema'])
            assert json['response_format']['json_schema']['strict'] is True
            assert json['max_tokens'] == 5000 and json['reasoning'] == {'effort': 'low'}
            assert json['provider'] == {'require_parameters': True, 'zdr': True, 'data_collection': 'deny'}
            return SimpleNamespace(status_code=200, json=lambda: {
                'choices': [{'message': {'content': __import__('json').dumps(encoded)}}]})
    monkeypatch.setattr(model_gateway.genai, 'Client', Client)
    monkeypatch.setattr(model_gateway.httpx, 'Client', Client)
    gateway = GeminiGateway(SETTINGS, provider_mode='openrouter_only' if route == 'openrouter' else 'production')
    result = gateway.interpret('Investigate the difference', previous_plan=None,
        reference_date='2026-11-08', analytical_timezone='UTC')
    assert result.request == request and result.request.playbook == request.playbook
    assert calls == [_strict_schema(InterpretationProposal)]
    assert gateway.budget.requests == 1


def test_missing_population_is_not_filled_by_wire_conversion():
    payload = reply()
    payload['request']['playbook'] = 'regional_spend|unspecified|unspecified'
    assert decode_interpretation(payload).request.playbook.population is None


@pytest.mark.parametrize('mutation', ['extra_field', 'query_count', 'playbook'])
def test_gateway_maps_invalid_wire_response_to_existing_failure(monkeypatch, mutation):
    payload = reply()
    if mutation == 'extra_field': payload['request']['authority'] = 'admin'
    elif mutation == 'query_count': payload['request']['queries'] *= 3
    else: payload['request']['playbook'] = 'invented'
    gateway = GeminiGateway(SETTINGS, provider_mode='openrouter_only')
    monkeypatch.setattr(gateway, '_generate', lambda *args: payload)
    with pytest.raises(ModelFailure, match='invalid_interpretation_contract') as exc:
        gateway.interpret('Synthetic', previous_plan=None, reference_date='2026-11-08', analytical_timezone='UTC')
    assert exc.value.validation_issues
    assert 'admin' not in exc.value.repair_feedback and 'invented' not in exc.value.repair_feedback
