"""Deterministic slash grammar and local session adapter; no model routing."""
from dataclasses import dataclass
from datetime import datetime
from .deletion import DeletionPreview
import re
import shlex

from .conversation_service import ConversationService
from .navigation import NavigationResult, NavigationFailure
from .report_service import ReportService
from .store import StoreFailure

HELP = '''Commands:
/reports [--limit N] [--cursor CURSOR]
/report REPORT_ID
/conversations [--limit N] [--cursor CURSOR]
/resume CONVERSATION_ID
/new
/help
/schema
/exit or /quit
Pagination: N is 1..50 (default 20); cursor requires the same limit.
Supported analyses include revenue, product revenue drivers, regional purchaser
spend and country churn change. Historical report browsing does not refresh
analysis or inject evidence. Context expires within 30 minutes.
Deletion: request an exact preview, then confirm/cancel; scripts and JSON require
confirm TOKEN or cancel TOKEN. --user is a trusted local demo selector.'''


@dataclass(frozen=True)
class SlashCommand:
    operation: str
    argument: str = ''
    limit: int = 20
    cursor: str | None = None


def parse_slash(text):
    if not text.strip().startswith('/'):
        return None
    try:
        parts = shlex.split(text.strip())
    except ValueError:
        raise NavigationFailure('invalid_command') from None
    operation = parts[0][1:].casefold()
    args = parts[1:]
    if operation in {'reports', 'conversations'}:
        options = {}
        while args:
            if len(args)<2 or args[0] not in {'--limit','--cursor'} or args[0] in options:
                raise NavigationFailure('invalid_command')
            options[args[0]], args = args[1], args[2:]
        raw = options.get('--limit', '20')
        if not re.fullmatch(r'[0-9]{1,2}', raw) or not 1<=int(raw)<=50:
            raise NavigationFailure('invalid_page')
        return SlashCommand(operation, limit=int(raw), cursor=options.get('--cursor'))
    if operation in {'report', 'resume'}:
        if len(args)!=1 or not args[0].strip() or len(args[0])>200:
            raise NavigationFailure('invalid_command')
        if operation=='report' and (not re.fullmatch(r'[1-9][0-9]{0,17}',args[0])):
            raise NavigationFailure('invalid_command')
        return SlashCommand(operation, args[0])
    if operation in {'help','schema','new','exit','quit'} and not args:
        return SlashCommand(operation)
    raise NavigationFailure('invalid_command')


class ChatSession:
    def __init__(self, workflow, owner, conversation=None, *, store=None):
        self.workflow, self.owner = workflow, owner
        store = store if store is not None else workflow.store
        self.conversations = ConversationService(store)
        self.reports = ReportService.from_workflow(workflow, store=store)
        self.active = None
        self.pending_confirmation: DeletionPreview | None = None
        self.startup = self.conversations.new(owner) if conversation is None else self.conversations.resume(owner, conversation)
        if self.startup.status=='ok':
            self.active = self.startup.active_conversation

    def clear_confirmation(self):
        self.pending_confirmation = None

    def displayed(self, result):
        """Called only AFTER normal-human terminal output succeeds."""
        preview = getattr(result, 'confirmation', None)
        if (preview is not None and preview.owner == self.owner and preview.conversation_id == self.active
                and self.conversations.store._now() < datetime.fromisoformat(preview.expires_at)):
            self.pending_confirmation = preview

    def handle(self, question):
        operation = 'navigation'
        try:
            local = self.pending_confirmation
            self.clear_confirmation()
            if question.strip().casefold() in {'confirm', 'cancel'}:
                if (local is None or local.owner != self.owner or local.conversation_id != self.active
                        or self.conversations.store._now() >= datetime.fromisoformat(local.expires_at)):
                    return NavigationResult('deletion', status='confirmation_unavailable', active_conversation=self.active,
                        message='Request a fresh deletion preview, or use confirm TOKEN / cancel TOKEN explicitly.')
                question = question.strip().casefold() + ' ' + local.token
            command = parse_slash(question)
            if command is None:
                result = self.workflow.run(self.owner, question, conversation_id=self.active)
                self.active = result.conversation_id
                return result
            op = operation = command.operation
            if op=='reports':
                result = self.reports.list(self.owner,limit=command.limit,cursor=command.cursor)
            elif op=='report':
                result = self.reports.read(self.owner,int(command.argument))
            elif op=='conversations':
                result = self.conversations.list(self.owner,limit=command.limit,cursor=command.cursor)
            elif op=='resume':
                result = self.conversations.resume(self.owner, command.argument, active=self.active)
                if result.status=='ok':self.active = result.active_conversation
            elif op=='new':
                result = self.conversations.new(self.owner)
                self.active = result.active_conversation
            else:
                from .workflow import SCHEMA_HELP
                result = NavigationResult(op, message=SCHEMA_HELP if op=='schema' else HELP if op=='help' else '',
                                          active_conversation=self.active)
            return result
        except (NavigationFailure, StoreFailure) as exc:
            return NavigationResult(operation, status=exc.code, active_conversation=self.active,
                query_executions=self.reports.query_executions if operation in {'reports', 'report'} else 0,
                message='Command unavailable. Check syntax or retry the local/access dependency.')
