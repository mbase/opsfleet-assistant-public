"""Review regressions: independent operands, real compiler/release, isolated state."""
from datetime import date
from decimal import Decimal
import json

from google.cloud import bigquery
import pytest

from opsfleet_assistant.contracts import (ActionSelection, FindingSelection, ObservationRef,
    InterpretationResult as I, RequestSpec as R, QuerySpec as Q, ReportSpec, ReportDraft,
    TemporalExpression as T, YearExpression as Y)
from opsfleet_assistant.evidence import Coverage, release_evidence
from opsfleet_assistant.model_gateway import ModelFailure
from opsfleet_assistant.store import SQLiteStore
from test_step4 import FakeTurn
from test_report_completeness import harness, request, quarter, OWNER, NOW


class ReviewTurn(FakeTurn):
    rows = (({"revenue": Decimal("87.125")},), ({"revenue": Decimal("125.5")},))
    queries = []
    def execute_compiled(self, query):
        self.queries.append(query)
        index = getattr(self, "analytical_calls", 0)
        self.analytical_calls = index + 1
        self.executions += 2
        self.billed_bytes += 20
        schema = tuple(bigquery.SchemaField(k, "INT64" if k in {"product_id", "order_count"} else "NUMERIC")
                       for k in query.result_contract.columns)
        return release_evidence(query, self.rows[index], schema, job_id=f"review_{index}", estimated_bytes=10,
            billed_bytes=10, query_as_of=NOW, coverage=Coverage(status="checked", source_rows=30,
                period_rows=10, note="Independent review operands.", job_id=f"coverage_{index}"))


@pytest.fixture(autouse=True)
def reset_review_turn():
    ReviewTurn.queries = []
    ReviewTurn.rows = (({"revenue": Decimal("87.125")},), ({"revenue": Decimal("125.5")},))


def using(flow, store=None):
    result = flow(store)
    result.turn_factory = ReviewTurn
    return result


def goal(intent):
    return I(status="ready", context_action="continue", request=R(intent=intent))


def pair(*, report=None):
    return R(mode="temporal_compare", report=report, analysis=quarter(3), comparison=quarter(2),
        queries=(Q(operation="aggregate", metric="revenue", dimension="product"),
                 Q(operation="aggregate", metric="revenue", dimension="product", temporal_role="comparison")))


def test_w4_r1_baseline_extra_group_fails_closed(harness):
    script, _, store, flow, _ = harness
    ReviewTurn.rows = (({"product_id": 1, "revenue": Decimal("150")},),
        ({"product_id": 1, "revenue": Decimal("100")}, {"product_id": 2, "revenue": Decimal("200")}))
    script["Compare released groups"] = I(status="ready", request=pair())
    result = using(flow).run(OWNER, "Compare released groups", conversation_id="invalid")
    assert result.status == "error" and result.factual_findings == ()
    assert store.load_continuity(OWNER, "invalid") is None
    assert result.model_requests == 2 and result.query_executions == 5


@pytest.mark.parametrize("purpose", ["executive", "performance"])
def test_w4_r2_comparison_section_without_choices_clarifies(harness, purpose):
    script, _, store, flow, _ = harness
    script["A composable report"] = I(status="ready", request=request(report=ReportSpec(purpose=purpose,
        requested_sections=("summary", "comparison", "recommendations"))))
    result = using(flow).run(OWNER, "A composable report", conversation_id="missing-baseline")
    assert result.status == "needs_clarification" and result.report_id is None
    assert result.model_requests == result.query_executions == 1 and ReviewTurn.queries == []
    assert "comparison" in store.load_continuity(OWNER, "missing-baseline").pending.report.requested_sections


