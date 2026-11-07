"""Navigation uses real saved artifacts and injected current catalog only."""
import json
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from opsfleet_assistant.cli_commands import parse_slash, ChatSession
from opsfleet_assistant.conversation_service import ConversationService
from opsfleet_assistant.identity import Catalog, resolve_demo_scope
from opsfleet_assistant.navigation import NavigationFailure
from opsfleet_assistant.report_service import ReportService
from opsfleet_assistant.report_artifact import artifact_binding_digest
from opsfleet_assistant.store import SQLiteStore
from opsfleet_assistant.presentation import render_navigation
from test_playbooks import run_workflow, spec, product_rows, regional_spec, regional_rows, churn_spec, churn_rows

OWNER='manager_apparel'


def auth():
    from test_playbooks import CATALOG
    return resolve_demo_scope(OWNER, CATALOG), CATALOG


def service(store):
    return ReportService(store, lambda owner: auth())


@pytest.mark.parametrize('text', ['/unknown','/help x','/report','/report 0','/report -1','/resume ""','/reports --limit 0','/reports --limit 51','/reports --limit 2 --limit 3','/reports --other x','/reports --cursor','/help "'])
def test_bad_parser(text):
    with pytest.raises(NavigationFailure):parse_slash(text)


@pytest.mark.parametrize('name',['help','schema','new','exit','quit','reports','conversations'])
def test_parser(name):
    assert parse_slash('/'+name).operation==name
    assert parse_slash('ordinary question') is None


@pytest.mark.parametrize('request_factory,rows',[(spec,product_rows),(regional_spec,regional_rows),(churn_spec,churn_rows)])
def test_restart_read_exact_and_no_context_injection(tmp_path,request_factory,rows):
    wf,store,result=run_workflow(tmp_path,request_factory(report=True),rows())
    fields=store.report(OWNER,result.report_id)
    store.close()
    store=SQLiteStore(wf.store.path)
    calls=[]
    def authorization(owner):calls.append(owner);return auth()
    srv=ReportService(store,authorization)
    page=srv.list(OWNER)
    assert len(page.items)==1
    read=srv.read(OWNER,result.report_id)
    assert read.status=='ok' and read.content['text']==fields['content']
    assert read.content['completeness']==fields['report_completeness']
    assert read.content['as_of']==tuple(e['query_as_of_utc'] for e in read.content['support']['snapshot']['evidence'])
    assert calls==[OWNER,OWNER]
    before=store.db.execute('SELECT continuity_json FROM conversations').fetchone()[0]
    srv.read(OWNER,result.report_id)
    assert store.db.execute('SELECT continuity_json FROM conversations').fetchone()[0]==before
    store.close()


def test_unavailable_ids_and_semantic_tamper(tmp_path):
    wf,store,result=run_workflow(tmp_path,spec(report=True),product_rows())
    srv=service(store)
    assert srv.read('manager_casual',result.report_id)==srv.read('manager_casual',99999)
    fields=store.report(OWNER,result.report_id)
    artifact=json.loads(fields['report_support_json'])
    artifact['snapshot']['grounded']['driver']['contributions'][0]['value']='123456'
    fields['report_support_json']=json.dumps(artifact)
    fields['report_binding_digest']=artifact_binding_digest(fields)
    store.db.execute('UPDATE reports SET report_support_json=?,report_binding_digest=? WHERE id=?',
        (fields['report_support_json'],fields['report_binding_digest'],result.report_id))
    assert srv.read(OWNER,result.report_id).status=='unavailable'
    assert srv.list(OWNER).items==()
    store.close()


def test_downgrade_checksum_revocation_and_catalog_error(tmp_path):
    wf,store,result=run_workflow(tmp_path,spec(report=True),product_rows())
    srv=service(store)
    store.db.execute('UPDATE reports SET report_binding_digest=?',('bad',))
    assert not srv.list(OWNER).items
    store.db.execute('UPDATE reports SET report_binding_digest=?,report_support_json=?',('',''))
    assert srv.read(OWNER,result.report_id).status=='unavailable'
    def fail(owner):raise RuntimeError('private catalog error')
    with pytest.raises(NavigationFailure,match='access_check_unavailable'):ReportService(store,fail).list(OWNER)
    store.close()


