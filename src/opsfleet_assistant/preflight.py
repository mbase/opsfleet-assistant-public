"""Bounded, read-only connectivity and schema inspection for Step 1.

Outputs only allowlisted metadata and safe error categories. Never log SDK
exceptions: provider errors can contain request details.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any, Sequence

from .config import Settings


DATASET = "bigquery-public-data.thelook_ecommerce"
TABLES = ("orders", "order_items", "products", "users")
MAX_QUERY_BYTES = 512 * 1024 * 1024


def _error(category: str, exc: Exception | None = None) -> dict[str, str]:
    result = {"status": category}
    if exc is not None:
        result["error_type"] = type(exc).__name__
        code = getattr(exc, "code", None)
        if isinstance(code, int) and 400 <= code <= 599:
            result["http_status"] = code
    return result


def _run_query(client: Any, sql: str, location: str) -> dict[str, Any]:
    from google.cloud import bigquery

    record: dict[str, Any] = {
        "status": "request_failed", "stage": "dry_run", "estimated_bytes": None,
        "billed_bytes": None, "billing_status": "not_submitted", "job_id": None,
        "dry_run_attempts": 0, "execution_attempts": 0, "submission_status": "not_attempted",
    }
    job = None
    try:
        dry_config = bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, maximum_bytes_billed=MAX_QUERY_BYTES
        )
        record["dry_run_attempts"] = 1
        dry_job = client.query(
            sql, job_config=dry_config, location=location, timeout=20,
            retry=None, job_retry=None,
        )
        estimated = _byte_count(dry_job.total_bytes_processed)
        record["estimated_bytes"] = estimated
        if estimated is None:
            record["status"] = "estimate_unknown"
            return record
        if estimated > MAX_QUERY_BYTES:
            record["status"] = "scan_limit"
            return record

        run_config = bigquery.QueryJobConfig(
            use_query_cache=False, maximum_bytes_billed=MAX_QUERY_BYTES,
            job_timeout_ms=30_000, labels={"purpose": "opsfleet_preflight"},
        )
        record.update(stage="submission", execution_attempts=1,
                      submission_status="unknown", billing_status="unknown")
        job = client.query(
            sql, job_config=run_config, location=location, timeout=20,
            retry=None, job_retry=None,
        )
        job_id = getattr(job, "job_id", None)
        record.update(stage="result", submission_status="submitted",
                      job_id=job_id if isinstance(job_id, str) and
                      re.fullmatch(r"[A-Za-z0-9_-]{1,256}", job_id) else None)
        rows = [dict(row.items()) for row in job.result(
            timeout=35, max_results=150, retry=None, job_retry=None,
        )]
        billed = _byte_count(job.total_bytes_billed)
        record.update(status="ok" if billed is not None else "billing_unknown",
                      stage="completed", rows=rows, billed_bytes=billed,
                      billing_status="known" if billed is not None else "unknown")
    except Exception as exc:
        record.update(_error("request_failed", exc))
        if job is not None:
            # Cancellation is a request, not proof that execution/billing settled.
            # Preserve the original failure even when cancellation itself fails.
            try:
                record["reported_billed_bytes"] = _byte_count(job.total_bytes_billed)
            except Exception:
                record["reported_billed_bytes"] = None
            try:
                requested = job.cancel(retry=None, timeout=20)
                record["cancellation_status"] = "requested" if requested else "not_confirmed"
            except Exception:
                record["cancellation_status"] = "failed"
    return record


def _byte_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _preflight_result(report: dict[str, Any]) -> dict[str, Any]:
    """Keep known work visible even when a later check could not complete."""
    checks = [report.get("schema"), *report["statuses"].values(),
              *report["date_coverage"].values(), report.get("join_grain")]
    checks = [check for check in checks if check is not None]
    known = sum(check["billed_bytes"] for check in checks
                if _byte_count(check.get("billed_bytes")) is not None)
    unknown = any(check.get("billing_status") == "unknown" for check in checks)
    report["accounting"] = {
        "dry_run_attempts": sum(check.get("dry_run_attempts", 0) for check in checks),
        "execution_attempts": sum(check.get("execution_attempts", 0) for check in checks),
        "submitted_jobs": sum(check.get("submission_status") == "submitted" for check in checks),
        "unknown_submissions": sum(check.get("submission_status") == "unknown" for check in checks),
        "known_billed_bytes": known, "billing_unknown": unknown,
        "billed_bytes": None if unknown else known,
    }
    return report


def bigquery_preflight(settings: Settings) -> dict[str, Any]:
    if not settings.project:
        return _error("missing_project")
    report: dict[str, Any] = {
        "status": "incomplete", "project": settings.project, "location": settings.location,
        "dataset": DATASET, "schema": None, "statuses": {}, "date_coverage": {},
        "join_grain": {"status": "not_run"},
    }
    current_check = "configuration"
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        return _error("missing_dependency", exc)

    try:
        client = bigquery.Client(project=settings.project, location=settings.location)
        schema_sql = f"""
            SELECT table_name, column_name, data_type, is_nullable
            FROM `{DATASET}.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name IN ('orders', 'order_items', 'products', 'users')
            ORDER BY table_name, ordinal_position
        """
        current_check = "schema"
        schema = report["schema"] = _run_query(client, schema_sql, settings.location)
        if schema["status"] != "ok":
            report["failed_check"] = current_check
            if schema["status"] == "scan_limit":
                report["status"] = "schema_scan_limit"
            return _preflight_result(report)

        columns = {
            table: {row["column_name"] for row in schema["rows"] if row["table_name"] == table}
            for table in TABLES
        }
        if any(not names for names in columns.values()):
            report.update(status="missing_required_table", failed_check=current_check)
            return _preflight_result(report)

        for table in ("orders", "order_items"):
            current_check = f"statuses.{table}"
            if "status" in columns[table]:
                result = _run_query(
                    client,
                    f"SELECT status, COUNT(*) AS row_count FROM `{DATASET}.{table}` "
                    "GROUP BY status ORDER BY row_count DESC LIMIT 20",
                    settings.location,
                )
            else:
                result = {"status": "missing_required_column", "missing_columns": ["status"]}
            report["statuses"][table] = result
            if result["status"] != "ok":
                report["failed_check"] = current_check
                return _preflight_result(report)

        for table in TABLES:
            current_check = f"date_coverage.{table}"
            if "created_at" in columns[table]:
                result = _run_query(
                    client,
                    f"SELECT MIN(created_at) AS first_created_at, "
                    f"MAX(created_at) AS last_created_at, "
                    f"COUNTIF(created_at IS NULL) AS missing_created_at "
                    f"FROM `{DATASET}.{table}`",
                    settings.location,
                )
            else:
                result = {"status": "missing_required_column", "missing_columns": ["created_at"]}
            report["date_coverage"][table] = result
            if result["status"] != "ok":
                report["failed_check"] = current_check
                return _preflight_result(report)

        join_columns = {
            "orders": {"order_id", "user_id"},
            "order_items": {"order_id", "product_id"},
            "products": {"id", "brand"},
            "users": {"id"},
        }
        joins = {table: sorted(required - columns[table]) for table, required in join_columns.items()}
        current_check = "join_grain"
        join_result = {"status": "missing_required_column", "missing_columns": joins}
        if all(not missing for missing in joins.values()):
            join_result = _run_query(
                client,
                f"""
                SELECT COUNT(*) AS item_rows,
                       COUNT(DISTINCT oi.order_id) AS distinct_item_orders,
                       COUNTIF(o.order_id IS NULL) AS items_without_order,
                       COUNTIF(p.id IS NULL) AS items_without_product,
                       COUNTIF(o.order_id IS NOT NULL AND u.id IS NULL) AS items_without_user
                FROM `{DATASET}.order_items` AS oi
                LEFT JOIN `{DATASET}.orders` AS o ON oi.order_id = o.order_id
                LEFT JOIN `{DATASET}.products` AS p ON oi.product_id = p.id
                LEFT JOIN `{DATASET}.users` AS u ON o.user_id = u.id
                """,
                settings.location,
            )

        report["join_grain"] = join_result
        if join_result["status"] == "ok":
            report["status"] = "ok"
        else:
            report["failed_check"] = current_check
    except Exception as exc:
        report.update(_error("request_failed", exc), failed_check=current_check)
    return _preflight_result(report)


def gemini_preflight(settings: Settings) -> dict[str, Any]:
    if not settings.gemini_api_key:
        return _error("missing_api_key")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        return _error("missing_dependency", exc)

    try:
        with genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=20_000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        ) as client:
            response = client.models.generate_content(
                model=settings.model,
                contents="Return a JSON object with status set to ok.",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema={
                        "type": "object",
                        "properties": {"status": {"type": "string", "enum": ["ok"]}},
                        "required": ["status"],
                        "additionalProperties": False,
                    },
                    max_output_tokens=512,
                    thinking_config=types.ThinkingConfig(thinking_level="low"),
                ),
            )
        parsed = json.loads(response.text or "")
        return {
            "status": "ok" if parsed == {"status": "ok"} else "unexpected_output",
            "model": settings.model,
            "structured_output": parsed == {"status": "ok"},
        }
    except Exception as exc:
        return {"model": settings.model, **_error("request_failed", exc)}


def openrouter_preflight(settings: Settings) -> dict[str, Any]:
    """Probe an independent API route using a fixed, non-sensitive prompt."""
    if not settings.openrouter_api_key:
        return _error("missing_api_key")
    try:
        import httpx
    except ImportError as exc:
        return _error("missing_dependency", exc)

    request = {
        "model": settings.openrouter_model,
        "messages": [{"role": "user", "content": "Return a JSON object with status set to ok."}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "opsfleet_preflight",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"status": {"type": "string", "enum": ["ok"]}},
                    "required": ["status"],
                    "additionalProperties": False,
                },
            },
        },
        "provider": {
            "require_parameters": True,
            "zdr": True,
            "data_collection": "deny",
        },
        "reasoning": {"effort": "low"},
        "max_tokens": 512,
    }
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            response = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json=request,
            )
        if response.status_code != 200:
            if response.status_code == 401:
                auth = openrouter_auth_preflight(settings)
                if auth["status"] == "wrong_key_type":
                    return {"model": settings.openrouter_model, **auth}
            return {
                "model": settings.openrouter_model,
                "status": "request_failed",
                "error_type": "HTTPError",
                "http_status": response.status_code,
            }
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") == "length" or choice.get(
            "native_finish_reason"
        ) == "MAX_TOKENS":
            return {"model": settings.openrouter_model, "status": "truncated_output"}
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            return {"model": settings.openrouter_model, "status": "empty_output"}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"model": settings.openrouter_model, "status": "invalid_json"}
        valid = parsed == {"status": "ok"}
        return {
            "model": settings.openrouter_model,
            "status": "ok" if valid else "unexpected_output",
            "structured_output": valid,
        }
    except Exception as exc:
        from .model_gateway import failure_details
        return {"model": settings.openrouter_model, **_error("request_failed", exc), **failure_details(exc)}


def openrouter_auth_preflight(settings: Settings) -> dict[str, Any]:
    """Check only key authentication; do not request a model response."""
    if not settings.openrouter_api_key:
        return _error("missing_api_key")
    try:
        import httpx
    except ImportError as exc:
        return _error("missing_dependency", exc)
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            )
        if response.status_code == 200:
            data = response.json().get("data", {})
            if data.get("is_management_key") is True:
                return {"status": "wrong_key_type", "required": "inference_api_key"}
            return {"status": "ok"}
        return {
            "status": "request_failed",
            "error_type": "HTTPError",
            "http_status": response.status_code,
        }
    except Exception as exc:
        return _error("request_failed", exc)


def _transient_model_failure(result: dict[str, Any]) -> bool:
    if result.get("status") != "request_failed":
        return False
    code = result.get("http_status")
    return code in {408, 429} or isinstance(code, int) and 500 <= code <= 599 or result.get(
        "error_type"
    ) in {"ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only OpsFleet Step 1 preflight")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--all", action="store_true")
    choice.add_argument("--bigquery", action="store_true")
    choice.add_argument("--gemini", action="store_true")
    choice.add_argument("--openrouter", action="store_true")
    choice.add_argument("--openrouter-auth", action="store_true", help="Check key without model use")
    choice.add_argument("--models", action="store_true", help="Gemini, then optional fallback")
    args = parser.parse_args(argv)

    settings = Settings.load()
    results: dict[str, Any] = {}
    if args.all or args.bigquery:
        results["bigquery"] = bigquery_preflight(settings)
    if args.all or args.gemini or args.models:
        results["gemini"] = gemini_preflight(settings)
    if args.openrouter or (
        (args.all or args.models) and _transient_model_failure(results["gemini"])
    ):
        results["openrouter"] = openrouter_preflight(settings)
    if args.openrouter_auth:
        results["openrouter_auth"] = openrouter_auth_preflight(settings)
    print(json.dumps(results, indent=2, default=str))
    if args.all or args.models:
        data_ok = not args.all or results["bigquery"]["status"] == "ok"
        model_ok = any(
            results.get(provider, {}).get("status") == "ok"
            for provider in ("gemini", "openrouter")
        )
        return 0 if data_ok and model_ok else 1
    return 0 if all(result["status"] == "ok" for result in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
