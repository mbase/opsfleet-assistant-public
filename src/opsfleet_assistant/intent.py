"""Coordinate interpretation outcomes, trusted dates and bounded analytical plans."""

from datetime import date
import json

from pydantic import ValidationError

from .contracts import (AnalysisPlan, InterpretationIssue, InterpretationProposal, InterpretationResult, Mode,
                        Operation, ResolvedRequest, Metric, TemporalAnchor, RequestSpec, QuerySpec)
from .identity import PolicyError
from .periods import ResolutionFailure, resolve_request


ISSUE_TEXT = {
    InterpretationIssue.MISSING_PERIOD: "What UTC period should I analyze?",
    InterpretationIssue.MISSING_YEAR: "Which year should I use for that period?",
    InterpretationIssue.MISSING_METRIC: "Which supported metric should I analyze?",
    InterpretationIssue.MISSING_PRODUCTS: "Please give two authorized numeric product IDs to compare.",
    InterpretationIssue.AMBIGUOUS_METRIC: "Do you mean total revenue or average spend per customer?",
    InterpretationIssue.CONFLICTING_PERIODS: "Please clarify the conflicting analysis, comparison or action periods.",
    InterpretationIssue.UNRESOLVED: "Please clarify the request; materially different interpretations remain.",
    InterpretationIssue.UNSUPPORTED_TEMPORAL: "Please use a supported calendar period or explicit start and exclusive end dates.",
    InterpretationIssue.UNSUPPORTED: "This request is outside the supported retail analysis capabilities.",
}


def finalize_interpretation(proposal: InterpretationProposal, *,
                            pending_request: RequestSpec | None = None) -> InterpretationResult:
    """Reconcile only completion metadata against application-owned pending state.

    A newly supplied year changes an incomplete calendar expression, so no
    explicit refresh flag is needed. Existing periods and all analytical choices
    remain authoritative. Neither history nor model-provided state grants context.
    """
    old, new = pending_request, proposal.request
    flags = proposal.temporal_updates
    if (old is not None and new is not None and old.intent == new.intent == "analysis"
            and proposal.status == "ready" and proposal.context_action == "continue"
            and flags and len(set(flags)) == len(flags) and not proposal.clear_fields):
        roles = {"analysis", "comparison", "action"}
        unchanged_choices = old.model_dump(exclude=roles | {"sources"}) == new.model_dump(exclude=roles | {"sources"})
        completion_only = unchanged_choices
        for role in roles:
            before, after = getattr(old, role), getattr(new, role)
            if role in flags:
                filled_year = (before is not None and after is not None
                    and before.kind in {"month", "quarter", "year"}
                    and before.year is None and after.year is not None
                    and before.model_dump(exclude={"year"}) == after.model_dump(exclude={"year"}))
                completion_only = completion_only and filled_year
            else:
                completion_only = completion_only and before == after
        if completion_only:
            proposal = proposal.model_copy(update={"temporal_updates": ()})
    # Never return an unchecked wire proposal or generally relax the result contract.
    return InterpretationResult.model_validate(proposal.model_dump())


def _query_choice(query: QuerySpec, *, effective_brands: tuple[str, ...] | None = None,
                  include_limit: bool = True) -> str:
    """Semantic comparison key; never rewrites requested/executed parameters."""
    choice = query.model_dump(mode="json", exclude={"temporal_role"} | (set() if include_limit else {"limit"}))
    if effective_brands is not None:
        choice["requested_brands"] = effective_brands
    for field in ("requested_brands", "product_ids", "demographic_values", "revenue_statuses"):
        values = choice[field]
        if values is not None:
            choice[field] = sorted(v.strip().casefold()
                if field == "requested_brands" and effective_brands is None else v for v in values)
    return json.dumps(choice, sort_keys=True)


def comparison_choices_missing(spec: RequestSpec, *,
                               effective_query_brands: tuple[tuple[str, ...], ...] | None = None) -> bool:
    """Multiple query objects need distinct analytical choices, not just limits.

    Group/series operations specify choices within one evidence set. Scalar
    requests instead need a baseline role or distinct populations/definitions.
    Normalize set-valued filters; authorized effective brands, when available,
    also equate an omitted brand filter with its explicit full authorized set.
    Numerical values and query as-of times cannot resolve this ambiguity.
    """
    report = spec.report
    if (not report or not (report.purpose == "comparison" or "comparison" in report.requested_sections)
            or spec.comparison is not None
            or any(q.operation in {Operation.PRODUCT_COMPARISON, Operation.DEMOGRAPHIC_COMPARISON,
                                   Operation.TIME_SERIES} or q.dimension is not None for q in spec.queries)):
        return False
    choices = set()
    for index, query in enumerate(spec.queries):
        choices.add(_query_choice(query, include_limit=False,
            effective_brands=effective_query_brands[index] if effective_query_brands is not None else None))
    return len(choices) < 2


