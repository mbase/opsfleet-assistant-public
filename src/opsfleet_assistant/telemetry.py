"""Local, best-effort, sanitized JSON events for the prototype.

This is diagnostic output, not a durable or tamper-resistant audit log.
Only fixed vocabulary and numeric fields can cross this boundary.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from uuid import UUID, uuid4

from .config import ROOT


EVENTS = frozenset({
    "playbook", "turn_start", "turn_end", "stage", "policy", "golden", "model_attempt",
    "model_result", "sql_validation", "bq_dry_run", "bq_job", "empty_result",
    "source_metadata", "recovery", "report_persistence", "deletion", "final_outcome", "prompt_budget",
})
WORDS = frozenset({
    "unavailable", "product_revenue", "regional_spend", "churn_change", "available", "incomplete_evidence",
    "missing_population", "incompatible_evidence", "undefined_denominator", "reconciliation_failed", "segment_unavailable",
    "start", "ok", "error", "clarification", "needs_clarification", "unsupported",
    "refusal", "confirmation_required", "deleted", "partial", "complete",
    "unknown", "known", "approved", "retired", "selected", "no_match", "outage", "excluded",
    "baseline", "monthly_revenue_trend", "none", "analysis", "report", "catalog", "coverage",
    "customer_check", "privacy_screen", "intent_metric_resolution", "structured_planning",
    "authorization", "step3_execution", "evidence_checks", "explanation_drafting",
    "claim_validation", "display_save", "deletion_preview", "deletion_confirm", "deletion_cancel",
    "gemini_direct", "openrouter_gemini", "injected_model", "aggregate", "rank_customers",
    "time_series", "product_comparison", "demographic_comparison", "churn_snapshot",
    "revenue", "order_count", "average_order_value", "customer_count", "customer_spend",
    "average_customer_spend", "churn_rate", "syntax", "schema", "transient", "budget",
    "empty", "metadata", "policy", "provider", "storage", "internal", "success",
    "inspect_schema", "recompile_supported_binding", "schema_rejection", "binding_changed",
    "retry_before_submission", "transient_before_submission", "submission_unsafe", "retry_limit", "billing_uncertain", "correction_unavailable", "not_retryable", "exhausted", "attempted",
    "retry", "repair", "fallback", "stop", "valid_empty", "source_empty", "coverage_mismatch",
    "saved", "reused", "failed", "previewed", "cancelled", "expired", "stale",
    "no_targets", "invalid", "dry_run", "execute", "report_skipped", "diagnostic",
})
ENUM_KEYS = frozenset({"playbook_id", "availability_reason", "action", "reason", "outcome", "stage", "status", "category", "route", "purpose", "operation", "metric", "method", "decision", "usage"})
INT_KEYS = frozenset({"count", "bytes", "tokens", "latency_ms", "version", "estimated_bytes", "billed_bytes", "executions", "dry_runs", "requests", "prompt_chars", "instructions_chars", "context_chars",
    "previous_plan_chars", "question_chars", "feedback_chars", "messages_dropped", "protected_messages"})
ID_KEYS = frozenset({"job_id", "trio_id"})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TRIO = re.compile(r"^GK-[0-9]{3}$")


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return str(uuid4())


class TurnTrace:
    def __init__(self, conversation_id: str, turn_id: str, path: Path | None = None):
        self.trace_id = str(uuid4())
        self.conversation_id = _uuid(conversation_id)
        self.turn_id = _uuid(turn_id)
        self.path = path or ROOT / "runtime" / "events.jsonl"
        self.started = time.monotonic()

    def emit(self, event: str, **fields: object) -> None:
        if event not in EVENTS:
            return
        record: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "trace_id": self.trace_id, "conversation_id": self.conversation_id,
            "turn_id": self.turn_id, "event": event,
        }
        for key, value in fields.items():
            if key in {'failure_stage','failure_code','finish_category','content_availability'}:
                from .model_gateway import FAILURE_CODES, FAILURE_STAGES, FINISH_CATEGORIES, CONTENT_AVAILABILITY
                allowed={'failure_stage':FAILURE_STAGES,'failure_code':FAILURE_CODES,'finish_category':FINISH_CATEGORIES,
                    'content_availability':CONTENT_AVAILABILITY}[key]
                record[key]=value if isinstance(value,str) and value in allowed else 'unknown'
            elif key == 'failure_kind':
                record[key] = value if isinstance(value, str) and value in {'http', 'dns_resolution', 'timeout', 'connection', 'unknown'} else 'unknown'
            elif key == 'transport_type':
                record[key] = value if isinstance(value, str) and value in {'gaierror', 'TimeoutException', 'ConnectError', 'NetworkError', 'ProtocolError', 'ProxyError'} else 'unknown'
            elif key == 'http_status':
                if type(value) is int and 100 <= value <= 599:
                    record[key] = value
            elif key == 'dns_errno':
                if type(value) is int and -10000 <= value <= 10000:
                    record[key] = value
            elif key in ENUM_KEYS:
                record[key] = value if isinstance(value, str) and value in WORDS else "unknown"
            elif key in INT_KEYS:
                if type(value) is int and 0 <= value <= 2**50:
                    record[key] = value
            elif key == "job_id":
                record[key] = value if isinstance(value, str) and _UUID.fullmatch(value) else "unknown"
            elif key == "trio_id":
                record[key] = value if isinstance(value, str) and _TRIO.fullmatch(value) else "unknown"
        if len(json.dumps(record)) > 1200:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError:
            pass  # Local diagnostics must not change the analysis result.

    def finish(self, status: str, *, category: str = "none", requests: int = 0,
               executions: int = 0, billed_bytes: int | None = 0) -> None:
        self.emit("final_outcome", status=status, category=category)
        self.emit("turn_end", status=status, category=category, requests=requests,
                  executions=executions, billed_bytes=billed_bytes,
                  latency_ms=max(0, int((time.monotonic() - self.started) * 1000)))


def agent_metrics(path: Path) -> dict[str, object]:
    """Derive bounded-cardinality counters from local events; no identity labels."""
    counts: Counter[str] = Counter()
    token_total = 0
    token_unknown = 0
    billed = 0
    estimated = 0
    latency: list[int] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        event = item.get("event")
        if event == "turn_end":
            status = item.get("status")
            label = "clarification" if status == "confirmation_required" else status
            counts[f"outcome.{label if label in {'ok', 'clarification', 'needs_clarification', 'unsupported', 'refusal', 'error', 'partial'} else 'other'}"] += 1
            latency.append(item.get("latency_ms", 0) if type(item.get("latency_ms")) is int else 0)
        elif event == "stage" and item.get("status") == "error":
            stage = item.get("stage")
            counts[f"error_stage.{stage if stage in WORDS else 'unknown'}"] += 1
        elif event == "model_result":
            if item.get("usage") == "known" and type(item.get("tokens")) is int:
                token_total += item["tokens"]
            else:
                token_unknown += 1
        elif event == "source_metadata":
            counts["bq.metadata_requests"] += 1
        elif event == "bq_dry_run":
            counts["bq.dry_runs"] += 1
            estimated += item.get("estimated_bytes", 0) if type(item.get("estimated_bytes")) is int else 0
        elif event == "bq_job":
            counts["bq.executions"] += 1
            if item.get("usage") == "unknown":
                counts["bq.billing_unknown"] += 1
            billed += item.get("billed_bytes", 0) if type(item.get("billed_bytes")) is int else 0
        elif event in {"recovery", "empty_result"}:
            if event == "recovery" and item.get("outcome") in {"attempted", "success", "unsupported", "exhausted", "failed"}:
                counts["recovery.outcome." + item["outcome"]] += 1
            decision = item.get("decision", item.get("action"))
            counts[f"{event}.{decision if decision in WORDS else 'unknown'}"] += 1
        elif event == "model_attempt":
            counts["model.requests"] += 1
        elif event == "final_outcome" and item.get("status") == "error":
            category = item.get("category")
            counts[f"error.{category if category in WORDS else 'unknown'}"] += 1
    return {"counts": dict(sorted(counts.items())), "provider_tokens_reported": token_total,
            "provider_token_reports_unknown": token_unknown,
            "estimated_bytes": estimated, "billed_bytes": billed,
            "latency_ms": {"count": len(latency), "sum": sum(latency), "max": max(latency, default=0)}}
