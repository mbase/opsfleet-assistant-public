"""Independent numerical oracles with released fixture evidence, never live semantics."""
from datetime import date, datetime, timezone
from decimal import Decimal
import json

from google.cloud import bigquery
import pytest
from pydantic import ValidationError

from opsfleet_assistant.compiler import compile_plan
from opsfleet_assistant.contracts import (ActionSelection, AnalysisPlan, FindingSelection,
    ObservationRef, Period, ReportDraft)
from opsfleet_assistant.evidence import Coverage, release_evidence
from opsfleet_assistant.identity import Catalog, PolicyError, resolve_demo_scope
from opsfleet_assistant.reporting import calculate, ground_report


# Deliberately independent inputs and expected arithmetic: 100 -> 200 means
# +100 units and +100%; 200 -> 100 means -100 units and -50%.
def fixture_evidence(*, operation="aggregate", metric="revenue", rows=None,
                     start="2025-04-01", end="2025-07-01", cutoff=None,
                     dimension=None, limit=20, statuses=None, products=None):
    catalog = Catalog.from_products(({"id": 1, "brand": "Calvin Klein"},
                                     {"id": 2, "brand": "Carhartt"}))
    scope = resolve_demo_scope("manager_apparel", catalog)
    fields = dict(operation=operation, metric=metric, limit=limit,
                  product_ids=products)
    if operation == "churn_snapshot":
        fields["cutoff_date"] = date.fromisoformat(cutoff or end)
    else:
        fields["period"] = Period(start=date.fromisoformat(start), end=date.fromisoformat(end))
    if operation == "product_comparison":
        fields["product_ids"] = (1, 2)
    if operation == "demographic_comparison":
        fields["dimension"] = "state"
        fields["demographic_values"] = ("Texas", "California")
    elif dimension:
        fields["dimension"] = dimension
    if statuses:
        fields["revenue_statuses"] = statuses
    query = compile_plan(AnalysisPlan(**fields), scope, catalog)
    rows = rows if rows is not None else ({metric: Decimal("200")},)
    schema = tuple(bigquery.SchemaField(k, "INT64" if k in {
        "product_id", "user_id", "item_count", "order_count", "customers", "eligible_customers", "inactive_customers", "customer_count"}
        else "DATE" if k == "month" else "STRING" if k in {"brand", "demographic_value"} else "NUMERIC") for k in rows[0])
    return release_evidence(query, rows, schema, job_id="fixture_analysis", estimated_bytes=0, billed_bytes=0,
        query_as_of=datetime(2026, 11, 4, tzinfo=timezone.utc),
        coverage=Coverage(status="checked", source_rows=None if operation == "demographic_comparison" else 20,
                          period_rows=None if operation == "demographic_comparison" else 10,
                          note="Independent synthetic fixture.", job_id="fixture_coverage"))


def ref(e=0, row=0, metric="revenue"):
    return ObservationRef(evidence_index=e, row_index=row, metric=metric)


def change(operation="percentage_change", metric="revenue"):
    return FindingSelection(operation=operation, current=ref(metric=metric), baseline=ref(1, metric=metric))


@pytest.mark.parametrize("prior,current,delta,percent", [
    ("100", "200", "100", "100"), ("200", "100", "-100", "-50"),
    ("100", "100", "0", "0"), ("0", "200", "200", None),
    ("0.12345678901234567890123456789", "0.22345678901234567890123456789", "0.1", None),
])
def test_independent_change_oracles_and_precision(prior, current, delta, percent):
    evidence = (fixture_evidence(rows=({"revenue": Decimal(current)},)),
                fixture_evidence(start="2025-01-01", end="2025-04-01", rows=({"revenue": Decimal(prior)},)))
    finding = calculate(change(), evidence)
    assert finding.absolute_change == Decimal(delta)
    assert finding.current_value == Decimal(current)
    assert finding.baseline_value == Decimal(prior)
    if prior == "0":
        assert finding.value is None and "Percentage change unavailable" in finding.text
        assert "is 200.00" in finding.text
    elif percent is not None:
        assert finding.value == Decimal(percent)
        assert f"Percentage change: {Decimal(percent):.2f}%." in finding.text
    absolute = calculate(change("difference"), evidence)
    assert absolute.value == Decimal(delta)