def resolve_interpretation(result: InterpretationResult, *, reference_date: date,
                           analytical_timezone: str, previous_plan: AnalysisPlan | None = None,
                           role_anchors: dict[str, TemporalAnchor] | None = None,
                           effective_query_brands: tuple[tuple[str, ...], ...] | None = None
                           ) -> tuple[InterpretationResult, ResolvedRequest | None]:
    if result.status != "ready" or result.request.intent != "analysis":
        return result, None
    if result.request.playbook and result.request.playbook.id == "product_revenue" and (
            not result.request.queries or not result.request.queries[0].product_ids
            or len(result.request.queries[0].product_ids) != 2):
        return result.model_copy(update={"status": "needs_clarification", "issues": (InterpretationIssue.MISSING_PRODUCTS,)}), None
    if result.request.playbook and result.request.playbook.id == "regional_spend" and (
            result.request.playbook.population != "qualifying_purchasers" or not result.request.queries
            or not result.request.queries[0].demographic_values):
        return result.model_copy(update={"status": "needs_clarification", "issues": (InterpretationIssue.UNRESOLVED,)}), None
    if not result.request.queries:
        return result.model_copy(update={"status": "needs_clarification",
                                         "issues": (InterpretationIssue.MISSING_METRIC,)}), None
    if comparison_choices_missing(result.request, effective_query_brands=effective_query_brands):
        return result.model_copy(update={"status": "needs_clarification",
                                         "issues": (InterpretationIssue.UNRESOLVED,)}), None
    try:
        resolved = resolve_request(result.request, reference_date=reference_date,
                                   analytical_timezone=analytical_timezone, previous_plan=previous_plan,
                                   role_anchors=role_anchors)
    except ResolutionFailure as exc:
        return result.model_copy(update={"status": "needs_clarification", "issues": (exc.issue,)}), None
    if result.request.mode == Mode.REPORT and not (result.request.playbook and result.request.playbook.id == "churn_change"):
        roles = [q.temporal_role for q in result.request.queries]
        if (resolved.comparison is not None and roles != ["analysis", "comparison"]
                or "comparison" in roles and resolved.comparison is None):
            return result.model_copy(update={"status": "needs_clarification",
                                             "issues": (InterpretationIssue.UNRESOLVED,)}), resolved
    return result, resolved


