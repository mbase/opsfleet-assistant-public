"""Versioned analytical defaults and disclosure text for Step 2."""

from __future__ import annotations

from .contracts import AnalysisPlan, Operation


METRIC_VERSION = 1
MIN_DEMOGRAPHIC_CUSTOMERS = 5


def effective_definition(plan: AnalysisPlan) -> dict[str, object]:
    """Attach the actual metric choices to later evidence and reports."""
    definition: dict[str, object] = {
        "version": METRIC_VERSION,
        "metric": plan.metric.value,
        "revenue_statuses": [status.value for status in plan.revenue_statuses],
        "excluded_null_sale_price": True,
        "time_basis": "order_items.created_at_utc",
        "period_end_exclusive": True,
    }
    if plan.playbook:
        definition["playbook"] = plan.playbook.model_dump(mode="json")
        definition["transaction_grain"] = "qualifying_item"
    if plan.operation == Operation.REGIONAL_SPEND:
        definition["population"] = "qualifying_purchasers"
        definition["dimension"] = "state"
        definition["demographic_values"] = list(plan.demographic_values)
        definition["order_definition"] = "distinct_orders_containing_scoped_qualifying_items"
    if plan.operation == Operation.CHURN_CHANGE:
        definition["baseline_cutoff_date"] = plan.baseline_cutoff_date.isoformat()
        definition["segment_definition"] = "country_profile_at_query_time; C:source_value; U:null; H:headline"
        definition["segment_release"] = "all_or_none; max9; each_active_and_inactive_at_least5; both_cutoffs"
    if plan.operation in {Operation.CHURN_SNAPSHOT, Operation.CHURN_CHANGE}:
        definition["cutoff_date"] = plan.cutoff_date.isoformat() if plan.cutoff_date else None
        definition["inactivity_days"] = plan.inactivity_days
        definition["population"] = "prior_purchasers_in_effective_brands"
    else:
        definition["period_start"] = plan.period.start.isoformat() if plan.period else None
        definition["period_end"] = plan.period.end.isoformat() if plan.period else None
    return definition