def test_w4_r3_metric_year_correction_keeps_unrelated_report_choices(harness):
    script, _, store, flow, path = harness
    spec = ReportSpec(purpose="performance", presentation="table", detail="detailed",
                      requested_sections=("performance_table", "recommendations"))
    action = T(kind="reference", reference="analysis", unit="quarter", offset=1)
    script["Report awaiting year"] = I(status="needs_clarification", request=R(mode="report", report=spec,
        analysis=quarter(4, None), action=action, queries=(Q(operation="aggregate", metric="revenue"),)), issues=("missing_year",))
    using(flow).run(OWNER, "Report awaiting year", conversation_id="correction")
    expiry = store.load_continuity(OWNER, "correction").expires_at
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    script["Actually order count in 2025"] = I(status="ready", context_action="correct", request=request(q=4,
        queries=(Q(operation="aggregate", metric="order_count"),)))
    ReviewTurn.rows = (({"order_count": 4},),)
    result = using(flow, current).run(OWNER, "Actually order count in 2025", conversation_id="correction")
    assert result.status == "ok" and result.report_composition["spec"] == spec.model_dump(mode="json")
    assert result.resolved_periods["action"] == {"start": "2026-01-01", "end": "2026-04-01"}
    assert current.load_continuity(OWNER, "correction").expires_at == expiry
    current.close()


@pytest.mark.parametrize("variant", ["scope", "definition"])
def test_w3_r2_incompatible_defaults_preserve_independent_observations(harness, variant):
    script, selections, _, flow, _ = harness
    queries = (Q(operation="aggregate", metric="revenue", **({"requested_brands": ("Calvin Klein",)} if variant == "scope" else {"revenue_statuses": ("Complete",)})),
               Q(operation="aggregate", metric="revenue", **({"requested_brands": ("Carhartt",)} if variant == "scope" else {"revenue_statuses": ("Returned",)})))
    script["Two independent observations"] = I(status="ready", request=request(queries=queries,
        report=ReportSpec(purpose="performance", requested_sections=("summary", "performance_table", "limitations"))))
    selections[0] = ReportDraft(findings=tuple(FindingSelection(operation="observed_value",
        current=ObservationRef(evidence_index=i, row_index=0, metric="revenue")) for i in (0, 1)))
    result = using(flow).run(OWNER, "Two independent observations")
    assert result.status == "ok" and result.factual_status == "grounded"
    assert [f["value"] for f in result.factual_findings] == ["87.125", "125.5"]
    assert "selection: selected" in result.text and result.report_composition["missing"] == []


def test_w3_r3_summary_keeps_each_definition(harness):
    script, _, _, flow, _ = harness
    ReviewTurn.rows = (({"revenue": Decimal("87.125")},), ({"order_count": 4},))
    script["Different metric conventions"] = I(status="ready", request=request(
        report=ReportSpec(requested_sections=("summary", "performance_table", "limitations")), queries=(
            Q(operation="aggregate", metric="revenue", revenue_statuses=("Complete",)),
            Q(operation="aggregate", metric="order_count", revenue_statuses=("Returned",)))))
    script["Summary"] = goal("summary")
    first = using(flow).run(OWNER, "Different metric conventions", conversation_id="definitions")
    result = using(flow).run(OWNER, "Summary", conversation_id="definitions")
    assert first.status == result.status == "ok" and result.factual_findings == first.factual_findings
    assert "Definitions for E1:" in result.text and "Definitions for E2:" in result.text
    assert '"Complete"' in result.text and '"Returned"' in result.text
    assert result.model_requests == result.query_executions == 1 and len(ReviewTurn.queries) == 2


@pytest.mark.parametrize("mutation", ["execution", "complete"])
def test_w2_r4_retry_rejects_internally_valid_but_unbound_metadata(harness, mutation):
    script, selections, store, flow, path = harness
    if mutation == "complete": selections[0] = ReportDraft()
    script["Stored report"] = I(status="ready", request=request())
    first = using(flow).run(OWNER, "Stored report", conversation_id="retry", turn_id="same")
    original = store.report(OWNER, first.report_id)
    comp = json.loads(original["report_composition_json"])
    if mutation == "execution":
        comp["request"]["analysis"] = {"start": "2020-07-01", "end": "2020-10-01"}
        comp["request"]["spec"]["analysis"]["year"]["value"] = 2020
        comp["request"]["spec"]["queries"][0]["metric"] = "order_count"
    else:
        comp["missing"] = []
        comp["completeness"] = "complete"
        next(s for s in comp["sections"] if s["kind"] == "recommendations").update(
            status="supported", finding_refs=[0], evidence_refs=[0])
        store.db.execute("UPDATE reports SET report_completeness='complete'")
    store.db.execute("UPDATE reports SET report_composition_json=? WHERE id=?", (json.dumps(comp), first.report_id))
    store.clear_continuity(OWNER, "retry")
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    result = using(flow, current).run(OWNER, "Stored report", conversation_id="retry", turn_id="same")
    assert result.status == "error" and result.factual_findings == () and result.report_composition is None
    assert result.model_requests == 0 and result.query_executions <= 1 and len(ReviewTurn.queries) == 1
    saved = current.report(OWNER, first.report_id)
    assert saved["content"] == original["content"] and saved["version"] == original["version"]
    current.close()


