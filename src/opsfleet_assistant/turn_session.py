"""Invocation-owned state, deterministic composition, and stable failure mapping."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable
from langgraph.graph.state import CompiledStateGraph
import hashlib

from .bigquery_adapter import ExecutionError, QueryLimits
from .recovery import SubmissionState
from .config import Settings
from .report_text import render_v1 as _render, ReportTextContract
from .contracts import RequestSpec, BrandScope, CohortOrigin, Mode as RequestMode
from .presentation import display_payload
from .identity import Catalog, PolicyError
from .model_gateway import ModelFailure, ReportDraft
from .safety import screen_question
from .reporting import ground_report, compose_report, apply_report_preferences, paired_coverage
from pydantic import ValidationError
from .store import SQLiteStore, StoreFailure
from .intent import ISSUE_TEXT

from .turn_types import TurnState, TurnResult

from .workflow_contracts import SAFE_ERROR_CODES
from .workflow_helpers import validate_draft as _validate_draft
from .request_resolver import RequestResolver
from .analysis_executor import AnalysisExecutor
from .report_service import ReportService
from .conversation_service import ConversationService
from .turn_context import TurnContext, TurnAuthority, GatewayFactory, WarehouseFactory, WarehouseTurn, ModelGateway, CompletedAnalysis
from .conversation import ConversationContext
from .compiler import CompiledQuery
from .golden import Selection

class TurnSession:
    """Mutable state belongs to exactly one invocation, never to the compiled graph."""

    def __init__(self, *, settings: Settings, store: SQLiteStore, context: TurnContext,
                 gateway_factory: GatewayFactory, turn_factory: WarehouseFactory,
                 graph: CompiledStateGraph, supplied_conversation: str | None, identity_root: Path,
                 resolve_scope: Callable[[str, Catalog], BrandScope],
                 retrieve_method: Callable[[RequestSpec, BrandScope], Selection | None]):
        self.settings, self.store, self.context = settings, store, context
        self.turn_factory = turn_factory
        self.graph = graph
        self.supplied_conversation = supplied_conversation
        self.identity_root = identity_root
        self.resolve_scope, self.retrieve_method = resolve_scope, retrieve_method
        self.analytical_timezone = context.analytical_timezone
        self.gateway_factory = gateway_factory
        self.gateway: ModelGateway | None = None
        self.warehouse: WarehouseTurn | None = None
        self.selected_golden: Selection | None = None
        self.golden_marker = "none"
        self.active_stage = "privacy_screen"
        self.source_binding_version = context.versions.source_binding
        self.continuity: ConversationContext | None = None
        self.authority: TurnAuthority | None = None
        self.completed_analysis: CompletedAnalysis | None = None
        self.context_queries: tuple[CompiledQuery, ...] = ()
        self.catalog_digest = ""
        self.cohort: CohortOrigin | None = None
        self.clean_question = ""
        self.identity_digest = ""
        self.request_digest = ""
        self.resolver = RequestResolver()
        self.executor = AnalysisExecutor()
        self.reports = ReportService(store, self._authorization)
        self.conversations = ConversationService(store)

    @property
    def active_scope(self) -> BrandScope | None:
        return self.authority.scope if self.authority else None

    def authorize(self, scope: BrandScope, catalog: Catalog) -> None:
        if self.authority is not None:
            raise PolicyError("scope_changed_start_new_conversation")
        self.authority = TurnAuthority(scope, catalog)

    def authorized(self) -> TurnAuthority:
        if self.authority is None:
            raise PolicyError("unauthorized_scope")
        return self.authority

    def open_warehouse(self, limits: QueryLimits | None = None) -> WarehouseTurn:
        if self.warehouse is None:
            limits = limits or replace(self.context.query_limits, turn_seconds=self.context.budget.remaining())
            self.warehouse = self.turn_factory(self.settings, limits=limits)
            if hasattr(self.warehouse, "bind_deadline"):
                self.warehouse.bind_deadline(self.context.budget.clock, self.context.deadline)
            self.warehouse.telemetry = self.context.trace
        return self.warehouse

    def _authorization(self, owner):
        warehouse = self.open_warehouse(replace(self.context.query_limits, max_executions=1,
            job_seconds=30, turn_seconds=min(30, self.context.budget.remaining())))
        try:
            catalog = warehouse.load_catalog()
            return self.resolve_scope(owner, catalog), catalog
        finally:
            self.reports.query_executions = warehouse.executions
            self.reports.billed_bytes = warehouse.billed_bytes

    def preview_service(self) -> ReportService:
        return self.reports

    def mark_stage(self, name: str) -> None:
        self.active_stage = name
        self.context.budget.remaining()
        self.context.trace.emit("stage", stage=name, status="start")

    def run(self, question: str) -> TurnResult:
        turn = self
        self.gateway = self.gateway_factory(self.settings, budget=self.context.budget)
        try:
            turn.continuity = turn.store.load_continuity(turn.context.owner, turn.context.conversation_id)
            # Legacy plans have no compatibility/expiry metadata and are unavailable
            # as execution context. Cohort provenance remains a conservative safety hint.
            try:
                _, legacy_cohort, _ = turn.store.load_context(turn.context.owner, turn.context.conversation_id)
            except StoreFailure:
                legacy_cohort = None
            turn.cohort = turn.continuity.cohort_origin if turn.continuity else legacy_cohort
            previous = turn.continuity.snapshot.plans[0] if turn.continuity and turn.continuity.snapshot else None
            scope_version = turn.continuity.authorization_version if turn.continuity else None
            try:
                turn.clean_question = screen_question(question, prior_demographic=turn.cohort == CohortOrigin.DEMOGRAPHIC)
                if len(turn.clean_question) > turn.settings.input_max_chars:
                    raise PolicyError("invalid_question")
            except PolicyError:
                # An attack cannot leave an unfinished request armed for a late reply.
                if turn.continuity and turn.continuity.pending:
                    turn.continuity = turn.continuity.model_copy(update={"pending": None, "pending_resolution": None, "issues": (), "messages": ()})
                    turn.store.save_continuity(turn.context.owner, turn.context.conversation_id, turn.continuity)
                raise
            turn.request_digest = hashlib.sha256((turn.context.turn_id + "\0" + turn.clean_question).encode("utf-8")).hexdigest()
            try:
                turn.identity_digest = hashlib.sha256((turn.identity_root / "config" / "demo_users.json").read_bytes()).hexdigest()
            except OSError:
                raise PolicyError("invalid_identity_config") from None
            reused = turn.store.find_retry(turn.context.owner, turn.context.conversation_id, turn.context.turn_id,
                                           request_digest=turn.request_digest, identity_digest=turn.identity_digest)
            if reused is not None:
                return self.reports.replay_turn(turn, reused)
            turn.context_queries = ()
            turn.catalog_digest = ""


            initial: TurnState = {"runtime": turn, "question": turn.clean_question, "owner": turn.context.owner,
                                  "conversation_id": turn.context.conversation_id, "turn_id": turn.context.turn_id,
                                  "previous_plan": previous, "previous_cohort": turn.cohort,
                                  "previous_scope_version": scope_version,
                                  "request_digest": turn.request_digest, "identity_digest": turn.identity_digest}
            final = self.graph.invoke(initial)
            interpretation = final["interpretation"]
            if interpretation.status != "ready":
                return TurnResult(interpretation.status, final.get("content") or " ".join(ISSUE_TEXT[i] for i in interpretation.issues),
                                  turn.context.conversation_id, turn.context.turn_id, tuple(turn.context.budget.routes), turn.context.budget.requests,
                                  turn.warehouse.executions if turn.warehouse else 0, turn.warehouse.billed_bytes if turn.warehouse else 0,
                                  interpretation_status=interpretation.status,
                                  resolved_periods=({role: getattr(final["resolved_request"], role).model_dump(mode="json")
                                                    if getattr(final["resolved_request"], role) else None
                                                    for role in ("analysis", "comparison", "action")}
                                                   if final.get("resolved_request") else None))
            request = final.get("resolved_request")
            periods = ({role: getattr(request, role).model_dump(mode="json") if getattr(request, role) else None
                        for role in ("analysis", "comparison", "action")} if request else None)
            return TurnResult("partial" if final.get("report_composition") and final["report_composition"].completeness == "partial" else "ok", final["content"], turn.context.conversation_id, turn.context.turn_id,
                              tuple(turn.context.budget.routes), turn.context.budget.requests,
                              turn.warehouse.executions if turn.warehouse else 0, turn.warehouse.billed_bytes if turn.warehouse else 0,
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
                              driver_finding=final["grounded_report"].driver.model_dump(mode="json") if final.get("grounded_report") and final["grounded_report"].driver else None,
                              factual_status=final["grounded_report"].factual_status if final.get("grounded_report") else None,
                              factual_findings=tuple(f.model_dump(mode="json") for f in final["grounded_report"].findings) if final.get("grounded_report") else (),
                              suggestions=tuple({"kind": kind, **s.model_dump(mode="json")} for kind in ("recommendations", "hypotheses")
                                                for s in getattr(final["grounded_report"], kind)) if final.get("grounded_report") else ())
        except (PolicyError, ExecutionError, ModelFailure, StoreFailure) as exc:
            code = getattr(exc, "code", None) or getattr(exc, "category", None) or "store_unavailable"
            code = code if isinstance(code, str) and code in SAFE_ERROR_CODES else "internal_failure"
            category = ("internal" if isinstance(exc, ExecutionError) and exc.kind == "internal"
                        else "syntax" if "syntax" in code else "schema" if "schema" in code
                        else "budget" if any(word in code for word in ("limit", "timeout", "billing"))
                        else "storage" if isinstance(exc, StoreFailure) else "provider" if isinstance(exc, (ExecutionError, ModelFailure))
                        else "policy")
            turn.context.trace.emit("stage", stage=turn.active_stage, status="error", category=category)
            if turn.active_stage == "display_save":
                turn.context.trace.emit("report_persistence", decision="failed", status="error", category="storage")
            return TurnResult("error", f"Unable to complete this request: {code}." + (" Correction is unavailable; no broader query was run. Please report this query defect." if code in {"query_schema_error", "query_syntax_error", "compiler_syntax_error"} else ""), turn.context.conversation_id, turn.context.turn_id,
                              tuple(turn.context.budget.routes), turn.context.budget.requests,
                              turn.warehouse.executions if turn.warehouse else 0,
                              None if turn.warehouse is not None and getattr(turn.warehouse, "billing_unknown", False) else turn.warehouse.billed_bytes if turn.warehouse else 0,
                              golden_status=turn.golden_marker,
                              golden_trio_id=turn.selected_golden.trio_id if turn.selected_golden else None,
                              golden_method=turn.selected_golden.method if turn.selected_golden else None,
                              error_category=category,
                              failure_stage=getattr(exc, "stage", "unknown") if getattr(exc, "stage", "unknown") in {"dry_run", "execute", "metadata", "compile", "evidence_checks"} else "unknown",
                              failure_purpose=getattr(exc, "purpose", "unknown") if getattr(exc, "purpose", "unknown") in {"catalog", "analysis", "coverage", "customer_check", "evidence_checks"} else "unknown",
                              analysis_submission_state=getattr(exc, "analysis_submission_state",
                                  SubmissionState.SUBMITTED if turn.completed_analysis else SubmissionState.UNKNOWN),
                              analysis_job_id=getattr(exc, "analysis_job_id", turn.completed_analysis.job_id if turn.completed_analysis else None),
                              analysis_billed_bytes=getattr(exc, "analysis_billed_bytes", turn.completed_analysis.billed_bytes if turn.completed_analysis else None),
                              failure_submission_state=getattr(exc, "submission_state", SubmissionState.UNKNOWN),
                              failure_job_id=getattr(exc, "job_id", None),
                              failure_billed_bytes=getattr(exc, "billed_bytes", None))
        except Exception:
            turn.context.trace.emit("stage", stage=turn.active_stage, status="error", category="internal")
            return TurnResult("error", "Unable to complete this request: internal_failure.",
                              turn.context.conversation_id, turn.context.turn_id, tuple(turn.context.budget.routes), turn.context.budget.requests,
                              turn.warehouse.executions if turn.warehouse else 0,
                              None if turn.warehouse is not None and getattr(turn.warehouse, "billing_unknown", False) else turn.warehouse.billed_bytes if turn.warehouse else 0,
                              golden_status=turn.golden_marker,
                              golden_trio_id=turn.selected_golden.trio_id if turn.selected_golden else None,
                              golden_method=turn.selected_golden.method if turn.selected_golden else None,
                              error_category="internal",
                              analysis_submission_state=SubmissionState.SUBMITTED if turn.completed_analysis else SubmissionState.UNKNOWN,
                              analysis_job_id=turn.completed_analysis.job_id if turn.completed_analysis else None,
                              analysis_billed_bytes=turn.completed_analysis.billed_bytes if turn.completed_analysis else None)

    def screen(self, state: TurnState) -> TurnState:
        turn = self
        turn.mark_stage("privacy_screen")
        return {"question": screen_question(state["question"], prior_demographic=state.get("previous_cohort") == CohortOrigin.DEMOGRAPHIC)}

    def draft(self, state: TurnState) -> TurnState:
        turn = self
        turn.mark_stage("explanation_drafting")
        if state.get("empty_diagnosis"):
            return {"draft": ReportDraft(), "draft_error": None}
        try:
            # Scope disclosure uses trusted metadata in the renderer;
            # avoid expanding model prompts with the full authorized catalog.
            payload = tuple(item.model_dump(mode="json", exclude={"execution_scope"})
                            for item in state["evidence"])
            return {"draft": turn.gateway.report(payload, report_requested=state["report_requested"]), "draft_error": None}
        except (ModelFailure, ValidationError) as exc:
            return {"draft": ReportDraft(), "draft_error": getattr(exc, "code", "invalid_report_contract")}

    def claim_validation(self, state: TurnState) -> TurnState:
        turn = self
        turn.mark_stage("claim_validation")
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
        if grounded.driver:
            turn.context.trace.emit("playbook", playbook_id=grounded.driver.playbook_id, version=grounded.driver.method_version,
                       stage="claim_validation", status="complete" if grounded.driver.complete else "partial",
                       availability_reason=grounded.driver.availability_reason)
        if state["report_requested"]:
            grounded = apply_report_preferences(state["resolved_request"],grounded)
        composition = compose_report(state["resolved_request"], state["evidence"], grounded) if state["report_requested"] else None
        return {"grounded_report": grounded,
                "report_text_contract": ReportTextContract.capture(
                    golden_status=state.get("golden_status", "none"), golden=state.get("golden"),
                    empty_diagnosis=state.get("empty_diagnosis")),
                **({"report_composition": composition} if composition else {}), "content": _render(state["evidence"], draft, draft_error,
                                   report_requested=state["report_requested"],
                                   golden_status=state.get("golden_status", "none"),
                                   golden=state.get("golden"),
                                   empty_diagnosis=state.get("empty_diagnosis"),
                                   resolved_request=state["resolved_request"], grounded=grounded)}
