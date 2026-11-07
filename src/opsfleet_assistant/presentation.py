"""Literal Rich views of trusted released data; no prose parsing or persistence.

The additive, versioned display payload is rebuilt only after workflow validation.
Legacy artifacts lack it and retain their original plain text. Display rounding
never changes evidence, findings, saved content or the JSON serializer.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import json
from typing import TYPE_CHECKING, Literal

from pydantic import Field
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .contracts import ResolvedRequest, StrictModel
from .evidence import Evidence, protected_unavailable, PROTECTED_UNAVAILABLE_TEXT
from .reporting import GroundedReport, action_heading

if TYPE_CHECKING:
    from .workflow import TurnResult


class PresentationData(StrictModel):
    """Ephemeral view, never an independently saved source of analytical truth."""

    schema_version: Literal[1, 2] = 1
    driver: dict | None = None
    headline: str
    selection_status: Literal["selected", "fallback", "refused"]
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=2)
    defaults: tuple[str, ...] = ()
    empty_diagnosis: Literal["valid_empty", "incomplete", "unavailable"] | None = None
    contextual_reuse: bool = False
    context_goal: Literal["explain", "why", "summary", "save"] | None = None
    actions_heading: str = "Recommended actions"


def display_payload(evidence: tuple[Evidence, ...], grounded: GroundedReport,
                    request: ResolvedRequest | None, *, empty_diagnosis: str | None = None,
                    contextual_reuse: bool = False, context_goal: str | None = None) -> dict:
    return PresentationData(schema_version=2 if grounded.driver else 1,
        driver=grounded.driver.model_dump(mode="json") if grounded.driver else None, headline=grounded.headline, selection_status=grounded.selection_status,
        evidence=evidence,
        defaults=tuple(dict.fromkeys(s.field for s in request.spec.sources if s.origin == "default")) if request else (),
        empty_diagnosis=empty_diagnosis, contextual_reuse=contextual_reuse,
        context_goal=context_goal, actions_heading=action_heading(request)).model_dump(mode="json")


_COUNTS = frozenset({"order_count", "customer_count", "item_count", "customers",
                     "eligible_customers", "inactive_customers"})
_MONEY = frozenset({"revenue", "average_order_value", "customer_spend",
                    "average_customer_spend", "average_item_price"})


def display_value(value: object, column: str) -> str:
    if value is None:
        return "unavailable"
    if column not in _COUNTS | _MONEY | {"churn_rate"}:
        return str(value)
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "unavailable"
        with localcontext() as ctx:
            ctx.prec = max(100, len(number.as_tuple().digits) + abs(number.adjusted()) + 10)
            if column == "churn_rate":
                return f"{(number * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN):f}%"
            if column in _COUNTS:
                # Released counts are integers; never round a fractional count.
                return str(int(number)) if number == number.to_integral_value() else "unavailable"
            return f"{number.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN):f}"
    except (InvalidOperation, ValueError, TypeError):
        return "unavailable"


def _line(console: Console, value: object, *, style: str = "") -> None:
    import re
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(value))
    value = ''.join(c for c in value if c in '\n\t' or c.isprintable())
    console.print(Text(value, style=style, overflow="fold"), highlight=False)


def _period(period: dict) -> str:
    if "cutoff_date" in period:
        return f"cutoff {period['cutoff_date']} UTC"
    start = period.get("start", period.get("period_start", "unavailable"))
    end = period.get("end", period.get("period_end", "unavailable"))
    return f"UTC [{start}, {end})"


def _values(console: Console, item: Evidence) -> None:
    if protected_unavailable(item):
        _line(console, PROTECTED_UNAVAILABLE_TEXT)
        return
    if item.coverage.period_rows == 0:
        _line(console, "No matching data; aggregate zero is not an observed result.")
        return
    if not item.rows:
        _line(console, "No qualifying rows.")
        return
    # Stack records on small screens or wide result shapes. Nothing is ellipsized
    # and every released row remains available even for a concise report request.
    if console.width < max(60, len(item.columns) * 16):
        for i, row in enumerate(item.rows, 1):
            _line(console, f"Row {i}", style="dim")
            for column in item.columns:
                _line(console, f"{column.replace('_', ' ')}: {display_value(row.get(column), column)}")
        return
    table = Table(box=None, padding=(0, 1), expand=False)
    for column in item.columns:
        table.add_column(Text(column.replace("_", " ")), overflow="fold")
    for row in item.rows:
        table.add_row(*(Text(display_value(row.get(c), c), overflow="fold") for c in item.columns))
    console.print(table)


def _definitions(item: Evidence) -> tuple[str, ...]:
    """Only definitions relevant to the released measures, from contract fields."""
    definition = item.metric_definition
    columns = set(item.columns)
    lines = []
    if "revenue_statuses" in definition:
        lines.append("Qualifying item statuses: " + ", ".join(definition["revenue_statuses"]) + ".")
        if "Processing" in definition["revenue_statuses"] and columns & _MONEY:
            lines.append("Including Processing is an analytical convention, not accounting recognition.")
    if definition.get("excluded_null_sale_price"):
        lines.append("Null-priced items are excluded.")
    if definition.get("time_basis"):
        lines.append(f"Time basis: {definition['time_basis']}; UTC start inclusive, end exclusive.")
    meanings = {
        "revenue": "Revenue sums qualifying item sale prices.",
        "order_count": "Order count counts distinct orders with qualifying scoped items.",
        "customer_count": "Customer count counts distinct purchasers with qualifying scoped items.",
        "item_count": "Item count counts qualifying scoped items.",
        "customer_spend": "Customer spend sums qualifying item sale prices per purchaser.",
        "average_order_value": "Average order value divides scoped revenue by distinct qualifying orders.",
        "average_item_price": "Average item price averages qualifying item sale prices.",
        "average_customer_spend": "Average customer spend divides cohort revenue by qualifying purchasers.",
    }
    lines.extend(text for metric, text in meanings.items() if metric in columns)
    if "churn_rate" in columns:
        days = definition.get("inactivity_days", "unavailable")
        cutoff = definition.get("cutoff_date", "unavailable")
        lines.append(f"Churn: {days}-day inactivity among prior purchasers in the effective brands at cutoff {cutoff} UTC; inactive / eligible purchasers. No prior purchasers means an undefined rate.")
    if item.operation.value == "demographic_comparison":
        lines.append("Demographic groups require at least five customers; complementary coverage counts are withheld.")
    lines.append("Current item statuses can restate historical results.")
    return tuple(lines)


_STATES = {
    "needs_clarification": "Clarification needed", "clarification": "Clarification needed",
    "unsupported": "Unsupported request", "error": "Request failed",
    "confirmation_required": "Deletion confirmation required", "deleted": "Report deleted",
    "partial": "Partial report", "ok": "Answer",
}
_RECOVERY = {
    "provider": "Check provider/warehouse connectivity and configuration. Check job status before retrying when submission is uncertain. No successful save is claimed.",
    "storage": "Check the local state path and access, then retry with the same conversation and turn IDs.",
    "budget": "Narrow the request or retry when dependencies are available; no complete result is claimed.",
    "policy": "Use an authorized scope and supported metric/period, or start a new conversation if context is incompatible.",
    "syntax": "The query compiler needs a code correction. Use the trace reference to report the defect.",
    "schema": "Source correction is unavailable. Use the trace reference to report incompatible source fields.",
    "internal": "Retry the request; use the trace reference if the problem persists.",
}


def render_result(console: Console, result: TurnResult, *, verbose: bool = False) -> None:
    payload = PresentationData.model_validate(result.presentation) if result.presentation else None
    if payload:
        _line(console, payload.headline, style="bold")
        state = (result.report_completeness.capitalize() + " report" if result.report_completeness
                 else _STATES.get(result.status, result.status))
        _line(console, state, style="yellow" if result.status == "partial" else "cyan")
        for finding in result.factual_findings:
            if finding["text"] != payload.headline:
                _line(console, finding["text"])
        if payload and payload.driver:
            from .playbooks.base import DriverFinding, text as driver_text
            _line(console, driver_text(DriverFinding.model_validate(payload.driver)))
        for role, period in (result.resolved_periods or {}).items():
            if period:
                _line(console, f"{role.capitalize()}: {_period(period)}")
                if role == "action":
                    _line(console, "Recommendation horizon only; no future results queried or asserted.")
        for i, item in enumerate(payload.evidence, 1):
            _line(console, f"E{i} — {item.operation.value.replace('_', ' ')}; {_period(item.period_utc)}", style="bold")
            scope = item.execution_scope
            brands = json.dumps(scope.brands, ensure_ascii=False) if scope else "unavailable (legacy metadata)"
            products = (json.dumps(scope.product_ids) if scope and scope.product_ids is not None
                        else "all products within these brands" if scope else "unavailable")
            _line(console, f"Scope: {item.scope_reference} v{item.scope_version}; brands {brands}; product IDs {products}.")
            _values(console, item)
        for kind in ("recommendations", "hypotheses"):
            actions = [s for s in result.suggestions if s["kind"] == kind]
            if actions:
                label = payload.actions_heading if kind == "recommendations" else "Hypotheses (unverified)"
                _line(console, label + " (suggestions, not observed outcomes)", style="bold")
                for action in actions:
                    _line(console, action["text"])
        _line(console, f"Factual findings: {result.factual_status}; selection: {payload.selection_status}.")
        if payload.selection_status in {"fallback", "refused"}:
            _line(console, "Model selection unavailable or refused; deterministic checked findings are shown. No model-selected actions were released.")
        composition = result.report_composition
        if composition:
            if composition["missing"]:
                _line(console, "Missing requested content or support: " + ", ".join(composition["missing"]) + ".", style="yellow")
                _line(console, "Recovery: request the missing scoped evidence or a supported section/action in a new analysis, or narrow the report. No extra queries filled these gaps.")
            if composition["spec"]["purpose"] == "causal_investigation":
                _line(console, "Causal limitation: observations and hypotheses do not establish causes.")
        if payload.defaults:
            _line(console, "Documented defaults applied: " + ", ".join(payload.defaults) + ".")
        if result.golden_status == "selected":
            _line(console, f"Methodology: synthetic authored example {result.golden_trio_id}; {result.golden_method}. All figures come from scoped warehouse evidence.")
        elif result.golden_status == "outage":
            _line(console, "Approved methodology lookup unavailable; the supported analysis path was used.")
        if payload.empty_diagnosis == "unavailable":
            _line(console, PROTECTED_UNAVAILABLE_TEXT)
        elif payload.empty_diagnosis:
            _line(console, "No matching scoped source data." if payload.empty_diagnosis == "valid_empty" else
                  "No matching data released; diagnosis could not finish within the remaining budget.")
        elif result.reused_report and any((not e.rows or e.coverage.period_rows == 0) and not protected_unavailable(e) for e in payload.evidence):
            _line(console, "Saved empty-data diagnostic outcome is unavailable in structured support; it may have stopped at the original budget. Exact original notes remain in the JSON text field.")
        for i, item in enumerate(payload.evidence, 1):
            _line(console, f"E{i} limitations and definitions", style="bold")
            for line in _definitions(item):
                _line(console, line)
            _line(console, f"Evidence {'complete' if item.complete else 'incomplete'}: {item.completeness_note}")
            _line(console, f"Original evidence as of: {item.query_as_of_utc}. {item.data_as_of_note}")
            _line(console, f"Coverage: {item.coverage.note} Latest scoped item: {item.coverage.latest_item_at_utc or 'unknown'}.")
        if payload.contextual_reuse:
            _line(console, "Reused original evidence after current authorization; no new analytical jobs supplied these figures.")
        if payload.context_goal == "why":
            _line(console, "These observations do not establish a cause.")
            metric = payload.evidence[0].metric_definition.get("metric")
            investigation = ("Compare earlier inactivity cutoffs within the same authorized purchaser population."
                if metric == "churn_rate" else
                "Compare aggregate cohort spend and group sizes without releasing membership IDs."
                if metric == "average_customer_spend" else
                "Request a scoped monthly trend to investigate when the pattern changed, then review relevant business events.")
            _line(console, "Suggested investigation: " + investigation)
        if result.report_id is None:
            _line(console, "Save: no report saved by this turn.")
    else:
        _line(console, _STATES.get(result.status, result.status), style="bold yellow" if result.status != "ok" else "bold cyan")
        if result.report_completeness == "legacy":
            _line(console, "Legacy saved report: structured evidence/completeness unavailable; original content follows. Historical completeness claims are not reverified.")
        _line(console, result.text)
        if payload and payload.driver:
            from .playbooks.base import DriverFinding, text as driver_text
            _line(console, driver_text(DriverFinding.model_validate(payload.driver)))
        for role, period in (result.resolved_periods or {}).items():
            if period:
                _line(console, f"{role.capitalize()}: {_period(period)}")
    if result.status == "error":
        warehouse_failure = (result.failure_stage in {"dry_run", "execute", "metadata", "compile", "evidence_checks"}
                             or result.failure_job_id is not None or result.analysis_job_id is not None)
        if result.analysis_submission_state == "submitted":
            _line(console, "Analysis completed but its diagnostic or evidence checks failed. Automatic replay stopped; inspect the job before submitting again.")
        elif warehouse_failure and result.failure_submission_state != "not_submitted":
            _line(console, "Submission may have run or billed. Automatic replay stopped; check warehouse job status and billing before submitting again.")
        else:
            _line(console, _RECOVERY.get(result.error_category, "Use the recovery instructions above; retry with the same IDs if appropriate."))
    if result.report_id is not None:
        _line(console, f"Saved report: {result.report_id} (completeness: {result.report_completeness or 'legacy'})")
    if result.reused_report:
        _line(console, "Loaded the existing report after rechecking current catalog and scope; no model call was made.")
    _line(console, f"Conversation: {result.conversation_id}")
    _line(console, f"Trace: {result.trace_id or 'unavailable'}")
    if verbose:
        if result.status == "error":
            failure_bytes = result.failure_billed_bytes if result.failure_billed_bytes is not None else "unknown"
            analysis_bytes = result.analysis_billed_bytes if result.analysis_billed_bytes is not None else "unknown"
            _line(console, f"Failure: stage {result.failure_stage}; purpose {result.failure_purpose}; submission {result.failure_submission_state}; job {result.failure_job_id or 'unavailable'}; billed bytes {failure_bytes}")
            if result.analysis_submission_state != "unknown":
                _line(console, f"Analysis: submission {result.analysis_submission_state}; job {result.analysis_job_id or 'unavailable'}; billed bytes {analysis_bytes}")
        _line(console, f"Turn: {result.turn_id}")
        _line(console, f"Model routes: {', '.join(result.model_routes) or 'none'}; requests: {result.model_requests}")
        billed = result.billed_bytes if result.billed_bytes is not None else "unknown"
        _line(console, f"BigQuery executions: {result.query_executions}; billed bytes: {billed}; analysis jobs: {', '.join(result.evidence_job_ids) or 'none'}")
        _line(console, f"Golden: {result.golden_status}; trio: {result.golden_trio_id or 'unavailable'}; method: {result.golden_method or 'unavailable'}")
        if payload:
            for i, item in enumerate(payload.evidence, 1):
                _line(console, f"E{i} detailed metadata: " + json.dumps(item.model_dump(mode="json", exclude={"rows"}), ensure_ascii=False, sort_keys=True))


def render_navigation(console, result):
    """Stacked bounded records work on narrow terminals; saved strings are literal."""
    def literal(value):
        # Strip C0/C1 controls and escape sequences at display time only.
        import re
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(value))
        text = ''.join(c for c in text if c in '\n\t' or c.isprintable())
        console.print(Text(text, overflow='fold'), highlight=False)
    literal(f'{result.operation}: {result.status}')
    if result.active_conversation:
        literal('Active conversation: ' + result.active_conversation)
    for item in result.items:
        for key, value in item.items():
            literal(f'{key}: {value}')
        literal('')
    if result.content:
        for key,value in result.content.items():
            if key != 'support':literal(f'{key}: {value}')
    if result.next_cursor:
        literal('Next page: --cursor ' + result.next_cursor + ' (retain --limit)')
    if result.message:literal(result.message)
