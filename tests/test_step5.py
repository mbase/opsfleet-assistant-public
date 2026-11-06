"""Step 5 owner-bound soft deletion; all data and adapters are local fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3

import pytest

from opsfleet_assistant.config import ROOT, Settings
from opsfleet_assistant.deletion import parse_deletion
from opsfleet_assistant.store import SQLiteStore, StoreFailure
from opsfleet_assistant.workflow import AssistantWorkflow


SETTINGS = Settings(project="test-project", location="US", model="unused", gemini_api_key="test")


def saved(store: SQLiteStore, owner: str, conversation: str, turn: str, content: str) -> int:
    return store.save_report(owner=owner, conversation_id=conversation, turn_id=turn,
                             content=content, metric_definitions=[], scope_reference=f"demo:{owner}",
                             scope_version=1, evidence_refs=[], request_digest="request",
                             identity_digest="identity")


def test_literal_matching_is_casefolded_substring_not_sql_wildcard(tmp_path: Path):
    assert parse_deletion("Please delete all reports mentioning Client X").value == "Client X"
    assert parse_deletion("Delete all reports mentioning Client X or Client Y").action == "ambiguous"
    assert parse_deletion("Could you delete some reports?").action == "ambiguous"
    store = SQLiteStore(tmp_path / "reports.sqlite")
    first = saved(store, "manager_apparel", "c1", "t1", "Client X account\nChecked details")
    second = saved(store, "manager_apparel", "c2", "t2", "Monthly report\nCLIENT x appears in body")
    wildcard = saved(store, "manager_apparel", "c1", "t3", "Revenue increased 25 percent")
    saved(store, "manager_casual", "c1", "t4", "Client X private report")
    preview = store.preview_deletion("manager_apparel", "literal", "client x")
    assert [t["id"] for t in preview["targets"]] == [first, second]
    assert store.preview_deletion("manager_apparel", "literal", "%")["targets"] == []
    assert store.preview_deletion("manager_apparel", "literal", "_")["targets"] == []
    assert store.report("manager_apparel", wildcard)
    assert store.db.execute("SELECT count(*) FROM deletion_proposals").fetchone()[0] == 1
    proposal_row = store.db.execute("SELECT * FROM deletion_proposals").fetchone()
    assert not any(preview["token"] in str(value) for value in proposal_row)
    assert datetime.fromisoformat(preview["expires_at"]) - datetime.fromisoformat(proposal_row[7]) == timedelta(minutes=5)
    assert len(preview["targets"][0]["title"]) <= 80
    store.close()


def test_conversation_owner_and_ceo_do_not_cross_boundaries(tmp_path: Path):
    store = SQLiteStore(tmp_path / "reports.sqlite")
    a = saved(store, "manager_apparel", "c1", "a", "A")
    saved(store, "manager_apparel", "c2", "b", "B")
    saved(store, "manager_casual", "c1", "c", "C")
    saved(store, "ceo", "c1", "d", "D")
    proposal = store.preview_deletion("manager_apparel", "conversation", "c1")
    assert [t["id"] for t in proposal["targets"]] == [a]
    assert store.resolve_deletion("ceo", proposal["token"]) == ("invalid", [])
    assert store.resolve_deletion("manager_casual", proposal["token"]) == ("invalid", [])
    assert store.resolve_deletion("manager_apparel", proposal["token"]) == ("deleted", [a])
    assert len(store.list_reports("manager_apparel")) == 1
    assert len(store.list_reports("manager_casual")) == 1
    assert len(store.list_reports("ceo")) == 1
    store.close()


def test_cancel_expiry_replay_and_restart(tmp_path: Path, monkeypatch):
    path = tmp_path / "reports.sqlite"
    store = SQLiteStore(path)
    report_id = saved(store, "manager_apparel", "c", "t", "Client X")
    cancelled = store.preview_deletion("manager_apparel", "literal", "Client X")
    assert store.resolve_deletion("manager_apparel", cancelled["token"], cancel=True) == ("cancelled", [])
    assert store.resolve_deletion("manager_apparel", cancelled["token"]) == ("invalid", [])
    expiring = store.preview_deletion("manager_apparel", "literal", "Client X")
    future = datetime.now(timezone.utc) + timedelta(minutes=6)
    monkeypatch.setattr(store, "_now", lambda: future)
    assert store.resolve_deletion("manager_apparel", expiring["token"]) == ("expired", [])
    assert store.report("manager_apparel", report_id)
    monkeypatch.undo()
    valid = store.preview_deletion("manager_apparel", "literal", "Client X")
    store.close()
    reopened = SQLiteStore(path)
    assert reopened.resolve_deletion("manager_apparel", valid["token"]) == ("deleted", [report_id])
    assert reopened.resolve_deletion("manager_apparel", valid["token"]) == ("invalid", [])
    assert reopened.report("manager_apparel", report_id) is None
    assert reopened.list_reports("manager_apparel") == []
    assert reopened.preview_deletion("manager_apparel", "literal", "Client X")["targets"] == []
    reopened.close()


def test_new_match_is_not_added_and_changed_version_aborts_all(tmp_path: Path):
    store = SQLiteStore(tmp_path / "reports.sqlite")
    first = saved(store, "manager_apparel", "c", "a", "Client X one")
    second = saved(store, "manager_apparel", "c", "b", "Client X two")
    preview = store.preview_deletion("manager_apparel", "literal", "Client X")
    new = saved(store, "manager_apparel", "c", "new", "Client X new")
    store.db.execute("UPDATE reports SET version=version+1 WHERE id=?", (second,))
    assert store.resolve_deletion("manager_apparel", preview["token"]) == ("stale", [])
    assert all(store.report("manager_apparel", i) for i in (first, second, new))
    fresh = store.preview_deletion("manager_apparel", "literal", "Client X")
    assert store.resolve_deletion("manager_apparel", fresh["token"]) == ("deleted", [first, second, new])
    store.close()


def test_disappeared_or_reowned_target_aborts_without_partial_delete(tmp_path: Path):
    store = SQLiteStore(tmp_path / "reports.sqlite")
    first = saved(store, "manager_apparel", "c", "a", "Client X one")
    second = saved(store, "manager_apparel", "c", "b", "Client X two")
    preview = store.preview_deletion("manager_apparel", "literal", "Client X")
    store.db.execute("UPDATE reports SET owner='ceo' WHERE id=?", (second,))
    assert store.resolve_deletion("manager_apparel", preview["token"]) == ("stale", [])
    assert store.report("manager_apparel", first)
    assert store.report("ceo", second)
    store.db.execute("UPDATE reports SET owner='manager_apparel' WHERE id=?", (second,))
    second_preview = store.preview_deletion("manager_apparel", "literal", "Client X")
    store.db.execute("DELETE FROM reports WHERE id=?", (second,))
    assert store.resolve_deletion("manager_apparel", second_preview["token"]) == ("stale", [])
    assert store.report("manager_apparel", first)
    store.close()


def test_conversation_move_after_preview_is_stale(tmp_path: Path):
    store = SQLiteStore(tmp_path / "reports.sqlite")
    report_id = saved(store, "manager_apparel", "original", "t", "Report")
    proposal = store.preview_deletion("manager_apparel", "conversation", "original")
    store.db.execute("UPDATE reports SET conversation_id='other' WHERE id=?", (report_id,))
    assert store.resolve_deletion("manager_apparel", proposal["token"]) == ("stale", [])
    assert store.report("manager_apparel", report_id)
    store.close()


def test_store_failure_rolls_back_every_report_and_proposal(tmp_path: Path):
    store = SQLiteStore(tmp_path / "reports.sqlite")
    first = saved(store, "manager_apparel", "c", "a", "Client X one")
    second = saved(store, "manager_apparel", "c", "b", "Client X two")
    proposal = store.preview_deletion("manager_apparel", "literal", "Client X")
    store.db.execute(f"CREATE TRIGGER fail_second BEFORE UPDATE OF deleted_at ON reports "
                     f"WHEN OLD.id={second} BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(StoreFailure, match="store_unavailable"):
        store.resolve_deletion("manager_apparel", proposal["token"])
    assert store.report("manager_apparel", first)
    assert store.report("manager_apparel", second)
    assert store.db.execute("SELECT state FROM deletion_proposals").fetchone()[0] == "pending"
    store.close()


def test_deleted_retry_is_tombstoned_and_cannot_expose_or_resurrect(tmp_path: Path):
    store = SQLiteStore(tmp_path / "reports.sqlite")
    report_id = saved(store, "manager_apparel", "c", "t", "Client X secret body")
    preview = store.preview_deletion("manager_apparel", "literal", "Client X")
    store.resolve_deletion("manager_apparel", preview["token"])
    retry = store.find_retry("manager_apparel", "c", "t",
                             request_digest="request", identity_digest="identity")
    assert retry == {"deleted": True}
    with pytest.raises(StoreFailure, match="report_deleted"):
        saved(store, "manager_apparel", "c", "t", "Client X secret body")
    assert store.report("manager_apparel", report_id) is None
    assert store.db.execute("SELECT count(*) FROM reports").fetchone()[0] == 1
    store.close()


def test_command_route_uses_no_model_or_bigquery_and_requires_explicit_token(tmp_path: Path):
    store = SQLiteStore(tmp_path / "reports.sqlite")
    report_id = saved(store, "manager_apparel", "c", "t", "Client X")
    def forbidden(*args, **kwargs):
        raise AssertionError("model or BigQuery instantiated during deletion")
    workflow = AssistantWorkflow(SETTINGS, store, gateway_factory=forbidden, turn_factory=forbidden)
    missing = workflow.run("manager_apparel", "Delete all the reports we made in this conversation")
    assert missing.status == "clarification"
    preview = workflow.run("manager_apparel", "Delete all reports mentioning Client X", conversation_id="c")
    assert preview.status == "confirmation_required" and f"ID {report_id}" in preview.text
    assert store.report("manager_apparel", report_id)
    assert parse_deletion("yes") is None
    token = preview.text.split("confirm ", 1)[1].splitlines()[0]
    done = workflow.run("manager_apparel", "confirm " + token, conversation_id="c")
    assert done.status == "ok" and done.model_requests == 0 and done.query_executions == 0
    assert store.report("manager_apparel", report_id) is None
    assert workflow.run("manager_apparel", "confirm " + token).status == "error"
    store.close()


def test_workflow_deleted_retry_never_starts_model_or_warehouse(tmp_path: Path):
    store = SQLiteStore(tmp_path / "reports.sqlite")
    question = "Create a Q1 2025 revenue report with Q2 actions"
    turn_id = "fixed-turn"
    request_digest = hashlib.sha256((turn_id + "\0" + question).encode()).hexdigest()
    identity_digest = hashlib.sha256((ROOT / "config" / "demo_users.json").read_bytes()).hexdigest()
    report_id = store.save_report(owner="manager_apparel", conversation_id="c", turn_id=turn_id,
                                  content="Synthetic report", metric_definitions=[], scope_reference="demo:manager_apparel",
                                  scope_version=1, evidence_refs=[], request_digest=request_digest,
                                  identity_digest=identity_digest)
    proposal = store.preview_deletion("manager_apparel", "conversation", "c")
    assert store.resolve_deletion("manager_apparel", proposal["token"]) == ("deleted", [report_id])
    class NoCallsGateway:
        def __init__(self, *args, **kwargs):
            pass
        def plan(self, *args, **kwargs):
            raise AssertionError("model called")
        def report(self, *args, **kwargs):
            raise AssertionError("model called")
    def forbidden_bq(*args, **kwargs):
        raise AssertionError("BigQuery instantiated")
    result = AssistantWorkflow(SETTINGS, store, gateway_factory=NoCallsGateway, turn_factory=forbidden_bq).run(
        "manager_apparel", question, conversation_id="c", turn_id=turn_id)
    assert result.status == "deleted" and result.model_requests == 0 and result.query_executions == 0
    assert "Synthetic report" not in result.text
    assert store.db.execute("SELECT count(*) FROM reports").fetchone()[0] == 1
    store.close()


def test_legacy_reports_table_migrates_without_losing_content(tmp_path: Path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE reports(id INTEGER PRIMARY KEY,owner TEXT,conversation_id TEXT,turn_id TEXT,"
               "version INTEGER,request_digest TEXT,identity_digest TEXT,plans_json TEXT,scope_digest TEXT,"
               "content TEXT,metric_definitions_json TEXT,scope_reference TEXT,scope_version INTEGER,"
               "evidence_refs_json TEXT,created_at TEXT)")
    db.execute("INSERT INTO reports VALUES(1,'manager_apparel','c','t',1,'','','[]','','Old report','[]',"
               "'demo:manager_apparel',1,'[]','2026-01-01')")
    db.commit()
    db.close()
    store = SQLiteStore(path)
    assert store.report("manager_apparel", 1)["content"] == "Old report"
    assert store.report("manager_apparel", 1)["grounded_report_json"] == ""
    assert store.db.execute("PRAGMA table_info(reports)").fetchall()[-1][1] == "deleted_at"
    store.close()
