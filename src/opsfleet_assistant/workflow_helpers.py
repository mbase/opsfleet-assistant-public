"""Pure grounding and scope-digest compatibility helpers."""
from __future__ import annotations

import hashlib
import json

from .compiler import CompiledQuery
from .evidence import Evidence
from .model_gateway import ReportDraft
from .reporting import GroundedReport, ground_report


def validate_draft(draft: ReportDraft, evidence: tuple[Evidence, ...]) -> GroundedReport:
    return ground_report(draft, evidence)


def scope_digest(queries: tuple[CompiledQuery, ...]) -> str:
    brand_sets = [list(query.effective_brands) for query in queries]
    return hashlib.sha256(json.dumps(brand_sets, separators=(",", ":")).encode("utf-8")).hexdigest()
