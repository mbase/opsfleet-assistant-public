"""Controlled Standard SQL rejection plus independent source-row execution."""
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest
from google.api_core import exceptions as ge

from opsfleet_assistant import compiler
from opsfleet_assistant.analysis_executor import AnalysisExecutor
from opsfleet_assistant.bigquery_adapter import BigQueryTurn, ExecutionError
from opsfleet_assistant.identity import PolicyError, resolve_demo_scope
from opsfleet_assistant.recovery import SubmissionState as S
from opsfleet_assistant.source_schema import PUBLIC_BINDING
from opsfleet_assistant.telemetry import TurnTrace
from test_recovery_1b import SchemaServer, run_workflow
from test_step3 import plan as adapter_plan
from opsfleet_assistant.contracts import Period
from datetime import date
from test_step4 import SETTINGS


def plan():
    return adapter_plan().model_copy(update={'period': Period(start=date(2025, 1, 1), end=date(2026, 1, 1))})

class SyntaxServer(SchemaServer):
    def __init__(self):
        super().__init__(price='sale_price')
        self.rejected = []
        self.persistent = False
        self.invalid_release = False

    def query(self, sql, **kw):
        # Reject by grammar on every request, never by an attempt counter.
        if 'scoped_items AS' in sql and ('==' in sql or self.persistent):
            self.rejected.append((sql, kw['job_config'].dry_run))
            raise ge.BadRequest('Syntax error: synthetic private sentinel')
        job = super().query(sql, **kw)
        if self.invalid_release and 'scoped_items AS' in sql and not kw['job_config'].dry_run:
            job.schema = ()
        return job


@pytest.fixture
def equality_defect(monkeypatch):
    monkeypatch.setattr(compiler, '_BASE_FROM', compiler._BASE_FROM.replace(' = ', ' == '))


def test_corrected_sql_changes_grammar_and_releases_independent_expected_figures(tmp_path, equality_defect):
    server = SyntaxServer()
    result, runner = run_workflow(tmp_path, server)
    assert result.status == 'ok'
    assert len(server.rejected) == 1 and server.rejected[0][1]
    submitted = [sql for sql in server.submissions if 'scoped_items AS' in sql]
    assert len(submitted) == 1 and '==' not in submitted[0]
    assert submitted[0] != server.rejected[0][0]
    assert [r[0]['revenue'] for sql, r in server.results if 'scoped_items AS' in sql] == [20.0]
    assert runner.executions == 3 and runner.dry_runs == 4 and not server.metadata
    events = [json.loads(s) for s in (tmp_path/'workflow-events.jsonl').read_text().splitlines()]
    correction = [e for e in events if e.get('action') == 'correct_syntax']
    assert [e['outcome'] for e in correction] == ['attempted', 'success']
    assert all(word not in json.dumps(events) for word in ('SELECT', 'sentinel', 'Calvin', 'parameters'))


def test_normal_sql_has_no_extra_requests(tmp_path):
    server = SyntaxServer()
    result, runner = run_workflow(tmp_path, server)
    assert result.status == 'ok' and not server.rejected
    assert runner.executions == runner.dry_runs == 3 and not server.metadata
    assert 'correct_syntax' not in (tmp_path/'workflow-events.jsonl').read_text()


@pytest.mark.parametrize('fault', ['persistent', 'unsupported', 'release', 'coverage', 'ambiguous'])
def test_failed_correction_never_saves_or_replays(tmp_path, equality_defect, fault, monkeypatch):
    server = SyntaxServer()
    if fault == 'unsupported':
        monkeypatch.setattr(compiler, '_BASE_FROM', compiler._BASE_FROM.replace(' == ', ' = '))
    server.persistent = fault in {'persistent', 'unsupported'}
    server.invalid_release = fault == 'release'
    server.fail_coverage = fault == 'coverage'
    server.ambiguous = fault == 'ambiguous'
    result, runner = run_workflow(tmp_path, server)
    assert result.status == 'error' and result.report_id is None
    assert len(server.rejected) == (2 if fault == 'persistent' else 1)
    assert len([s for s in server.submissions if 'scoped_items AS' in s]) <= 1
    assert not server.metadata
    events = (tmp_path/'workflow-events.jsonl').read_text()
    assert '"outcome":"success"' not in events
    expected = 'unsupported' if fault == 'unsupported' else 'failed'
    assert any(e.get('action') == 'correct_syntax' and e.get('outcome') == expected
               for e in map(json.loads, events.splitlines()))


