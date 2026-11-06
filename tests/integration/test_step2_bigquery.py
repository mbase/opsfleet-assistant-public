"""Opt-in numeric reference checks using synthetic rows in BigQuery CTEs.

This does not query the public dataset or implement the Step 3 execution adapter.
Run with OPSFLEET_LIVE_TESTS=1 after configuring the Step 1 billing project.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from opsfleet_assistant.compiler import DATASET, BoundParameter, compile_plan
from opsfleet_assistant.config import Settings
from opsfleet_assistant.contracts import AnalysisPlan, BrandScope, Period, Role
from opsfleet_assistant.identity import Catalog


pytestmark = pytest.mark.skipif(
    os.getenv("OPSFLEET_LIVE_TESTS") != "1", reason="explicit live opt-in required",
)

FIXTURE_CTES = """
fixture_products AS (
  SELECT * FROM UNNEST([
    STRUCT(1 AS id, 'Calvin Klein' AS brand),
    STRUCT(2 AS id, 'Hanes' AS brand),
    STRUCT(3 AS id, 'Calvin Klein' AS brand)
  ])
),
fixture_orders AS (
  SELECT * FROM UNNEST([
    STRUCT(101 AS order_id, 1 AS user_id), STRUCT(102, 2), STRUCT(103, 3),
    STRUCT(104, 4), STRUCT(105, 5), STRUCT(106, 6), STRUCT(107, 7),
    STRUCT(108, 1), STRUCT(109, 8), STRUCT(110, 9), STRUCT(111, 10),
    STRUCT(112, 11), STRUCT(113, 12)
  ])
),
fixture_users AS (
  SELECT * FROM UNNEST([
    STRUCT(1 AS id, 'CA' AS state), STRUCT(2, 'CA'), STRUCT(3, 'CA'),
    STRUCT(4, 'CA'), STRUCT(5, 'CA'), STRUCT(6, 'CA'), STRUCT(7, 'CA'),
    STRUCT(8, 'CA'), STRUCT(9, 'CA'), STRUCT(10, 'CA'),
    STRUCT(11, 'TX'), STRUCT(12, 'TX')
  ])
),
fixture_order_items AS (
  SELECT * FROM UNNEST([
    STRUCT(101 AS order_id, 1 AS product_id, 'Shipped' AS status,
           TIMESTAMP '2026-01-10 12:00:00+00' AS created_at, 100.0 AS sale_price),
    STRUCT(101, 2, 'Shipped', TIMESTAMP '2026-01-10 12:00:00+00', __B_AMOUNT__),
    STRUCT(102, 3, 'Complete', TIMESTAMP '2026-01-10 12:00:00+00', 50.0),
    STRUCT(103, 1, 'Returned', TIMESTAMP '2026-01-10 12:00:00+00', 70.0),
    STRUCT(104, 1, 'Cancelled', TIMESTAMP '2026-01-10 12:00:00+00', 30.0),
    STRUCT(105, 1, 'Processing', TIMESTAMP '2026-01-10 12:00:00+00', 40.0),
    STRUCT(106, 1, 'Shipped', TIMESTAMP '2026-01-10 12:00:00+00', CAST(NULL AS FLOAT64)),
    STRUCT(107, 2, 'Shipped', TIMESTAMP '2026-01-10 12:00:00+00', 1000.0),
    STRUCT(108, 1, 'Shipped', TIMESTAMP '2026-04-15 12:00:00+00', 20.0),
    STRUCT(109, 1, 'Shipped', TIMESTAMP '2026-01-10 12:00:00+00', 10.0),
    STRUCT(110, 1, 'Shipped', TIMESTAMP '2026-01-10 12:00:00+00', 10.0),
    STRUCT(111, 1, 'Shipped', TIMESTAMP '2026-01-10 12:00:00+00', 10.0),
    STRUCT(112, 1, 'Shipped', TIMESTAMP '2026-01-10 12:00:00+00', 10.0),
    STRUCT(113, 1, 'Shipped', TIMESTAMP '2026-01-10 12:00:00+00', 10.0)
  ])
)
"""


def _query_parameters(parameters: tuple[BoundParameter, ...]):
    from google.cloud import bigquery

    converted = []
    for param in parameters:
        if param.type_name.startswith("ARRAY<"):
            converted.append(bigquery.ArrayQueryParameter(
                param.name, param.type_name.removeprefix("ARRAY<").removesuffix(">"),
                list(param.value),
            ))
        else:
            converted.append(bigquery.ScalarQueryParameter(
                param.name, param.type_name, param.value,
            ))
    return converted


def _fixture_sql(sql: str, brand_b_amount: float) -> str:
    amount = {900.0: "900.0", 999999.0: "999999.0"}[brand_b_amount]
    sql = sql.replace(f"`{DATASET}.products`", "fixture_products")
    sql = sql.replace(f"`{DATASET}.orders`", "fixture_orders")
    sql = sql.replace(f"`{DATASET}.order_items`", "fixture_order_items")
    sql = sql.replace(f"`{DATASET}.users`", "fixture_users")
    assert DATASET not in sql
    return sql.replace("WITH scoped_items AS", f"WITH {FIXTURE_CTES.replace('__B_AMOUNT__', amount)}, scoped_items AS", 1)


def test_compiled_queries_on_synthetic_bigquery_rows() -> None:
    from google.cloud import bigquery

    settings = Settings.load()
    assert settings.project, "GOOGLE_CLOUD_PROJECT must be set"
    client = bigquery.Client(project=settings.project, location=settings.location)
    catalog = Catalog.from_products([
        {"id": 1, "brand": "Calvin Klein"},
        {"id": 2, "brand": "Hanes"},
        {"id": 3, "brand": "Calvin Klein"},
    ])
    scope = BrandScope(user_id="fixture_manager", role=Role.MANAGER,
                       brands=("Calvin Klein",), authorization_version=1)
    january = Period(start=date(2026, 1, 1), end=date(2026, 2, 1))

    def run(plan: AnalysisPlan, brand_b_amount: float = 900.0) -> list[dict]:
        compiled = compile_plan(plan, scope, catalog)
        sql = _fixture_sql(compiled.sql, brand_b_amount)
        config = bigquery.QueryJobConfig(
            query_parameters=_query_parameters(compiled.parameters),
            maximum_bytes_billed=10 * 1024 * 1024,
            job_timeout_ms=30_000,
            labels={"purpose": "opsfleet_step2_fixture"},
        )
        dry = client.query(sql, job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False,
            query_parameters=config.query_parameters,
            maximum_bytes_billed=10 * 1024 * 1024,
        ), location=settings.location, retry=None, job_retry=None)
        assert (dry.total_bytes_processed or 0) <= 10 * 1024 * 1024
        job = client.query(sql, job_config=config, location=settings.location,
                           retry=None, job_retry=None)
        rows = [dict(row.items()) for row in job.result(
            timeout=35, max_results=100, retry=None, job_retry=None,
        )]
        compiled.result_contract.validate_rows(rows)
        return rows

    revenue_plan = AnalysisPlan(operation="aggregate", metric="revenue", period=january)
    assert run(revenue_plan)[0]["revenue"] == pytest.approx(240.0)
    assert run(revenue_plan, 999999.0)[0]["revenue"] == pytest.approx(240.0)
    assert run(AnalysisPlan(operation="aggregate", metric="order_count", period=january))[0]["order_count"] == 8
    assert run(AnalysisPlan(operation="aggregate", metric="average_order_value", period=january))[0]["average_order_value"] == pytest.approx(30.0)
    assert run(AnalysisPlan(operation="aggregate", metric="revenue", period=january,
                            revenue_statuses=("Complete",)))[0]["revenue"] == pytest.approx(50.0)

    ranked = run(AnalysisPlan(operation="rank_customers", metric="customer_spend", period=january))
    assert ranked[0] == {"user_id": 1, "customer_spend": pytest.approx(100.0)}
    compared = run(AnalysisPlan(operation="product_comparison", metric="revenue",
                                period=january, product_ids=(1, 3)))
    assert {row["product_id"]: row["revenue"] for row in compared} == {
        1: pytest.approx(190.0), 3: pytest.approx(50.0),
    }
    months = run(AnalysisPlan(operation="time_series", metric="revenue",
                              period=Period(start=date(2026, 1, 1), end=date(2026, 5, 1))))
    assert [(str(row["month"]), row["revenue"]) for row in months] == [
        ("2026-01-01", pytest.approx(240.0)), ("2026-04-01", pytest.approx(20.0)),
    ]
    states = run(AnalysisPlan(operation="demographic_comparison", metric="average_customer_spend",
                              period=january, dimension="state", demographic_values=("CA", "TX")))
    assert len(states) == 1 and states[0]["demographic_value"] == "CA"
    assert states[0]["customers"] == 6
    assert states[0]["revenue"] == pytest.approx(220.0)
    assert states[0]["average_customer_spend"] == pytest.approx(220.0 / 6)
    churn = run(AnalysisPlan(operation="churn_snapshot", metric="churn_rate",
                             cutoff_date=date(2026, 5, 1)))[0]
    assert churn["eligible_customers"] == 8
    assert churn["inactive_customers"] == 7
    assert churn["churn_rate"] == pytest.approx(7 / 8)