def test_bounded_sparse_pages_and_owner_scoped_opaque_cursors(tmp_path):
    wf,store,result=run_workflow(tmp_path,spec(report=True),product_rows())
    # Clone synthetic saved rows; corrupt ones must not disclose their titles.
    fields=store.report(OWNER,result.report_id)
    columns=tuple(k for k in fields if k!='id')
    for i in range(120):
        clone={**fields,'turn_id':str(i),'content':'SECRET HIDDEN TITLE','report_binding_digest':'bad'}
        store.db.execute(f"INSERT INTO reports({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",tuple(clone[k] for k in columns))
    calls=[];sizes=[]
    original=store.report_candidates
    def candidates(*a,**k):
        rows=original(*a,**k);sizes.append(len(rows));return rows
    store.report_candidates=candidates
    srv=ReportService(store,lambda o:(calls.append(o) or auth()))
    page=srv.list(OWNER,limit=5)
    assert not page.items and page.next_cursor and len(page.next_cursor)==32
    assert sizes==[6] and calls==[OWNER]
    for owner,limit,operation in [('manager_casual',5,'reports'),(OWNER,6,'reports'),(OWNER,5,'conversations')]:
        with pytest.raises(NavigationFailure,match='invalid_cursor'):
            if operation=='reports':srv.list(owner,limit=limit,cursor=page.next_cursor)
            else:ConversationService(store).list(owner,limit=limit,cursor=page.next_cursor)
    page2=srv.list(OWNER,limit=5,cursor=page.next_cursor)
    assert page2.next_cursor!=page.next_cursor and not page2.items
    assert 'SECRET' not in json.dumps(page.payload())
    store.close()


def test_conversation_lifecycle_no_warehouse_and_expired_metadata(tmp_path):
    store=SQLiteStore(tmp_path/'state.sqlite')
    cs=ConversationService(store)
    new=cs.new(OWNER);cid=new.active_conversation
    assert store.conversation_exists(OWNER,cid)
    assert not store.conversation_exists('manager_casual',cid)
    assert cs.resume('manager_casual',cid,active='prior').active_conversation=='prior'
    assert cs.resume(OWNER,'',active=cid).status=='unavailable'
    page=cs.list(OWNER,limit=1)
    assert page.items[0]['continuity']=='unavailable' and set(page.items[0])=={'conversation_id','updated_at','continuity'}
    cs.new(OWNER)
    assert len(cs.list(OWNER).items)==2
    store.close()


def test_expired_context_report_still_readable_and_resume_keeps_clock(tmp_path):
    wf,store,result=run_workflow(tmp_path,spec(report=True),product_rows())
    original=store._now
    store._now=lambda:original()+timedelta(hours=1)
    cs=ConversationService(store)
    assert cs.list(OWNER).items[0]['continuity']=='expired'
    assert cs.resume(OWNER,result.conversation_id).content['continuity']=='expired'
    assert service(store).read(OWNER,result.report_id).status=='ok'
    store.close()


def test_terminal_literal_and_json_errors_recover(tmp_path,monkeypatch):
    from opsfleet_assistant import cli
    from opsfleet_assistant.navigation import NavigationResult
    buf=StringIO();console=Console(file=buf,width=23)
    render_navigation(console,NavigationResult('reports',items=({'title':'[red]literal[/red]\x1b[2J\x00X'},)))
    assert '[red]literal[/red]' in buf.getvalue().replace('\n','')
    assert '\x1b' not in buf.getvalue() and '\x00' not in buf.getvalue()
    monkeypatch.setattr(cli.Settings,'load',lambda:SimpleNamespace())
    output=CliRunner().invoke(cli.app,['chat','--user',OWNER,'--db',str(tmp_path/'cli.sqlite'),'--json'],
        input='/unknown\n/help\n/conversations --limit 1\n/resume missing\n/new\n/exit\n')
    assert output.exit_code==0,output.exception
    values=[json.loads(line) for line in output.stdout.splitlines()]
    assert len(values)==5 and values[0]['status']=='invalid_command'
    assert values[2]['operation']=='conversations' and values[3]['status']=='unavailable'
    assert values[4]['active_conversation']!=values[3]['active_conversation']
    bad=CliRunner().invoke(cli.app,['chat','--user',OWNER,'--db',str(tmp_path/'cli.sqlite'),'--conversation','','--json'])
    assert bad.exit_code==1 and json.loads(bad.stdout)['status']=='unavailable'


