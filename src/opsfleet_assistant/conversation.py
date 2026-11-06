"""Versioned, bounded released context. Assistant prose is never evidence."""
from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .contracts import (AnalysisPlan, CohortOrigin, InterpretationIssue, RequestSpec,
                        ResolvedRequest, StrictModel, Mode, Period, Operation, Metric, TemporalAnchor)
from .evidence import Evidence
from .reporting import GroundedReport
from .safety import screen_question


class SafeMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


def request_binding_digest(request: ResolvedRequest, plans: tuple[AnalysisPlan, ...],
                           reference_date: date, previous_period: Period | None,
                           previous_cutoff: date | None,
                           role_anchors: dict[str, TemporalAnchor] | None = None) -> str:
    """Detect corrupted duplicated metadata; authorization is checked separately."""
    payload = {"request": request.model_dump(mode="json"),
               "plans": [p.model_dump(mode="json") for p in plans],
               "reference_date": reference_date.isoformat(),
               "previous_period": previous_period.model_dump(mode="json") if previous_period else None,
               "previous_cutoff": previous_cutoff.isoformat() if previous_cutoff else None}
    if role_anchors:
        payload["role_anchors"] = {role: anchor.model_dump(mode="json") for role, anchor in role_anchors.items()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _pending_temporal_digest(spec: RequestSpec, anchors: dict[str, TemporalAnchor]) -> str:
    payload = {"roles": {role: getattr(spec, role).model_dump(mode="json") if getattr(spec, role) else None
                         for role in ("analysis", "comparison", "action")},
               "anchors": {role: anchor.model_dump(mode="json") for role, anchor in anchors.items()}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class PendingTemporalResolution(StrictModel):
    schema_version: Literal[1] = 1
    anchors: dict[Literal["analysis", "comparison", "action"], TemporalAnchor]
    binding_digest: str = Field(min_length=64, max_length=64)

    @classmethod
    def capture(cls, spec: RequestSpec, anchors: dict[str, TemporalAnchor]):
        return cls(anchors=anchors, binding_digest=_pending_temporal_digest(spec, anchors))

    def matches(self, spec: RequestSpec) -> bool:
        return (set(self.anchors) == {role for role in ("analysis", "comparison", "action") if getattr(spec, role)}
                and self.binding_digest == _pending_temporal_digest(spec, self.anchors))


def request_role_anchors(spec: RequestSpec, *, previous_spec: RequestSpec | None,
                         previous_anchors: dict[str, TemporalAnchor] | None,
                         context_action: str, reference_date: date, previous_plan: AnalysisPlan | None,
                         temporal_updates: tuple[str, ...] = ()
                         ) -> dict[str, TemporalAnchor]:
    """Keep supplied concepts anchored; newly supplied concepts use the reply clock."""
    current = TemporalAnchor.capture(reference_date, previous_plan)
    result = {}
    for role in ("analysis", "comparison", "action"):
        expression = getattr(spec, role)
        if expression is None:
            continue
        explicit_refresh = context_action == "correct" and role in temporal_updates
        unchanged = previous_spec is not None and getattr(previous_spec, role) == expression
        result[role] = (previous_anchors[role]
            if context_action != "new" and unchanged and not explicit_refresh and previous_anchors
            else current)
    return result


class AnalysisSnapshot(StrictModel):
    schema_version: Literal[2] = 2
    created_at: datetime
    plans: tuple[AnalysisPlan, ...] = Field(min_length=1, max_length=2)
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=2)
    request: ResolvedRequest
    grounded: GroundedReport
    report_id: int | None = Field(default=None, ge=1)
    resolution_reference_date: date
    resolution_previous_period: Period | None = None
    resolution_previous_cutoff: date | None = None
    request_binding_digest: str = Field(min_length=64, max_length=64)
    resolution_role_anchors: dict[Literal["analysis", "comparison", "action"], TemporalAnchor] = Field(default_factory=dict)

    def temporal_anchors(self) -> dict[str, TemporalAnchor]:
        """Original inputs for corrections, including compatible v2 global clocks.

        Call only after snapshot semantic validation and current authorization.
        Lifecycle created_at is deliberately absent from analytical resolution.
        """
        original = TemporalAnchor(reference_date=self.resolution_reference_date,
            previous_period=self.resolution_previous_period, previous_cutoff=self.resolution_previous_cutoff)
        return {role: self.resolution_role_anchors.get(role, original)
                for role in ("analysis", "comparison", "action") if getattr(self.request.spec, role) is not None}

    @classmethod
    def capture(cls, *, reference_date: date, previous_plan: AnalysisPlan | None,
                role_anchors: dict[str, TemporalAnchor] | None = None, **values):
        period = previous_plan.period if previous_plan else None
        cutoff = previous_plan.cutoff_date if previous_plan else None
        return cls(**values, resolution_reference_date=reference_date,
                   resolution_previous_period=period, resolution_previous_cutoff=cutoff,
                   resolution_role_anchors=role_anchors or {},
                   request_binding_digest=request_binding_digest(values["request"], values["plans"],
                                                               reference_date, period, cutoff, role_anchors))

    @model_validator(mode="after")
    def bounded(self):
        if (self.created_at.tzinfo is None or len(self.plans) != len(self.evidence)
                or any(p.limit > 20 or (p.operation == "demographic_comparison" and p.limit > 10)
                       or len(e.rows) > min(p.limit, 10 if p.operation == "demographic_comparison" else 20)
                       for p, e in zip(self.plans, self.evidence))):
            raise ValueError("invalid_snapshot")
        if self.request_binding_digest != request_binding_digest(self.request, self.plans,
                self.resolution_reference_date, self.resolution_previous_period, self.resolution_previous_cutoff,
                self.resolution_role_anchors):
            raise ValueError("invalid_request_binding")
        return self


class ConversationContext(StrictModel):
    schema_version: Literal[1] = 1
    owner: str
    identity_digest: str
    catalog_digest: str
    authorization_version: int
    identity_brands: tuple[str, ...]
    identity_role: str
    created_at: datetime
    expires_at: datetime
    cohort_origin: CohortOrigin = CohortOrigin.BUSINESS
    messages: tuple[SafeMessage, ...] = Field(default=(), max_length=20)
    pending: RequestSpec | None = None
    pending_resolution: PendingTemporalResolution | None = None
    issues: tuple[InterpretationIssue, ...] = ()
    snapshot: AnalysisSnapshot | None = None

    @model_validator(mode="after")
    def timestamps(self):
        if (self.created_at.tzinfo is None or self.expires_at.tzinfo is None
                or self.expires_at <= self.created_at
                or self.expires_at - self.created_at > timedelta(minutes=30)):
            raise ValueError("invalid_context_time")
        if self.pending_resolution and (self.pending is None or not self.pending_resolution.matches(self.pending)):
            raise ValueError("invalid_pending_temporal_binding")
        return self


def safe_spec(spec: RequestSpec | None) -> RequestSpec | None:
    """Source phrases are diagnostic, never necessary durable user content."""
    if spec is None:
        return None
    spec = spec.model_copy(update={"sources": tuple(s.model_copy(update={"phrase": None}) for s in spec.sources)})
    for query in spec.queries:
        for value in (*(query.requested_brands or ()), *(query.demographic_values or ())):
            screen_question("Filter " + value)
    return RequestSpec.model_validate(spec.model_dump())


def history(messages: tuple[SafeMessage, ...], *, max_messages: int, max_chars: int) -> tuple[SafeMessage, ...]:
    selected = []
    used = 0
    for message in reversed(messages):
        if len(selected) >= max_messages or used + len(message.content) > max_chars:
            break
        selected.append(message)
        used += len(message.content)
    return tuple(reversed(selected))


def model_context(context: ConversationContext | None) -> dict:
    if context is None:
        return {}
    snapshot = context.snapshot
    return {"messages": [m.model_dump() for m in context.messages],
            "pending_request": context.pending.model_dump(mode="json", exclude_defaults=True, exclude={"sources"}) if context.pending else None,
            "unresolved_fields": [i.value for i in context.issues],
            "pending_temporal_anchors": {role: anchor.model_dump(mode="json") for role, anchor in context.pending_resolution.anchors.items()}
                                        if context.pending_resolution else {},
            "last_request": snapshot.request.spec.model_dump(mode="json", exclude_defaults=True, exclude={"sources"}) if snapshot else None,
            "released_evidence": [{"index": i, "operation": e.operation.value, "columns": e.columns,
                                    "period": e.period_utc, "as_of": e.query_as_of_utc,
                                    "rows": len(e.rows), "complete": e.complete}
                                   for i, e in enumerate(snapshot.evidence)] if snapshot else [],
            "cohort_origin": context.cohort_origin.value}


def validate_cached_evidence(query, item: Evidence) -> None:
    """Reapply the release boundary to deserialized rows, without a warehouse job."""
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace
    from .evidence import release_evidence
    from .identity import PolicyError
    try:
        screen_question("Coverage note " + item.coverage.note)
        if item.coverage.latest_item_at_utc:
            datetime.fromisoformat(item.coverage.latest_item_at_utc)
        rows = []
        for row in item.rows:
            clean = dict(row)
            for column, kind in zip(item.columns, item.column_types):
                value = row[column]
                if value is not None and kind in {"NUMERIC", "BIGNUMERIC", "DECIMAL", "FLOAT", "FLOAT64"} and isinstance(value, str):
                    clean[column] = Decimal(value)
                if value is not None and kind == "DATE":
                    clean[column] = date.fromisoformat(value)
            rows.append(clean)
        restored = release_evidence(query, rows,
            tuple(SimpleNamespace(name=name, field_type=kind) for name, kind in zip(item.columns, item.column_types)),
            job_id=item.job_id, estimated_bytes=item.estimated_bytes, billed_bytes=item.billed_bytes,
            query_as_of=datetime.fromisoformat(item.query_as_of_utc), coverage=item.coverage)
        if restored.model_dump() != item.model_dump():
            raise ValueError("incompatible_released_evidence")
    except (ValueError, KeyError, TypeError, ArithmeticError):
        raise PolicyError("saved_scope_changed") from None


def validate_snapshot_request(snapshot: AnalysisSnapshot) -> None:
    """Bind symbolic/concrete metadata to execution using its original clocks.

    The digest detects independent corruption (including action-only metadata).
    Semantic validation additionally checks the duplicated request against plans,
    allowing only the supported Golden transform and derived investigation plan.
    """
    from .identity import PolicyError
    from .intent import validate_bound_plans
    from .periods import resolve_request, ResolutionFailure
    previous = None
    try:
        if snapshot.resolution_previous_period is not None:
            previous = AnalysisPlan(operation=Operation.AGGREGATE, metric=Metric.REVENUE,
                                    period=snapshot.resolution_previous_period)
        elif snapshot.resolution_previous_cutoff is not None:
            previous = AnalysisPlan(operation=Operation.CHURN_SNAPSHOT, metric=Metric.CHURN_RATE,
                                    cutoff_date=snapshot.resolution_previous_cutoff)
        if (snapshot.resolution_previous_period is not None and snapshot.resolution_previous_cutoff is not None
                or snapshot.request.spec.intent != "analysis"):
            raise ValueError("invalid_resolution_context")
        resolved = resolve_request(snapshot.request.spec,
            reference_date=snapshot.resolution_reference_date,
            analytical_timezone=snapshot.request.analytical_timezone, previous_plan=previous,
            role_anchors=snapshot.resolution_role_anchors)
        if resolved != snapshot.request:
            raise ValueError("inconsistent_resolved_request")
        if snapshot.request.spec.mode == Mode.INVESTIGATE_TOP_PRODUCTS:
            validate_bound_plans(snapshot.plans[:1], snapshot.request)
            ids = tuple(row["product_id"] for row in snapshot.evidence[0].rows)
            if len(ids) == 2:
                base = snapshot.plans[0]
                expected = AnalysisPlan(operation=Operation.PRODUCT_COMPARISON, metric=Metric.REVENUE,
                    period=base.period, requested_brands=base.requested_brands,
                    product_ids=ids, revenue_statuses=base.revenue_statuses)
                if len(snapshot.plans) != 2 or snapshot.plans[1] != expected:
                    raise ValueError("invalid_derived_plan")
            elif len(snapshot.plans) != 1:
                raise ValueError("unexpected_derived_plan")
        else:
            validate_bound_plans(snapshot.plans, snapshot.request,
                effective_query_brands=tuple(e.execution_scope.brands if e.execution_scope else ()
                    for e in snapshot.evidence))
    except (ValueError, KeyError, ResolutionFailure, PolicyError):
        raise PolicyError("saved_scope_changed") from None