@pytest.mark.parametrize("side", ["baseline", "current"])
@pytest.mark.parametrize("as_report", [False, True])
def test_w4_r1_both_group_sets_across_restart_save(harness, side, as_report):
    script, _, store, flow, path = harness
    single = ({"product_id": 1, "revenue": Decimal("150")},)
    both = ({"product_id": 1, "revenue": Decimal("100")}, {"product_id": 2, "revenue": Decimal("200")})
    ReviewTurn.rows = (single, both) if side == "baseline" else (both, single)
    script["Paired coverage"] = I(status="ready", request=pair(report=ReportSpec(purpose="comparison") if as_report else None))
    first = using(flow).run(OWNER, "Paired coverage", conversation_id="coverage")
    assert first.status == ("partial" if as_report else "error")
    if as_report:
        assert first.report_composition["missing"] == ["comparison"] and first.factual_findings
    else:
        assert first.factual_findings == () and store.load_continuity(OWNER, "coverage") is None
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    for intent in ("explain", "summary", "save"):
        script[intent] = goal(intent)
        result = using(flow, current).run(OWNER, intent, conversation_id="coverage")
        assert result.status == ("partial" if as_report else "needs_clarification")
        assert result.model_requests == result.query_executions == 1
        assert result.factual_findings == (first.factual_findings if as_report else ())
    assert len(ReviewTurn.queries) == 2
    current.close()


@pytest.mark.parametrize("outcome", ["selected", "refused", "provider"])
def test_w4_r1_reordered_matching_zero_baseline_retains_provenance(harness, outcome):
    script, selections, store, flow, path = harness
    ReviewTurn.rows = (({"product_id": 1, "revenue": Decimal("150")}, {"product_id": 2, "revenue": Decimal("0")}),
        ({"product_id": 2, "revenue": Decimal("0")}, {"product_id": 1, "revenue": Decimal("100")}))
    if outcome == "refused": selections[0] = ReportDraft(status="refused")
    if outcome == "provider": selections[0] = ModelFailure("direct_failure")
    script["Matching groups"] = I(status="ready", request=pair())
    first = using(flow).run(OWNER, "Matching groups", conversation_id="matched")
    assert first.status == "ok" and [f["value"] for f in first.factual_findings] == ["50", "50.0", "0", None]
    status = "fallback" if outcome == "provider" else outcome
    assert f"selection: {status}" in first.text and first.query_executions == 5
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    for intent in ("explain", "why", "summary", "save"):
        script[intent] = goal(intent)
        result = using(flow, current).run(OWNER, intent, conversation_id="matched", turn_id=intent)
        assert result.factual_findings == first.factual_findings
        assert f"selection: {status}" in result.text and result.model_requests == result.query_executions == 1
        assert result.status == ("partial" if intent == "save" and outcome != "selected" else "ok")
    replay = using(flow, current).run(OWNER, "save", conversation_id="matched", turn_id="save")
    assert replay.reused_report and replay.text == result.text and replay.factual_findings == first.factual_findings
    assert replay.model_requests == 0 and replay.query_executions == 1 and len(ReviewTurn.queries) == 2
    current.close()


