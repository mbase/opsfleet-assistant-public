"""Part 3 N1/I1: pre-dispatch JSON failures and saved text/support integrity."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opsfleet_assistant import cli
from opsfleet_assistant.identity import resolve_demo_scope
from opsfleet_assistant.report_service import ReportService
from opsfleet_assistant.report_artifact import artifact_binding_digest
from test_playbooks import (run_workflow, spec, product_rows, regional_spec, regional_rows,
                           churn_spec, churn_rows, CATALOG)

OWNER = 'manager_apparel'


@pytest.mark.parametrize('arguments', [
    ['reports', '--limit', '0'], ['reports', '--limit', '51'],
    ['reports', '--limit', 'PRIVATE_VALUE'], ['reports', '--cursor'],
    ['report', 'not-an-id'], ['report', '0'], ['report'], ['report', '1', 'extra'],
    ['conversations', '--unknown'], ['conversations', '--limit', '-1'],
    ['reports', '--user', 'PRIVATE_VALUE'], ['unknown-command'],
    ['ask'], ['chat', '--unknown'],
])
@pytest.mark.parametrize('json_position', ['early', 'late'])
def test_json_usage_errors_are_one_safe_object_without_dependencies(monkeypatch, arguments, json_position):
    def forbidden(*args, **kwargs):pytest.fail('dependency reached for malformed command')
    monkeypatch.setattr(cli, 'SQLiteStore', forbidden)
    monkeypatch.setattr(cli.Settings, 'load', forbidden)
    args = list(arguments)
    if '--user' not in args and args[0] != 'unknown-command':
        args += ['--user', OWNER]
    args.insert(1 if json_position == 'early' else len(args), '--json')
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload['status'] == 'invalid_command' and payload['content'] is None
    assert payload['items'] == []
    assert payload['model_requests'] == payload['query_executions'] == payload['business_queries'] == 0
    assert 'PRIVATE_VALUE' not in result.stdout + result.stderr


def test_missing_identity_json_and_plain_usage_errors_stay_local(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):pytest.fail('store opened')
    monkeypatch.setattr(cli, 'SQLiteStore', forbidden)
    result = CliRunner().invoke(cli.app, ['reports', '--json'])
    assert result.exit_code == 2 and json.loads(result.stdout)['status'] == 'invalid_command'
    result = CliRunner().invoke(cli.app, ['report', 'not-an-id', '--user', OWNER])
    assert result.exit_code == 2 and result.stdout == '' and 'Usage' in result.stderr


@pytest.mark.parametrize('request_factory,rows', [
    (spec, product_rows), (regional_spec, regional_rows), (churn_spec, churn_rows)])
@pytest.mark.parametrize('mutation', ['title', 'body', 'append'])
@pytest.mark.parametrize('exact_save', [False, True])
@pytest.mark.parametrize('pre_contract', [False, True])
def test_text_only_tampering_recomputed_checksum_withholds_all_surfaces(
        tmp_path, request_factory, rows, mutation, exact_save, pre_contract):
    wf, store, original = run_workflow(tmp_path, request_factory(report=True), rows())
    question = 'offline investigation'
    if exact_save:
        original = wf.run(OWNER, 'save', conversation_id=original.conversation_id)
        question = 'save'
    rid = original.report_id
    fields = store.report(OWNER, rid)
    if pre_contract:
        support = json.loads(fields['report_support_json'])
        support.pop('rendering')
        fields['report_support_json'] = json.dumps(support)
        fields['report_binding_digest'] = artifact_binding_digest(fields)
        store.db.execute('UPDATE reports SET report_support_json=?,report_binding_digest=? WHERE id=?',
            (fields['report_support_json'], fields['report_binding_digest'], rid))
    support_before = fields['report_support_json']
    if mutation == 'title':
        first, _, rest = fields['content'].partition('\n')
        fields['content'] = first.split(':', 1)[0] + ': UNSUPPORTED_CONFIDENTIAL_TITLE\n' + rest
    elif mutation == 'body':
        title = fields['content'].splitlines()[0]
        fields['content'] = title + '\nRevenue is 123456789; churn is zero.'
    else:
        fields['content'] += '\nRevenue is 123456789; churn is zero.'
    fields['report_binding_digest'] = artifact_binding_digest(fields)
    store.db.execute('UPDATE reports SET content=?,report_binding_digest=? WHERE id=?',
        (fields['content'], fields['report_binding_digest'], rid))
    service = ReportService(store, lambda owner: (resolve_demo_scope(owner, CATALOG), CATALOG))
    assert service.read(OWNER, rid).status == 'unavailable'
    assert rid not in [item['id'] for item in service.list(OWNER).items]
    before = store.db.execute('SELECT count(*) FROM deletion_proposals').fetchone()[0]
    preview = wf.run(OWNER, 'Delete all the reports we made in this conversation',
                     conversation_id=original.conversation_id)
    assert preview.status == 'error' and getattr(preview, 'confirmation', None) is None
    assert 'UNSUPPORTED_CONFIDENTIAL' not in preview.text and '123456789' not in preview.text
    assert store.db.execute('SELECT count(*) FROM deletion_proposals').fetchone()[0] == before
    replay = wf.run(OWNER, question, conversation_id=original.conversation_id, turn_id=original.turn_id)
    assert replay.status == 'error' and not replay.reused_report and replay.report_id is None
    assert replay.driver_finding is None and '123456789' not in replay.text
    assert store.report(OWNER, rid)['report_support_json'] == support_before
    events = (tmp_path / 'workflow-events.jsonl').read_text()
    assert 'UNSUPPORTED_CONFIDENTIAL' not in events and '123456789' not in events
    store.close()


@pytest.mark.parametrize('request_factory,rows', [
    (spec, product_rows), (regional_spec, regional_rows), (churn_spec, churn_rows)])
@pytest.mark.parametrize('exact_save', [False, True])
def test_pre_contract_reader_preserves_original_text_as_of_and_no_rewrite(tmp_path, request_factory, rows, exact_save):
    wf, store, result = run_workflow(tmp_path, request_factory(report=True), rows())
    if exact_save:result = wf.run(OWNER, 'save', conversation_id=result.conversation_id)
    fields = store.report(OWNER, result.report_id)
    support = json.loads(fields['report_support_json'])
    assert support.pop('rendering')['version'] == 1
    fields['report_support_json'] = json.dumps(support)
    fields['report_binding_digest'] = artifact_binding_digest(fields)
    store.db.execute('UPDATE reports SET report_support_json=?,report_binding_digest=? WHERE id=?',
        (fields['report_support_json'], fields['report_binding_digest'], result.report_id))
    service = ReportService(store, lambda owner: (resolve_demo_scope(owner, CATALOG), CATALOG))
    read = service.read(OWNER, result.report_id)
    assert read.status == 'ok' and read.content['text'] == result.text
    assert read.content['as_of'] == tuple(e['query_as_of_utc'] for e in support['snapshot']['evidence'])
    assert store.report(OWNER, result.report_id) == fields
    store.close()


@pytest.mark.parametrize('pre_contract', [False, True])
def test_ordinary_report_text_is_checked_without_removing_genuine_legacy_behavior(tmp_path, pre_contract):
    from opsfleet_assistant.workflow import AssistantWorkflow
    from opsfleet_assistant.store import SQLiteStore
    from test_step4 import FakeGateway, FakeTurn, SETTINGS
    store = SQLiteStore(tmp_path/'ordinary.sqlite')
    wf = AssistantWorkflow(SETTINGS, store, gateway_factory=FakeGateway, turn_factory=FakeTurn)
    r = wf.run(OWNER, 'Q1 report')
    fields = store.report(OWNER, r.report_id)
    if pre_contract:
        support = json.loads(fields['report_support_json']);support.pop('rendering')
        fields['report_support_json'] = json.dumps(support)
    fields['content'] += '\nUnsupported revenue: 123456789.'
    fields['report_binding_digest'] = artifact_binding_digest(fields)
    store.db.execute('UPDATE reports SET content=?,report_support_json=?,report_binding_digest=? WHERE id=?',
        (fields['content'], fields['report_support_json'], fields['report_binding_digest'], r.report_id))
    service = ReportService(store, lambda owner: (resolve_demo_scope(owner, CATALOG), CATALOG))
    assert service.read(OWNER, r.report_id).status == 'unavailable'
    store.close()


@pytest.mark.parametrize('kind', ['regional','churn'])
@pytest.mark.parametrize('pre_contract', [False, True])
def test_protected_partial_text_and_pre_contract_compatibility(kind, pre_contract, tmp_path):
    from test_part2_review_fixes import flow, suppressed_source
    client, request_spec = suppressed_source(kind)
    wf, store, _ = flow(tmp_path, client, request_spec)
    result = wf.run(OWNER, 'protected report')
    assert result.status == 'partial' and 'No safely releasable observations' in result.text
    fields = store.report(OWNER, result.report_id)
    if pre_contract:
        support = json.loads(fields['report_support_json']);support.pop('rendering')
        fields['report_support_json'] = json.dumps(support)
        fields['report_binding_digest'] = artifact_binding_digest(fields)
        store.db.execute('UPDATE reports SET report_support_json=?,report_binding_digest=? WHERE id=?',
            (fields['report_support_json'], fields['report_binding_digest'], result.report_id))
    service = ReportService(store, lambda owner: (resolve_demo_scope(owner, CATALOG), CATALOG))
    assert service.read(OWNER, result.report_id).content['text'] == result.text
    fields['content'] = fields['content'].splitlines()[0] + '\nNo matching source data; revenue is zero.'
    fields['report_binding_digest'] = artifact_binding_digest(fields)
    store.db.execute('UPDATE reports SET content=?,report_binding_digest=? WHERE id=?',
        (fields['content'], fields['report_binding_digest'], result.report_id))
    assert service.read(OWNER, result.report_id).status == 'unavailable'
    store.close()


def test_unknown_renderer_fails_closed_without_rewriting_or_downgrading(tmp_path):
    wf, store, result = run_workflow(tmp_path, spec(report=True), product_rows())
    fields = store.report(OWNER, result.report_id)
    support = json.loads(fields['report_support_json']);support['rendering']['version'] = 999
    fields['report_support_json'] = json.dumps(support)
    fields['report_binding_digest'] = artifact_binding_digest(fields)
    store.db.execute('UPDATE reports SET report_support_json=?,report_binding_digest=? WHERE id=?',
        (fields['report_support_json'], fields['report_binding_digest'], result.report_id))
    service = ReportService(store, lambda owner: (resolve_demo_scope(owner, CATALOG), CATALOG))
    assert service.read(OWNER, result.report_id).status == 'unavailable'
    assert store.report(OWNER, result.report_id)['content'] == result.text
    store.close()


@pytest.mark.parametrize('pre_contract', [False, True])
def test_golden_provenance_uses_trusted_method_text_and_historical_reader(tmp_path, pre_contract):
    from opsfleet_assistant.workflow import AssistantWorkflow
    from opsfleet_assistant.store import SQLiteStore
    from opsfleet_assistant.contracts import ReportSpec
    from test_step4 import FakeGateway, SETTINGS
    from test_step6 import TrendTurn
    class Gateway(FakeGateway):
        def interpret(self, *a, **k):
            result = super().interpret(*a, **k)
            return result.model_copy(update={'request':result.request.model_copy(update={'report':ReportSpec()})})
    store = SQLiteStore(tmp_path/'golden.sqlite')
    wf = AssistantWorkflow(SETTINGS, store, gateway_factory=Gateway, turn_factory=TrendTurn)
    result = wf.run(OWNER, 'Show the sales trajectory across 2025')
    assert result.report_id and result.golden_status == 'selected', result.text
    fields = store.report(OWNER, result.report_id)
    if pre_contract:
        support = json.loads(fields['report_support_json']);support.pop('rendering')
        fields['report_support_json'] = json.dumps(support)
        fields['report_binding_digest'] = artifact_binding_digest(fields)
        store.db.execute('UPDATE reports SET report_support_json=?,report_binding_digest=? WHERE id=?',
            (fields['report_support_json'], fields['report_binding_digest'], result.report_id))
    service = ReportService(store, lambda owner: (resolve_demo_scope(owner, CATALOG), CATALOG))
    assert service.read(OWNER, result.report_id).content['text'] == result.text
    fields['content'] = fields['content'].replace('Use the supported monthly', 'Revenue is 123456789. Use the supported monthly')
    fields['report_binding_digest'] = artifact_binding_digest(fields)
    store.db.execute('UPDATE reports SET content=?,report_binding_digest=? WHERE id=?',
        (fields['content'], fields['report_binding_digest'], result.report_id))
    assert service.read(OWNER, result.report_id).status == 'unavailable'
    store.close()
