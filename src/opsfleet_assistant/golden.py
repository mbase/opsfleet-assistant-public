"""Small authored/synthetic Golden corpus and strict method-only retrieval."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .config import ROOT
from .contracts import AnalysisPlan, BrandScope, Metric, Operation, RequestSpec
from .metrics import METRIC_VERSION


CORPUS = ROOT / "config" / "golden_demo.json"
SCHEMA_VERSION = 1
METHODS = {"monthly_revenue_trend": "Use the supported monthly time_series operation for a revenue trajectory."}


@dataclass(frozen=True)
class Selection:
    trio_id: str
    version: int
    method: str
    summary: str


def retrieve(request: RequestSpec, scope: BrandScope, *, corpus: Path = CORPUS) -> Selection | None:
    """Use interpreted intent and trusted scope; raw question/SQL/report never leave here."""
    if (request.mode != "analysis" or request.purpose != "trend" or len(request.queries) != 1):
        return None
    query = request.queries[0]
    if (query.metric != Metric.REVENUE or query.operation not in {Operation.AGGREGATE, Operation.TIME_SERIES}
            or query.dimension is not None or query.product_ids is not None or query.demographic_values is not None):
        return None
    try:
        records = json.loads(corpus.read_text(encoding="utf-8"))
        if not isinstance(records, list) or len(records) > 100:
            raise ValueError("invalid_corpus")
    except (OSError, ValueError, TypeError) as exc:
        raise RetrievalFailure from exc
    metric = query.metric
    eligible: list[tuple[int, Selection]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        # Exact known fields and controlled method registry; raw trios never leave this module.
        if (record.get("status") != "approved" or record.get("schema_version") != SCHEMA_VERSION
                or record.get("metric") != metric.value or record.get("metric_version") != METRIC_VERSION
                or record.get("operation") != Operation.TIME_SERIES.value
                or record.get("method") not in METHODS):
            continue
        visibility = record.get("visibility")
        brands = record.get("brand_scope")
        if visibility == "brand_restricted":
            if not isinstance(brands, list) or not brands or not set(brands).issubset(scope.brands):
                continue
        elif visibility != "global_methodology":
            continue
        trio_id, version = record.get("trio_id"), record.get("version")
        if not isinstance(trio_id, str) or not re.fullmatch(r"GK-[0-9]{3}", trio_id) or type(version) is not int or version < 1:
            continue
        keywords = record.get("keywords")
        if not isinstance(keywords, list) or any(not isinstance(k, str) or not re.fullmatch(r"[a-z]+", k) for k in keywords):
            continue
        eligible.append((1, Selection(trio_id, version, record["method"], METHODS[record["method"]])))
    eligible.sort(key=lambda pair: (-pair[0], pair[1].trio_id))
    return eligible[0][1] if eligible else None


class RetrievalFailure(Exception):
    pass


def apply_method(plan: AnalysisPlan, selection: Selection | None) -> AnalysisPlan:
    if selection is None or selection.method != "monthly_revenue_trend":
        return plan
    if plan.metric != Metric.REVENUE or plan.period is None or plan.operation not in {Operation.AGGREGATE, Operation.TIME_SERIES}:
        return plan
    if plan.dimension is not None or plan.product_ids is not None or plan.demographic_values is not None:
        return plan
    data = plan.model_dump(mode="python")
    data["operation"] = Operation.TIME_SERIES
    data["limit"] = min(20, max(12, plan.limit))
    return AnalysisPlan.model_validate(data)
