"""Owner-only metadata and selection; never restores or redates evidence."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import timedelta
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .turn_session import TurnSession
from .contracts import CohortOrigin, Operation, RequestSpec, InterpretationIssue
from .conversation import AnalysisSnapshot, PendingTemporalResolution, SafeMessage, history, safe_spec
from .store import SQLiteStore
from uuid import uuid4
from pydantic import ValidationError
from .conversation import ConversationContext
from .navigation import NavigationResult, position


@dataclass(frozen=True)
class ConversationMetadata:
    conversation_id: str
    updated_at: str
    continuity: str


class ConversationService:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def _availability(self, raw):
        if not raw:
            return 'unavailable'
        try:
            context = ConversationContext.model_validate_json(raw)
            if self.store._now() < context.created_at or self.store._now() >= context.expires_at:
                return 'expired'
            return 'available_pending_revalidation'
        except (ValueError, ValidationError):
            return 'incompatible'

    def list(self, owner, *, limit=20, cursor=None):
        after = position(self.store, owner, 'conversations', limit, cursor, '')
        rows = self.store.conversation_metadata(owner, after=after, limit=limit+1)
        items = tuple(asdict(ConversationMetadata(r['conversation_id'], r['updated_at'],
            self._availability(r['continuity_json']))) for r in rows[:limit])
        next_cursor = self.store.navigation_cursor(owner, 'conversations', limit, rows[limit-1]['conversation_id']) if len(rows)>limit else None
        return NavigationResult('conversations', items=items, next_cursor=next_cursor)

    def resume(self, owner, conversation_id, *, active=None):
        if (not conversation_id or not conversation_id.strip() or len(conversation_id)>200
                or not self.store.conversation_exists(owner, conversation_id)):
            return NavigationResult('resume', status='unavailable', active_conversation=active,
                                    message='Conversation unavailable; selection unchanged.')
        availability = self._availability(self.store.conversation_payload(owner, conversation_id))
        return NavigationResult('resume', active_conversation=conversation_id, content={'continuity': availability},
            message='Original date anchors retained; analytical reuse rechecks current scope.' if availability=='available_pending_revalidation'
            else 'Fresh analysis required; expired or incompatible evidence is unavailable.')

    def new(self, owner):
        conversation_id = str(uuid4())
        self.store.create_conversation(owner, conversation_id)
        return NavigationResult('new', active_conversation=conversation_id,
            message='Fresh conversation created. Previous saved work is retained.')

    def updated_context(self, turn: TurnSession, *, pending: RequestSpec | None = None,
                        pending_resolution: PendingTemporalResolution | None = None,
                        issues: tuple[InterpretationIssue, ...] = (), snapshot: AnalysisSnapshot | None = None,
                        clarification: str | None = None, preserve_expiry: bool = False,
                        reset_history: bool = False) -> ConversationContext:
        assert turn.active_scope is not None
        now = turn.store._now()
        messages = (turn.continuity.messages if turn.continuity and not reset_history else ()) + (SafeMessage(role="user", content=turn.clean_question),)
        if clarification:
            messages += (SafeMessage(role="assistant", content=clarification),)
        messages = history(messages, max_messages=turn.settings.history_max_messages,
                           max_chars=turn.settings.history_max_chars)
        demographic = turn.cohort == CohortOrigin.DEMOGRAPHIC
        if pending and any(q.operation in {Operation.DEMOGRAPHIC_COMPARISON, Operation.REGIONAL_SPEND, Operation.CHURN_CHANGE} for q in pending.queries):
            demographic = True
        if snapshot and any(p.operation in {Operation.DEMOGRAPHIC_COMPARISON, Operation.REGIONAL_SPEND, Operation.CHURN_CHANGE} for p in snapshot.plans):
            demographic = True
        return ConversationContext(owner=turn.context.owner, identity_digest=turn.identity_digest,
            authorization_version=turn.active_scope.authorization_version, catalog_digest=turn.catalog_digest,
            identity_brands=tuple(sorted(turn.active_scope.brands)), identity_role=turn.active_scope.role.value,
            created_at=turn.continuity.created_at if preserve_expiry and turn.continuity else now,
            expires_at=turn.continuity.expires_at if preserve_expiry and turn.continuity else now + timedelta(seconds=turn.settings.context_ttl_seconds),
            cohort_origin=CohortOrigin.DEMOGRAPHIC if demographic else CohortOrigin.BUSINESS,
            messages=messages, pending=safe_spec(pending), pending_resolution=pending_resolution,
            issues=issues, snapshot=snapshot)