def test_schema_migration_idempotent(tmp_path):
    p=tmp_path/'old.sqlite';s=SQLiteStore(p)
    s.db.execute('PRAGMA user_version=4');s.db.execute('DROP TABLE navigation_cursors');s.close()
    for _ in range(2):
        s=SQLiteStore(p);assert s.db.execute('PRAGMA user_version').fetchone()[0]==5;s.close()


def test_revoked_product_and_brand_scope_hidden_without_counts(tmp_path):
    wf,store,result=run_workflow(tmp_path,spec(report=True),product_rows())
    catalog=Catalog.from_products([{'id':1,'brand':'Calvin Klein'},{'id':2,'brand':'Hanes'},{'id':3,'brand':'Carhartt'}])
    srv=ReportService(store,lambda o:(resolve_demo_scope(o,catalog),catalog))
    page=srv.list(OWNER)
    assert page.items==() and page.next_cursor is None
    assert 'count' not in json.dumps(page.payload())
    assert srv.read(OWNER,result.report_id).status=='unavailable'
    # Brand grant itself revoked, with unchanged numerical policy version.
    scope,catalog=auth()
    srv=ReportService(store,lambda o:(scope.model_copy(update={'brands':('Calvin Klein',)}),catalog))
    assert not srv.list(OWNER).items
    store.close()


@pytest.mark.parametrize('kind',['regional','churn'])
def test_protected_unavailable_original_content_readable(kind,tmp_path):
    from test_part2_review_fixes import flow, suppressed_source
    client,request_spec=suppressed_source(kind)
    wf,store,_=flow(tmp_path,client,request_spec)
    result=wf.run(OWNER,'Synthetic protected report')
    read=service(store).read(OWNER,result.report_id)
    assert read.status=='ok' and read.content['completeness']=='partial'
    assert read.content['text']==result.text and 'No safely releasable observations' in read.content['text']
    assert 'No matching data' not in read.content['text']
    store.close()


def test_ordinary_durable_and_genuine_legacy_reader_and_no_row_factory_mutation(tmp_path):
    from test_step4 import FakeGateway,FakeTurn,SETTINGS
    from opsfleet_assistant.workflow import AssistantWorkflow
    store=SQLiteStore(tmp_path/'ordinary.sqlite')
    wf=AssistantWorkflow(SETTINGS,store,gateway_factory=FakeGateway,turn_factory=FakeTurn)
    first=wf.run(OWNER,'Create report Q1')
    assert first.report_id
    read=service(store).read(OWNER,first.report_id)
    assert read.status=='ok' and read.content['text']==first.text
    assert read.content['support']['schema_version']==1
    store.db.execute('UPDATE reports SET report_binding_digest=?,report_support_json=?,report_composition_json=?',('','',''))
    read=service(store).read(OWNER,first.report_id)
    assert read.status=='ok' and read.content['text']==first.text
    assert read.content['support'] is None and read.content['completeness']=='legacy'
    assert store.db.row_factory is None
    store.report(OWNER,first.report_id)
    assert store.db.row_factory is None
    store.close()


def test_keyset_insertion_deletion_and_cursor_expiry(tmp_path):
    store=SQLiteStore(tmp_path/'pages.sqlite')
    cs=ConversationService(store)
    for cid in ('a','c','e'):store.create_conversation(OWNER,cid)
    page=cs.list(OWNER,limit=1)
    assert page.items[0]['conversation_id']=='a'
    store.create_conversation(OWNER,'0');store.create_conversation(OWNER,'b')
    store.db.execute('DELETE FROM conversations WHERE owner=? AND conversation_id=?',(OWNER,'c'))
    next_page=cs.list(OWNER,limit=1,cursor=page.next_cursor)
    assert next_page.items[0]['conversation_id']=='b'
    assert cs.list(OWNER,limit=1,cursor=next_page.next_cursor).items[0]['conversation_id']=='e'
    now=store._now();store._now=lambda:now+timedelta(days=2)
    with pytest.raises(NavigationFailure,match='invalid_cursor'):cs.list(OWNER,limit=1,cursor=page.next_cursor)
    store.close()


