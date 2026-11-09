"""Trusted planning, bounded execution/recovery, and checked evidence release."""
from __future__ import annotations

from .bigquery_adapter import ExecutionError
from .compiler import CompiledQuery, compile_plan
from .recovery import FailureContext, SubmissionState, decide_recovery, terminal_outcome
from .contracts import AnalysisPlan, Metric, Operation
from .evidence import Evidence, protected_unavailable
from .identity import PolicyError, effective_brands, authorize_products
from .model_gateway import Mode, PlanEnvelope
from .golden import RetrievalFailure, apply_method
from .intent import analytical_plans, validate_bound_plans

from typing import TYPE_CHECKING
from .turn_context import CompletedAnalysis
from .turn_types import TurnState
if TYPE_CHECKING:
    from .turn_session import TurnSession


class AnalysisExecutor:
    """Own trusted plans, recovery and evidence release within one warehouse turn."""

    def plan(self, state: TurnState, turn: TurnSession) -> TurnState:
        turn.mark_stage("structured_planning")
        request = state["resolved_request"]
        assert turn.warehouse is not None
        authority = turn.authorized()
        scope, catalog = authority.scope, authority.catalog
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
            golden = turn.retrieve_method(request.spec, retrieval_scope)
            golden_status = "selected" if golden else "no_match"
        except RetrievalFailure:
            golden_status = "outage"
        turn.selected_golden, turn.golden_marker = golden, golden_status
        changed = tuple(apply_method(p, golden) for p in plans)
        validate_bound_plans(changed, request, effective_query_brands=query_brands)
        turn.context.trace.emit("golden", decision=golden_status, trio_id=golden.trio_id if golden else "unknown",
                   version=golden.version if golden else 0, method=golden.method if golden else "none",
                   operation=changed[0].operation.value, count=1 if changed != plans else 0)
        turn.context.trace.emit("policy", decision="approved", operation=changed[0].operation.value,
                   metric=changed[0].metric.value, version=1)
        return {"plan_envelope": PlanEnvelope(mode=request.spec.mode, plans=changed),
                "report_requested": request.spec.mode == Mode.REPORT or request.spec.report is not None,
                "golden": golden, "golden_status": golden_status}

    def authorize(self, state: TurnState, turn: TurnSession) -> TurnState:
        turn.mark_stage("authorization")
        if turn.warehouse is None:
            turn.open_warehouse()
        authority = turn.authorized()
        scope, catalog = authority.scope, authority.catalog
        if state.get("previous_scope_version") is not None and state["previous_scope_version"] != scope.authorization_version:
            raise PolicyError("scope_changed_start_new_conversation")
        queries = []
        for index, plan_item in enumerate(state["plan_envelope"].plans):
            try:
                query = compile_plan(plan_item, scope, catalog,
                    temporal_role=state["resolved_request"].spec.queries[index].temporal_role)
                turn.context.trace.emit("sql_validation", status="ok", operation=plan_item.operation.value,
                           metric=plan_item.metric.value)
            except PolicyError as exc:
                if exc.code != "compiler_syntax_error":
                    raise
                turn.context.trace.emit("sql_validation", status="error", category="syntax")
                decision = decide_recovery(FailureContext(
                    exc.code, "compile", SubmissionState.NOT_SUBMITTED,
                    budget_available=True, deadline_available=True))
                turn.context.trace.emit("recovery", action=decision.action, reason=decision.reason, outcome="unsupported")
                raise
            queries.append(query)
        return {"queries": tuple(queries), "scope_version": scope.authorization_version}

    def execute(self, state: TurnState, turn: TurnSession) -> TurnState:
        assert turn.warehouse is not None
        turn.mark_stage("step3_execution")
        queries = list(state["queries"])
        plans = list(state["plan_envelope"].plans)


        evidence = []
        for index, query in enumerate(queries):
            item, regenerated = self.run_with_recovery(turn, query, plans[index])
            queries[index] = regenerated
            turn.source_binding_version = regenerated.source_binding_version
            evidence.append(item)
            turn.completed_analysis = CompletedAnalysis(item.job_id, item.billed_bytes)
        if state["plan_envelope"].mode == Mode.INVESTIGATE_TOP_PRODUCTS:
            first = evidence[0]
            ids = tuple(row["product_id"] for row in first.rows)
            if len(ids) == 2:
                base = state["plan_envelope"].plans[0]
                second = AnalysisPlan(operation=Operation.PRODUCT_COMPARISON,
                                      metric=Metric.REVENUE, period=base.period,
                                      requested_brands=base.requested_brands,
                                      product_ids=ids, revenue_statuses=base.revenue_statuses)
                authority = turn.authorized()
                scope, catalog = authority.scope, authority.catalog
                second_query = compile_plan(second, scope, catalog)
                second_item, second_query = self.run_with_recovery(turn, second_query, second)
                turn.source_binding_version = second_query.source_binding_version
                evidence.append(second_item)
                turn.completed_analysis = CompletedAnalysis(second_item.job_id, second_item.billed_bytes)
                queries.append(second_query)
                plans.append(second)
        diagnosis = None
        for index, item in enumerate(evidence):
            if protected_unavailable(item):
                diagnosis = "unavailable"
                turn.context.trace.emit("empty_result", decision="unavailable", operation=item.operation.value)
                continue
            empty = (not item.rows or item.coverage.period_rows == 0
                     or item.operation == Operation.CHURN_SNAPSHOT and item.rows[0].get("eligible_customers") == 0)
            if not empty:
                continue
            diagnosis = "valid_empty"
            turn.context.trace.emit("empty_result", decision="source_empty" if item.coverage.source_rows == 0 else "valid_empty",
                       operation=item.operation.value)
            if not item.rows and item.coverage.period_rows:
                raise ExecutionError("coverage_invalid")
        return {"evidence": tuple(evidence), "queries": tuple(queries),
                "executed_plans": tuple(plans), "empty_diagnosis": diagnosis}

    def evidence_check(self, state: TurnState, turn: TurnSession) -> TurnState:
        turn.mark_stage("evidence_checks")
        if not state.get("evidence") or any(item.scope_reference != f"demo:{turn.context.owner}" or item.scope_version != state["scope_version"] for item in state["evidence"]):
            raise PolicyError("evidence_scope_mismatch")
        return {}

    def run_with_recovery(self, turn: TurnSession, query: CompiledQuery, source_plan: AnalysisPlan) -> tuple[Evidence, CompiledQuery]:
        self.check_versions(turn, query)
        if hasattr(turn.warehouse, "adopt_inspected_binding"):
            query = turn.warehouse.adopt_inspected_binding(query, source_plan, turn.context.owner)
            self.check_versions(turn, query)
        try:
            return turn.warehouse.execute_compiled(query), query
        except ExecutionError as exc:
            if exc.category not in {"query_syntax_error", "query_schema_error"}:
                raise
            available = (not turn.correction_started
                         and hasattr(turn.warehouse, "correct_schema_binding"))
            syntax_available = (not turn.correction_started
                                and hasattr(turn.warehouse, "correct_query_syntax")
                                and exc.purpose == 'analysis'
                                and exc.analysis_submission_state == SubmissionState.NOT_SUBMITTED)
            remaining = turn.warehouse.recovery_available() if hasattr(turn.warehouse, "recovery_available") else True
            decision = decide_recovery(FailureContext(
                exc.category, exc.stage,
                SubmissionState.SUBMITTED if exc.analysis_submission_state == SubmissionState.SUBMITTED else exc.submission_state,
                budget_available=remaining, deadline_available=turn.context.budget.remaining() > 0,
                schema_inspection_available=available, syntax_correction_available=syntax_available))
            if decision.action not in {"inspect_schema", "correct_syntax"}:
                if not exc.recovery_recorded:
                    turn.context.trace.emit("recovery", action=decision.action, reason=decision.reason, outcome=terminal_outcome(decision))
                raise
            turn.correction_started = True
            turn.context.trace.emit("recovery", action=decision.action, reason=decision.reason, outcome="attempted")
            corrected = None
            try:
                corrected = (turn.warehouse.correct_query_syntax(query, source_plan, turn.context.owner)
                             if decision.action == 'correct_syntax' else
                             turn.warehouse.correct_schema_binding(query, source_plan, turn.context.owner))
                self.check_versions(turn, corrected)
                turn.context.trace.emit("sql_validation", status="ok", operation=source_plan.operation.value,
                           metric=source_plan.metric.value)
                if decision.action == 'inspect_schema':
                    turn.context.trace.emit("recovery", action="recompile_supported_binding", reason="binding_changed", outcome="attempted")
                item = turn.warehouse.execute_compiled(corrected)
            except (ExecutionError, PolicyError) as failure:
                terminal = decide_recovery(FailureContext(
                    getattr(failure, "category", getattr(failure, "code", "unknown")),
                    getattr(failure, "stage", "unknown"),
                    SubmissionState.SUBMITTED if getattr(failure, "analysis_submission_state", "unknown") == "submitted"
                    else getattr(failure, "submission_state", SubmissionState.UNKNOWN),
                    budget_available=turn.warehouse.recovery_available(), deadline_available=turn.context.budget.remaining() > 0))
                if decision.action == "correct_syntax" or not getattr(failure, "recovery_recorded", False):
                    outcome = terminal_outcome(terminal)
                    if decision.action == 'correct_syntax':
                        unsupported = corrected is None and getattr(failure, 'code', '') == 'query_syntax_error'
                        outcome = 'unsupported' if unsupported else 'failed'
                    turn.context.trace.emit("recovery", action=decision.action,
                                            reason=terminal.reason, outcome=outcome)
                raise
            return item, corrected

    @staticmethod
    def check_versions(turn: TurnSession, query: CompiledQuery) -> None:
        versions = turn.context.versions
        if (query.compiler_version != versions.compiler
                or query.field_policy_version != versions.field_policy
                or query.source_binding_version not in versions.approved_source_bindings):
            raise PolicyError("recovery_scope_changed")
