"""Offline Step 2 contract, policy, and compiler checks."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pydantic import ValidationError
from sqlglot import exp, parse_one

from opsfleet_assistant.compiler import compile_plan
from opsfleet_assistant.contracts import AnalysisContext, AnalysisPlan, BrandScope, Period, Role
from opsfleet_assistant.identity import Catalog, PolicyError, resolve_demo_scope
from opsfleet_assistant.privacy import validate_source_fields


@pytest.fixture
def catalog() -> Catalog:
    return Catalog.from_products([
        {"id": 1, "brand": "Calvin Klein"},
        {"id": 2, "brand": "Carhartt"},
        {"id": 3, "brand": "Hanes"},
        {"id": 4, "brand": "Volcom"},
    ])


@pytest.fixture
def manager(catalog: Catalog) -> BrandScope:
    return resolve_demo_scope("manager_apparel", catalog)


@pytest.fixture
def period() -> Period:
    return Period(start=date(2026, 1, 1), end=date(2026, 2, 1))


def test_demo_identity_roles_and_catalog_validation(catalog: Catalog) -> None:
    manager = resolve_demo_scope("manager_apparel", catalog)
    assert manager.role == Role.MANAGER
    assert manager.brands == ("Calvin Klein", "Carhartt")
    ceo = resolve_demo_scope("ceo", catalog)
    assert ceo.role == Role.CEO
    assert ceo.brands == tuple(sorted(catalog.brands))
    with pytest.raises(PolicyError, match="unknown_identity"):
        resolve_demo_scope("not_a_user", catalog)
    with TemporaryDirectory() as directory:
        path = Path(directory) / "users.json"
        path.write_text('{"version":1,"users":[{"user_id":"bad","role":"manager","brands":[]}]}')
        with pytest.raises(PolicyError, match="invalid_identity_config"):
            resolve_demo_scope("bad", catalog, path)


def test_catalog_preserves_distinct_case_variants_without_ambiguous_alias() -> None:
    catalog = Catalog.from_products([
        {"id": 1, "brand": "NEXT LEVEL"},
        {"id": 2, "brand": "Next Level"},
    ])
    assert catalog.canonical_brand("NEXT LEVEL") == "NEXT LEVEL"
    assert catalog.canonical_brand("Next Level") == "Next Level"
    with pytest.raises(PolicyError, match="brand_not_available"):
        catalog.canonical_brand("next level")


def test_scope_never_expands_and_products_must_be_authorized(
    catalog: Catalog, manager: BrandScope, period: Period,
) -> None:
    base = dict(operation="aggregate", metric="revenue", period=period)
    query = compile_plan(AnalysisPlan(**base), manager, catalog)
    assert query.parameter("brands").value == ("Calvin Klein", "Carhartt")
    narrowed = compile_plan(
        AnalysisPlan(**base, requested_brands=("calvin klein",)), manager, catalog,
    )
    assert narrowed.effective_brands == ("Calvin Klein",)
    with pytest.raises(PolicyError, match="unauthorized_brand"):
        compile_plan(AnalysisPlan(**base, requested_brands=("Hanes",)), manager, catalog)
    with pytest.raises(PolicyError, match="unauthorized_brand"):
        compile_plan(AnalysisPlan(**base, requested_brands=("Calvin Klein", "Hanes")), manager, catalog)
    with pytest.raises(PolicyError, match="product_not_available"):
        compile_plan(AnalysisPlan(**base, product_ids=(3,)), manager, catalog)
    with pytest.raises(PolicyError, match="product_not_available"):
        compile_plan(AnalysisPlan(**base, product_ids=(999,)), manager, catalog)
    ceo = resolve_demo_scope("ceo", catalog)
    assert compile_plan(AnalysisPlan(**base, requested_brands=("Hanes",)), ceo, catalog).effective_brands == ("Hanes",)


def test_plan_cannot_add_sql_permissions_or_demographic_customer_ids(period: Period) -> None:
    base = dict(operation="rank_customers", metric="customer_spend", period=period)
    with pytest.raises(ValidationError):
        AnalysisPlan(**base, sql="SELECT * FROM users")
    with pytest.raises(ValidationError):
        AnalysisPlan(**base, role="ceo")
    with pytest.raises(ValidationError):
        AnalysisPlan(**base, dimension="state")
    with pytest.raises(ValidationError):
        AnalysisPlan(**base, demographic_values=("CA", "TX"))
    with pytest.raises(ValidationError):
        AnalysisPlan(operation="demographic_comparison", metric="customer_spend", period=period, dimension="state")
    with pytest.raises(ValidationError):
        AnalysisPlan(operation="aggregate", metric="revenue", period=period, dimension="email")


def test_inherited_demographic_cohort_cannot_become_customer_ids(
    catalog: Catalog, manager: BrandScope, period: Period,
) -> None:
    plan = AnalysisPlan(operation="rank_customers", metric="customer_spend", period=period)
    with pytest.raises(PolicyError, match="unsupported_cohort_reference"):
        compile_plan(plan, manager, catalog, AnalysisContext(inherited_cohort="demographic"))


def test_scope_filter_precedes_all_aggregations(
    catalog: Catalog, manager: BrandScope, period: Period,
) -> None:
    plans = [
        AnalysisPlan(operation="aggregate", metric="revenue", period=period),
        AnalysisPlan(operation="rank_customers", metric="customer_spend", period=period),
        AnalysisPlan(operation="time_series", metric="revenue", period=period),
        AnalysisPlan(operation="product_comparison", metric="revenue", period=period, product_ids=(1, 2)),
        AnalysisPlan(operation="demographic_comparison", metric="average_customer_spend", period=period, dimension="state"),
        AnalysisPlan(operation="churn_snapshot", metric="churn_rate", cutoff_date=date(2026, 5, 1)),
    ]
    for plan in plans:
        query = compile_plan(plan, manager, catalog)
        assert "p.brand IN UNNEST(@brands)" in query.sql
        assert query.sql.index("p.brand IN UNNEST(@brands)") < query.sql.index("FROM scoped_items")
        assert "o.total" not in query.sql
        assert "SELECT *" not in query.sql
        assert "email" not in query.sql
        statement = parse_one(query.sql, read="bigquery")
        assert isinstance(statement, exp.Select)
        assert query.parameter("brands").value == manager.brands


def test_demographic_result_contract_rejects_membership_and_small_groups(
    catalog: Catalog, manager: BrandScope, period: Period,
) -> None:
    query = compile_plan(AnalysisPlan(
        operation="demographic_comparison", metric="average_customer_spend",
        period=period, dimension="state", demographic_values=("CA", "TX"),
    ), manager, catalog)
    assert "HAVING COUNT(*) >= @min_group_size" in query.sql
    assert query.parameter("min_group_size").value == 5
    assert "u.state" in query.sql
    assert "user_id" not in query.result_contract.columns
    query.result_contract.validate_rows([{
        "demographic_value": "CA", "customers": 5,
        "revenue": 120.0, "average_customer_spend": 24.0,
    }])
    with pytest.raises(PolicyError, match="demographic_group_too_small"):
        query.result_contract.validate_rows([{
            "demographic_value": "TX", "customers": 4,
            "revenue": 80.0, "average_customer_spend": 20.0,
        }])
    with pytest.raises(PolicyError, match="result_schema_violation"):
        query.result_contract.validate_rows([{
            "demographic_value": "CA", "customers": 5,
            "revenue": 120.0, "average_customer_spend": 24.0,
            "user_id": 1,
        }])


def test_metric_defaults_overrides_and_utc_boundaries(
    catalog: Catalog, manager: BrandScope, period: Period,
) -> None:
    default = compile_plan(AnalysisPlan(operation="aggregate", metric="revenue", period=period), manager, catalog)
    assert default.parameter("statuses").value == ("Shipped", "Complete", "Processing")
    assert default.parameter("start_ts").value == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert default.parameter("end_ts").value == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert "oi.created_at >= @start_ts AND oi.created_at < @end_ts" in default.sql
    overridden = compile_plan(AnalysisPlan(
        operation="aggregate", metric="revenue", period=period,
        revenue_statuses=("Complete",),
    ), manager, catalog)
    assert overridden.parameter("statuses").value == ("Complete",)
    assert overridden.metric_definition["revenue_statuses"] == ["Complete"]
    churn = compile_plan(AnalysisPlan(
        operation="churn_snapshot", metric="churn_rate",
        cutoff_date=date(2026, 5, 1), inactivity_days=60,
    ), manager, catalog)
    assert churn.parameter("inactivity_threshold_ts").value == datetime(2026, 3, 2, tzinfo=timezone.utc)
    assert churn.metric_definition["inactivity_days"] == 60
    with pytest.raises(ValidationError):
        Period(start=date(2026, 2, 1), end=date(2026, 2, 1))


def test_customer_result_rejects_extra_pii(
    catalog: Catalog, manager: BrandScope, period: Period,
) -> None:
    query = compile_plan(AnalysisPlan(
        operation="rank_customers", metric="customer_spend", period=period,
    ), manager, catalog)
    query.result_contract.validate_rows([{"user_id": 1, "customer_spend": 120.0}])
    with pytest.raises(PolicyError, match="result_schema_violation"):
        query.result_contract.validate_rows([{
            "user_id": 1, "customer_spend": 120.0, "email": "private@example.com",
        }])


def test_source_field_registry_blocks_transformed_pii_and_unaggregated_demographics() -> None:
    with pytest.raises(PolicyError, match="prohibited_source_field"):
        validate_source_fields(parse_one("SELECT LOWER(email) FROM users"), demographic=True)
    with pytest.raises(PolicyError, match="aggregate_only_source_field"):
        validate_source_fields(parse_one("SELECT state FROM users"), demographic=False)
