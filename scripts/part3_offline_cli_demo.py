"""Real CLI/service/workflow/store demonstration with synthetic, isolated inputs.

All public output is a small assertion summary. Capability values never print.
Injected interpretation and SQLite source rows do not certify live/human UX.
"""
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

os.environ['OPSFLEET_LIVE_TESTS'] = '0'
def offline(*args, **kwargs):
    raise RuntimeError('Offline demonstration: external transport disabled')
socket.socket.connect = offline
socket.socket.connect_ex = offline
socket.create_connection = offline
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]

from typer.testing import CliRunner
from opsfleet_assistant import cli, workflow as workflow_module
from opsfleet_assistant.bigquery_adapter import BigQueryTurn
from opsfleet_assistant.workflow import AssistantWorkflow
from opsfleet_assistant.store import SQLiteStore
from opsfleet_assistant.telemetry import TurnTrace
from test_playbooks import PlaybookGateway, SETTINGS, spec
from playbook_source import product_source


def main():
    runner = CliRunner()
    client = product_source()
    clock = [datetime(2026, 11, 6, 12, tzinfo=timezone.utc)]
    turns = []
    class Gateway(PlaybookGateway):
        pass
    Gateway.spec = spec(report=True)
    def turn_factory(settings, *, limits):
        turn = BigQueryTurn(settings, client=client, limits=limits)
        turns.append(turn)
        return turn
    class OfflineWorkflow(AssistantWorkflow):
        def __init__(self, settings, store):
            super().__init__(SETTINGS, store, gateway_factory=Gateway,
                             turn_factory=turn_factory, today=date(2026, 11, 6))
    class ClockStore(SQLiteStore):
        def __init__(self, path):
            super().__init__(path, clock=lambda: clock[0])
    cli.Settings = SimpleNamespace(load=lambda: SETTINGS)
    cli.AssistantWorkflow = OfflineWorkflow
    cli.SQLiteStore = ClockStore
    with TemporaryDirectory(prefix='opsfleet-part3-offline-') as directory:
        temp = Path(directory)
        db = temp/'synthetic.sqlite'
        class Trace(TurnTrace):
            def __init__(self, c, t):
                super().__init__(c, t, temp/'events.jsonl')
        workflow_module.TurnTrace = Trace
        def invoke(command, *args, input=None, user='manager_apparel', expected=0, structured=True):
            result = runner.invoke(cli.app, [command, *map(str,args), '--user', user, '--db', str(db),
                                   *(['--json'] if structured else [])], input=input)
            # Never dump captured output: previews include capability tokens.
            assert result.exit_code == expected, 'Synthetic CLI outcome mismatch'
            return json.loads(result.stdout) if structured else result.stdout
        first = invoke('ask', 'Synthetic product investigation')
        saved = invoke('ask', 'save', '--conversation', first['conversation_id'])
        rid = saved['report_id']; cid = saved['conversation_id']
        before = len(client.calls)
        page = invoke('reports', '--limit', 1)
        read = invoke('report', rid)
        assert read['content']['text'] == saved['text']
        assert read['content']['completeness'] == saved['report_completeness']
        assert read['content']['support']['snapshot']['evidence'][0]['rows'] == saved['presentation']['evidence'][0]['rows']
        assert len(page['items'])==1 and page['next_cursor']
        assert {stage for stage, dry, sql in client.calls[before:]} == {'catalog'}
        assert page['model_requests']==read['model_requests']==0
        assert page['query_executions']==read['query_executions']==1
        metadata_before = len(client.calls)
        conversations = invoke('conversations')
        assert conversations['items'][0]['conversation_id'] == cid
        help_text = invoke('chat', input='/help\n/schema\n/exit\n', structured=False)
        assert '/reports' in help_text and len(client.calls)==metadata_before
        resume_human = invoke('chat', input='/resume '+cid+'\nsave\n/exit\n', structured=False)
        assert 'Original date anchors retained' in resume_human and 'Complete report' in resume_human
        resumed = invoke('ask', 'save', '--conversation', cid)
        assert resumed['driver_finding']==saved['driver_finding']
        unknown = invoke('report', rid, user='manager_casual', expected=1)
        assert unknown['status']=='unavailable' and unknown['content'] is None
        client.fault='coverage'; client.fault_stage='catalog'
        unavailable = invoke('report', rid, expected=1)
        assert unavailable['status']=='access_check_unavailable' and unavailable['content'] is None
        client.fault=None
        state=SQLiteStore(db, clock=lambda:clock[0])
        digest=state.report('manager_apparel',rid)['report_binding_digest']
        state.db.execute('UPDATE reports SET report_binding_digest=? WHERE id=?',('corrupt',rid))
        assert invoke('report',rid,expected=1)['status']=='unavailable'
        state.db.execute('UPDATE reports SET report_binding_digest=? WHERE id=?',(digest,rid))
        state.close()
        # Expired continuity remains metadata; the historical report still reads.
        clock[0] += timedelta(hours=1)
        expired_conversation = next(item for item in invoke('conversations')['items'] if item['conversation_id'] == cid)
        assert expired_conversation['continuity']=='expired'
        assert invoke('report', rid)['content']['text']==saved['text']
        resume_text = invoke('chat', '--conversation', cid, input='/exit\n', structured=False)
        assert 'Fresh analysis required' in resume_text
        expired_save = invoke('ask', 'save', '--conversation', cid)
        assert expired_save['report_id'] is None
        # Revoke a product while retaining the configured brand elsewhere.
        client.db.execute("INSERT INTO products VALUES(4,'Carhartt')")
        client.db.execute("UPDATE products SET brand='Hanes' WHERE id=2")
        revoked = invoke('report', rid, expected=1)
        assert revoked['status']=='unavailable' and revoked['content'] is None
        client.db.execute("UPDATE products SET brand='Carhartt' WHERE id=2")
        # Human adapter displays a typed preview and resolves bare commands.
        cancel_text = invoke('chat', '--conversation', cid,
            input='Delete all the reports we made in this conversation\ncancel\n/exit\n', structured=False)
        assert 'Deletion proposal cancelled' in cancel_text
        assert invoke('report', rid)['status']=='ok'
        confirm_text = invoke('chat', '--conversation', cid,
            input='Delete all the reports we made in this conversation\nconfirm\n/exit\n', structured=False)
        assert 'Soft-deleted' in confirm_text
        assert invoke('report', rid, expected=1)['status']=='unavailable'
        print(json.dumps(dict(mode='OFFLINE SYNTHETIC', restart_list_read=True,
            original_content_and_precision=True, historical_read_after_expiry=True,
            expired_save_blocked=True, unavailable_catalog_and_corruption_blocked=True, other_owner_and_revocation_blocked=True,
            human_cancel_and_confirm=True, capability_values='REDACTED',
            navigation_model_requests=0, business_queries=0, catalog_executions_per_report_command=1,
            help_schema_conversation_warehouse_executions=0)))
    client.db.close()


if __name__ == '__main__':
    main()