def analytical_plans(request: ResolvedRequest, *,
                     effective_query_brands: tuple[tuple[str, ...], ...] | None = None) -> tuple[AnalysisPlan, ...]:
    """An action horizon can never be selected by a query.

    Temporal comparison is a bounded pair of matching aggregate plans. Churn
    retains its existing separately bound cutoff comparison.
    """
    spec = request.spec
    if spec.playbook is not None:
        from .playbooks import validate_request
        validate_request(request)
    plans = []
    try:
        for query in spec.queries:
            period = request.analysis if query.temporal_role == "analysis" else request.comparison
            if period is None:
                raise PolicyError("plan_period_mismatch")
            data = query.model_dump(exclude={"temporal_role"})
            data["playbook"] = spec.playbook
            if spec.playbook:
                data["version"] = 2
            if query.operation == Operation.CHURN_CHANGE:
                if request.comparison is None:
                    raise PolicyError("plan_period_mismatch")
                data["cutoff_date"] = request.analysis.end
                data["baseline_cutoff_date"] = request.comparison.end
            elif query.operation == Operation.CHURN_SNAPSHOT:
                data["cutoff_date"] = period.end
            else:
                data["period"] = period
            plan = AnalysisPlan.model_validate(data)
            if plan.operation == Operation.DEMOGRAPHIC_COMPARISON and plan.limit > 10:
                raise PolicyError("plan_limit_mismatch")
            plans.append(plan)
    except ValidationError:
        raise PolicyError("invalid_plan_contract") from None
    expected = 2 if spec.mode in {Mode.CHURN_COMPARE, Mode.TEMPORAL_COMPARE} else 1
    if (spec.mode == Mode.REPORT and not 1 <= len(plans) <= 2
            or spec.mode != Mode.REPORT and len(plans) != expected):
        raise PolicyError("plan_intent_mismatch")
    if spec.mode == Mode.CHURN_COMPARE:
        if ([q.temporal_role for q in spec.queries] != ["analysis", "comparison"]
                or any(p.operation != Operation.CHURN_SNAPSHOT for p in plans)):
            raise PolicyError("plan_intent_mismatch")
        left, right = plans
        if (left.requested_brands != right.requested_brands or left.product_ids != right.product_ids
                or left.revenue_statuses != right.revenue_statuses or left.inactivity_days != right.inactivity_days):
            raise PolicyError("plan_intent_mismatch")
    elif spec.playbook and spec.playbook.id == "churn_change":
        if len(plans) != 1 or spec.queries[0].temporal_role != "analysis":
            raise PolicyError("plan_intent_mismatch")
    elif spec.mode == Mode.TEMPORAL_COMPARE or spec.mode == Mode.REPORT and request.comparison is not None:
        # Pair compatibility concerns populations/definitions, while each plan's
        # exact raw choices (including its limit) remain independently bound.
        # A scalar aggregate always has one row; differing scalar limits cannot
        # change its population. Grouped limits still have to match.
        scalar = all(p.operation == Operation.AGGREGATE and p.dimension is None for p in plans)
        choices = [_query_choice(q, include_limit=not scalar,
            effective_brands=effective_query_brands[i] if effective_query_brands is not None else None)
            for i, q in enumerate(spec.queries)]
        if ([q.temporal_role for q in spec.queries] != ["analysis", "comparison"]
                or choices[0] != choices[1]
                or request.comparison is None or request.comparison.end > request.analysis.start
                or plans[0].operation != Operation.AGGREGATE):
            raise PolicyError("plan_intent_mismatch")
    elif any(q.temporal_role != "analysis" for q in spec.queries):
        raise PolicyError("plan_intent_mismatch")
    if spec.mode == Mode.INVESTIGATE_TOP_PRODUCTS:
        first = plans[0]
        if (first.operation != Operation.AGGREGATE or first.dimension != "product"
                or first.metric != "revenue" or first.limit != 2):
            raise PolicyError("plan_investigation_mismatch")
    return tuple(plans)


def validate_bound_plans(plans: tuple[AnalysisPlan, ...], request: ResolvedRequest, *,
                         effective_query_brands: tuple[tuple[str, ...], ...] | None = None) -> None:
    """Bind every choice; only the supported monthly method may expand a limit."""
    expected = analytical_plans(request, effective_query_brands=effective_query_brands)
    if len(plans) != len(expected):
        raise PolicyError("plan_intent_mismatch")
    for actual, original in zip(plans, expected):
        if actual.baseline_cutoff_date != original.baseline_cutoff_date:
            raise PolicyError("plan_period_mismatch")
        if actual.playbook != original.playbook:
            raise PolicyError("plan_intent_mismatch")
        if actual.period != original.period or actual.cutoff_date != original.cutoff_date:
            raise PolicyError("plan_period_mismatch")
        if actual.metric != original.metric or actual.inactivity_days != original.inactivity_days:
            raise PolicyError("plan_metric_mismatch")
        if actual.requested_brands != original.requested_brands:
            raise PolicyError("plan_scope_mismatch")
        if actual.revenue_statuses != original.revenue_statuses:
            raise PolicyError("plan_status_mismatch")
        if actual.product_ids != original.product_ids:
            raise PolicyError("plan_product_mismatch")
        if actual.demographic_values != original.demographic_values or actual.dimension != original.dimension:
            raise PolicyError("plan_intent_mismatch")
        monthly_method = (request.spec.mode == Mode.ANALYSIS and request.spec.purpose == "trend"
            and len(expected) == 1 and original.metric == Metric.REVENUE
            and original.operation in {Operation.AGGREGATE, Operation.TIME_SERIES}
            and actual.operation == Operation.TIME_SERIES and original.period is not None
            and original.dimension is None and original.product_ids is None
            and original.demographic_values is None)
        transformed_limit = min(20, max(12, original.limit))
        if actual.operation != original.operation:
            if not monthly_method:
                raise PolicyError("plan_intent_mismatch")
            valid_limits = {transformed_limit}
        else:
            valid_limits = {original.limit}
            if monthly_method:
                valid_limits.add(transformed_limit)
        if (actual.limit not in valid_limits or actual.limit > 20
                or actual.operation == Operation.DEMOGRAPHIC_COMPARISON and actual.limit > 10):
            raise PolicyError("plan_limit_mismatch")