@pytest.mark.parametrize('mutation', ['scope', 'metric', 'dates', 'parameters', 'join', 'statements', 'field', 'plan'])
def test_structural_and_plan_guards_reject_changed_inputs(equality_defect, mutation):
    server = SyntaxServer(); runner = BigQueryTurn(SETTINGS, client=server)
    catalog = runner.load_catalog(); scope = resolve_demo_scope('manager_apparel', catalog)
    original = compiler.compile_plan(plan(), scope, catalog)
    bad = original
    source_plan = plan()
    if mutation == 'scope': bad = replace(original, effective_brands=('Hanes',))
    if mutation == 'metric': bad = replace(original, sql=original.sql.replace('SUM(sale_price)', 'COUNT(sale_price)'))
    if mutation == 'dates': bad = replace(original, sql=original.sql.replace('>= @start_ts', '> @start_ts'))
    if mutation == 'parameters': bad = replace(original, parameters=original.parameters[:-1])
    if mutation == 'join': bad = replace(original, sql=original.sql.replace('JOIN', 'LEFT JOIN', 1))
    if mutation == 'statements': bad = replace(original, sql=original.sql+'; SELECT 1')
    if mutation == 'field': bad = replace(original, sql=original.sql.replace('oi.sale_price,', 'oi.email,'))
    if mutation == 'plan': source_plan = source_plan.model_copy(update={'limit': source_plan.limit+1})
    # Even a compiler-sealed changed AST must match an independent compilation.
    if mutation != 'plan': bad = replace(bad, _seal=compiler._signature(bad))
    with pytest.raises(PolicyError):
        compiler.correct_syntax(bad, source_plan, scope, catalog, binding=PUBLIC_BINDING)


def session(tmp_path, runner):
    return SimpleNamespace(warehouse=runner, correction_started=False,
        context=SimpleNamespace(owner='manager_apparel', budget=SimpleNamespace(remaining=lambda: 100),
            trace=TurnTrace('c', 't', tmp_path/'events.jsonl'),
            versions=SimpleNamespace(compiler=1, field_policy=1, approved_source_bindings=('public_v1',))))


@pytest.mark.parametrize('limit', ['executions', 'dry_runs', 'bytes', 'deadline'])
def test_correction_does_not_reset_limits(tmp_path, equality_defect, limit):
    server = SyntaxServer(); runner = BigQueryTurn(SETTINGS, client=server)
    catalog = runner.load_catalog(); scope = resolve_demo_scope('manager_apparel', catalog)
    query = compiler.compile_plan(plan(), scope, catalog)
    if limit == 'executions': runner.executions = runner.limits.max_executions
    if limit == 'dry_runs': runner.dry_runs = runner.limits.max_executions+4
    if limit == 'bytes': runner.billed_bytes = runner.limits.per_turn_bytes
    if limit == 'deadline': runner._clock = lambda: runner.deadline + 1
    before = len(server.calls)
    with pytest.raises(ExecutionError):
        AnalysisExecutor().run_with_recovery(session(tmp_path, runner), query, plan())
    assert len(server.calls) == before and not server.rejected


def test_one_correction_shared_across_analyses(tmp_path, equality_defect):
    server = SyntaxServer(); runner = BigQueryTurn(SETTINGS, client=server)
    catalog = runner.load_catalog(); scope = resolve_demo_scope('manager_apparel', catalog)
    query = compiler.compile_plan(plan(), scope, catalog)
    turn = session(tmp_path, runner)
    assert AnalysisExecutor().run_with_recovery(turn, query, plan())[0].complete
    with pytest.raises(ExecutionError):
        AnalysisExecutor().run_with_recovery(turn, query, plan())
    assert turn.correction_started and len(server.rejected) == 2
    assert len([s for s in server.submissions if 'scoped_items AS' in s]) == 1


@pytest.mark.parametrize('state', [S.SUBMITTED, S.UNKNOWN])
def test_unsafe_analytical_provenance_stops(tmp_path, equality_defect, state):
    server = SyntaxServer(); runner = BigQueryTurn(SETTINGS, client=server)
    catalog = runner.load_catalog(); scope = resolve_demo_scope('manager_apparel', catalog)
    query = compiler.compile_plan(plan(), scope, catalog)
    def fail(q):
        error = ExecutionError('query_syntax_error', stage='dry_run', submission_state=S.NOT_SUBMITTED)
        error.purpose = 'analysis'; error.analysis_submission_state = state
        raise error
    runner.execute_compiled = fail
    turn = session(tmp_path, runner)
    with pytest.raises(ExecutionError): AnalysisExecutor().run_with_recovery(turn, query, plan())
    assert not turn.correction_started and not server.rejected


def test_valid_empty_keeps_original_interpretation(tmp_path, equality_defect):
    server = SyntaxServer()
    server.db.execute('DELETE FROM order_items')
    result, runner = run_workflow(tmp_path, server)
    assert result.status == 'ok' and len(server.rejected) == 1
    analyses = [r for sql, r in server.results if 'scoped_items AS' in sql]
    assert analyses == [[{'revenue': 0}]]
    assert len([s for s in server.submissions if 'scoped_items AS' in s]) == 1
    assert not server.metadata


