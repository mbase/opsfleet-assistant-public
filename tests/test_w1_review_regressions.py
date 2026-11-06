"""Round-2 W1 regressions using isolated state and scripted model output."""

from datetime import date
import json

import pytest

from opsfleet_assistant import workflow as workflow_module
from opsfleet_assistant.contracts import InterpretationResult, QuerySpec, RequestSpec, TemporalExpression, YearExpression
from opsfleet_assistant.evidence import Evidence
from opsfleet_assistant.model_gateway import ReportDraft
from opsfleet_assistant.store import SQLiteStore
from opsfleet_assistant.telemetry import TurnTrace, agent_metrics
from opsfleet_assistant.workflow import AssistantWorkflow, _render
from test_step4 import FakeGateway, FakeTurn, SETTINGS


PERIOD = TemporalExpression(kind="month", month=1, year=YearExpression(kind="absolute", value=2025))


def assistant(tmp_path, proposal):
    class RecordingTurn(FakeTurn):
        queries = []
        released = []

        def execute_compiled(self, query):
            self.queries.append(query)
            evidence = super().execute_compiled(query)
            self.released.append(evidence)
            return evidence

    class ScriptedGateway(FakeGateway):
        def interpret(self, question, **kwargs):
            self.budget.charge("injected_model")
            return proposal

        def report(self, evidence, **kwargs):
            # The trusted display metadata need not expand the existing model payload.
            assert all("execution_scope" not in item for item in evidence)
            return super().report(evidence, **kwargs)

    store = SQLiteStore(tmp_path / "state.sqlite")
    runner = AssistantWorkflow(SETTINGS, store, gateway_factory=ScriptedGateway,
                               turn_factory=RecordingTurn, today=date(2026, 11, 4))
    return runner, store, RecordingTurn


def ready(*queries, mode="analysis"):
    return InterpretationResult(status="ready", request=RequestSpec(
        mode=mode, analysis=PERIOD, queries=queries))


@pytest.mark.parametrize("requested,products,brands,visible", [
    (None, None, ("Calvin Klein", "Carhartt"),
     'brands ["Calvin Klein", "Carhartt"]; product IDs all products within these brands.'),
    (("calvin klein",), None, ("Calvin Klein",),
     'brands ["Calvin Klein"]; product IDs all products within these brands.'),
    (("Calvin Klein",), (1,), ("Calvin Klein",),
     'brands ["Calvin Klein"]; product IDs [1].'),
])
def test_authorized_execution_filters_are_visible_and_json_compatible(tmp_path, requested, products, brands, visible):
    proposal = ready(QuerySpec(operation="aggregate", metric="revenue",
                               requested_brands=requested, product_ids=products))
    runner, store, warehouse = assistant(tmp_path, proposal)
    try:
        result = runner.run("manager_apparel", "Revenue in January 2025")
        assert result.status == "ok" and result.model_requests == 2 and result.query_executions == 3
        query, = warehouse.queries
        evidence, = warehouse.released
        assert query.parameter("brands").value == brands
        assert query.requested_product_ids == products
        assert evidence.execution_scope.brands == brands
        assert evidence.execution_scope.product_ids == products
        if products:
            assert query.parameter("product_ids").value == products
        else:
            assert "product_ids" not in {parameter.name for parameter in query.parameters}
        assert f"Scope filters for E1: {visible}" in result.text
        assert "Hanes" not in result.text and "calvin klein" not in result.text
        public = json.loads(json.dumps(result.__dict__))
        assert public["status"] == "ok" and public["interpretation_status"] == "ready"
        assert public["resolved_periods"]["analysis"] == {"start": "2025-01-01", "end": "2025-02-01"}
        assert public["evidence_job_ids"] == ["analysis_3"]
        assert public["text"] == result.text and "scope demo:manager_apparel version 1" in result.text
    finally:
        store.close()


def test_each_report_query_discloses_its_own_effective_filters_and_retry_preserves_them(tmp_path):
    proposal = ready(QuerySpec(operation="aggregate", metric="revenue"),
                     QuerySpec(operation="aggregate", metric="order_count",
                               requested_brands=("calvin klein",), product_ids=(1,)), mode="report")
    runner, store, warehouse = assistant(tmp_path, proposal)
    try:
        result = runner.run("manager_apparel", "Create a January 2025 revenue report",
                            conversation_id="scope-report", turn_id="scope-turn")
        assert result.status == "ok" and result.query_executions == 5 and result.report_id is not None
        assert [q.parameter("brands").value for q in warehouse.queries] == [
            ("Calvin Klein", "Carhartt"), ("Calvin Klein",)]
        assert [q.requested_product_ids for q in warehouse.queries] == [None, (1,)]
        lines = [line for line in result.text.splitlines() if line.startswith("Scope filters for E")]
        assert lines == [
            'Scope filters for E1: brands ["Calvin Klein", "Carhartt"]; product IDs all products within these brands.',
            'Scope filters for E2: brands ["Calvin Klein"]; product IDs [1].',
        ]
        assert store.report("manager_apparel", result.report_id)["content"] == result.text
        retry = runner.run("manager_apparel", "Create a January 2025 revenue report",
                           conversation_id="scope-report", turn_id="scope-turn")
        assert retry.reused_report and retry.text == result.text and retry.model_requests == 0
        assert len(warehouse.queries) == 2
    finally:
        store.close()


