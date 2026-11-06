"""Bounded, read-only BigQuery execution for trusted compiled analyses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from typing import Any, Mapping, Sequence

from google.api_core import exceptions as google_errors
import google.auth
from google.auth import exceptions as auth_errors
from google.auth.transport.requests import AuthorizedSession, Request
from google.cloud import bigquery
import requests

from .compiler import BoundParameter, CompiledQuery, DATASET, compile_plan, is_trusted_compiled
from .config import Settings
from .contracts import AnalysisPlan, Operation
from .evidence import Coverage, Evidence, release_evidence
from .identity import Catalog, PolicyError, resolve_demo_scope


_CATALOG_LIMIT = 100_000
_CATALOG_SQL = (
    f"SELECT id, brand FROM `{DATASET}.products` "
    "WHERE id IS NOT NULL AND brand IS NOT NULL ORDER BY id LIMIT 100001"
)
_COVERAGE_SQL = f"""
SELECT COUNT(*) AS source_rows,
       COUNTIF(oi.created_at >= @start_ts AND oi.created_at < @end_ts) AS period_rows,
       MAX(oi.created_at) AS latest_item_at
FROM `{DATASET}.order_items` AS oi
JOIN `{DATASET}.products` AS p ON oi.product_id = p.id
WHERE p.brand IN UNNEST(@brands)
  AND oi.status IN UNNEST(@statuses)
  AND oi.sale_price IS NOT NULL
"""
_COVERAGE_CHURN_SQL = f"""
SELECT COUNT(*) AS source_rows,
       COUNTIF(oi.created_at < @cutoff_ts) AS period_rows,
       MAX(oi.created_at) AS latest_item_at
FROM `{DATASET}.order_items` AS oi
JOIN `{DATASET}.products` AS p ON oi.product_id = p.id
WHERE p.brand IN UNNEST(@brands)
  AND oi.status IN UNNEST(@statuses)
  AND oi.sale_price IS NOT NULL
"""
_CUSTOMER_CHECK_SQL = f"""
SELECT COUNT(DISTINCT o.user_id) AS matched_customers
FROM `{DATASET}.order_items` AS oi
JOIN `{DATASET}.orders` AS o ON oi.order_id = o.order_id
JOIN `{DATASET}.products` AS p ON oi.product_id = p.id
WHERE p.brand IN UNNEST(@brands)
  AND oi.status IN UNNEST(@statuses)
  AND oi.sale_price IS NOT NULL
  AND oi.created_at >= @start_ts AND oi.created_at < @end_ts
  AND o.user_id IN UNNEST(@candidate_ids)