def test_null_baseline_and_missing_reference_never_generate_percentage():
    # Contract-level null input: the warehouse release normally marks an empty
    # undefined average incomplete, so it cannot reach a comparison at all.
    current = fixture_evidence(metric="average_order_value", rows=({"average_order_value": Decimal("20")},))
    baseline = fixture_evidence(metric="average_order_value", start="2025-01-01", end="2025-04-01",
                                rows=({"average_order_value": Decimal("10")},))
    baseline = baseline.model_copy(update={"rows": ({"average_order_value": None},)})
    result = calculate(change(metric="average_order_value"), (current, baseline))
    assert result.value is None and result.absolute_change is None
    assert "Percentage change unavailable" in result.text
    with pytest.raises(PolicyError, match="invalid_finding_reference"):
        calculate(change(), (fixture_evidence(),))


@pytest.mark.parametrize("patch", [
    {"execution_scope": None}, {"scope_version": 99}, {"scope_reference": "demo:another"},
    {"field_policy_version": 99}, {"complete": False},
])
def test_incompatible_or_incomplete_evidence_rejected(patch):
    current = fixture_evidence()
    baseline = fixture_evidence(start="2025-01-01", end="2025-04-01")
    with pytest.raises(PolicyError):
        calculate(change(), (current, baseline.model_copy(update=patch)))


def test_incompatible_definitions_scopes_periods_and_metrics():
    current = fixture_evidence()
    baseline = fixture_evidence(start="2025-01-01", end="2025-04-01", statuses=("Complete",))
    with pytest.raises(PolicyError, match="incompatible_finding_definition"):
        calculate(change(), (current, baseline))
    baseline = fixture_evidence(start="2025-01-01", end="2025-04-01", products=(1,))
    with pytest.raises(PolicyError, match="incompatible_finding_scope"):
        calculate(change(), (current, baseline))
    with pytest.raises(PolicyError, match="invalid_finding_period"):
        calculate(change(), (current, fixture_evidence(start="2025-03-01", end="2025-06-01")))
    with pytest.raises(PolicyError, match="incompatible_finding_metric"):
        calculate(change().model_copy(update={"baseline": ref(1, metric="order_count")}),
                  (current, fixture_evidence(metric="order_count", rows=({"order_count": 5},))))
    with pytest.raises(PolicyError, match="incompatible_finding_metric"):
        calculate(change("percentage_point_change"),
                  (current, fixture_evidence(start="2025-01-01", end="2025-04-01")))


@pytest.mark.parametrize("payload", [
    {"headline": "Revenue doubled across the period"}, {"headline": "Revenue collapsed across the period"},
    {"headline": "The weather is sunny in Paris"}, {"hypotheses": ["Revenue doubled"]},
    {"findings": [{"operation": "observed_value", "current": {"evidence_index": 0, "row_index": 0, "metric": "weather"}}]},
    {"findings": [{"operation": "difference", "current": {"evidence_index": 0, "row_index": 0, "metric": "revenue"}}]},
    {"findings": [{"operation": "observed_value", "current": {"evidence_index": True, "row_index": 0, "metric": "revenue"}}]},
])
def test_free_prose_and_malformed_model_selections_are_rejected(payload):
    with pytest.raises(ValidationError):
        ReportDraft.model_validate(payload)


def test_unknown_rows_metrics_and_headline_references_are_rejected():
    evidence = (fixture_evidence(),)
    for current in (ref(row=1), ref(metric="item_count"), ref(1)):
        with pytest.raises(PolicyError):
            calculate(FindingSelection(operation="observed_value", current=current), evidence)
    with pytest.raises(PolicyError, match="invalid_finding_reference"):
        ground_report(ReportDraft(headline_index=19), evidence)


def test_rank_ties_and_partial_results_never_claim_global_best():
    item = fixture_evidence(dimension="product", rows=(
        {"product_id": 1, "revenue": Decimal("200")}, {"product_id": 2, "revenue": Decimal("200")}))
    selection = FindingSelection(operation="rank", current=ref())
    finding = calculate(selection, (item,))
    assert finding.rank == 1 and finding.tied
    assert "rank 1 (tied)" in finding.text
    partial = item.model_copy(update={"complete": False})
    finding = calculate(selection, (partial,))
    assert "returned groups only" in finding.text and "no global rank asserted" in finding.text
    with pytest.raises(PolicyError, match="incomplete_finding_comparison"):
        calculate(FindingSelection(operation="difference", current=ref(), baseline=ref(row=1)), (partial,))