@pytest.mark.parametrize("purpose", ["executive", "performance", "comparison"])
def test_w4_r2_pending_comparison_completion_retains_sections_targets(harness, purpose):
    script, _, store, flow, path = harness
    spec = ReportSpec(purpose=purpose, requested_sections=("comparison", "recommendations"), presentation="table", detail="detailed")
    horizon = T(kind="reference", reference="analysis", unit="quarter", offset=1)
    script["Report requiring baseline"] = I(status="ready", request=request(report=spec, action=horizon))
    first = using(flow).run(OWNER, "Report requiring baseline", conversation_id="baseline")
    assert first.status == "needs_clarification" and ReviewTurn.queries == []
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    script["Compare with Q2"] = I(status="ready", context_action="continue", request=R(mode="report",
        analysis=quarter(3), comparison=quarter(2), queries=(Q(operation="aggregate", metric="revenue"),
            Q(operation="aggregate", metric="revenue", temporal_role="comparison"))))
    completed = using(flow, current).run(OWNER, "Compare with Q2", conversation_id="baseline")
    assert completed.status == "ok" and completed.report_composition["spec"] == spec.model_dump(mode="json")
    assert completed.resolved_periods["action"] == {"start": "2025-10-01", "end": "2026-01-01"}
    assert Decimal(completed.factual_findings[0]["value"]) == Decimal("-38.375")
    assert completed.model_requests == 2 and completed.query_executions == 5
    current.close()


@pytest.mark.parametrize("change", ["status", "product", "metric"])
def test_w4_r3_non_temporal_corrections_preserve_anchors_and_expiry(harness, change):
    from datetime import timedelta
    script, _, store, flow, path = harness
    original = T(kind="window", unit="month", count=1, offset=0, include_current=False)
    action = T(kind="reference", reference="analysis", unit="quarter", offset=1)
    spec = ReportSpec(purpose="performance", presentation="table", detail="detailed")
    script["Pending relative report"] = I(status="needs_clarification", request=R(mode="report", report=spec,
        analysis=original, action=action, queries=(Q(operation="aggregate", metric="revenue"),)), issues=("unresolved_interpretation",))
    using(flow).run(OWNER, "Pending relative report", conversation_id="retained")
    prior = store.load_continuity(OWNER, "retained")
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW + timedelta(minutes=10))
    query = Q(operation="aggregate", metric="order_count" if change == "metric" else "revenue",
              product_ids=(1,) if change == "product" else None,
              revenue_statuses=("Complete",) if change == "status" else ("Shipped", "Complete", "Processing"))
    if change == "metric": ReviewTurn.rows = (({"order_count": 4},),)
    script["Change one analytical choice"] = I(status="ready", context_action="correct", request=R(mode="report", analysis=original, queries=(query,)))
    assistant = using(flow, current)
    assistant.today = date(2030, 1, 1)
    result = assistant.run(OWNER, "Change one analytical choice", conversation_id="retained")
    assert result.status == "ok" and result.report_composition["spec"] == spec.model_dump(mode="json")
    assert result.resolved_periods["analysis"] == {"start": "2026-10-01", "end": "2026-11-01"}
    snapshot = current.load_continuity(OWNER, "retained").snapshot
    assert snapshot.resolution_role_anchors["analysis"] == prior.pending_resolution.anchors["analysis"]
    assert snapshot.resolution_role_anchors["action"] == prior.pending_resolution.anchors["action"]
    assert current.load_continuity(OWNER, "retained").expires_at == prior.expires_at
    current.close()


@pytest.mark.parametrize("change", ["replace", "reset", "clear", "target", "refresh", "new"])
def test_w4_r3_explicit_changes_and_clears_remain_possible(harness, change):
    script, _, store, flow, _ = harness
    old_spec = ReportSpec(purpose="performance", presentation="table", detail="detailed")
    original = T(kind="window", unit="month", count=1, offset=0, include_current=False)
    old_action = T(kind="reference", reference="analysis", unit="quarter", offset=1)
    script["Pending report choices"] = I(status="needs_clarification", request=R(mode="report", report=old_spec,
        analysis=original, action=old_action, queries=(Q(operation="aggregate", metric="revenue"),)), issues=("unresolved_interpretation",))
    using(flow).run(OWNER, "Pending report choices", conversation_id="explicit")
    replacement = ReportSpec(purpose="executive", requested_sections=("summary", "limitations"))
    proposal = R(mode="report", analysis=original, queries=(Q(operation="aggregate", metric="revenue"),),
        report=replacement if change == "replace" else None,
        action=quarter(2, 2031) if change == "target" else None)
    script["Apply explicit change"] = I(status="ready", context_action="new" if change == "new" else "correct", request=proposal,
        clear_fields=("report",) if change == "reset" else ("action",) if change == "clear" else (),
        temporal_updates=("analysis",) if change == "refresh" else ())
    assistant = using(flow)
    assistant.today = date(2030, 1, 1)
    result = assistant.run(OWNER, "Apply explicit change", conversation_id="explicit")
    assert result.status == "ok"
    assert result.report_composition["spec"] == (replacement if change == "replace" else ReportSpec() if change in {"reset", "new"} else old_spec).model_dump(mode="json")
    assert (result.resolved_periods["action"] is None) == (change in {"clear", "new"})
    if change == "target": assert result.resolved_periods["action"] == {"start": "2031-04-01", "end": "2031-07-01"}
    assert result.resolved_periods["analysis"]["start"] == ("2029-12-01" if change in {"refresh", "new"} else "2026-10-01")


