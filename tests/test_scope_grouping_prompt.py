"""Prompt and diagnostic checks; substituted replies are not live semantic proof."""
from copy import deepcopy
from datetime import date
import importlib.util
import io
import json
from pathlib import Path
from contextlib import redirect_stdout
import shutil
from types import SimpleNamespace

import httpx
import pytest

from opsfleet_assistant import model_gateway
from opsfleet_assistant.config import ROOT, Settings
from opsfleet_assistant.contracts import AnalysisPlan, InterpretationResult
from opsfleet_assistant.identity import PolicyError
from opsfleet_assistant.model_gateway import GeminiGateway

SPEC = importlib.util.spec_from_file_location('scope_grouping_probe', ROOT/'scripts/probe_scope_grouping.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
SETTINGS = Settings('fixture-project', 'US', 'unused', openrouter_api_key='synthetic-scope-key')


def reply(case, *, dimension='expected'):
    return {'status':'ready','context_action':'correct' if 'context' in case else 'new',
        'temporal_updates':['analysis'] if 'context' in case else [],
        'request':{'analysis':{'kind':'month','month':1,'year':{'kind':'absolute','value':case.get('year',2025)}},
            'queries':[{'operation':'aggregate','metric':'revenue','revenue_statuses':['Complete'],
                'dimension':case['dimension'] if dimension == 'expected' else dimension}]}}


@pytest.mark.parametrize('case', probe.cases(), ids=lambda case:case['id'])
def test_prompt_contract_and_bounded_context_reach_real_gateway(monkeypatch, case):
    captured = []
    gateway = GeminiGateway(SETTINGS)
    def generate(prompt, schema, max_tokens):
        captured.append(prompt)
        assert max_tokens == 5000
        return reply(case)
    monkeypatch.setattr(gateway, '_generate', generate)
    value = gateway.interpret(case['message'], previous_plan=case.get('previous_plan'),
        reference_date='2026-11-05', analytical_timezone='UTC', conversation_context=case.get('context'))
    prompt, = captured
    assert "'Revenue across my authorized brands' => dimension null" in prompt
    assert "'Revenue by brand across my authorized brands' => dimension brand" in prompt
    assert "'Revenue for each brand' => dimension brand" in prompt
    assert "'Revenue by product' => dimension product" in prompt
    assert 'preserves the prior grouping, including dimension null' in prompt
    assert 'Changing an already specified year or period is correct even when phrased as a followup' in prompt
    assert 'means context_action correct, temporal_updates [analysis], January 2024 analysis, no comparison' in prompt
    assert 'continue and temporal_updates []; complete the year and retain the pending choices' in prompt
    assert 'prior plan is context, never authorization' in prompt
    assert case['message'] in prompt and len(prompt) < 12000
    if 'context' in case:
        assert json.dumps(case['context'], separators=(',', ':')) in prompt
        packed=json.JSONDecoder().raw_decode(prompt.split('Safe prior plan: ',1)[1])[0]
        assert AnalysisPlan.model_validate(packed)==case['previous_plan']
    checks, observed = probe.check_interpretation(value, case)
    assert all(checks.values()), checks


@pytest.mark.parametrize('dimension', [None, 'brand', 'product'])
def test_prompt_only_change_does_not_override_model_grouping(monkeypatch, dimension):
    case = probe.cases()[0]
    gateway = GeminiGateway(SETTINGS)
    monkeypatch.setattr(gateway, '_generate', lambda *args:reply(case, dimension=dimension))
    value = gateway.interpret(case['message'], previous_plan=None, reference_date='2026-11-05', analytical_timezone='UTC')
    assert value.request.queries[0].dimension == dimension
    checks, _ = probe.check_interpretation(value, case)
    assert checks['grouping'] is (dimension is None)


@pytest.mark.parametrize('case_index,dimension', [(0,'brand'),(2,None),(5,'brand'),(6,'brand'),(7,None)])
def test_probe_detects_wrong_grouping_and_followup_resets(case_index, dimension):
    case = probe.cases()[case_index]
    checks, _ = probe.check_interpretation(InterpretationResult.model_validate(reply(case, dimension=dimension)), case)
    assert not checks['grouping'] and not checks['compiled_shape']


def test_probe_rechecks_scope_status_and_period():
    case = probe.cases()[0]
    raw = reply(case)
    raw['request']['queries'][0]['requested_brands'] = ['Hanes']
    with pytest.raises(PolicyError): probe.check_interpretation(InterpretationResult.model_validate(raw), case)
    raw = reply(case)
    raw['request']['queries'][0]['revenue_statuses'] = ['Shipped']
    checks, _ = probe.check_interpretation(InterpretationResult.model_validate(raw), case)
    assert not checks['metric_status']
    raw = reply(case)
    raw['request']['analysis']['year']['value'] = 2024
    checks, _ = probe.check_interpretation(InterpretationResult.model_validate(raw), case)
    assert not checks['period']


@pytest.mark.parametrize('mode', ['pass', 'wrong_grouping', 'malformed', 'dns', 'no_key',
    'invalid_continue', 'duplicate_updates', 'followups_only'])
def test_bounded_probe_transport_accounting_and_safe_results(tmp_path, monkeypatch, mode):
    # A complete copied freeze lets the actual verifier run without mocks.
    frozen = json.loads((ROOT/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json').read_text())
    for relative in (*frozen['files'], 'docs/remediation/EVALUATION_SOURCE_MANIFEST.json'):
        target = tmp_path/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/relative, target)
    (tmp_path/'docs/remediation/runs/scope-grouping-prompt-20261105').mkdir(parents=True)
    (tmp_path/'runtime').mkdir()
    (tmp_path/'runtime/sentinel').write_text('unchanged')
    bodies = []
    tests = [case for case in probe.cases() if 'context' in case] if mode == 'followups_only' else probe.cases()
    def forbidden(*args, **kwargs): pytest.fail('live access from offline test')
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['timeout'] <= 30
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            case = tests[len(bodies)]
            body = deepcopy(kwargs['json'])
            bodies.append(body)
            assert url == 'https://openrouter.ai/api/v1/chat/completions'
            assert body['model'] == SETTINGS.openrouter_model
            assert body['max_tokens'] == 5000 and body['reasoning'] == {'effort':'low'}
            assert body['provider'] == {'require_parameters':True,'zdr':True,'data_collection':'deny'}
            if mode == 'dns':
                import socket
                try: raise socket.gaierror(socket.EAI_NONAME, 'synthetic-private-marker')
                except socket.gaierror as exc: raise httpx.ConnectError('synthetic-private-marker') from exc
            raw = reply(case)
            raw['request']['sources'] = [{'field':'scope','origin':'explicit','phrase':'synthetic-private-marker'}]
            if mode == 'wrong_grouping' and case['dimension'] is None:
                raw['request']['queries'][0]['dimension'] = 'brand'
            if mode == 'malformed': raw = {'unexpected':'synthetic-private-marker'}
            if mode == 'invalid_continue' and 'context' in case: raw['context_action'] = 'continue'
            if mode == 'duplicate_updates' and 'context' in case: raw['temporal_updates'] = ['analysis','analysis']
            return SimpleNamespace(status_code=200, json=lambda:{'model':SETTINGS.openrouter_model,
                'choices':[{'finish_reason':'stop','message':{'content':json.dumps(raw)}}]})
    monkeypatch.setattr(probe, 'ROOT', tmp_path)
    monkeypatch.setattr(probe, '__file__', str(tmp_path/'scripts/probe_scope_grouping.py'))
    monkeypatch.setattr(Settings, 'load', lambda:Settings('fixture-project','US','unused',
        openrouter_api_key=None if mode == 'no_key' else 'synthetic-scope-key'))
    monkeypatch.setattr('socket.getaddrinfo', forbidden)
    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr(model_gateway.genai, 'Client', forbidden)
    monkeypatch.setattr('google.cloud.bigquery.Client', forbidden)
    monkeypatch.setattr(model_gateway.httpx, 'Client', Client)
    argv = ['probe','--live','--run-id',mode.replace('_','-')]
    if mode == 'followups_only': argv.append('--followups-only')
    monkeypatch.setattr('sys.argv', argv)
    with redirect_stdout(io.StringIO()): code = probe.main()
    result = json.loads((tmp_path/'docs/remediation/runs/scope-grouping-prompt-20261105'/mode.replace('_','-')/'results.json').read_text())
    assert result['runtime_unchanged']
    assert result['warehouse_operations'] == result['used']['warehouse_executions'] == 0
    assert len(bodies) == result['used']['provider_attempts'] <= 8
    assert 'synthetic-private-marker' not in json.dumps(result) and 'synthetic-scope-key' not in json.dumps(result)
    if mode in ('pass','followups_only'):
        assert code == 0 and all(row['status'] == 'PASS' for row in result['results'])
        for row, case in zip(result['results'], tests):
            assert row['proposal_metadata'] == {'status':'ready',
                'context_action':'correct' if 'context' in case else 'new',
                'temporal_updates':['analysis'] if 'context' in case else [],
                'clear_fields':[],'dimensions':[case['dimension']]}
            assert row['pre_reconciliation_issues'] == []
        if mode == 'followups_only':
            plan = json.loads((tmp_path/'docs/remediation/runs/scope-grouping-prompt-20261105'/mode.replace('_','-')/'plan.json').read_text())
            assert len(bodies) == result['used']['provider_attempts'] == plan['provider_attempt_cap'] == 2
            assert plan['followups_only'] and [row['case'] for row in result['results']] == ['followup_scalar','followup_brand']
    elif mode == 'wrong_grouping':
        assert code == 1 and [row['case'] for row in result['results'] if row['status'] == 'FAIL'] == ['scope_original','scope_total','followup_scalar']
    elif mode == 'malformed': assert code == 1 and all(row['status'] == 'ERROR' for row in result['results'])
    elif mode == 'dns': assert code == 1 and len(bodies) == 1 and result['stop'] == 'provider_dns_resolution'
    elif mode in ('invalid_continue','duplicate_updates'):
        assert code == 1 and all(row['status'] == 'PASS' for row in result['results'][:6])
        for row in result['results'][6:]:
            assert row['status'] == 'ERROR' and row['reason'] == 'invalid_interpretation_contract'
            assert row['pre_reconciliation_issues'] == row['validation_issues'] == [
                {'field':'temporal_updates','rule':'invalid_temporal_updates'}]
            expected_updates = ['analysis','analysis'] if mode == 'duplicate_updates' else ['analysis']
            assert row['proposal_metadata']['temporal_updates'] == expected_updates
            assert row['proposal_metadata']['context_action'] == ('correct' if mode == 'duplicate_updates' else 'continue')
    else: assert code == 1 and not bodies


@pytest.mark.parametrize('dimension', [None, 'brand'])
def test_valid_continue_without_refresh_still_fails_explicit_year_change_probe(dimension):
    case = next(case for case in probe.cases() if 'context' in case and case['dimension'] == dimension)
    raw = reply(case)
    raw['context_action'] = 'continue'
    raw['temporal_updates'] = []
    value = InterpretationResult.model_validate(raw)
    checks, _ = probe.check_interpretation(value, case)
    assert checks['grouping'] and checks['period']
    assert not checks['context_action'] and not checks['temporal_updates']