def test_product_demographic_and_churn_oracles_and_relevant_actions():
    product = fixture_evidence(operation="product_comparison", rows=(
        {"product_id": 1, "revenue": Decimal("200"), "item_count": 20, "order_count": 10, "average_item_price": Decimal("10")},
        {"product_id": 2, "revenue": Decimal("100"), "item_count": 5, "order_count": 4, "average_item_price": Decimal("20")}))
    selection = FindingSelection(operation="difference", current=ref(), baseline=ref(row=1))
    report = ground_report(ReportDraft(findings=(selection,), recommendations=(
        ActionSelection(kind="investigate_price_volume", finding_index=0),), hypotheses=(
        ActionSelection(kind="investigate_price_volume", finding_index=0),)), (product,))
    assert report.findings[0].value == 100
    assert "do not establish a cause" in report.recommendations[0].text
    assert "test this" in report.hypotheses[0].text
    demographic = fixture_evidence(operation="demographic_comparison", metric="average_customer_spend", rows=(
        {"demographic_value": "Texas", "customers": 5, "revenue": Decimal("350"), "average_customer_spend": Decimal("70")},
        {"demographic_value": "California", "customers": 5, "revenue": Decimal("300"), "average_customer_spend": Decimal("60")}))
    delta = FindingSelection(operation="difference", current=ref(metric="average_customer_spend"), baseline=ref(row=1, metric="average_customer_spend"))
    assert calculate(delta, (demographic,)).value == 10
    with pytest.raises(PolicyError, match="incompatible_finding_population"):
        calculate(change(metric="average_customer_spend"), (demographic, demographic))
    churn = fixture_evidence(operation="churn_snapshot", metric="churn_rate", cutoff="2025-07-01", rows=(
        {"eligible_customers": 20, "inactive_customers": 4, "churn_rate": Decimal("0.2")},))
    prior = fixture_evidence(operation="churn_snapshot", metric="churn_rate", cutoff="2025-04-01", rows=(
        {"eligible_customers": 10, "inactive_customers": 1, "churn_rate": Decimal("0.1")},))
    finding = calculate(change("percentage_point_change", "churn_rate"), (churn, prior))
    assert finding.value == 10 and finding.absolute_change == Decimal("0.1")
    assert "rose by 10.00 percentage points" in finding.text
    assert "4 inactive / 20 prior purchasers" in finding.text and "1 inactive / 10 prior purchasers" in finding.text
    assert "2025-07-01" in finding.text and "2025-04-01" in finding.text and "90 days" in finding.text


@pytest.mark.parametrize("kind,index,hypothesis", [
    ("review_retention", 0, False), ("investigate_price_volume", 0, False),
    ("review_cohort_mix", 0, False), ("review_trend", 0, False),
    ("review_performance", 19, False), ("review_performance", 0, True),
])
def test_existing_index_does_not_make_an_action_relevant(kind, index, hypothesis):
    action = ActionSelection(kind=kind, finding_index=index)
    with pytest.raises(PolicyError, match="unsupported_recommendation"):
        ground_report(ReportDraft(**{"hypotheses" if hypothesis else "recommendations": (action,)}), (fixture_evidence(),))


