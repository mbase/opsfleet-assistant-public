"""Authorized historical report reads. One bounded catalog snapshot per command."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Callable

from .bigquery_adapter import QueryLimits
from .store import SQLiteStore
from .turn_types import TurnState, TurnResult
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .turn_session import TurnSession
from .conversation import AnalysisSnapshot, request_role_anchors
from .contracts import AnalysisPlan, InterpretationResult, Mode as RequestMode, CohortOrigin, Operation
from .report_artifact import ReportArtifact, evidence_references
from .workflow_helpers import scope_digest
from .presentation import display_payload
from .identity import resolve_demo_scope, Catalog, effective_brands
from .contracts import BrandScope
from .navigation import NavigationResult, NavigationFailure, position
from .saved_report import validate_saved_report
from .reporting import report_spec


@dataclass(frozen=True)
class ReportMetadata:
    id: int
    title: str
    created_at: str
    conversation_id: str
    completeness: str
    analytical_period: dict | None
    as_of: tuple[str, ...]


@dataclass(frozen=True)
class ReportContent:
    metadata: ReportMetadata
    text: str
    support: dict | None

    def payload(self):
        return {**asdict(self.metadata), 'text': self.text, 'support': self.support}


class ReportService:
    def __init__(self, store: SQLiteStore, authorization: Callable[[str], tuple[BrandScope, Catalog]]):
        self.store, self.authorization = store, authorization
        self.query_executions = 0
        self.billed_bytes = 0

    @classmethod
    def from_workflow(cls, workflow, *, store=None):
        def authorization(owner):
            # No separate retry owner: one execution allowance means no second
            # catalog execution, even after a classified pre-submission failure.
            turn = workflow.turn_factory(workflow.settings, limits=QueryLimits(max_executions=1,
                job_seconds=30, turn_seconds=30))
            try:
                catalog = turn.load_catalog()
                return resolve_demo_scope(owner, catalog), catalog
            finally:
                service.query_executions = turn.executions
                service.billed_bytes = turn.billed_bytes
        service = cls(store if store is not None else workflow.store, authorization)
        return service

    def _authorization(self, owner):
        try:
            return self.authorization(owner)
        except Exception:
            raise NavigationFailure('access_check_unavailable') from None

    @staticmethod
    def _checked(fields, scope, catalog):
        try:
            if (type(fields['id']) is not int or fields['id'] < 1
                    or not isinstance(fields['created_at'], str) or len(fields['created_at']) > 64
                    or not isinstance(fields['conversation_id'], str) or not 1 <= len(fields['conversation_id']) <= 200):
                return None
            datetime.fromisoformat(fields['created_at'])
            return validate_saved_report(fields, scope, catalog)
        except Exception:
            # Corrupt/unknown persisted shapes never expose parser details or
            # break the interactive adapter. Process-control exceptions propagate.
            return None

    @staticmethod
    def _summary(fields, checked):
        snapshot = checked.artifact.snapshot if checked.artifact else None
        return ReportMetadata(id=fields['id'], title=_safe_title(fields['content']), created_at=fields['created_at'],
            conversation_id=fields['conversation_id'], completeness=checked.completeness,
            analytical_period=snapshot.request.analysis.model_dump(mode='json') if snapshot else None,
            as_of=tuple(e.query_as_of_utc for e in snapshot.evidence) if snapshot else ())

    def list(self, owner, *, limit=20, cursor=None):
        self.query_executions, self.billed_bytes = 0, 0
        try:
            before = int(position(self.store, owner, 'reports', limit, cursor, 9223372036854775807))
        except (TypeError, ValueError):
            raise NavigationFailure('invalid_cursor') from None
        rows = self.store.report_candidates(owner, before=before, limit=limit+1)
        scope, catalog = self._authorization(owner)
        items = []
        for fields in rows[:limit]:
            checked = self._checked(fields, scope, catalog)
            if checked:
                items.append(asdict(self._summary(fields, checked)))
        next_cursor = self.store.navigation_cursor(owner, 'reports', limit, str(rows[limit-1]['id'])) if len(rows)>limit else None
        return NavigationResult('reports', items=tuple(items), next_cursor=next_cursor, query_executions=self.query_executions,
            message='Historical snapshots; sparse pages may occur. Use /report ID to read.')

    def read(self, owner, report_id):
        self.query_executions, self.billed_bytes = 0, 0
        if type(report_id) is not int or not 1 <= report_id < 9223372036854775807:
            return NavigationResult('report', status='unavailable', message='Report unavailable.')
        fields = self.store.report(owner, report_id)
        if fields is None:
            return NavigationResult('report', status='unavailable', message='Report unavailable.', query_executions=self.query_executions)
        scope, catalog = self._authorization(owner)
        checked = self._checked(fields, scope, catalog)
        if checked is None:
            return NavigationResult('report', status='unavailable', message='Report unavailable.', query_executions=self.query_executions)
        content = ReportContent(self._summary(fields, checked), fields['content'],
            checked.artifact.model_dump(mode='json') if checked.artifact else None)
        return NavigationResult('report', query_executions=self.query_executions, content=content.payload(),
            message='Historical saved content. Reading does not refresh evidence or conversation context.')

    def authorize_preview(self, owner, fields):
        """All-or-nothing gate, called inside proposal selection transaction."""
        if not fields:
            return
        scope, catalog = self._authorization(owner)
        if any(self._checked(row, scope, catalog) is None for row in fields):
            raise NavigationFailure('report_unavailable')

    def replay_turn(self, turn: TurnSession, reused: dict) -> TurnResult:
        if reused.get("deleted"):
            return TurnResult("deleted", "This saved report was soft-deleted and cannot be replayed. Start a new turn for a new report.",
                              turn.context.conversation_id, turn.context.turn_id)
        turn.open_warehouse()
        catalog = turn.warehouse.load_catalog()
        scope = turn.resolve_scope(turn.context.owner, catalog)
        from .saved_report import validate_saved_report
        checked = validate_saved_report(reused["fields"], scope, catalog, field_policy_version=turn.context.versions.field_policy)
        artifact, stored_grounded, stored_composition = checked.artifact, checked.grounded, checked.composition
        durable = artifact is not None
        reused["report_completeness"] = checked.completeness
        return TurnResult("partial" if reused["report_completeness"] == "partial" or reused["content"].startswith("Partial report:") else "ok", reused["content"], turn.context.conversation_id, turn.context.turn_id,
                          query_executions=turn.warehouse.executions, billed_bytes=turn.warehouse.billed_bytes,
                          evidence_job_ids=tuple(ref["job_id"] for ref in reused["evidence_refs"]),
                          report_id=reused["id"], reused_report=True,
                          report_completeness=reused["report_completeness"],
                          presentation=display_payload(artifact.snapshot.evidence, stored_grounded,
                              artifact.snapshot.request) if durable else None,
                          report_composition=stored_composition.model_dump(mode="json") if stored_composition else None,
                          resolved_periods={role: getattr(stored_composition.request, role).model_dump(mode="json")
                              if getattr(stored_composition.request, role) else None
                              for role in ("analysis", "comparison", "action")} if stored_composition else None,
                          driver_finding=stored_grounded.driver.model_dump(mode="json") if stored_grounded and stored_grounded.driver else None,
                          factual_status=stored_grounded.factual_status if stored_grounded else None,
                          factual_findings=tuple(f.model_dump(mode="json") for f in stored_grounded.findings) if stored_grounded else (),
                          suggestions=tuple({"kind": kind, **s.model_dump(mode="json")} for kind in ("recommendations", "hypotheses")
                                            for s in getattr(stored_grounded, kind)) if stored_grounded else ())

    def persist_turn(self, state: TurnState, turn: TurnSession) -> TurnState:
        turn.mark_stage("display_save")
        if "evidence" not in state:
            return {}
        last = state["executed_plans"][0]
        if state.get("contextual_only"):
            assert turn.continuity and turn.continuity.snapshot
            snapshot = turn.continuity.snapshot
        else:
            snapshot = AnalysisSnapshot.capture(reference_date=turn.context.reference_date, previous_plan=state.get("previous_plan"),
                role_anchors=state.get("resolution_role_anchors"),
                created_at=self.store._now(), plans=state["executed_plans"],
                evidence=state["evidence"], request=state["resolved_request"], grounded=state["grounded_report"])
        continuity = turn.conversations.updated_context(turn, snapshot=snapshot, preserve_expiry=bool(state.get("contextual_only")
            or turn.continuity and turn.continuity.pending and state["interpretation"].context_action in {"continue", "correct"}))
        cohort_origin = (CohortOrigin.DEMOGRAPHIC if any(p.operation in {Operation.DEMOGRAPHIC_COMPARISON, Operation.REGIONAL_SPEND, Operation.CHURN_CHANGE} for p in state["executed_plans"])
                         else CohortOrigin.BUSINESS)
        if not state["report_requested"]:
            if not state.get("contextual_only"):
                self.store.save_context(turn.context.owner, turn.context.conversation_id, last, cohort_origin, state["scope_version"])
            self.store.save_continuity(turn.context.owner, turn.context.conversation_id, continuity)
            turn.context.trace.emit("report_persistence", decision="saved", status="ok")
            return {"report_id": None}
        evidence = state["evidence"]
        report_id = self.store.save_turn_report(
            owner=turn.context.owner, conversation_id=turn.context.conversation_id, turn_id=turn.context.turn_id,
            plan=last, cohort_origin=cohort_origin,
            content=state["content"],
            grounded_report=state["grounded_report"].model_dump(mode="json"),
            continuity=continuity,
            report_support=ReportArtifact(schema_version=2 if snapshot.schema_version == 3 else 1,
                snapshot=snapshot.model_copy(update={"report_id": None}),
                rendering=state["report_text_contract"]).model_dump(mode="json"),
            report_completeness=state["report_composition"].completeness,
            report_composition=state["report_composition"].model_dump(mode="json"),
            metric_definitions=[item.metric_definition for item in evidence],
            scope_reference=evidence[0].scope_reference,
            scope_version=evidence[0].scope_version,
            request_digest=state["request_digest"], identity_digest=state["identity_digest"],
            plans=[plan.model_dump(mode="json") for plan in state["executed_plans"]],
            scope_digest=scope_digest(state["queries"]),
            evidence_refs=evidence_references(evidence),
        )
        turn.context.trace.emit("report_persistence", decision="saved", status=state["report_composition"].completeness)
        return {"report_id": report_id}

    @staticmethod
    def save_modifies_snapshot(interpretation: InterpretationResult, snapshot: AnalysisSnapshot,
                               scope: BrandScope, catalog: Catalog, reference_date: date,
                               previous_plan: AnalysisPlan | None) -> bool:
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
        if (proposed.queries or proposed.playbook is not None) and proposed.playbook != original.playbook:
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


def _safe_title(content):
    from .store import SQLiteStore
    return SQLiteStore._title(content)
