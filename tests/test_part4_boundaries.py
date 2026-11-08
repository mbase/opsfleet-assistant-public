"""Part 4 boundary contracts, with injected adapters and clocks only."""
from datetime import datetime, timedelta, timezone

import pytest

from opsfleet_assistant.bigquery_adapter import BigQueryTurn, ExecutionError
from opsfleet_assistant.model_gateway import ModelFailure
from opsfleet_assistant.store import SQLiteStore
from opsfleet_assistant.workflow import AssistantWorkflow
from test_step4 import SETTINGS, FakeGateway, FakeTurn
from test_playbooks import PlaybookGateway, spec, product_plan
from opsfleet_assistant.compiler import compile_plan
from opsfleet_assistant.identity import Catalog, resolve_demo_scope
from playbook_source import product_source


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.utc = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)

    def monotonic(self):
        return self.seconds

    def now(self):
        return self.utc + timedelta(seconds=self.seconds)


def test_one_compiled_graph_and_isolated_success_failure_success(tmp_path):
    built = []
    class Workflow(AssistantWorkflow):
        def _build_graph(self):
            graph = super()._build_graph()
            built.append(graph)
            return graph
    gateways, turns, contexts = [], [], []
    class Gateway(FakeGateway):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            gateways.append(self)
        def interpret(self, question, **kw):
            contexts.append(kw)
            if question == 'controlled failure':
                self.budget.charge('injected_model')
                raise ModelFailure('provider_failure')
            return super().interpret(question, **kw)
    def factory(settings, *, limits):
        turn = FakeTurn(settings, limits=limits)
        turn.catalog = Catalog.from_products([
            *({"id": pid, "brand": brand} for pid, brand in turn.catalog.product_brands.items()),
            {"id": 4, "brand": "Volcom"}])
        turns.append(turn)
        return turn
    clock = Clock()
    store = SQLiteStore(tmp_path/'state', clock=clock.now)
    wf = Workflow(SETTINGS, store, gateway_factory=Gateway, turn_factory=factory, clock=clock)
    first = wf.run('manager_apparel', 'Q1 report', conversation_id='a')
    clock.seconds = 20
    failed = wf.run('manager_casual', 'controlled failure', conversation_id='b')
    clock.seconds = 40
    last = wf.run('manager_casual', 'Revenue in 2025', conversation_id='c')
    assert built == [wf.graph]
    assert first.status == last.status == 'ok' and first.report_id
    assert failed.status == 'error' and failed.report_id is None
    assert failed.presentation is None and failed.evidence_job_ids == ()
    assert failed.golden_status == 'none' and failed.factual_findings == ()
    assert (first.model_requests, failed.model_requests, last.model_requests) == (2, 1, 2)
    assert (first.query_executions, failed.query_executions, last.query_executions) == (5, 1, 3)
    assert len({id(g.budget) for g in gateways}) == 3
    assert [g.budget.started for g in gateways] == [0, 20, 40]
    assert len({g.budget.telemetry.trace_id for g in gateways}) == 3
    assert {first.trace_id, failed.trace_id, last.trace_id} == {g.budget.telemetry.trace_id for g in gateways}
    assert all(c['previous_plan'] is None and c['pending_request'] is None for c in contexts)
    assert all(c['conversation_context'] == {} for c in contexts)
    assert all(t.executions <= 8 for t in turns)
    assert last.report_id is None and last.report_composition is None
    assert last.presentation['evidence'][0]['scope_reference'] == 'demo:manager_casual'
    assert store.report('manager_casual', first.report_id) is None
    assert store.load_continuity('manager_apparel', 'c') is None
    assert not any(k in vars(wf) for k in ('context', 'authority', 'warehouse', 'budget', 'continuity'))


@pytest.mark.parametrize('late_at', ['interpretation', 'analysis', 'coverage', 'persistence'])
def test_original_deadline_crosses_service_boundaries(tmp_path, late_at):
    clock = Clock()
    client = product_source()
    turns, budgets = [], []
    class Gateway(PlaybookGateway):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            budgets.append(self.budget)
        def interpret(self, *a, **kw):
            result = super().interpret(*a, **kw)
            clock.seconds = 121 if late_at == 'interpretation' else 70
            return result
        def report(self, *a, **kw):
            result = super().report(*a, **kw)
            if late_at == 'persistence':
                clock.seconds = 121
            return result
    Gateway.spec = spec(report=True)
    original_query = client.query
    def query(*a, **kw):
        # Submission is synthetic. Advancing time cannot grant a fresh job/turn.
        job = original_query(*a, **kw)
        stage = client.calls[-1][0]
        if stage == late_at and not kw['job_config'].dry_run:
            clock.seconds = 121
        return job
    client.query = query
    def factory(settings, *, limits):
        turn = BigQueryTurn(settings, client=client, limits=limits)
        turns.append(turn)
        return turn
    store = SQLiteStore(tmp_path/'deadline', clock=clock.now)
    wf = AssistantWorkflow(SETTINGS, store, gateway_factory=Gateway, turn_factory=factory, clock=clock)
    result = wf.run('manager_apparel', 'Synthetic investigation')
    assert result.status == 'error' and result.report_id is None
    assert store.list_reports('manager_apparel') == []
    assert budgets[0].started == 0 and budgets[0].requests <= 2
    assert turns[0].deadline <= 120
    assert turns[0].dry_runs <= 3 and turns[0].executions <= 3
    assert sum(stage == 'analysis' and not dry for stage, dry, sql in client.calls) <= 1
    if late_at == 'interpretation':
        assert turns[0].executions == 1 and len(client.calls) == 2
    if late_at in {'coverage', 'persistence'}:
        assert result.analysis_submission_state == 'submitted'
        assert result.analysis_job_id and result.analysis_billed_bytes is not None


