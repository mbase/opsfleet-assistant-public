"""Compile approved analytical operations to bounded BigQuery Standard SQL.

All identifiers and expressions come from this module. Request values, including
brands and product IDs, are bound parameters. The caller must provide a scope
resolved by the trusted identity layer and a current product catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Any

import sqlglot
from sqlglot import exp

from .contracts import AnalysisContext, AnalysisPlan, Dimension, Metric, Operation, BrandScope
from .identity import Catalog, PolicyError, authorize_products, effective_brands
from .metrics import MIN_DEMOGRAPHIC_CUSTOMERS, effective_definition
from .privacy import FIELD_POLICY_VERSION, ResultContract, validate_source_fields


DATASET = "bigquery-public-data.thelook_ecommerce"
_COMPILER_KEY = secrets.token_bytes(32)
_BASE_FROM = f"""
FROM `{DATASET}.order_items` AS oi
JOIN `{DATASET}.orders` AS o ON oi.order_id = o.order_id
JOIN `{DATASET}.products` AS p ON oi.product_id = p.id
"""
_METRIC_SQL = {
    Metric.REVENUE: "COALESCE(SUM(sale_price), 0) AS revenue",
    Metric.ORDER_COUNT: "COUNT(DISTINCT order_id) AS order_count",
    Metric.AVERAGE_ORDER_VALUE: "SAFE_DIVIDE(SUM(sale_price), COUNT(DISTINCT order_id)) AS average_order_value",
    Metric.CUSTOMER_COUNT: "COUNT(DISTINCT user_id) AS customer_count",
}
_DEMOGRAPHIC_SQL = {
    Dimension.CITY: "u.city",
    Dimension.STATE: "u.state",
    Dimension.COUNTRY: "u.country",
    Dimension.GENDER: "u.gender",
    Dimension.TRAFFIC_SOURCE: "u.traffic_source",
    Dimension.AGE_BAND: """CASE
        WHEN u.age IS NULL THEN 'Unknown'
        WHEN u.age < 18 THEN 'Under 18'
        WHEN u.age < 25 THEN '18-24'
        WHEN u.age < 35 THEN '25-34'
        WHEN u.age < 45 THEN '35-44'
        WHEN u.age < 55 THEN '45-54'
        ELSE '55+'
    END""",
}


@dataclass(frozen=True)
class BoundParameter:
    name: str
    type_name: str
    value: Any


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    parameters: tuple[BoundParameter, ...]
    result_contract: ResultContract
    effective_brands: tuple[str, ...]
    metric_definition: dict[str, object]
    field_policy_version: int
    operation: Operation
    scope_user_id: str
    scope_role: str
    scope_version: int
    requested_product_ids: tuple[int, ...] | None
    requested_demographic_values: tuple[str, ...] | None
    _seal: str = field(default="", repr=False, compare=False)

    def parameter(self, name: str) -> BoundParameter:
        return next(param for param in self.parameters if param.name == name)


def _signature(query: CompiledQuery) -> str:
    material = repr((
        query.sql, query.parameters, query.result_contract, query.effective_brands,
        query.metric_definition, query.field_policy_version, query.operation,
        query.scope_user_id, query.scope_role, query.scope_version,
        query.requested_product_ids, query.requested_demographic_values,
    )).encode("utf-8")
    return hmac.new(_COMPILER_KEY, material, hashlib.sha256).hexdigest()


def is_trusted_compiled(query: object) -> bool:
    """Detect construction or mutation outside this compiler's approved path."""
    return (isinstance(query, CompiledQuery) and bool(query._seal)
            and hmac.compare_digest(query._seal, _signature(query)))


