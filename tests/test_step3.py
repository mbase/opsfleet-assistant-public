"""Offline Step 3 adapter and release checks with an injected BigQuery client."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as google_errors
from google.cloud import bigquery

from opsfleet_assistant.bigquery_adapter import BigQueryTurn, ExecutionError, QueryLimits
from opsfleet_assistant.compiler import compile_plan
from opsfleet_assistant.config import Settings
from opsfleet_assistant.contracts import AnalysisPlan, Period
from opsfleet_assistant.identity import Catalog, resolve_demo_scope


NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
CATALOG = ({"id": 1, "brand": "Calvin Klein"}, {"id": 2, "brand": "Carhartt"},
           {"id": 3, "brand": "Hanes"}, {"id": 4, "brand": "Volcom"})


def fields(*items):
    return tuple(bigquery.SchemaField(name, kind) for name, kind in items)


class Job:
    def __init__(self, rows, schema, *, billed=10, failure=None, job_id="test_job"):
        self.rows = rows
        self.schema = schema
        self.total_bytes_billed = billed
        self.failure = failure
        self.job_id = job_id
        self.ended = NOW
        self.cancelled = False

    def result(self, **kwargs):
        assert kwargs["retry"] is None and kwargs["job_retry"] is None
        if self.failure:
            raise self.failure
        return self.rows

    def cancel(self, **kwargs):
        assert kwargs["retry"] is None and kwargs["timeout"] <= 1.0
        self.cancelled = True


class Client:
    def __init__(self, stages):
        self.stages = list(stages)
        self.calls = []

    def query(self, sql, *, job_config, location, timeout, retry, job_retry):
        assert retry is None and job_retry is None and timeout > 0
        self.calls.append((sql, job_config))
        estimate, job = self.stages[0]
        if job_config.dry_run:
            return SimpleNamespace(total_bytes_processed=estimate)
        self.stages.pop(0)
        assert job_config.use_query_cache is False
        assert job_config.maximum_bytes_billed > 0
        return job


def stage(rows, schema, estimate=10, billed=10, failure=None, job_id="test_job"):
    return estimate, Job(rows, schema, billed=billed, failure=failure, job_id=job_id)


def base_stages(rows=None, schema=None):
    if rows is None:
        rows = ({"revenue": Decimal("42.50")},)
    if schema is None:
        schema = fields(("revenue", "NUMERIC"))
    return [
        stage(CATALOG, fields(("id", "INT64"), ("brand", "STRING")), job_id="catalog_job"),
        stage(rows, schema, job_id="analysis_job"),
        stage(({"source_rows": 9, "period_rows": 4, "latest_item_at": NOW},),
              fields(("source_rows", "INT64"), ("period_rows", "INT64"), ("latest_item_at", "TIMESTAMP")),
              job_id="coverage_job"),
    ]


def turn(stages, limits=QueryLimits()):
    client = Client(stages)
    return BigQueryTurn(Settings(project="test-project", location="US", model="unused"),
                        client=client, limits=limits), client


def plan(**kwargs):
    return AnalysisPlan(operation="aggregate", metric="revenue",
                        period=Period(start=date(2026, 1, 1), end=date(2026, 2, 1)), **kwargs)


def category(exc):
    return exc.value.category


def test_parameter_translation_and_safe_evidence():
    runner, client = turn(base_stages())
    evidence = runner.run(plan(), "manager_apparel")
    assert evidence.rows == ({"revenue": "42.50"},)
    assert evidence.complete and evidence.coverage.status == "checked"
    assert evidence.coverage.latest_item_at_utc == NOW.isoformat()
    assert evidence.job_id == "analysis_job"
    assert evidence.scope_reference == "demo:manager_apparel"
    assert evidence.metric_definition["revenue_statuses"] == ["Shipped", "Complete", "Processing"]
    assert runner.executions == 3 and runner.billed_bytes == 30 and runner.estimated_bytes == 30
    assert len(client.calls) == 6
    for dry, live in zip(client.calls[::2], client.calls[1::2]):
        assert dry[0] == live[0]
        assert [p.to_api_repr() for p in dry[1].query_parameters] == [p.to_api_repr() for p in live[1].query_parameters]
    analysis_params = {p.name: p for p in client.calls[3][1].query_parameters}
    assert analysis_params["brands"].array_type == "STRING"
    assert analysis_params["start_ts"].type_ == "TIMESTAMP"


def test_dry_run_rejects_before_execution_and_cumulative_cap():
    stages = base_stages()
    stages[1] = (101, stages[1][1])
    runner, client = turn(stages, QueryLimits(per_job_bytes=100, per_turn_bytes=200))
    with pytest.raises(ExecutionError) as exc:
        runner.run(plan(), "manager_apparel")
    assert category(exc) == "job_byte_limit"
    assert runner.executions == 1 and len(client.calls) == 3

    stages = base_stages()
    stages[0] = stage(CATALOG, fields(("id", "INT64"), ("brand", "STRING")),
                      estimate=10, billed=80)
    stages[1] = stage(({"revenue": Decimal("42.50")},), fields(("revenue", "NUMERIC")), estimate=30)
    runner, client = turn(stages, QueryLimits(per_job_bytes=100, per_turn_bytes=100))
    with pytest.raises(ExecutionError) as exc:
        runner.run(plan(), "manager_apparel")
    assert category(exc) == "turn_byte_limit"
    assert runner.executions == 1


def test_query_count_and_unauthorized_scope_and_tampering():
    runner, _ = turn(base_stages(), QueryLimits(max_executions=1))
    with pytest.raises(ExecutionError) as exc:
        runner.run(plan(), "manager_apparel")
    assert category(exc) == "query_count_limit"

    runner, client = turn(base_stages())
    catalog = Catalog.from_products(CATALOG)
    forged_scope = resolve_demo_scope("ceo", catalog).model_copy(update={"user_id": "manager_apparel"})
    compiled = compile_plan(plan(requested_brands=("Hanes",)), forged_scope, catalog)
    with pytest.raises(ExecutionError) as exc:
        runner.execute_compiled(compiled)
    assert category(exc) == "unauthorized_scope" and runner.executions == 1
    valid = compile_plan(plan(), resolve_demo_scope("manager_apparel", catalog), catalog)
    with pytest.raises(ExecutionError) as exc:
        runner.execute_compiled(replace(valid, sql="SELECT email FROM users"))
    assert category(exc) == "untrusted_compiled_query"


def test_timeout_requests_cancellation_and_permission_is_safe():
    stages = base_stages()
    failed = Job((), fields(("revenue", "NUMERIC")), billed=None, failure=TimeoutError("secret SQL"))
    stages[1] = (10, failed)
    runner, _ = turn(stages)
    with pytest.raises(ExecutionError) as exc:
        runner.run(plan(), "manager_apparel")
    assert category(exc) == "timeout_cancellation_requested"
    assert exc.value.kind == "transient" and "secret" not in str(exc.value)
    assert failed.cancelled and runner.billed_bytes == 10 + runner.limits.per_job_bytes

    stages = base_stages()
    stages[1] = (10, Job((), fields(("revenue", "NUMERIC")), failure=google_errors.Forbidden("secret")))
    runner, _ = turn(stages)
    with pytest.raises(ExecutionError) as exc:
        runner.run(plan(), "manager_apparel")
    assert category(exc) == "permission_denied" and exc.value.kind == "provider"

    stages = base_stages()
    stages[1] = (10, Job((), fields(("revenue", "NUMERIC")),
                         failure=google_errors.ServiceUnavailable("secret")))
    runner, _ = turn(stages)
    with pytest.raises(ExecutionError) as exc:
        runner.run(plan(), "manager_apparel")
    assert category(exc) == "provider_transient" and exc.value.kind == "transient"


def test_actual_billing_limit_is_counted_conservatively():
    stages = base_stages()
    stages[1] = stage(({"revenue": Decimal("42.50")},), fields(("revenue", "NUMERIC")),
                      estimate=10, billed=95)
    runner, _ = turn(stages, QueryLimits(per_job_bytes=90, per_turn_bytes=200))
    with pytest.raises(ExecutionError) as exc:
        runner.run(plan(), "manager_apparel")
    assert category(exc) == "result_or_billing_limit"
    assert runner.billed_bytes >= 105


def test_result_cap_rejects_extra_rows():
    stages = base_stages(({"revenue": Decimal("1")}, {"revenue": Decimal("2")}),
                         fields(("revenue", "NUMERIC")))
    runner, _ = turn(stages)
    with pytest.raises(ExecutionError) as exc:
        runner.run(plan(), "manager_apparel")
    assert category(exc) == "result_or_billing_limit"


@pytest.mark.parametrize("rows,schema,expected", [
    ((), fields(("revenue", "NUMERIC")), "result_empty_violation"),
    (({"revenue": Decimal("1"), "email": "secret@example.com"},),
     fields(("revenue", "NUMERIC"), ("email", "STRING")), "result_schema_violation"),
    (({"revenue": "bad"},), fields(("revenue", "NUMERIC")), "result_type_violation"),
    (({"revenue": float("nan")},), fields(("revenue", "FLOAT64")), "result_type_violation"),
    (({"revenue": Decimal("NaN")},), fields(("revenue", "NUMERIC")), "result_type_violation"),
])
def test_result_validation(rows, schema, expected):
    runner, _ = turn(base_stages(rows, schema))
    with pytest.raises(ExecutionError) as exc:
        runner.run(plan(), "manager_apparel")
    assert category(exc) == expected


def test_partial_product_and_demographic_comparisons_are_rejected():
    product = AnalysisPlan(operation="product_comparison", metric="revenue",
                           period=Period(start=date(2026, 1, 1), end=date(2026, 2, 1)),
                           product_ids=(1, 2))
    rows = ({"product_id": 1, "revenue": Decimal("12"), "item_count": 1,
             "order_count": 1, "average_item_price": Decimal("12")},)
    schema = fields(("product_id", "INT64"), ("revenue", "NUMERIC"),
                    ("item_count", "INT64"), ("order_count", "INT64"),
                    ("average_item_price", "NUMERIC"))
    runner, client = turn(base_stages(rows, schema))
    with pytest.raises(ExecutionError) as exc:
        runner.run(product, "manager_apparel")
    assert category(exc) == "incomplete_comparison"
    assert "oi.product_id IN UNNEST(@product_ids)" in client.calls[5][0]

    demographic = AnalysisPlan(operation="demographic_comparison", metric="average_customer_spend",
                               dimension="state", demographic_values=("CA", "TX"),
                               period=Period(start=date(2026, 1, 1), end=date(2026, 2, 1)))
    schema = fields(("demographic_value", "STRING"), ("customers", "INT64"),
                    ("revenue", "NUMERIC"), ("average_customer_spend", "NUMERIC"))
    rows = ({"demographic_value": "CA", "customers": 5, "revenue": Decimal("50"),
             "average_customer_spend": Decimal("10")},)
    runner, _ = turn(base_stages(rows, schema))
    with pytest.raises(ExecutionError) as exc:
        runner.run(demographic, "manager_apparel")
    assert category(exc) == "incomplete_comparison"
    rows = ({**rows[0], "customers": 4}, {"demographic_value": "TX", "customers": 5,
            "revenue": Decimal("50"), "average_customer_spend": Decimal("10")})
    runner, _ = turn(base_stages(rows, schema))
    with pytest.raises(ExecutionError) as exc:
        runner.run(demographic, "manager_apparel")
    assert category(exc) == "demographic_group_too_small"


def test_complete_demographic_evidence_withholds_complementary_counts():
    demographic = AnalysisPlan(operation="demographic_comparison", metric="average_customer_spend",
                               dimension="state", demographic_values=("CA", "TX"),
                               period=Period(start=date(2026, 1, 1), end=date(2026, 2, 1)))
    schema = fields(("demographic_value", "STRING"), ("customers", "INT64"),
                    ("revenue", "NUMERIC"), ("average_customer_spend", "NUMERIC"))
    rows = ({"demographic_value": "CA", "customers": 5, "revenue": Decimal("50"),
             "average_customer_spend": Decimal("10")},
            {"demographic_value": "TX", "customers": 6, "revenue": Decimal("60"),
             "average_customer_spend": Decimal("10")})
    runner, _ = turn(base_stages(rows, schema))
    evidence = runner.run(demographic, "manager_apparel")
    assert evidence.complete
    assert evidence.coverage.source_rows is None and evidence.coverage.period_rows is None


def test_limit_hit_marks_grouped_result_incomplete():
    rows = ({"brand": "Calvin Klein", "revenue": Decimal("1")},)
    schema = fields(("brand", "STRING"), ("revenue", "NUMERIC"))
    runner, _ = turn(base_stages(rows, schema))
    evidence = runner.run(plan(dimension="brand", limit=1), "manager_apparel")
    assert not evidence.complete and "row limit" in evidence.completeness_note


def test_zero_aggregate_discloses_no_qualifying_items():
    stages = base_stages(({"revenue": Decimal("0")},), fields(("revenue", "NUMERIC")))
    stages[2] = stage(({"source_rows": 9, "period_rows": 0, "latest_item_at": NOW},),
                      fields(("source_rows", "INT64"), ("period_rows", "INT64"),
                             ("latest_item_at", "TIMESTAMP")))
    runner, _ = turn(stages)
    evidence = runner.run(plan(), "manager_apparel")
    assert not evidence.complete and "No qualifying scoped items" in evidence.completeness_note


def test_customer_ids_require_scoped_membership_check():
    ranked = AnalysisPlan(operation="rank_customers", metric="customer_spend",
                          period=Period(start=date(2026, 1, 1), end=date(2026, 2, 1)))
    stages = base_stages(({"user_id": 123, "customer_spend": Decimal("9")},),
                         fields(("user_id", "INT64"), ("customer_spend", "NUMERIC")))
    stages.insert(2, stage(({"matched_customers": 0},), fields(("matched_customers", "INT64"))))
    runner, client = turn(stages)
    with pytest.raises(ExecutionError) as exc:
        runner.run(ranked, "manager_apparel")
    assert category(exc) == "result_scope_violation" and runner.executions == 3
    assert "candidate_ids" in {p.name for p in client.calls[5][1].query_parameters}