def test_deletion_keeps_its_original_cap_and_never_constructs_gateway(tmp_path):
    store = SQLiteStore(tmp_path/'state')
    # The existing services validate a real saved artifact before a proposal exists.
    wf = AssistantWorkflow(SETTINGS, store, gateway_factory=FakeGateway, turn_factory=FakeTurn)
    saved = wf.run('manager_apparel', 'Q1 report', conversation_id='c')
    limits_seen = []
    def forbidden(*a, **kw):
        pytest.fail('deterministic deletion constructed a model adapter')
    def factory(settings, *, limits):
        limits_seen.append(limits)
        return FakeTurn(settings, limits=limits)
    wf.gateway_factory, wf.turn_factory = forbidden, factory
    preview = wf.run('manager_apparel', 'Delete all the reports we made in this conversation', conversation_id='c')
    assert preview.status == 'confirmation_required' and preview.query_executions == 1
    assert preview.model_requests == 0
    assert len(limits_seen) == 1 and limits_seen[0].max_executions == 1
    assert 0 < limits_seen[0].turn_seconds <= 30
    cancelled = wf.run('manager_apparel', 'cancel '+preview.confirmation.token, conversation_id='c')
    assert cancelled.status == 'ok' and cancelled.query_executions == 0
    assert store.report('manager_apparel', saved.report_id)
    other = wf.run('manager_casual', 'confirm '+preview.confirmation.token, conversation_id='c')
    assert other.status == 'error' and other.query_executions == 0
    assert len(limits_seen) == 1


def test_warehouse_deadline_cannot_be_rebound_after_dispatch(tmp_path):
    clock = Clock()
    turn = BigQueryTurn(SETTINGS, client=product_source())
    turn.bind_deadline(clock.monotonic, 120)
    turn.load_catalog()
    with pytest.raises(ValueError, match='already_started'):
        turn.bind_deadline(clock.monotonic, 240)
    catalog = turn.load_catalog()
    query = compile_plan(product_plan(), resolve_demo_scope('manager_apparel', catalog), catalog)
    clock.seconds = 121
    with pytest.raises(ExecutionError, match='turn_timeout'):
        turn.execute_compiled(query)


@pytest.mark.parametrize('fault', ['ambiguous', 'coverage', 'schema'])
def test_warehouse_failure_then_other_owner_success_on_same_graph(tmp_path, fault):
    client = product_source()
    client.fault, client.fault_stage = fault, 'coverage' if fault == 'coverage' else 'analysis'
    turns = []
    class Gateway(PlaybookGateway):
        pass
    Gateway.spec = spec(report=True)
    def factory(settings, *, limits):
        turn = BigQueryTurn(settings, client=client, limits=limits)
        turns.append(turn)
        return turn
    store = SQLiteStore(tmp_path/'state')
    wf = AssistantWorkflow(SETTINGS, store, gateway_factory=Gateway, turn_factory=factory)
    failed = wf.run('manager_apparel', 'Synthetic failure', conversation_id='same')
    assert failed.status == 'error' and failed.report_id is None
    assert sum(stage == 'analysis' and not dry for stage, dry, sql in client.calls) <= 1
    if fault == 'ambiguous':
        assert failed.billed_bytes is None and failed.failure_submission_state == 'unknown'
    if fault == 'coverage':
        assert failed.analysis_submission_state == 'submitted' and failed.analysis_job_id
    client.fault = None
    before = len(client.calls)
    success = wf.run('ceo', 'Synthetic success', conversation_id='same')
    assert success.status == 'ok' and success.report_id
    assert success.driver_finding['complete']
    assert success.model_requests == 2 and success.query_executions == 3 and success.billed_bytes == 30
    assert [stage for stage, dry, sql in client.calls[before:] if not dry] == ['catalog', 'analysis', 'coverage']
    assert turns[0] is not turns[1] and turns[1].dry_runs == 3
    assert success.trace_id != failed.trace_id
    assert store.report('manager_apparel', success.report_id) is None
    assert success.presentation['evidence'][0]['scope_reference'] == 'demo:ceo'