@pytest.mark.parametrize("variant", ["scope", "definition"])
@pytest.mark.parametrize("outcome", ["selected", "neutral", "refused", "provider"])
@pytest.mark.parametrize("comparison", [False, True])
def test_w3_r2_independent_observation_provenance_lifecycle(harness, variant, outcome, comparison):
    script, selections, store, flow, path = harness
    sections = ("summary", "performance_table", "limitations") + (("comparison",) if comparison else ())
    # This lifecycle test explicitly requests the selected action it verifies.
    # Facts-only omission is covered separately by post-full remediation checks.
    if outcome == "selected": sections += ("recommendations",)
    qs = (Q(operation="aggregate", metric="revenue", **({"requested_brands": ("Calvin Klein",)} if variant == "scope" else {"revenue_statuses": ("Complete",)})),
          Q(operation="aggregate", metric="revenue", **({"requested_brands": ("Carhartt",)} if variant == "scope" else {"revenue_statuses": ("Returned",)})))
    script["Independent report"] = I(status="ready", request=request(report=ReportSpec(purpose="performance", requested_sections=sections), queries=qs))
    if outcome == "selected":
        selections[0] = ReportDraft(findings=tuple(FindingSelection(operation="observed_value", current=ObservationRef(evidence_index=i, row_index=0, metric="revenue")) for i in (0, 1)), recommendations=(ActionSelection(kind="review_performance", finding_index=1),))
    elif outcome == "neutral": selections[0] = ReportDraft()
    elif outcome == "refused": selections[0] = ReportDraft(status="refused")
    else: selections[0] = ModelFailure("direct_failure")
    first = using(flow).run(OWNER, "Independent report", conversation_id="independent", turn_id="initial")
    assert first.status == ("partial" if comparison else "ok")
    assert [f["value"] for f in first.factual_findings] == ["87.125", "125.5"]
    assert all(f["selection"]["baseline"] is None for f in first.factual_findings)
    assert first.report_composition["missing"] == (["comparison"] if comparison else [])
    state = "selected" if outcome in {"selected", "neutral"} else "fallback" if outcome == "provider" else "refused"
    assert f"selection: {state}" in first.text
    assert bool(first.suggestions) == (outcome == "selected")
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    for intent in ("explain", "why", "summary", "save"):
        script[intent] = goal(intent)
        result = using(flow, current).run(OWNER, intent, conversation_id="independent", turn_id=intent)
        assert result.status == first.status and result.factual_findings == first.factual_findings
        assert result.suggestions == first.suggestions and result.report_composition == first.report_composition
        assert f"selection: {state}" in result.text and "Definitions for E1:" in result.text and "Definitions for E2:" in result.text
        assert result.model_requests == result.query_executions == 1 and NOW.isoformat() in result.text
    replay = using(flow, current).run(OWNER, "save", conversation_id="independent", turn_id="save")
    assert replay.reused_report and replay.text == result.text and replay.factual_findings == first.factual_findings
    assert replay.report_composition == result.report_composition and replay.model_requests == 0
    preview = current.preview_deletion(OWNER, "conversation", "independent")
    assert current.resolve_deletion(OWNER, preview["token"])[0] == "deleted"
    assert current.load_continuity(OWNER, "independent") is None and len(ReviewTurn.queries) == 2
    current.close()


