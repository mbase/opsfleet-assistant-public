"""Independent report expectations using synthetic, isolated execution only.

Scripted interpretation is not evidence of live language understanding. Numeric
oracles deliberately differ by period; reports cannot repair evidence with jobs.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import json

from google.cloud import bigquery
import pytest
from pydantic import ValidationError

from opsfleet_assistant.contracts import (ActionSelection,
    InterpretationResult as I, RequestSpec as R, QuerySpec as Q, TemporalExpression as T,
    YearExpression as Y, ReportSpec, ReportDraft)
from opsfleet_assistant.evidence import Coverage, release_evidence
from opsfleet_assistant.model_gateway import ModelFailure, _strict_schema
from opsfleet_assistant.reporting import compose_report, ground_report
from opsfleet_assistant.periods import resolve_request
from opsfleet_assistant.store import SQLiteStore
from opsfleet_assistant.workflow import AssistantWorkflow
from opsfleet_assistant.telemetry import TurnTrace, agent_metrics
from test_step4 import FakeTurn, SETTINGS
from test_grounded_findings import fixture_evidence

OWNER = "manager_apparel"
NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def quarter(q=3, year=2025):
    return T(kind="quarter", quarter=q, year=Y(kind="absolute", value=year) if year else None)


def request(*, q=3, action=None, report=None, queries=None, **extra):
    return R(mode="report", analysis=quarter(q), action=action, report=report,
             queries=queries or (Q(operation="aggregate", metric="revenue"),), **extra)


class ReportTurn(FakeTurn):
    queries = []
    state = "normal"
    def execute_compiled(self, query):
        self.queries.append(query)
        if query.operation.value == "demographic_comparison":
            self.executions += 2
            self.billed_bytes += 20
            rows = () if self.state == "suppressed" else (
                {"demographic_value": "Texas", "customers": 5, "average_customer_spend": Decimal("30"), "revenue": Decimal("150")},
                {"demographic_value": "California", "customers": 5, "average_customer_spend": Decimal("20"), "revenue": Decimal("100")})
            schema = tuple(bigquery.SchemaField(name, "STRING" if name == "demographic_value" else
                "INT64" if name == "customers" else "NUMERIC") for name in query.result_contract.columns)
            return release_evidence(query, rows, schema, job_id="demographic_fixture", estimated_bytes=10,
                billed_bytes=10, query_as_of=NOW, coverage=Coverage(status="checked", source_rows=None,
                    period_rows=None, note="Aggregate coverage only; complementary counts withheld.", job_id="aggregate_coverage"))
        if self.state == "normal" and query.operation.value != "time_series":
            item = super().execute_compiled(query)
            if query.result_contract.columns == ("revenue",):
                start = query.parameter("start_ts").value
                # Q3 80 vs Q2 50 => independent +30 and +60% oracle.
                amount = Decimal("80") if start.month == 7 else Decimal("50")
                item = item.model_copy(update={"rows": ({"revenue": str(amount)},)})
            return item
        self.executions += 2
        self.billed_bytes += 20
        if query.operation.value == "time_series":
            rows = ({"month": date(2025, 7, 1), "revenue": Decimal("20")},
                    {"month": date(2025, 8, 1), "revenue": Decimal("60")})
            schema = (bigquery.SchemaField("month", "DATE"), bigquery.SchemaField("revenue", "NUMERIC"))
        else:
            rows = ({"revenue": Decimal("0" if self.state == "empty" else "80")},)
            schema = (bigquery.SchemaField("revenue", "NUMERIC"),)
        return release_evidence(query, rows, schema, job_id="report_fixture", estimated_bytes=10,
            billed_bytes=10, query_as_of=NOW,
            coverage=Coverage(status="checked", source_rows=20,
                period_rows=0 if self.state == "empty" else 12, note="Synthetic report oracle.", job_id="coverage_fixture"))


@pytest.fixture
def harness(tmp_path):
    script = {}
    selections = [ReportDraft(recommendations=(ActionSelection(kind="review_performance", finding_index=0),))]
    ReportTurn.queries, ReportTurn.state = [], "normal"
    class Gateway:
        def __init__(self, settings, *, budget):
            self.budget = budget
        def interpret(self, question, **kwargs):
            self.budget.charge("injected_model")
            item = script[question]
            return item(kwargs) if callable(item) else item
        def report(self, evidence, *, report_requested):
            self.budget.charge("injected_model")
            item = selections[0]
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item()
            return item
    path = tmp_path / "reports.sqlite"
    store = SQLiteStore(path, clock=lambda: NOW)
    def flow(current=None):
        return AssistantWorkflow(SETTINGS, current or store, gateway_factory=Gateway,
                                 turn_factory=ReportTurn, today=date(2026, 11, 4))
    return script, selections, store, flow, path


@pytest.mark.parametrize("q,start,end,label", [
    (1, "2025-04-01", "2025-07-01", "Q2 2025 actions"),
    (3, "2025-10-01", "2026-01-01", "Q4 2025 actions"),
    (4, "2026-01-01", "2026-04-01", "Q1 2026 actions")])
def test_quarter_action_horizons_and_no_future_queries(harness, q, start, end, label):
    script, _, store, flow, _ = harness
    script["Prepare the report"] = I(status="ready", request=request(q=q,
        action=T(kind="reference", reference="analysis", unit="quarter", offset=1)))
    result = flow().run(OWNER, "Prepare the report", conversation_id="horizon", turn_id="same")
    assert result.status == "ok" and result.report_completeness == "complete"
    assert result.resolved_periods["action"] == {"start": start, "end": end}
    assert label in result.text and "no future results queried or asserted" in result.text
    assert len(ReportTurn.queries) == 1 and result.query_executions == 3 and result.model_requests == 2
    assert ReportTurn.queries[0].parameter("end_ts").value.date().isoformat() <= start
    stored = store.report(OWNER, result.report_id)
    assert stored["report_completeness"] == "complete"
    assert json.loads(stored["report_composition_json"])["request"]["action"] == result.resolved_periods["action"]


def test_generic_report_actions_have_no_invented_quarter(harness):
    script, _, _, flow, _ = harness
    script["A revenue briefing"] = I(status="ready", request=request())
    result = flow().run(OWNER, "A revenue briefing")
    assert result.status == "ok" and "Recommended actions" in result.text
    assert result.resolved_periods["action"] is None
    assert not any(f"Q{q} " in result.text for q in range(1, 5))
    assert len(ReportTurn.queries) == 1


@pytest.mark.parametrize("failure", ["absent", "irrelevant", "rejected", "provider", "refusal", "malformed"])
def test_missing_or_rejected_actions_are_partial_and_persisted(harness, failure):
    script, selections, store, flow, _ = harness
    choices = {
        "absent": ReportDraft(),
        "irrelevant": ReportDraft(recommendations=(ActionSelection(kind="review_retention", finding_index=0),)),
        "rejected": ReportDraft(recommendations=(ActionSelection(kind="review_performance", finding_index=19),)),
        "provider": ModelFailure("direct_failure"), "refusal": ReportDraft(status="refused"),
        "malformed": lambda: ReportDraft.model_validate({"headline": "Will grow 90%"}),
    }
    selections[0] = choices[failure]
    script["A requested report"] = I(status="ready", request=request())
    result = flow().run(OWNER, "A requested report")
    assert result.status == "partial" and result.report_completeness == "partial"
    assert result.factual_status == "grounded" and result.factual_findings
    assert result.suggestions == () and result.text.startswith("Partial report:")
    assert "recommendations" in result.report_composition["missing"] and "Recovery:" in result.text
    assert store.list_reports(OWNER)[0]["title"].startswith("Partial report:")
    stored = store.report(OWNER, result.report_id)
    assert stored["report_completeness"] == "partial" and stored["content"] == result.text
    assert result.model_requests == 2 and result.query_executions == 3


@pytest.mark.parametrize("purpose,presentation,detail,sections", [
    ("performance", "table", "detailed", ("performance_table", "recommendations")),
    ("executive", "briefing", "concise", ("summary", "limitations")),
    ("causal_investigation", "narrative", "standard", ("summary", "recommendations")),
])
def test_distinct_report_goals_and_explicit_sections(harness, purpose, presentation, detail, sections):
    script, _, _, flow, _ = harness
    spec = ReportSpec(purpose=purpose, presentation=presentation, detail=detail, requested_sections=sections)
    script["The selected layout"] = I(status="ready", request=request(report=spec))
    result = flow().run(OWNER, "The selected layout")
    assert result.status == "ok" and result.report_composition["spec"] == spec.model_dump(mode="json")
    assert f"Presentation: {presentation}; detail: {detail}" in result.text
    if presentation == "table":
        assert "  revenue\n  80" in result.text
    if "performance_table" not in sections:
        assert "revenue=80" not in result.text
    kinds = [s["kind"] for s in result.report_composition["sections"]]
    assert all(k in kinds for k in sections) and "summary" in kinds and "limitations" in kinds
    if purpose == "causal_investigation":
        assert "do not establish causes" in result.text


def test_non_revenue_report_does_not_force_fixed_pair(harness):
    script, _, _, flow, _ = harness
    script["Order count report"] = I(status="ready", request=request(queries=(Q(operation="aggregate", metric="order_count"),)))
    result = flow().run(OWNER, "Order count report")
    assert result.status == "ok" and result.factual_findings[0]["value"] == "4"
    assert len(ReportTurn.queries) == 1 and ReportTurn.queries[0].metric_definition["metric"] == "order_count"


def test_explicit_no_actions_can_complete_but_explicit_horizon_requires_action(harness):
    script, selections, _, flow, _ = harness
    selections[0] = ReportDraft()
    spec = ReportSpec(requested_sections=("summary", "limitations"))
    script["Facts only"] = I(status="ready", request=request(report=spec))
    script["Same facts with Q4 actions"] = I(status="ready", request=request(report=spec, action=quarter(4)))
    first = flow().run(OWNER, "Facts only")
    second = flow().run(OWNER, "Same facts with Q4 actions")
    assert first.status == "ok" and first.report_completeness == "complete"
    assert second.status == "partial" and second.report_composition["missing"] == ["recommendations"]


def test_trend_section_needs_monthly_evidence_and_no_hidden_queries(harness):
    script, selections, _, flow, _ = harness
    spec = ReportSpec(purpose="trend", requested_sections=("trend", "recommendations"))
    script["Monthly report"] = I(status="ready", request=request(report=spec, queries=(Q(operation="time_series", metric="revenue"),)))
    selections[0] = ReportDraft(recommendations=(ActionSelection(kind="review_trend", finding_index=0),))
    result = flow().run(OWNER, "Monthly report")
    assert result.status == "ok" and result.query_executions == 3
    trend = next(s for s in result.report_composition["sections"] if s["kind"] == "trend")
    assert trend["status"] == "supported" and trend["finding_refs"] == [0, 1] and trend["evidence_refs"] == [0]
    script["Aggregate with requested trend"] = I(status="ready", request=request(report=spec))
    selections[0] = ReportDraft(recommendations=(ActionSelection(kind="review_performance", finding_index=0),))
    partial = flow().run(OWNER, "Aggregate with requested trend")
    assert partial.status == "partial" and partial.report_composition["missing"] == ["trend"]
    assert partial.query_executions == 3 and len(ReportTurn.queries) == 2


@pytest.mark.parametrize("mode", ["report", "temporal_compare"])
def test_report_comparison_has_trusted_deltas_and_valid_actions(harness, mode):
    script, _, _, flow, _ = harness
    spec = ReportSpec(purpose="comparison", requested_sections=("comparison", "recommendations"))
    script["Compare quarter results"] = I(status="ready", request=R(mode=mode, report=spec,
        analysis=quarter(3), comparison=quarter(2),
        queries=(Q(operation="aggregate", metric="revenue"), Q(operation="aggregate", metric="revenue", temporal_role="comparison"))))
    result = flow().run(OWNER, "Compare quarter results")
    assert result.status == "ok" and result.report_completeness == "complete"
    assert [Decimal(f["value"]) for f in result.factual_findings] == [Decimal("30"), Decimal("60")]
    assert result.suggestions and result.query_executions == 5 and len(ReportTurn.queries) == 2
    section = next(s for s in result.report_composition["sections"] if s["kind"] == "comparison")
    assert section["status"] == "supported" and section["evidence_refs"] == [0, 1]


def test_comparison_goal_without_baseline_clarifies_before_analysis(harness):
    script, _, _, flow, _ = harness
    script["Compare results"] = I(status="ready", request=request(report=ReportSpec(purpose="comparison")))
    result = flow().run(OWNER, "Compare results")
    assert result.status == "needs_clarification" and ReportTurn.queries == []
    assert result.model_requests == result.query_executions == 1


def test_product_comparison_section_uses_one_observation_set(harness):
    script, selections, _, flow, _ = harness
    script["Compare product results"] = I(status="ready", request=request(report=ReportSpec(purpose="comparison"),
        queries=(Q(operation="product_comparison", metric="revenue", product_ids=(1, 2)),)))
    selections[0] = ReportDraft(recommendations=(ActionSelection(kind="investigate_price_volume", finding_index=2),))
    result = flow().run(OWNER, "Compare product results")
    assert result.status == "ok" and len(ReportTurn.queries) == 1 and result.resolved_periods["comparison"] is None
    assert result.factual_findings[2]["value"] == "17.50" and result.report_completeness == "complete"


def test_empty_data_report_is_partial_without_invented_actions(harness):
    script, _, store, flow, _ = harness
    ReportTurn.state = "empty"
    script["Empty report"] = I(status="ready", request=request())
    result = flow().run(OWNER, "Empty report")
    assert result.status == "partial" and result.factual_status == "evidence_only"
    assert result.factual_findings == result.suggestions == () and result.model_requests == 1
    assert set(result.report_composition["missing"]) >= {"summary", "observations", "evidence_completeness", "recommendations"}
    assert store.list_reports(OWNER)[0]["completeness"] == "partial"
    assert result.query_executions == 3


@pytest.mark.parametrize("kind", ["sparse", "suppressed", "null"])
def test_section_level_sparse_suppressed_and_undefined_evidence(kind):
    item = fixture_evidence()
    if kind == "sparse":
        item = item.model_copy(update={"complete": False})
    elif kind == "suppressed":
        item = item.model_copy(update={"rows": (), "complete": False})
    else:
        item = item.model_copy(update={"rows": ({"revenue": None},), "complete": False})
    resolved = resolve_request(request(report=ReportSpec(requested_sections=("summary", "trend", "comparison", "recommendations"))), reference_date=date(2026, 11, 4), analytical_timezone="UTC")
    report = ground_report(ReportDraft(), (item,), fallback=True)
    composition = compose_report(resolved, (item,), report)
    assert composition.completeness == "partial" and "evidence_completeness" in composition.missing
    assert {"trend", "comparison", "recommendations"} <= set(composition.missing)
    if kind != "sparse":
        assert "summary" in composition.missing


@pytest.mark.parametrize("partial", [False, True])
def test_exact_retry_soft_delete_and_no_resurrection(harness, partial):
    script, selections, store, flow, _ = harness
    if partial:
        selections[0] = ReportDraft()
    script["Save report"] = I(status="ready", request=request())
    first = flow().run(OWNER, "Save report", conversation_id="retry", turn_id="fixed")
    replay = flow().run(OWNER, "Save report", conversation_id="retry", turn_id="fixed")
    assert replay.reused_report and replay.status == first.status and replay.report_composition == first.report_composition
    assert replay.text == first.text and replay.resolved_periods == first.resolved_periods
    assert replay.model_requests == 0 and replay.query_executions == 1 and len(ReportTurn.queries) == 1
    proposal = store.preview_deletion(OWNER, "conversation", "retry")
    assert store.resolve_deletion(OWNER, proposal["token"])[0] == "deleted"
    assert store.resolve_deletion(OWNER, proposal["token"])[0] != "deleted"
    deleted = flow().run(OWNER, "Save report", conversation_id="retry", turn_id="fixed")
    assert deleted.status == "deleted" and deleted.model_requests == deleted.query_executions == 0
    assert store.load_continuity(OWNER, "retry") is None and store.list_reports(OWNER) == []
    assert len(ReportTurn.queries) == 1


def test_pending_report_choices_and_action_horizon_survive_restart_save(harness):
    script, selections, store, flow, path = harness
    spec = ReportSpec(purpose="performance", presentation="table", detail="detailed")
    action = T(kind="reference", reference="analysis", unit="quarter", offset=1)
    script["Q4 report awaiting year"] = I(status="needs_clarification", request=R(mode="report", report=spec,
        analysis=quarter(4, None), action=action, queries=(Q(operation="aggregate", metric="revenue"),)), issues=("missing_year",))
    first = flow().run(OWNER, "Q4 report awaiting year", conversation_id="pending")
    assert first.status == "needs_clarification" and ReportTurn.queries == []
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    script["2025"] = I(status="ready", context_action="continue", request=R(mode="report", analysis=quarter(4), queries=(Q(operation="aggregate", metric="revenue"),)))
    result = flow(current).run(OWNER, "2025", conversation_id="pending")
    assert result.status == "ok" and result.report_composition["spec"] == spec.model_dump(mode="json")
    assert result.resolved_periods["action"] == {"start": "2026-01-01", "end": "2026-04-01"}
    selections[0] = ModelFailure("direct_failure")  # Saving must never try to complete by a new report call.
    for intent in ("explain", "summary", "save"):
        script[intent] = I(status="ready", context_action="continue", request=R(intent=intent))
        followup = flow(current).run(OWNER, intent, conversation_id="pending")
        assert followup.status == "ok" and followup.resolved_periods == result.resolved_periods
        assert followup.report_composition == result.report_composition and followup.model_requests == 1
        assert followup.query_executions == 1 and followup.factual_findings == result.factual_findings
        assert "Q1 2026 actions" in followup.text
    assert len(ReportTurn.queries) == 1
    current.close()


def test_save_followup_cannot_complete_missing_actions(harness):
    script, selections, _, flow, _ = harness
    selections[0] = ReportDraft()
    script["Released analysis"] = I(status="ready", request=R(analysis=quarter(3), queries=(Q(operation="aggregate", metric="revenue"),)))
    first = flow().run(OWNER, "Released analysis", conversation_id="save-partial")
    script["Save it"] = I(status="ready", context_action="continue", request=R(intent="save"))
    saved = flow().run(OWNER, "Save it", conversation_id="save-partial")
    assert first.status == "ok" and saved.status == "partial" and saved.report_id
    assert saved.factual_findings == first.factual_findings and saved.model_requests == saved.query_executions == 1
    assert len(ReportTurn.queries) == 1 and "original as-of" in saved.text
    script["Explain the saved report"] = I(status="ready", context_action="continue", request=R(intent="explain"))
    explained = flow().run(OWNER, "Explain the saved report", conversation_id="save-partial")
    assert explained.status == "partial" and explained.report_composition == saved.report_composition
    assert explained.model_requests == explained.query_executions == 1 and len(ReportTurn.queries) == 1


@pytest.mark.parametrize("partial", [False, True])
def test_save_failure_rolls_back_completeness_and_continuity(harness, partial):
    script, selections, store, flow, _ = harness
    if partial:
        selections[0] = ReportDraft()
    script["Atomic report"] = I(status="ready", request=request())
    store.db.execute("CREATE TRIGGER abort_continuity BEFORE INSERT ON conversations BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    result = flow().run(OWNER, "Atomic report", conversation_id="atomic")
    assert result.status == "error" and result.report_id is None and "Saved report" not in result.text
    assert store.db.execute("SELECT count(*) FROM reports").fetchone()[0] == 0
    assert store.load_continuity(OWNER, "atomic") is None


def test_additive_migration_keeps_legacy_content_and_pending_proposal(harness):
    _, _, store, _, path = harness
    rid = store.save_report(owner=OWNER, conversation_id="old", turn_id="old", content="Original legacy content",
        metric_definitions=[], scope_reference="demo:" + OWNER, scope_version=1, evidence_refs=[])
    proposal = store.preview_deletion(OWNER, "conversation", "old")
    # Drop only new W4 columns to emulate a W2 database, including live proposal.
    store.db.execute("ALTER TABLE reports DROP COLUMN report_completeness")
    store.db.execute("ALTER TABLE reports DROP COLUMN report_composition_json")
    store.db.execute("ALTER TABLE reports DROP COLUMN report_support_json")
    store.db.execute("ALTER TABLE reports DROP COLUMN report_binding_digest")
    store.db.execute("PRAGMA user_version=2")
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    old = current.report(OWNER, rid)
    assert old["content"] == "Original legacy content" and old["report_completeness"] == "legacy"
    assert old["report_composition_json"] == "" and current.db.execute("PRAGMA user_version").fetchone()[0] == 5
    assert current.resolve_deletion(OWNER, proposal["token"]) == ("deleted", [rid])
    current.close()


def test_partial_telemetry_is_recognized_and_payload_free(tmp_path):
    path = tmp_path / "events.jsonl"
    trace = TurnTrace("synthetic-conversation", "synthetic-turn", path)
    trace.emit("report_persistence", status="partial", decision="saved", message="do not retain", purpose="sensitive purpose")
    trace.finish("partial", category="partial", requests=2, executions=3)
    content = path.read_text()
    assert "do not retain" not in content and "sensitive purpose" not in content
    records = [json.loads(line) for line in content.splitlines()]
    assert records[0]["status"] == "partial" and records[0]["purpose"] == "unknown"
    assert agent_metrics(path)["counts"] == {"outcome.partial": 1}


def test_report_schema_bars_free_prose_and_period_injection():
    for payload in ({"purpose": "make a guaranteed 90% gain"}, {"analysis": {"start": "1990-01-01"}},
                    {"requested_sections": ["summary", "summary"]}, {"requested_sections": ["causal_proof"]}):
        with pytest.raises(ValidationError):
            ReportSpec.model_validate(payload)
    schema = _strict_schema(I)
    assert schema["$defs"]["ReportSpec"]["additionalProperties"] is False
    assert "report" in schema["$defs"]["ProviderRequestSpec"]["properties"]


def test_incomplete_temporal_report_retains_observations_as_partial(harness):
    script, _, _, flow, _ = harness
    spec = ReportSpec(purpose="comparison")
    query = Q(operation="aggregate", metric="revenue", dimension="product", limit=2)
    script["Report on compared products"] = I(status="ready", request=R(mode="temporal_compare", report=spec,
        analysis=quarter(3), comparison=quarter(2), queries=(query, query.model_copy(update={"temporal_role": "comparison"}))))
    result = flow().run(OWNER, "Report on compared products")
    assert result.status == "partial" and result.report_id and result.factual_status == "grounded"
    assert result.factual_findings and all(f["selection"]["baseline"] is None for f in result.factual_findings)
    assert {"comparison", "evidence_completeness"} <= set(result.report_composition["missing"])
    assert result.query_executions == 5 and len(ReportTurn.queries) == 2
    assert "Definitions for E1:" in result.text and "Definitions for E2:" in result.text


def test_actual_partial_outcome_telemetry_and_cli_status(harness, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from opsfleet_assistant import cli
    script, selections, _, flow, _ = harness
    selections[0] = ReportDraft()
    script["A partial report"] = I(status="ready", request=request())
    result = flow().run(OWNER, "A partial report")
    counts = agent_metrics(tmp_path / "workflow-events.jsonl")["counts"]
    assert counts["outcome.partial"] == 1 and "outcome.other" not in counts
    assert '"status":"partial"' in (tmp_path / "workflow-events.jsonl").read_text()
    class Completed:
        def __init__(self, *args, **kwargs): pass
        def run(self, *args, **kwargs): return result
    monkeypatch.setattr(cli, "AssistantWorkflow", Completed)
    shown = CliRunner().invoke(cli.app, ["ask", "A partial report", "--user", OWNER,
        "--db", str(tmp_path / "cli.sqlite"), "--json"])
    assert shown.exit_code == 0
    payload = json.loads(shown.stdout)
    assert payload["status"] == "partial" and payload["report_completeness"] == "partial"
    assert payload["report_composition"]["missing"] == ["recommendations"]


def test_legacy_replay_does_not_retroactively_verify_completion(harness):
    script, _, store, flow, _ = harness
    script["A saved artifact"] = I(status="ready", request=request())
    first = flow().run(OWNER, "A saved artifact", conversation_id="legacy", turn_id="same")
    store.db.execute("UPDATE reports SET report_completeness='legacy',report_composition_json='',report_support_json='',report_binding_digest='' WHERE id=?", (first.report_id,))
    replay = flow().run(OWNER, "A saved artifact", conversation_id="legacy", turn_id="same")
    assert replay.reused_report and replay.text == first.text and replay.report_completeness == "legacy"
    assert replay.report_composition is None and replay.model_requests == 0 and replay.query_executions == 1
    assert store.report(OWNER, first.report_id)["report_completeness"] == "legacy"


@pytest.mark.parametrize("missing", ["comparison_query", "comparison_period"])
def test_report_comparison_missing_analytical_role_clarifies(harness, missing):
    script, _, _, flow, _ = harness
    queries = (Q(operation="aggregate", metric="revenue"),)
    comparison = quarter(2)
    if missing == "comparison_period":
        queries += (Q(operation="aggregate", metric="revenue", temporal_role="comparison"),)
        comparison = None
    script["A comparison awaiting details"] = I(status="ready", request=request(comparison=comparison, queries=queries))
    result = flow().run(OWNER, "A comparison awaiting details")
    assert result.status == "needs_clarification" and result.report_id is None
    assert ReportTurn.queries == [] and result.model_requests == result.query_executions == 1


@pytest.mark.parametrize("reply_kind", ["omit", "contradict", "correct"])
def test_non_year_clarification_preserves_or_explicitly_corrects_report_spec(harness, reply_kind):
    script, _, store, flow, path = harness
    original = ReportSpec(purpose="performance", presentation="table", detail="detailed",
                          requested_sections=("performance_table", "recommendations"))
    script["Quarterly report awaiting metric"] = I(status="needs_clarification", request=R(mode="report",
        report=original, analysis=quarter(3)), issues=("missing_metric",))
    flow().run(OWNER, "Quarterly report awaiting metric", conversation_id="spec")
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    changed = ReportSpec(purpose="executive", requested_sections=("summary", "limitations"))
    script["Use order count"] = I(status="ready", context_action="correct" if reply_kind == "correct" else "continue",
        request=request(report=None if reply_kind == "omit" else changed,
                        queries=(Q(operation="aggregate", metric="order_count"),)))
    result = flow(current).run(OWNER, "Use order count", conversation_id="spec")
    if reply_kind == "contradict":
        assert result.status == "error" and result.model_requests == 3 and ReportTurn.queries == []
        assert current.load_continuity(OWNER, "spec").pending.report == original
    else:
        assert result.status == "ok" and result.report_composition["spec"] == (changed if reply_kind == "correct" else original).model_dump(mode="json")
        assert len(ReportTurn.queries) == 1 and result.factual_findings[0]["value"] == "4"
    current.close()


def test_completed_followup_omitting_relative_horizon_retains_original_target(harness):
    script, _, store, flow, _ = harness
    script["Report with this year's actions"] = I(status="ready", request=request(
        action=T(kind="year", year=Y(kind="relative", offset=0))))
    first = flow().run(OWNER, "Report with this year's actions", conversation_id="original-target")
    script["What about 2024"] = I(status="ready", context_action="continue", request=request(q=3).model_copy(update={"analysis": quarter(3, 2024)}))
    later = flow()
    later.today = date(2029, 2, 1)
    result = later.run(OWNER, "What about 2024", conversation_id="original-target")
    assert first.resolved_periods["action"] == result.resolved_periods["action"] == {"start": "2026-01-01", "end": "2027-01-01"}
    assert result.resolved_periods["analysis"] == {"start": "2024-07-01", "end": "2024-10-01"}
    assert result.status == "ok" and len(ReportTurn.queries) == 2
    assert store.load_continuity(OWNER, "original-target").snapshot.request.action.model_dump(mode="json") == first.resolved_periods["action"]


@pytest.mark.parametrize("mutation", ["spec", "sections", "missing", "version", "references"])
def test_stored_composition_contract_rejects_independent_metadata_corruption(harness, mutation):
    from opsfleet_assistant.reporting import ReportComposition
    script, _, _, flow, _ = harness
    script["Structured report"] = I(status="ready", request=request(report=ReportSpec(purpose="performance")))
    result = flow().run(OWNER, "Structured report")
    payload = result.report_composition
    if mutation == "spec": payload["spec"]["purpose"] = "executive"
    if mutation == "sections": payload["sections"].pop()
    if mutation == "missing": payload["missing"] = ["recommendations"]
    if mutation == "version": payload["schema_version"] = 2
    if mutation == "references": payload["sections"][0]["finding_refs"] = [900]
    with pytest.raises(ValidationError):
        ReportComposition.model_validate(payload)


@pytest.mark.parametrize("suppressed", [False, True])
def test_real_release_demographic_report_does_not_require_protected_counts(harness, suppressed):
    script, selections, store, flow, _ = harness
    ReportTurn.state = "suppressed" if suppressed else "normal"
    selections[0] = ReportDraft(recommendations=(ActionSelection(kind="review_cohort_mix", finding_index=2),))
    script["Aggregate cohort report"] = I(status="ready", request=request(
        queries=(Q(operation="demographic_comparison", metric="average_customer_spend", dimension="state", limit=10),),
        report=ReportSpec(purpose="comparison")))
    result = flow().run(OWNER, "Aggregate cohort report", conversation_id="cohort")
    assert result.status == ("partial" if suppressed else "ok") and result.report_id
    assert result.query_executions == 3 and len(ReportTurn.queries) == 1
    item = store.load_continuity(OWNER, "cohort").snapshot.evidence[0]
    assert item.coverage.source_rows is None and item.coverage.period_rows is None
    if suppressed:
        assert result.factual_findings == result.suggestions == () and result.model_requests == 1
        assert "evidence_completeness" in result.report_composition["missing"]
    else:
        assert result.report_completeness == "complete" and result.factual_findings[2]["value"] == "10"
        assert result.suggestions and result.model_requests == 2
