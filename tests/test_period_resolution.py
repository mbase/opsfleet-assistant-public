"""Deterministic calendar tests. These do not test language understanding."""

from datetime import date

import pytest
from pydantic import ValidationError

from opsfleet_assistant.contracts import (AnalysisPlan, RequestSpec, TemporalExpression as T,
                                         YearExpression as Y)
from opsfleet_assistant.periods import ResolutionFailure, resolve_request

TODAY = date(2026, 11, 4)


def resolve(analysis, **roles):
    return resolve_request(RequestSpec(analysis=analysis, **roles), reference_date=TODAY,
                           analytical_timezone="UTC")


@pytest.mark.parametrize("expression,start,end", [
    (T(kind="month", month=1, year=Y(kind="absolute", value=2025)), "2025-01-01", "2025-02-01"),
    (T(kind="month", month=2, year=Y(kind="absolute", value=2024)), "2024-02-01", "2024-03-01"),
    (T(kind="month", month=12, year=Y(kind="absolute", value=2025)), "2025-12-01", "2026-01-01"),
    (T(kind="month", month=1, year=Y(kind="relative", offset=-1)), "2025-01-01", "2025-02-01"),
    (T(kind="year", year=Y(kind="absolute", value=2025)), "2025-01-01", "2026-01-01"),
    (T(kind="year", year=Y(kind="relative", offset=-1)), "2025-01-01", "2026-01-01"),
    (T(kind="quarter", quarter=4, year=Y(kind="absolute", value=2025)), "2025-10-01", "2026-01-01"),
    (T(kind="window", unit="month", count=3, offset=0, include_current=False), "2026-08-01", "2026-11-01"),
    (T(kind="window", unit="month", count=1, offset=0, include_current=False), "2026-10-01", "2026-11-01"),
    (T(kind="window", unit="quarter", count=2, offset=0, include_current=False), "2026-04-01", "2026-10-01"),
    (T(kind="window", unit="year", count=2, offset=0, include_current=False), "2024-01-01", "2026-01-01"),
    (T(kind="window", unit="month", count=1, offset=1, include_current=True), "2026-12-01", "2027-01-01"),
    (T(kind="to_date", unit="month"), "2026-11-01", "2026-11-05"),
    (T(kind="to_date", unit="quarter"), "2026-10-01", "2026-11-05"),
    (T(kind="to_date", unit="year"), "2026-01-01", "2026-11-05"),
    (T(kind="interval", start="2024-02-29", end="2024-03-02"), "2024-02-29", "2024-03-02"),
])
def test_calendar_concepts(expression, start, end):
    result = resolve(expression)
    assert result.analysis.start.isoformat() == start
    assert result.analysis.end.isoformat() == end


def test_distinct_roles_and_following_quarter_use_analysis_not_clock():
    result = resolve(T(kind="quarter", quarter=3, year=Y(kind="absolute", value=2025)),
                     comparison=T(kind="quarter", quarter=3, year=Y(kind="absolute", value=2024)),
                     action=T(kind="reference", reference="analysis", unit="quarter", offset=1))
    assert result.analysis.model_dump(mode="json") == {"start": "2025-07-01", "end": "2025-10-01"}
    assert result.comparison.model_dump(mode="json") == {"start": "2024-07-01", "end": "2024-10-01"}
    assert result.action.model_dump(mode="json") == {"start": "2025-10-01", "end": "2026-01-01"}


def test_reference_chains_and_prior_period_preserve_calendar_width():
    prior = AnalysisPlan(operation="aggregate", metric="revenue", period={"start": "2024-01-01", "end": "2025-01-01"})
    spec = RequestSpec(analysis=T(kind="reference", reference="previous", unit="period", offset=-1),
                       comparison=T(kind="reference", reference="analysis", unit="year", offset=-1),
                       action=T(kind="reference", reference="comparison", unit="year", offset=3))
    result = resolve_request(spec, reference_date=TODAY, analytical_timezone="UTC", previous_plan=prior)
    assert result.analysis.start == date(2023, 1, 1)
    assert result.comparison.start == date(2022, 1, 1)
    assert result.action.start == date(2025, 1, 1)


@pytest.mark.parametrize("expression,roles,issue", [
    (T(kind="month", month=1), {}, "missing_year"),
    (T(kind="month", month=1, year=Y(kind="context")), {}, "missing_year"),
    (T(kind="reference", reference="comparison", unit="period", offset=0), {}, "missing_period"),
    (T(kind="reference", reference="analysis", unit="period", offset=0), {}, "conflicting_periods"),
    (T(kind="reference", reference="action", unit="quarter", offset=-1),
     {"action": T(kind="reference", reference="analysis", unit="quarter", offset=1)}, "conflicting_periods"),
    (T(kind="year", year=Y(kind="absolute", value=1)),
     {"action": T(kind="reference", reference="analysis", unit="year", offset=-1)}, "unsupported_temporal"),
])
def test_unresolved_roles_fail_without_widening(expression, roles, issue):
    with pytest.raises(ResolutionFailure) as error:
        resolve(expression, **roles)
    assert error.value.issue.value == issue


@pytest.mark.parametrize("data", [
    {"kind": "month", "month": 13}, {"kind": "quarter", "quarter": 0},
    {"kind": "month", "month": 1, "quarter": 1},
    {"kind": "interval", "start": "2025-02-01", "end": "2025-01-01"},
    {"kind": "interval", "start": "2025-01-01", "end": "2025-01-01"},
    {"kind": "interval", "start": "2025-02-29", "end": "2025-03-01"},
    {"kind": "window", "unit": "period", "count": 3, "offset": 0, "include_current": False},
    {"kind": "window", "unit": "month", "count": 121, "offset": 0, "include_current": False},
    {"kind": "reference", "reference": "today", "unit": "month", "offset": 1},
    {"kind": "reference", "reference": "analysis", "unit": "month", "offset": 121},
    {"kind": "month", "month": 1, "timezone": "Europe/Minsk"},
    {"kind": "month", "month": 1, "year": {"kind": "absolute", "value": 2025, "offset": -1}},
])
def test_invalid_fields_are_contract_errors(data):
    with pytest.raises(ValidationError):
        T.model_validate(data)


def test_context_year_requires_one_unambiguous_prior_year():
    expression = RequestSpec(analysis=T(kind="month", month=1, year=Y(kind="context")))
    for end, expected in [("2026-01-01", 2025), ("2026-02-01", None)]:
        previous = AnalysisPlan(operation="aggregate", metric="revenue", period={"start": "2025-01-01", "end": end})
        if expected:
            assert resolve_request(expression, reference_date=TODAY, analytical_timezone="UTC", previous_plan=previous).analysis.start.year == expected
        else:
            with pytest.raises(ResolutionFailure, match="missing_year"):
                resolve_request(expression, reference_date=TODAY, analytical_timezone="UTC", previous_plan=previous)


def test_timezone_is_supplied_by_application_and_utc_only():
    with pytest.raises(ValueError, match="unsupported_analytical_timezone"):
        resolve_request(RequestSpec(), reference_date=TODAY, analytical_timezone="Europe/Minsk")


def test_previous_quarter_reference_matches_existing_calendar_behavior():
    prior = AnalysisPlan(operation="aggregate", metric="revenue", period={"start": "2025-01-01", "end": "2025-04-01"})
    request = RequestSpec(analysis=T(kind="reference", reference="previous", unit="period", offset=-1))
    actual = resolve_request(request, reference_date=TODAY, analytical_timezone="UTC", previous_plan=prior)
    assert actual.analysis.model_dump(mode="json") == {"start": "2024-10-01", "end": "2025-01-01"}