def _utc_midnight(day) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _scoped_items(plan: AnalysisPlan, *, churn: bool) -> str:
    date_condition = (
        "AND oi.created_at < @cutoff_ts" if churn else
        "AND oi.created_at >= @start_ts AND oi.created_at < @end_ts"
    )
    product_condition = (
        "AND oi.product_id IN UNNEST(@product_ids)" if plan.product_ids is not None else ""
    )
    return f"""scoped_items AS (
    SELECT oi.order_id, o.user_id, oi.product_id, oi.created_at,
           oi.sale_price, p.brand
    {_BASE_FROM}
    WHERE p.brand IN UNNEST(@brands)
      AND oi.status IN UNNEST(@statuses)
      AND oi.sale_price IS NOT NULL
      {date_condition}
      {product_condition}
)"""


def _compile_body(plan: AnalysisPlan) -> tuple[str, ResultContract]:
    if plan.operation == Operation.AGGREGATE:
        metric_sql = _METRIC_SQL[plan.metric]
        if plan.dimension is None:
            return (f"SELECT {metric_sql} FROM scoped_items",
                    ResultContract((plan.metric.value,), max_rows=1))
        dimension_sql = "brand" if plan.dimension == Dimension.BRAND else "product_id"
        query = (f"SELECT {dimension_sql}, {metric_sql} FROM scoped_items "
                 f"GROUP BY 1 ORDER BY {plan.metric.value} DESC, {dimension_sql} ASC LIMIT @limit")
        return query, ResultContract((dimension_sql, plan.metric.value), max_rows=plan.limit)

    if plan.operation == Operation.TIME_SERIES:
        metric_sql = _METRIC_SQL[plan.metric]
        query = ("SELECT DATE_TRUNC(DATE(created_at, 'UTC'), MONTH) AS month, "
                 f"{metric_sql} FROM scoped_items GROUP BY 1 ORDER BY month ASC LIMIT @limit")
        return query, ResultContract(("month", plan.metric.value), max_rows=plan.limit)

    if plan.operation == Operation.RANK_CUSTOMERS:
        query = ("SELECT user_id, SUM(sale_price) AS customer_spend "
                 "FROM scoped_items WHERE user_id IS NOT NULL GROUP BY user_id "
                 "ORDER BY customer_spend DESC, user_id ASC LIMIT @limit")
        return query, ResultContract(("user_id", "customer_spend"), customer_ids=True,
                                     max_rows=plan.limit)

    if plan.operation == Operation.PRODUCT_COMPARISON:
        query = ("SELECT product_id, SUM(sale_price) AS revenue, "
                 "COUNT(*) AS item_count, COUNT(DISTINCT order_id) AS order_count, "
                 "SAFE_DIVIDE(SUM(sale_price), COUNT(*)) AS average_item_price "
                 "FROM scoped_items GROUP BY product_id ORDER BY product_id")
        return query, ResultContract(
            ("product_id", "revenue", "item_count", "order_count", "average_item_price"),
            max_rows=2,
        )

    if plan.operation == Operation.DEMOGRAPHIC_COMPARISON:
        dimension = plan.dimension
        assert dimension is not None
        expression = _DEMOGRAPHIC_SQL[dimension]
        filter_sql = (
            "AND demographic_value IN UNNEST(@demographic_values)"
            if plan.demographic_values is not None else ""
        )
        query = f""", customer_spend AS (
    SELECT user_id, SUM(sale_price) AS spend
    FROM scoped_items WHERE user_id IS NOT NULL GROUP BY user_id
), demographic_customers AS (
    SELECT {expression} AS demographic_value, cs.spend
    FROM customer_spend AS cs
    JOIN `{DATASET}.users` AS u ON cs.user_id = u.id
)
SELECT demographic_value, COUNT(*) AS customers, SUM(spend) AS revenue,
       SAFE_DIVIDE(SUM(spend), COUNT(*)) AS average_customer_spend
FROM demographic_customers
WHERE demographic_value IS NOT NULL {filter_sql}
GROUP BY demographic_value
HAVING COUNT(*) >= @min_group_size
ORDER BY average_customer_spend DESC, demographic_value ASC
LIMIT @limit"""
        return query, ResultContract(
            ("demographic_value", "customers", "revenue", "average_customer_spend"),
            demographic=True, max_rows=plan.limit,
        )

    if plan.operation == Operation.CHURN_SNAPSHOT:
        query = """, customer_activity AS (
    SELECT user_id, MAX(created_at) AS last_purchase
    FROM scoped_items WHERE user_id IS NOT NULL GROUP BY user_id
)
SELECT COUNT(*) AS eligible_customers,
       COUNTIF(last_purchase <= @inactivity_threshold_ts) AS inactive_customers,
       SAFE_DIVIDE(COUNTIF(last_purchase <= @inactivity_threshold_ts), COUNT(*)) AS churn_rate
FROM customer_activity"""
        return query, ResultContract(
            ("eligible_customers", "inactive_customers", "churn_rate"), max_rows=1,
        )
    raise PolicyError("unsupported_operation")


