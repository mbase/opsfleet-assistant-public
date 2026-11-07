"""Synthetic approved migration, executed against independent in-memory SQLite.

This fixture does not represent an observed migration of the public warehouse.
The server rejects nonexistent columns regardless of attempt count.
"""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import sqlite3

from google.api_core import exceptions as ge
from google.cloud import bigquery
import pytest
import sqlglot

from opsfleet_assistant import source_schema as ss
from opsfleet_assistant.bigquery_adapter import BigQueryTurn, ExecutionError
from opsfleet_assistant.compiler import compile_plan, semantic_fingerprint, execution_fingerprint, is_trusted_compiled
from opsfleet_assistant.identity import resolve_demo_scope, PolicyError
from opsfleet_assistant.telemetry import TurnTrace, agent_metrics
from test_step3 import Job, plan, turn, base_stages
from test_step4 import SETTINGS
from test_step6 import workflow


@pytest.fixture
def approved_migration(monkeypatch):
    migration = replace(ss.PUBLIC_BINDING, version='synthetic_price_v2', fields=tuple(
        replace(f, physical='unit_price') if (f.table, f.logical) == ('order_items', 'sale_price') else f
        for f in ss.PUBLIC_BINDING.fields))
    monkeypatch.setattr(ss, 'APPROVED_BINDINGS', (ss.PUBLIC_BINDING, migration))
    return migration


class SchemaServer:
    def __init__(self, *, price='unit_price', empty=False):
        assert price in {'unit_price', 'sale_price', 'unknown_price'}
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(f"""
            CREATE TABLE products(id INTEGER, brand TEXT);
            CREATE TABLE orders(order_id INTEGER, user_id INTEGER);
            CREATE TABLE users(id INTEGER, age INTEGER, city TEXT, state TEXT, country TEXT, gender TEXT, traffic_source TEXT);
            CREATE TABLE order_items(order_id INTEGER, product_id INTEGER, created_at TEXT, {price} REAL, status TEXT);
            INSERT INTO products VALUES (1,'Calvin Klein'),(2,'Carhartt'),(3,'Hanes');
            INSERT INTO orders VALUES (1,11),(2,12),(3,13),(4,14),(5,15);
        """)
        if not empty:
            self.db.executemany('INSERT INTO order_items VALUES (?,?,?,?,?)', [
                (1,1,'2025-02-01',12.5,'Complete'), (2,2,'2025-03-01',7.5,'Shipped'),
                (3,3,'2025-03-01',900,'Complete'), (4,1,'2024-12-31',800,'Complete'),
                (5,1,'2025-04-01',700,'Cancelled')])
        self.calls = []
        self.metadata = []
        self.submissions = []
        self.results = []
        self.force_syntax = False
        self.ambiguous = False
        self.fail_coverage = False
        self.type_override = None

    def get_table(self, name, *, retry, timeout):
        assert retry is None and 0 < timeout <= 30
        self.metadata.append(name)
        table = name.rsplit('.',1)[1]
        types = {'INTEGER':'INTEGER', 'TEXT':'STRING', 'REAL':'FLOAT'}
        return SimpleNamespace(schema=[bigquery.SchemaField(r['name'],
            self.type_override if r['name'] == 'unit_price' and self.type_override else
            'TIMESTAMP' if r['name'] == 'created_at' else types[r['type']])
            for r in self.db.execute(f'PRAGMA table_info({table})')])

    def query(self, sql, *, job_config, retry, job_retry, timeout, **kwargs):
        assert retry is None and job_retry is None and timeout > 0
        self.calls.append((sql, job_config))
        if 'scoped_items AS' in sql and self.force_syntax:
            raise ge.BadRequest('Syntax error: controlled persistent compiler defect')
        if 'COUNTIF' in sql and self.fail_coverage:
            raise ge.BadRequest('Unrecognized name: controlled coverage defect')
        rendered = sql.replace('`bigquery-public-data.thelook_ecommerce.', '`')
        values = {}
        for param in job_config.query_parameters:
            if isinstance(param, bigquery.ArrayQueryParameter):
                # Fixture-only literals; never used by application rendering.
                literal = ','.join("'" + str(v).replace("'", "''") + "'" for v in param.values)
                rendered = rendered.replace(f'UNNEST(@{param.name})', '('+literal+')')
            else:
                value = param.value
                values[param.name] = value.isoformat() if isinstance(value, datetime) else value
        translated = sqlglot.transpile(rendered, read='bigquery', write='sqlite')[0]
        try:
            cursor = self.db.execute(translated, values)
            rows = [dict(r) for r in cursor]
            # SQLite renders COUNTIF as SUM; BigQuery COUNTIF(empty) is zero.
            for row in rows:
                if 'period_rows' in row and row['period_rows'] is None:
                    row['period_rows'] = 0
        except sqlite3.OperationalError as exc:
            raise ge.BadRequest('Unrecognized name: fixture schema rejection') from exc
        if job_config.dry_run:
            return SimpleNamespace(total_bytes_processed=10)
        self.submissions.append(sql)
        self.results.append((sql, rows))
        if self.ambiguous and 'scoped_items AS' in sql:
            raise ge.ServiceUnavailable('server accepted before client lost response')
        names = [d[0] for d in cursor.description]
        kinds = {'demographic_value':'STRING', 'customers':'INT64', 'average_customer_spend':'NUMERIC',
                 'id':'INT64', 'brand':'STRING', 'revenue':'NUMERIC', 'source_rows':'INT64',
                 'period_rows':'INT64', 'latest_item_at':'TIMESTAMP'}
        for row in rows:
            if row.get('latest_item_at'):
                row['latest_item_at'] = datetime.fromisoformat(row['latest_item_at']).replace(tzinfo=timezone.utc)
        return Job(rows, tuple(bigquery.SchemaField(n, kinds[n]) for n in names),
                   job_id='fixture_job', billed=10)


