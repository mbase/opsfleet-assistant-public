"""Evidence-linked calculations and trusted text shared by answers and reports.

Only released Evidence is accepted here. This is not another authorization layer:
callers must reauthorize evidence before reuse (durable reuse belongs to W2).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .contracts import (ActionSelection, FindingSelection, ObservationRef,
                        Operation, ReportDraft, StrictModel, ReportSpec, ReportSection, ResolvedRequest, RequestSpec)
from .evidence import Evidence
from .identity import PolicyError
from .safety import screen_model_prose


class GroundedFinding(StrictModel):
    selection: FindingSelection
    value: Decimal | None
    current_value: Decimal | None
    baseline_value: Decimal | None = None
    absolute_change: Decimal | None = None
    rank: int | None = None
    tied: bool = False
    text: str


class GroundedSuggestion(StrictModel):
    selection: ActionSelection
    text: str


class GroundedReport(StrictModel):
    schema_version: Literal[1] = 1
    factual_status: Literal["grounded", "evidence_only"]
    selection_status: Literal["selected", "fallback", "refused"]
    headline: str
    findings: tuple[GroundedFinding, ...] = ()
    recommendations: tuple[GroundedSuggestion, ...] = ()
    hypotheses: tuple[GroundedSuggestion, ...] = ()


_GROUPS = ("brand", "product_id", "user_id", "month", "demographic_value")
_COUNTS = {"order_count", "customer_count", "item_count", "customers", "eligible_customers", "inactive_customers"}
_SUPPORT = {
    Operation.PRODUCT_COMPARISON: {"revenue", "item_count", "order_count", "average_item_price"},
    Operation.DEMOGRAPHIC_COMPARISON: {"average_customer_spend", "customers", "revenue"},
    Operation.CHURN_SNAPSHOT: {"churn_rate", "eligible_customers", "inactive_customers"},
}


def _reject(code: str = "unsupported_finding") -> None:
    raise PolicyError(code)


def _observation(ref: ObservationRef, evidence: tuple[Evidence, ...]):
    if ref.evidence_index >= len(evidence):
        _reject("invalid_finding_reference")
    item = evidence[ref.evidence_index]
    if ref.row_index >= len(item.rows):
        _reject("invalid_finding_reference")
    row = item.rows[ref.row_index]
    allowed = _SUPPORT.get(item.operation, {item.metric_definition.get("metric")})
    if ref.metric not in allowed or ref.metric not in item.columns or ref.metric not in row:
        _reject("invalid_finding_metric")
    if item.coverage.status != "checked" or item.coverage.period_rows == 0:
        _reject("unobserved_finding")
    value = row[ref.metric]
    if value is None:
        return item, row, None
    if isinstance(value, bool):
        _reject("invalid_finding_value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        _reject("invalid_finding_value")
    if not number.is_finite() or number < 0:
        _reject("invalid_finding_value")
    return item, row, number


def _label(item: Evidence, row: dict, metric: str) -> str:
    labels = [f"{key} {row[key]}" for key in _GROUPS if key in row]
    return (", ".join(labels) + " " if labels else "") + metric.replace("_", " ")


def _display(number: Decimal | None, metric: str = "") -> str:
    if number is None:
        return "unavailable"
    with localcontext() as context:
        context.prec = max(100, len(number.as_tuple().digits) + abs(number.adjusted()) + 10)
        return str(number.quantize(Decimal("1") if metric in _COUNTS else Decimal("0.01")))


def _timing(item: Evidence, row: dict) -> str:
    if item.operation == Operation.CHURN_SNAPSHOT:
        cutoff = item.period_utc.get("cutoff_date")
        days = item.metric_definition.get("inactivity_days")
        if not cutoff or type(days) is not int or days < 1:
            _reject("invalid_finding_period")
        threshold = date.fromisoformat(cutoff) - timedelta(days=days)
        return (f"at cutoff {cutoff} UTC; inactive means last qualifying purchase at or before {threshold} UTC "
                f"({days} days); {row['inactive_customers']} inactive / {row['eligible_customers']} prior purchasers")
    start, end = item.period_utc.get("period_start"), item.period_utc.get("period_end")
    if not start or not end or date.fromisoformat(start) >= date.fromisoformat(end):
        _reject("invalid_finding_period")
    return f"UTC [{start}, {end})"


def _compatible(a: Evidence, ar: dict, b: Evidence, br: dict, same: bool) -> None:
    if not a.complete or not b.complete:
        _reject("incomplete_finding_comparison")
    if same:
        if a.operation == Operation.TIME_SERIES and ar.get("month", "") <= br.get("month", ""):
            _reject("invalid_finding_period")
        return  # Both groups were released by the same authorized query.
    if (a.execution_scope is None or b.execution_scope is None
            or a.scope_reference != b.scope_reference or a.scope_version != b.scope_version
            or a.field_policy_version != b.field_policy_version
            or set(a.execution_scope.brands) != set(b.execution_scope.brands)
            or set(a.execution_scope.product_ids or ()) != set(b.execution_scope.product_ids or ())
            or (a.execution_scope.product_ids is None) != (b.execution_scope.product_ids is None)
            or a.operation != b.operation or a.columns != b.columns):
        _reject("incompatible_finding_scope")
    ignore = {"period_start", "period_end", "cutoff_date"}
    definitions = [{k: v for k, v in x.metric_definition.items() if k not in ignore} for x in (a, b)]
    for definition in definitions:
        # Status predicates are sets. Preserve original serialized definitions
        # for exact replay/disclosure, but do not confuse order with meaning.
        if "revenue_statuses" in definition:
            definition["revenue_statuses"] = sorted(definition["revenue_statuses"])
    if definitions[0] != definitions[1]:
        _reject("incompatible_finding_definition")
    # Legacy definitions omit the demographic dimension/cohort filter. Do not
    # infer compatibility from equal-looking group names in different queries.
    if a.operation == Operation.DEMOGRAPHIC_COMPARISON:
        _reject("incompatible_finding_population")
    if any(ar.get(k) != br.get(k) for k in _GROUPS):
        _reject("incompatible_finding_population")
    if a.period_utc == b.period_utc:
        return
    if a.operation == Operation.CHURN_SNAPSHOT:
        if a.period_utc.get("cutoff_date", "") <= b.period_utc.get("cutoff_date", ""):
            _reject("invalid_finding_period")
    elif a.period_utc.get("period_start", "") < b.period_utc.get("period_end", ""):
        _reject("invalid_finding_period")


def calculate(selection: FindingSelection, evidence: tuple[Evidence, ...]) -> GroundedFinding:
    """Compute a selected claim. Current minus baseline defines every delta."""
    selection = FindingSelection.model_validate(selection.model_dump())
    current = selection.current
    a, ar, av = _observation(current, evidence)
    label = _label(a, ar, current.metric)
    timing = _timing(a, ar)
    refs = f"E{current.evidence_index + 1} row {current.row_index + 1}"
    value, baseline, delta, rank, tied = av, None, None, None, False
    if selection.operation == "observed_value":
        with localcontext() as context:
            context.prec = 100
            shown = _display(av * 100 if av is not None and current.metric == "churn_rate" else av, current.metric)
        text = f"Observed {label}: {shown}{'%' if current.metric == 'churn_rate' and av is not None else ''}; {timing}."
    elif selection.operation == "rank":
        if av is None or not any(k in ar for k in _GROUPS) or a.operation == Operation.CHURN_SNAPSHOT:
            _reject("unsupported_finding_rank")
        values = [_observation(current.model_copy(update={"row_index": i}), evidence)[2] for i in range(len(a.rows))]
        if any(v is None for v in values):
            _reject("unsupported_finding_rank")
        rank = 1 + sum(v > av if selection.order == "descending" else v < av for v in values)
        tied = values.count(av) > 1
        population = "all released groups" if a.complete else "returned groups only; result incomplete, no global rank asserted"
        text = (f"{label}: rank {rank}{' (tied)' if tied else ''} by {selection.order} value "
                f"among {population}; {timing}.")
        value = Decimal(rank)
    else:
        assert selection.baseline is not None
        b, br, baseline = _observation(selection.baseline, evidence)
        if current.metric != selection.baseline.metric:
            _reject("incompatible_finding_metric")
        _compatible(a, ar, b, br, current.evidence_index == selection.baseline.evidence_index)
        baseline_label = _label(b, br, current.metric)
        baseline_timing = _timing(b, br)
        refs += f"; E{selection.baseline.evidence_index + 1} row {selection.baseline.row_index + 1}"
        with localcontext() as context:
            context.prec = 100
            delta = av - baseline if av is not None and baseline is not None else None
            value = delta
            if selection.operation == "percentage_change":
                value = delta / baseline * 100 if delta is not None and baseline != 0 else None
            elif selection.operation == "percentage_point_change":
                if current.metric != "churn_rate":
                    _reject("incompatible_finding_metric")
                value = delta * 100 if delta is not None else None
        text = (f"Trusted comparison: {label} minus {baseline_label} is {_display(delta, current.metric)} "
                f"(current {timing}; baseline {baseline_timing}).")
        if selection.operation == "percentage_change":
            text += (f" Percentage change: {_display(value)}%." if value is not None else
                     " Percentage change unavailable: baseline is zero, null or missing, or current value is undefined.")
        elif selection.operation == "percentage_point_change":
            direction = (f"rose by {_display(value)} percentage points" if value is not None and value > 0 else
                         f"fell by {_display(-value)} percentage points" if value is not None and value < 0 else
                         "was unchanged" if value is not None else "change unavailable")
            text += f" Churn {direction}. " + ("The claimed spike is not supported." if value is not None and value <= 0 else
                                             "The increase alone does not establish why it happened." if value is not None else
                                             "No change is asserted from undefined rates.")
    text += f" [{refs}]"
    return GroundedFinding(selection=selection, value=value, current_value=av, baseline_value=baseline,
                           absolute_change=delta, rank=rank, tied=tied, text=text)


_ACTIONS = {
    "review_performance": "Review performance against business targets before choosing an intervention.",
    "investigate_price_volume": "Review qualifying item counts and average item prices to investigate the product revenue difference; these observations do not establish a cause.",
    "review_retention": "Review retention patterns in the defined prior-purchaser population; inactivity alone does not establish a cause.",
    "review_cohort_mix": "Review aggregate cohort mix to investigate the spend difference; this does not establish a demographic cause.",
    "review_trend": "Review the observed monthly pattern against business events before choosing an intervention.",
}
_HYPOTHESES = {
    "investigate_price_volume": "Price and volume may contribute to the product revenue difference; test this, rather than treating it as a causal finding.",
    "review_retention": "Retention factors may contribute to inactivity; test this in the defined population before attributing a cause.",
    "review_cohort_mix": "Cohort mix may contribute to the average spend difference; this remains an untested explanation.",
}


def _suggestion(selection: ActionSelection, findings: tuple[GroundedFinding, ...], evidence: tuple[Evidence, ...], *, hypothesis: bool) -> GroundedSuggestion:
    if selection.finding_index >= len(findings):
        _reject("unsupported_recommendation")
    finding = findings[selection.finding_index]
    ref = finding.selection.current
    item = evidence[ref.evidence_index]
    compared = finding.selection.baseline is not None and finding.absolute_change is not None and finding.absolute_change != 0
    relevant = {
        "review_performance": ref.metric in {"revenue", "order_count", "average_order_value", "customer_spend", "customer_count"},
        "investigate_price_volume": item.operation == Operation.PRODUCT_COMPARISON and ref.metric == "revenue" and compared,
        "review_retention": item.operation == Operation.CHURN_SNAPSHOT and ref.metric == "churn_rate",
        "review_cohort_mix": item.operation == Operation.DEMOGRAPHIC_COMPARISON and ref.metric == "average_customer_spend" and compared,
        "review_trend": item.operation == Operation.TIME_SERIES and len(item.rows) > 1,
    }[selection.kind]
    texts = _HYPOTHESES if hypothesis else _ACTIONS
    if finding.current_value is None or not relevant or selection.kind not in texts:
        _reject("unsupported_recommendation")
    return GroundedSuggestion(selection=selection, text=f"{screen_model_prose(texts[selection.kind])} [F{selection.finding_index + 1}; E{ref.evidence_index + 1}]")


def paired_coverage(findings: tuple[GroundedFinding, ...], evidence: tuple[Evidence, ...]) -> bool:
    """Every released group on both sides needs a defined compatible pair.

    A zero baseline is defined (only its percentage is unavailable). A null
    operand or an omitted group cannot be substituted with an invented zero.
    """
    if len(evidence) != 2 or not all(e.complete and e.rows for e in evidence):
        return False
    pairs = [f.selection for f in findings if f.selection.baseline is not None
             and f.current_value is not None and f.baseline_value is not None
             and f.selection.current.evidence_index == 0 and f.selection.baseline.evidence_index == 1]
    return ({p.current.row_index for p in pairs} == set(range(len(evidence[0].rows)))
            and {p.baseline.row_index for p in pairs} == set(range(len(evidence[1].rows))))


def default_selections(evidence: tuple[Evidence, ...]) -> tuple[FindingSelection, ...]:
    """Bounded neutral fallback; never invent an action or a global ranking."""
    result = []
    for i, item in enumerate(evidence):
        metric = item.metric_definition.get("metric")
        for row in range(min(len(item.rows), 2)):
            try:
                result.append(FindingSelection(operation="observed_value", current=ObservationRef(evidence_index=i, row_index=row, metric=metric)))
            except ValidationError:
                pass
        if item.operation in {Operation.PRODUCT_COMPARISON, Operation.DEMOGRAPHIC_COMPARISON} and len(item.rows) == 2:
            result.append(FindingSelection(operation="difference",
                current=ObservationRef(evidence_index=i, row_index=0, metric=metric),
                baseline=ObservationRef(evidence_index=i, row_index=1, metric=metric)))
            if item.operation == Operation.PRODUCT_COMPARISON:
                for support in ("item_count", "average_item_price"):
                    result.append(FindingSelection(operation="difference",
                        current=ObservationRef(evidence_index=i, row_index=0, metric=support),
                        baseline=ObservationRef(evidence_index=i, row_index=1, metric=support)))
    if len(evidence) == 2 and all(e.operation == Operation.CHURN_SNAPSHOT for e in evidence):
        result.append(FindingSelection(operation="percentage_point_change",
            current=ObservationRef(evidence_index=0, row_index=0, metric="churn_rate"),
            baseline=ObservationRef(evidence_index=1, row_index=0, metric="churn_rate")))
    if len(evidence) == 2 and all(e.operation == Operation.AGGREGATE for e in evidence):
        left, right = evidence
        metric = left.metric_definition.get("metric")
        candidates = []
        checked = []
        if metric == right.metric_definition.get("metric"):
            for row_index, row in enumerate(left.rows):
                matches = [j for j, other in enumerate(right.rows)
                           if all(row.get(k) == other.get(k) for k in _GROUPS)]
                if len(matches) != 1:
                    continue
                for operation in (("difference", "percentage_change") if len(left.rows) <= 10 else ("difference",)):
                    selection = FindingSelection(operation=operation,
                        current=ObservationRef(evidence_index=0, row_index=row_index, metric=metric),
                        baseline=ObservationRef(evidence_index=1, row_index=matches[0], metric=metric))
                    try:
                        finding = calculate(selection, evidence)
                    except PolicyError:
                        continue  # An internal default must not poison independent observations.
                    candidates.append(selection)
                    checked.append(finding)
        # Preserve the established compact fully paired representation. Partial
        # pairs retain independently observed values, including unmatched groups.
        if paired_coverage(tuple(checked), evidence):
            result = candidates
        else:
            result += candidates[:max(0, 20 - len(result))]
    return tuple(result)


def ground_report(draft: ReportDraft, evidence: tuple[Evidence, ...], *, fallback: bool = False) -> GroundedReport:
    # Revalidation also catches objects made with Pydantic's unchecked model_copy.
    draft = ReportDraft.model_validate(draft.model_dump())
    defaults = default_selections(evidence)
    selections = draft.findings or defaults
    if draft.findings:
        # Preserve established product/demographic/churn comparisons regardless
        # of whether the model selects a shorter or observation-only summary.
        selections += tuple(s for s in defaults if s.baseline is not None and s not in selections)[:max(0, 20 - len(selections))]
    findings = []
    for selection in selections:
        try:
            findings.append(calculate(selection, evidence))
        except PolicyError:
            if draft.findings and not fallback:
                raise
    findings = tuple(findings)
    if draft.headline_index is not None and draft.headline_index >= len(findings):
        _reject("invalid_finding_reference")
    recommendations = tuple(_suggestion(s, findings, evidence, hypothesis=False) for s in draft.recommendations)
    hypotheses = tuple(_suggestion(s, findings, evidence, hypothesis=True) for s in draft.hypotheses)
    return GroundedReport(factual_status="grounded" if findings else "evidence_only",
        selection_status="fallback" if fallback else draft.status,
        headline=findings[draft.headline_index or 0].text if findings else "Checked retail analysis: no supported factual finding available.",
        findings=findings, recommendations=recommendations, hypotheses=hypotheses)


# Report completion is independent of whether individual facts were grounded.

class ComposedSection(StrictModel):
    kind: ReportSection
    status: Literal["supported", "missing"]
    evidence_refs: tuple[Annotated[int, Field(strict=True, ge=0, le=1)], ...] = Field(default=(), max_length=2)
    finding_refs: tuple[Annotated[int, Field(strict=True, ge=0, le=19)], ...] = Field(default=(), max_length=20)


class ReportComposition(StrictModel):
    schema_version: Literal[1] = 1
    spec: ReportSpec
    # One canonical resolved request owns the temporal roles, scope and metrics.
    request: ResolvedRequest
    completeness: Literal["complete", "partial"]
    sections: tuple[ComposedSection, ...] = Field(min_length=1, max_length=6)
    missing: tuple[Literal["summary", "performance_table", "trend", "comparison", "limitations", "recommendations",
                           "observations", "evidence_completeness"], ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def bound_composition(self):
        if self.spec != report_spec(self.request):
            raise ValueError("unbound_report_spec")
        kinds = [s.kind for s in self.sections]
        if kinds != _required_sections(self.spec, self.request):
            raise ValueError("invalid_report_sections")
        absent = {s.kind for s in self.sections if s.status == "missing"}
        if (len(set(self.missing)) != len(self.missing)
                or absent != set(self.missing) - {"observations", "evidence_completeness"}
                or (self.completeness == "complete") != (not self.missing)):
            raise ValueError("invalid_report_completeness")
        return self


def report_spec(request: ResolvedRequest | RequestSpec) -> ReportSpec:
    spec = request.spec if isinstance(request, ResolvedRequest) else request
    if spec.report is not None:
        return spec.report
    sections = ["summary", "performance_table"]
    if spec.purpose == "trend":
        sections.append("trend")
    if spec.comparison is not None or any(q.operation in {Operation.PRODUCT_COMPARISON,
            Operation.DEMOGRAPHIC_COMPARISON} for q in spec.queries):
        sections.append("comparison")
    return ReportSpec(purpose="trend" if spec.purpose == "trend" else "executive",
                      requested_sections=tuple(sections + ["limitations", "recommendations"]))


def _required_sections(spec: ReportSpec, request: ResolvedRequest) -> list[str]:
    required = list(spec.requested_sections)
    for kind in ("summary", "limitations"):
        if kind not in required:
            required.append(kind)
    # The selected table presentation is an effective content obligation even
    # when the proposal's requested-section list omitted the table.
    if spec.presentation == "table" and "performance_table" not in required:
        required.append("performance_table")
    purpose_section = {"trend": "trend", "comparison": "comparison"}.get(spec.purpose)
    if purpose_section and purpose_section not in required:
        required.append(purpose_section)
    if request.action is not None and "recommendations" not in required:
        required.append("recommendations")
    return required


def apply_report_preferences(request: ResolvedRequest, grounded: GroundedReport) -> GroundedReport:
    """Keep unrequested actions out of every representation of a facts-only report."""
    if 'recommendations' not in _required_sections(report_spec(request), request):
        return grounded.model_copy(update={'recommendations':(), 'hypotheses':()})
    return grounded


def compose_report(request: ResolvedRequest, evidence: tuple[Evidence, ...],
                   grounded: GroundedReport) -> ReportComposition:
    """Assess the requested goal using released facts; never execute a query."""
    spec = report_spec(request)
    required = _required_sections(spec, request)
    observed = tuple(i for i, f in enumerate(grounded.findings) if f.current_value is not None
                     and f.value is not None)
    all_checked = bool(evidence) and all(e.coverage.status == "checked" and e.coverage.period_rows != 0
                                        and e.complete and bool(e.rows) for e in evidence)
    comparison = tuple(i for i in observed if grounded.findings[i].selection.baseline is not None)
    trend_evidence = {i for i, e in enumerate(evidence) if e.operation == Operation.TIME_SERIES
                      and e.complete and len({str(r.get("month")) for r in e.rows if r.get("month") is not None}) >= 2}
    trend = tuple(i for i in observed if grounded.findings[i].selection.current.evidence_index in trend_evidence)
    sections = []
    missing = []
    for kind in required:
        refs = (comparison if kind == "comparison" else trend if kind == "trend" else observed)
        supported = (bool(grounded.recommendations) if kind == "recommendations" else
                     True if kind == "limitations" else bool(refs))
        if kind == "summary":
            supported = any(grounded.findings[i].text == grounded.headline for i in observed)
        if kind == "comparison" and request.comparison is not None:
            supported = supported and all_checked and paired_coverage(grounded.findings, evidence)
        # A trend section must expose at least two observed monthly points.
        if kind == "trend":
            supported = any(len({grounded.findings[i].selection.current.row_index for i in refs
                if grounded.findings[i].selection.current.evidence_index == e}) >= 2 for e in trend_evidence)
        if kind == "recommendations":
            refs = tuple(dict.fromkeys(a.selection.finding_index for a in grounded.recommendations))
        evidence_refs = tuple(sorted({r.evidence_index for i in refs
            for r in (grounded.findings[i].selection.current, grounded.findings[i].selection.baseline) if r}))
        if kind == "limitations":
            evidence_refs = tuple(range(len(evidence)))
        sections.append(ComposedSection(kind=kind, status="supported" if supported else "missing",
                                        evidence_refs=evidence_refs, finding_refs=refs))
        if not supported:
            missing.append(kind)
    if not observed:
        missing.append("observations")
    if not all_checked:
        missing.append("evidence_completeness")
    return ReportComposition(spec=spec, request=request,
        completeness="partial" if missing else "complete", sections=tuple(sections), missing=tuple(missing))


def action_heading(request: ResolvedRequest | None) -> str:
    period = request.action if request else None
    if period is None:
        return "Recommended actions"
    start, end = period.start, period.end
    if (start.day == 1 and start.month in {1, 4, 7, 10}
            and (end.year * 12 + end.month) - (start.year * 12 + start.month) == 3 and end.day == 1):
        return f"Q{(start.month - 1) // 3 + 1} {start.year} actions"
    return f"Actions for UTC [{start}, {end})"


def composition_text(composition: ReportComposition, grounded: GroundedReport | None = None) -> str:
    status = "Partial report" if composition.completeness == "partial" else "Complete report"
    lines = [f"{status}: {composition.spec.purpose.replace('_', ' ')}",
             f"Presentation: {composition.spec.presentation}; detail: {composition.spec.detail}."]
    for section in composition.sections:
        label = action_heading(composition.request) if section.kind == "recommendations" else section.kind.replace("_", " ").capitalize()
        refs = ", ".join(f"F{i + 1}" for i in section.finding_refs)
        lines.append(f"{label}: {section.status}" + (f" [{refs}]" if refs else "") + ".")
        if grounded and section.status == "supported":
            if section.kind == "recommendations":
                lines.extend("  " + action.text for action in grounded.recommendations)
            elif section.kind in {"summary", "trend", "comparison"}:
                selected = section.finding_refs
                if section.kind == "summary":
                    lines.append(grounded.headline)
                else:
                    displayed = selected[:2] if composition.spec.detail == "concise" else selected
                    lines.extend(f"F{i + 1}: {grounded.findings[i].text}" for i in displayed)
                    if len(displayed) < len(selected):
                        lines.append("Additional checked findings are retained in the structured result.")
    if composition.spec.purpose == "causal_investigation":
        lines.append("Causal limitation: these observations support associations or hypotheses only; they do not establish causes.")
    if composition.missing:
        lines.append("Missing requested content or support: " + ", ".join(composition.missing) + ".")
        lines.append("Recovery: request the missing scoped evidence or a supported section/action selection in a new analysis, "
                     "or narrow the requested report. Saved figures retain their original as-of time; no extra queries were run to fill gaps.")
    return "\n".join(lines)
