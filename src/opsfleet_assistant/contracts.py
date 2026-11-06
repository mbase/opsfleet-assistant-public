"""Strict analytical request contracts. Model output never supplies identity or SQL."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Role(StrEnum):
    MANAGER = "manager"
    CEO = "ceo"


class CohortOrigin(StrEnum):
    BUSINESS = "business"
    DEMOGRAPHIC = "demographic"


class AnalysisContext(StrictModel):
    """Trusted conversation state; model plans cannot assert cohort provenance."""

    inherited_cohort: CohortOrigin | None = None


class Operation(StrEnum):
    AGGREGATE = "aggregate"
    RANK_CUSTOMERS = "rank_customers"
    TIME_SERIES = "time_series"
    PRODUCT_COMPARISON = "product_comparison"
    DEMOGRAPHIC_COMPARISON = "demographic_comparison"
    CHURN_SNAPSHOT = "churn_snapshot"


class Metric(StrEnum):
    REVENUE = "revenue"
    ORDER_COUNT = "order_count"
    AVERAGE_ORDER_VALUE = "average_order_value"
    CUSTOMER_COUNT = "customer_count"
    CUSTOMER_SPEND = "customer_spend"
    AVERAGE_CUSTOMER_SPEND = "average_customer_spend"
    CHURN_RATE = "churn_rate"


class Dimension(StrEnum):
    BRAND = "brand"
    PRODUCT = "product"
    CITY = "city"
    STATE = "state"
    COUNTRY = "country"
    AGE_BAND = "age_band"
    GENDER = "gender"
    TRAFFIC_SOURCE = "traffic_source"


class ItemStatus(StrEnum):
    SHIPPED = "Shipped"
    COMPLETE = "Complete"
    PROCESSING = "Processing"
    CANCELLED = "Cancelled"
    RETURNED = "Returned"


DEFAULT_STATUSES = (
    ItemStatus.SHIPPED,
    ItemStatus.COMPLETE,
    ItemStatus.PROCESSING,
)
DEMOGRAPHIC_DIMENSIONS = frozenset({
    Dimension.CITY, Dimension.STATE, Dimension.COUNTRY, Dimension.AGE_BAND,
    Dimension.GENDER, Dimension.TRAFFIC_SOURCE,
})


class Period(StrictModel):
    start: date
    end: date

    @model_validator(mode="after")
    def valid_range(self) -> "Period":
        if self.start >= self.end:
            raise ValueError("period_start_must_precede_end")
        return self


class TemporalAnchor(StrictModel):
    """Application-owned inputs for one temporal role; never model output."""
    reference_date: date
    previous_period: Period | None = None
    previous_cutoff: date | None = None

    @model_validator(mode="after")
    def one_previous(self):
        if self.previous_period is not None and self.previous_cutoff is not None:
            raise ValueError("conflicting_previous_temporal_inputs")
        return self

    @classmethod
    def capture(cls, reference_date: date, previous_plan=None):
        return cls(reference_date=reference_date,
                   previous_period=previous_plan.period if previous_plan else None,
                   previous_cutoff=previous_plan.cutoff_date if previous_plan else None)


class BrandScope(StrictModel):
    """Trusted application input created from a local identity record, never model output."""

    user_id: str = Field(min_length=1, max_length=100)
    role: Role
    brands: tuple[str, ...]
    authorization_version: int = Field(ge=1)


class AnalysisPlan(StrictModel):
    version: int = Field(default=1, ge=1, le=1)
    operation: Operation
    metric: Metric
    period: Period | None = None
    cutoff_date: date | None = None
    dimension: Dimension | None = None
    requested_brands: tuple[str, ...] | None = None
    product_ids: tuple[int, ...] | None = None
    demographic_values: tuple[str, ...] | None = None
    revenue_statuses: tuple[ItemStatus, ...] = DEFAULT_STATUSES
    inactivity_days: int = Field(default=90, ge=1, le=365)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def valid_operation(self) -> "AnalysisPlan":
        if not self.revenue_statuses or len(set(self.revenue_statuses)) != len(self.revenue_statuses):
            raise ValueError("invalid_status_set")
        if self.requested_brands is not None:
            if not self.requested_brands or any(not b.strip() for b in self.requested_brands):
                raise ValueError("invalid_brand_request")
            if len({b.casefold() for b in self.requested_brands}) != len(self.requested_brands):
                raise ValueError("duplicate_brand_request")
        if self.product_ids is not None:
            if not self.product_ids or len(self.product_ids) > 10:
                raise ValueError("invalid_product_count")
            if any(type(p) is not int or p <= 0 for p in self.product_ids):
                raise ValueError("invalid_product_id")
            if len(set(self.product_ids)) != len(self.product_ids):
                raise ValueError("duplicate_product_id")
        if self.demographic_values is not None:
            if not self.demographic_values or len(self.demographic_values) != 2:
                raise ValueError("invalid_demographic_comparison")
            if any(not value.strip() or len(value) > 100 for value in self.demographic_values):
                raise ValueError("invalid_demographic_value")
            if len(set(self.demographic_values)) != 2:
                raise ValueError("duplicate_demographic_value")

        if self.operation == Operation.CHURN_SNAPSHOT:
            if (self.metric != Metric.CHURN_RATE or self.cutoff_date is None or
                    self.period is not None or self.dimension is not None or
                    self.product_ids is not None or self.demographic_values is not None):
                raise ValueError("invalid_churn_plan")
            return self
        if self.period is None or self.cutoff_date is not None or self.inactivity_days != 90:
            raise ValueError("invalid_period_or_churn_override")
        if self.operation == Operation.AGGREGATE:
            if (self.metric not in {Metric.REVENUE, Metric.ORDER_COUNT,
                                    Metric.AVERAGE_ORDER_VALUE, Metric.CUSTOMER_COUNT}
                    or self.dimension not in {None, Dimension.BRAND, Dimension.PRODUCT}
                    or self.demographic_values is not None):
                raise ValueError("invalid_aggregate_plan")
        elif self.operation == Operation.TIME_SERIES:
            if (self.metric not in {Metric.REVENUE, Metric.ORDER_COUNT,
                                    Metric.AVERAGE_ORDER_VALUE, Metric.CUSTOMER_COUNT}
                    or self.dimension is not None or self.demographic_values is not None):
                raise ValueError("invalid_time_series_plan")
        elif self.operation == Operation.RANK_CUSTOMERS:
            if (self.metric != Metric.CUSTOMER_SPEND or self.dimension is not None
                    or self.demographic_values is not None):
                raise ValueError("invalid_customer_rank_plan")
        elif self.operation == Operation.PRODUCT_COMPARISON:
            if (self.metric != Metric.REVENUE or self.dimension is not None
                    or self.product_ids is None or len(self.product_ids) != 2
                    or self.demographic_values is not None):
                raise ValueError("invalid_product_comparison_plan")
        elif self.operation == Operation.DEMOGRAPHIC_COMPARISON:
            if self.metric != Metric.AVERAGE_CUSTOMER_SPEND or self.dimension not in DEMOGRAPHIC_DIMENSIONS:
                raise ValueError("invalid_demographic_plan")
        return self


class Mode(StrEnum):
    ANALYSIS = "analysis"
    REPORT = "report"
    INVESTIGATE_TOP_PRODUCTS = "investigate_top_products"
    CHURN_COMPARE = "churn_compare"
    TEMPORAL_COMPARE = "temporal_compare"


class TemporalRole(StrEnum):
    ANALYSIS = "analysis"
    COMPARISON = "comparison"
    ACTION = "action"
    PREVIOUS = "previous"


class YearExpression(StrictModel):
    kind: Literal["absolute", "relative", "context"]
    value: int | None = Field(default=None, strict=True, ge=1, le=9998)
    offset: int | None = Field(default=None, strict=True, ge=-100, le=100)

    @model_validator(mode="after")
    def supported_fields(self) -> "YearExpression":
        if ((self.kind == "absolute" and (self.value is None or self.offset is not None))
                or (self.kind == "relative" and (self.offset is None or self.value is not None))
                or (self.kind == "context" and (self.value is not None or self.offset is not None))):
            raise ValueError("invalid_year_expression")
        return self


class TemporalExpression(StrictModel):
    """A calendar concept, never a natural-language parser or a trusted clock.

    Unused fields must be null. A calendar year may be absent so that trusted
    resolution can return a normal missing-year clarification.
    """

    kind: Literal["month", "quarter", "year", "interval", "window", "to_date", "reference"]
    year: YearExpression | None = None
    month: int | None = Field(default=None, strict=True, ge=1, le=12)
    quarter: int | None = Field(default=None, strict=True, ge=1, le=4)
    start: date | None = None
    end: date | None = None
    unit: Literal["month", "quarter", "year", "period"] | None = None
    count: int | None = Field(default=None, strict=True, ge=1, le=120)
    offset: int | None = Field(default=None, strict=True, ge=-120, le=120)
    include_current: bool | None = Field(default=None, strict=True)
    reference: TemporalRole | None = None

    @model_validator(mode="after")
    def supported_fields(self) -> "TemporalExpression":
        allowed = {
            "month": {"year", "month"}, "quarter": {"year", "quarter"}, "year": {"year"},
            "interval": {"start", "end"},
            "window": {"unit", "count", "offset", "include_current"},
            "to_date": {"unit"}, "reference": {"reference", "unit", "offset"},
        }[self.kind]
        present = {name for name in type(self).model_fields if name != "kind" and getattr(self, name) is not None}
        required = allowed - ({"year"} if self.kind in {"month", "quarter", "year"} else set())
        if present - allowed or required - present:
            raise ValueError("invalid_temporal_fields")
        if self.kind in {"window", "to_date"} and self.unit == "period":
            raise ValueError("invalid_calendar_unit")
        if self.kind == "interval" and self.start >= self.end:
            raise ValueError("period_start_must_precede_end")
        return self


class QuerySpec(StrictModel):
    """Bounded analytical choices; dates and authority are supplied elsewhere."""

    operation: Operation
    metric: Metric
    temporal_role: Literal["analysis", "comparison"] = "analysis"
    dimension: Dimension | None = None
    requested_brands: tuple[str, ...] | None = Field(default=None, max_length=20)
    product_ids: tuple[int, ...] | None = Field(default=None, max_length=10)
    demographic_values: tuple[str, ...] | None = Field(default=None, max_length=2)
    revenue_statuses: tuple[ItemStatus, ...] = DEFAULT_STATUSES
    inactivity_days: int = Field(default=90, ge=1, le=365)
    limit: int = Field(default=20, ge=1, le=20)


class FieldSource(StrictModel):
    field: Literal["metric", "analysis", "comparison", "action", "scope", "statuses"]
    origin: Literal["explicit", "context", "default"]
    phrase: str | None = Field(default=None, max_length=120)


ReportSection = Literal["summary", "performance_table", "trend", "comparison", "limitations", "recommendations"]


class ReportSpec(StrictModel):
    """Bounded presentation proposal. Temporal roles live only on RequestSpec.

    The trusted composition binds this spec to one ResolvedRequest and adds
    evidence references after execution; the model cannot invent observations.
    """
    purpose: Literal["executive", "performance", "trend", "comparison", "causal_investigation"] = "executive"
    requested_sections: tuple[ReportSection, ...] = Field(
        default=("summary", "performance_table", "limitations", "recommendations"), min_length=1, max_length=6)
    presentation: Literal["narrative", "table", "briefing"] = "briefing"
    detail: Literal["concise", "standard", "detailed"] = "standard"

    @model_validator(mode="after")
    def unique_sections(self):
        if len(set(self.requested_sections)) != len(self.requested_sections):
            raise ValueError("duplicate_report_section")
        return self


class RequestSpec(StrictModel):
    intent: Literal["analysis", "schema_help", "explain", "why", "summary", "save", "cancel"] = "analysis"
    mode: Mode = Mode.ANALYSIS
    purpose: Literal["standard", "trend"] = "standard"
    queries: tuple[QuerySpec, ...] = Field(default=(), max_length=2)
    analysis: TemporalExpression | None = None
    comparison: TemporalExpression | None = None
    action: TemporalExpression | None = None
    report: ReportSpec | None = None
    sources: tuple[FieldSource, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def valid_help_request(self) -> "RequestSpec":
        if self.intent == "schema_help" and (self.queries or self.analysis or self.comparison or self.action
                                              or self.mode != Mode.ANALYSIS or self.purpose != "standard" or self.report is not None):
            raise ValueError("conflicting_help_request")
        return self


class InterpretationIssue(StrEnum):
    MISSING_PERIOD = "missing_period"
    MISSING_YEAR = "missing_year"
    MISSING_METRIC = "missing_metric"
    MISSING_PRODUCTS = "missing_products"
    AMBIGUOUS_METRIC = "ambiguous_metric"
    CONFLICTING_PERIODS = "conflicting_periods"
    UNRESOLVED = "unresolved_interpretation"
    UNSUPPORTED_TEMPORAL = "unsupported_temporal"
    UNSUPPORTED = "unsupported_capability"


# A typed wire proposal; context-dependent metadata is reconciled before use.
class InterpretationProposal(StrictModel):
    # Null under a correction means omission unless the field is explicitly
    # cleared. For report this resets to mode defaults; for action it removes
    # the horizon. Non-null replacement values remain explicit typed changes.
    clear_fields: tuple[Literal["report", "action"], ...] = Field(default=(), max_length=2)
    temporal_updates: tuple[Literal["analysis", "comparison", "action"], ...] = Field(default=(), max_length=3)
    context_action: Literal["new", "continue", "correct", "cancel"] = "new"
    status: Literal["ready", "needs_clarification", "unsupported"]
    request: RequestSpec | None = None
    issues: tuple[InterpretationIssue, ...] = Field(default=(), max_length=5)


# Final interpretation, with the original cross-field invariants enforced.
class InterpretationResult(InterpretationProposal):

    @model_validator(mode="after")
    def valid_outcome(self) -> "InterpretationResult":
        if (len(set(self.clear_fields)) != len(self.clear_fields)
                or self.clear_fields and (self.context_action != "correct" or self.request is None)
                or self.request and any(getattr(self.request, name) is not None for name in self.clear_fields)):
            raise ValueError("invalid_request_clear")
        if (len(set(self.temporal_updates)) != len(self.temporal_updates)
                or self.temporal_updates and self.context_action not in {"new", "correct"}):
            raise ValueError("invalid_temporal_updates")
        if self.status == "ready" and (self.request is None or self.issues):
            raise ValueError("invalid_ready_interpretation")
        if self.status != "ready" and not self.issues:
            raise ValueError("missing_interpretation_issue")
        return self


class ResolvedRequest(StrictModel):
    spec: RequestSpec
    analysis: Period
    comparison: Period | None = None
    action: Period | None = None
    analytical_timezone: Literal["UTC"] = "UTC"


# Factual output is a selection of calculations, never model-written prose.
FindingMetric = Literal[
    "revenue", "order_count", "average_order_value", "customer_count",
    "customer_spend", "average_customer_spend", "churn_rate", "item_count",
    "average_item_price", "customers", "eligible_customers", "inactive_customers",
]


class ObservationRef(StrictModel):
    evidence_index: int = Field(strict=True, ge=0, le=1)
    row_index: int = Field(strict=True, ge=0, le=19)
    metric: FindingMetric


class FindingSelection(StrictModel):
    operation: Literal["observed_value", "rank", "difference", "percentage_change", "percentage_point_change"]
    current: ObservationRef
    baseline: ObservationRef | None = None
    order: Literal["ascending", "descending"] = "descending"

    @model_validator(mode="after")
    def valid_operands(self) -> "FindingSelection":
        paired = self.operation in {"difference", "percentage_change", "percentage_point_change"}
        if paired != (self.baseline is not None) or (self.operation != "rank" and self.order != "descending"):
            raise ValueError("invalid_finding_operands")
        return self


class ActionSelection(StrictModel):
    kind: Literal["review_performance", "investigate_price_volume", "review_retention", "review_cohort_mix", "review_trend"]
    finding_index: int = Field(strict=True, ge=0, le=19)


class ReportDraft(StrictModel):
    status: Literal["selected", "refused"] = "selected"
    findings: tuple[FindingSelection, ...] = Field(default=(), max_length=20)
    headline_index: int | None = Field(default=None, strict=True, ge=0, le=19)
    recommendations: tuple[ActionSelection, ...] = Field(default=(), max_length=3)
    hypotheses: tuple[ActionSelection, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def valid_selection(self) -> "ReportDraft":
        if self.status == "refused" and (self.findings or self.recommendations or self.hypotheses or self.headline_index is not None):
            raise ValueError("invalid_refusal_selection")
        return self
