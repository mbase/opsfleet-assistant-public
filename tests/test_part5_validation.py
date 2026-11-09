"""Part 5 preparation and injected transport checks; no live acceptance claims."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from opsfleet_assistant import evaluation_part5 as p5, evaluation_runner as runner
from opsfleet_assistant.config import ROOT
from opsfleet_assistant.contracts import InterpretationResult, ReportDraft
from opsfleet_assistant.evaluation_contracts import Accounting, MAX_CAPS, StopScheduling
from test_evaluation_preparation import http_transport, SETTINGS
from opsfleet_assistant.provider_contracts import encode_interpretation
from test_playbooks import spec, regional_spec, churn_spec

AREA = ROOT / 'docs/validation/part5-v1'


def manifest_path():
    return Path(os.environ['OPSFLEET_TEST_MANIFEST'])


def manifest():
    return json.loads(manifest_path().read_text())


def specification():
    return p5.validate(manifest(), verify=False)


def forbidden(*args, **kwargs):
    pytest.fail('external/credential boundary called')


def test_prepared_counts_review_blocks_and_caps():
    m = manifest()
    summary, cases = p5.plan(m, specification(), 'interpretation')
    assert len(cases) == 45 and summary['turns'] == 45
    assert summary['maximum_provider_calls'] == 240
    assert summary['maximum_warehouse_jobs'] == summary['maximum_billed_bytes'] == 0
    assert p5.environment_blocks(m, 'interpretation') == [
        'paraphrase_review_missing', 'owner_plan_approval_missing', 'provider_cost_plan_missing']
    assert 'same_snapshot_oracle_unavailable' in p5.environment_blocks(m, 'warehouse')
    assert m['source']['uncommitted'] and not m['acceptance_complete']
    assert all(c['independent_review'] == 'NOT_RUN' for c in cases)


@pytest.mark.parametrize('layer', ['interpretation', 'warehouse'])
def test_dry_paths_do_not_load_secrets_or_network(monkeypatch, capsys, layer):
    monkeypatch.setattr(p5.Settings, 'load', forbidden)
    monkeypatch.setattr(runner, '_client_from_adc', forbidden)
    monkeypatch.setattr(runner, 'execute', forbidden)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'private-sentinel-do-not-print')
    args = ['--manifest', str(manifest_path()), '--run-id', 'dry-test', '--layer', layer]
    assert p5.main(args) == 0
    assert p5.main(args + ['--dry-run', '--live']) == 0
    assert 'private-sentinel' not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        p5.main(args + ['--live'])
    with pytest.raises(SystemExit):
        p5.main(args + ['--live', '--confirm-owner-run'])


@pytest.mark.parametrize('bad', [True, -1, float('nan'), 'secret-cost', {'raw': 'private'}])
def test_manifest_rejects_unsafe_preflight_costs(bad):
    m = manifest()
    m['provider']['estimated_cost_usd'] = bad
    with pytest.raises(ValueError):
        p5.validate(m, verify=False)


def test_snapshot_all_tables_same_time_and_invalid_time():
    sql = 'SELECT * FROM `bigquery-public-data.thelook_ecommerce.order_items` i JOIN `bigquery-public-data.thelook_ecommerce.products` p ON i.product_id=p.id'
    rendered = p5.snapshot_sql(sql, '2026-11-08T01:00:00+00:00')
    assert rendered.count('FOR SYSTEM_TIME AS OF') == 2
    assert rendered.count("TIMESTAMP('2026-11-08T01:00:00+00:00')") == 2
    for timestamp in ['2026-11-08', '2026-11-08T01:00:00+03:00', "x'); SELECT secret"]:
        with pytest.raises(ValueError):
            p5.snapshot_sql(sql, timestamp)
    with pytest.raises(ValueError):
        p5.snapshot_sql('SELECT * FROM `other.dataset.table`', '2026-11-08T01:00:00Z')


@pytest.mark.parametrize('profile,factory', [('product', spec), ('regional', regional_spec), ('churn', churn_spec)])
def test_all_authored_semantic_rubrics_with_injected_http(tmp_path, monkeypatch, profile, factory):
    cases = [c for c in specification()['scenarios'] if c.get('fixture') == profile and c['layer'] == 'interpretation']
    responses = []
    for _ in cases:
        responses.extend([json.dumps(encode_interpretation(InterpretationResult(status='ready', request=factory()))), ReportDraft().model_dump_json()])
    sent = http_transport(monkeypatch, responses)
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'synthetic-rubric'}, caps=MAX_CAPS)
    records = runner.execute(account, {'scenarios': cases}, tmp_path, live=True, settings=SETTINGS)
    failures = [(r['scenario'], r['status'], [c for c in r['attempts'][0].get('assertions', []) if not c['passed']],
                 r['attempts'][0].get('observed', {}).get('safe_error_code')) for r in records if r['status'] != 'PASS']
    assert not failures, failures
    assert len(sent) == 30 and len(records) == 15
    assert account.state['used']['warehouse_executions'] == 0
    public = p5.public_result(account)
    assert len(public['cases']) == 15
    serialized = json.dumps(public)
    for key in ('response_excerpt', 'interpretations', 'confirmation', 'private-sentinel', 'secret-router-test'):
        assert key not in serialized
    # Completed and failed cases are never silently retried on resume.
    before = len(sent)
    runner.execute(account, {'scenarios': cases}, tmp_path, live=True, settings=SETTINGS)
    assert len(sent) == before


def test_failed_first_attempt_is_not_replayed_or_promoted(tmp_path, monkeypatch):
    case = next(c for c in specification()['scenarios'] if c.get('fixture') == 'product')
    sent = http_transport(monkeypatch, [403])
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'failed-case'})
    runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)
    first = deepcopy(account.state['turns'])
    assert first[0]['status'] == 'PROVIDER_FAILURE'
    runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)
    assert account.state['turns'] == first and len(sent) == 1


def test_owner_journey_with_fake_provider_and_snapshot_transport(tmp_path, monkeypatch):
    from opsfleet_assistant.contracts import RequestSpec, QuerySpec, TemporalExpression, YearExpression
    from opsfleet_assistant.evaluation_fixtures import FixtureClient
    import re
    case = next(c for c in specification()['scenarios'] if c['id'] == 'warehouse-report-lifecycle')
    case['snapshot_timestamp'] = '2026-11-08T01:00:00+00:00'
    request = RequestSpec(queries=(QuerySpec(operation='aggregate', metric='revenue', revenue_statuses=('Complete',)),),
                          analysis=TemporalExpression(kind='quarter', quarter=1, year=YearExpression(kind='absolute', value=2025)))
    sent = http_transport(monkeypatch, [json.dumps(encode_interpretation(InterpretationResult(status='ready', request=request))),
                                      ReportDraft().model_dump_json(),
                                      json.dumps(encode_interpretation(InterpretationResult(status='ready', context_action='continue', request=RequestSpec(intent='save'))))])
    snapshots = []
    class SnapshotFake(FixtureClient):
        def query(self, sql, **kwargs):
            snapshots.append(sql)
            assert 'FOR SYSTEM_TIME AS OF' in sql
            sql = re.sub(r" FOR SYSTEM_TIME AS OF TIMESTAMP\('[^']+'\)", '', sql)
            return super().query(sql, **kwargs)
    monkeypatch.setattr(runner, '_client_from_adc', lambda settings: SnapshotFake())
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'fake-journey'})
    records = runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)
    assert all(r['status'] == 'PASS' for r in records), [
        (r['turn_id'], r['status'], r['attempts'][0].get('assertions') if r['attempts'] else [], r['attempts'][0].get('observed') if r['attempts'] else {})
        for r in records if r['status'] != 'PASS']
    assert len(sent) == 3 and snapshots


@pytest.mark.parametrize('profile,factory', [('product', spec), ('regional', regional_spec), ('churn', churn_spec)])
def test_independent_warehouse_playbook_oracle_on_synthetic_snapshot(tmp_path, monkeypatch, profile, factory):
    import re
    from opsfleet_assistant.evaluation_fixtures import FixtureClient
    case = next(c for c in specification()['scenarios'] if c['id'] == 'warehouse-' + profile)
    case['snapshot_timestamp'] = '2026-11-08T01:00:00+00:00'
    http_transport(monkeypatch, [json.dumps(encode_interpretation(InterpretationResult(status='ready', request=factory()))), ReportDraft().model_dump_json()])
    class SnapshotFake(FixtureClient):
        def __init__(self):
            super().__init__(profile, snapshot=AREA / 'source_rows.json')
        def query(self, sql, **kwargs):
            assert 'FOR SYSTEM_TIME AS OF' in sql
            sql = re.sub(r" FOR SYSTEM_TIME AS OF TIMESTAMP\('[^']+'\)", '', sql)
            return super().query(sql, **kwargs)
    monkeypatch.setattr(runner, '_client_from_adc', lambda settings: SnapshotFake())
    a = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'fake-oracle'})
    rows = runner.execute(a, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)
    assert rows[0]['status'] == 'PASS', rows[0]
    assert rows[0]['attempts'][0]['observed']['independent_reference']['snapshot_pinned']
    assert 4 <= a.state['used']['warehouse_executions'] <= 5


def test_cost_reservation_and_unknown_cost_stop(tmp_path):
    a = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'cost-test'})
    observer = p5.CostObserver(a, {'maximum_cost_per_call_usd': .1, 'cost_limit_usd': .1})
    event = observer.begin('google/frozen-test')
    observer.result(event, {'model': 'google/frozen-test', 'usage': {'cost': .05}})
    with pytest.raises(StopScheduling):
        observer.begin('google/frozen-test')
    assert a.state['used']['provider_attempts'] == 1
    b = Accounting(tmp_path / 'other.json', identity={'run_id': 'unknown-cost'})
    observer = p5.CostObserver(b, {'maximum_cost_per_call_usd': .1, 'cost_limit_usd': 1})
    with pytest.raises(StopScheduling):
        observer.result(observer.begin('google/frozen-test'), {'model': 'google/frozen-test'})
    assert b.state['stop'] == 'provider_cost_unknown_or_over_reservation'


def test_cost_budget_releases_settled_reservations_and_survives_reload(tmp_path):
    path = tmp_path / 'settled.json'
    a = Accounting(path, identity={'run_id': 'settled'})
    observer = p5.CostObserver(a, {'maximum_cost_per_call_usd': .1, 'cost_limit_usd': .3})
    for _ in range(30):
        observer.result(observer.begin('google/frozen-test'),
                        {'model': 'google/frozen-test', 'usage': {'cost': .005}})
    assert a.state['used']['provider_attempts'] == 30 and a.state['stop'] is None
    a = Accounting(path, identity={'run_id': 'settled'})
    observer = p5.CostObserver(a, {'maximum_cost_per_call_usd': .1, 'cost_limit_usd': .3})
    # Thirty $.005 responses settled to $.15, not thirty $.10 reservations.
    for _ in range(2):
        observer.result(observer.begin('google/frozen-test'),
                        {'model': 'google/frozen-test', 'usage': {'cost': .05}})
    with pytest.raises(StopScheduling, match='provider_cost_reservation_cap'):
        observer.begin('google/frozen-test')
    assert a.state['used']['provider_attempts'] == 32


def test_cost_budget_keeps_inflight_reservations(tmp_path):
    a = Accounting(tmp_path / 'pending.json', identity={'run_id': 'pending'})
    observer = p5.CostObserver(a, {'maximum_cost_per_call_usd': .1, 'cost_limit_usd': .15})
    observer.begin('google/frozen-test')
    with pytest.raises(StopScheduling, match='provider_cost_reservation_cap'):
        observer.begin('google/frozen-test')
    assert a.state['used']['provider_attempts'] == 1 and len(a.state['inflight']) == 1


def test_failed_dispatch_unknown_cost_does_not_release_reservation(tmp_path):
    a = Accounting(tmp_path / 'failed.json', identity={'run_id': 'failed'})
    observer = p5.CostObserver(a, {'maximum_cost_per_call_usd': .1, 'cost_limit_usd': 3})
    observer.failed(observer.begin('google/frozen-test'))
    with pytest.raises(StopScheduling, match='provider_cost_unknown_or_over_reservation'):
        observer.begin('google/frozen-test')
    assert a.state['used']['provider_attempts'] == 1


@pytest.mark.parametrize('cost', [None, .11])
def test_cost_missing_or_over_per_call_cap_stays_stopped(tmp_path, cost):
    a = Accounting(tmp_path / 'over.json', identity={'run_id': 'over'})
    observer = p5.CostObserver(a, {'maximum_cost_per_call_usd': .1, 'cost_limit_usd': 3})
    with pytest.raises(StopScheduling, match='provider_cost_unknown_or_over_reservation'):
        observer.result(observer.begin('google/frozen-test'),
                        {'model': 'google/frozen-test', 'usage': {'cost': cost}})
    with pytest.raises(StopScheduling):
        observer.begin('google/frozen-test')
    assert a.state['used']['provider_attempts'] == 1


def test_public_projection_keeps_unknown_billing(tmp_path):
    a = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'unknown-bill'})
    a.begin_turn('test-case')
    event = a.reserve('warehouse', cap=10)
    a.finish(event, billed=None)
    a.state['turns'] = [{'scenario': 'test-scenario', 'turn_id': 'test-case', 'status': 'FAIL',
                         'mode': 'live_bigquery', 'attempts': [], 'expected': []}]
    projected = p5.public_result(a)
    assert projected['cases'][0]['billed_bytes'] is None


def test_offline_environment_strips_selectors_and_credentials(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / 'scripts'))
    from offline_check import isolated_env
    for key in ('GOOGLE_APPLICATION_CREDENTIALS', 'OPENROUTER_API_KEY', 'GOOGLE_CLOUD_PROJECT', 'GEMINI_API_KEY', 'PYTHONPATH', 'OPSFLEET_LIVE_TESTS'):
        monkeypatch.setenv(key, 'sensitive-sentinel')
    env = isolated_env(tmp_path)
    assert env['OPSFLEET_LIVE_TESTS'] == '0'
    assert all(v != 'sensitive-sentinel' for v in env.values())


def test_lock_metadata_and_historical_decision():
    path = ROOT / 'scripts/prepare_part5_validation.py'
    spec = importlib.util.spec_from_file_location('p5prepare', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    audit = module.lock_audit() if (ROOT / 'uv.lock').exists() else json.loads((AREA / 'lock_audit.json').read_text())
    assert all(audit[key] for key in ('metadata_matches', 'python_matches', 'editable_source', 'version_matches'))
    assert audit['decision'] == 'RETAIN_UNTOUCHED_NOT_ADOPTED'
    assert audit['only_uv_pins'] and audit['only_accepted_pins']


def test_explicit_env_file_overrides_stale_exports_without_shell_execution(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    marker = tmp_path / 'must-not-execute'
    path.write_text('OPENROUTER_API_KEY="file-only-secret"\n'
                    'OPENROUTER_MODEL="google/gemini-3.8-flash"\n'
                    'GOOGLE_CLOUD_PROJECT=synthetic-project\nBQ_LOCATION=US\n'
                    'UNRELATED=$(touch ' + str(marker) + ')\n')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'stale-shell-secret')
    monkeypatch.setenv('OPENROUTER_MODEL', 'wrong/stale-model')
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'wrong-project')
    m = manifest()
    m['warehouse']['project'] = 'synthetic-project'
    settings = p5.load_live_settings(m, 'warehouse', path)
    assert settings.openrouter_api_key == 'file-only-secret'
    assert settings.openrouter_model == m['provider']['model'] and settings.project == 'synthetic-project'
    assert not marker.exists()
    assert 'file-only-secret' not in repr(settings)
    assert p5.os.environ['OPENROUTER_API_KEY'] == 'stale-shell-secret'


def test_env_file_can_omit_model_and_location_but_not_substitute_gemini_key(tmp_path, monkeypatch):
    for key in ('OPENROUTER_API_KEY', 'OPENROUTER_MODEL', 'BQ_LOCATION'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / '.env'
    path.write_text('OPENROUTER_API_KEY=file-only-secret\n')
    settings = p5.load_live_settings(manifest(), 'interpretation', path)
    assert settings.openrouter_model == manifest()['provider']['model'] and settings.location == 'US'
    path.write_text('GEMINI_API_KEY=wrong-service-secret\n')
    with pytest.raises(p5.LiveConfigurationError, match='^openrouter_api_key_missing$'):
        p5.load_live_settings(manifest(), 'interpretation', path)


@pytest.mark.parametrize('failure', ['missing', 'unreadable', 'model', 'project'])
def test_env_configuration_errors_are_fixed_codes(tmp_path, monkeypatch, failure):
    path = tmp_path / '.env'
    path.write_text('OPENROUTER_API_KEY=never-print-secret\n')
    m = manifest()
    if failure == 'missing':
        path.unlink()
    elif failure == 'unreadable':
        def denied(*args):
            raise OSError('never-print-secret')
        monkeypatch.setattr(p5.config, '_read_dotenv', denied)
    elif failure == 'model':
        path.write_text('OPENROUTER_API_KEY=never-print-secret\nOPENROUTER_MODEL=unreviewed/model\n')
    else:
        path.write_text('OPENROUTER_API_KEY=never-print-secret\nGOOGLE_CLOUD_PROJECT=wrong-project\n')
    with pytest.raises(p5.LiveConfigurationError) as exc:
        p5.load_live_settings(m, 'warehouse' if failure == 'project' else 'interpretation', path)
    assert str(exc.value) == {'missing': 'env_file_unavailable', 'unreadable': 'env_file_unreadable',
                             'model': 'openrouter_model_mismatch', 'project': 'warehouse_environment_incomplete'}[failure]
    assert 'never-print-secret' not in str(exc.value)


def test_env_file_help_dry_and_unapproved_modes_never_read_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(p5.config, '_read_dotenv', forbidden)
    monkeypatch.setattr(p5, 'load_live_settings', forbidden)
    monkeypatch.setattr(runner, 'execute', forbidden)
    args = ['--manifest', str(manifest_path()), '--run-id', 'env-dry-check',
            '--layer', 'interpretation', '--env-file', str(tmp_path / 'not-readable.env')]
    assert p5.main(args) == 0
    assert p5.main(args + ['--live', '--dry-run']) == 0
    for flags in (['--help'], ['--live'], ['--live', '--confirm-owner-run']):
        with pytest.raises(SystemExit):
            p5.main(args + flags)
    assert 'secret' not in capsys.readouterr().out


def test_confirmed_fake_run_uses_env_file_and_prints_final_outcome(tmp_path, monkeypatch, capsys):
    import tempfile
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.delenv('OPENROUTER_MODEL', raising=False)
    path = tmp_path / '.env'
    path.write_text('OPENROUTER_API_KEY=synthetic-env-secret\n')
    reviewed = manifest()
    reviewed['review'].update(owner_approved=True, paraphrases_independently_reviewed=True)
    reviewed['provider'].update(estimated_cost_usd=.2, cost_limit_usd=1, maximum_cost_per_call_usd=.1)
    selected = tmp_path / 'approved.json'
    selected.write_text(json.dumps(reviewed))
    output = tmp_path / 'output'
    output.mkdir()
    (output / 'source_rows.json').write_bytes((AREA / 'source_rows.json').read_bytes())
    monkeypatch.setattr(p5, 'AREA', output)
    def fake_execute(account, cases, private, *, live, settings, selection):
        assert live and settings.openrouter_api_key == 'synthetic-env-secret'
        account.state['turns'] = [{'scenario': 'product-p1-r1', 'turn_id': 'product-p1-r1-interpret',
            'mode': 'live_fixture', 'status': 'PROVIDER_FAILURE', 'attempts': [], 'expected': []}]
        account.save()
    monkeypatch.setattr(runner, 'execute', fake_execute)
    with tempfile.TemporaryDirectory(prefix='opsfleet-env-fake-') as directory:
        state = Path(directory).resolve() / 'new-state'
        assert p5.main(['--manifest', str(selected), '--run-id', 'env-fake-live', '--layer', 'interpretation',
            '--select', 'product-p1-r1', '--state', str(state), '--env-file', str(path),
            '--live', '--confirm-owner-run']) == 1
    captured = capsys.readouterr()
    assert 'synthetic-env-secret' not in captured.out + captured.err
    final = json.loads(captured.out.splitlines()[-1])
    assert final['phase'] == 'results_saved'
    assert final['selected_statuses'] == {'product-p1-r1-interpret': 'PROVIDER_FAILURE'}
    assert Path(final['results_path']).is_file()


def test_near_zero_reference_has_no_component_percentages():
    from decimal import Decimal
    from test_playbooks import released, product_plan, product_rows
    from opsfleet_assistant.playbooks import investigate
    finding = investigate((released(product_plan(), product_rows(ra='600.00000000000001', rb='600')),))
    assert finding.total_difference == Decimal('0.00000000000001')
    assert [c.value for c in finding.contributions] == [Decimal('450.0000000000000025'), Decimal('-449.9999999999999925')]
    assert all(c.percentage is None for c in finding.contributions)


@pytest.mark.parametrize('profile,factory', [('product', spec), ('regional', regional_spec), ('churn', churn_spec)])
@pytest.mark.parametrize('extra_first', [False, True])
def test_p5_r1_extra_query_fails_with_or_without_workflow_repair(tmp_path, monkeypatch, profile, factory, extra_first):
    from opsfleet_assistant.contracts import QuerySpec
    case = next(c for c in specification()['scenarios'] if c['layer'] == 'interpretation' and c['fixture'] == profile)
    good = factory()
    extra = (QuerySpec(operation='aggregate', metric='revenue'),)
    bad = good.model_copy(update={'queries': extra + good.queries if extra_first else good.queries + extra})
    sent = http_transport(monkeypatch, [json.dumps(encode_interpretation(InterpretationResult(status='ready', request=bad))),
        json.dumps(encode_interpretation(InterpretationResult(status='ready', request=good))), ReportDraft().model_dump_json()])
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'first-extra'})
    rows = runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)
    row = rows[0]
    observed = row['attempts'][0]['observed']
    clarified = extra_first and profile in {'product', 'regional'}
    expected_status, expected_calls = ('needs_clarification', 1) if clarified else ('ok', 3)
    assert observed['status'] == expected_status and len(sent) == expected_calls
    assert len(observed['interpretations']) == (1 if clarified else 2) and observed['first_query_count'] == 2
    assert row['status'] == 'FAIL' and row['attempts'][0]['first_semantic_status'] == 'FAIL'
    failed_paths = {c['rule']['path'] for c in row['attempts'][0]['assertions'] if not c['passed']}
    assert {'first_query_count', 'first_operations', 'first_request'}.issubset(failed_paths)
    public = p5.public_result(account)['cases'][0]['attempts'][0]
    assert public['first_semantic_status'] == 'FAIL' and public['workflow_status'] == expected_status
    previous = deepcopy(rows)
    runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)
    assert account.state['turns'] == previous and len(sent) == expected_calls


def test_p5_r1_unrequested_report_choices_fail_first_request_rubric(tmp_path, monkeypatch):
    from opsfleet_assistant.contracts import ReportSpec
    case = next(c for c in specification()['scenarios'] if c['layer'] == 'interpretation' and c['fixture'] == 'product')
    bad = spec().model_copy(update={'report': ReportSpec()})
    http_transport(monkeypatch, [json.dumps(encode_interpretation(InterpretationResult(status='ready', request=bad))),
                                ReportDraft().model_dump_json()])
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'extra-effect'})
    row = runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)[0]
    assert row['status'] == 'FAIL'
    assert any(c['rule']['path'] == 'first_request' and not c['passed'] for c in row['attempts'][0]['assertions'])


@pytest.mark.parametrize('profile,factory', [('product', spec), ('regional', regional_spec), ('churn', churn_spec)])
def test_semantically_inert_playbook_query_defaults_do_not_fail_review(tmp_path, monkeypatch, profile, factory):
    case = next(c for c in specification()['scenarios'] if c['layer'] == 'interpretation' and c['fixture'] == profile)
    request = factory()
    changes = {'limit': 2 if profile != 'regional' else 20}
    if profile == 'regional':
        changes['inactivity_days'] = 30
    request = request.model_copy(update={'queries': (request.queries[0].model_copy(update=changes),)})
    # Some prototype plan guards repair inert defaults. Return the contract
    # actually requested by each fake dispatch rather than feeding report JSON
    # into a repair interpretation and creating a fake provider failure.
    from types import SimpleNamespace
    from opsfleet_assistant import model_gateway
    sent = []
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def post(self, url, *, headers, json):
            first = not sent
            sent.append(json)
            interpreting = 'request' in json['response_format']['json_schema']['schema']['properties']
            value = (InterpretationResult(status='ready', request=request if first else factory())
                     if interpreting else ReportDraft())
            payload = {'model': SETTINGS.openrouter_model, 'usage': {'cost': .001},
                       'choices': [{'message': {'content': __import__('json').dumps(encode_interpretation(value)) if interpreting else value.model_dump_json()}}]}
            return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(model_gateway.httpx, 'Client', Client)
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'inert-defaults'})
    row = runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)[0]
    assert row['status'] == 'PASS', row
    assert row['attempts'][0]['first_semantic_status'] == 'PASS'
    assert len(sent) == (2 if profile == 'product' else 3)


@pytest.mark.parametrize('status,issue', [('unsupported', 'unsupported_capability'),
                                        ('needs_clarification', 'missing_period')])
def test_requestless_interpretation_preserves_semantic_failure(tmp_path, monkeypatch, status, issue):
    case = next(c for c in specification()['scenarios'] if c['id'] == 'product-p1-r1')
    response = InterpretationResult(status=status, request=None, issues=(issue,))
    calls = http_transport(monkeypatch, [json.dumps(encode_interpretation(response))])
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'requestless'})
    row = runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)[0]
    attempt = row['attempts'][0]
    assert row['status'] == 'FAIL' and attempt['first_semantic_status'] == 'FAIL'
    assert attempt['workflow_status'] == status
    observed = attempt['observed']
    assert observed['status'] == status and len(observed['interpretations']) == 1
    assert observed['interpretations'][0]['issues'] == [issue]
    assert observed['first_request'] is None and observed['first_periods'] is None
    assert observed['first_query_count'] == 0 and observed['first_operations'] == []
    assert observed['analysis_queries'] == 0 and len(calls) == 1
    assert observed.get('category') != 'safe_execution_exception'
    failed = {c['rule']['path'] for c in attempt['assertions'] if not c['passed']}
    assert {'status', 'first_request', 'first_query_count', 'first_operations'}.issubset(failed)
    public = p5.public_result(account)['cases'][0]['attempts'][0]
    assert public['workflow_status'] == status and public['first_semantic_status'] == 'FAIL'


def test_semantic_churn_inactivity_window_is_still_required(tmp_path, monkeypatch):
    case = next(c for c in specification()['scenarios'] if c['layer'] == 'interpretation' and c['fixture'] == 'churn')
    request = churn_spec()
    request = request.model_copy(update={'queries': (request.queries[0].model_copy(update={'inactivity_days': 30}),)})
    http_transport(monkeypatch, [json.dumps(encode_interpretation(InterpretationResult(status='ready', request=request))),
        json.dumps(encode_interpretation(InterpretationResult(status='ready', request=churn_spec()))), ReportDraft().model_dump_json()])
    account = Accounting(tmp_path / 'checkpoint.json', identity={'run_id': 'changed-window'})
    row = runner.execute(account, {'scenarios': [case]}, tmp_path, live=True, settings=SETTINGS)[0]
    assert row['status'] == 'FAIL'
    assert any(c['rule']['path'] == 'first_request' and not c['passed'] for c in row['attempts'][0]['assertions'])


def copied_frozen_tree(tmp_path, *, git=False):
    import shutil
    import subprocess
    destination = tmp_path / 'copy'
    if git and (ROOT / '.git').exists():
        # Local clone only: no network, new commits or original-repo changes.
        subprocess.run(['git', 'clone', '--local', '--no-hardlinks', str(ROOT), str(destination)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        destination.mkdir()
    for relative in manifest()['source']['files']:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    if git and (destination / '.git').exists():
        # A local clone contains HEAD, but not newly added index entries. Match
        # their tracking state in the disposable clone so `git diff HEAD` has
        # the same identity as the source checkout, including staged additions.
        original_index = set(subprocess.check_output(
            ['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0'))
        copied_index = set(subprocess.check_output(
            ['git', 'ls-files', '-z'], cwd=destination).decode().split('\0'))
        additions = sorted((original_index - copied_index) & set(manifest()['source']['files']))
        if additions:
            subprocess.run(['git', 'add', '--', *additions], cwd=destination, check=True)
    return destination


@pytest.mark.parametrize('git', [False, True])
@pytest.mark.parametrize('addition', ['src/opsfleet_assistant/playbooks/product_revenue/__init__.py',
    'src/opsfleet_assistant/untracked_helper.py', 'config/new-policy.json', 'tests/new-test.json',
    'conftest.py', 'pytest.ini', 'setup.cfg', 'opsfleet_assistant/__init__.py', 'root_hook.pth',
    'src/opsfleet_assistant/runtime/__init__.py', 'src/build/conftest.py'])
def test_p5_r2_added_inputs_rejected_in_copy_and_git_tree(tmp_path, monkeypatch, addition, git):
    from opsfleet_assistant.evaluation_inventory import verify_inventory
    root = copied_frozen_tree(tmp_path, git=git)
    verify_inventory(root, manifest()['source'])
    target = root / addition
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('synthetic input identity sentinel\n')
    if git and (root / '.git').is_dir() and addition.endswith('untracked_helper.py'):
        import subprocess
        with (root / '.git/info/exclude').open('a') as stream:
            stream.write('\n/' + addition + '\n')
        untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=root, text=True)
        assert addition not in untracked  # Filesystem verification must still reject it.
    with pytest.raises(ValueError, match='input_inventory_mismatch'):
        verify_inventory(root, manifest()['source'])
    monkeypatch.setattr(p5, 'ROOT', root)
    monkeypatch.setattr(p5, 'AREA', root / 'docs/validation/part5-v1')
    with pytest.raises(ValueError, match='input_inventory_mismatch'):
        p5.validate(manifest())


@pytest.mark.parametrize('change', ['missing', 'modified', 'symlink'])
def test_p5_r2_existing_input_drift_rejected(tmp_path, change):
    from opsfleet_assistant.evaluation_inventory import verify_inventory
    root = copied_frozen_tree(tmp_path)
    target = root / 'config/demo_users.json'
    if change == 'missing':
        target.unlink()
    elif change == 'modified':
        target.write_text('{}\n')
    else:
        target.unlink()
        target.symlink_to(ROOT / 'config/demo_users.json')
    with pytest.raises(ValueError):
        verify_inventory(root, manifest()['source'])


def test_p5_r2_outputs_history_and_caches_are_intentional_exclusions(tmp_path):
    from opsfleet_assistant.evaluation_inventory import verify_inventory
    root = copied_frozen_tree(tmp_path)
    for relative in ['README.md', 'docs/PART_5_REVIEW.md', 'docs/validation/part5-v1/results/new.json',
                     'docs/validation/part5-v1/history/old.json', 'src/opsfleet_assistant/__pycache__/cache.pyc',
                     'src/opsfleet_assistant.egg-info/PKG-INFO']:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('excluded synthetic narrative/cache\n')
    assert verify_inventory(root, manifest()['source']) == manifest()['source']['revision']


def test_p5_r2_recorded_git_diff_is_verified(monkeypatch):
    from opsfleet_assistant.evaluation_inventory import verify_inventory
    if not (ROOT / '.git').exists():
        return  # Clean-copy path is covered independently without Git metadata.
    bad = deepcopy(manifest()['source'])
    bad['tracked_application_diff_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='tracked_application_diff_changed'):
        verify_inventory(ROOT, bad)


@pytest.mark.parametrize('mode', ['default', 'dry', 'help'])
def test_p5_r2_terminal_rejects_shadow_package_before_application_import(tmp_path, mode):
    import subprocess
    root = copied_frozen_tree(tmp_path)
    manifest_path = root / 'docs/validation/part5-v1/candidate_manifest.json'
    manifest_path.write_text(json.dumps(manifest()))
    package = root / 'src/opsfleet_assistant/playbooks/product_revenue/__init__.py'
    package.parent.mkdir()
    marker = tmp_path / 'unexpected-import'
    package.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("imported")\n')
    result = subprocess.run([sys.executable, str(root / 'scripts/run_live_evaluation.py'),
        '--manifest', str(manifest_path), '--run-id', 'shadow-check', '--layer', 'interpretation',
        *({'default': [], 'dry': ['--dry-run'], 'help': ['--help']}[mode])],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if mode == 'help':
        assert result.returncode == 0 and b'--manifest' in result.stdout
    else:
        assert result.returncode == 2 and b'invalid_frozen_input_inventory' in result.stderr
    assert not marker.exists()


def test_temporary_identity_has_no_approval_or_acceptance_grants():
    m = manifest()
    assert m['status'] == 'LOCAL_TEST_IDENTITY'
    assert not m['acceptance_complete']
    assert m['review'] == {'owner_approved': False, 'paraphrases_independently_reviewed': False}
    assert not m['warehouse']['same_snapshot_authorized']
    assert all(m['provider'][k] is None for k in
               ('estimated_cost_usd', 'cost_limit_usd', 'maximum_cost_per_call_usd'))
    assert manifest_path().resolve().is_relative_to(Path(__import__('tempfile').gettempdir()).resolve())


@pytest.mark.parametrize('key', ['application_head', 'application_tree'])
def test_current_identity_still_rejects_mismatched_git_identity(key):
    from opsfleet_assistant.evaluation_inventory import verify_inventory
    if not (ROOT / '.git').exists():
        pytest.skip('Git metadata absent; full source inventory remains verified')
    source = deepcopy(manifest()['source'])
    source[key] = '0' * 40
    with pytest.raises(ValueError, match='application_head_or_tree_changed'):
        verify_inventory(ROOT, source)


def test_historical_candidate_still_rejects_actual_source_drift():
    from opsfleet_assistant.evaluation_inventory import verify_inventory
    archived = json.loads((AREA / 'candidate_manifest.json').read_text())
    assert archived['source']['revision'] != manifest()['source']['revision']
    with pytest.raises(ValueError):
        verify_inventory(ROOT, archived['source'])


@pytest.mark.parametrize('key', ['fixture_snapshot_sha256', 'metadata_sha256', 'driver_sha256', 'dependency_lock_sha256'])
def test_current_identity_still_rejects_altered_frozen_fields(key):
    bad = manifest()
    bad[key] = '0' * 64
    with pytest.raises(ValueError, match='frozen_field_mismatch'):
        p5.validate(bad)