@pytest.mark.parametrize("brands,products,code", [
    (("Hanes",), None, "unauthorized_brand"),
    (("Calvin Klein",), (3,), "product_not_available"),
])
def test_unauthorized_scope_never_enters_released_disclosures(tmp_path, brands, products, code):
    runner, store, warehouse = assistant(tmp_path, ready(QuerySpec(
        operation="aggregate", metric="revenue", requested_brands=brands, product_ids=products)))
    try:
        result = runner.run("manager_apparel", "Revenue in January 2025")
        assert result.status == "error" and code in result.text
        assert not warehouse.queries and not warehouse.released
        assert "Scope filters" not in result.text and "Hanes" not in result.text
    finally:
        store.close()


def test_legacy_evidence_does_not_invent_unrestricted_filters(tmp_path):
    runner, store, warehouse = assistant(tmp_path, ready(QuerySpec(operation="aggregate", metric="revenue")))
    try:
        assert runner.run("manager_apparel", "Revenue in January 2025").status == "ok"
        old = warehouse.released[0].model_dump(exclude={"execution_scope"})
        legacy = Evidence.model_validate(old)
        assert legacy.execution_scope is None
        text = _render((legacy,), ReportDraft(), None, report_requested=False)
        assert "Scope filters for E1: unavailable in this evidence's metadata." in text
        assert "all products" not in text
    finally:
        store.close()


def test_normal_outcomes_keep_distinct_sanitized_events_and_metrics(tmp_path, monkeypatch):
    path = tmp_path / "normal-outcomes.jsonl"

    class LocalTrace(TurnTrace):
        def __init__(self, conversation_id, turn_id):
            super().__init__(conversation_id, turn_id, path)

    monkeypatch.setattr(workflow_module, "TurnTrace", LocalTrace)
    marker = "person@example.com PASSWORD=secret deletion_token_marker"
    for status, issue in (("needs_clarification", "missing_year"), ("unsupported", "unsupported_capability")):
        proposal = InterpretationResult(status=status, issues=(issue,), request=RequestSpec(
            sources=({"field": "analysis", "origin": "explicit", "phrase": marker},)))
        runner, store, warehouse = assistant(tmp_path / status, proposal)
        try:
            result = runner.run("manager_apparel", "Revenue in January PRIVATE_PROMPT_MARKER")
            assert result.status == status and result.model_requests == 1 and result.query_executions == 1
            assert not warehouse.queries and not warehouse.released and not result.evidence_job_ids
            events = [json.loads(line) for line in path.read_text().splitlines()]
            final = [event for event in events if event["trace_id"] == result.trace_id
                     and event["event"] in {"final_outcome", "turn_end"}]
            assert len(final) == 2
            assert all(event["status"] == event["category"] == status for event in final)
        finally:
            store.close()
    metrics = agent_metrics(path)
    assert metrics["counts"]["outcome.needs_clarification"] == 1
    assert metrics["counts"]["outcome.unsupported"] == 1
    assert "outcome.other" not in metrics["counts"]
    content = path.read_text()
    for secret in ("PRIVATE_PROMPT_MARKER", "person@example.com", "PASSWORD=secret", "deletion_token_marker"):
        assert secret not in content


def test_unknown_values_and_legacy_outcomes_keep_existing_metric_mappings(tmp_path):
    path = tmp_path / "events.jsonl"
    marker = "person@example.com secret_token SELECT * FROM users"
    for status in ("clarification", "confirmation_required", "refusal", "error", marker):
        trace = TurnTrace("c", "t", path)
        trace.finish(status, category=marker, requests=0, executions=0)
    content = path.read_text()
    assert marker not in content and "person@example.com" not in content and "secret_token" not in content
    events = [json.loads(line) for line in content.splitlines()]
    assert all(event["category"] == "unknown" for event in events)
    assert events[-1]["status"] == "unknown"
    counts = agent_metrics(path)["counts"]
    assert counts["outcome.clarification"] == 2
    assert counts["outcome.refusal"] == 1 and counts["outcome.error"] == 1 and counts["outcome.other"] == 1


def test_deletion_clarification_and_confirmation_remain_recognizable(tmp_path):
    runner, store, warehouse = assistant(tmp_path, ready(QuerySpec(operation="aggregate", metric="revenue")))
    try:
        store.save_report(owner="manager_apparel", conversation_id="c", turn_id="t", content="Synthetic scope report",
                          metric_definitions=[], scope_reference="demo:manager_apparel", scope_version=1, evidence_refs=[])
        clarification = runner.run("manager_apparel", "Delete all the reports we made in this conversation")
        preview = runner.run("manager_apparel", "Delete all reports mentioning Synthetic scope report")
        assert clarification.status == "clarification" and preview.status == "confirmation_required"
        assert clarification.model_requests == preview.model_requests == 0
        assert clarification.query_executions == preview.query_executions == 0 and not warehouse.queries
        events = [json.loads(line) for line in (tmp_path / "workflow-events.jsonl").read_text().splitlines()]
        assert [(event["status"], event["category"]) for event in events if event["event"] == "final_outcome"] == [
            ("clarification", "clarification"), ("confirmation_required", "confirmation_required")]
        assert agent_metrics(tmp_path / "workflow-events.jsonl")["counts"]["outcome.clarification"] == 2
        assert "Synthetic scope report" not in (tmp_path / "workflow-events.jsonl").read_text()
    finally:
        store.close()
