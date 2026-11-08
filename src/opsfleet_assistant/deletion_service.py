"""Deterministic deletion routing over atomic, owner-bound store operations."""
from __future__ import annotations

from .deletion import DeletionCommand, DeletionPreview, DeletionTarget
from .navigation import NavigationFailure
from .store import StoreFailure

from typing import TYPE_CHECKING
from .turn_types import TurnResult, DeletionTurnResult
if TYPE_CHECKING:
    from .turn_session import TurnSession


class DeletionService:
    """Typed preview/confirm/cancel route; the store owns the atomic operation."""

    def run(self, command: DeletionCommand, turn: TurnSession) -> TurnResult:
        turn.context.trace.emit("stage", stage=("deletion_preview" if command.action == "preview" else
                                   "deletion_cancel" if command.action == "cancel" else "deletion_confirm"), status="start")
        boundary = " --user is a local demo identity selector, not authentication."
        try:
            if command.action == "ambiguous":
                return TurnResult("clarification", "Use 'Delete all reports mentioning <literal>' or "
                                  "'Delete all the reports we made in this conversation'." + boundary,
                                  turn.context.conversation_id, turn.context.turn_id)
            if command.action == "preview":
                if command.selector == "conversation" and not turn.supplied_conversation:
                    return TurnResult("clarification", "Provide --conversation with the exact conversation ID." + boundary,
                                      turn.context.conversation_id, turn.context.turn_id)
                value = turn.supplied_conversation if command.selector == "conversation" else command.value
                assert value is not None
                from .report_service import ReportService
                preview_service = turn.preview_service()
                preview = turn.store.preview_deletion(turn.context.owner, command.selector, value,
                    conversation_id=turn.supplied_conversation,
                    disclosure_check=preview_service.authorize_preview)
                if not preview["targets"]:
                    turn.context.trace.emit("deletion", decision="no_targets", status="ok", count=0)
                    return TurnResult("ok", "No active owned reports match. Nothing was deleted; no proposal was created." + boundary,
                                      turn.context.conversation_id, turn.context.turn_id)
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
                turn.context.trace.emit("deletion", decision="previewed", status="confirmation_required", count=len(preview["targets"]))
                return DeletionTurnResult("confirmation_required", "\n".join(lines), turn.context.conversation_id, turn.context.turn_id,
                    query_executions=preview_service.query_executions, billed_bytes=preview_service.billed_bytes,
                    confirmation=DeletionPreview(turn.context.owner, turn.context.conversation_id, preview["token"], preview["expires_at"],
                        tuple(DeletionTarget(**t) for t in preview["targets"])))
            state, ids = turn.store.resolve_deletion(turn.context.owner, command.value, cancel=command.action == "cancel")
            turn.context.trace.emit("deletion", decision=state if state in {"deleted", "cancelled", "stale", "expired"} else "failed",
                       status="ok" if state in {"deleted", "cancelled"} else "error", count=len(ids))
            if state == "deleted":
                message = f"Soft-deleted {len(ids)} report(s): {', '.join(map(str, ids))}. They are no longer active; bytes remain in SQLite pending a retention/purge policy."
                return TurnResult("ok", message + boundary, turn.context.conversation_id, turn.context.turn_id)
            if state == "cancelled":
                return TurnResult("ok", "Deletion proposal cancelled. No reports changed." + boundary,
                                  turn.context.conversation_id, turn.context.turn_id)
            if state == "stale":
                message = "A previewed report changed or disappeared. Nothing was deleted; request a fresh preview."
            elif state == "expired":
                message = "The five-minute proposal expired. Nothing was deleted; request a fresh preview."
            else:
                message = "No active deletion proposal matches this token and identity. Nothing was deleted."
            return TurnResult("error", message + boundary, turn.context.conversation_id, turn.context.turn_id)
        except (StoreFailure, NavigationFailure) as exc:
            turn.context.trace.emit("deletion", decision="failed", status="error", category="storage")
            message = ("Selection is too broad; narrow the request." if exc.code == "selection_too_broad"
                       else "Unable to verify report access for this preview. Nothing was disclosed or deleted."
                       if isinstance(exc, NavigationFailure)
                       else "Unable to complete report deletion: local state store unavailable.")
            return TurnResult("error", message + boundary, turn.context.conversation_id, turn.context.turn_id, error_category=exc.code,
                query_executions=preview_service.query_executions if "preview_service" in locals() else 0,
                billed_bytes=preview_service.billed_bytes if "preview_service" in locals() else 0)