def test_full_workflow_trusted_text_persisted_and_json_additive(tmp_path):
    from test_step4 import FakeGateway, FakeTurn, SETTINGS
    from opsfleet_assistant.store import SQLiteStore
    from opsfleet_assistant.workflow import AssistantWorkflow
    class SelectedGateway(FakeGateway):
        def report(self, evidence, **kwargs):
            self.budget.charge("injected_model")
            return ReportDraft(findings=(FindingSelection(operation="observed_value", current=ref()),),
                headline_index=0, recommendations=(ActionSelection(kind="review_performance", finding_index=0),))
    store = SQLiteStore(tmp_path / "state.sqlite")
    result = AssistantWorkflow(SETTINGS, store, gateway_factory=SelectedGateway, turn_factory=FakeTurn).run(
        "manager_apparel", "Create a Q1 2025 revenue report with Q2 actions", conversation_id="grounded", turn_id="t1")
    assert result.status == "ok" and result.report_id
    assert result.text.startswith("Complete report:")
    assert "Observed revenue: 42.50; UTC [2025-01-01, 2025-04-01)" in result.text
    assert result.factual_status == "grounded" and result.factual_findings[0]["value"] == "42.50"
    assert result.model_requests == 2 and result.query_executions == 5
    saved = store.report("manager_apparel", result.report_id)
    assert saved["content"] == result.text
    assert json.loads(saved["grounded_report_json"])["schema_version"] == 1
    store.close()
    store = SQLiteStore(tmp_path / "state.sqlite")
    assistant = AssistantWorkflow(SETTINGS, store, gateway_factory=SelectedGateway, turn_factory=FakeTurn)
    replay = assistant.run("manager_apparel", "Create a Q1 2025 revenue report with Q2 actions", conversation_id="grounded", turn_id="t1")
    assert replay.reused_report and replay.model_requests == 0 and replay.query_executions == 1
    assert replay.factual_findings == result.factual_findings and replay.suggestions == result.suggestions
    # Legacy metadata stays unavailable; content remains an immutable saved artifact.
    store.db.execute("UPDATE reports SET grounded_report_json='',report_completeness='legacy',report_composition_json='',report_support_json='',report_binding_digest='' WHERE id=?", (result.report_id,))
    legacy = assistant.run("manager_apparel", "Create a Q1 2025 revenue report with Q2 actions", conversation_id="grounded", turn_id="t1")
    assert legacy.text == result.text and legacy.factual_status is None and legacy.factual_findings == ()
    store.close()


@pytest.mark.parametrize("failure", ["prose", "bad_reference", "irrelevant_action", "refused", "provider"])
def test_full_workflow_fallback_is_factual_and_bounded(tmp_path, failure):
    from test_step4 import FakeGateway, FakeTurn, SETTINGS
    from opsfleet_assistant.model_gateway import ModelFailure
    from opsfleet_assistant.store import SQLiteStore
    from opsfleet_assistant.workflow import AssistantWorkflow
    class BrokenGateway(FakeGateway):
        def report(self, evidence, **kwargs):
            self.budget.charge("injected_model")
            if failure == "provider":
                raise ModelFailure("direct_failure")
            if failure == "prose":
                return ReportDraft.model_validate({"headline": "Revenue doubled across the period"})
            if failure == "bad_reference":
                return ReportDraft(findings=(FindingSelection(operation="observed_value", current=ref(row=19)),))
            if failure == "irrelevant_action":
                return ReportDraft(recommendations=(ActionSelection(kind="review_retention", finding_index=0),))
            return ReportDraft(status="refused")
    store = SQLiteStore(tmp_path / "state.sqlite")
    result = AssistantWorkflow(SETTINGS, store, gateway_factory=BrokenGateway, turn_factory=FakeTurn).run(
        "manager_apparel", "Revenue in 2025")
    assert result.status == "ok" and result.factual_status == "grounded"
    assert result.model_requests == 2 and result.query_executions == 3
    assert "Observed revenue: 42.50" in result.text and "Revenue doubled" not in result.text
    assert "Model explanation unavailable" in result.text and "No model-selected actions" in result.text
    assert result.report_completeness is None  # Ordinary analysis does not claim report completion.
    assert result.suggestions == ()
    store.close()


def test_monthly_comparison_keeps_period_direction_and_population():
    item = fixture_evidence(operation="time_series", rows=(
        {"month": date(2025, 4, 1), "revenue": Decimal("100")},
        {"month": date(2025, 5, 1), "revenue": Decimal("200")}))
    selection = FindingSelection(operation="percentage_change", current=ref(row=1), baseline=ref())
    finding = calculate(selection, (item,))
    assert finding.value == 100 and finding.absolute_change == 100
    assert "month 2025-05-01" in finding.text and "month 2025-04-01" in finding.text
    with pytest.raises(PolicyError, match="invalid_finding_period"):
        calculate(selection.model_copy(update={"current": ref(), "baseline": ref(row=1)}), (item,))
    other = item.model_copy(update={"period_utc": {"period_start": "2025-07-01", "period_end": "2025-10-01"}})
    with pytest.raises(PolicyError, match="incompatible_finding_population"):
        calculate(change(), (other, item.model_copy(update={"rows": (item.rows[1],)})))


