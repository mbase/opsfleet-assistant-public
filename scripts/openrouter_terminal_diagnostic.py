#!/usr/bin/env python3
"""Run a small OpenRouter DNS and response-limit diagnostic.

The script makes two independent requests to the configured diagnostic model:
one with max_tokens=10 and one with max_tokens=64. It never retries either
request, never prints or saves the API key, and saves only allowlisted response
metadata under docs/remediation/runs/.

Run from any directory with:
    python3 /path/to/opsfleet-assistant/scripts/openrouter_terminal_diagnostic.py

OPENROUTER_API_KEY may be set in the environment. If it is absent, the script
prompts for the key without echoing it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from getpass import getpass
import json
import math
import os
from pathlib import Path
import re
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


MODEL = "google/gemini-3.8-flash"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
HOST = "openrouter.ai"
TIMEOUT_SECONDS = 30
MAX_TOKENS_CASES = (10, 64)
SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}\Z")
SAFE_MODEL = re.compile(r"[A-Za-z0-9._:/-]{1,160}\Z")
SAFE_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_category_for_http(status: int) -> str:
    if status in (401, 403):
        return "authentication"
    if status == 402:
        return "quota_or_billing"
    if status == 429:
        return "UNKNOWN_429_quota_or_rate_limit"
    if status == 404:
        return "requested_model_or_endpoint_unavailable"
    if status == 408:
        return "network_or_timeout"
    if status >= 500:
        return "other_service_failure"
    return "other_http_failure"


def safe_network_category(exc: BaseException) -> tuple[str, str]:
    """Return a category and safe exception class; never include exception text."""
    candidates: list[BaseException] = [exc]
    seen: set[int] = set()
    while candidates:
        current = candidates.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, URLError) and isinstance(current.reason, BaseException):
            candidates.append(current.reason)
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                candidates.append(linked)

    classes = {type(item).__name__ for item in seen_objects(exc)}
    if "gaierror" in classes:
        return "dns_resolution", "gaierror"
    if "SSLError" in classes:
        return "tls_failure", "SSLError"
    if "TimeoutError" in classes or "socket.timeout" in classes:
        return "network_or_timeout", "TimeoutError"
    if "ConnectionRefusedError" in classes:
        return "connection_refused", "ConnectionRefusedError"
    if "RemoteDisconnected" in classes:
        return "connection_closed", "RemoteDisconnected"
    if "URLError" in classes:
        return "network_or_timeout", "URLError"
    if "HTTPException" in classes:
        return "network_or_timeout", "HTTPException"
    return "UNKNOWN", "unclassified"


def seen_objects(exc: BaseException) -> list[BaseException]:
    items: list[BaseException] = []
    queue = [exc]
    seen: set[int] = set()
    while queue and len(items) < 8:
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        items.append(current)
        if isinstance(current, URLError) and isinstance(current.reason, BaseException):
            queue.append(current.reason)
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                queue.append(linked)
    return items


def dns_check() -> dict[str, Any]:
    started = time.monotonic()
    record: dict[str, Any] = {"host": HOST, "status": "unknown"}
    try:
        addresses = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        record.update(status="resolved", address_count=len(addresses))
    except socket.gaierror as exc:
        code = exc.errno
        if code == socket.EAI_AGAIN:
            category = "temporary_dns_failure"
        elif code == socket.EAI_NONAME:
            category = "name_not_found"
        elif code == socket.EAI_FAIL:
            category = "nonrecoverable_dns_failure"
        else:
            category = "dns_resolution_failure"
        record.update(status="failed", category=category, safe_exception_class="gaierror")
    except Exception as exc:
        category, safe_class = safe_network_category(exc)
        record.update(status="failed", category=category, safe_exception_class=safe_class)
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return record


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def number_or_none(value: Any) -> int | float | None:
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and math.isfinite(value) and value >= 0:
        return value
    return None


def safe_text(value: Any, pattern: re.Pattern[str], *, secret: str = "") -> str | None:
    # Provider metadata can echo credentials while still matching the syntax.
    # Reject the whole value so neither exact nor embedded echoes reach reports.
    if not isinstance(value, str) or (secret and secret in value):
        return None
    return value if pattern.fullmatch(value) else None


def request_case(api_key: str, max_tokens: int, opener: Any) -> dict[str, Any]:
    started_at = utc_now()
    started = time.monotonic()
    record: dict[str, Any] = {
        "started_at_utc": started_at,
        "requested_model": MODEL,
        "max_tokens": max_tokens,
        "success": False,
        "actual_model": None,
        "provider": None,
        "trace_id": None,
        "usage": None,
        "cost": None,
    }
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": max_tokens,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    response_body: bytes | None = None
    response_headers: Any = None
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            record["http_status"] = response.status
            response_headers = response.headers
            response_body = response.read(1_000_000)
    except HTTPError as exc:
        # Status is safe to retain. Do not read, print, or persist error body/headers.
        record["http_status"] = exc.code
        record["category"] = safe_category_for_http(exc.code)
        exc.close()
    except Exception as exc:
        category, safe_class = safe_network_category(exc)
        record["http_status"] = None
        record["category"] = category
        record["safe_transport_class"] = safe_class

    if response_body is not None:
        if response_headers is not None:
            for header in ("x-request-id", "openrouter-request-id"):
                value = response_headers.get(header)
                safe_id = safe_text(value, SAFE_ID, secret=api_key)
                if safe_id:
                    record["trace_id"] = safe_id
                    break
        if record.get("http_status") == 200:
            try:
                payload = json.loads(response_body)
                actual_model = safe_text(payload.get("model"), SAFE_MODEL, secret=api_key)
                provider = safe_text(payload.get("provider"), SAFE_LABEL, secret=api_key)
                if actual_model:
                    record["actual_model"] = actual_model
                if provider:
                    record["provider"] = provider
                if record["trace_id"] is None:
                    record["trace_id"] = safe_text(payload.get("id"), SAFE_ID, secret=api_key)

                choices = payload.get("choices")
                choice = choices[0] if isinstance(choices, list) and choices else {}
                finish = safe_text(choice.get("finish_reason"), SAFE_LABEL, secret=api_key)
                native_finish = safe_text(choice.get("native_finish_reason"), SAFE_LABEL, secret=api_key)
                record["finish_reason"] = finish
                record["native_finish_reason"] = native_finish
                content = choice.get("message", {}).get("content") if isinstance(choice, dict) else None
                record["exact_ok"] = content == "OK"
                record["content_characters"] = len(content) if isinstance(content, str) else 0

                usage = payload.get("usage")
                if isinstance(usage, dict):
                    safe_usage = {
                        key: usage[key]
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if type(usage.get(key)) is int and usage[key] >= 0
                    }
                    cost = number_or_none(usage.get("cost"))
                    record["usage"] = safe_usage or None
                    record["cost"] = cost
                record["success"] = record["exact_ok"] and finish == "stop"
                record["category"] = "usable_response" if record["success"] else (
                    "truncated_response" if finish == "length" else "other_response"
                )
            except Exception:
                record["category"] = "invalid_or_unrecognized_response"
                record["success"] = False
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    record["completed_at_utc"] = utc_now()
    return record


def make_output_dir() -> Path:
    root = Path(__file__).resolve().parents[1]
    runs = root / "docs" / "remediation" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = runs / f"openrouter-terminal-diagnostic-{stamp}"
    output.mkdir(mode=0o700, exist_ok=False)
    return output


def write_outputs(output_dir: Path, results: dict[str, Any]) -> None:
    json_path = output_dir / "results.json"
    json_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(json_path, 0o600)

    dns = results["dns"]
    lines = [
        "# OpenRouter terminal diagnostic",
        "",
        f"Completed: `{results['completed_at_utc']}`",
        f"Requested model: `{MODEL}`",
        f"DNS: **{dns['status']}**" + (f" (`{dns.get('category')}`)" if dns.get("category") else ""),
        "",
        "| Token cap | HTTP status | Category | Actual model | Provider | Finish | Exact `OK` | Latency | Usage / cost |",
        "|---:|---:|---|---|---|---|---|---:|---|",
    ]
    for case in results["requests"]:
        usage = case.get("usage") or "unknown"
        cost = case.get("cost")
        lines.append(
            f"| {case['max_tokens']} | {case.get('http_status', 'unknown')} | "
            f"{case.get('category', 'unknown')} | {case.get('actual_model') or 'unknown'} | "
            f"{case.get('provider') or 'unknown'} | {case.get('finish_reason') or 'unknown'} | "
            f"{case.get('exact_ok', 'unknown')} | {case.get('elapsed_seconds', 'unknown')}s | "
            f"{usage} / {cost if cost is not None else 'unknown'} |"
        )
    lines += [
        "",
        "The script sent at most two requests with no automatic retries. It did not save the API key, raw response text, raw error body, or raw headers.",
        "",
    ]
    report_path = output_dir / "REPORT.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(report_path, 0o600)


def main() -> int:
    api_key = os.environ.get("OPENROUTER_API_KEY") or getpass("OpenRouter API key (input hidden): ").strip()
    output_dir = make_output_dir()
    dns = dns_check()
    results: dict[str, Any] = {
        "kind": "openrouter_terminal_diagnostic",
        "created_at_utc": utc_now(),
        "completed_at_utc": None,
        "model": MODEL,
        "endpoint_host": HOST,
        "api_key_configured": bool(api_key),
        "proxy_environment_configured": any(
            os.environ.get(name) for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
        ),
        "dns": dns,
        "transport_retries": 0,
        "requests": [],
        "raw_provider_text_headers_and_errors": "not retained",
    }

    if not api_key:
        results["requests"] = [
            {"max_tokens": cap, "category": "credentials_unavailable", "success": False}
            for cap in MAX_TOKENS_CASES
        ]
    else:
        opener = build_opener(NoRedirect())
        for cap in MAX_TOKENS_CASES:
            results["requests"].append(request_case(api_key, cap, opener))
        api_key = ""  # Drop the reference before writing any output.

    results["completed_at_utc"] = utc_now()
    write_outputs(output_dir, results)
    print(f"Diagnostic complete. Sanitized results saved to: {output_dir}")
    print(f"DNS: {dns['status']}" + (f" ({dns['category']})" if dns.get("category") else ""))
    for case in results["requests"]:
        print(
            f"max_tokens={case['max_tokens']}: {case.get('category', 'unknown')}; "
            f"HTTP {case.get('http_status', 'unknown')}; "
            f"exact_OK={case.get('exact_ok', 'unknown')}"
        )
    # The 10-token case is intentionally expected to truncate; the 64-token
    # case is the functional check used for the process exit status.
    usable_case = next((case for case in results["requests"] if case.get("max_tokens") == 64), None)
    return 0 if usable_case and usable_case.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
