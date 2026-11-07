"""Authorized historical report reads. One bounded catalog snapshot per command."""
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Callable

from .bigquery_adapter import QueryLimits
from .identity import resolve_demo_scope, Catalog
from .contracts import BrandScope
from .navigation import NavigationResult, NavigationFailure, position
from .saved_report import validate_saved_report


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
    def __init__(self, store, authorization: Callable[[str], tuple[BrandScope, Catalog]]):
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


def _safe_title(content):
    from .store import SQLiteStore
    return SQLiteStore._title(content)
