"""Small owner-bound SQLite state and report store. Only bounded screened messages and released evidence are retained in versioned continuity."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import ROOT
from .contracts import AnalysisPlan, CohortOrigin
from .conversation import ConversationContext
from .report_artifact import ReportArtifact, artifact_binding_digest, validate_artifact
from .reporting import GroundedReport, ReportComposition
from .identity import PolicyError
from pydantic import ValidationError


class StoreFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SQLiteStore:
    def __init__(self, path: Path = ROOT / "runtime" / "opsfleet.sqlite", *, clock=None):
        self.path = path
        if clock is not None:
            self._now = clock
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
            if path != Path(":memory:"):
                os.chmod(path, 0o600)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                  owner TEXT NOT NULL, conversation_id TEXT NOT NULL,
                  plan_json TEXT, cohort_origin TEXT, scope_version INTEGER,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY(owner, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS reports (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  owner TEXT NOT NULL, conversation_id TEXT NOT NULL,
                  turn_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                  request_digest TEXT NOT NULL DEFAULT '', identity_digest TEXT NOT NULL DEFAULT '',
                  plans_json TEXT NOT NULL DEFAULT '[]', scope_digest TEXT NOT NULL DEFAULT '',
                  content TEXT NOT NULL, metric_definitions_json TEXT NOT NULL,
                  scope_reference TEXT NOT NULL, scope_version INTEGER NOT NULL,
                  evidence_refs_json TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  deleted_at TEXT,
                  UNIQUE(owner, conversation_id, turn_id)
                );
                CREATE TABLE IF NOT EXISTS deletion_proposals (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  owner TEXT NOT NULL, selector TEXT NOT NULL, selector_value TEXT NOT NULL,
                  conversation_id TEXT, targets_json TEXT NOT NULL,
                  token_digest TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('pending','consumed','cancelled','expired','stale'))
                );
            """)
            existing_columns = {row[1] for row in self.db.execute("PRAGMA table_info(reports)")}
            for column in ("request_digest", "identity_digest", "plans_json", "scope_digest", "grounded_report_json", "report_completeness", "report_composition_json", "report_support_json", "report_binding_digest"):
                if column not in existing_columns:
                    default = "'[]'" if column == "plans_json" else "'legacy'" if column == "report_completeness" else "''"
                    self.db.execute(f"ALTER TABLE reports ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}")
            if "deleted_at" not in existing_columns:
                self.db.execute("ALTER TABLE reports ADD COLUMN deleted_at TEXT")
            conversation_columns = {row[1] for row in self.db.execute("PRAGMA table_info(conversations)")}
            if "continuity_json" not in conversation_columns:
                self.db.execute("ALTER TABLE conversations ADD COLUMN continuity_json TEXT NOT NULL DEFAULT ''")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 4:
                self.db.execute("PRAGMA user_version=4")
            self.db.execute("CREATE INDEX IF NOT EXISTS reports_active_owner ON reports(owner,deleted_at,conversation_id)")
        except (OSError, sqlite3.Error) as exc:
            raise StoreFailure("store_unavailable") from None

    def close(self) -> None:
        self.db.close()

    def load_context(self, owner: str, conversation_id: str) -> tuple[AnalysisPlan | None, CohortOrigin | None, int | None]:
        try:
            row = self.db.execute(
                "SELECT plan_json, cohort_origin, scope_version FROM conversations WHERE owner=? AND conversation_id=?",
                (owner, conversation_id),
            ).fetchone()
            if row is None:
                return None, None, None
            return (AnalysisPlan.model_validate_json(row[0]) if row[0] else None,
                    CohortOrigin(row[1]) if row[1] else None, row[2])
        except (sqlite3.Error, ValueError) as exc:
            raise StoreFailure("store_unavailable") from None

    def clear_continuity(self, owner: str, conversation_id: str) -> None:
        try:
            self.db.execute("UPDATE conversations SET continuity_json='',plan_json=NULL,scope_version=NULL "
                            "WHERE owner=? AND conversation_id=?", (owner, conversation_id))
        except sqlite3.Error:
            raise StoreFailure("store_unavailable") from None

    def load_continuity(self, owner: str, conversation_id: str) -> ConversationContext | None:
        try:
            row = self.db.execute("SELECT continuity_json FROM conversations WHERE owner=? AND conversation_id=?",
                                  (owner, conversation_id)).fetchone()
            if row is None or not row[0]:
                return None
            try:
                if len(row[0]) > 100_000:
                    raise ValueError("context_limit")
                context = ConversationContext.model_validate_json(row[0])
                now = self._now()
                if (context.owner != owner or now < context.created_at or now >= context.expires_at
                        or context.snapshot and now < context.snapshot.created_at):
                    raise ValueError("context_unavailable")
                if context.snapshot and context.snapshot.report_id is not None:
                    if self.report(owner, context.snapshot.report_id) is None:
                        raise ValueError("report_unavailable")
                return context
            except (ValueError, ValidationError):
                self.clear_continuity(owner, conversation_id)
                return None
        except sqlite3.Error:
            raise StoreFailure("store_unavailable") from None

    def save_continuity(self, owner: str, conversation_id: str, context: ConversationContext) -> None:
        try:
            payload = ConversationContext.model_validate(context.model_dump()).model_dump_json()
            if context.owner != owner or len(payload) > 100_000:
                raise StoreFailure("store_unavailable")
            self.db.execute("INSERT INTO conversations(owner,conversation_id,cohort_origin,scope_version,continuity_json) "
                            "VALUES(?,?,?,?,?) ON CONFLICT(owner,conversation_id) DO UPDATE SET "
                            "continuity_json=excluded.continuity_json,cohort_origin=excluded.cohort_origin,"
                            "scope_version=excluded.scope_version,updated_at=CURRENT_TIMESTAMP",
                            (owner, conversation_id, context.cohort_origin.value, context.authorization_version, payload))
        except (sqlite3.Error, ValueError):
            raise StoreFailure("store_unavailable") from None

    def save_context(self, owner: str, conversation_id: str, plan: AnalysisPlan,
                     cohort_origin: CohortOrigin, scope_version: int) -> None:
        try:
            self.db.execute("""
                INSERT INTO conversations(owner,conversation_id,plan_json,cohort_origin,scope_version)
                VALUES(?,?,?,?,?)
                ON CONFLICT(owner,conversation_id) DO UPDATE SET
                  plan_json=excluded.plan_json, cohort_origin=excluded.cohort_origin,
                  scope_version=excluded.scope_version, updated_at=CURRENT_TIMESTAMP
            """, (owner, conversation_id, plan.model_dump_json(), cohort_origin.value, scope_version))
        except sqlite3.Error:
            raise StoreFailure("store_unavailable") from None

    def save_report(self, *, owner: str, conversation_id: str, turn_id: str,
                    content: str, metric_definitions: list[dict[str, Any]],
                    scope_reference: str, scope_version: int,
                    evidence_refs: list[dict[str, Any]],
                    request_digest: str = "", identity_digest: str = "",
                    plans: list[dict[str, Any]] | None = None, scope_digest: str = "",
                    grounded_report: dict[str, Any] | None = None,
                    report_completeness: str = "legacy",
                    report_composition: dict[str, Any] | None = None,
                    report_support: dict[str, Any] | None = None) -> int:
        try:
            def encoded(value, missing=""):
                return json.dumps(value, separators=(",", ":")) if value is not None else missing
            fields = dict(owner=owner, conversation_id=conversation_id, turn_id=turn_id,
                request_digest=request_digest, identity_digest=identity_digest,
                plans_json=encoded(plans or []), scope_digest=scope_digest, content=content,
                metric_definitions_json=encoded(metric_definitions), scope_reference=scope_reference,
                scope_version=scope_version, evidence_refs_json=encoded(evidence_refs),
                grounded_report_json=encoded(grounded_report), report_completeness=report_completeness,
                report_composition_json=encoded(report_composition), report_support_json=encoded(report_support))
            if report_completeness not in {"legacy", "complete", "partial"}:
                raise StoreFailure("invalid_result_contract")
            fields["report_binding_digest"] = ""
            if report_completeness != "legacy":
                try:
                    artifact = ReportArtifact.model_validate(report_support)
                    composition = ReportComposition.model_validate(report_composition)
                    grounded = GroundedReport.model_validate(grounded_report)
                    validate_artifact(fields, artifact, composition, grounded)
                except (ValidationError, PolicyError, ValueError, TypeError, KeyError):
                    raise StoreFailure("invalid_result_contract") from None
                fields["report_binding_digest"] = artifact_binding_digest(fields)
            columns = tuple(fields)
            self.db.execute(
                f"INSERT INTO reports({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                "ON CONFLICT(owner,conversation_id,turn_id) DO NOTHING", tuple(fields.values()))
            row = self.db.execute(
                f"SELECT id,{','.join(columns)} FROM reports "
                "WHERE owner=? AND conversation_id=? AND turn_id=? AND deleted_at IS NULL",
                (owner, conversation_id, turn_id)).fetchone()
            if row is None:
                tombstone = self.db.execute(
                    "SELECT 1 FROM reports WHERE owner=? AND conversation_id=? AND turn_id=? AND deleted_at IS NOT NULL",
                    (owner, conversation_id, turn_id)).fetchone()
                raise StoreFailure("report_deleted" if tombstone else "store_unavailable")
            if tuple(row[1:]) != tuple(fields.values()):
                raise StoreFailure("idempotency_conflict")
            return int(row[0])
        except sqlite3.Error:
            raise StoreFailure("store_unavailable") from None

    def save_turn_report(self, *, owner: str, conversation_id: str, turn_id: str,
                         plan: AnalysisPlan, cohort_origin: CohortOrigin,
                         content: str, metric_definitions: list[dict[str, Any]],
                         scope_reference: str, scope_version: int,
                         evidence_refs: list[dict[str, Any]],
                         request_digest: str, identity_digest: str,
                         plans: list[dict[str, Any]], scope_digest: str,
                         grounded_report: dict[str, Any] | None = None,
                         continuity: ConversationContext | None = None,
                         report_completeness: str = "legacy",
                         report_composition: dict[str, Any] | None = None,
                         report_support: dict[str, Any] | None = None) -> int:
        try:
            self.db.execute("BEGIN IMMEDIATE")
            report_id = self.save_report(
                owner=owner, conversation_id=conversation_id, turn_id=turn_id,
                content=content, metric_definitions=metric_definitions,
                scope_reference=scope_reference, scope_version=scope_version,
                evidence_refs=evidence_refs, request_digest=request_digest,
                identity_digest=identity_digest, plans=plans, scope_digest=scope_digest,
                grounded_report=grounded_report, report_completeness=report_completeness,
                report_composition=report_composition, report_support=report_support,
            )
            self.save_context(owner, conversation_id, plan, cohort_origin, scope_version)
            if continuity is not None:
                linked = continuity.model_copy(update={"snapshot": continuity.snapshot.model_copy(update={"report_id": report_id})})
                self.save_continuity(owner, conversation_id, linked)
            self.db.execute("COMMIT")
            return report_id
        except (StoreFailure, sqlite3.Error) as exc:
            try:
                self.db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, StoreFailure):
                raise
            raise StoreFailure("store_unavailable") from None

    def find_retry(self, owner: str, conversation_id: str, turn_id: str,
                   *, request_digest: str, identity_digest: str) -> dict[str, Any] | None:
        try:
            cursor = self.db.execute("SELECT * FROM reports WHERE owner=? AND conversation_id=? AND turn_id=?",
                                     (owner, conversation_id, turn_id))
            raw = cursor.fetchone()
            if raw is None:
                return None
            fields = dict(zip((column[0] for column in cursor.description), raw))
            if fields["request_digest"] != request_digest or fields["identity_digest"] != identity_digest:
                raise StoreFailure("idempotency_conflict")
            if fields["deleted_at"] is not None:
                return {"deleted": True}
            return {"id": int(fields["id"]), "content": fields["content"],
                    "evidence_refs": json.loads(fields["evidence_refs_json"]),
                    "plans": json.loads(fields["plans_json"]), "scope_digest": fields["scope_digest"],
                    "scope_version": fields["scope_version"], "fields": fields,
                    "grounded_report": json.loads(fields["grounded_report_json"]) if fields["grounded_report_json"] else None,
                    "report_completeness": fields["report_completeness"],
                    "report_composition": json.loads(fields["report_composition_json"]) if fields["report_composition_json"] else None}
        except (sqlite3.Error, ValueError):
            raise StoreFailure("store_unavailable") from None

    def report(self, owner: str, report_id: int) -> dict[str, Any] | None:
        try:
            self.db.row_factory = sqlite3.Row
            row = self.db.execute("SELECT * FROM reports WHERE owner=? AND id=? AND deleted_at IS NULL", (owner, report_id)).fetchone()
            return dict(row) if row else None
        except sqlite3.Error:
            raise StoreFailure("store_unavailable") from None

    def list_reports(self, owner: str) -> list[dict[str, Any]]:
        """Active owner-bound metadata only; never return bodies from a listing."""
        try:
            rows = self.db.execute(
                "SELECT id,content,conversation_id,version,report_completeness,report_support_json,report_binding_digest FROM reports "
                "WHERE owner=? AND deleted_at IS NULL ORDER BY id", (owner,),
            ).fetchall()
            return [{"id": row[0], "title": self._title(row[1]),
                     "conversation_id": row[2], "version": row[3], "completeness": row[4] if row[5] and row[6] else "legacy"} for row in rows]
        except sqlite3.Error:
            raise StoreFailure("store_unavailable") from None

    @staticmethod
    def _title(content: str) -> str:
        line = next((line.strip() for line in content.splitlines() if line.strip()), "Untitled report")
        return "".join(ch if ch.isprintable() else " " for ch in line)[:80]

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def preview_deletion(self, owner: str, selector: str, value: str,
                         *, conversation_id: str | None = None) -> dict[str, Any]:
        """Persist an exact active target set before returning its single-use token."""
        if selector not in {"literal", "conversation"} or not value or len(value) > 200:
            raise StoreFailure("invalid_selector")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if selector == "conversation":
                rows = self.db.execute(
                    "SELECT id,version,content FROM reports WHERE owner=? AND conversation_id=? "
                    "AND deleted_at IS NULL ORDER BY id LIMIT 101", (owner, value),
                ).fetchall()
            else:
                # Python casefold gives literal Unicode matching; SQL LIKE would treat % and _ as wildcards.
                candidates = self.db.execute(
                    "SELECT id,version,content FROM reports WHERE owner=? AND deleted_at IS NULL "
                    "ORDER BY id LIMIT 1001", (owner,),
                ).fetchall()
                if len(candidates) > 1000:
                    raise StoreFailure("selection_too_broad")
                rows = [row for row in candidates if value.casefold() in row[2].casefold()]
            if len(rows) > 100:
                raise StoreFailure("selection_too_broad")
            targets = [{"id": int(row[0]), "version": int(row[1]),
                        "title": self._title(row[2])} for row in rows]
            if not targets:
                self.db.execute("COMMIT")
                return {"targets": [], "token": None}
            token = secrets.token_urlsafe(32)
            now = self._now()
            expires = now + timedelta(minutes=5)
            self.db.execute(
                "INSERT INTO deletion_proposals(owner,selector,selector_value,conversation_id,targets_json,"
                "token_digest,created_at,expires_at,state) VALUES(?,?,?,?,?,?,?,?, 'pending')",
                (owner, selector, value, conversation_id,
                 json.dumps([{"id": t["id"], "version": t["version"]} for t in targets], separators=(",", ":")),
                 hashlib.sha256(token.encode()).hexdigest(), now.isoformat(), expires.isoformat()),
            )
            self.db.execute("COMMIT")
            return {"targets": targets, "token": token, "expires_at": expires.isoformat()}
        except (sqlite3.Error, StoreFailure) as exc:
            self._rollback()
            if isinstance(exc, StoreFailure):
                raise
            raise StoreFailure("store_unavailable") from None

    def resolve_deletion(self, owner: str, token: str, *, cancel: bool = False) -> tuple[str, list[int]]:
        """Revalidate and consume a proposal in one transaction; never return content."""
        if len(token) != 43:
            return "invalid", []
        try:
            self.db.execute("BEGIN IMMEDIATE")
            digest = hashlib.sha256(token.encode()).hexdigest()
            proposal = self.db.execute(
                "SELECT id,selector,selector_value,targets_json,expires_at,state FROM deletion_proposals "
                "WHERE owner=? AND token_digest=?", (owner, digest),
            ).fetchone()
            if proposal is None or proposal[5] != "pending":
                self.db.execute("COMMIT")
                return "invalid", []
            proposal_id, selector, selector_value, targets_json, expires_at, _ = proposal
            if self._now() >= datetime.fromisoformat(expires_at):
                self.db.execute("UPDATE deletion_proposals SET state='expired' WHERE id=?", (proposal_id,))
                self.db.execute("COMMIT")
                return "expired", []
            if cancel:
                self.db.execute("UPDATE deletion_proposals SET state='cancelled' WHERE id=?", (proposal_id,))
                self.db.execute("COMMIT")
                return "cancelled", []
            targets = json.loads(targets_json)
            ids = [int(t["id"]) for t in targets]
            if not ids or len(ids) > 100 or len(ids) != len(set(ids)):
                raise StoreFailure("invalid_proposal")
            placeholders = ",".join("?" for _ in ids)
            rows = self.db.execute(
                f"SELECT id,owner,version,deleted_at,conversation_id FROM reports WHERE id IN ({placeholders})", ids,
            ).fetchall()
            current = {row[0]: row for row in rows}
            if any(t["id"] not in current or current[t["id"]][1] != owner or
                   current[t["id"]][2] != t["version"] or current[t["id"]][3] is not None or
                   (selector == "conversation" and current[t["id"]][4] != selector_value)
                   for t in targets):
                self.db.execute("UPDATE deletion_proposals SET state='stale' WHERE id=?", (proposal_id,))
                self.db.execute("COMMIT")
                return "stale", []
            now = self._now().isoformat()
            for target in targets:
                result = self.db.execute(
                    "UPDATE reports SET deleted_at=?,version=version+1 WHERE id=? AND owner=? "
                    "AND version=? AND deleted_at IS NULL",
                    (now, target["id"], owner, target["version"]),
                )
                if result.rowcount != 1:
                    raise StoreFailure("store_unavailable")
            # Invalidate every linked cache in the same deletion transaction.
            for report_id in ids:
                self.db.execute("UPDATE conversations SET continuity_json='',plan_json=NULL,scope_version=NULL "
                                "WHERE owner=? AND CASE WHEN json_valid(continuity_json) THEN "
                                "json_extract(continuity_json,'$.snapshot.report_id') END=?", (owner, report_id))
            self.db.execute("UPDATE deletion_proposals SET state='consumed' WHERE id=?", (proposal_id,))
            self.db.execute("COMMIT")
            return "deleted", ids
        except (sqlite3.Error, ValueError, KeyError, TypeError, StoreFailure) as exc:
            self._rollback()
            if isinstance(exc, StoreFailure):
                raise
            raise StoreFailure("store_unavailable") from None

    def _rollback(self) -> None:
        try:
            self.db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
