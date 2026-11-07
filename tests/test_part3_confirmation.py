"""The displayed local capability binds an exact persisted snapshot across sessions."""
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from opsfleet_assistant import cli
from opsfleet_assistant.cli_commands import ChatSession
from opsfleet_assistant.store import SQLiteStore
from opsfleet_assistant.workflow import AssistantWorkflow
from test_playbooks import run_workflow, spec, product_rows, SETTINGS

OWNER='manager_apparel'


def setup(tmp_path):
    wf,store,r=run_workflow(tmp_path,spec(report=True),product_rows())
    session=ChatSession(wf,OWNER,r.conversation_id)
    return wf,store,r,session


def preview(session):
    result=session.handle('Delete all the reports we made in this conversation')
    assert result.status=='confirmation_required',result.text
    assert result.model_requests==0 and result.query_executions==1
    assert result.confirmation is not None
    session.displayed(result)
    return result


@pytest.mark.parametrize('action',['confirm','cancel'])
def test_displayed_preview_exact_resolution_and_replay(tmp_path,action):
    wf,store,r,session=setup(tmp_path)
    first=preview(session)
    done=session.handle(action)
    assert done.status=='ok' and done.query_executions==done.model_requests==0
    assert (store.report(OWNER,r.report_id) is None)==(action=='confirm')
    assert session.pending_confirmation is None
    assert session.handle(action).status=='confirmation_unavailable'
    replay=wf.run(OWNER,action+' '+first.confirmation.token)
    assert replay.status=='error'
    events=(tmp_path/'workflow-events.jsonl').read_text()
    assert first.confirmation.token not in events
    store.close()


@pytest.mark.parametrize('command',['/help','/schema','/reports','/report 99999','/conversations','/new','/resume missing','/unknown','/reports --limit 0','unrelated analysis','yes'])
def test_unrelated_input_invalidates_local_only(tmp_path,command):
    wf,store,r,session=setup(tmp_path)
    first=preview(session)
    session.handle(command)
    assert session.pending_confirmation is None
    assert session.handle('confirm').status=='confirmation_unavailable'
    # Clearing local state must not silently cancel the persisted proposal.
    assert wf.run(OWNER,'cancel '+first.confirmation.token).status=='ok'
    assert store.report(OWNER,r.report_id)
    store.close()


def test_not_displayed_restart_expiry_owner_conversation_and_errors(tmp_path):
    wf,store,r,session=setup(tmp_path)
    unseen=session.handle('Delete all the reports we made in this conversation')
    assert session.handle('confirm').status=='confirmation_unavailable'
    for mutation in ('restart','expiry','owner','conversation','store'):
        session=ChatSession(wf,OWNER,r.conversation_id)
        first=preview(session)
        if mutation=='restart':session=ChatSession(wf,OWNER,r.conversation_id)
        elif mutation=='expiry':
            clock=store._now;store._now=lambda:clock()+timedelta(minutes=6)
        elif mutation=='owner':session.owner='manager_casual'
        elif mutation=='conversation':session.active='other'
        else:
            store.db.execute("UPDATE deletion_proposals SET state='cancelled' WHERE token_digest=?",(__import__('hashlib').sha256(first.confirmation.token.encode()).hexdigest(),))
        result=session.handle('confirm')
        assert result.status in {'confirmation_unavailable','error'}
        assert session.pending_confirmation is None and store.report(OWNER,r.report_id)
        if mutation=='expiry':store._now=clock
    store.close()


def test_two_sessions_keep_own_proposals_and_new_targets_are_excluded(tmp_path):
    wf,store,r,one=setup(tmp_path)
    # Another connection, same owner/conversation, independently displayed preview.
    other=SQLiteStore(store.path)
    wf2=AssistantWorkflow(SETTINGS,other,turn_factory=wf.turn_factory,gateway_factory=wf.gateway_factory,today=wf.today)
    two=ChatSession(wf2,OWNER,r.conversation_id)
    a=preview(one)
    # New exact-save report is absent from A, present in B.
    new=wf.run(OWNER,'save',conversation_id=r.conversation_id)
    assert new.report_id!=r.report_id
    b=preview(two)
    assert a.confirmation.token!=b.confirmation.token
    assert [t.id for t in a.confirmation.targets]==[r.report_id]
    assert {t.id for t in b.confirmation.targets}=={r.report_id,new.report_id}
    assert one.handle('confirm').status=='ok'
    assert store.report(OWNER,new.report_id)
    # B's original set changed; never reselect or partially delete the remainder.
    stale=two.handle('confirm')
    assert stale.status=='error' and 'changed or disappeared' in stale.text
    assert store.report(OWNER,new.report_id)
    assert two.pending_confirmation is None
    other.close();store.close()


def test_two_sessions_different_sets_each_confirms_own_snapshot(tmp_path):
    wf,store,r,one=setup(tmp_path)
    second=wf.run(OWNER,'offline investigation')
    other=SQLiteStore(store.path)
    wf2=AssistantWorkflow(SETTINGS,other,turn_factory=wf.turn_factory,gateway_factory=wf.gateway_factory,today=wf.today)
    two=ChatSession(wf2,OWNER,second.conversation_id)
    a=preview(one);b=preview(two)
    assert [t.id for t in a.confirmation.targets]==[r.report_id]
    assert [t.id for t in b.confirmation.targets]==[second.report_id]
    assert one.handle('confirm').status=='ok'
    assert store.report(OWNER,second.report_id)
    assert two.handle('confirm').status=='ok'
    assert store.report(OWNER,second.report_id) is None
    other.close();store.close()