def test_store_access_errors_and_cli_flags(tmp_path,monkeypatch):
    from opsfleet_assistant import cli
    from opsfleet_assistant.store import StoreFailure
    wf,store,r=run_workflow(tmp_path,spec(report=True),product_rows())
    session=ChatSession(wf,OWNER,r.conversation_id)
    active=session.active
    store.close()
    for command in ('/reports','/report 1','/conversations','/resume missing','/new'):
        result=session.handle(command)
        assert result.status=='store_unavailable' and session.active==active
    runner=CliRunner()
    for op in ('reports','report','conversations','chat','ask'):
        def fail(*a,**k):raise StoreFailure('store_unavailable')
        monkeypatch.setattr(cli,'SQLiteStore',fail)
        args=[op]
        if op=='report':args+=['1']
        elif op=='ask':args+=['Fixture']
        output=runner.invoke(cli.app,[*args,'--user',OWNER,'--db',str(tmp_path/'bad'),'--json'])
        assert output.exit_code==1 and json.loads(output.stdout)['status']=='store_unavailable'
    for args in (['reports','--limit','0'],['conversations','--limit','51'],['report','0']):
        assert runner.invoke(cli.app,[*args,'--user',OWNER]).exit_code==2


def test_valid_resume_original_anchors_correct_analysis_and_failed_startup(tmp_path,monkeypatch):
    from opsfleet_assistant import cli
    wf,store,r=run_workflow(tmp_path,spec(report=True),product_rows())
    before=store.conversation_payload(OWNER,r.conversation_id)
    session=ChatSession(wf,OWNER)
    previous=session.active
    assert session.handle('/resume missing').active_conversation==previous
    selected=session.handle('/resume '+r.conversation_id)
    assert selected.active_conversation==r.conversation_id
    assert store.conversation_payload(OWNER,r.conversation_id)==before
    session.handle('/report '+str(r.report_id))
    saved=session.handle('save')
    assert saved.driver_finding==r.driver_finding and saved.factual_findings==r.factual_findings
    after=store.load_continuity(OWNER,r.conversation_id)
    original=json.loads(before)['snapshot']
    assert after.snapshot.resolution_reference_date.isoformat()==original['resolution_reference_date']
    monkeypatch.setattr(cli.Settings,'load',lambda:SimpleNamespace())
    for cid in ('arbitrary',r.conversation_id,''):
        output=CliRunner().invoke(cli.app,['chat','--user','manager_casual','--db',str(store.path),'--conversation',cid,'--json'])
        assert output.exit_code==1 and json.loads(output.stdout)['status']=='unavailable'
        assert not store.conversation_exists('manager_casual',cid)
    store.close()


@pytest.mark.parametrize('cursor',['','!','a'*31,'a'*32,'a'*33])
def test_bad_cursors_fail_locally_before_catalog(tmp_path,cursor):
    store=SQLiteStore(tmp_path/'cursor.sqlite')
    def forbidden(owner):pytest.fail('catalog invoked for invalid cursor')
    with pytest.raises(NavigationFailure,match='invalid_cursor'):
        ReportService(store,forbidden).list(OWNER,cursor=cursor)
    store.close()


def test_incompatible_continuity_and_deleted_reports_are_unavailable(tmp_path):
    wf,store,r=run_workflow(tmp_path,spec(report=True),product_rows())
    store.db.execute('UPDATE conversations SET continuity_json=?',('{"schema_version":999}',))
    cs=ConversationService(store)
    assert cs.list(OWNER).items[0]['continuity']=='incompatible'
    assert cs.resume(OWNER,r.conversation_id).content['continuity']=='incompatible'
    store.db.execute('UPDATE reports SET deleted_at=?',('2026-11-07',))
    srv=service(store)
    assert srv.read(OWNER,r.report_id)==srv.read(OWNER,99999)
    assert srv.list(OWNER).items==()
    store.close()


def test_corrupt_legacy_shapes_do_not_crash_chat(tmp_path):
    from test_step4 import FakeGateway,FakeTurn,SETTINGS
    from opsfleet_assistant.workflow import AssistantWorkflow
    store=SQLiteStore(tmp_path/'corrupt.sqlite')
    wf=AssistantWorkflow(SETTINGS,store,gateway_factory=FakeGateway,turn_factory=FakeTurn)
    r=wf.run(OWNER,'Create report Q1')
    store.db.execute('UPDATE reports SET report_binding_digest=?,report_support_json=?,grounded_report_json=?,metric_definitions_json=?',('','','','[null]'))
    session=ChatSession(wf,OWNER,r.conversation_id)
    assert session.handle('/report '+str(r.report_id)).status=='unavailable'
    preview=session.handle('Delete all the reports we made in this conversation')
    assert preview.status=='error' and 'Complete report' not in preview.text
    assert session.handle('/help').status=='ok'
    store.close()
