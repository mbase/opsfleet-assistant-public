"""Interpretation, continuity compatibility, clarification, and temporal correction."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import re

from .compiler import compile_plan
from .report_text import render_v1 as _render, ReportTextContract
from .contracts import CohortOrigin, Operation, InterpretationResult, RequestSpec
from .conversation import history, model_context, safe_spec, validate_cached_evidence, validate_snapshot_request, PendingTemporalResolution, request_role_anchors
from .identity import PolicyError, effective_brands, authorize_products
from .model_gateway import Mode, ModelFailure, ReportDraft
from .safety import screen_question
from .report_artifact import reconstruct_grounded
from .reporting import compose_report, apply_report_preferences, composition_text, report_spec
from .intent import ISSUE_TEXT, analytical_plans, comparison_choices_missing, resolve_interpretation

from typing import TYPE_CHECKING
from .turn_types import TurnState
if TYPE_CHECKING:
    from .turn_session import TurnSession

from .workflow_contracts import SCHEMA_HELP


class RequestResolver:
    """Interpret only after revalidating continuity under trusted current authority."""

    def resolve(self, state: TurnState, turn: TurnSession) -> TurnState:
        turn.mark_stage("intent_metric_resolution")
        # Authorize prior context before including it in a model prompt.
        # This catalog work is charged to the same warehouse turn budget.
        turn.open_warehouse()
        catalog = turn.warehouse.load_catalog()
        scope = turn.resolve_scope(turn.context.owner, catalog)
        turn.authorize(scope, catalog)
        turn.catalog_digest = hashlib.sha256(json.dumps(sorted(catalog.product_brands.items()),
                                                   separators=(",", ":")).encode()).hexdigest()
        if turn.continuity:
            compatible = (turn.continuity.identity_digest == turn.identity_digest and turn.continuity.catalog_digest == turn.catalog_digest
                and turn.continuity.authorization_version == scope.authorization_version
                and turn.continuity.identity_brands == tuple(sorted(scope.brands))
                and turn.continuity.identity_role == scope.role.value)
            if not compatible:
                turn.store.clear_continuity(turn.context.owner, turn.context.conversation_id)
                raise PolicyError("scope_changed_start_new_conversation")
            for message in turn.continuity.messages:
                screen_question(message.content)
            turn.continuity = turn.continuity.model_copy(update={"messages": history(turn.continuity.messages,
                max_messages=turn.settings.history_max_messages, max_chars=turn.settings.history_max_chars)})
            if turn.continuity.pending:
                if any(getattr(turn.continuity.pending, role) for role in ("analysis", "comparison", "action")) and turn.continuity.pending_resolution is None:
                    turn.continuity = turn.continuity.model_copy(update={"pending": None, "pending_resolution": None, "issues": (), "messages": ()})
                    turn.store.save_continuity(turn.context.owner, turn.context.conversation_id, turn.continuity)
                    return {"interpretation": InterpretationResult(status="needs_clarification", issues=("unresolved_interpretation",)),
                            "content": "The unfinished request has no valid original analytical date context. Please restate its period and metric."}
                safe_spec(turn.continuity.pending)
                for q in turn.continuity.pending.queries:
                    brands = effective_brands(scope, q.requested_brands, catalog)
                    authorize_products(q.product_ids, brands, catalog)
            if turn.continuity.snapshot:
                snapshot = turn.continuity.snapshot
                if turn.store._now() >= snapshot.created_at + timedelta(seconds=turn.settings.context_ttl_seconds):
                    turn.continuity = turn.continuity.model_copy(update={"snapshot": None})
                    turn.store.save_continuity(turn.context.owner, turn.context.conversation_id, turn.continuity)
                    state["previous_plan"] = None
                else:
                    try:
                        validate_snapshot_request(snapshot)
                    except PolicyError:
                        turn.store.clear_continuity(turn.context.owner, turn.context.conversation_id)
                        raise
                    turn.context_queries = tuple(compile_plan(p, scope, catalog) for p in snapshot.plans)
                    for query, item in zip(turn.context_queries, snapshot.evidence):
                        if (item.scope_reference != f"demo:{turn.context.owner}" or item.scope_version != scope.authorization_version
                                or item.field_policy_version != turn.context.versions.field_policy
                                or item.execution_scope is None
                                or item.execution_scope.brands != query.effective_brands
                                or item.execution_scope.product_ids != query.requested_product_ids
                                or item.metric_definition != query.metric_definition
                                or item.columns != query.result_contract.columns):
                            turn.store.clear_continuity(turn.context.owner, turn.context.conversation_id)
                            raise PolicyError("saved_scope_changed")
                        try:
                            validate_cached_evidence(query, item)
                        except PolicyError:
                            turn.store.clear_continuity(turn.context.owner, turn.context.conversation_id)
                            raise
                    # Recompute trusted W3 sentences from typed selections, never report prose.
                    checked = reconstruct_grounded(snapshot)
                    if checked.driver != snapshot.grounded.driver:
                        raise PolicyError("saved_scope_changed")
                    turn.continuity = turn.continuity.model_copy(update={"snapshot": snapshot.model_copy(update={"grounded": checked})})
        # Exact catalog-name screening is an authorization defense, not an intent parser.
        for brand in catalog.brands:
            if (re.search(r"(?<!\w)" + re.escape(brand.casefold()) + r"(?!\w)", state["question"].casefold())
                    and brand not in scope.brands):
                raise PolicyError("unauthorized_brand")
        feedback = ""
        save_repair_pending = False


        for attempt in range(turn.context.max_repairs + 1):
            try:
                interpretation = turn.gateway.interpret(
                    state["question"], previous_plan=state.get("previous_plan"),
                    reference_date=turn.context.reference_date.isoformat(), analytical_timezone=turn.analytical_timezone,
                    feedback=feedback, conversation_context=model_context(turn.continuity),
                    pending_request=turn.continuity.pending if turn.continuity else None)
                if getattr(turn.gateway, "interpretation_context_overflow", False):
                    content = ("This request and its required context are too long to process safely. "
                        "Please restate the request more briefly or start a new analysis. No analysis or report was created.")
                    turn.store.save_continuity(turn.context.owner, turn.context.conversation_id, turn.conversations.updated_context(turn,
                        pending=turn.continuity.pending if turn.continuity else None,
                        pending_resolution=turn.continuity.pending_resolution if turn.continuity else None,
                        issues=turn.continuity.issues if turn.continuity else (), snapshot=turn.continuity.snapshot if turn.continuity else None,
                        clarification=content, preserve_expiry=bool(turn.continuity)))
                    return {"interpretation": interpretation, "contextual_only": True, "content": content}
                if interpretation.request:
                    interpretation = interpretation.model_copy(update={"request": safe_spec(interpretation.request)})
                # A save repair must not turn into an executable modified analysis
                # or arm a pending save that a later bare confirmation could run.
                if save_repair_pending and not (interpretation.status == "ready"
                        and interpretation.request and interpretation.request.intent == "save"):
                    return self.clarify_modified_save(turn)
                if (interpretation.status == "ready" and interpretation.request
                        and interpretation.request.intent == "save" and turn.continuity and turn.continuity.snapshot
                        and turn.reports.save_modifies_snapshot(interpretation, turn.continuity.snapshot, scope, catalog,
                            turn.context.reference_date, state.get("previous_plan"))):
                    # Only ambiguous report choices are eligible. This temporary
                    # projection detects other differences; it is never executed
                    # or saved, and the actual proposal remains unchanged.
                    without_report = interpretation.model_copy(update={"request":
                        interpretation.request.model_copy(update={"report": None})})
                    if (not save_repair_pending and attempt < turn.context.max_repairs
                            and interpretation.request.report is not None
                            and not turn.reports.save_modifies_snapshot(without_report, turn.continuity.snapshot, scope, catalog,
                                turn.context.reference_date, state.get("previous_plan"))):
                        save_repair_pending = True
                        turn.context.budget.repairs += 1
                        turn.context.trace.emit("recovery", decision="repair", category="policy")
                        feedback = (
                            "Reinterpret the original current user message: the save proposal changes report choices. "
                            "Determine whether the user explicitly requested these changes or they were added by the interpreter. "
                            "For an unchanged save return only the compact save intent, null report and periods, empty queries, "
                            "sources, clear_fields and temporal_updates. Inherit snapshot choices; do not restate defaults. "
                            "Never discard explicit user changes: retain the full revised request when changes were requested; "
                            "they require a separate request before saving. Clarify unresolved meaning. "
                            "Typed report choices (data): " + json.dumps({
                                "snapshot_report": report_spec(turn.continuity.snapshot.request).model_dump(mode="json"),
                                "proposed_report": interpretation.request.report.model_dump(mode="json")},
                                separators=(",", ":")))
                        continue
                    return self.clarify_modified_save(turn)
                # A numeric year is a narrow format consistency check, not a language parser.
                bare_year = bool(re.fullmatch(r"[1-9][0-9]{3}", state["question"]))
                if bare_year and not (turn.continuity and (turn.continuity.pending or turn.continuity.snapshot)):
                    interpretation = InterpretationResult(status="needs_clarification",
                        request=RequestSpec(analysis=interpretation.request.analysis if interpretation.request else None),
                        issues=("missing_metric",))
                if bare_year and turn.continuity and turn.continuity.pending and interpretation.request and interpretation.request.intent == "analysis":
                    interpretation = interpretation.model_copy(update={"context_action": "continue", "temporal_updates": (), "clear_fields": ()})
                # Continuing a clarification cannot silently discard supplied choices.
                continuity_spec = None
                if turn.continuity:
                    continuity_spec = turn.continuity.pending or (turn.continuity.snapshot.request.spec if turn.continuity.snapshot else None)
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
                    if turn.continuity and turn.continuity.pending and interpretation.context_action != "correct":
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
                        if new.playbook != old.playbook:
                            raise PolicyError("plan_intent_mismatch")
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
                                    if (field == "temporal_role" and turn.continuity.pending
                                            and value == "analysis" and after.temporal_role == "comparison"
                                            and new.comparison is not None
                                            and comparison_choices_missing(old, effective_query_brands=tuple(
                                                effective_brands(scope, q.requested_brands, catalog) for q in old.queries))):
                                        continue
                                    raise PolicyError("plan_intent_mismatch")
                anchors = {}
                if turn.continuity and continuity_spec and interpretation.request:
                    if turn.continuity.pending:
                        previous_anchors = turn.continuity.pending_resolution.anchors if turn.continuity.pending_resolution else {}
                    else:
                        previous_anchors = turn.continuity.snapshot.temporal_anchors()
                    anchors = request_role_anchors(interpretation.request, previous_spec=continuity_spec,
                        previous_anchors=previous_anchors, context_action=interpretation.context_action,
                        reference_date=turn.context.reference_date, previous_plan=state.get("previous_plan"),
                        temporal_updates=interpretation.temporal_updates)
                interpretation, resolved = resolve_interpretation(
                    interpretation, reference_date=turn.context.reference_date, analytical_timezone=turn.analytical_timezone,
                    previous_plan=state.get("previous_plan"), role_anchors=anchors,
                    effective_query_brands=tuple(effective_brands(scope, q.requested_brands, catalog)
                        for q in interpretation.request.queries) if interpretation.request else None)
                if resolved and resolved.comparison and not (resolved.spec.playbook and resolved.spec.playbook.id == "churn_change") and resolved.spec.mode not in {Mode.REPORT, Mode.TEMPORAL_COMPARE, Mode.CHURN_COMPARE}:
                    interpretation = interpretation.model_copy(update={"status": "needs_clarification", "issues": ("unresolved_interpretation",)})
                if resolved and interpretation.status == "ready":
                    # Validate operation combinations now, before any analytical query or retrieval.
                    plans_to_check = analytical_plans(resolved, effective_query_brands=tuple(
                        effective_brands(scope, q.requested_brands, catalog) for q in resolved.spec.queries))
                    if turn.cohort == CohortOrigin.DEMOGRAPHIC and any(p.operation == Operation.RANK_CUSTOMERS for p in plans_to_check):
                        raise PolicyError("demographic_membership_blocked")
                goal = interpretation.request.intent if interpretation.request else None
                if interpretation.status != "ready":
                    clarification = " ".join(ISSUE_TEXT[i] for i in interpretation.issues)
                    turn.store.save_continuity(turn.context.owner, turn.context.conversation_id, turn.conversations.updated_context(turn,
                        pending=interpretation.request if interpretation.status == "needs_clarification" else None,
                        pending_resolution=(PendingTemporalResolution.capture(interpretation.request,
                            anchors or request_role_anchors(interpretation.request, previous_spec=None, previous_anchors=None,
                                context_action="new", reference_date=turn.context.reference_date, previous_plan=state.get("previous_plan")))
                            if interpretation.status == "needs_clarification" and interpretation.request else None),
                        issues=interpretation.issues if interpretation.status == "needs_clarification" else (),
                        snapshot=turn.continuity.snapshot if turn.continuity else None, clarification=clarification,
                        reset_history=interpretation.context_action == "new",
                        preserve_expiry=bool(turn.continuity and turn.continuity.pending and
                            (interpretation.context_action in {"continue", "correct"} or bare_year))))
                elif goal == "cancel" or interpretation.context_action == "cancel":
                    turn.store.save_continuity(turn.context.owner, turn.context.conversation_id, turn.conversations.updated_context(turn,
                        snapshot=turn.continuity.snapshot if turn.continuity else None, reset_history=True))
                    return {"interpretation": interpretation, "contextual_only": True,
                            "content": "Pending analysis request cancelled. For a report deletion proposal, use the token-bound cancel command shown in its preview."}
                elif goal in {"explain", "why", "summary", "save"}:
                    snapshot = turn.continuity.snapshot if turn.continuity else None
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
                            "executed_plans": snapshot.plans, "queries": turn.context_queries,
                            "grounded_report": snapshot.grounded, "resolved_request": snapshot.request,
                            "report_requested": goal == "save", "scope_version": scope.authorization_version,
                            "report_text_contract": ReportTextContract(mode="exact_save"),
                            **({"report_composition": composition} if composition else {})}
                elif goal == "schema_help":
                    turn.store.save_continuity(turn.context.owner, turn.context.conversation_id, turn.conversations.updated_context(turn, snapshot=turn.continuity.snapshot if turn.continuity else None))
                else:
                    # Once an independently interpreted new request starts, stale pending state is cleared,
                    # even if warehouse/provider work subsequently fails.
                    if turn.continuity and turn.continuity.pending:
                        turn.store.save_continuity(turn.context.owner, turn.context.conversation_id, turn.conversations.updated_context(turn, ))
                return {"interpretation": interpretation, "resolution_role_anchors": anchors,
                        **({"resolved_request": resolved} if resolved else {}),
                        **({"content": SCHEMA_HELP} if interpretation.status == "ready" and
                           interpretation.request.intent == "schema_help" else {})}
            except (PolicyError, ModelFailure) as exc:
                code = exc.code
                repairable = {"invalid_interpretation_contract", "invalid_model_json", "invalid_plan_contract",
                              "plan_limit_mismatch", "plan_intent_mismatch", "plan_investigation_mismatch", "plan_period_mismatch"}
                if save_repair_pending and code in repairable:
                    return self.clarify_modified_save(turn)
                if code not in repairable or attempt == turn.context.max_repairs:
                    raise
                turn.context.budget.repairs += 1
                turn.context.trace.emit("recovery", decision="repair", category="policy")
                feedback = f"Repair the structured request: {code}. Preserve all user constraints. Clarify unresolved meaning."
                if isinstance(exc, ModelFailure) and exc.validation_issues:
                    feedback += " " + exc.repair_feedback
        raise ModelFailure("plan_repair_limit")

    def clarify_modified_save(self, turn: TurnSession) -> TurnState:
        content = ("I could not confirm an unchanged save. "
            "Please request any analysis or report changes separately, review the result, then ask to save it. "
            "No report was saved.")
        turn.store.save_continuity(turn.context.owner, turn.context.conversation_id, turn.conversations.updated_context(turn,
            snapshot=turn.continuity.snapshot, clarification=content, preserve_expiry=True))
        return {"interpretation": InterpretationResult(status="needs_clarification",
                    context_action="continue", issues=("unresolved_interpretation",)),
                "contextual_only": True, "content": content}