@pytest.mark.parametrize("homogeneous", [False, True])
def test_w3_r3_definition_association_survives_all_contextual_routes(harness, homogeneous):
    script, _, store, flow, path = harness
    ReviewTurn.rows = (({"revenue": Decimal("87.125")},), ({"order_count": 4},))
    script["Metric definitions"] = I(status="ready", request=request(report=ReportSpec(
        requested_sections=("summary", "performance_table", "limitations")), queries=(
        Q(operation="aggregate", metric="revenue", revenue_statuses=("Complete",)),
        Q(operation="aggregate", metric="order_count", revenue_statuses=("Complete",) if homogeneous else ("Returned",)))))
    first = using(flow).run(OWNER, "Metric definitions", conversation_id="per-evidence")
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW)
    for intent in ("explain", "why", "summary", "save"):
        script[intent] = goal(intent)
        result = using(flow, current).run(OWNER, intent, conversation_id="per-evidence", turn_id=intent)
        assert result.factual_findings == first.factual_findings and result.status == "ok"
        assert '"metric": "revenue"' in result.text and '"metric": "order_count"' in result.text
        line1 = next(line for line in result.text.splitlines() if line.startswith("Definitions for E1:"))
        line2 = next(line for line in result.text.splitlines() if line.startswith("Definitions for E2:"))
        assert '"Complete"' in line1 and ('"Complete"' if homogeneous else '"Returned"') in line2
        assert NOW.isoformat() in result.text and result.model_requests == result.query_executions == 1
    replay = using(flow, current).run(OWNER, "save", conversation_id="per-evidence", turn_id="save")
    assert replay.text == result.text and replay.factual_findings == first.factual_findings
    assert replay.model_requests == 0 and len(ReviewTurn.queries) == 2
    current.close()


@pytest.mark.parametrize("reseal", [False, True])
@pytest.mark.parametrize("mutation", ["execution", "complete", "goal", "action", "comparison", "finding_ref", "evidence_ref", "definition"])
def test_w2_r4_saved_semantics_checked_even_with_resealed_checksum(harness, mutation, reseal):
    from datetime import timedelta
    from opsfleet_assistant.report_artifact import artifact_binding_digest
    script, selections, store, flow, path = harness
    if mutation == "complete": selections[0] = ReportDraft()
    spec = ReportSpec(purpose="performance")
    script["Immutable report"] = I(status="ready", request=request(report=spec, action=quarter(4)))
    first = using(flow).run(OWNER, "Immutable report", conversation_id="bound", turn_id="fixed")
    original = store.report(OWNER, first.report_id)
    comp = json.loads(original["report_composition_json"])
    if mutation == "execution":
        comp["request"]["analysis"] = {"start": "2020-07-01", "end": "2020-10-01"}
        comp["request"]["spec"]["analysis"]["year"]["value"] = 2020
        comp["request"]["spec"]["queries"][0]["metric"] = "order_count"
    elif mutation == "complete":
        comp["missing"], comp["completeness"] = [], "complete"
        next(s for s in comp["sections"] if s["kind"] == "recommendations").update(status="supported", finding_refs=[0], evidence_refs=[0])
        store.db.execute("UPDATE reports SET report_completeness='complete'")
    elif mutation == "goal":
        comp["spec"]["purpose"] = comp["request"]["spec"]["report"]["purpose"] = "executive"
    elif mutation == "action":
        comp["request"]["action"] = {"start": "2026-10-01", "end": "2027-01-01"}
        comp["request"]["spec"]["action"]["year"]["value"] = 2026
    elif mutation == "comparison":
        comp["request"]["comparison"] = {"start": "2025-04-01", "end": "2025-07-01"}
        comp["request"]["spec"]["comparison"] = quarter(2).model_dump(mode="json")
    elif mutation == "finding_ref": comp["sections"][0]["finding_refs"] = [1]
    elif mutation == "evidence_ref": comp["sections"][0]["evidence_refs"] = [1]
    elif mutation == "definition":
        defs = json.loads(original["metric_definitions_json"])
        defs[0]["revenue_statuses"] = ["Returned"]
        store.db.execute("UPDATE reports SET metric_definitions_json=?", (json.dumps(defs),))
    store.db.execute("UPDATE reports SET report_composition_json=?", (json.dumps(comp),))
    if reseal:
        fields = store.report(OWNER, first.report_id)
        store.db.execute("UPDATE reports SET report_binding_digest=?", (artifact_binding_digest(fields),))
    # Half the matrix uses expired continuity, half explicitly absent continuity.
    if reseal: store.clear_continuity(OWNER, "bound")
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW + timedelta(hours=1))
    result = using(flow, current).run(OWNER, "Immutable report", conversation_id="bound", turn_id="fixed")
    assert result.status == "error" and result.report_id is None and result.factual_findings == ()
    assert result.report_composition is None and result.resolved_periods is None
    assert result.model_requests == 0 and result.query_executions == 1 and len(ReviewTurn.queries) == 1
    saved = current.report(OWNER, first.report_id)
    for name in ("content", "id", "version", "plans_json", "grounded_report_json", "evidence_refs_json", "report_support_json"):
        assert saved[name] == original[name]
    proposal = current.preview_deletion(OWNER, "conversation", "bound")
    assert current.resolve_deletion(OWNER, proposal["token"])[0] == "deleted"
    deleted = using(flow, current).run(OWNER, "Immutable report", conversation_id="bound", turn_id="fixed")
    assert deleted.status == "deleted" and deleted.model_requests == deleted.query_executions == 0
    current.close()


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("state", ["absent", "expired", "old_metadata"])
def test_w2_r4_valid_durable_replay_and_legacy_without_continuity(harness, partial, state):
    from datetime import timedelta
    script, selections, store, flow, path = harness
    if partial: selections[0] = ReportDraft()
    script["Durable report"] = I(status="ready", request=request(action=quarter(4)))
    first = using(flow).run(OWNER, "Durable report", conversation_id="durable", turn_id="same")
    original = store.report(OWNER, first.report_id)
    if state == "absent": store.clear_continuity(OWNER, "durable")
    if state == "old_metadata":
        # Existing schema-3 content remains intact; missing new durable support is
        # unavailable metadata, never a newly verified complete report.
        store.db.execute("UPDATE reports SET report_support_json='',report_binding_digest=''")
        store.clear_continuity(OWNER, "durable")
    store.close()
    current = SQLiteStore(path, clock=lambda: NOW + timedelta(hours=1))
    result = using(flow, current).run(OWNER, "Durable report", conversation_id="durable", turn_id="same")
    assert result.reused_report and result.text == first.text and result.report_id == first.report_id
    assert result.status == ("partial" if partial else "ok") and result.factual_findings == first.factual_findings
    assert result.model_requests == 0 and result.query_executions == 1 and len(ReviewTurn.queries) == 1
    if state == "old_metadata":
        assert result.report_completeness == "legacy" and result.report_composition is None and result.resolved_periods is None
    else:
        assert result.report_composition == first.report_composition and result.resolved_periods == first.resolved_periods
    assert current.report(OWNER, first.report_id)["content"] == original["content"]
    current.close()


