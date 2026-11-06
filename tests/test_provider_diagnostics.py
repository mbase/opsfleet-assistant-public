"""Transport diagnostics must survive redaction and stop a DNS-broken batch."""
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from opsfleet_assistant.config import Settings
from opsfleet_assistant.evaluation_contracts import Accounting, Redactor
from opsfleet_assistant.evaluation_runner import ProviderObserver, execute, outcome_status
from opsfleet_assistant.model_gateway import GeminiGateway, ModelBudget, ModelFailure
from opsfleet_assistant.telemetry import TurnTrace


@pytest.mark.parametrize('fault,kind,status', [
    (429, 'http', 'PROVIDER_FAILURE'), (503, 'http', 'PROVIDER_FAILURE'),
    ('dns', 'dns_resolution', 'BLOCKED_ENVIRONMENT'),
    ('timeout', 'timeout', 'PROVIDER_FAILURE'),
    ('unknown', 'unknown', 'PROVIDER_FAILURE'),
])
def test_diagnostics_survive_checkpoint_public_projection_and_telemetry(tmp_path, monkeypatch, fault, kind, status):
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, *args, **kwargs):
            if fault == 'dns':
                try:
                    raise socket.gaierror(socket.EAI_NONAME, 'secret-provider-body')
                except socket.gaierror as exc:
                    raise httpx.ConnectError('secret-provider-body') from exc
            if fault == 'unknown': raise RuntimeError('secret-provider-body')
            if fault == 'timeout': raise httpx.ReadTimeout('secret-provider-body')
            return SimpleNamespace(status_code=fault)
    monkeypatch.setattr('opsfleet_assistant.model_gateway.httpx.Client', Client)
    account = Accounting(tmp_path/'checkpoint.json', identity={'run_id':'test'})
    account.begin_turn('t1')
    budget = ModelBudget()
    budget.telemetry = TurnTrace('conversation', 'turn', tmp_path/'telemetry.jsonl')
    gateway = GeminiGateway(Settings(project=None,location='US',model='unused',
        openrouter_api_key='secret-provider-body'), budget=budget,
        provider_mode='openrouter_only', observer=ProviderObserver(account))
    with pytest.raises(ModelFailure, match='backup_failure' if fault == 'unknown' else 'backup_transient'):
        gateway._openrouter('secret-provider-body', {}, 64)
    persisted = json.loads((tmp_path/'checkpoint.json').read_text())
    public = Redactor(('secret-provider-body',))(persisted)
    event = public['events'][0]
    details = event['diagnostics']
    assert details['failure_kind'] == kind
    assert event['usage'] is None and event['cost'] is None
    assert outcome_status([], {}, [event], public['stop']) == status
    telemetry = [json.loads(line) for line in (tmp_path/'telemetry.jsonl').read_text().splitlines()]
    result = next(e for e in telemetry if e['event']=='model_result')
    assert all(result[k] == v for k, v in details.items())
    if kind == 'http': assert details['http_status'] == fault
    if kind == 'dns_resolution':
        assert details['dns_errno'] == socket.EAI_NONAME
        assert public['stop'] == 'provider_dns_resolution'
        # A confirmed DNS error stops all remaining work without reserving calls.
        monkeypatch.setattr('opsfleet_assistant.evaluation_runner.run_workflow',
                            lambda *a: pytest.fail('dispatched after DNS stop'))
        account.end_turn()
        manifest={'scenarios':[{'id':'next','mode':'live_fixture','turns':[
            {'id':'next-t1','depends_on':[],'assertions':[]}]}]}
        rows=execute(account,manifest,tmp_path,live=True,settings=gateway.settings)
        assert rows[0]['status']=='NOT_RUN' and rows[0]['reason']=='provider_dns_resolution'
        assert account.state['used']['provider_attempts']==1
    else:
        assert public['stop'] is None
    assert 'secret-provider-body' not in json.dumps(public) + json.dumps(telemetry)


