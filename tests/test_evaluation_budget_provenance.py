"""Budget-denial provenance through real evaluation/workflow boundaries; no live calls."""
from copy import deepcopy
import importlib.util
import json
import shutil

import pytest

from opsfleet_assistant import evaluation_runner as runner
from opsfleet_assistant.config import ROOT
from opsfleet_assistant.contracts import ActionSelection, InterpretationResult, QuerySpec, ReportDraft, ReportSpec, RequestSpec, TemporalExpression, YearExpression
from opsfleet_assistant.evaluation_contracts import Accounting, MAX_CAPS, StopScheduling
from opsfleet_assistant.evaluation_fixtures import FixtureClient
from test_evaluation_preparation import SETTINGS, http_transport


def smoke_driver():
    spec = importlib.util.spec_from_file_location('budget_smoke_driver', ROOT/'scripts/report_schema_live_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def account(tmp_path, **caps):
    return Accounting(tmp_path/'checkpoint.json', identity={'run_id':'budget-test'}, caps={**MAX_CAPS, **caps})


def live_journey():
    manifest = json.loads((ROOT/'docs/remediation/SCENARIO_MANIFEST.json').read_text())
    return deepcopy(next(case for case in manifest['scenarios'] if case['id'] == 'live-journey'))


def model_replies():
    def month(year): return TemporalExpression(kind='month',month=1,year=YearExpression(kind='absolute',value=year))
    def quarter(number): return TemporalExpression(kind='quarter',quarter=number,year=YearExpression(kind='absolute',value=2025))
    def query(role='analysis'): return QuerySpec(operation='aggregate',metric='revenue',revenue_statuses=('Complete',),temporal_role=role)
    draft = ReportDraft(recommendations=(ActionSelection(kind='review_performance',finding_index=0),))
    values = [
        InterpretationResult(status='ready',request=RequestSpec(analysis=month(2025),queries=(query(),))), draft,
        InterpretationResult(status='ready',context_action='continue',request=RequestSpec(intent='explain')),
        InterpretationResult(status='ready',context_action='correct',temporal_updates=('comparison',),
            request=RequestSpec(mode='temporal_compare',analysis=month(2025),comparison=month(2024),queries=(query(),query('comparison')))), draft,
        InterpretationResult(status='ready',request=RequestSpec(mode='report',analysis=quarter(3),action=quarter(4),
            queries=(query(),),report=ReportSpec(presentation='table'))), draft,
    ]
    return [value.model_dump_json() for value in values]


def test_smoke_budget_covers_all_fourteen_planned_jobs():
    driver = smoke_driver()
    # Catalog/analysis/coverage/reference; catalog; catalog/two analysis-coverage pairs; report four jobs.
    assert driver.CAPS['warehouse_executions'] == 4 + 1 + 5 + 4 == 14
    assert driver.CAPS == {'turns':10,'provider_attempts':20,'warehouse_executions':14,
        'billed_bytes':1024**3,'active_seconds':600}


@pytest.mark.parametrize('cap', [12,14])
def test_actual_live_journey_path_preserves_cap_denial_and_finishes_with_correct_budget(tmp_path, monkeypatch, cap):
    calls = http_transport(monkeypatch, model_replies())
    def fixture_client(settings):
        client = FixtureClient()
        # Standard fixtures lack January 2024; supply the comparison operand
        # so the real completeness check can reach the final budget boundary.
        client.add(99901,41001,'Complete','15','2024-01-10')
        client.add(99902,41002,'Complete','5','2024-01-10')
        return client
    monkeypatch.setattr(runner, '_client_from_adc', fixture_client)
    monkeypatch.setattr('socket.getaddrinfo', lambda *args, **kwargs:pytest.fail('real network access'))
    a = account(tmp_path, warehouse_executions=cap)
    rows = runner.execute(a, {'scenarios':[live_journey()]}, tmp_path, live=True, settings=SETTINGS)
    assert [row['status'] for row in rows[:3]] == ['PASS']*3
    warehouse = [event for event in a.state['events'] if event['kind'] == 'warehouse']
    assert len(warehouse) == a.state['used']['warehouse_executions'] == cap
    final = rows[-1]
    observed = final['attempts'][0]['observed']
    if cap == 12:
        expected = [{'turn':'live-journey-t04','reason':'warehouse_executions_cap'}]
        assert a.state['denials'] == expected
        assert final['status'] == 'FAIL' and final['reason'] == 'warehouse_executions_cap'
        assert observed['budget_denials'] == expected
        # Keep the original adapter observation; the explicit denial supplies its real cause.
        assert observed['terminal_error'] == {'boundary':'warehouse','stage':'coverage','reason':'provider_failure'}
        assert not observed['saved'] and observed['evidence_count'] == 0
        assert [event['purpose'] for event in warehouse[-2:]] == ['opsfleet_catalog','opsfleet_analysis']
        assert len(calls) == 6
    else:
        assert final['status'] == 'PASS' and final['reason'] is None
        assert all(check['passed'] for check in final['attempts'][0]['assertions'])
        assert observed['saved'] and observed['report_completeness'] == 'complete'
        assert observed['independent_reference']['matches']
        assert not a.state.get('denials')
        assert len(calls) == 7


def one_turn():
    return {'scenarios':[{'id':'single','mode':'live_fixture','turns':[
        {'id':'single-t01','depends_on':[],'assertions':[{'path':'status','op':'eq','value':'ok'}]}]}]}


def test_swallowed_provider_cap_is_not_a_passing_turn(tmp_path, monkeypatch):
    a = account(tmp_path, provider_attempts=1)
    def work(*args):
        ticket = a.reserve('provider',route='openrouter_gemini')
        a.finish(ticket,outcome='response_received')
        try: a.reserve('provider',route='openrouter_gemini')
        except StopScheduling: pass
        return {'status':'ok'}
    monkeypatch.setattr(runner, 'run_workflow', work)
    row, = runner.execute(a, one_turn(), tmp_path, live=True, settings=SETTINGS)
    assert row['status'] == 'FAIL' and row['reason'] == 'provider_attempts_cap'
    assert row['attempts'][0]['observed']['budget_denials'] == [{'turn':'single-t01','reason':'provider_attempts_cap'}]


def test_real_model_failure_provenance_remains_visible_alongside_cap(tmp_path, monkeypatch):
    a = account(tmp_path, provider_attempts=1)
    def work(*args):
        ticket = a.reserve('provider',route='openrouter_gemini')
        a.finish(ticket,outcome='failed')
        a.reserve('provider',route='openrouter_gemini')
    monkeypatch.setattr(runner, 'run_workflow', work)
    row, = runner.execute(a, one_turn(), tmp_path, live=True, settings=SETTINGS)
    assert row['status'] == 'PROVIDER_FAILURE' and row['reason'] == 'provider_attempts_cap'
    assert row['attempts'][0]['observed']['budget_denials']


def test_billed_byte_denial_is_durable_before_dispatch(tmp_path):
    a = account(tmp_path, billed_bytes=1)
    a.begin_turn('single-t01')
    ticket = a.reserve('warehouse',cap=1)
    a.finish(ticket,billed=1,outcome='completed')
    with pytest.raises(StopScheduling,match='billed_bytes_cap'): a.reserve('warehouse',cap=1)
    saved = json.loads(a.path.read_text())
    assert saved['denials'] == [{'turn':'single-t01','reason':'billed_bytes_cap'}]
    assert len(saved['events']) == 1 and saved['used']['warehouse_executions'] == 1


def test_previous_turn_denial_does_not_poison_an_independent_turn(tmp_path, monkeypatch):
    a = account(tmp_path)
    a.state['denials'] = [{'turn':'other-turn','reason':'warehouse_executions_cap'}]
    monkeypatch.setattr(runner, 'run_workflow', lambda *args:{'status':'ok'})
    row, = runner.execute(a, one_turn(), tmp_path, live=True, settings=SETTINGS)
    assert row['status'] == 'PASS' and 'budget_denials' not in row['attempts'][0]['observed']


def test_interruption_after_a_denial_keeps_interrupted_status(tmp_path, monkeypatch):
    a = account(tmp_path, provider_attempts=1)
    def work(*args):
        ticket = a.reserve('provider',route='openrouter_gemini')
        a.finish(ticket,outcome='response_received')
        try: a.reserve('provider',route='openrouter_gemini')
        except StopScheduling: raise KeyboardInterrupt()
    monkeypatch.setattr(runner, 'run_workflow', work)
    with pytest.raises(KeyboardInterrupt): runner.execute(a, one_turn(), tmp_path, live=True, settings=SETTINGS)
    row, = a.state['turns']
    assert row['status'] == 'BLOCKED_ENVIRONMENT' and row['reason'] == 'interrupted_turn_not_replayed'
    assert row['attempts'][0]['outcome'] == 'INTERRUPTED'
    assert row['attempts'][0]['observed']['budget_denials']


def test_smoke_results_and_stdout_export_budget_denials(tmp_path, monkeypatch, capsys):
    driver = smoke_driver()
    frozen = json.loads((ROOT/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json').read_text())
    for relative in (*frozen['files'], 'docs/remediation/EVALUATION_SOURCE_MANIFEST.json'):
        target = tmp_path/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/relative, target)
    (tmp_path/'docs/remediation/runs/report-schema-fix-20261105').mkdir(parents=True)
    (tmp_path/'runtime').mkdir()
    expected = [{'turn':'single-t01','reason':'warehouse_executions_cap'}]
    def no_dispatch(a, selected, private, **kwargs):
        assert a.state['caps']['warehouse_executions'] == 14
        assert len(selected['scenarios']) == 5
        a.state['denials'] = expected
        return []
    monkeypatch.setattr(driver, 'ROOT', tmp_path)
    monkeypatch.setattr(driver, '__file__', str(tmp_path/'scripts/report_schema_live_smoke.py'))
    monkeypatch.setattr(driver, 'execute', no_dispatch)
    monkeypatch.setattr(driver.Settings, 'load', lambda:SETTINGS)
    monkeypatch.setattr('sys.argv', ['smoke','--live','--run-id','denial-export-test'])
    monkeypatch.setattr('socket.getaddrinfo', lambda *args, **kwargs:pytest.fail('real network access'))
    assert driver.main() == 0
    summary = json.loads(capsys.readouterr().out)
    result = json.loads((tmp_path/'docs/remediation/runs/report-schema-fix-20261105/denial-export-test/results.json').read_text())
    assert summary['denials'] == result['denials'] == expected
    assert result['runtime_unchanged'] and result['used']['warehouse_executions'] == 0
