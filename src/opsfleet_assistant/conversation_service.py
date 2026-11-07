"""Owner-only metadata and selection; never restores or redates evidence."""
from dataclasses import dataclass, asdict
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
    def __init__(self, store):
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