@pytest.mark.parametrize('stage,limit', [('interpretation', 5000), ('report', 2000)])
@pytest.mark.parametrize('finish', ['stop', 'length'])
def test_bounded_output_limits_preserve_truncation_failure_and_single_dispatch(tmp_path, monkeypatch, stage, limit, finish):
    """Even parseable JSON must fail on length; increasing a cap adds no retry."""
    from test_evaluation_preparation import SETTINGS
    from opsfleet_assistant.contracts import InterpretationProposal, ReportDraft
    from opsfleet_assistant.model_gateway import _strict_schema
    calls = []
    raw = ({'status': 'ready', 'context_action': 'continue', 'request': {'intent': 'save'}}
           if stage == 'interpretation' else {'status': 'selected'})
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False and 0 < kwargs['timeout'] <= 30
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, *, headers, json):
            calls.append(json)
            return SimpleNamespace(status_code=200, json=lambda: {
                'model': SETTINGS.openrouter_model,
                'usage': {'completion_tokens': 19, 'total_tokens': 42, 'cost': 0.001},
                'choices': [{'finish_reason': finish, 'message': {'content': __import__('json').dumps(raw)}}]})
    monkeypatch.setattr('opsfleet_assistant.model_gateway.httpx.Client', Client)
    monkeypatch.setattr('opsfleet_assistant.model_gateway.genai.Client', lambda **kw: pytest.fail('Direct fallback'))
    account = Accounting(tmp_path/'checkpoint.json', identity={'run_id': 'output-control'}, caps={
        'turns': 1, 'provider_attempts': 1, 'warehouse_executions': 1, 'billed_bytes': 1, 'active_seconds': 120})
    account.begin_turn(stage)
    budget = ModelBudget(max_requests=1)
    gateway = GeminiGateway(SETTINGS, budget=budget, provider_mode='openrouter_only', observer=ProviderObserver(account))
    def invoke():
        if stage == 'interpretation':
            return gateway.interpret('Save that exact analysis.', previous_plan=None,
                reference_date='2026-11-06', analytical_timezone='UTC')
        return gateway.report((), report_requested=False)
    if finish == 'length':
        with pytest.raises(ModelFailure, match='truncated_model_output'):
            invoke()
    else:
        assert invoke().status == ('ready' if stage == 'interpretation' else 'selected')
    assert len(calls) == budget.requests == account.state['used']['provider_attempts'] == 1
    body = calls[0]
    assert body['max_tokens'] == limit and body['reasoning'] == {'effort': 'low'}
    assert body['provider'] == {'require_parameters': True, 'zdr': True, 'data_collection': 'deny'}
    schema = InterpretationProposal if stage == 'interpretation' else ReportDraft
    assert body['response_format']['json_schema']['schema'] == _strict_schema(schema)
    event = account.state['events'][0]
    assert event['actual_model'] == SETTINGS.openrouter_model and event['cost'] == 0.001
    assert event['usage']['completion_tokens'] == 19
    assert event['response_metadata']['finish_category'] == finish
    if finish == 'length':
        assert event['diagnostics']['failure_code'] == 'truncated_output'
        assert outcome_status([], {}, [event], account.state['stop']) == 'PROVIDER_FAILURE'
    else:
        assert event['outcome'] == 'response_received'


def test_direct_interpretation_sends_bounded_output_limit_without_fallback(monkeypatch):
    from test_evaluation_preparation import SETTINGS
    calls = []
    class Client:
        def __init__(self, **kwargs): self.models = self
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text=json.dumps({'status': 'ready', 'context_action': 'continue',
                'request': {'intent': 'save'}}))
    monkeypatch.setattr('opsfleet_assistant.model_gateway.genai.Client', Client)
    monkeypatch.setattr('opsfleet_assistant.model_gateway.httpx.Client', lambda **kw: pytest.fail('Router fallback'))
    gateway = GeminiGateway(SETTINGS, budget=ModelBudget(max_requests=1))
    assert gateway.interpret('Save that exact analysis.', previous_plan=None,
        reference_date='2026-11-06', analytical_timezone='UTC').status == 'ready'
    assert len(calls) == gateway.budget.requests == 1
    assert calls[0]['config'].max_output_tokens == 5000