"""
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class ExecutionError(Exception):
    """Safe category and class only; no provider text or SQL escapes."""

    def __init__(self, category: str, kind: str = "policy"):
        self.category = category
        self.kind = kind
        super().__init__(category)


@dataclass(frozen=True)
class QueryLimits:
    per_job_bytes: int = 512 * 1024 * 1024
    per_turn_bytes: int = 1024 * 1024 * 1024
    max_executions: int = 8
    job_seconds: float = 30.0
    turn_seconds: float = 120.0

    def __post_init__(self) -> None:
        if min(self.per_job_bytes, self.per_turn_bytes, self.max_executions) <= 0 or min(self.job_seconds, self.turn_seconds) <= 0:
            raise ValueError("invalid_query_limits")


@dataclass(frozen=True)
class _Completed:
    rows: tuple[Mapping[str, Any], ...]
    schema: Sequence[Any]
    job_id: str
    estimated_bytes: int
    billed_bytes: int
    as_of: datetime


def _parameters(parameters: Sequence[BoundParameter]) -> list[Any]:
    translated: list[Any] = []
    for parameter in parameters:
        if parameter.type_name.startswith("ARRAY<") and parameter.type_name.endswith(">"):
            element = parameter.type_name[6:-1]
            if element not in {"STRING", "INT64"} or not isinstance(parameter.value, (tuple, list)):
                raise ExecutionError("invalid_parameter")
            translated.append(bigquery.ArrayQueryParameter(parameter.name, element, list(parameter.value)))
        elif parameter.type_name in {"TIMESTAMP", "INT64", "STRING"}:
            translated.append(bigquery.ScalarQueryParameter(parameter.name, parameter.type_name, parameter.value))
        else:
            raise ExecutionError("invalid_parameter")
    if len({item.name for item in translated}) != len(translated):
        raise ExecutionError("invalid_parameter")
    return translated


def _classify(exc: Exception) -> ExecutionError:
    if isinstance(exc, ExecutionError):
        return exc
    if isinstance(exc, PolicyError):
        return ExecutionError(exc.code)
    if isinstance(exc, google_errors.Forbidden) or isinstance(exc, google_errors.Unauthorized):
        return ExecutionError("permission_denied", "provider")
    if isinstance(exc, (google_errors.TooManyRequests, google_errors.ServiceUnavailable,
                        google_errors.InternalServerError, google_errors.GatewayTimeout,
                        google_errors.DeadlineExceeded, auth_errors.TransportError,
                        requests.exceptions.RequestException, TimeoutError, ConnectionError)):
        return ExecutionError("provider_transient", "transient")
    if isinstance(exc, google_errors.BadRequest):
        detail = str(exc).casefold()  # Classification only; never retained or displayed.
        if "syntax error" in detail or "parse error" in detail:
            return ExecutionError("query_syntax_error", "provider")
        if "unrecognized name" in detail or "no matching signature" in detail or "type mismatch" in detail:
            return ExecutionError("query_schema_error", "provider")
        return ExecutionError("query_provider_rejected", "provider")
    return ExecutionError("provider_failure", "provider")


def _client_from_adc(settings: Settings) -> Any:
    """Use local ADC with query, auth, and HTTP transport retries disabled."""
    credentials, _ = google.auth.default()
    auth_transport = requests.Session()
    auth_transport.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    session = AuthorizedSession(
        credentials, auth_request=Request(auth_transport), max_refresh_attempts=0,
    )
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    return bigquery.Client(project=settings.project, location=settings.location,
                           credentials=credentials, _http=session)


class BigQueryTurn:
    """One active-turn budget. Create a new instance for a new user turn."""

    def __init__(self, settings: Settings, *, client: Any | None = None,
                 limits: QueryLimits = QueryLimits(), clock: Any = time.monotonic):
        if not settings.project:
            raise ExecutionError("missing_project")
        self.settings = settings
        if client is None:
            try:
                client = _client_from_adc(settings)
            except Exception as exc:
                raise _classify(exc) from None
        self.client = client
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self.executions = 0
        self.estimated_bytes = 0
        self.billed_bytes = 0
        self.billing_unknown = False
        self.dry_runs = 0
        self.retries = 0
        self.telemetry = None
        self._catalog: Catalog | None = None

    def _remaining_seconds(self) -> float:
        remaining = self.limits.turn_seconds - (self._clock() - self._started)
        if remaining <= 0:
            raise ExecutionError("turn_timeout")
        return min(self.limits.job_seconds, remaining)

    def _run(self, sql: str, parameters: Sequence[BoundParameter], *, max_rows: int, purpose: str) -> _Completed:
        for attempt in range(2):
            executions_before = self.executions
            try:
                return self._run_once(sql, parameters, max_rows=max_rows, purpose=purpose)
            except ExecutionError as exc:
                # A submitted job may still run or bill after failure. Retry only a failed dry run.
                if (exc.category != "provider_transient" or self.executions != executions_before
                        or attempt or self.retries >= 2):
                    raise
                self.retries += 1
                if self.telemetry:
                    self.telemetry.emit("recovery", decision="retry", category="transient", purpose=purpose)
                delay = min(0.2, max(0.0, self._remaining_seconds() - 0.01))
                if delay <= 0:
                    raise ExecutionError("turn_timeout") from None
                time.sleep(delay)
        raise ExecutionError("provider_failure")

    def _run_once(self, sql: str, parameters: Sequence[BoundParameter], *, max_rows: int, purpose: str) -> _Completed:
        if self.executions >= self.limits.max_executions:
            raise ExecutionError("query_count_limit")
        if self.dry_runs >= self.limits.max_executions + 4:
            raise ExecutionError("dry_run_limit")
        remaining_bytes = self.limits.per_turn_bytes - self.billed_bytes
        if remaining_bytes <= 0:
            raise ExecutionError("turn_byte_limit")
        cap = min(self.limits.per_job_bytes, remaining_bytes)
        translated = _parameters(parameters)
        timeout = self._remaining_seconds()
        dry_config = bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=translated,
        )
        self.dry_runs += 1
        try:
            dry_job = self.client.query(sql, job_config=dry_config, location=self.settings.location,
                                        timeout=timeout, retry=None, job_retry=None)
            estimate = int(dry_job.total_bytes_processed or 0)
        except Exception as exc:
            if self.telemetry:
                self.telemetry.emit("bq_dry_run", status="error", purpose=purpose, category="provider")
            raise _classify(exc) from None
        self.estimated_bytes += estimate
        if self.telemetry:
            self.telemetry.emit("bq_dry_run", status="ok", purpose=purpose, estimated_bytes=estimate)
        if estimate > cap:
            raise ExecutionError("job_byte_limit" if estimate > self.limits.per_job_bytes else "turn_byte_limit")
        timeout = self._remaining_seconds()
        run_config = bigquery.QueryJobConfig(
            dry_run=False, use_query_cache=False, query_parameters=translated,
            maximum_bytes_billed=cap, job_timeout_ms=max(1, int(timeout * 1000)),
            labels={"purpose": f"opsfleet_{purpose}"},
        )
        job = None
        billed_recorded = False
        job_deadline = self._clock() + timeout
        self.executions += 1
        try:
            job = self.client.query(sql, job_config=run_config, location=self.settings.location,
                                    timeout=timeout, retry=None, job_retry=None)
            result_timeout = min(self._remaining_seconds(), job_deadline - self._clock())
            if result_timeout <= 0:
                raise TimeoutError("job_deadline")
            result = job.result(timeout=result_timeout, max_results=max_rows + 1,
                                retry=None, job_retry=None)
            rows = tuple(dict(row.items()) for row in result)
            billed = job.total_bytes_billed
            if billed is None:
                raise ExecutionError("billing_unknown", "provider")
            billed = int(billed)
            self.billed_bytes += billed
            billed_recorded = True
            if billed > cap or len(rows) > max_rows:
                raise ExecutionError("result_or_billing_limit")
            job_id = str(job.job_id or "")
            if not _JOB_ID.fullmatch(job_id):
                raise ExecutionError("invalid_job_metadata", "provider")
            as_of = job.ended or datetime.now(timezone.utc)
            schema = job.schema or result.schema
            if self.telemetry:
                self.telemetry.emit("bq_job", status="ok", purpose=purpose, job_id=job_id,
                                    billed_bytes=billed, estimated_bytes=estimate)
            return _Completed(rows, schema, job_id, estimate, billed, as_of)
        except Exception as exc:
            if self.telemetry:
                known_billing = job is not None and job.total_bytes_billed is not None
                self.telemetry.emit("bq_job", status="error", purpose=purpose,
                                    job_id=str(getattr(job, "job_id", "")) if job is not None else "unknown",
                                    category="provider", usage="known" if known_billing else "unknown",
                                    **({"billed_bytes": max(0, int(job.total_bytes_billed))} if known_billing else {}))
            if job is not None:
                try:
                    # Cancellation is a request, not proof that billing or execution stopped.
                    job.cancel(retry=None, timeout=1.0)
                except Exception:
                    pass
                if not billed_recorded:
                    if job.total_bytes_billed is not None:
                        self.billed_bytes += max(0, int(job.total_bytes_billed))
                    else:
                        self.billing_unknown = True
                        self.billed_bytes += cap
            elif not billed_recorded:
                self.billing_unknown = True
                self.billed_bytes += cap
            if isinstance(exc, (TimeoutError, google_errors.DeadlineExceeded)) or self._clock() - self._started >= self.limits.turn_seconds:
                raise ExecutionError("timeout_cancellation_requested", "transient") from None
            raise _classify(exc) from None

    def refresh_catalog(self) -> Catalog:
        """One explicitly charged metadata refresh for a compatible schema failure."""
        self._catalog = None
        return self.load_catalog()

    def load_catalog(self) -> Catalog:
        if self._catalog is None:
            completed = self._run(_CATALOG_SQL, (), max_rows=_CATALOG_LIMIT + 1, purpose="catalog")
            if tuple(field.name for field in completed.schema) != ("id", "brand") or len(completed.rows) > _CATALOG_LIMIT:
                raise ExecutionError("invalid_catalog")
            try:
                catalog = Catalog.from_products(completed.rows)
            except PolicyError as exc:
                raise _classify(exc) from None
            if len(catalog.product_brands) != len(completed.rows):
                raise ExecutionError("invalid_catalog")
            self._catalog = catalog
        return self._catalog

    def _check_compiled(self, query: CompiledQuery, catalog: Catalog) -> None:
        if not is_trusted_compiled(query):
            raise ExecutionError("untrusted_compiled_query")
        try:
            scope = resolve_demo_scope(query.scope_user_id, catalog)
        except PolicyError as exc:
            raise _classify(exc) from None
        if (scope.authorization_version != query.scope_version or scope.role.value != query.scope_role
                or not set(query.effective_brands).issubset(scope.brands)
                or query.parameter("brands").value != query.effective_brands):
            raise ExecutionError("unauthorized_scope")
        if query.requested_product_ids is not None and any(
            catalog.product_brands.get(product_id) not in query.effective_brands
            for product_id in query.requested_product_ids
        ):
            raise ExecutionError("product_not_available")

    def _coverage(self, query: CompiledQuery) -> Coverage:
        names = {"brands", "statuses", "cutoff_ts"} if query.operation.value == "churn_snapshot" else {"brands", "statuses", "start_ts", "end_ts"}
        if query.requested_product_ids is not None:
            names.add("product_ids")
        params = tuple(parameter for parameter in query.parameters if parameter.name in names)
        sql = _COVERAGE_CHURN_SQL if query.operation.value == "churn_snapshot" else _COVERAGE_SQL
        if query.requested_product_ids is not None:
            sql += "  AND oi.product_id IN UNNEST(@product_ids)\n"
        completed = self._run(
            sql, params, max_rows=1, purpose="coverage",
        )
        if tuple(field.name for field in completed.schema) != ("source_rows", "period_rows", "latest_item_at") or len(completed.rows) != 1:
            raise ExecutionError("coverage_invalid")
        row = completed.rows[0]
        source_rows, period_rows, latest = row["source_rows"], row["period_rows"], row["latest_item_at"]
        if type(source_rows) is not int or type(period_rows) is not int or source_rows < period_rows or period_rows < 0:
            raise ExecutionError("coverage_invalid")
        if latest is not None and (not isinstance(latest, datetime) or latest.tzinfo is None):
            raise ExecutionError("coverage_invalid")
        demographic = query.operation == Operation.DEMOGRAPHIC_COMPARISON
        return Coverage(
            status="checked",
            source_rows=None if demographic else source_rows,
            period_rows=None if demographic else period_rows,
            latest_item_at_utc=latest.astimezone(timezone.utc).isoformat() if latest else None,
            note=("Scoped item counts withheld for demographic privacy; user profile freshness and ingestion completeness are unknown."
                  if demographic else
                  "Scoped qualifying item coverage; user profile freshness is unknown. Latest item date does not establish ingestion completeness."),
            job_id=completed.job_id, estimated_bytes=completed.estimated_bytes,
            billed_bytes=completed.billed_bytes,
        )

    def _check_customer_ids(self, query: CompiledQuery, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        ids = tuple(row.get("user_id") for row in rows)
        if any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids):
            raise ExecutionError("result_type_violation")
        names = {"brands", "statuses", "start_ts", "end_ts"}
        params = tuple(parameter for parameter in query.parameters if parameter.name in names)
        params += (BoundParameter("candidate_ids", "ARRAY<INT64>", ids),)
        sql = _CUSTOMER_CHECK_SQL
        if query.requested_product_ids is not None:
            params += (query.parameter("product_ids"),)
            sql += "  AND oi.product_id IN UNNEST(@product_ids)\n"
        completed = self._run(sql, params, max_rows=1, purpose="customer_check")
        if (tuple(field.name for field in completed.schema) != ("matched_customers",)
                or len(completed.rows) != 1
                or completed.rows[0].get("matched_customers") != len(ids)):
            raise ExecutionError("result_scope_violation")

    def execute_compiled(self, query: CompiledQuery) -> Evidence:
        catalog = self.load_catalog()
        self._check_compiled(query, catalog)
        completed = self._run(query.sql, query.parameters,
                              max_rows=query.result_contract.max_rows, purpose="analysis")
        if query.operation == Operation.RANK_CUSTOMERS:
            self._check_customer_ids(query, completed.rows)
        coverage = self._coverage(query)
        try:
            return release_evidence(
                query, completed.rows, completed.schema,
                job_id=completed.job_id, estimated_bytes=completed.estimated_bytes,
                billed_bytes=completed.billed_bytes, query_as_of=completed.as_of,
                coverage=coverage,
            )
        except PolicyError as exc:
            raise _classify(exc) from None

    def run(self, plan: AnalysisPlan, user_id: str) -> Evidence:
        if not isinstance(plan, AnalysisPlan) or not isinstance(user_id, str):
            raise ExecutionError("invalid_request")
        catalog = self.load_catalog()
        try:
            scope = resolve_demo_scope(user_id, catalog)
            query = compile_plan(plan, scope, catalog)
        except PolicyError as exc:
            raise _classify(exc) from None
        return self.execute_compiled(query)