def test_new_preview_replaces_local_reference_only_and_version_change_atomic(tmp_path):
    wf,store,r,session=setup(tmp_path)
    a=preview(session);b=preview(session)
    assert session.pending_confirmation==b.confirmation
    store.db.execute('UPDATE reports SET version=version+1 WHERE id=?',(r.report_id,))
    stale=session.handle('confirm')
    assert stale.status=='error' and store.report(OWNER,r.report_id)
    assert wf.run(OWNER,'cancel '+a.confirmation.token).status=='ok'
    store.close()


def test_fail_closed_preview_before_titles_or_proposal(tmp_path):
    wf,store,r,session=setup(tmp_path)
    store.db.execute('UPDATE reports SET report_support_json=?,report_binding_digest=?,content=?',('','','PRIVATE TITLE'))
    result=session.handle('Delete all the reports we made in this conversation')
    assert result.status=='error' and 'PRIVATE' not in result.text
    assert result.error_category=='report_unavailable'
    assert store.db.execute('SELECT count(*) FROM deletion_proposals').fetchone()[0]==0
    assert session.pending_confirmation is None
    store.close()


def test_json_and_scripts_keep_explicit_tokens_and_one_object(tmp_path,monkeypatch):
    wf,store,r,session=setup(tmp_path)
    monkeypatch.setattr(cli.Settings,'load',lambda:SETTINGS)
    def workflow(settings,state):
        return AssistantWorkflow(SETTINGS,state,turn_factory=wf.turn_factory,gateway_factory=wf.gateway_factory,today=wf.today)
    monkeypatch.setattr(cli,'AssistantWorkflow',workflow)
    args=['chat','--user',OWNER,'--db',str(store.path),'--conversation',r.conversation_id,'--json']
    output=CliRunner().invoke(cli.app,args,input='Delete all the reports we made in this conversation\nconfirm\n/exit\n')
    assert output.exit_code==0,output.exception
    results=[json.loads(line) for line in output.stdout.splitlines()]
    assert len(results)==2 and results[0]['deletion_preview']['targets'][0]['id']==r.report_id
    token=results[0]['deletion_preview']['token']
    assert results[1]['status']=='confirmation_unavailable' and store.report(OWNER,r.report_id)
    explicit=CliRunner().invoke(cli.app,['ask','cancel '+token,'--user',OWNER,'--db',str(store.path),'--json'])
    assert explicit.exit_code==0 and json.loads(explicit.stdout)['status']=='ok'
    bare=CliRunner().invoke(cli.app,['ask','confirm','--user',OWNER,'--db',str(store.path),'--json'])
    assert json.loads(bare.stdout)['status']=='clarification' and store.report(OWNER,r.report_id)
    store.close()


def test_concurrent_version_change_serializes_before_confirmation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    wf,store,r,session=setup(tmp_path)
    other_report=wf.run(OWNER,'save',conversation_id=r.conversation_id)
    first=preview(session)
    ready=Event();start=Event();attempting=Event()
    def resolve():
        connection=SQLiteStore(store.path)
        ready.set()
        assert start.wait(5)
        attempting.set()
        try:return connection.resolve_deletion(OWNER,first.confirmation.token)
        finally:connection.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(resolve)
        assert ready.wait(5)
        store.db.execute('BEGIN IMMEDIATE')
        store.db.execute('UPDATE reports SET version=version+1 WHERE id=?',(r.report_id,))
        start.set()
        assert attempting.wait(5)
        store.db.execute('COMMIT')
        assert future.result(timeout=10)==('stale',[])
    assert store.report(OWNER,r.report_id) and store.report(OWNER,other_report.report_id)
    store.close()


def test_human_cli_eof_clears_successfully_displayed_reference(tmp_path,monkeypatch):
    wf,store,r,_=setup(tmp_path)
    created=[]
    class RecordingSession(ChatSession):
        def __init__(self,*a,**k):
            super().__init__(*a,**k);created.append(self)
    monkeypatch.setattr(cli,'ChatSession',RecordingSession)
    monkeypatch.setattr(cli.Settings,'load',lambda:SETTINGS)
    monkeypatch.setattr(cli,'AssistantWorkflow',lambda settings,state:AssistantWorkflow(
        SETTINGS,state,turn_factory=wf.turn_factory,gateway_factory=wf.gateway_factory,today=wf.today))
    output=CliRunner().invoke(cli.app,['chat','--user',OWNER,'--db',str(store.path),'--conversation',r.conversation_id],
        input='Delete all the reports we made in this conversation\n')
    assert output.exit_code==0,output.exception
    assert 'type confirm or cancel' in output.stdout
    assert created[0].pending_confirmation is None and store.report(OWNER,r.report_id)
    # EOF clears memory without altering the durable pending proposal.
    assert store.db.execute('SELECT state FROM deletion_proposals').fetchone()[0]=='pending'
    store.close()