def test_full_workflow_independent_product_percentage_oracle(tmp_path):
    from test_step4 import FakeGateway, FakeTurn, SETTINGS
    from opsfleet_assistant.evidence import Coverage
    from opsfleet_assistant.contracts import InterpretationResult, RequestSpec, QuerySpec, TemporalExpression, YearExpression
    from opsfleet_assistant.store import SQLiteStore
    from opsfleet_assistant.workflow import AssistantWorkflow
    class ProductTurn(FakeTurn):
        def execute_compiled(self, query):
            self.executions += 2
            self.billed_bytes += 20
            rows = (
                {"product_id": 1, "revenue": Decimal("200"), "item_count": 20, "order_count": 10, "average_item_price": Decimal("10")},
                {"product_id": 2, "revenue": Decimal("100"), "item_count": 5, "order_count": 4, "average_item_price": Decimal("20")})
            schema = tuple(bigquery.SchemaField(k, "INT64" if k in {"product_id", "item_count", "order_count"} else "NUMERIC") for k in rows[0])
            return release_evidence(query, rows, schema, job_id="product_oracle", estimated_bytes=10, billed_bytes=10,
                query_as_of=datetime(2026, 11, 4, tzinfo=timezone.utc),
                coverage=Coverage(status="checked", source_rows=25, period_rows=25, note="Scoped fixture.", job_id="product_coverage"))
    class ProductGateway(FakeGateway):
        def interpret(self, *args, **kwargs):
            self.budget.charge("injected_model")
            return InterpretationResult(status="ready", request=RequestSpec(
                analysis=TemporalExpression(kind="year", year=YearExpression(kind="absolute", value=2025)),
                queries=(QuerySpec(operation="product_comparison", metric="revenue", product_ids=(1, 2)),)))
        def report(self, *args, **kwargs):
            self.budget.charge("injected_model")
            return ReportDraft(findings=(FindingSelection(operation="percentage_change", current=ref(), baseline=ref(row=1)),),
                recommendations=(ActionSelection(kind="investigate_price_volume", finding_index=0),))
    store = SQLiteStore(tmp_path / "state.sqlite")
    result = AssistantWorkflow(SETTINGS, store, gateway_factory=ProductGateway, turn_factory=ProductTurn).run(
        "manager_apparel", "Compare product 1 and 2 in 2025")
    assert result.status == "ok" and result.model_requests == 2 and result.query_executions == 3
    assert result.text.startswith("Trusted comparison: product_id 1 revenue minus product_id 2 revenue is 100.00")
    assert "Percentage change: 100.00%" in result.text
    assert result.factual_findings[0]["value"] == "100"
    assert result.factual_findings[0]["absolute_change"] == "100"
    assert "item count minus product_id 2 item count is 15" in result.text
    assert "average item price minus product_id 2 average item price is -10.00" in result.text
    assert "recommendations, not observed outcomes" in result.text
    store.close()


def test_empty_fallback_has_no_factual_claims_or_actions():
    item = fixture_evidence()
    item = item.model_copy(update={"rows": ({"revenue": "0"},), "complete": False,
                                  "coverage": item.coverage.model_copy(update={"period_rows": 0})})
    report = ground_report(ReportDraft(), (item,), fallback=True)
    assert report.factual_status == "evidence_only" and report.findings == ()
    assert report.headline == "Checked retail analysis: no supported factual finding available."
    assert report.recommendations == () and report.selection_status == "fallback"


def test_refusal_and_bad_selection_do_not_save_unqualified_report(tmp_path):
    from test_step4 import FakeGateway, FakeTurn, SETTINGS
    from opsfleet_assistant.store import SQLiteStore
    from opsfleet_assistant.workflow import AssistantWorkflow
    class RefusalGateway(FakeGateway):
        def report(self, *args, **kwargs):
            self.budget.charge("injected_model")
            return ReportDraft(status="refused")
    store = SQLiteStore(tmp_path / "state.sqlite")
    result = AssistantWorkflow(SETTINGS, store, gateway_factory=RefusalGateway, turn_factory=FakeTurn).run(
        "manager_apparel", "Create a Q1 2025 revenue report with Q2 actions")
    assert result.report_id and result.factual_status == "grounded"
    assert result.status == "partial" and result.report_completeness == "partial"
    assert result.text.startswith("Partial report:") and "recommendations" in result.report_composition["missing"]
    assert "No model-selected actions were released" in result.text
    stored = json.loads(store.report("manager_apparel", result.report_id)["grounded_report_json"])
    assert stored["selection_status"] == "refused" and stored["recommendations"] == []
    store.close()