def test_unexpected_exception_after_release_keeps_provenance_and_sanitizes_telemetry(tmp_path):
    from dataclasses import asdict
    import json
    class Gateway(FakeGateway):
        def report(self, *a, **kw):
            raise RuntimeError('private-marker SQL SELECT capability-token')
    store = SQLiteStore(tmp_path/'state')
    wf = AssistantWorkflow(SETTINGS, store, gateway_factory=Gateway, turn_factory=FakeTurn)
    failed = wf.run('manager_apparel', 'Revenue in 2025', conversation_id='failed')
    assert failed.status == 'error' and failed.error_category == 'internal'
    assert failed.analysis_submission_state == 'submitted'
    assert failed.analysis_job_id == 'analysis_3' and failed.analysis_billed_bytes == 10
    assert failed.failure_submission_state == 'unknown' and failed.failure_job_id is None
    assert failed.presentation is None and failed.factual_findings == () and failed.report_id is None
    assert failed.query_executions == 3 and store.load_continuity('manager_apparel', 'failed') is None
    assert 'private-marker' not in json.dumps(asdict(failed))
    events = (tmp_path/'workflow-events.jsonl').read_text()
    assert 'private-marker' not in events and 'SQL SELECT' not in events and 'capability-token' not in events
    wf.gateway_factory = FakeGateway
    success = wf.run('manager_apparel', 'Revenue in 2025', conversation_id='fresh')
    assert success.status == 'ok' and success.model_requests == 2 and success.query_executions == 3
    assert success.analysis_job_id is None and success.failure_job_id is None


@pytest.mark.parametrize('warehouse_seconds,expected_deadline', [(90, 90), (150, 120)])
def test_active_discovery_adopts_same_bound_clock_without_resetting_budget(tmp_path, warehouse_seconds, expected_deadline):
    from dataclasses import replace
    clock = Clock()
    client = product_source()
    turns, discovery = [], []
    class Gateway(PlaybookGateway):
        pass
    Gateway.spec = spec(report=True)
    def factory(settings, *, limits):
        turn = BigQueryTurn(settings, client=client,
            limits=replace(limits, turn_seconds=warehouse_seconds), clock=clock.monotonic)
        turn.load_catalog()
        discovery.append((turn.deadline, turn.executions, turn.dry_runs, turn.billed_bytes))
        clock.seconds = 10
        turns.append(turn)
        return turn
    store = SQLiteStore(tmp_path/'state', clock=clock.now)
    wf = AssistantWorkflow(SETTINGS, store, gateway_factory=Gateway, turn_factory=factory, clock=clock)
    result = wf.run('manager_apparel', 'Synthetic discovery investigation')
    assert result.status == 'ok' and result.report_id, result.text
    assert result.model_requests == 2 and result.query_executions == 3 and result.billed_bytes == 30
    assert discovery == [(warehouse_seconds, 1, 1, 10)]
    assert len(turns) == 1 and turns[0].deadline == expected_deadline
    assert turns[0].executions == turns[0].dry_runs == 3
    assert [stage for stage, dry, sql in client.calls if not dry] == ['catalog', 'analysis', 'coverage']
    with pytest.raises(ValueError, match='already_started'):
        turns[0].bind_deadline(clock.monotonic, 240)
    catalog = turns[0].load_catalog()
    query = compile_plan(product_plan(), resolve_demo_scope('manager_apparel', catalog), catalog)
    before = len(client.calls)
    clock.seconds = expected_deadline
    with pytest.raises(ExecutionError, match='turn_timeout'):
        turns[0].execute_compiled(query)
    assert len(client.calls) == before


@pytest.mark.parametrize('replacement', ['other_instance', 'other_method', 'equal_callable'])
def test_active_discovery_rejects_a_genuinely_different_clock(replacement):
    class OtherClock(Clock):
        def other_monotonic(self):
            return self.seconds
    class EqualCallable:
        def __call__(self):
            return 0.0
        def __eq__(self, other):
            return True
    clock = OtherClock()
    client = product_source()
    turn = BigQueryTurn(SETTINGS, client=client, clock=clock.monotonic)
    catalog = turn.load_catalog()
    original_deadline = turn.deadline
    other = {'other_instance': OtherClock().monotonic,
             'other_method': clock.other_monotonic, 'equal_callable': EqualCallable()}[replacement]
    with pytest.raises(ValueError, match='clock_changed_after_dispatch'):
        turn.bind_deadline(other, 240)
    assert turn.deadline == original_deadline
    assert turn.executions == turn.dry_runs == 1 and turn.billed_bytes == 10
    query = compile_plan(product_plan(), resolve_demo_scope('manager_apparel', catalog), catalog)
    before = len(client.calls)
    clock.seconds = original_deadline
    with pytest.raises(ExecutionError, match='turn_timeout'):
        turn.execute_compiled(query)
    assert len(client.calls) == before