def run_workflow(tmp_path, server):
    runners = []
    def factory(settings, *, limits):
        runner = BigQueryTurn(settings, client=server, limits=limits)
        runners.append(runner)
        return runner
    return workflow(tmp_path, turn=factory).run('manager_apparel', 'Revenue in 2025'), runners[0]


def test_supported_synthetic_binding_changes_execution_and_preserves_semantics(tmp_path, approved_migration):
    server = SchemaServer()
    result, runner = run_workflow(tmp_path, server)
    assert result.status == 'ok' and result.factual_findings
    assert [rows[0]['revenue'] for sql, rows in server.results if 'scoped_items AS' in sql] == [20.0]
    assert '20.00' in result.text or '20' in result.text
    assert runner.executions == 3 and runner.dry_runs == 4
    assert len(server.metadata) == runner.schema_metadata_requests == 4
    analyses = [sql for sql, cfg in server.calls if 'scoped_items AS' in sql]
    assert len(analyses) == 3 and 'unit_price' not in analyses[0] and 'unit_price' in analyses[1]
    assert len([sql for sql in server.submissions if 'scoped_items AS' in sql]) == 1
    original = compile_plan(plan(), resolve_demo_scope('manager_apparel', runner.load_catalog()), runner.load_catalog())
    corrected = runner.correct_schema_binding(original, plan(), 'manager_apparel')
    assert is_trusted_compiled(corrected)
    assert semantic_fingerprint(original) == semantic_fingerprint(corrected)
    assert execution_fingerprint(original) != execution_fingerprint(corrected)
    assert original.parameters == corrected.parameters and original.result_contract == corrected.result_contract
    assert len(server.metadata) == 4  # Cached cycle, no second refresh.
    events = (tmp_path/'workflow-events.jsonl').read_text()
    assert 'unit_price' not in events and 'Calvin' not in events and 'SELECT' not in events
    assert agent_metrics(tmp_path/'workflow-events.jsonl')['counts']['recovery.outcome.success'] == 1


