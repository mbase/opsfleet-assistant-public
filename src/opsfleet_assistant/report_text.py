"""Versioned trusted saved-text format. Validate equality; never replace history.

Version 1 preserves the renderer shipped with Part 3. Keep compatible readers
when introducing another format. No model, warehouse or arbitrary prose input.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import Field, model_validator

from .contracts import ReportDraft, ResolvedRequest, StrictModel
from .evidence import Evidence, protected_unavailable, PROTECTED_UNAVAILABLE_TEXT
from .golden import Selection, METHODS
from .identity import PolicyError
from .reporting import GroundedReport, compose_report, composition_text, ground_report, action_heading


def render_v1(evidence: tuple[Evidence, ...], draft: ReportDraft, draft_error: str | None,
            *, report_requested: bool, golden_status: str = "none",
            golden: Selection | None = None, empty_diagnosis: str | None = None,
            resolved_request: ResolvedRequest | None = None,
            grounded: GroundedReport | None = None) -> str:
    grounded = grounded or ground_report(draft, evidence, fallback=bool(draft_error))
    composition = compose_report(resolved_request, evidence, grounded) if report_requested and resolved_request else None
    parts = ([composition_text(composition, grounded), ""] if composition else []) + [grounded.headline, ""]
    if composition is None:
        for index, finding in enumerate(grounded.findings, 1):
            parts.append(f"F{index}: {finding.text}")
    parts.append(f"Factual findings: {grounded.factual_status}; selection: {grounded.selection_status}.")
    if resolved_request:
        request = resolved_request
        metrics = ", ".join(dict.fromkeys(q.metric.value for q in request.spec.queries))
        parts.append(f"Interpreted metrics: {metrics}; analysis UTC [{request.analysis.start}, {request.analysis.end}).")
        for role in ("comparison", "action"):
            period = getattr(request, role)
            if period:
                parts.append(f"{role.capitalize()} period UTC [{period.start}, {period.end})." +
                             (" Recommendation horizon only; no future results queried or asserted." if role == "action" else ""))
        defaults = ", ".join(source.field for source in request.spec.sources if source.origin == "default")
        if defaults:
            parts.append(f"Documented defaults applied: {defaults}.")
    if golden is not None:
        parts.append(f"Golden Knowledge: synthetic authored {golden.trio_id} v{golden.version} selected; {golden.summary} Fresh scoped warehouse evidence supplies all figures.")
    elif golden_status in {"no_match", "outage"}:
        parts.append("Golden Knowledge: no compatible approved example available; using the supported analysis path.")
    if empty_diagnosis == "unavailable":
        parts.append(PROTECTED_UNAVAILABLE_TEXT)
    elif empty_diagnosis:
        parts.append("Empty-result diagnosis: " + ("No matching scoped source data for this request." if empty_diagnosis == "valid_empty"
                                                 else "No matching data was released; diagnosis could not finish within the remaining budget."))
    for i, item in enumerate(evidence, 1):
        parts.append(f"Evidence E{i}: {item.operation.value}; period {item.period_utc or item.metric_definition.get('cutoff_date')}; "
                     f"scope {item.scope_reference} version {item.scope_version}; job {item.job_id}; "
                     f"coverage job {item.coverage.job_id}.")
        execution_scope = getattr(item, "execution_scope", None)
        if execution_scope is not None:
            brands = json.dumps(execution_scope.brands, ensure_ascii=False)
            products = (json.dumps(execution_scope.product_ids) if execution_scope.product_ids is not None
                        else "all products within these brands")
            parts.append(f"Scope filters for E{i}: brands {brands}; product IDs {products}.")
        else:
            parts.append(f"Scope filters for E{i}: unavailable in this evidence's metadata.")
        if protected_unavailable(item):
            parts.append("  " + PROTECTED_UNAVAILABLE_TEXT)
        elif empty_diagnosis and (item.coverage.period_rows == 0 or not item.rows):
            parts.append("  No matching data; aggregate zero is not treated as an observed result.")
        elif item.rows:
            show_table = composition is None or any(s.kind == "performance_table" for s in composition.sections)
            if show_table:
                rows = item.rows[:2] if composition and composition.spec.detail == "concise" else item.rows
                if composition and composition.spec.presentation == "table":
                    parts.append("  " + " | ".join(item.columns))
                    parts.extend("  " + " | ".join(str(row.get(key)) for key in item.columns) for row in rows)
                else:
                    for row in rows:
                        parts.append("  " + ", ".join(f"{key}={value}" for key, value in row.items()))
                if len(rows) < len(item.rows):
                    parts.append("  Additional released rows are retained in evidence; concise display only.")
        else:
            parts.append("  No qualifying rows.")
        parts.append(f"Definitions for E{i}: {json.dumps(item.metric_definition, sort_keys=True)}")
        parts.append(f"Completeness: {'complete' if item.complete else 'incomplete'} — {item.completeness_note}")
        parts.append(f"Coverage: {item.coverage.note} Latest scoped item: {item.coverage.latest_item_at_utc or 'unknown'}; query as of {item.query_as_of_utc}.")
    for hypothesis in grounded.hypotheses:
        parts.append(f"Hypothesis (unverified): {hypothesis.text}")
    if grounded.recommendations and composition is None:
        label = action_heading(resolved_request)
        parts.append(label + " (recommendations, not observed outcomes):")
        for action in grounded.recommendations:
            parts.append(f"  {action.text}")
    if grounded.driver and composition is None:
        from .playbooks.base import text as driver_text
        parts.append(driver_text(grounded.driver))
    if draft_error or grounded.selection_status == "refused":
        parts.append("Model explanation unavailable; checked evidence and supported deterministic findings are shown above. "
                     "No model-selected actions were released.")
    if not evidence:
        return "\n".join(parts)
    definitions = evidence[0].metric_definition
    statuses = ", ".join(definitions.get("revenue_statuses", ()))
    days = definitions.get("inactivity_days", 90)
    parts.append(f"Definitions: revenue sums non-null item sale prices for {statuses}; "
                 "other statuses and null-priced items are excluded. UTC item creation time uses an inclusive start and "
                 f"exclusive end. Processing, when included, is an analysis convention, not an accounting-recognition claim. "
                 f"Churn is {days}-day inactivity among prior purchasers in the effective brands. "
                 "Demographics are aggregate-only with at least five customers per released group.")
    return "\n".join(parts)



class GoldenText(StrictModel):
    trio_id: str = Field(pattern=r'^GK-[0-9]{3}$')
    version: int = Field(ge=1)
    method: Literal['monthly_revenue_trend']

    def selection(self) -> Selection:
        return Selection(self.trio_id, self.version, self.method, METHODS[self.method])


class ReportTextContract(StrictModel):
    """Only finite rendering switches, never saved factual text or an opaque hash."""
    version: Literal[1] = 1
    mode: Literal['fresh', 'exact_save'] = 'fresh'
    golden_status: Literal['none', 'no_match', 'outage', 'selected'] = 'none'
    golden: GoldenText | None = None
    empty_diagnosis: Literal['valid_empty', 'incomplete', 'unavailable'] | None = None

    @model_validator(mode='after')
    def coherent(self):
        if ((self.golden_status == 'selected') != (self.golden is not None)
                or self.mode == 'exact_save' and (self.golden_status != 'none' or self.empty_diagnosis is not None)):
            raise ValueError('invalid_report_rendering')
        return self

    @classmethod
    def capture(cls, *, golden_status='none', golden=None, empty_diagnosis=None):
        provenance = None
        if golden is not None:
            if golden.method not in METHODS or golden.summary != METHODS[golden.method]:
                raise PolicyError('invalid_result_contract')
            provenance = GoldenText(trio_id=golden.trio_id, version=golden.version, method=golden.method)
        return cls(golden_status=golden_status, golden=provenance, empty_diagnosis=empty_diagnosis)

    def render(self, snapshot) -> str:
        protected = any(protected_unavailable(e) for e in snapshot.evidence)
        empty = any(not e.rows or e.coverage.period_rows == 0
            or e.operation == 'churn_snapshot' and e.rows[0].get('eligible_customers') == 0 for e in snapshot.evidence)
        if (self.empty_diagnosis == 'unavailable' and not protected
                or self.empty_diagnosis in {'valid_empty', 'incomplete'} and (protected or not empty)):
            raise PolicyError('invalid_result_contract')
        content = render_v1(snapshot.evidence, ReportDraft(),
            'selection_unavailable' if snapshot.grounded.selection_status == 'fallback' else None,
            report_requested=True, golden_status=self.golden_status,
            golden=self.golden.selection() if self.golden else None,
            empty_diagnosis=self.empty_diagnosis, resolved_request=snapshot.request, grounded=snapshot.grounded)
        return content + reuse_suffix(snapshot) if self.mode == 'exact_save' else content


def reuse_suffix(snapshot) -> str:
    return '\nReused released evidence; original as-of UTC: ' + ', '.join(e.query_as_of_utc for e in snapshot.evidence) + '.'


def historical_contract(content: str, snapshot) -> ReportTextContract:
    """Compatible pre-contract v1 reader, not an unchecked legacy fallback.

    Recover only the original finite nonfactual switches from exact known lines;
    all factual strings must still equal rendering from the validated support.
    Unknown/additional text or formats fail equality. No rows are migrated.
    """
    if content.endswith(reuse_suffix(snapshot)):
        return ReportTextContract(mode='exact_save')
    lines = content.splitlines()
    status, golden, diagnosis = 'none', None, None
    for line in lines:
        if line == 'Golden Knowledge: no compatible approved example available; using the supported analysis path.':
            status = 'no_match'
        for method, summary in METHODS.items():
            match = re.fullmatch(r'Golden Knowledge: synthetic authored (GK-[0-9]{3}) v([1-9][0-9]*) selected; '
                + re.escape(summary) + r' Fresh scoped warehouse evidence supplies all figures\.', line)
            if match:
                status = 'selected'
                golden = GoldenText(trio_id=match[1], version=int(match[2]), method=method)
        if line == PROTECTED_UNAVAILABLE_TEXT:
            diagnosis = 'unavailable'
        elif line == 'Empty-result diagnosis: No matching scoped source data for this request.':
            diagnosis = 'valid_empty'
        elif line == 'Empty-result diagnosis: No matching data was released; diagnosis could not finish within the remaining budget.':
            diagnosis = 'incomplete'
    return ReportTextContract(golden_status=status, golden=golden, empty_diagnosis=diagnosis)


def validate_report_text(content: str, snapshot, contract: ReportTextContract | None) -> None:
    reader = contract if contract is not None else historical_contract(content, snapshot)
    if reader.render(snapshot) != content:
        raise PolicyError('invalid_result_contract')
