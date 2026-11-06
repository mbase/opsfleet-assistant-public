"""The only model-facing representation of warehouse analysis results."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Mapping, Sequence

from pydantic import Field

from .compiler import CompiledQuery, is_trusted_compiled
from .contracts import Operation, StrictModel
from .identity import PolicyError
from .metrics import MIN_DEMOGRAPHIC_CUSTOMERS
from .privacy import FIELD_POLICY_VERSION, PROHIBITED_SOURCE_FIELDS


_INTEGER = {"INTEGER", "INT64"}
_NUMERIC = {"FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC", "DECIMAL"}
_COLUMN_TYPES: dict[str, frozenset[str]] = {
    "brand": frozenset({"STRING"}),
    "product_id": frozenset(_INTEGER),
    "user_id": frozenset(_INTEGER),
    "month": frozenset({"DATE"}),
    "demographic_value": frozenset({"STRING"}),
    "revenue": frozenset(_NUMERIC),
    "average_order_value": frozenset(_NUMERIC),
    "customer_spend": frozenset(_NUMERIC),
    "average_customer_spend": frozenset(_NUMERIC),
    "average_item_price": frozenset(_NUMERIC),
    "churn_rate": frozenset(_NUMERIC),
    "order_count": frozenset(_INTEGER),
    "customer_count": frozenset(_INTEGER),
    "customers": frozenset(_INTEGER),
    "item_count": frozenset(_INTEGER),
    "eligible_customers": frozenset(_INTEGER),
    "inactive_customers": frozenset(_INTEGER),
}
_TEXT_PII = re.compile(r"@|\b\+?\d[\d ()-]{7,}\d\b|\b\d{5}(?:-\d{4})?\b")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class Coverage(StrictModel):
    status: str
    source_rows: int | None = None
    period_rows: int | None = None
    latest_item_at_utc: str | None = None
    note: str
    job_id: str | None = None
    estimated_bytes: int | None = None
    billed_bytes: int | None = None


class ExecutionScope(StrictModel):
    """Canonical authorized filters copied from a sealed compiled query."""

    brands: tuple[str, ...] = Field(min_length=1)
    product_ids: tuple[int, ...] | None = None


class Evidence(StrictModel):
    operation: Operation
    columns: tuple[str, ...]
    column_types: tuple[str, ...]
    rows: tuple[dict[str, int | float | str | None], ...]
    complete: bool
    completeness_note: str
    metric_definition: dict[str, Any]
    field_policy_version: int
    scope_reference: str
    scope_version: int
    scope_brand_count: int
    # None means unavailable legacy metadata, not an unrestricted query.
    execution_scope: ExecutionScope | None = None
    period_utc: dict[str, str]
    query_as_of_utc: str
    data_as_of_note: str
    job_id: str
    estimated_bytes: int = Field(ge=0)
    billed_bytes: int = Field(ge=0)
    coverage: Coverage


def _safe_text(value: str) -> str:
    if not value or len(value) > 100 or _TEXT_PII.search(value):
        raise PolicyError("unsafe_result_text")
    return value


def _normal_value(name: str, value: Any, kind: str, query: CompiledQuery) -> int | float | str | None:
    if value is None:
        if name in {"average_order_value", "churn_rate"}:
            return None
        raise PolicyError("result_null_violation")
    if kind in _INTEGER:
        if type(value) is not int or value < 0:
            raise PolicyError("result_type_violation")
        if name in {"product_id", "user_id"} and value == 0:
            raise PolicyError("result_type_violation")
        if name == "product_id" and query.requested_product_ids is not None and value not in query.requested_product_ids:
            raise PolicyError("result_scope_violation")
        return value
    if kind in _NUMERIC:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise PolicyError("result_type_violation")
        try:
            finite = math.isfinite(float(value))
            nonnegative = value >= 0
        except (OverflowError, ValueError, InvalidOperation):
            raise PolicyError("result_type_violation") from None
        if not finite or not nonnegative:
            raise PolicyError("result_type_violation")
        if name == "churn_rate" and value > 1:
            raise PolicyError("result_type_violation")
        return str(value) if isinstance(value, Decimal) else value
    if kind == "DATE":
        if type(value) is not date:
            raise PolicyError("result_type_violation")
        return value.isoformat()
    if kind == "STRING":
        if not isinstance(value, str):
            raise PolicyError("result_type_violation")
        if name == "brand" and value not in query.effective_brands:
            raise PolicyError("result_scope_violation")
        if name == "demographic_value" and query.requested_demographic_values is not None and value not in query.requested_demographic_values:
            raise PolicyError("result_scope_violation")
        return _safe_text(value)
    raise PolicyError("result_type_violation")


def release_evidence(
    query: CompiledQuery,
    rows: Sequence[Mapping[str, Any]],
    schema: Sequence[Any],
    *,
    job_id: str,
    estimated_bytes: int,
    billed_bytes: int,
    query_as_of: datetime,
    coverage: Coverage,
) -> Evidence:
    """Validate warehouse data before allocating any model-visible object."""
    if not is_trusted_compiled(query) or query.field_policy_version != FIELD_POLICY_VERSION:
        raise PolicyError("untrusted_compiled_query")
    columns = tuple(field.name for field in schema)
    kinds = tuple(field.field_type.upper() for field in schema)
    if columns != query.result_contract.columns or len(set(columns)) != len(columns):
        raise PolicyError("result_schema_violation")
    if any(name in PROHIBITED_SOURCE_FIELDS or kind not in _COLUMN_TYPES.get(name, ())
           for name, kind in zip(columns, kinds)):
        raise PolicyError("result_type_violation")
    query.result_contract.validate_rows(rows)
    if not rows and query.operation in {Operation.AGGREGATE, Operation.CHURN_SNAPSHOT} and len(columns) == 1:
        raise PolicyError("result_empty_violation")
    if query.operation in {Operation.AGGREGATE, Operation.CHURN_SNAPSHOT} and query.result_contract.max_rows == 1 and len(rows) != 1:
        raise PolicyError("result_empty_violation")

    clean: list[dict[str, int | float | str | None]] = []
    for row in rows:
        clean.append({name: _normal_value(name, row[name], kind, query)
                      for name, kind in zip(columns, kinds)})
    if query.operation == Operation.PRODUCT_COMPARISON:
        if not clean and coverage.period_rows == 0:
            pass  # Genuine empty source: do not manufacture two requested products.
        elif len(clean) != 2 or {row["product_id"] for row in clean} != set(query.requested_product_ids or ()):
            raise PolicyError("incomplete_comparison")
    if query.operation == Operation.DEMOGRAPHIC_COMPARISON:
        if any(row["customers"] < MIN_DEMOGRAPHIC_CUSTOMERS for row in clean):
            raise PolicyError("demographic_group_too_small")
        if query.requested_demographic_values is not None:
            if len(clean) != 2 or {row["demographic_value"] for row in clean} != set(query.requested_demographic_values):
                raise PolicyError("incomplete_comparison")
        elif len(clean) == query.result_contract.max_rows:
            raise PolicyError("result_limit")
    if query.operation == Operation.CHURN_SNAPSHOT and clean:
        if clean[0]["inactive_customers"] > clean[0]["eligible_customers"]:
            raise PolicyError("result_type_violation")
        if (clean[0]["eligible_customers"] == 0) != (clean[0]["churn_rate"] is None):
            raise PolicyError("result_null_violation")
    if query.operation == Operation.AGGREGATE and len(columns) == 1 and clean and clean[0][columns[0]] is None:
        note = "No qualifying rows; the metric is undefined."
    elif not clean:
        note = "No qualifying rows."
    elif query.operation in {Operation.RANK_CUSTOMERS, Operation.TIME_SERIES, Operation.AGGREGATE} and len(clean) == query.result_contract.max_rows and query.result_contract.max_rows > 1:
        note = "The result reached its row limit; more rows may exist."
    else:
        note = "All requested groups returned within the row limit."
    complete = note == "All requested groups returned within the row limit."
    if query.operation in {Operation.AGGREGATE, Operation.CHURN_SNAPSHOT} and len(columns) == 1 and clean and clean[0][columns[0]] is not None:
        complete, note = True, "Aggregate result returned."
    if query.operation == Operation.CHURN_SNAPSHOT and clean:
        complete = clean[0]["eligible_customers"] > 0
        note = "Churn cohort returned." if complete else "No prior purchasers; churn rate is undefined."
    if query.operation == Operation.AGGREGATE and query.result_contract.max_rows == 1 and len(columns) > 1 and clean:
        complete, note = False, "The result reached its row limit; more rows may exist."
    if coverage.period_rows == 0:
        complete, note = False, "No qualifying scoped items in the requested UTC period or cutoff population."
    elif any(row.get("average_order_value", 0) is None for row in clean):
        raise PolicyError("result_null_violation")

    if (query_as_of.tzinfo is None or not _SAFE_ID.fullmatch(query.scope_user_id)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", job_id)
            or coverage.status != "checked" or coverage.job_id is None
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", coverage.job_id)
            or estimated_bytes < 0 or billed_bytes < 0):
        raise PolicyError("invalid_job_metadata")
    if query.operation == Operation.DEMOGRAPHIC_COMPARISON:
        if coverage.source_rows is not None or coverage.period_rows is not None:
            raise PolicyError("demographic_complement_violation")
    elif (type(coverage.source_rows) is not int or type(coverage.period_rows) is not int
          or coverage.source_rows < coverage.period_rows or coverage.period_rows < 0):
        raise PolicyError("invalid_job_metadata")
    period = {key: str(value) for key, value in query.metric_definition.items()
              if key in {"period_start", "period_end", "cutoff_date"} and value is not None}
    return Evidence(
        operation=query.operation, columns=columns, column_types=kinds, rows=tuple(clean),
        complete=complete, completeness_note=note,
        metric_definition=dict(query.metric_definition),
        field_policy_version=query.field_policy_version,
        scope_reference=f"demo:{query.scope_user_id}", scope_version=query.scope_version,
        scope_brand_count=len(query.effective_brands), period_utc=period,
        execution_scope=ExecutionScope(
            brands=tuple(_safe_text(brand) for brand in query.effective_brands),
            product_ids=query.requested_product_ids,
        ),
        query_as_of_utc=query_as_of.astimezone(timezone.utc).isoformat(),
        data_as_of_note="Query completion time is not source freshness; see scoped coverage.",
        job_id=job_id, estimated_bytes=estimated_bytes, billed_bytes=billed_bytes,
        coverage=coverage,
    )