def compile_plan(
    plan: AnalysisPlan,
    scope: BrandScope,
    catalog: Catalog,
    context: AnalysisContext | None = None,
) -> CompiledQuery:
    """Authorize scope, then compile a single read-only statement."""
    if context is not None and context.inherited_cohort is not None:
        # No inherited-cohort SQL operator exists yet. Reject rather than
        # interpreting "their IDs" as an unscoped customer ranking.
        raise PolicyError("unsupported_cohort_reference")
    brands = effective_brands(scope, plan.requested_brands, catalog)
    authorize_products(plan.product_ids, brands, catalog)
    params = [
        BoundParameter("brands", "ARRAY<STRING>", brands),
        BoundParameter("statuses", "ARRAY<STRING>", tuple(status.value for status in plan.revenue_statuses)),
    ]
    if plan.product_ids is not None:
        params.append(BoundParameter("product_ids", "ARRAY<INT64>", plan.product_ids))
    churn = plan.operation == Operation.CHURN_SNAPSHOT
    if churn:
        assert plan.cutoff_date is not None
        cutoff = _utc_midnight(plan.cutoff_date)
        params.extend((
            BoundParameter("cutoff_ts", "TIMESTAMP", cutoff),
            BoundParameter("inactivity_threshold_ts", "TIMESTAMP", cutoff - timedelta(days=plan.inactivity_days)),
        ))
    else:
        assert plan.period is not None
        params.extend((
            BoundParameter("start_ts", "TIMESTAMP", _utc_midnight(plan.period.start)),
            BoundParameter("end_ts", "TIMESTAMP", _utc_midnight(plan.period.end)),
        ))
    if plan.demographic_values is not None:
        params.append(BoundParameter("demographic_values", "ARRAY<STRING>", plan.demographic_values))
    if plan.operation == Operation.DEMOGRAPHIC_COMPARISON:
        params.append(BoundParameter("min_group_size", "INT64", MIN_DEMOGRAPHIC_CUSTOMERS))
    if plan.operation in {Operation.AGGREGATE, Operation.TIME_SERIES,
                          Operation.RANK_CUSTOMERS, Operation.DEMOGRAPHIC_COMPARISON}:
        params.append(BoundParameter("limit", "INT64", plan.limit))

    body, contract = _compile_body(plan)
    sql = f"WITH {_scoped_items(plan, churn=churn)}\n{body}"
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except Exception as exc:
        raise PolicyError("compiler_syntax_error") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise PolicyError("compiler_syntax_error")
    validate_source_fields(
        statements[0], demographic=plan.operation == Operation.DEMOGRAPHIC_COMPARISON,
    )
    query = CompiledQuery(
        sql=sql,
        parameters=tuple(params),
        result_contract=contract,
        effective_brands=brands,
        metric_definition=effective_definition(plan),
        field_policy_version=FIELD_POLICY_VERSION,
        operation=plan.operation,
        scope_user_id=scope.user_id,
        scope_role=scope.role.value,
        scope_version=scope.authorization_version,
        requested_product_ids=plan.product_ids,
        requested_demographic_values=plan.demographic_values,
    )
    return replace(query, _seal=_signature(query))