@pytest.mark.parametrize("kwargs", [
    {"clear_fields": ("action",), "context_action": "continue"},
    {"clear_fields": ("action", "action"), "context_action": "correct"},
    {"clear_fields": ("report",), "context_action": "correct"},
])
def test_w4_r3_clear_signal_is_typed_and_cannot_clear_nonnull_choices(kwargs):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        I(status="ready", request=request(report=ReportSpec()), **kwargs)


@pytest.mark.parametrize("context_action", ["correct", "new"])
def test_w4_r3_bare_report_intent_survives_omitted_default_mode_only_on_correction(harness, context_action):
    script, _, _, flow, _ = harness
    script["Bare report pending year"] = I(status="needs_clarification", request=R(mode="report", analysis=quarter(4, None),
        action=T(kind="reference", reference="analysis", unit="quarter", offset=1),
        queries=(Q(operation="aggregate", metric="revenue"),)), issues=("missing_year",))
    using(flow).run(OWNER, "Bare report pending year", conversation_id="bare-report")
    script["Order count 2025"] = I(status="ready", context_action=context_action, request=R(analysis=quarter(4),
        queries=(Q(operation="aggregate", metric="order_count"),)))
    ReviewTurn.rows = (({"order_count": 4},),)
    result = using(flow).run(OWNER, "Order count 2025", conversation_id="bare-report")
    assert result.status == "ok" and bool(result.report_id) == (context_action == "correct")
    if context_action == "correct":
        assert result.report_composition["spec"] == ReportSpec().model_dump(mode="json")
        assert result.resolved_periods["action"] == {"start": "2026-01-01", "end": "2026-04-01"}
    else:
        assert result.report_composition is None and result.resolved_periods["action"] is None
