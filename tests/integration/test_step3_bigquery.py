"""Opt-in, bounded live checks against the public dataset using Step 3 adapter."""

from __future__ import annotations

from datetime import date
import json
import os

import pytest

from opsfleet_assistant.bigquery_adapter import BigQueryTurn, QueryLimits
from opsfleet_assistant.config import Settings
from opsfleet_assistant.contracts import AnalysisPlan, Period


pytestmark = pytest.mark.skipif(
    os.getenv("OPSFLEET_LIVE_TESTS") != "1", reason="set OPSFLEET_LIVE_TESTS=1 for bounded public-data checks",
)


def test_live_scoped_evidence_across_required_tables():
    settings = Settings.load()
    assert settings.project, "GOOGLE_CLOUD_PROJECT is required"
    limits = QueryLimits()
    print("\nStep 3 live limits:", limits)
    print("Six isolated turns; each has at most eight executions, 512 MiB per job and 1 GiB per turn; query cache disabled.")
    period = Period(start=date(2025, 1, 1), end=date(2026, 11, 1))

    def run(label, user, plan):
        turn = BigQueryTurn(settings, limits=limits)
        evidence = turn.run(plan, user)
        print(json.dumps({
            "case": label, "evidence": evidence.model_dump(mode="json"),
            "turn_executions": turn.executions,
            "turn_estimated_bytes": turn.estimated_bytes,
            "turn_billed_bytes": turn.billed_bytes,
        }, sort_keys=True))
        assert evidence.coverage.status == "checked"
        return evidence

    revenue = run("manager_revenue", "manager_apparel", AnalysisPlan(
        operation="aggregate", metric="revenue", period=period,
    ))
    assert revenue.rows[0]["revenue"] is not None

    customers = run("manager_customers", "manager_apparel", AnalysisPlan(
        operation="rank_customers", metric="customer_spend", period=period, limit=5,
    ))
    assert customers.rows

    demographic = run("manager_states", "manager_apparel", AnalysisPlan(
        operation="demographic_comparison", metric="average_customer_spend", period=period,
        dimension="state", demographic_values=("California", "Texas"),
    ))
    assert len(demographic.rows) == 2

    candidates = run("manager_product_candidates", "manager_apparel", AnalysisPlan(
        operation="aggregate", metric="revenue", period=period, dimension="product", limit=5,
    ))
    ids = tuple(row["product_id"] for row in candidates.rows[:2])
    assert len(ids) == 2
    products = run("manager_product_comparison", "manager_apparel", AnalysisPlan(
        operation="product_comparison", metric="revenue", period=period, product_ids=ids,
    ))
    assert len(products.rows) == 2

    ceo = run("ceo_all_brands", "ceo", AnalysisPlan(
        operation="aggregate", metric="revenue", period=period,
    ))
    assert ceo.rows[0]["revenue"] is not None
