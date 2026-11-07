"""Offline boundary tests; injected server behavior is independent of recovery policy."""
import json
import pytest
from google.api_core import exceptions as ge
from opsfleet_assistant.recovery import FailureContext, SubmissionState as S, decide_recovery
from opsfleet_assistant.bigquery_adapter import ExecutionError
from opsfleet_assistant.telemetry import TurnTrace, agent_metrics
from test_step3 import turn, base_stages, plan


@pytest.mark.parametrize('state', [S.SUBMITTED, S.UNKNOWN])
def test_missing_or_unsafe_submission_never_replays(state):
    assert decide_recovery(FailureContext('provider_transient', 'dry_run', state, 0, True, True)).action == 'stop'


@pytest.mark.parametrize('category', ['permission_denied', 'unauthorized_scope', 'billing_unknown', 'query_schema_error', 'query_syntax_error'])
def test_nontransient_stops(category):
    assert decide_recovery(FailureContext(category, 'dry_run', S.NOT_SUBMITTED, 0, True, True)).action == 'stop'


def test_server_accepts_submission_then_transport_breaks_without_counter_evidence():
    runner, client = turn(base_stages())
    original = client.query
    submissions = []
    def query(sql, **kw):
        if not kw['job_config'].dry_run:
            submissions.append(sql)
            runner.executions = 0  # Deliberately unreliable local accounting.
            raise ge.ServiceUnavailable('private SQL and identity')
        return original(sql, **kw)
    client.query = query
    with pytest.raises(ExecutionError) as caught:
        runner.run(plan(), 'manager_apparel')
    assert len(submissions) == 1 and runner.retries == 0
    assert caught.value.submission_state == S.UNKNOWN
    assert caught.value.billed_bytes is None and runner.billing_unknown


@pytest.mark.parametrize('billed', [None, 17])
def test_submitted_timeout_preserves_job_and_billing(billed):
    stages = base_stages()
    stages[0][1].failure = TimeoutError('private')
    stages[0][1].total_bytes_billed = billed
    runner, client = turn(stages)
    with pytest.raises(ExecutionError) as caught:
        runner.run(plan(), 'manager_apparel')
    assert caught.value.submission_state == S.SUBMITTED
    assert caught.value.job_id == 'catalog_job' and caught.value.billed_bytes == billed
    assert len(client.calls) == 2 and runner.retries == 0


@pytest.mark.parametrize('limit', ['executions', 'dry_runs', 'bytes', 'deadline'])
def test_exhaustion_prevents_dispatch(limit):
    runner, client = turn(base_stages())
    if limit == 'executions': runner.executions = 8
    if limit == 'dry_runs': runner.dry_runs = 12
    if limit == 'bytes': runner.billed_bytes = runner.limits.per_turn_bytes
    if limit == 'deadline': runner._clock = lambda: runner._started + 121
    with pytest.raises(ExecutionError): runner.run(plan(), 'manager_apparel')
    assert not client.calls


def test_transient_retry_success_only_after_release_and_safe_telemetry(tmp_path):
    runner, client = turn(base_stages())
    path = tmp_path/'events.jsonl'
    runner.telemetry = TurnTrace('c', 't', path)
    original = client.query
    failed = False
    def query(sql, **kw):
        nonlocal failed
        if not failed:
            failed = True
            raise ge.ServiceUnavailable('secret SQL parameters PII')
        assert 'recovery.outcome.success' not in agent_metrics(path)['counts']
        return original(sql, **kw)
    client.query = query
    assert runner.run(plan(), 'manager_apparel').complete
    counts = agent_metrics(path)['counts']
    assert counts['recovery.outcome.attempted'] == counts['recovery.outcome.success'] == 1
    assert 'secret' not in path.read_text()


@pytest.mark.parametrize('json_output', [False, True])
def test_chat_continues_after_failed_turn(tmp_path, monkeypatch, json_output):
    from typer.testing import CliRunner
    from opsfleet_assistant import cli
    from opsfleet_assistant.workflow import TurnResult
    calls = []
    class Workflow:
        def __init__(self, *args): pass
        def run(self, owner, question, **kw):
            calls.append(question)
            return TurnResult('error' if len(calls) == 1 else 'ok',
                              'Controlled failure' if len(calls) == 1 else 'Next turn works', 'c', 't')
    monkeypatch.setattr(cli, 'AssistantWorkflow', Workflow)
    result = CliRunner().invoke(cli.app, ['chat', '--user', 'manager_apparel', '--db', str(tmp_path/'state.sqlite')]
                                + (['--json'] if json_output else []), input='first\nsecond\n/exit\n')
    assert result.exit_code == 0 and calls == ['first', 'second']
    assert 'Controlled failure' in result.output and 'Next turn works' in result.output
    if json_output:
        assert [json.loads(line)['status'] for line in result.output.splitlines()] == ['error', 'ok']


def test_coverage_inconsistency_stops_without_widening(tmp_path):
    from test_step6 import workflow
    from test_step4 import FakeTurn
    class Inconsistent(FakeTurn):
        seen = []
        def execute_compiled(self, query):
            self.seen.append(query)
            item = super().execute_compiled(query)
            assert item.coverage.period_rows > 0
            return item.model_copy(update={'rows': ()})
    result = workflow(tmp_path, turn=Inconsistent).run('manager_apparel', 'Revenue in 2025')
    assert result.status == 'error' and result.report_id is None
    assert len(Inconsistent.seen) == 1 and 'coverage_invalid' in result.text


def test_failed_retry_never_counts_success(tmp_path):
    runner, client = turn(base_stages())
    path = tmp_path/'events.jsonl'
    runner.telemetry = TurnTrace('c', 't', path)
    calls = []
    def query(sql, **kw):
        calls.append(kw['job_config'].dry_run)
        raise ge.ServiceUnavailable('private')
    client.query = query
    with pytest.raises(ExecutionError): runner.run(plan(), 'manager_apparel')
    assert calls == [True, True]
    assert agent_metrics(path)['counts'].get('recovery.outcome.success', 0) == 0