@pytest.mark.parametrize('fault', ['unknown', 'type', 'syntax', 'unchanged', 'ambiguous', 'coverage'])
def test_fail_closed_faults_no_replay(tmp_path, approved_migration, fault):
    server = SchemaServer(price='unknown_price' if fault == 'unknown' else 'sale_price' if fault in {'unchanged','coverage'} else 'unit_price')
    server.type_override = 'STRING' if fault == 'type' else None
    server.force_syntax = fault == 'syntax'
    server.ambiguous = fault == 'ambiguous'
    server.fail_coverage = fault == 'coverage'
    if fault == 'unchanged':
        original = server.query
        def reject(sql, **kw):
            if 'scoped_items AS' in sql:
                server.calls.append((sql, kw['job_config']))
                raise ge.BadRequest('Unrecognized name: persistent defect')
            return original(sql, **kw)
        server.query = reject
    result, runner = run_workflow(tmp_path, server)
    assert result.status == 'error' and result.report_id is None
    assert len([s for s in server.submissions if 'scoped_items AS' in s]) <= 1
    assert len(server.metadata) <= 4
    if fault in {'syntax', 'coverage'}: assert not server.metadata
    if fault == 'ambiguous':
        assert result.failure_submission_state == 'unknown' and result.billed_bytes is None
    if fault == 'coverage':
        assert result.failure_submission_state == 'not_submitted'
        assert result.failure_job_id is None
        assert result.analysis_submission_state == 'submitted'
        assert result.analysis_job_id == 'fixture_job'
    if fault != 'ambiguous':
        assert agent_metrics(tmp_path/'workflow-events.jsonl')['counts'].get('recovery.outcome.success',0) == 0


def test_corrected_valid_empty_stays_empty(tmp_path, approved_migration):
    result, runner = run_workflow(tmp_path, SchemaServer(empty=True))
    assert result.status == 'ok' and 'No matching data' in result.text
    assert not result.factual_findings and result.presentation['empty_diagnosis'] == 'valid_empty'
    assert runner.executions == 3


def test_unregistered_binding_and_semantic_changes_rejected(approved_migration):
    runner, _ = turn(base_stages())
    catalog = runner.load_catalog()
    scope = resolve_demo_scope('manager_apparel', catalog)
    with pytest.raises(PolicyError):
        compile_plan(plan(), scope, catalog, binding=replace(approved_migration, version='unapproved'))
    query = compile_plan(plan(), scope, catalog)
    runner._inspected_binding = approved_migration
    runner._schema_inspection_started = True
    with pytest.raises(ExecutionError, match='recovery_scope_changed'):
        runner.correct_schema_binding(query, plan(product_ids=(1,)), 'manager_apparel')


@pytest.mark.parametrize('limit', ['executions', 'dry_runs', 'bytes', 'deadline', 'unknown_billing'])
def test_schema_inspection_obeys_remaining_limits(limit):
    server = SchemaServer()
    runner = BigQueryTurn(SETTINGS, client=server)
    if limit == 'executions': runner.executions = 8
    if limit == 'dry_runs': runner.dry_runs = 12
    if limit == 'bytes': runner.billed_bytes = runner.limits.per_turn_bytes
    if limit == 'unknown_billing': runner.billing_unknown = True
    if limit == 'deadline': runner._clock = lambda: runner._started + 121
    with pytest.raises(ExecutionError): runner.inspect_source_schema()
    assert not server.metadata and not server.calls
    with pytest.raises(ExecutionError): runner.inspect_source_schema()
    assert not server.metadata  # Exhausted/partial cycles cannot restart.


