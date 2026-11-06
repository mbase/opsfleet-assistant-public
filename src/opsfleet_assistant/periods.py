"""Trusted calendar arithmetic over typed concepts; no language matching.

All intervals use UTC dates, inclusive starts and exclusive ends. To-date
intervals include the reference UTC day, matching the prototype convention.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from .contracts import (AnalysisPlan, InterpretationIssue, Period, RequestSpec,
                        ResolvedRequest, TemporalExpression, TemporalRole, YearExpression, TemporalAnchor)


class ResolutionFailure(Exception):
    def __init__(self, issue: InterpretationIssue):
        self.issue = issue
        super().__init__(issue.value)


def shift_months(day: date, months: int) -> date:
    index = (day.year - 1) * 12 + day.month - 1 + months
    year, month = index // 12 + 1, index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _year(expr: YearExpression | None, today: date, previous: Period | None) -> int:
    if expr is None:
        raise ResolutionFailure(InterpretationIssue.MISSING_YEAR)
    if expr.kind == "absolute":
        return expr.value
    if expr.kind == "relative":
        return today.year + expr.offset
    if previous and previous.start.year == (previous.end - timedelta(days=1)).year:
        return previous.start.year
    raise ResolutionFailure(InterpretationIssue.MISSING_YEAR)


def _floor(day: date, unit: str) -> date:
    month = 1 if unit == "year" else ((day.month - 1) // 3 * 3 + 1 if unit == "quarter" else day.month)
    return date(day.year, month, 1)


def resolve_request(spec: RequestSpec, *, reference_date: date,
                    analytical_timezone: str, previous_plan: AnalysisPlan | None = None,
                    role_anchors: dict[str, TemporalAnchor] | None = None) -> ResolvedRequest:
    """Resolve at most three roles plus a bounded, application-owned prior period.

    References are a finite role graph, not nested programs. Missing targets,
    cycles, out-of-range arithmetic and unsupported timezones never broaden dates.
    """
    if analytical_timezone != "UTC":
        raise ValueError("unsupported_analytical_timezone")
    default_anchor = TemporalAnchor.capture(reference_date, previous_plan)
    anchors = role_anchors or {}
    if set(anchors) - {"analysis", "comparison", "action"}:
        raise ValueError("invalid_temporal_anchor_role")
    expressions = {TemporalRole.ANALYSIS: spec.analysis, TemporalRole.COMPARISON: spec.comparison,
                   TemporalRole.ACTION: spec.action}
    resolved: dict[TemporalRole, Period] = {}
    visiting: set[TemporalRole] = set()

    def visit(role: TemporalRole) -> Period:
        if role in resolved:
            return resolved[role]
        if role in visiting:
            raise ResolutionFailure(InterpretationIssue.CONFLICTING_PERIODS)
        expr = expressions[role]
        if expr is None:
            raise ResolutionFailure(InterpretationIssue.MISSING_PERIOD)
        visiting.add(role)
        value = calculate(expr, role)
        visiting.remove(role)
        resolved[role] = value
        return value

    def calculate(expr: TemporalExpression, role: TemporalRole) -> Period:
        anchor = anchors.get(role, default_anchor)
        clock = anchor.reference_date
        prior = anchor.previous_period
        if prior is None and anchor.previous_cutoff:
            prior = Period(start=anchor.previous_cutoff - timedelta(days=1), end=anchor.previous_cutoff)
        kind = expr.kind
        if kind == "interval":
            return Period(start=expr.start, end=expr.end)
        if kind in {"month", "quarter", "year"}:
            year = _year(expr.year, clock, anchor.previous_period)
            month = expr.month if kind == "month" else (expr.quarter - 1) * 3 + 1 if kind == "quarter" else 1
            start = date(year, month, 1)
            return Period(start=start, end=shift_months(start, {"month": 1, "quarter": 3, "year": 12}[kind]))
        months = {"month": 1, "quarter": 3, "year": 12}.get(expr.unit)
        if kind == "window":
            boundary = shift_months(_floor(clock, expr.unit), months * expr.offset)
            end = shift_months(boundary, months if expr.include_current else 0)
            return Period(start=shift_months(end, -months * expr.count), end=end)
        if kind == "to_date":
            return Period(start=_floor(clock, expr.unit), end=clock + timedelta(days=1))
        if expr.reference == TemporalRole.PREVIOUS:
            if prior is None:
                raise ResolutionFailure(InterpretationIssue.MISSING_PERIOD)
            base = prior
        else:
            base = visit(expr.reference)
        if expr.unit == "period":
            # Preserve calendar widths where possible, including leap years.
            width = (base.end.year - base.start.year) * 12 + base.end.month - base.start.month
            if base.start.day == base.end.day == 1 and width > 0:
                months = width
            else:
                delta = (base.end - base.start) * expr.offset
                return Period(start=base.start + delta, end=base.end + delta)
        return Period(start=shift_months(base.start, months * expr.offset),
                      end=shift_months(base.end, months * expr.offset))

    try:
        analysis = visit(TemporalRole.ANALYSIS)
        comparison = visit(TemporalRole.COMPARISON) if spec.comparison else None
        action = visit(TemporalRole.ACTION) if spec.action else None
        return ResolvedRequest(spec=spec, analysis=analysis, comparison=comparison, action=action)
    except (ValueError, OverflowError):
        raise ResolutionFailure(InterpretationIssue.UNSUPPORTED_TEMPORAL) from None
