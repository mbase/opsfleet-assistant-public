"""Manual continuation regressions; scripted semantics, trusted compilation/release.

Expected calendars and call counts are fixed independently of the resolver.
Every workflow uses temporary SQLite and pytest's isolated telemetry fixture.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from opsfleet_assistant.contracts import (ReportDraft, ReportSpec,
    InterpretationResult as I, RequestSpec as R, QuerySpec as Q,
    TemporalExpression as T, YearExpression as Y)
from opsfleet_assistant.store import SQLiteStore
from test_report_completeness import harness, quarter, OWNER
from test_w4_review_regressions import ReviewTurn, using, reset_review_turn


@pytest.mark.parametrize("purpose", ["executive", "performance", "comparison"])
@pytest.mark.parametrize("variant", ["identical", "limit", "filter_order"])
def test_duplicate_scalar_comparison_choices_clarify_before_execution(harness, purpose, variant):
    script, _, store, flow, _ = harness
    query = Q(operation="aggregate", metric="revenue", requested_brands=("Calvin Klein", "Carhartt"),
              product_ids=(1, 2), revenue_statuses=("Complete", "Returned"))
    other = query if variant == "identical" else query.model_copy(update={"limit": 1}) if variant == "limit" else Q(
        operation="aggregate", metric="revenue", requested_brands=("carhartt", "calvin klein"),
        product_ids=(2, 1), revenue_statuses=("Returned", "Complete"))
    spec = ReportSpec(purpose=purpose, presentation="table", detail="detailed",
                      requested_sections=("comparison", "recommendations"))
    action = T(kind="reference", reference="analysis", unit="quarter", offset=1)
    script["Unresolved comparison"] = I(status="ready", request=R(mode="report", report=spec,
        analysis=quarter(3), action=action, queries=(query, other)))
    # Equal numbers are not the oracle: no analytical execution should occur.
    ReviewTurn.rows = (({"revenue": Decimal("87.125")},),) * 2
    result = using(flow).run(OWNER, "Unresolved comparison", conversation_id="duplicate")
    assert result.status == "needs_clarification", (result.status, result.report_completeness)
    assert result.report_id is None and result.factual_findings == () and ReviewTurn.queries == []
    assert result.model_requests == result.query_executions == 1
    pending = store.load_continuity(OWNER, "duplicate")
    assert pending.pending.report == spec and pending.pending.action == action
    assert pending.pending.queries == (query, other)
    assert store.list_reports(OWNER) == []


def window():
    return T(kind="window", unit="month", count=1, offset=0, include_current=False)


def test_completed_context_metric_correction_month_boundary_restart(harness):
    script, _, store, flow, path = harness
    life = datetime(2026, 11, 30, 23, 50, tzinfo=timezone.utc)
    store._now = lambda: life
    spec = ReportSpec(purpose="performance", presentation="table", detail="detailed",
        requested_sections=("summary", "performance_table", "limitations", "recommendations"))
    query = Q(operation="aggregate", metric="revenue", requested_brands=("Calvin Klein",),
              revenue_statuses=("Complete",))
    script["Completed report"] = I(status="ready", request=R(mode="report", report=spec,
        analysis=window(), action=T(kind="reference", reference="analysis", unit="month", offset=1), queries=(query,)))
    assistant = using(flow)
    assistant.today = date(2026, 11, 30)
    first = assistant.run(OWNER, "Completed report", conversation_id="completed", turn_id="initial")
    assert first.status == "ok" and first.report_completeness == "complete"
    october = {"start": "2026-10-01", "end": "2026-11-01"}
    november = {"start": "2026-11-01", "end": "2026-12-01"}
    assert first.resolved_periods["analysis"] == october and first.resolved_periods["action"] == november
    assert ReviewTurn.queries[0].parameter("start_ts").value.date() == date(2026, 10, 1)
    prior = store.load_continuity(OWNER, "completed")
    assert prior.pending is None and prior.snapshot is not None
    original = store.report(OWNER, first.report_id)
    store.close()
    current = SQLiteStore(path, clock=lambda: life + timedelta(minutes=15))
    try:
        script["Correct metric"] = I(status="ready", context_action="correct", request=R(
            analysis=window(), queries=(Q.model_validate({**query.model_dump(), "metric": "order_count"}),)))
        ReviewTurn.rows = (({"order_count": 4},),)
        assistant = using(flow, current)
        assistant.today = date(2026, 12, 1)
        result = assistant.run(OWNER, "Correct metric", conversation_id="completed", turn_id="correction")
        assert result.status == "ok", result.text
        assert result.resolved_periods["analysis"] == october, result.resolved_periods
        assert result.resolved_periods["action"] == november
        assert result.report_composition["spec"] == spec.model_dump(mode="json")
        actual = ReviewTurn.queries[-1]
        assert actual.parameter("start_ts").value.date() == date(2026, 10, 1)
        assert actual.parameter("end_ts").value.date() == date(2026, 11, 1)
        assert actual.metric_definition["metric"] == "order_count"
        snapshot = current.load_continuity(OWNER, "completed").snapshot
        assert snapshot.request.spec.analysis == window()
        assert snapshot.plans[0].requested_brands == query.requested_brands
        assert snapshot.plans[0].revenue_statuses == query.revenue_statuses
        assert result.model_requests == 2 and result.query_executions == 3 and len(ReviewTurn.queries) == 2
        assert current.report(OWNER, first.report_id) == original
    finally:
        current.close()


def test_duplicate_pending_baseline_completion_after_restart(harness):
    script, _, store, flow, path = harness
    query = Q(operation="aggregate", metric="revenue")
    spec = ReportSpec(purpose="performance", presentation="table", detail="detailed",
                      requested_sections=("comparison", "recommendations"))
    action = T(kind="reference", reference="analysis", unit="quarter", offset=1)
    script["Missing choice"] = I(status="ready", request=R(mode="report", report=spec, analysis=quarter(3),
        action=action, queries=(query, query)))
    first = using(flow).run(OWNER, "Missing choice", conversation_id="complete-duplicate")
    assert first.status == "needs_clarification"
    prior = store.load_continuity(OWNER, "complete-duplicate")
    store.close()
    current = SQLiteStore(path, clock=lambda: prior.created_at + timedelta(minutes=10))
    try:
        script["Supply baseline"] = I(status="ready", context_action="continue", request=R(mode="report",
            analysis=quarter(3), comparison=quarter(2), queries=(query,
                query.model_copy(update={"temporal_role": "comparison"}))))
        # Genuinely distinct periods can have equal outcomes: equality is valid.
        ReviewTurn.rows = (({"revenue": Decimal("87.125")},),) * 2
        result = using(flow, current).run(OWNER, "Supply baseline", conversation_id="complete-duplicate")
        assert result.status == "ok", result.text
        assert result.report_completeness == "complete" and result.report_composition["spec"] == spec.model_dump(mode="json")
        assert result.resolved_periods["action"] == {"start": "2025-10-01", "end": "2026-01-01"}
        assert [Decimal(f["value"]) for f in result.factual_findings] == [Decimal(0), Decimal(0)]
        assert result.model_requests == 2 and result.query_executions == 5 and len(ReviewTurn.queries) == 2
        assert current.load_continuity(OWNER, "complete-duplicate").expires_at == prior.expires_at
    finally:
        current.close()


def test_omitted_and_explicit_full_scope_are_one_scalar_choice(harness):
    script, _, store, flow, _ = harness
    script["Equivalent scopes"] = I(status="ready", request=R(mode="report", analysis=quarter(3),
        report=ReportSpec(purpose="comparison", requested_sections=("summary", "limitations")),
        queries=(Q(operation="aggregate", metric="revenue"), Q(operation="aggregate", metric="revenue",
            requested_brands=("Carhartt", "Calvin Klein")))))
    result = using(flow).run(OWNER, "Equivalent scopes", conversation_id="full-scope")
    assert result.status == "needs_clarification" and ReviewTurn.queries == [] and result.report_id is None
    assert result.model_requests == result.query_executions == 1
    assert store.load_continuity(OWNER, "full-scope").pending.report.purpose == "comparison"


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("change", ["metric", "status", "product"])
@pytest.mark.parametrize("expression,started,reply,expected", [
    (window(), date(2026, 11, 30), date(2026, 12, 1), {"start": "2026-10-01", "end": "2026-11-01"}),
    (T(kind="year", year=Y(kind="relative", offset=-1)), date(2026, 12, 31), date(2027, 1, 1), {"start": "2025-01-01", "end": "2026-01-01"}),
    (T(kind="to_date", unit="month"), date(2024, 2, 29), date(2030, 1, 1), {"start": "2024-02-01", "end": "2024-03-01"}),
    (T(kind="year", year=Y(kind="absolute", value=2023)), date(2026, 12, 31), date(2027, 1, 1), {"start": "2023-01-01", "end": "2024-01-01"}),
])
def test_completed_non_temporal_corrections_keep_original_inputs(harness, restart, change, expression, started, reply, expected):
    script, _, store, flow, path = harness
    life = datetime(2026, 12, 31, 23, 50, tzinfo=timezone.utc)
    clock = [life]
    store._now = lambda: clock[0]
    query = Q(operation="aggregate", metric="revenue", requested_brands=("Calvin Klein",), revenue_statuses=("Complete",))
    action = T(kind="reference", reference="analysis", unit="month", offset=1)
    script["First complete"] = I(status="ready", request=R(mode="report", analysis=expression, action=action, queries=(query,)))
    assistant = using(flow)
    assistant.today = started
    first = assistant.run(OWNER, "First complete", conversation_id="calendar-matrix", turn_id="first")
    assert first.status == "ok" and first.resolved_periods["analysis"] == expected
    original = store.report(OWNER, first.report_id)
    clock[0] += timedelta(minutes=15)
    current = store
    if restart:
        store.close()
        current = SQLiteStore(path, clock=lambda: clock[0])
    try:
        changes = {"metric": "order_count"} if change == "metric" else {"revenue_statuses": ("Returned",)} if change == "status" else {"product_ids": (1,)}
        changed_query = Q.model_validate({**query.model_dump(), **changes})
        script["Non temporal correction"] = I(status="ready", context_action="correct", request=R(analysis=expression, queries=(changed_query,)))
        if change == "metric": ReviewTurn.rows = (({"order_count": 4},),)
        assistant = using(flow, current)
        assistant.today = reply
        result = assistant.run(OWNER, "Non temporal correction", conversation_id="calendar-matrix", turn_id="corrected")
        assert result.status == "ok", result.text
        assert result.resolved_periods == first.resolved_periods
        query = ReviewTurn.queries[-1]
        assert query.parameter("start_ts").value.date().isoformat() == expected["start"]
        assert query.parameter("end_ts").value.date().isoformat() == expected["end"]
        assert query.parameter("statuses").value == tuple(s.value for s in changed_query.revenue_statuses)
        assert query.effective_brands == ("Calvin Klein",) and query.requested_product_ids == changed_query.product_ids
        snapshot = current.load_continuity(OWNER, "calendar-matrix").snapshot
        assert snapshot.request.spec.analysis == expression and snapshot.request.spec.action == action
        assert snapshot.resolution_role_anchors["analysis"].reference_date == started
        assert result.model_requests == 2 and result.query_executions == 3 and len(ReviewTurn.queries) == 2
        # A second completed correction exercises captured per-role overrides,
        # rather than only legacy-compatible original global inputs.
        script["Second correction"] = I(status="ready", context_action="correct", request=R(analysis=expression, queries=(changed_query,)))
        assistant.today = date(2035, 6, 1)
        again = assistant.run(OWNER, "Second correction", conversation_id="calendar-matrix", turn_id="corrected-again")
        assert again.resolved_periods == first.resolved_periods and again.status == "ok"
        assert current.report(OWNER, first.report_id) == original
        # Saved replay is still exact after continuity disappears and reply dates change.
        current.clear_continuity(OWNER, "calendar-matrix")
        replay = assistant.run(OWNER, "Non temporal correction", conversation_id="calendar-matrix", turn_id="corrected")
        assert replay.reused_report and replay.text == result.text and replay.resolved_periods == result.resolved_periods
        assert replay.factual_findings == result.factual_findings and replay.evidence_job_ids == result.evidence_job_ids
        assert replay.model_requests == 0 and replay.query_executions == 1 and len(ReviewTurn.queries) == 3
    finally:
        if restart: current.close()


@pytest.mark.parametrize("control,expected_analysis,expected_action", [
    ("new", {"start": "2026-01-01", "end": "2027-01-01"}, None),
    ("refresh", {"start": "2026-01-01", "end": "2027-01-01"}, {"start": "2027-01-01", "end": "2028-01-01"}),
    ("changed_relative", {"start": "2024-01-01", "end": "2025-01-01"}, {"start": "2025-01-01", "end": "2026-01-01"}),
    ("absolute", {"start": "2022-01-01", "end": "2023-01-01"}, {"start": "2023-01-01", "end": "2024-01-01"}),
    ("new_action", {"start": "2025-01-01", "end": "2026-01-01"}, {"start": "2027-01-01", "end": "2028-01-01"}),
    ("clear_action", {"start": "2025-01-01", "end": "2026-01-01"}, None),
])
def test_completed_correction_new_refresh_and_changed_roles(harness, control, expected_analysis, expected_action):
    script, _, store, flow, path = harness
    life = datetime(2026, 12, 31, 23, 50, tzinfo=timezone.utc)
    store._now = lambda: life
    expression = T(kind="year", year=Y(kind="relative", offset=-1))
    query = Q(operation="aggregate", metric="revenue", revenue_statuses=("Complete",))
    action = T(kind="reference", reference="analysis", unit="year", offset=1)
    script["Original relative report"] = I(status="ready", request=R(mode="report", analysis=expression,
        action=action, queries=(query,), report=ReportSpec(presentation="table", detail="detailed")))
    assistant = using(flow)
    assistant.today = date(2026, 12, 31)
    first = assistant.run(OWNER, "Original relative report", conversation_id="controls")
    assert first.resolved_periods["analysis"] == {"start": "2025-01-01", "end": "2026-01-01"}
    store.close()
    current = SQLiteStore(path, clock=lambda: life + timedelta(minutes=15))
    try:
        analysis = (T(kind="year", year=Y(kind="relative", offset=-3)) if control == "changed_relative" else
                    T(kind="year", year=Y(kind="absolute", value=2022)) if control == "absolute" else expression)
        script["Temporal control"] = I(status="ready", context_action="new" if control == "new" else "correct",
            temporal_updates=("analysis",) if control == "refresh" else (),
            clear_fields=("action",) if control == "clear_action" else (),
            request=R(analysis=analysis, queries=(query,), action=T(kind="year", year=Y(kind="relative", offset=0)) if control == "new_action" else None))
        assistant = using(flow, current)
        assistant.today = date(2027, 1, 1)
        result = assistant.run(OWNER, "Temporal control", conversation_id="controls")
        assert result.status == "ok", result.text
        assert result.resolved_periods["analysis"] == expected_analysis
        assert result.resolved_periods["action"] == expected_action
        assert bool(result.report_id) == (control != "new")
        assert (result.report_composition is None) == (control == "new")
        assert ReviewTurn.queries[-1].parameter("start_ts").value.date().isoformat() == expected_analysis["start"]
        assert result.model_requests == 2 and result.query_executions == 3
        snapshot = current.load_continuity(OWNER, "controls").snapshot
        if control == "new_action":
            assert snapshot.resolution_role_anchors["analysis"].reference_date == date(2026, 12, 31)
            assert snapshot.resolution_role_anchors["action"].reference_date == date(2027, 1, 1)
    finally:
        current.close()


def test_completed_correction_preserves_original_previous_role_input(harness):
    script, _, store, flow, _ = harness
    life = datetime(2026, 12, 31, 23, 50, tzinfo=timezone.utc)
    store._now = lambda: life
    query = Q(operation="aggregate", metric="revenue")
    script["Prior context"] = I(status="ready", request=R(analysis=T(kind="year", year=Y(kind="absolute", value=2025)), queries=(query,)))
    using(flow).run(OWNER, "Prior context", conversation_id="prior-role")
    expression = T(kind="reference", reference="previous", unit="year", offset=1)
    script["Relative to previous"] = I(status="ready", request=R(mode="report", analysis=expression,
        action=T(kind="reference", reference="analysis", unit="year", offset=1), queries=(query,)))
    first = using(flow).run(OWNER, "Relative to previous", conversation_id="prior-role")
    assert first.resolved_periods["analysis"] == {"start": "2026-01-01", "end": "2027-01-01"}
    old = store.load_continuity(OWNER, "prior-role").snapshot
    store._now = lambda: life + timedelta(minutes=15)
    script["Metric only"] = I(status="ready", context_action="correct", request=R(analysis=expression,
        queries=(Q(operation="aggregate", metric="order_count"),)))
    ReviewTurn.rows = (({"order_count": 4},),)
    assistant = using(flow)
    assistant.today = date(2030, 1, 1)
    result = assistant.run(OWNER, "Metric only", conversation_id="prior-role")
    assert result.status == "ok" and result.resolved_periods == first.resolved_periods
    snapshot = store.load_continuity(OWNER, "prior-role").snapshot
    assert snapshot.resolution_role_anchors["analysis"].previous_period == old.resolution_role_anchors["analysis"].previous_period
    assert snapshot.resolution_role_anchors["analysis"].previous_period.start == date(2025, 1, 1)
    assert ReviewTurn.queries[-1].parameter("start_ts").value.date() == date(2026, 1, 1)


def test_completed_correction_new_baseline_uses_reply_clock(harness):
    script, _, store, flow, _ = harness
    life = datetime(2026, 12, 31, 23, 50, tzinfo=timezone.utc)
    store._now = lambda: life
    expression = T(kind="year", year=Y(kind="relative", offset=-1))
    query = Q(operation="aggregate", metric="revenue")
    script["Report before midnight"] = I(status="ready", request=R(mode="report", analysis=expression,
        action=T(kind="reference", reference="analysis", unit="year", offset=1), queries=(query,)))
    assistant = using(flow)
    assistant.today = date(2026, 12, 31)
    assistant.run(OWNER, "Report before midnight", conversation_id="new-baseline")
    store._now = lambda: life + timedelta(minutes=15)
    script["Add relative baseline"] = I(status="ready", context_action="correct", request=R(mode="report",
        analysis=expression, comparison=T(kind="year", year=Y(kind="relative", offset=-3)),
        queries=(query, query.model_copy(update={"temporal_role": "comparison"}))))
    assistant = using(flow)
    assistant.today = date(2027, 1, 1)
    result = assistant.run(OWNER, "Add relative baseline", conversation_id="new-baseline")
    assert result.status == "ok", result.text
    assert result.resolved_periods == {"analysis": {"start": "2025-01-01", "end": "2026-01-01"},
        "comparison": {"start": "2024-01-01", "end": "2025-01-01"}, "action": {"start": "2026-01-01", "end": "2027-01-01"}}
    snapshot = store.load_continuity(OWNER, "new-baseline").snapshot
    assert snapshot.resolution_role_anchors["analysis"].reference_date == date(2026, 12, 31)
    assert snapshot.resolution_role_anchors["comparison"].reference_date == date(2027, 1, 1)
    assert [q.parameter("start_ts").value.date() for q in ReviewTurn.queries[-2:]] == [date(2025, 1, 1), date(2024, 1, 1)]
    assert result.model_requests == 2 and result.query_executions == 5


@pytest.mark.parametrize("reseal", [False, True])
@pytest.mark.parametrize("continuity", ["absent", "expired"])
@pytest.mark.parametrize("mutation", ["2020_order_count", "partial_to_complete"])
def test_original_w2_r4_year_and_completeness_mutations(harness, mutation, continuity, reseal):
    from opsfleet_assistant.report_artifact import artifact_binding_digest
    from opsfleet_assistant.reporting import ReportComposition
    script, selections, store, flow, path = harness
    if mutation == "partial_to_complete": selections[0] = ReportDraft()
    script["Year report"] = I(status="ready", request=R(mode="report", analysis=T(kind="year", year=Y(kind="absolute", value=2025)),
        queries=(Q(operation="aggregate", metric="revenue"),)))
    first = using(flow).run(OWNER, "Year report", conversation_id="original-w2", turn_id="original")
    assert first.status == ("partial" if mutation == "partial_to_complete" else "ok")
    original = store.report(OWNER, first.report_id)
    comp = json.loads(original["report_composition_json"])
    if mutation == "2020_order_count":
        comp["request"]["analysis"] = {"start": "2020-01-01", "end": "2021-01-01"}
        comp["request"]["spec"]["analysis"]["year"]["value"] = 2020
        comp["request"]["spec"]["queries"][0]["metric"] = "order_count"
    else:
        comp["completeness"], comp["missing"] = "complete", []
        next(s for s in comp["sections"] if s["kind"] == "recommendations").update(
            status="supported", finding_refs=[0], evidence_refs=[0])
        store.db.execute("UPDATE reports SET report_completeness='complete' WHERE id=?", (first.report_id,))
    # Both are valid shapes. Semantic consistency, not schema/digest alone, is required.
    ReportComposition.model_validate(comp)
    store.db.execute("UPDATE reports SET report_composition_json=? WHERE id=?", (json.dumps(comp), first.report_id))
    if reseal:
        store.db.execute("UPDATE reports SET report_binding_digest=? WHERE id=?",
            (artifact_binding_digest(store.report(OWNER, first.report_id)), first.report_id))
    if continuity == "absent": store.clear_continuity(OWNER, "original-w2")
    store.close()
    current = SQLiteStore(path, clock=lambda: datetime(2026, 11, 4, 13, tzinfo=timezone.utc))
    try:
        before_retry = current.report(OWNER, first.report_id)
        replay = using(flow, current).run(OWNER, "Year report", conversation_id="original-w2", turn_id="original")
        assert replay.status == "error" and not replay.reused_report and replay.report_id is None
        assert replay.report_composition is replay.resolved_periods is None and replay.factual_findings == replay.suggestions == ()
        assert replay.model_requests == 0 and replay.query_executions == 1 and len(ReviewTurn.queries) == 1
        after = current.report(OWNER, first.report_id)
        assert after == before_retry  # No silent repair, regeneration, ID or version change.
        for field in ("id", "version", "content", "plans_json", "evidence_refs_json", "grounded_report_json", "report_support_json"):
            assert after[field] == original[field]
    finally:
        current.close()


def test_completed_correction_expires_without_freezing_later_request(harness):
    script, _, store, flow, _ = harness
    life = datetime(2026, 11, 30, 23, 50, tzinfo=timezone.utc)
    store._now = lambda: life
    query = Q(operation="aggregate", metric="revenue")
    script["Expiring report"] = I(status="ready", request=R(mode="report", analysis=window(), queries=(query,)))
    assistant = using(flow)
    assistant.today = date(2026, 11, 30)
    first = assistant.run(OWNER, "Expiring report", conversation_id="expiry", turn_id="original")
    original = store.report(OWNER, first.report_id)
    store._now = lambda: life + timedelta(minutes=30)
    script["Complete request after expiry"] = I(status="ready", context_action="correct", request=R(
        analysis=window(), queries=(Q(operation="aggregate", metric="order_count"),)))
    ReviewTurn.rows = (({"order_count": 4},),)
    assistant = using(flow)
    assistant.today = date(2026, 12, 1)
    result = assistant.run(OWNER, "Complete request after expiry", conversation_id="expiry")
    assert result.status == "ok" and result.report_id is None and result.report_composition is None
    assert result.resolved_periods["analysis"] == {"start": "2026-11-01", "end": "2026-12-01"}
    assert store.report(OWNER, first.report_id) == original


@pytest.mark.parametrize("failure", ["scope", "catalog", "policy"])
def test_durable_replay_still_requires_current_authorization(harness, monkeypatch, failure):
    from opsfleet_assistant.bigquery_adapter import ExecutionError
    import opsfleet_assistant.workflow as module
    script, _, store, flow, _ = harness
    script["Authorized report"] = I(status="ready", request=R(mode="report", analysis=quarter(3),
        queries=(Q(operation="aggregate", metric="revenue", requested_brands=("Calvin Klein",)),)))
    first = using(flow).run(OWNER, "Authorized report", conversation_id="authorization", turn_id="same")
    original = store.report(OWNER, first.report_id)
    store.clear_continuity(OWNER, "authorization")
    if failure == "scope":
        resolve_scope = module.resolve_demo_scope
        monkeypatch.setattr(module, "resolve_demo_scope", lambda *a: resolve_scope(*a).model_copy(update={"brands": ("Carhartt",)}))
    elif failure == "catalog":
        def unavailable(self):
            self.executions += 1
            raise ExecutionError("query_provider_rejected")
        monkeypatch.setattr(ReviewTurn, "load_catalog", unavailable)
    else:
        monkeypatch.setattr(module, "FIELD_POLICY_VERSION", 999)
    replay = using(flow).run(OWNER, "Authorized report", conversation_id="authorization", turn_id="same")
    assert replay.status == "error" and replay.factual_findings == () and replay.report_composition is None
    assert replay.report_id is None and replay.model_requests == 0 and replay.query_executions == 1
    assert len(ReviewTurn.queries) == 1 and store.report(OWNER, first.report_id) == original
    assert store.report("manager_other", first.report_id) is None


def test_additive_durable_migration_preserves_future_version_and_active_proposal(harness):
    _, _, store, _, path = harness
    rid = store.save_report(owner=OWNER, conversation_id="migration", turn_id="old", content="Original synthetic legacy artifact",
        metric_definitions=[], scope_reference="demo:" + OWNER, scope_version=1, evidence_refs=[])
    store.db.execute("UPDATE reports SET version=7 WHERE id=?", (rid,))
    preview = store.preview_deletion(OWNER, "conversation", "migration")
    for column in ("report_completeness", "report_composition_json", "report_support_json", "report_binding_digest"):
        store.db.execute(f"ALTER TABLE reports DROP COLUMN {column}")
    store.db.execute("PRAGMA user_version=9")
    store.close()
    current = SQLiteStore(path, clock=lambda: datetime(2026, 11, 4, 12, tzinfo=timezone.utc))
    try:
        row = current.report(OWNER, rid)
        assert row["version"] == 7 and row["content"] == "Original synthetic legacy artifact"
        assert row["report_completeness"] == "legacy" and row["report_support_json"] == row["report_binding_digest"] == ""
        assert current.db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert current.resolve_deletion(OWNER, preview["token"]) == ("deleted", [rid])
        assert current.resolve_deletion(OWNER, preview["token"]) == ("invalid", [])
    finally:
        current.close()


@pytest.mark.parametrize("purpose", ["executive", "performance", "comparison"])
@pytest.mark.parametrize("variant", ["limit", "reordered", "full_brands"])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("baseline", ["99.375", "79.5"])
def test_equivalent_pending_choices_complete_with_baseline(harness, purpose, variant, restart, baseline):
    script, _, store, flow, path = harness
    a = Q(operation="aggregate", metric="revenue")
    b = a
    if variant == "limit":
        b = Q.model_validate({**a.model_dump(), "limit": 1})
    elif variant == "reordered":
        a = Q(operation="aggregate", metric="revenue", requested_brands=("Calvin Klein", "Carhartt"),
              product_ids=(1, 2), revenue_statuses=("Complete", "Returned"))
        b = Q(operation="aggregate", metric="revenue", requested_brands=("carhartt", "calvin klein"),
              product_ids=(2, 1), revenue_statuses=("Returned", "Complete"))
    else:
        b = Q(operation="aggregate", metric="revenue", requested_brands=("Carhartt", "Calvin Klein"))
    spec = ReportSpec(purpose=purpose, presentation="table", detail="detailed",
        requested_sections=("summary", "comparison", "limitations", "recommendations"))
    action = T(kind="reference", reference="analysis", unit="quarter", offset=1)
    script["Equivalent unresolved pair"] = I(status="ready", request=R(mode="report", report=spec,
        analysis=quarter(3), action=action, queries=(a, b)))
    first = using(flow).run(OWNER, "Equivalent unresolved pair", conversation_id="equivalent")
    assert first.status == "needs_clarification" and first.report_id is None and ReviewTurn.queries == []
    assert first.model_requests == first.query_executions == 1 and store.list_reports(OWNER) == []
    prior = store.load_continuity(OWNER, "equivalent")
    assert prior.pending.queries == (a, b)
    reply_time = prior.created_at + timedelta(minutes=5)
    current = store
    if restart:
        store.close()
        current = SQLiteStore(path, clock=lambda: reply_time)
    else:
        current._now = lambda: reply_time
    try:
        paired = Q.model_validate({**b.model_dump(), "temporal_role": "comparison"})
        script["Q2 baseline"] = I(status="ready", context_action="continue", request=R(mode="report",
            analysis=quarter(3), comparison=quarter(2), queries=(a, paired)))
        ReviewTurn.rows = (({"revenue": Decimal("99.375")},), ({"revenue": Decimal(baseline)},))
        result = using(flow, current).run(OWNER, "Q2 baseline", conversation_id="equivalent", turn_id="completed")
        assert result.status == "ok", result.text
        assert result.report_completeness == "complete" and result.report_composition["spec"] == spec.model_dump(mode="json")
        assert result.resolved_periods == {"analysis": {"start": "2025-07-01", "end": "2025-10-01"},
            "comparison": {"start": "2025-04-01", "end": "2025-07-01"}, "action": {"start": "2025-10-01", "end": "2026-01-01"}}
        expected = (Decimal(0), Decimal(0)) if baseline == "99.375" else (Decimal("19.875"), Decimal("25"))
        assert tuple(Decimal(f["value"]) for f in result.factual_findings) == expected
        assert result.suggestions and result.model_requests == 2 and result.query_executions == 5
        assert len(ReviewTurn.queries) == 2
        for actual, query, start, end in zip(ReviewTurn.queries, (a, b),
                (date(2025, 7, 1), date(2025, 4, 1)), (date(2025, 10, 1), date(2025, 7, 1))):
            assert actual.parameter("start_ts").value.date() == start and actual.parameter("end_ts").value.date() == end
            assert actual.parameter("limit").value == query.limit
            assert actual.parameter("statuses").value == tuple(s.value for s in query.revenue_statuses)
            assert actual.effective_brands == ("Calvin Klein", "Carhartt")
            assert actual.requested_product_ids == query.product_ids
        context = current.load_continuity(OWNER, "equivalent")
        assert context.expires_at == prior.expires_at and context.snapshot.request.spec.queries == (a, paired)
        for plan, query in zip(context.snapshot.plans, (a, paired)):
            assert plan.model_dump(exclude={"period", "cutoff_date", "version"}) == query.model_dump(exclude={"temporal_role"})
        # Saved replay and re-grounded reuse must support the equivalent pair too.
        script["Explain equivalent pair"] = I(status="ready", context_action="continue", request=R(intent="explain"))
        explained = using(flow, current).run(OWNER, "Explain equivalent pair", conversation_id="equivalent")
        assert explained.status == "ok" and explained.factual_findings == result.factual_findings
        assert explained.report_composition == result.report_composition and explained.model_requests == explained.query_executions == 1
        original = current.report(OWNER, result.report_id)
        current.clear_continuity(OWNER, "equivalent")
        replay = using(flow, current).run(OWNER, "Q2 baseline", conversation_id="equivalent", turn_id="completed")
        assert replay.reused_report and replay.text == result.text and replay.factual_findings == result.factual_findings
        assert replay.report_composition == result.report_composition and replay.model_requests == 0 and replay.query_executions == 1
        assert current.report(OWNER, result.report_id) == original and len(ReviewTurn.queries) == 2
    finally:
        if restart: current.close()


@pytest.mark.parametrize("change", ["metric", "scope", "products", "statuses", "limit"])
def test_baseline_only_continue_cannot_change_retained_material_choices(harness, change):
    script, _, store, flow, _ = harness
    a = Q(operation="aggregate", metric="revenue")
    b = Q(operation="aggregate", metric="revenue", limit=1)
    script["Await baseline"] = I(status="ready", request=R(mode="report", analysis=quarter(3),
        report=ReportSpec(purpose="comparison"), queries=(a, b)))
    assert using(flow).run(OWNER, "Await baseline", conversation_id="material").status == "needs_clarification"
    prior = store.load_continuity(OWNER, "material")
    changes = {"metric": {"metric": "order_count"}, "scope": {"requested_brands": ("Carhartt",)},
        "products": {"product_ids": (1,)}, "statuses": {"revenue_statuses": ("Complete",)}, "limit": {"limit": 2}}[change]
    changed = Q.model_validate({**b.model_dump(), **changes, "temporal_role": "comparison"})
    script["Baseline with changed choice"] = I(status="ready", context_action="continue", request=R(mode="report",
        analysis=quarter(3), comparison=quarter(2), queries=(a, changed)))
    result = using(flow).run(OWNER, "Baseline with changed choice", conversation_id="material")
    assert result.status == "error" and "plan_intent_mismatch" in result.text
    assert result.model_requests == 3 and result.query_executions == 1 and ReviewTurn.queries == []
    assert result.report_id is None and result.factual_findings == () and store.list_reports(OWNER) == []
    assert store.load_continuity(OWNER, "material") == prior


@pytest.mark.parametrize("change", ["metric", "scope", "products", "statuses", "grouped_limit", "overlap", "later_baseline"])
def test_ready_temporal_pair_keeps_population_and_period_guards(harness, change):
    script, _, store, flow, _ = harness
    a = Q(operation="aggregate", metric="revenue")
    b = Q(operation="aggregate", metric="revenue", temporal_role="comparison")
    updates = {"metric": {"metric": "order_count"}, "scope": {"requested_brands": ("Calvin Klein",)},
        "products": {"product_ids": (1,)}, "statuses": {"revenue_statuses": ("Complete",)}}
    if change in updates: b = Q.model_validate({**b.model_dump(), **updates[change]})
    if change == "grouped_limit":
        a = Q.model_validate({**a.model_dump(), "dimension": "product"})
        b = Q.model_validate({**b.model_dump(), "dimension": "product", "limit": 1})
    script["Material incompatible pair"] = I(status="ready", request=R(mode="report", analysis=quarter(3),
        comparison=quarter(3 if change == "overlap" else 4 if change == "later_baseline" else 2),
        report=ReportSpec(purpose="comparison"), queries=(a, b)))
    result = using(flow).run(OWNER, "Material incompatible pair", conversation_id="incompatible")
    assert result.status == "error" and result.factual_findings == () and result.report_id is None
    assert result.model_requests == 3 and result.query_executions == 1 and ReviewTurn.queries == []
    assert store.load_continuity(OWNER, "incompatible") is None and store.list_reports(OWNER) == []


def test_explicit_scalar_limit_correction_remains_exactly_bound(harness):
    script, _, store, flow, _ = harness
    a = Q(operation="aggregate", metric="revenue")
    script["Unspecified baseline"] = I(status="ready", request=R(mode="report", analysis=quarter(3),
        report=ReportSpec(purpose="comparison"), queries=(a, Q.model_validate({**a.model_dump(), "limit": 1}))))
    using(flow).run(OWNER, "Unspecified baseline", conversation_id="explicit-limit")
    script["Explicit limit and baseline correction"] = I(status="ready", context_action="correct", request=R(mode="report",
        analysis=quarter(3), comparison=quarter(2), queries=(a, Q.model_validate({**a.model_dump(), "limit": 2, "temporal_role": "comparison"}))))
    result = using(flow).run(OWNER, "Explicit limit and baseline correction", conversation_id="explicit-limit")
    assert result.status == "ok" and result.model_requests == 2 and result.query_executions == 5
    snapshot = store.load_continuity(OWNER, "explicit-limit").snapshot
    assert [p.limit for p in snapshot.plans] == [20, 2]
    assert [q.parameter("limit").value for q in ReviewTurn.queries] == [20, 2]


@pytest.mark.parametrize("index,replacement", [(0, 1), (1, 20)])
def test_compatible_scalar_pair_cannot_weaken_per_query_limit_binding(index, replacement):
    from opsfleet_assistant.intent import analytical_plans, validate_bound_plans
    from opsfleet_assistant.periods import resolve_request
    from opsfleet_assistant.identity import PolicyError
    spec = R(mode="report", analysis=quarter(3), comparison=quarter(2), queries=(
        Q(operation="aggregate", metric="revenue", limit=20),
        Q(operation="aggregate", metric="revenue", requested_brands=("Carhartt", "Calvin Klein"), limit=1, temporal_role="comparison")))
    resolved = resolve_request(spec, reference_date=date(2026, 11, 4), analytical_timezone="UTC")
    brands = (("Calvin Klein", "Carhartt"),) * 2
    plans = analytical_plans(resolved, effective_query_brands=brands)
    validate_bound_plans(plans, resolved, effective_query_brands=brands)
    changed = tuple(p.model_copy(update={"limit": replacement}) if i == index else p for i, p in enumerate(plans))
    with pytest.raises(PolicyError, match="plan_limit_mismatch"):
        validate_bound_plans(changed, resolved, effective_query_brands=brands)