def test_rewrite_ignores_string_literals_and_comments(equality_defect):
    runner = BigQueryTurn(SETTINGS, client=SyntaxServer())
    catalog = runner.load_catalog(); scope = resolve_demo_scope('manager_apparel', catalog)
    original = compiler.compile_plan(plan(), scope, catalog)
    # A comment does not authorize rewriting SQL payload; only EQ tokens change.
    original = replace(original, sql=original.sql + ' -- == leave this comment')
    original = replace(original, _seal=compiler._signature(original))
    corrected = compiler.correct_syntax(original, plan(), scope, catalog, binding=PUBLIC_BINDING)
    assert corrected.sql.endswith('-- == leave this comment')
    assert ' == ' not in corrected.sql.split('--')[0]


@pytest.mark.parametrize('limit', ['executions', 'dry_runs', 'bytes', 'deadline'])
def test_rejection_cannot_replenish_exhausted_budget(tmp_path, equality_defect, limit):
    server = SyntaxServer(); runner = BigQueryTurn(SETTINGS, client=server)
    original = server.query
    def reject(sql, **kw):
        try:
            return original(sql, **kw)
        except ge.BadRequest:
            if limit == 'executions': runner.executions = runner.limits.max_executions
            if limit == 'dry_runs': runner.dry_runs = runner.limits.max_executions+4
            if limit == 'bytes': runner.billed_bytes = runner.limits.per_turn_bytes
            if limit == 'deadline': runner._clock = lambda: runner._started + 121
            raise
    server.query = reject
    def factory(settings, *, limits):
        return runner
    from test_step6 import workflow
    result = workflow(tmp_path, turn=factory).run('manager_apparel', 'Revenue in 2025')
    assert result.status == 'error' and result.report_id is None
    assert len(server.rejected) == 1
    assert not [s for s in server.submissions if 'scoped_items AS' in s]


@pytest.mark.parametrize('direction', ['syntax_then_schema', 'schema_then_syntax'])
def test_schema_and_syntax_recovery_cannot_chain(tmp_path, equality_defect, direction, monkeypatch):
    from opsfleet_assistant import source_schema as ss
    if direction == 'syntax_then_schema':
        server = SyntaxServer()
        original = server.query
        def reject(sql, **kw):
            if 'scoped_items AS' in sql and '==' not in sql:
                raise ge.BadRequest('Unrecognized name: synthetic')
            return original(sql, **kw)
        server.query = reject
    else:
        migration = replace(ss.PUBLIC_BINDING, version='synthetic_price_v2', fields=tuple(
            replace(f, physical='unit_price') if (f.table, f.logical) == ('order_items', 'sale_price') else f
            for f in ss.PUBLIC_BINDING.fields))
        monkeypatch.setattr(ss, 'APPROVED_BINDINGS', (ss.PUBLIC_BINDING, migration))
        server = SchemaServer()
        original = server.query
        def reject(sql, **kw):
            if 'scoped_items AS' in sql and 'unit_price' in sql:
                raise ge.BadRequest('Syntax error: synthetic')
            return original(sql, **kw)
        server.query = reject
    result, runner = run_workflow(tmp_path, server)
    assert result.status == 'error' and result.report_id is None
    assert len(server.metadata) == (0 if direction == 'syntax_then_schema' else 4)
    events = [json.loads(s) for s in (tmp_path/'workflow-events.jsonl').read_text().splitlines()]
    starts = [e for e in events if e.get('outcome') == 'attempted' and e.get('action') in {'correct_syntax', 'inspect_schema'}]
    assert len(starts) == 1


@pytest.mark.parametrize('which', ['regional', 'churn'])
def test_corrected_privacy_withheld_population_stays_withheld(tmp_path, equality_defect, which):
    from playbook_source import regional_source, churn_source
    from test_playbooks import PlaybookGateway, regional_spec, churn_spec
    from opsfleet_assistant.workflow import AssistantWorkflow
    from opsfleet_assistant.store import SQLiteStore
    client = regional_source() if which == 'regional' else churn_source()
    if which == 'regional':
        client.db.execute("DELETE FROM users WHERE state='California' AND id>104")
    else:
        client.db.execute("UPDATE users SET country='Tiny' WHERE id=1")
    original = client.query
    rejected = []
    def query(sql, **kw):
        if 'scoped_items AS' in sql and '==' in sql:
            rejected.append(sql)
            raise ge.BadRequest('Syntax error: synthetic')
        return original(sql, **kw)
    client.query = query
    class Gateway(PlaybookGateway): pass
    Gateway.spec = (regional_spec if which == 'regional' else churn_spec)(report=True)
    wf = AssistantWorkflow(SETTINGS, SQLiteStore(tmp_path/'state.sqlite'), gateway_factory=Gateway,
        turn_factory=lambda settings, limits: BigQueryTurn(settings, client=client, limits=limits),
        today=date(2026, 11, 6))
    result = wf.run('manager_apparel', 'Privacy limited offline')
    assert len(rejected) == 1 and result.report_completeness == 'partial'
    assert not result.driver_finding['contributions'] and result.driver_finding['residual'] is None
    evidence = result.presentation['evidence'][0]
    assert evidence['coverage']['period_rows'] is None and evidence['coverage']['source_rows'] is None
    assert 'Tiny' not in json.dumps(evidence)
    assert not client.metadata
