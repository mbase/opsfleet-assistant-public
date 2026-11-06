"""Bounded LangGraph request path over the trusted Step 3 adapter."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from .bigquery_adapter import BigQueryTurn, ExecutionError, QueryLimits
from .compiler import CompiledQuery, compile_plan
from .config import ROOT, Settings
from .deletion import parse_deletion
from .contracts import (AnalysisPlan, CohortOrigin, Dimension, Metric, Operation,
                        InterpretationResult, ResolvedRequest, RequestSpec, Mode as RequestMode)
from .evidence import Evidence
from .presentation import display_payload
from .conversation import (AnalysisSnapshot, ConversationContext, SafeMessage, history, model_context, safe_spec, validate_cached_evidence, validate_snapshot_request, PendingTemporalResolution, request_role_anchors)
from .privacy import FIELD_POLICY_VERSION
from .identity import PolicyError, resolve_demo_scope, effective_brands, authorize_products
from .model_gateway import (GeminiGateway, Mode, ModelBudget, ModelFailure, PlanEnvelope,
                            ReportDraft)
from .safety import screen_question
from .report_artifact import (ReportArtifact, artifact_binding_digest, validate_artifact,
                              reconstruct_grounded, evidence_references)
from .reporting import (GroundedReport, ground_report, ReportComposition, compose_report, apply_report_preferences,
                        composition_text, action_heading, paired_coverage, report_spec)
from pydantic import ValidationError
from .store import SQLiteStore, StoreFailure
from .golden import Selection, RetrievalFailure, apply_method, retrieve
from .intent import (ISSUE_TEXT, analytical_plans, comparison_choices_missing,
                     resolve_interpretation, validate_bound_plans)
from .telemetry import TurnTrace


def _save_modifies_snapshot(interpretation, snapshot, scope, catalog, reference_date, previous_plan):
    """Conservative typed save boundary: omissions inherit, real differences clarify.

    This does not infer meaning from user words or perform a revised analysis.
    Equivalent resolved periods and reordered status sets remain exact saves.
    """
    from .periods import resolve_request, ResolutionFailure
    proposed = interpretation.request
    original = snapshot.request.spec
    if interpretation.clear_fields or interpretation.temporal_updates:
        return True  # Explicit reset/refresh is not an exact snapshot save.
    if proposed.mode not in {RequestMode.ANALYSIS, RequestMode.REPORT, original.mode}:
        return True
    if proposed.report is not None and proposed.report != report_spec(snapshot.request):
        return True
    if proposed.purpose != "standard" and proposed.purpose != original.purpose:
        return True
    if proposed.queries:
        if len(proposed.queries) != len(original.queries):
            return True
        def choice(query):
            value = query.model_dump(mode="json")
            value["requested_brands"] = sorted(effective_brands(scope, query.requested_brands, catalog))
            value["revenue_statuses"] = sorted(value["revenue_statuses"])
            if query.operation != Operation.PRODUCT_COMPARISON and value["product_ids"] is not None:
                value["product_ids"] = sorted(value["product_ids"])
            return value
        if any(choice(a) != choice(b) for a, b in zip(proposed.queries, original.queries)):
            return True
    updates = {role: getattr(proposed, role) for role in ("analysis", "comparison", "action")
               if getattr(proposed, role) is not None}
    if updates:
        candidate = original.model_copy(update=updates)
        anchors = request_role_anchors(candidate, previous_spec=original,
            previous_anchors=snapshot.temporal_anchors(), context_action="correct",
            reference_date=reference_date, previous_plan=previous_plan)
        try:
            resolved = resolve_request(candidate, reference_date=reference_date,
                analytical_timezone=snapshot.request.analytical_timezone,
                previous_plan=previous_plan, role_anchors=anchors)
        except (ResolutionFailure, ValueError):
            return True  # Unresolvable modifications must never save old evidence.
        if any(getattr(resolved, role) != getattr(snapshot.request, role)
               for role in ("analysis", "comparison", "action")):
            return True
    return False


SCHEMA_HELP = (
    "Available sources: orders (order ID, customer ID, status), order_items "
    "(order ID, product ID, item status, sale price, creation time), products "
    "(product ID, brand), and users (aggregate state, city, country, age, gender, "
    "traffic source). Orders join to items by order ID, items to products by "
    "product ID, and orders to users by customer ID. Supported analyses include "
    "scoped revenue, customer ranking, monthly trends, product comparison, "
    "aggregate demographics, and inactivity churn. Names, contact details, "
    "addresses, exact coordinates, and demographic membership IDs are unavailable."
)

SAFE_ERROR_CODES = frozenset({
    "aggregate_only_source_field", "backup_failure", "backup_transient", "backup_unconfigured",
    "billing_unknown", "brand_not_available", "compiler_syntax_error", "coverage_invalid",
    "customer_id_violation", "demographic_complement_violation", "demographic_group_too_small",
    "demographic_membership_blocked", "demographic_membership_violation", "direct_failure",
    "direct_transient", "direct_unconfigured", "dry_run_limit", "empty_model_output",
    "evidence_scope_mismatch", "idempotency_conflict", "incomplete_comparison",
    "instruction_attack_blocked", "invalid_catalog", "invalid_identity_config", "invalid_job_metadata",
    "invalid_model_json", "invalid_interpretation_contract", "invalid_parameter", "invalid_plan_contract", "invalid_proposal",
    "invalid_question", "invalid_report_contract", "invalid_request", "invalid_result_contract",
    "invalid_selector", "job_byte_limit", "missing_project", "model_request_limit",
    "permission_denied", "pii_request_blocked", "plan_intent_mismatch", "plan_investigation_mismatch",
    "plan_limit_mismatch", "plan_metric_mismatch", "plan_period_mismatch", "plan_product_mismatch",
    "plan_repair_limit", "plan_scope_mismatch", "plan_status_mismatch", "product_not_available",
    "prohibited_source_field", "prompt_limit", "provider_failure", "provider_transient",
    "query_count_limit", "query_provider_rejected", "query_schema_error", "query_syntax_error",
    "recovery_scope_changed", "report_deleted", "result_empty_violation", "result_limit",
    "result_null_violation", "result_or_billing_limit", "result_schema_violation", "result_scope_violation",
    "result_type_violation", "saved_scope_changed", "scope_changed_start_new_conversation",
    "selection_too_broad", "store_unavailable", "timeout_cancellation_requested", "truncated_model_output",
    "turn_byte_limit", "turn_timeout", "unauthorized_brand", "unauthorized_scope",
    "unknown_identity", "unsafe_report_text", "unsafe_result_text", "unsupported_cohort_reference",
    "unsupported_operation", "unsupported_recommendation", "unsupported_request", "untrusted_compiled_query",
})


class TurnState(TypedDict, total=False):
    question: str
    owner: str
    conversation_id: str
    turn_id: str
    previous_plan: AnalysisPlan | None
    previous_cohort: CohortOrigin | None
    previous_scope_version: int | None
    interpretation: InterpretationResult
    resolved_request: ResolvedRequest
    report_requested: bool
    plan_envelope: PlanEnvelope
    queries: tuple[CompiledQuery, ...]
    executed_plans: tuple[AnalysisPlan, ...]
    evidence: tuple[Evidence, ...]
    draft: ReportDraft
    draft_error: str | None
    grounded_report: GroundedReport
    report_composition: ReportComposition
    content: str
    report_id: int | None
    scope_version: int
    request_digest: str
    identity_digest: str
    golden: Selection | None
    golden_status: str
    empty_diagnosis: str | None
    contextual_only: bool
    resolution_role_anchors: dict


@dataclass(frozen=True)
class TurnResult:
    status: str
    text: str
    conversation_id: str
    turn_id: str
    model_routes: tuple[str, ...] = ()
    model_requests: int = 0
    query_executions: int = 0
    billed_bytes: int | None = 0
    evidence_job_ids: tuple[str, ...] = ()
    report_id: int | None = None
    reused_report: bool = False
    trace_id: str = ""
    golden_status: str = "none"
    golden_trio_id: str | None = None
    golden_method: str | None = None
    error_category: str | None = None
    interpretation_status: str | None = None
    resolved_periods: dict[str, Any] | None = None
    factual_status: str | None = None
    factual_findings: tuple[dict[str, Any], ...] = ()
    suggestions: tuple[dict[str, Any], ...] = ()
    report_completeness: str | None = None
    report_composition: dict[str, Any] | None = None
    presentation: dict[str, Any] | None = None


def _validate_draft(draft: ReportDraft, evidence: tuple[Evidence, ...]) -> GroundedReport:
    return ground_report(draft, evidence)


def _render(evidence: tuple[Evidence, ...], draft: ReportDraft, draft_error: str | None,
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
    if empty_diagnosis:
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
        if empty_diagnosis and (item.coverage.period_rows == 0 or not item.rows):
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


def _scope_digest(queries: tuple[CompiledQuery, ...]) -> str:
    brand_sets = [list(query.effective_brands) for query in queries]
    return hashlib.sha256(json.dumps(brand_sets, separators=(",", ":")).encode("utf-8")).hexdigest()


class AssistantWorkflow:
    def __init__(self, settings: Settings, store: SQLiteStore,
                 *, gateway_factory: Any = GeminiGateway, turn_factory: Any = BigQueryTurn,
                 today: date | None = None, analytical_timezone: str = "UTC"):
        if analytical_timezone != "UTC":
            raise ValueError("unsupported_analytical_timezone")
        self.analytical_timezone = analytical_timezone
        self.settings, self.store = settings, store
        self.gateway_factory, self.turn_factory = gateway_factory, turn_factory
        self.today = today

    def run(self, owner: str, question: str, *, conversation_id: str | None = None,
            turn_id: str | None = None) -> TurnResult:
        supplied_conversation = conversation_id
        conversation_id = conversation_id or str(uuid4())
        turn_id = turn_id or str(uuid4())
        trace = TurnTrace(conversation_id, turn_id)
        trace.emit("turn_start", status="start")
        result = self._run_impl(owner, question, conversation_id=conversation_id,
                                turn_id=turn_id, trace=trace, supplied_conversation=supplied_conversation)
        category = result.error_category or ("success" if result.status == "ok" else result.status)
        trace.finish(result.status, category=category, requests=result.model_requests,
                     executions=result.query_executions, billed_bytes=result.billed_bytes)
        return replace(result, trace_id=trace.trace_id)

    def _run_impl(self, owner: str, question: str, *, conversation_id: str,
                  turn_id: str, trace: TurnTrace, supplied_conversation: str | None) -> TurnResult:
        command = parse_deletion(question)
        if command is not None:
            trace.emit("stage", stage=("deletion_preview" if command.action == "preview" else
                                       "deletion_cancel" if command.action == "cancel" else "deletion_confirm"), status="start")
            boundary = " --user is a local demo identity selector, not authentication."
            try:
                if command.action == "ambiguous":
                    return TurnResult("clarification", "Use 'Delete all reports mentioning <literal>' or "
                                      "'Delete all the reports we made in this conversation'." + boundary,
                                      conversation_id, turn_id)
                if command.action == "preview":
                    if command.selector == "conversation" and not supplied_conversation:
                        return TurnResult("clarification", "Provide --conversation with the exact conversation ID." + boundary,
                                          conversation_id, turn_id)
                    value = supplied_conversation if command.selector == "conversation" else command.value
                    assert value is not None
                    preview = self.store.preview_deletion(owner, command.selector, value,
                                                          conversation_id=supplied_conversation)
                    if not preview["targets"]:
                        trace.emit("deletion", decision="no_targets", status="ok", count=0)
                        return TurnResult("ok", "No active owned reports match. Nothing was deleted; no proposal was created." + boundary,
                                          conversation_id, turn_id)
                    rule = (f"case-insensitive literal substring of saved title/body: {command.value!r}"
                            if command.selector == "literal" else f"exact conversation ID: {value}")
                    lines = [f"Deletion preview: {len(preview['targets'])} active owned report(s).",
                             f"Matching rule: {rule}"]
                    lines.extend(f"  ID {item['id']} (version {item['version']}): {item['title']}"
                                 for item in preview["targets"])
                    lines.extend([f"Expires: {preview['expires_at']}",
                                  f"To delete exactly these versions: confirm {preview['token']}",
                                  f"To cancel: cancel {preview['token']}",
                                  "Deletion is soft: reports disappear from active use immediately, but bytes remain in SQLite pending a separate retention/purge policy." + boundary])
                    trace.emit("deletion", decision="previewed", status="confirmation_required", count=len(preview["targets"]))
                    return TurnResult("confirmation_required", "\n".join(lines), conversation_id, turn_id)
                state, ids = self.store.resolve_deletion(owner, command.value, cancel=command.action == "cancel")
                trace.emit("deletion", decision=state if state in {"deleted", "cancelled", "stale", "expired"} else "failed",
                           status="ok" if state in {"deleted", "cancelled"} else "error", count=len(ids))
                if state == "deleted":
                    message = f"Soft-deleted {len(ids)} report(s): {', '.join(map(str, ids))}. They are no longer active; bytes remain in SQLite pending a retention/purge policy."
                    return TurnResult("ok", message + boundary, conversation_id, turn_id)
                if state == "cancelled":
                    return TurnResult("ok", "Deletion proposal cancelled. No reports changed." + boundary,
                                      conversation_id, turn_id)
                if state == "stale":
                    message = "A previewed report changed or disappeared. Nothing was deleted; request a fresh preview."
                elif state == "expired":
                    message = "The five-minute proposal expired. Nothing was deleted; request a fresh preview."
                else:
                    message = "No active deletion proposal matches this token and identity. Nothing was deleted."
                return TurnResult("error", message + boundary, conversation_id, turn_id)
            except StoreFailure as exc:
                trace.emit("deletion", decision="failed", status="error", category="storage")
                message = ("Selection is too broad; narrow the request." if exc.code == "selection_too_broad"
                           else "Unable to complete report deletion: local state store unavailable.")
                return TurnResult("error", message + boundary, conversation_id, turn_id)
        reference_date = self.today or datetime.now(timezone.utc).date()
        budget = ModelBudget()
        budget.telemetry = trace
        gateway = self.gateway_factory(self.settings, budget=budget)
        bq: BigQueryTurn | None = None
        selected_golden: Selection | None = None
        golden_marker = "none"
        active_stage = "privacy_screen"

        def mark_stage(name: str) -> None:
            nonlocal active_stage
            active_stage = name
            budget.remaining()
            trace.emit("stage", stage=name, status="start")

        try:
            context = self.store.load_continuity(owner, conversation_id)
            # Legacy plans have no compatibility/expiry metadata and are unavailable
            # as execution context. Cohort provenance remains a conservative safety hint.
            try:
                _, legacy_cohort, _ = self.store.load_context(owner, conversation_id)
            except StoreFailure:
                legacy_cohort = None
            cohort = context.cohort_origin if context else legacy_cohort
            previous = context.snapshot.plans[0] if context and context.snapshot else None
            scope_version = context.authorization_version if context else None
            try:
                clean_question = screen_question(question, prior_demographic=cohort == CohortOrigin.DEMOGRAPHIC)
                if len(clean_question) > self.settings.input_max_chars:
                    raise PolicyError("invalid_question")
            except PolicyError:
                # An attack cannot leave an unfinished request armed for a late reply.
                if context and context.pending:
                    context = context.model_copy(update={"pending": None, "pending_resolution": None, "issues": (), "messages": ()})
                    self.store.save_continuity(owner, conversation_id, context)
                raise
            request_digest = hashlib.sha256((turn_id + "\0" + clean_question).encode("utf-8")).hexdigest()
            try:
                identity_digest = hashlib.sha256((ROOT / "config" / "demo_users.json").read_bytes()).hexdigest()
            except OSError:
                raise PolicyError("invalid_identity_config") from None
            reused = self.store.find_retry(owner, conversation_id, turn_id,
                                           request_digest=request_digest, identity_digest=identity_digest)
            if reused is not None:
                if reused.get("deleted"):
                    return TurnResult("deleted", "This saved report was soft-deleted and cannot be replayed. Start a new turn for a new report.",
                                      conversation_id, turn_id)
                bq = self.turn_factory(self.settings, limits=QueryLimits(max_executions=8,
                                             turn_seconds=max(0.01, budget.remaining())))
                bq.telemetry = trace
                catalog = bq.load_catalog()
                scope = resolve_demo_scope(owner, catalog)
                if scope.authorization_version != reused["scope_version"] or not reused["plans"]:
                    raise PolicyError("saved_scope_changed")
                current_queries = tuple(compile_plan(AnalysisPlan.model_validate(raw), scope, catalog)
                                        for raw in reused["plans"])
                if _scope_digest(current_queries) != reused["scope_digest"]:
                    raise PolicyError("saved_scope_changed")
                stored_grounded = GroundedReport.model_validate(reused["grounded_report"]) if reused.get("grounded_report") else None
                fields = reused["fields"]
                durable = bool(fields["report_support_json"] or fields["report_binding_digest"])
                stored_composition = None
                if durable:
                    if (not fields["report_support_json"] or not fields["report_binding_digest"]
                            or artifact_binding_digest(fields) != fields["report_binding_digest"]):
                        raise StoreFailure("invalid_result_contract")
                    artifact = ReportArtifact.model_validate_json(fields["report_support_json"])
                    stored_composition = ReportComposition.model_validate(reused["report_composition"])
                    validate_artifact(fields, artifact, stored_composition, stored_grounded)
                    for query, item in zip(current_queries, artifact.snapshot.evidence):
                        if (item.metric_definition != query.metric_definition or item.columns != query.result_contract.columns
                                or item.scope_reference != f"demo:{owner}" or item.scope_version != query.scope_version
                                or item.field_policy_version != FIELD_POLICY_VERSION
                                or item.execution_scope is None or item.execution_scope.brands != query.effective_brands
                                or item.execution_scope.product_ids != query.requested_product_ids):
                            raise StoreFailure("invalid_result_contract")
                        validate_cached_evidence(query, item)
                else:
                    # Pre-binding artifacts retain exact content. Missing durable
                    # support cannot verify new composition or completeness.
                    reused["report_completeness"] = "legacy"
                return TurnResult("partial" if reused["report_completeness"] == "partial" or reused["content"].startswith("Partial report:") else "ok", reused["content"], conversation_id, turn_id,
                                  query_executions=bq.executions, billed_bytes=bq.billed_bytes,
                                  evidence_job_ids=tuple(ref["job_id"] for ref in reused["evidence_refs"]),
                                  report_id=reused["id"], reused_report=True,
                                  report_completeness=reused["report_completeness"],
                                  presentation=display_payload(artifact.snapshot.evidence, stored_grounded,
                                      artifact.snapshot.request) if durable else None,
                                  report_composition=stored_composition.model_dump(mode="json") if stored_composition else None,
                                  resolved_periods={role: getattr(stored_composition.request, role).model_dump(mode="json")
                                      if getattr(stored_composition.request, role) else None
                                      for role in ("analysis", "comparison", "action")} if stored_composition else None,
                                  factual_status=stored_grounded.factual_status if stored_grounded else None,
                                  factual_findings=tuple(f.model_dump(mode="json") for f in stored_grounded.findings) if stored_grounded else (),
                                  suggestions=tuple({"kind": kind, **s.model_dump(mode="json")} for kind in ("recommendations", "hypotheses")
                                                    for s in getattr(stored_grounded, kind)) if stored_grounded else ())
            active_scope = None
            context_queries = ()
            catalog_digest = ""

            def updated_context(*, pending=None, pending_resolution=None, issues=(), snapshot=None, clarification=None,
                                preserve_expiry=False, reset_history=False):
                assert active_scope is not None
                now = self.store._now()
                messages = (context.messages if context and not reset_history else ()) + (SafeMessage(role="user", content=clean_question),)
                if clarification:
                    messages += (SafeMessage(role="assistant", content=clarification),)
                messages = history(messages, max_messages=self.settings.history_max_messages,
                                   max_chars=self.settings.history_max_chars)
                demographic = cohort == CohortOrigin.DEMOGRAPHIC
                if pending and any(q.operation == Operation.DEMOGRAPHIC_COMPARISON for q in pending.queries):
                    demographic = True
                if snapshot and any(p.operation == Operation.DEMOGRAPHIC_COMPARISON for p in snapshot.plans):
                    demographic = True
                return ConversationContext(owner=owner, identity_digest=identity_digest,
                    authorization_version=active_scope.authorization_version, catalog_digest=catalog_digest,
                    identity_brands=tuple(sorted(active_scope.brands)), identity_role=active_scope.role.value,
                    created_at=context.created_at if preserve_expiry and context else now,
                    expires_at=context.expires_at if preserve_expiry and context else now + timedelta(seconds=self.settings.context_ttl_seconds),
                    cohort_origin=CohortOrigin.DEMOGRAPHIC if demographic else CohortOrigin.BUSINESS,
                    messages=messages, pending=safe_spec(pending), pending_resolution=pending_resolution,
                    issues=issues, snapshot=snapshot)

            initial: TurnState = {"question": clean_question, "owner": owner,
                                  "conversation_id": conversation_id, "turn_id": turn_id,
                                  "previous_plan": previous, "previous_cohort": cohort,
                                  "previous_scope_version": scope_version,
                                  "request_digest": request_digest, "identity_digest": identity_digest}
            graph = StateGraph(TurnState)

            def screen(state: TurnState) -> dict[str, Any]:
                mark_stage("privacy_screen")
                return {"question": screen_question(state["question"], prior_demographic=state.get("previous_cohort") == CohortOrigin.DEMOGRAPHIC)}

            def resolve(state: TurnState) -> dict[str, Any]:
                nonlocal bq, context, active_scope, context_queries, catalog_digest
                mark_stage("intent_metric_resolution")
                # Authorize prior context before including it in a model prompt.
                # This catalog work is charged to the same warehouse turn budget.
                bq = self.turn_factory(self.settings, limits=QueryLimits(max_executions=8,
                                             turn_seconds=max(0.01, budget.remaining())))
                bq.telemetry = trace
                catalog = bq.load_catalog()
                scope = resolve_demo_scope(owner, catalog)
                active_scope = scope
                catalog_digest = hashlib.sha256(json.dumps(sorted(catalog.product_brands.items()),
                                                           separators=(",", ":")).encode()).hexdigest()
                if context:
                    compatible = (context.identity_digest == identity_digest and context.catalog_digest == catalog_digest
                        and context.authorization_version == scope.authorization_version
                        and context.identity_brands == tuple(sorted(scope.brands))
                        and context.identity_role == scope.role.value)
                    if not compatible:
                        self.store.clear_continuity(owner, conversation_id)
                        raise PolicyError("scope_changed_start_new_conversation")
                    for message in context.messages:
                        screen_question(message.content)
                    context = context.model_copy(update={"messages": history(context.messages,
                        max_messages=self.settings.history_max_messages, max_chars=self.settings.history_max_chars)})
                    if context.pending:
                        if any(getattr(context.pending, role) for role in ("analysis", "comparison", "action")) and context.pending_resolution is None:
                            context = context.model_copy(update={"pending": None, "pending_resolution": None, "issues": (), "messages": ()})
                            self.store.save_continuity(owner, conversation_id, context)
                            return {"interpretation": InterpretationResult(status="needs_clarification", issues=("unresolved_interpretation",)),
                                    "content": "The unfinished request has no valid original analytical date context. Please restate its period and metric."}
                        safe_spec(context.pending)
                        for q in context.pending.queries:
                            brands = effective_brands(scope, q.requested_brands, catalog)
                            authorize_products(q.product_ids, brands, catalog)
                    if context.snapshot:
                        snapshot = context.snapshot
                        if self.store._now() >= snapshot.created_at + timedelta(seconds=self.settings.context_ttl_seconds):
                            context = context.model_copy(update={"snapshot": None})
                            self.store.save_continuity(owner, conversation_id, context)
                            state["previous_plan"] = None
                        else:
                            try:
                                validate_snapshot_request(snapshot)
                            except PolicyError:
                                self.store.clear_continuity(owner, conversation_id)
                                raise
                            context_queries = tuple(compile_plan(p, scope, catalog) for p in snapshot.plans)
                            for query, item in zip(context_queries, snapshot.evidence):
                                if (item.scope_reference != f"demo:{owner}" or item.scope_version != scope.authorization_version
                                        or item.field_policy_version != FIELD_POLICY_VERSION
                                        or item.execution_scope is None
                                        or item.execution_scope.brands != query.effective_brands
                                        or item.execution_scope.product_ids != query.requested_product_ids
                                        or item.metric_definition != query.metric_definition
                                        or item.columns != query.result_contract.columns):
                                    self.store.clear_continuity(owner, conversation_id)
                                    raise PolicyError("saved_scope_changed")
                                try:
                                    validate_cached_evidence(query, item)
                                except PolicyError:
                                    self.store.clear_continuity(owner, conversation_id)
                                    raise
                            # Recompute trusted W3 sentences from typed selections, never report prose.
                            checked = reconstruct_grounded(snapshot)
                            context = context.model_copy(update={"snapshot": snapshot.model_copy(update={"grounded": checked})})
                # Exact catalog-name screening is an authorization defense, not an intent parser.
                for brand in catalog.brands:
                    if (re.search(r"(?<!\w)" + re.escape(brand.casefold()) + r"(?!\w)", state["question"].casefold())
                            and brand not in scope.brands):
                        raise PolicyError("unauthorized_brand")
                feedback = ""
                save_repair_pending = False

                def clarify_modified_save():
                    content = ("I could not confirm an unchanged save. "
                        "Please request any analysis or report changes separately, review the result, then ask to save it. "
                        "No report was saved.")
                    self.store.save_continuity(owner, conversation_id, updated_context(
                        snapshot=context.snapshot, clarification=content, preserve_expiry=True))
                    return {"interpretation": InterpretationResult(status="needs_clarification",
                                context_action="continue", issues=("unresolved_interpretation",)),
                            "contextual_only": True, "content": content}

                for attempt in range(3):
                    try:
                        interpretation = gateway.interpret(
                            state["question"], previous_plan=state.get("previous_plan"),
                            reference_date=reference_date.isoformat(), analytical_timezone=self.analytical_timezone,
                            feedback=feedback, conversation_context=model_context(context),
                            pending_request=context.pending if context else None)
                        if getattr(gateway, "interpretation_context_overflow", False):
                            content = ("This request and its required context are too long to process safely. "
                                "Please restate the request more briefly or start a new analysis. No analysis or report was created.")
                            self.store.save_continuity(owner, conversation_id, updated_context(
                                pending=context.pending if context else None,
                                pending_resolution=context.pending_resolution if context else None,
                                issues=context.issues if context else (), snapshot=context.snapshot if context else None,
                                clarification=content, preserve_expiry=bool(context)))
                            return {"interpretation": interpretation, "contextual_only": True, "content": content}
                        if interpretation.request:
                            interpretation = interpretation.model_copy(update={"request": safe_spec(interpretation.request)})
                        # A save repair must not turn into an executable modified analysis
                        # or arm a pending save that a later bare confirmation could run.
                        if save_repair_pending and not (interpretation.status == "ready"
                                and interpretation.request and interpretation.request.intent == "save"):
                            return clarify_modified_save()
                        if (interpretation.status == "ready" and interpretation.request
                                and interpretation.request.intent == "save" and context and context.snapshot
                                and _save_modifies_snapshot(interpretation, context.snapshot, scope, catalog,
                                    reference_date, state.get("previous_plan"))):
                            # Only ambiguous report choices are eligible. This temporary
                            # projection detects other differences; it is never executed
                            # or saved, and the actual proposal remains unchanged.
                            without_report = interpretation.model_copy(update={"request":
                                interpretation.request.model_copy(update={"report": None})})
                            if (not save_repair_pending and attempt < 2
                                    and interpretation.request.report is not None
                                    and not _save_modifies_snapshot(without_report, context.snapshot, scope, catalog,
                                        reference_date, state.get("previous_plan"))):
                                save_repair_pending = True
                                budget.repairs += 1
                                trace.emit("recovery", decision="repair", category="policy")
                                feedback = (
                                    "Reinterpret the original current user message: the save proposal changes report choices. "
                                    "Determine whether the user explicitly requested these changes or they were added by the interpreter. "
                                    "For an unchanged save return only the compact save intent, null report and periods, empty queries, "
                                    "sources, clear_fields and temporal_updates. Inherit snapshot choices; do not restate defaults. "
                                    "Never discard explicit user changes: retain the full revised request when changes were requested; "
                                    "they require a separate request before saving. Clarify unresolved meaning. "
                                    "Typed report choices (data): " + json.dumps({
                                        "snapshot_report": report_spec(context.snapshot.request).model_dump(mode="json"),
                                        "proposed_report": interpretation.request.report.model_dump(mode="json")},
                                        separators=(",", ":")))
                                continue
                            return clarify_modified_save()
                        # A numeric year is a narrow format consistency check, not a language parser.
                        bare_year = bool(re.fullmatch(r"[1-9][0-9]{3}", state["question"]))
                        if bare_year and not (context and (context.pending or context.snapshot)):
                            interpretation = InterpretationResult(status="needs_clarification",
                                request=RequestSpec(analysis=interpretation.request.analysis if interpretation.request else None),
                                issues=("missing_metric",))
                        if bare_year and context and context.pending and interpretation.request and interpretation.request.intent == "analysis":
                            interpretation = interpretation.model_copy(update={"context_action": "continue", "temporal_updates": (), "clear_fields": ()})
                        # Continuing a clarification cannot silently discard supplied choices.
                        continuity_spec = None
                        if context:
                            continuity_spec = context.pending or (context.snapshot.request.spec if context.snapshot else None)
                        if (continuity_spec and (interpretation.context_action in {"continue", "correct"} or bare_year)
                                and interpretation.request and interpretation.request.intent == "analysis"):
                            old, new = continuity_spec, interpretation.request
                            # A choice correction does not cancel a pending report
                            # merely because the model used the default analysis mode.
                            if interpretation.context_action == "correct" and old.mode == Mode.REPORT and new.mode == Mode.ANALYSIS:
                                new = new.model_copy(update={"mode": old.mode})
                                interpretation = interpretation.model_copy(update={"request": new})
                            if old.action is not None and new.action is None and "action" not in interpretation.clear_fields:
                                new = new.model_copy(update={"action": old.action})
                                interpretation = interpretation.model_copy(update={"request": new})
                            retained_report = old.report or (report_spec(old) if old.mode == Mode.REPORT else None)
                            if retained_report is not None and "report" not in interpretation.clear_fields:
                                if new.report is None:
                                    new = new.model_copy(update={"report": retained_report})
                                    interpretation = interpretation.model_copy(update={"request": new})
                                elif new.report != retained_report and interpretation.context_action != "correct":
                                    raise PolicyError("plan_intent_mismatch")
                            if context and context.pending and interpretation.context_action != "correct":
                                inherited = {}
                                for role in ("analysis", "comparison", "action"):
                                    before, after = getattr(old, role), getattr(new, role)
                                    if before is not None and after is None:
                                        inherited[role] = before
                                    elif before is not None and after != before:
                                        filled_year = (before.kind in {"month", "quarter", "year"}
                                            and before.year is None and after is not None
                                            and before.model_dump(exclude={"year"}) == after.model_dump(exclude={"year"}))
                                        if not filled_year:
                                            raise PolicyError("plan_period_mismatch")
                                new = new.model_copy(update=inherited)
                                interpretation = interpretation.model_copy(update={"request": new})
                            if interpretation.context_action != "correct":
                                if new.purpose != old.purpose:
                                    raise PolicyError("plan_intent_mismatch")
                                if new.mode != old.mode and new.mode != Mode.TEMPORAL_COMPARE:
                                    raise PolicyError("plan_intent_mismatch")
                                if old.queries and not new.queries:
                                    raise PolicyError("plan_intent_mismatch")
                                if len(new.queries) < len(old.queries):
                                    raise PolicyError("plan_intent_mismatch")
                                if bare_year and old.analysis and old.analysis.kind in {"month", "quarter", "year"}:
                                    if (new.analysis is None or old.analysis.model_dump(exclude={"year"}) != new.analysis.model_dump(exclude={"year"})):
                                        raise PolicyError("plan_period_mismatch")
                                for before, after in zip(old.queries, new.queries):
                                    for field in ("operation", "metric", "dimension", "requested_brands", "product_ids",
                                                  "demographic_values", "revenue_statuses", "inactivity_days", "limit", "temporal_role"):
                                        value = getattr(before, field)
                                        if value is not None and value != getattr(after, field):
                                            # Completing a missing baseline may bind a
                                            # redundant scalar query to the newly supplied
                                            # comparison role. All other choices still
                                            # match; analytical_plans validates the pair.
                                            if (field == "temporal_role" and context.pending
                                                    and value == "analysis" and after.temporal_role == "comparison"
                                                    and new.comparison is not None
                                                    and comparison_choices_missing(old, effective_query_brands=tuple(
                                                        effective_brands(scope, q.requested_brands, catalog) for q in old.queries))):
                                                continue
                                            raise PolicyError("plan_intent_mismatch")
                        anchors = {}
                        if context and continuity_spec and interpretation.request:
                            if context.pending:
                                previous_anchors = context.pending_resolution.anchors if context.pending_resolution else {}
                            else:
                                previous_anchors = context.snapshot.temporal_anchors()
                            anchors = request_role_anchors(interpretation.request, previous_spec=continuity_spec,
                                previous_anchors=previous_anchors, context_action=interpretation.context_action,
                                reference_date=reference_date, previous_plan=state.get("previous_plan"),
                                temporal_updates=interpretation.temporal_updates)
                        interpretation, resolved = resolve_interpretation(
                            interpretation, reference_date=reference_date, analytical_timezone=self.analytical_timezone,
                            previous_plan=state.get("previous_plan"), role_anchors=anchors,
                            effective_query_brands=tuple(effective_brands(scope, q.requested_brands, catalog)
                                for q in interpretation.request.queries) if interpretation.request else None)
                        if resolved and resolved.comparison and resolved.spec.mode not in {Mode.REPORT, Mode.TEMPORAL_COMPARE, Mode.CHURN_COMPARE}:
                            interpretation = interpretation.model_copy(update={"status": "needs_clarification", "issues": ("unresolved_interpretation",)})
                        if resolved and interpretation.status == "ready":
                            # Validate operation combinations now, before any analytical query or retrieval.
                            plans_to_check = analytical_plans(resolved, effective_query_brands=tuple(
                                effective_brands(scope, q.requested_brands, catalog) for q in resolved.spec.queries))
                            if cohort == CohortOrigin.DEMOGRAPHIC and any(p.operation == Operation.RANK_CUSTOMERS for p in plans_to_check):
                                raise PolicyError("demographic_membership_blocked")
                        goal = interpretation.request.intent if interpretation.request else None
                        if interpretation.status != "ready":
                            clarification = " ".join(ISSUE_TEXT[i] for i in interpretation.issues)
                            self.store.save_continuity(owner, conversation_id, updated_context(
                                pending=interpretation.request if interpretation.status == "needs_clarification" else None,
                                pending_resolution=(PendingTemporalResolution.capture(interpretation.request,
                                    anchors or request_role_anchors(interpretation.request, previous_spec=None, previous_anchors=None,
                                        context_action="new", reference_date=reference_date, previous_plan=state.get("previous_plan")))
                                    if interpretation.status == "needs_clarification" and interpretation.request else None),
                                issues=interpretation.issues if interpretation.status == "needs_clarification" else (),
                                snapshot=context.snapshot if context else None, clarification=clarification,
                                reset_history=interpretation.context_action == "new",
                                preserve_expiry=bool(context and context.pending and
                                    (interpretation.context_action in {"continue", "correct"} or bare_year))))
                        elif goal == "cancel" or interpretation.context_action == "cancel":
                            self.store.save_continuity(owner, conversation_id, updated_context(
                                snapshot=context.snapshot if context else None, reset_history=True))
                            return {"interpretation": interpretation, "contextual_only": True,
                                    "content": "Pending analysis request cancelled. For a report deletion proposal, use the token-bound cancel command shown in its preview."}
                        elif goal in {"explain", "why", "summary", "save"}:
                            snapshot = context.snapshot if context else None
                            if snapshot is None:
                                return {"interpretation": interpretation.model_copy(update={"status": "needs_clarification", "issues": ("unresolved_interpretation",)}), "contextual_only": True,
                                    "content": "Which analysis do you mean? No valid released snapshot is available; run a fresh analysis before explaining or saving its figures."}
                            requested_report = goal == "save" or snapshot.report_id is not None or snapshot.request.spec.mode == Mode.REPORT or snapshot.request.spec.report is not None
                            if requested_report:
                                snapshot = snapshot.model_copy(update={"grounded":apply_report_preferences(snapshot.request,snapshot.grounded)})
                            composition = compose_report(snapshot.request, snapshot.evidence, snapshot.grounded) if requested_report else None
                            content = _render(snapshot.evidence, ReportDraft(),
                                              "previous_selection_unavailable" if snapshot.grounded.selection_status == "fallback" else None,
                                              report_requested=requested_report,
                                              resolved_request=snapshot.request, grounded=snapshot.grounded)
                            if goal == "summary":
                                facts = snapshot.grounded.findings[:3]
                                lines = ["Summary of the released analysis:", *(f.text for f in facts),
                                         f"Factual findings: {snapshot.grounded.factual_status}; selection: {snapshot.grounded.selection_status}."]
                                for evidence_index, item in enumerate(snapshot.evidence, 1):
                                    lines.append(f"Evidence E{evidence_index}: UTC period {item.period_utc}; brands {json.dumps(item.execution_scope.brands)}; "
                                                 f"product IDs {item.execution_scope.product_ids}; complete={item.complete}. "
                                                 f"{item.completeness_note}")
                                    lines.append(f"Definitions for E{evidence_index}: " + json.dumps(item.metric_definition, sort_keys=True))
                                if snapshot.grounded.selection_status != "selected":
                                    lines.append("Model explanation unavailable; checked deterministic findings retained. No model-selected actions were released.")
                                content = ((composition_text(composition, snapshot.grounded) + "\n") if composition else "") + "\n".join(lines)
                            content += "\nReused released evidence; original as-of UTC: " + ", ".join(e.query_as_of_utc for e in snapshot.evidence) + "."
                            if goal == "why":
                                content += "\nThese observations do not establish a cause."
                                metric = snapshot.evidence[0].metric_definition.get("metric")
                                investigation = ("Compare earlier inactivity cutoffs within the same authorized purchaser population."
                                    if metric == "churn_rate" else
                                    "Compare aggregate cohort spend and group sizes without releasing membership IDs."
                                    if metric == "average_customer_spend" else
                                    "Request a scoped monthly trend to investigate when the pattern changed, then review relevant business events.")
                                content += " Suggested investigation: " + investigation
                            return {"interpretation": interpretation, "contextual_only": True,
                                    "content": content, "evidence": snapshot.evidence,
                                    "executed_plans": snapshot.plans, "queries": context_queries,
                                    "grounded_report": snapshot.grounded, "resolved_request": snapshot.request,
                                    "report_requested": goal == "save", "scope_version": scope.authorization_version,
                                    **({"report_composition": composition} if composition else {})}
                        elif goal == "schema_help":
                            self.store.save_continuity(owner, conversation_id, updated_context(snapshot=context.snapshot if context else None))
                        else:
                            # Once an independently interpreted new request starts, stale pending state is cleared,
                            # even if warehouse/provider work subsequently fails.
                            if context and context.pending:
                                self.store.save_continuity(owner, conversation_id, updated_context())
                        return {"interpretation": interpretation, "resolution_role_anchors": anchors,
                                **({"resolved_request": resolved} if resolved else {}),
                                **({"content": SCHEMA_HELP} if interpretation.status == "ready" and
                                   interpretation.request.intent == "schema_help" else {})}
                    except (PolicyError, ModelFailure) as exc:
                        code = exc.code
                        repairable = {"invalid_interpretation_contract", "invalid_model_json", "invalid_plan_contract",
                                      "plan_limit_mismatch", "plan_intent_mismatch", "plan_investigation_mismatch", "plan_period_mismatch"}
                        if save_repair_pending and code in repairable:
                            return clarify_modified_save()
                        if code not in repairable or attempt == 2:
                            raise
                        budget.repairs += 1
                        trace.emit("recovery", decision="repair", category="policy")
                        feedback = f"Repair the structured request: {code}. Preserve all user constraints. Clarify unresolved meaning."
                        if isinstance(exc, ModelFailure) and exc.validation_issues:
                            feedback += " " + exc.repair_feedback
                raise ModelFailure("plan_repair_limit")

            def plan(state: TurnState) -> dict[str, Any]:
                nonlocal selected_golden, golden_marker
                mark_stage("structured_planning")
                request = state["resolved_request"]
                assert bq is not None
                catalog = bq.load_catalog()
                scope = resolve_demo_scope(owner, catalog)
                query_brands = tuple(effective_brands(scope, q.requested_brands, catalog) for q in request.spec.queries)
                plans = analytical_plans(request, effective_query_brands=query_brands)
                # Validate every requested scope before any Golden content is used.
                for item in plans:
                    brands = effective_brands(scope, item.requested_brands, catalog)
                    authorize_products(item.product_ids, brands, catalog)
                golden, golden_status = None, "no_match"
                try:
                    # Retrieval visibility uses the effective requested scope,
                    # which may be narrower than the identity's full scope.
                    retrieval_scope = scope.model_copy(update={"brands": effective_brands(
                        scope, plans[0].requested_brands, catalog)})
                    golden = retrieve(request.spec, retrieval_scope)
                    golden_status = "selected" if golden else "no_match"
                except RetrievalFailure:
                    golden_status = "outage"
                selected_golden, golden_marker = golden, golden_status
                changed = tuple(apply_method(p, golden) for p in plans)
                validate_bound_plans(changed, request, effective_query_brands=query_brands)
                trace.emit("golden", decision=golden_status, trio_id=golden.trio_id if golden else "unknown",
                           version=golden.version if golden else 0, method=golden.method if golden else "none",
                           operation=changed[0].operation.value, count=1 if changed != plans else 0)
                trace.emit("policy", decision="approved", operation=changed[0].operation.value,
                           metric=changed[0].metric.value, version=1)
                return {"plan_envelope": PlanEnvelope(mode=request.spec.mode, plans=changed),
                        "report_requested": request.spec.mode == Mode.REPORT or request.spec.report is not None,
                        "golden": golden, "golden_status": golden_status}

            def authorize(state: TurnState) -> dict[str, Any]:
                nonlocal bq
                mark_stage("authorization")
                if bq is None:
                    bq = self.turn_factory(self.settings, limits=QueryLimits(max_executions=8,
                                                 turn_seconds=max(0.01, budget.remaining())))
                    bq.telemetry = trace
                catalog = bq.load_catalog()
                scope = resolve_demo_scope(owner, catalog)
                if state.get("previous_scope_version") is not None and state["previous_scope_version"] != scope.authorization_version:
                    raise PolicyError("scope_changed_start_new_conversation")
                queries = []
                for plan_item in state["plan_envelope"].plans:
                    try:
                        query = compile_plan(plan_item, scope, catalog)
                        trace.emit("sql_validation", status="ok", operation=plan_item.operation.value,
                                   metric=plan_item.metric.value)
                    except PolicyError as exc:
                        if exc.code != "compiler_syntax_error":
                            raise
                        trace.emit("sql_validation", status="error", category="syntax")
                        trace.emit("recovery", decision="repair", category="syntax")
                        query = compile_plan(plan_item, scope, catalog)
                        trace.emit("sql_validation", status="ok", operation=plan_item.operation.value,
                                   metric=plan_item.metric.value)
                    queries.append(query)
                return {"queries": queries, "scope_version": scope.authorization_version}

            def execute(state: TurnState) -> dict[str, Any]:
                assert bq is not None
                mark_stage("step3_execution")
                queries = list(state["queries"])
                plans = list(state["plan_envelope"].plans)

                def run_with_recovery(query: CompiledQuery, source_plan: AnalysisPlan) -> tuple[Evidence, CompiledQuery]:
                    try:
                        return bq.execute_compiled(query), query
                    except ExecutionError as exc:
                        if exc.category not in {"query_syntax_error", "query_schema_error"}:
                            raise
                        kind = "syntax" if exc.category == "query_syntax_error" else "schema"
                        trace.emit("recovery", decision="repair", category=kind)
                        catalog = bq.refresh_catalog() if kind == "schema" else bq.load_catalog()
                        scope = resolve_demo_scope(owner, catalog)
                        regenerated = compile_plan(source_plan, scope, catalog)
                        if (regenerated.effective_brands != query.effective_brands
                                or regenerated.metric_definition != query.metric_definition
                                or regenerated.parameters != query.parameters
                                or regenerated.scope_version != query.scope_version):
                            raise ExecutionError("recovery_scope_changed")
                        trace.emit("sql_validation", status="ok", operation=source_plan.operation.value,
                                   metric=source_plan.metric.value)
                        return bq.execute_compiled(regenerated), regenerated

                evidence = []
                for index, query in enumerate(queries):
                    item, regenerated = run_with_recovery(query, plans[index])
                    queries[index] = regenerated
                    evidence.append(item)
                if state["plan_envelope"].mode == Mode.INVESTIGATE_TOP_PRODUCTS:
                    first = evidence[0]
                    ids = tuple(row["product_id"] for row in first.rows)
                    if len(ids) == 2:
                        base = state["plan_envelope"].plans[0]
                        second = AnalysisPlan(operation=Operation.PRODUCT_COMPARISON,
                                              metric=Metric.REVENUE, period=base.period,
                                              requested_brands=base.requested_brands,
                                              product_ids=ids, revenue_statuses=base.revenue_statuses)
                        catalog = bq.load_catalog()
                        scope = resolve_demo_scope(owner, catalog)
                        second_query = compile_plan(second, scope, catalog)
                        second_item, second_query = run_with_recovery(second_query, second)
                        evidence.append(second_item)
                        queries.append(second_query)
                        plans.append(second)
                diagnosis = None
                for index, item in enumerate(evidence):
                    empty = (not item.rows or item.coverage.period_rows == 0
                             or item.operation == Operation.CHURN_SNAPSHOT and item.rows[0].get("eligible_customers") == 0)
                    if not empty:
                        continue
                    diagnosis = "valid_empty"
                    trace.emit("empty_result", decision="source_empty" if item.coverage.source_rows == 0 else "valid_empty",
                               operation=item.operation.value)
                    if item.coverage.source_rows == 0 and item.coverage.period_rows == 0:
                        try:
                            refreshed = bq.refresh_catalog()
                            new_scope = resolve_demo_scope(owner, refreshed)
                            rebuilt = compile_plan(plans[index], new_scope, refreshed)
                            if (rebuilt.effective_brands != queries[index].effective_brands
                                    or rebuilt.parameters != queries[index].parameters
                                    or rebuilt.scope_version != queries[index].scope_version):
                                raise ExecutionError("recovery_scope_changed")
                            trace.emit("recovery", decision="diagnostic", category="metadata")
                            evidence[index], queries[index] = run_with_recovery(rebuilt, plans[index])
                            if evidence[index].coverage.period_rows:
                                diagnosis = None
                        except (ExecutionError, PolicyError):
                            diagnosis = "incomplete"
                            trace.emit("empty_result", decision="stop", category="budget")
                return {"evidence": tuple(evidence), "queries": tuple(queries),
                        "executed_plans": tuple(plans), "empty_diagnosis": diagnosis}

            def evidence_check(state: TurnState) -> dict[str, Any]:
                mark_stage("evidence_checks")
                if not state.get("evidence") or any(item.scope_reference != f"demo:{owner}" or item.scope_version != state["scope_version"] for item in state["evidence"]):
                    raise PolicyError("evidence_scope_mismatch")
                return {}

            def draft(state: TurnState) -> dict[str, Any]:
                mark_stage("explanation_drafting")
                if state.get("empty_diagnosis"):
                    return {"draft": ReportDraft(), "draft_error": None}
                try:
                    # Scope disclosure uses trusted metadata in the renderer;
                    # avoid expanding model prompts with the full authorized catalog.
                    payload = tuple(item.model_dump(mode="json", exclude={"execution_scope"})
                                    for item in state["evidence"])
                    return {"draft": gateway.report(payload, report_requested=state["report_requested"]), "draft_error": None}
                except (ModelFailure, ValidationError) as exc:
                    return {"draft": ReportDraft(), "draft_error": getattr(exc, "code", "invalid_report_contract")}

            def claim_validation(state: TurnState) -> dict[str, Any]:
                mark_stage("claim_validation")
                draft = state["draft"]
                draft_error = state.get("draft_error")
                try:
                    grounded = _validate_draft(draft, state["evidence"])
                except (PolicyError, ValidationError) as exc:
                    draft = ReportDraft()
                    draft_error = getattr(exc, "code", "invalid_report_contract")
                if draft_error:
                    grounded = ground_report(ReportDraft(), state["evidence"], fallback=True)
                if state["resolved_request"].spec.mode == RequestMode.TEMPORAL_COMPARE:
                    # Require the paired trusted calculation, preserving valid selected actions.
                    if not paired_coverage(grounded.findings, state["evidence"]) and not state["report_requested"]:
                        raise PolicyError("incomplete_comparison")
                if state["report_requested"]:
                    grounded = apply_report_preferences(state["resolved_request"],grounded)
                composition = compose_report(state["resolved_request"], state["evidence"], grounded) if state["report_requested"] else None
                return {"grounded_report": grounded,
                        **({"report_composition": composition} if composition else {}), "content": _render(state["evidence"], draft, draft_error,
                                           report_requested=state["report_requested"],
                                           golden_status=state.get("golden_status", "none"),
                                           golden=state.get("golden"),
                                           empty_diagnosis=state.get("empty_diagnosis"),
                                           resolved_request=state["resolved_request"], grounded=grounded)}

            def display_save(state: TurnState) -> dict[str, Any]:
                mark_stage("display_save")
                if "evidence" not in state:
                    return {}
                last = state["executed_plans"][0]
                if state.get("contextual_only"):
                    assert context and context.snapshot
                    snapshot = context.snapshot
                else:
                    snapshot = AnalysisSnapshot.capture(reference_date=reference_date, previous_plan=state.get("previous_plan"),
                        role_anchors=state.get("resolution_role_anchors"),
                        created_at=self.store._now(), plans=state["executed_plans"],
                        evidence=state["evidence"], request=state["resolved_request"], grounded=state["grounded_report"])
                continuity = updated_context(snapshot=snapshot, preserve_expiry=bool(state.get("contextual_only")
                    or context and context.pending and state["interpretation"].context_action in {"continue", "correct"}))
                cohort_origin = (CohortOrigin.DEMOGRAPHIC if any(p.operation == Operation.DEMOGRAPHIC_COMPARISON for p in state["executed_plans"])
                                 else CohortOrigin.BUSINESS)
                if not state["report_requested"]:
                    if not state.get("contextual_only"):
                        self.store.save_context(owner, conversation_id, last, cohort_origin, state["scope_version"])
                    self.store.save_continuity(owner, conversation_id, continuity)
                    trace.emit("report_persistence", decision="saved", status="ok")
                    return {"report_id": None}
                evidence = state["evidence"]
                report_id = self.store.save_turn_report(
                    owner=owner, conversation_id=conversation_id, turn_id=turn_id,
                    plan=last, cohort_origin=cohort_origin,
                    content=state["content"],
                    grounded_report=state["grounded_report"].model_dump(mode="json"),
                    continuity=continuity,
                    report_support=ReportArtifact(snapshot=snapshot.model_copy(update={"report_id": None})).model_dump(mode="json"),
                    report_completeness=state["report_composition"].completeness,
                    report_composition=state["report_composition"].model_dump(mode="json"),
                    metric_definitions=[item.metric_definition for item in evidence],
                    scope_reference=evidence[0].scope_reference,
                    scope_version=evidence[0].scope_version,
                    request_digest=state["request_digest"], identity_digest=state["identity_digest"],
                    plans=[plan.model_dump(mode="json") for plan in state["executed_plans"]],
                    scope_digest=_scope_digest(state["queries"]),
                    evidence_refs=evidence_references(evidence),
                )
                trace.emit("report_persistence", decision="saved", status=state["report_composition"].completeness)
                return {"report_id": report_id}

            graph.add_node("privacy_screen", screen)
            graph.add_node("intent_metric_resolution", resolve)
            graph.add_node("structured_planning", plan)
            graph.add_node("authorization", authorize)
            graph.add_node("step3_execution", execute)
            graph.add_node("evidence_checks", evidence_check)
            graph.add_node("explanation_drafting", draft)
            graph.add_node("claim_validation", claim_validation)
            graph.add_node("display_save", display_save)
            graph.add_edge(START, "privacy_screen")
            graph.add_edge("privacy_screen", "intent_metric_resolution")
            def route_interpretation(state: TurnState) -> str:
                if state.get("content") == SCHEMA_HELP or state.get("contextual_only"):
                    return "help"
                if state["interpretation"].status != "ready":
                    return "stop"
                return "analyze"

            graph.add_conditional_edges("intent_metric_resolution", route_interpretation,
                                        {"help": "display_save", "analyze": "structured_planning", "stop": END})
            chain = ["structured_planning", "authorization", "step3_execution", "evidence_checks",
                     "explanation_drafting", "claim_validation", "display_save"]
            for source, target in zip(chain, chain[1:]):
                graph.add_edge(source, target)
            graph.add_edge("display_save", END)
            final = graph.compile().invoke(initial)
            interpretation = final["interpretation"]
            if interpretation.status != "ready":
                return TurnResult(interpretation.status, final.get("content") or " ".join(ISSUE_TEXT[i] for i in interpretation.issues),
                                  conversation_id, turn_id, tuple(budget.routes), budget.requests,
                                  bq.executions if bq else 0, bq.billed_bytes if bq else 0,
                                  interpretation_status=interpretation.status,
                                  resolved_periods=({role: getattr(final["resolved_request"], role).model_dump(mode="json")
                                                    if getattr(final["resolved_request"], role) else None
                                                    for role in ("analysis", "comparison", "action")}
                                                   if final.get("resolved_request") else None))
            request = final.get("resolved_request")
            periods = ({role: getattr(request, role).model_dump(mode="json") if getattr(request, role) else None
                        for role in ("analysis", "comparison", "action")} if request else None)
            return TurnResult("partial" if final.get("report_composition") and final["report_composition"].completeness == "partial" else "ok", final["content"], conversation_id, turn_id,
                              tuple(budget.routes), budget.requests,
                              bq.executions if bq else 0, bq.billed_bytes if bq else 0,
                              tuple(item.job_id for item in final.get("evidence", ())), final.get("report_id"),
                              golden_status=final.get("golden_status", "none"),
                              golden_trio_id=final["golden"].trio_id if final.get("golden") else None,
                              golden_method=final["golden"].method if final.get("golden") else None,
                              interpretation_status="ready", resolved_periods=periods,
                              presentation=display_payload(final["evidence"], final["grounded_report"], request,
                                  empty_diagnosis=final.get("empty_diagnosis"),
                                  contextual_reuse=bool(final.get("contextual_only")),
                                  context_goal=interpretation.request.intent if final.get("contextual_only") else None) if final.get("evidence") else None,
                              report_completeness=final["report_composition"].completeness if final.get("report_composition") else None,
                              report_composition=final["report_composition"].model_dump(mode="json") if final.get("report_composition") else None,
                              factual_status=final["grounded_report"].factual_status if final.get("grounded_report") else None,
                              factual_findings=tuple(f.model_dump(mode="json") for f in final["grounded_report"].findings) if final.get("grounded_report") else (),
                              suggestions=tuple({"kind": kind, **s.model_dump(mode="json")} for kind in ("recommendations", "hypotheses")
                                                for s in getattr(final["grounded_report"], kind)) if final.get("grounded_report") else ())
        except (PolicyError, ExecutionError, ModelFailure, StoreFailure) as exc:
            code = getattr(exc, "code", None) or getattr(exc, "category", None) or "store_unavailable"
            code = code if isinstance(code, str) and code in SAFE_ERROR_CODES else "internal_failure"
            category = ("syntax" if "syntax" in code else "schema" if "schema" in code
                        else "budget" if any(word in code for word in ("limit", "timeout", "billing"))
                        else "storage" if isinstance(exc, StoreFailure) else "provider" if isinstance(exc, (ExecutionError, ModelFailure))
                        else "policy")
            trace.emit("stage", stage=active_stage, status="error", category=category)
            if active_stage == "display_save":
                trace.emit("report_persistence", decision="failed", status="error", category="storage")
            return TurnResult("error", f"Unable to complete this request: {code}.", conversation_id, turn_id,
                              tuple(budget.routes), budget.requests,
                              bq.executions if bq else 0,
                              None if bq is not None and getattr(bq, "billing_unknown", False) else bq.billed_bytes if bq else 0,
                              golden_status=golden_marker,
                              golden_trio_id=selected_golden.trio_id if selected_golden else None,
                              golden_method=selected_golden.method if selected_golden else None,
                              error_category=category)
        except Exception:
            trace.emit("stage", stage=active_stage, status="error", category="internal")
            return TurnResult("error", "Unable to complete this request: internal_failure.",
                              conversation_id, turn_id, tuple(budget.routes), budget.requests,
                              bq.executions if bq else 0,
                              None if bq is not None and getattr(bq, "billing_unknown", False) else bq.billed_bytes if bq else 0,
                              golden_status=golden_marker,
                              golden_trio_id=selected_golden.trio_id if selected_golden else None,
                              golden_method=selected_golden.method if selected_golden else None,
                              error_category="internal")
