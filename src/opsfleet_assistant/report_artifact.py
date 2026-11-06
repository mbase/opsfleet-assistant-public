"""Durable released report support, independent of expiring conversation state.

The checksum detects independent corruption, not a malicious local operator.
Semantic checks remain necessary even when that checksum has been recomputed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from .contracts import AnalysisPlan, ReportDraft, StrictModel
from .conversation import AnalysisSnapshot, validate_snapshot_request
from .evidence import Evidence
from .identity import PolicyError
from .reporting import GroundedReport, ReportComposition, compose_report, ground_report


class ReportArtifact(StrictModel):
    schema_version: Literal[1] = 1
    snapshot: AnalysisSnapshot


BINDING_FIELDS = (
    "owner", "conversation_id", "turn_id", "request_digest", "identity_digest", "plans_json",
    "scope_digest", "content", "metric_definitions_json", "scope_reference", "scope_version",
    "evidence_refs_json", "grounded_report_json", "report_completeness", "report_composition_json",
    "report_support_json",
)


def artifact_binding_digest(fields: dict) -> str:
    payload = {key: fields[key] for key in BINDING_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def evidence_references(evidence: tuple[Evidence, ...]) -> list[dict]:
    return [{"job_id": item.job_id, "coverage_job_id": item.coverage.job_id,
             "period_utc": item.period_utc, "complete": item.complete} for item in evidence]


def reconstruct_grounded(snapshot: AnalysisSnapshot) -> GroundedReport:
    """Rebuild facts from typed selections; caller chooses reuse vs equality check."""
    old = snapshot.grounded
    refused = old.selection_status == "refused"
    draft = ReportDraft(status="refused" if refused else "selected",
        findings=() if refused else tuple(f.selection for f in old.findings),
        headline_index=None if refused else next((i for i, f in enumerate(old.findings) if f.text == old.headline), None),
        recommendations=tuple(a.selection for a in old.recommendations),
        hypotheses=tuple(a.selection for a in old.hypotheses))
    return ground_report(draft, snapshot.evidence, fallback=old.selection_status == "fallback")


def validate_artifact(fields: dict, artifact: ReportArtifact, composition: ReportComposition,
                      grounded: GroundedReport) -> None:
    """Validate saved execution and actual section support; never regenerate content.

    Snapshot clocks validate symbolic roles after continuity expires. The saved
    released operands validate findings, actions, references and completeness.
    Current owner/catalog/field authorization remains the workflow's boundary.
    """
    snapshot = artifact.snapshot
    validate_snapshot_request(snapshot)
    plans = tuple(AnalysisPlan.model_validate(p) for p in json.loads(fields["plans_json"]))
    if (snapshot.plans != plans or snapshot.request != composition.request
            or json.loads(fields["metric_definitions_json"]) != [e.metric_definition for e in snapshot.evidence]
            or json.loads(fields["evidence_refs_json"]) != evidence_references(snapshot.evidence)
            or any(e.scope_reference != fields["scope_reference"] or e.scope_version != fields["scope_version"]
                   or e.scope_reference != f"demo:{fields['owner']}" for e in snapshot.evidence)):
        raise PolicyError("invalid_result_contract")
    checked = reconstruct_grounded(snapshot)
    if checked != snapshot.grounded or grounded != checked:
        raise PolicyError("invalid_result_contract")
    expected = compose_report(snapshot.request, snapshot.evidence, checked)
    prefix = "Partial report:" if expected.completeness == "partial" else "Complete report:"
    if (composition != expected or fields["report_completeness"] != expected.completeness
            or not fields["content"].startswith(prefix)):
        raise PolicyError("invalid_result_contract")
