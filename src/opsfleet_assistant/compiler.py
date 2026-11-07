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
from .source_schema import PUBLIC_BINDING, SourceBinding, bind_sql
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
    semantic_plan: str = ""
    temporal_role: str = "analysis"
    source_binding_version: str = "public_v1"
    compiler_version: int = 1
    _seal: str = field(default="", repr=False, compare=False)

    def parameter(self, name: str) -> BoundParameter:
        return next(param for param in self.parameters if param.name == name)


def _signature(query: CompiledQuery) -> str:
    material = repr((
        query.sql, query.parameters, query.result_contract, query.effective_brands,
        query.metric_definition, query.field_policy_version, query.operation,
        query.scope_user_id, query.scope_role, query.scope_version,
        query.requested_product_ids, query.requested_demographic_values,
        query.source_binding_version, query.compiler_version, query.semantic_plan, query.temporal_role,
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

    if plan.operation == Operation.REGIONAL_SPEND:
        query = f""", purchaser_spend AS (
    SELECT user_id, SUM(sale_price) AS spend, COUNT(DISTINCT order_id) AS orders
    FROM scoped_items WHERE user_id IS NOT NULL GROUP BY user_id
)
SELECT u.state AS demographic_value, COUNT(*) AS customers, SUM(cs.spend) AS revenue,
       SAFE_DIVIDE(SUM(cs.spend), COUNT(*)) AS average_customer_spend,
       SUM(cs.orders) AS order_count
FROM purchaser_spend AS cs JOIN `{DATASET}.users` AS u ON cs.user_id = u.id
WHERE u.state IN UNNEST(@demographic_values)
GROUP BY u.state HAVING COUNT(*) >= @min_group_size
ORDER BY demographic_value"""
        return query, ResultContract(
            ("demographic_value", "customers", "revenue", "average_customer_spend", "order_count"),
            demographic=True, max_rows=2)

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

    if plan.operation == Operation.CHURN_CHANGE:
        # Global all-or-nothing segment release. Headline is never combined with
        # an independently selected top-N / suppressed segment subset.
        query = f""", cutoffs AS (
    SELECT 'current' AS cutoff_role, @cutoff_ts AS cutoff, @inactivity_threshold_ts AS threshold
    UNION ALL SELECT 'baseline', @baseline_cutoff_ts, @baseline_threshold_ts
), activity AS (
    SELECT c.cutoff_role, si.user_id, MAX(si.created_at) AS last_purchase, c.threshold
    FROM scoped_items AS si CROSS JOIN cutoffs AS c
    WHERE si.user_id IS NOT NULL AND si.created_at < c.cutoff
    GROUP BY c.cutoff_role, si.user_id, c.threshold
), segments AS (
    SELECT a.cutoff_role,
           CASE WHEN u.country IS NULL THEN 'U:' ELSE CONCAT('C:', u.country) END AS segment_key,
           COUNT(*) AS eligible_customers,
           COUNTIF(a.last_purchase <= a.threshold) AS inactive_customers
    FROM activity AS a LEFT JOIN `{DATASET}.users` AS u ON a.user_id = u.id
    GROUP BY a.cutoff_role, segment_key
), headlines AS (
    SELECT cutoff_role, 'H:' AS segment_key, SUM(eligible_customers) AS eligible_customers,
           SUM(inactive_customers) AS inactive_customers
    FROM segments GROUP BY cutoff_role
), headline_safe AS (
    SELECT COUNT(*) = 2 AND MIN(inactive_customers) >= @min_group_size
       AND MIN(eligible_customers - inactive_customers) >= @min_group_size AS safe
    FROM headlines
), segment_safe AS (
    SELECT COUNT(*) <= 18 AND COUNT(*) >= 2
       AND MIN(inactive_customers) >= @min_group_size
       AND MIN(eligible_customers - inactive_customers) >= @min_group_size
       AND NOT EXISTS (SELECT segment_key FROM segments GROUP BY segment_key HAVING COUNT(*) != 2) AS safe
    FROM segments
), released AS (
    SELECT h.* FROM headlines AS h CROSS JOIN headline_safe AS hs WHERE hs.safe
    UNION ALL
    SELECT s.* FROM segments AS s CROSS JOIN headline_safe AS hs CROSS JOIN segment_safe AS ss
    WHERE hs.safe AND ss.safe
)
SELECT cutoff_role, segment_key, eligible_customers, inactive_customers,
       SAFE_DIVIDE(inactive_customers, eligible_customers) AS churn_rate
FROM released ORDER BY segment_key, cutoff_role DESC"""
        return query, ResultContract(("cutoff_role", "segment_key", "eligible_customers", "inactive_customers", "churn_rate"), max_rows=20)

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
    *, binding: SourceBinding = PUBLIC_BINDING, temporal_role: str = "analysis",
) -> CompiledQuery:
    """Authorize scope, then compile a single read-only statement."""
    if temporal_role not in {"analysis", "comparison"}:
        raise PolicyError("invalid_plan_contract")
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
    churn = plan.operation in {Operation.CHURN_SNAPSHOT, Operation.CHURN_CHANGE}
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
    if plan.operation == Operation.CHURN_CHANGE:
        baseline = _utc_midnight(plan.baseline_cutoff_date)
        params.extend((BoundParameter("baseline_cutoff_ts", "TIMESTAMP", baseline),
                       BoundParameter("baseline_threshold_ts", "TIMESTAMP", baseline - timedelta(days=90))))
    if plan.demographic_values is not None:
        params.append(BoundParameter("demographic_values", "ARRAY<STRING>", plan.demographic_values))
    if plan.operation in {Operation.DEMOGRAPHIC_COMPARISON, Operation.REGIONAL_SPEND, Operation.CHURN_CHANGE}:
        params.append(BoundParameter("min_group_size", "INT64", MIN_DEMOGRAPHIC_CUSTOMERS))
    if plan.operation in {Operation.AGGREGATE, Operation.TIME_SERIES,
                          Operation.RANK_CUSTOMERS, Operation.DEMOGRAPHIC_COMPARISON}:
        params.append(BoundParameter("limit", "INT64", plan.limit))

    body, contract = _compile_body(plan)
    sql = f"WITH {_scoped_items(plan, churn=churn)}\n{body}"
    try:
        sql = bind_sql(sql, binding)
        statements = sqlglot.parse(sql, read="bigquery")
    except PolicyError:
        raise
    except Exception as exc:
        raise PolicyError("compiler_syntax_error") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise PolicyError("compiler_syntax_error")
    validate_source_fields(
        statements[0], demographic=plan.operation in {Operation.DEMOGRAPHIC_COMPARISON, Operation.REGIONAL_SPEND, Operation.CHURN_CHANGE},
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
        source_binding_version=binding.version, compiler_version=binding.compiler_version,
        semantic_plan=plan.model_dump_json(), temporal_role=temporal_role,
    )
    return replace(query, _seal=_signature(query))


def semantic_fingerprint(query: CompiledQuery) -> str:
    """Internal equivalence; exclude SQL, physical binding and random seals."""
    from dataclasses import asdict
    import json
    material = asdict(query)
    for key in ('sql', '_seal', 'source_binding_version', 'compiler_version'):
        material.pop(key)
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def execution_fingerprint(query: CompiledQuery) -> str:
    """Never logged: includes SQL, parameters and binding, not random seals."""
    canonical_sql = sqlglot.parse_one(query.sql, read="bigquery").sql(dialect="bigquery")
    material = (canonical_sql, query.parameters)
    return hashlib.sha256(repr(material).encode()).hexdigest()
