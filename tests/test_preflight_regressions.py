"""Regression contracts for shareable diagnostics and partial warehouse preflight."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opsfleet_assistant import preflight
from opsfleet_assistant.config import Settings


KEY = 'sk-or-v1-' + 'a' * 64  # Synthetic, deliberately accepted by metadata regexes.
SCHEMA = {
    'orders': ['order_id', 'user_id', 'status', 'created_at'],
    'order_items': ['order_id', 'product_id', 'status', 'created_at'],
    'products': ['id', 'brand', 'created_at'],
    'users': ['id', 'created_at'],
}
SETTINGS = Settings('synthetic-project', 'US', 'unused', openrouter_api_key=KEY)


@pytest.fixture(autouse=True)
def no_live_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Regression attempted real network or credential access')
    monkeypatch.setattr('socket.getaddrinfo', forbidden)
    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr(Settings, 'load', forbidden)


@pytest.fixture
def terminal():
    spec = importlib.util.spec_from_file_location(
        'regression_terminal', Path(__file__).parents[1] / 'scripts/openrouter_terminal_diagnostic.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    status = 200

    def __init__(self, payload, headers):
        self.payload, self.headers = payload, headers

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size): return json.dumps(self.payload).encode()[:size]


@pytest.mark.parametrize('field', [
    'id', 'model', 'provider', 'finish_reason', 'native_finish_reason',
    'x-request-id', 'openrouter-request-id',
])
@pytest.mark.parametrize('embedded', [False, True])
def test_terminal_echoed_credentials_never_enter_shareable_output(
        terminal, monkeypatch, tmp_path, capsys, field, embedded):
    echo = f'x-{KEY}-y' if embedded else KEY
    payload = {'id': 'safe-id', 'model': terminal.MODEL, 'provider': 'Google',
               'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop',
                            'native_finish_reason': 'STOP'}]}
    headers = {}
    if field.endswith('request-id'):
        headers[field] = echo
    elif field in {'finish_reason', 'native_finish_reason'}:
        payload['choices'][0][field] = echo
    else:
        payload[field] = echo
    calls = []
    def dispatch(request, timeout):
        calls.append(json.loads(request.data)['max_tokens'])
        assert timeout == 30
        return Response(payload, headers)
    opener = SimpleNamespace(open=dispatch)
    record = terminal.request_case(KEY, 64, opener)
    assert KEY not in json.dumps(record)
    monkeypatch.setenv('OPENROUTER_API_KEY', KEY)
    monkeypatch.setattr(terminal, 'make_output_dir', lambda: tmp_path)
    monkeypatch.setattr(terminal, 'dns_check', lambda: {'status': 'resolved'})
    monkeypatch.setattr(terminal, 'build_opener', lambda *args: opener)
    terminal.main()
    assert calls == [64, 10, 64]
    output = capsys.readouterr()
    assert KEY not in output.out and KEY not in output.err
    for name in ['results.json', 'REPORT.md']:
        assert KEY not in (tmp_path / name).read_text()
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600


class Job:
    def __init__(self, number, *, dry=False, unknown_billing=False, failure=False,
                 cancel_failure=False, missing_schema_column=None):
        self.total_bytes_processed = 100
        self.total_bytes_billed = None if unknown_billing else 100
        self.job_id = f'query-{number}'
        self.number, self.dry, self.failure = number, dry, failure
        self.cancel_failure = cancel_failure
        self.missing_schema_column = missing_schema_column
        self.cancel_calls = []

    def result(self, **kwargs):
        assert kwargs == dict(timeout=35, max_results=150, retry=None, job_retry=None)
        if self.failure:
            raise httpx.ReadTimeout('synthetic-private-result-error')
        if self.number == 1:
            return [{'table_name': table, 'column_name': column}
                    for table, columns in SCHEMA.items() for column in columns
                    if (table, column) != self.missing_schema_column]
        return []

    def cancel(self, **kwargs):
        self.cancel_calls.append(kwargs)
        if self.cancel_failure:
            raise RuntimeError('synthetic-private-cancellation-error')
        return True


class Client:
    def __init__(self, *, scan_limit_at=None, dry_failure_at=None, submit_failure_at=None,
                 result_failure_at=None, unknown_billing_at=None, cancel_failure=False,
                 missing_schema_column=None):
        self.scan_limit_at, self.dry_failure_at = scan_limit_at, dry_failure_at
        self.submit_failure_at, self.result_failure_at = submit_failure_at, result_failure_at
        self.unknown_billing_at, self.cancel_failure = unknown_billing_at, cancel_failure
        self.missing_schema_column = missing_schema_column
        self.calls, self.jobs = [], []
        self.number = 0

    def query(self, sql, *, job_config, **kwargs):
        assert kwargs['retry'] is None and kwargs['job_retry'] is None
        assert kwargs['timeout'] == 20
        assert int(job_config.maximum_bytes_billed) == preflight.MAX_QUERY_BYTES
        dry = bool(job_config.dry_run)
        if dry: self.number += 1
        self.calls.append((self.number, dry))
        if dry and self.number == self.dry_failure_at:
            raise httpx.ReadTimeout('synthetic-private-dry-error')
        if not dry:
            assert int(job_config.job_timeout_ms) == 30_000
            if self.number == self.submit_failure_at:
                raise httpx.ReadTimeout('synthetic-private-submission-error')
        job = Job(self.number, dry=dry, unknown_billing=self.number == self.unknown_billing_at,
                  failure=self.number == self.result_failure_at,
                  cancel_failure=self.cancel_failure,
                  missing_schema_column=self.missing_schema_column)
        if dry and self.number == self.scan_limit_at:
            job.total_bytes_processed = preflight.MAX_QUERY_BYTES + 1
        if not dry: self.jobs.append(job)
        return job


@pytest.mark.parametrize('check_number', [2, 4, 8], ids=['status', 'date', 'join'])
@pytest.mark.parametrize('command', ['--bigquery', '--all'])
def test_incomplete_subcheck_prevents_cli_success(monkeypatch, capsys, check_number, command):
    client = Client(scan_limit_at=check_number)
    monkeypatch.setattr('google.cloud.bigquery.Client', lambda **kwargs: client)
    monkeypatch.setattr(Settings, 'load', lambda: SETTINGS)
    monkeypatch.setattr(preflight, 'gemini_preflight', lambda settings: {'status': 'ok'})
    assert preflight.main([command]) == 1
    result = json.loads(capsys.readouterr().out)['bigquery']
    assert result['status'] != 'ok'
    assert 'scan_limit' in json.dumps(result)


@pytest.mark.parametrize('command', ['--bigquery', '--all'])
def test_complete_preflight_preserves_success_and_accounts_all_jobs(monkeypatch, capsys, command):
    client = Client()
    monkeypatch.setattr('google.cloud.bigquery.Client', lambda **kwargs: client)
    monkeypatch.setattr(Settings, 'load', lambda: SETTINGS)
    monkeypatch.setattr(preflight, 'gemini_preflight', lambda settings: {'status': 'ok'})
    assert preflight.main([command]) == 0
    result = json.loads(capsys.readouterr().out)['bigquery']
    assert result['status'] == 'ok'
    assert len(client.calls) == 16
    assert result['accounting']['execution_attempts'] == 8
    assert result['accounting']['billed_bytes'] == 800
    assert result['accounting']['known_billed_bytes'] == 800


@pytest.mark.parametrize('column', [('orders','status'), ('products','created_at'), ('users','id')])
def test_missing_required_schema_columns_prevent_success(monkeypatch, column):
    client = Client(missing_schema_column=column)
    monkeypatch.setattr('google.cloud.bigquery.Client', lambda **kwargs: client)
    result = preflight.bigquery_preflight(SETTINGS)
    assert result['status'] != 'ok'


@pytest.mark.parametrize('failure', ['dry', 'submission', 'result', 'unknown_billing'])
def test_partial_preflight_retains_jobs_and_stops_unresolved_work(monkeypatch, failure):
    options = {'dry': {'dry_failure_at': 2}, 'submission': {'submit_failure_at': 2},
               'result': {'result_failure_at': 2}, 'unknown_billing': {'unknown_billing_at': 2}}
    client = Client(**options[failure])
    monkeypatch.setattr('google.cloud.bigquery.Client', lambda **kwargs: client)
    result = preflight.bigquery_preflight(SETTINGS)
    assert result['status'] != 'ok'
    assert result['schema']['job_id'] == 'query-1'
    assert result['schema']['billed_bytes'] == 100
    assert max(number for number, dry in client.calls) == 2
    accounting = result['accounting']
    assert accounting['known_billed_bytes'] == 100
    assert accounting['execution_attempts'] == (1 if failure == 'dry' else 2)
    assert accounting['billed_bytes'] == (100 if failure == 'dry' else None)
    assert 'synthetic-private-' not in json.dumps(result)
    if failure in {'result', 'unknown_billing'}:
        assert result['statuses']['orders']['job_id'] == 'query-2'


@pytest.mark.parametrize('billed', [None, 0])
def test_unknown_and_zero_billing_remain_distinct(billed):
    client = Client(unknown_billing_at=1)
    query = client.query
    def transport(sql, **kwargs):
        job = query(sql, **kwargs)
        if not kwargs['job_config'].dry_run: job.total_bytes_billed = billed
        return job
    result = preflight._run_query(SimpleNamespace(query=transport), 'SELECT 1', 'US')
    assert result['billed_bytes'] is billed
    assert (result['status'] == 'ok') is (billed == 0)


def test_cancellation_failure_does_not_replace_original_query_error():
    client = Client(result_failure_at=1, cancel_failure=True)
    result = preflight._run_query(client, 'SELECT 1', 'US')
    assert result['status'] == 'request_failed'
    assert result['error_type'] == 'ReadTimeout'
    assert result['job_id'] == 'query-1'
    assert result['billed_bytes'] is None
    assert result['cancellation_status'] == 'failed'
    assert client.jobs[0].cancel_calls == [dict(retry=None, timeout=20)]
    assert 'synthetic-private-' not in json.dumps(result)


def test_unknown_schema_billing_stops_before_other_checks(monkeypatch):
    client = Client(unknown_billing_at=1)
    monkeypatch.setattr('google.cloud.bigquery.Client', lambda **kwargs: client)
    result = preflight.bigquery_preflight(SETTINGS)
    assert result['status'] != 'ok'
    assert result['failed_check'] == 'schema'
    assert client.calls == [(1, True), (1, False)]
    assert result['schema']['job_id'] == 'query-1'
    assert result['schema']['billing_status'] == 'unknown'
    assert result['accounting']['billed_bytes'] is None
    assert result['accounting']['known_billed_bytes'] == 0


@pytest.mark.parametrize('estimate', [None, True, -1])
def test_unusable_dry_run_estimate_never_submits_execution(estimate):
    calls = []
    def transport(sql, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(total_bytes_processed=estimate)
    result = preflight._run_query(SimpleNamespace(query=transport), 'SELECT 1', 'US')
    assert result['status'] != 'ok'
    assert len(calls) == 1 and calls[0]['job_config'].dry_run
    assert result['execution_attempts'] == 0
