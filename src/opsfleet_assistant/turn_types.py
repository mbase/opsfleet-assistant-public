"""Stable graph state and public result contracts; no orchestration imports at runtime."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from .compiler import CompiledQuery
from .report_text import ReportTextContract
from .deletion import DeletionPreview
from .contracts import AnalysisPlan, CohortOrigin, InterpretationResult, ResolvedRequest
from .evidence import Evidence
from .model_gateway import PlanEnvelope, ReportDraft
from .reporting import GroundedReport, ReportComposition
from .golden import Selection

from typing import TYPE_CHECKING, Protocol
from .turn_context import TurnContext
if TYPE_CHECKING:
    from .request_resolver import RequestResolver
    from .analysis_executor import AnalysisExecutor
    from .report_service import ReportService

class TurnRuntime(Protocol):
    context: TurnContext
    resolver: RequestResolver
    executor: AnalysisExecutor
    reports: ReportService

    def screen(self, state: TurnState) -> TurnState: ...
    def draft(self, state: TurnState) -> TurnState: ...
    def claim_validation(self, state: TurnState) -> TurnState: ...


class TurnState(TypedDict, total=False):
    runtime: TurnRuntime
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
    report_text_contract: ReportTextContract
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
    failure_stage: str = "unknown"
    failure_purpose: str = "unknown"
    analysis_submission_state: str = "unknown"
    analysis_job_id: str | None = None
    analysis_billed_bytes: int | None = None
    failure_submission_state: str = "unknown"
    failure_job_id: str | None = None
    failure_billed_bytes: int | None = None
    interpretation_status: str | None = None
    resolved_periods: dict[str, Any] | None = None
    factual_status: str | None = None
    driver_finding: dict | None = None
    factual_findings: tuple[dict[str, Any], ...] = ()
    suggestions: tuple[dict[str, Any], ...] = ()
    report_completeness: str | None = None
    report_composition: dict[str, Any] | None = None
    presentation: dict[str, Any] | None = None


@dataclass(frozen=True)
class DeletionTurnResult(TurnResult):
    confirmation: DeletionPreview | None = None