def test_partial_metadata_cycle_deadline_and_permission_stop_without_retry():
    server = SchemaServer()
    runner = BigQueryTurn(SETTINGS, client=server)
    original = server.get_table
    def expiring(*args, **kwargs):
        table = original(*args, **kwargs)
        runner._clock = lambda: runner._started + 121
        return table
    server.get_table = expiring
    with pytest.raises(ExecutionError, match='turn_timeout'): runner.inspect_source_schema()
    assert len(server.metadata) == 1
    with pytest.raises(ExecutionError): runner.inspect_source_schema()
    assert len(server.metadata) == 1

    runner = BigQueryTurn(SETTINGS, client=server)
    def forbidden(*args, **kwargs): raise ge.Forbidden('private account')
    server.get_table = forbidden
    with pytest.raises(ExecutionError, match='permission_denied'): runner.inspect_source_schema()
    assert runner.schema_metadata_requests == 1 and not server.calls


def test_required_modes_and_types_fail_closed(approved_migration):
    server = SchemaServer()
    schema = {table: {f.name: ({'INTEGER':'INT64','FLOAT':'FLOAT64'}.get(f.field_type,f.field_type), f.mode)
              for f in server.get_table(ss.DATASET+'.'+table, retry=None, timeout=1).schema}
              for table in ss.TABLES}
    assert ss.choose_binding(schema) == approved_migration
    schema['order_items']['unit_price'] = ('FLOAT64','REPEATED')
    with pytest.raises(PolicyError): ss.choose_binding(schema)


def test_identical_public_binding_does_not_claim_correction(tmp_path):
    server = SchemaServer(price='sale_price')
    runner = BigQueryTurn(SETTINGS, client=server)
    catalog = runner.load_catalog()
    query = compile_plan(plan(), resolve_demo_scope('manager_apparel', catalog), catalog)
    with pytest.raises(ExecutionError, match='query_schema_error'):
        runner.correct_schema_binding(query, plan(), 'manager_apparel')
    assert runner.schema_metadata_requests == 4 and runner.executions == 1


def test_persistent_rejection_after_binding_change_stops_once(tmp_path, approved_migration):
    server = SchemaServer()
    original = server.query
    attempts = []
    def rejected(sql, **kwargs):
        if 'scoped_items AS' in sql:
            attempts.append(sql)
            raise ge.BadRequest('Unrecognized name: permanent server rejection')
        return original(sql, **kwargs)
    server.query = rejected
    result, runner = run_workflow(tmp_path, server)
    assert result.status == 'error' and result.report_id is None
    assert len(attempts) == 2 and 'unit_price' not in attempts[0] and 'unit_price' in attempts[1]
    assert len(server.metadata) == 4 and runner.executions == 1
    assert agent_metrics(tmp_path/'workflow-events.jsonl')['counts'].get('recovery.outcome.success',0) == 0


def test_version_only_change_and_random_seals_are_not_execution_changes(monkeypatch):
    migration = replace(ss.PUBLIC_BINDING, version='synthetic_identity')
    monkeypatch.setattr(ss, 'APPROVED_BINDINGS', (ss.PUBLIC_BINDING, migration))
    runner, _ = turn(base_stages())
    catalog = runner.load_catalog()
    scope = resolve_demo_scope('manager_apparel', catalog)
    query = compile_plan(plan(), scope, catalog)
    second = compile_plan(plan(), scope, catalog, binding=migration)
    assert execution_fingerprint(query) == execution_fingerprint(second)
    assert execution_fingerprint(query) == execution_fingerprint(replace(query, _seal='random'))
    runner._schema_inspection_started = True
    runner._inspected_binding = migration
    with pytest.raises(ExecutionError, match='query_schema_error'):
        runner.correct_schema_binding(query, plan(), 'manager_apparel')


def test_alternate_binding_requires_metadata_before_execution(approved_migration):
    runner, client = turn(base_stages())
    catalog = runner.load_catalog()
    query = compile_plan(plan(), resolve_demo_scope('manager_apparel', catalog), catalog, binding=approved_migration)
    before = len(client.calls)
    with pytest.raises(ExecutionError, match='query_schema_error'): runner.execute_compiled(query)
    assert len(client.calls) == before
