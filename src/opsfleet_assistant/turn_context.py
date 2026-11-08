"""Trusted, invocation-owned dependencies. Model schemas never contain these types."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import time
from typing import Callable, Protocol

from .bigquery_adapter import QueryLimits
from .compiler import CompiledQuery
from .config import Settings
from .contracts import AnalysisPlan, BrandScope, InterpretationResult, RequestSpec, ReportDraft
from .evidence import Evidence
from .identity import Catalog
from .model_gateway import ModelBudget
from .privacy import FIELD_POLICY_VERSION
from .source_schema import COMPILER_VERSION, PUBLIC_BINDING
from . import source_schema
from .metrics import METRIC_VERSION
from .playbooks import REGISTRY
from .telemetry import TurnTrace


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemClock:
    utc_now: Callable[[], datetime]
    monotonic: Callable[[], float] = time.monotonic

    def now(self) -> datetime:
        return self.utc_now()


class ModelGateway(Protocol):
    def interpret(self, question: str, *, previous_plan: AnalysisPlan | None,
                  reference_date: str, analytical_timezone: str, feedback: str = "",
                  conversation_context: dict | None = None,
                  pending_request: RequestSpec | None = None) -> InterpretationResult: ...
    def report(self, evidence: tuple[dict, ...], *, report_requested: bool) -> ReportDraft: ...


class WarehouseTurn(Protocol):
    limits: QueryLimits
    executions: int
    billed_bytes: int
    telemetry: TurnTrace | None

    def load_catalog(self) -> Catalog: ...
    def execute_compiled(self, query: CompiledQuery) -> Evidence: ...


class GatewayFactory(Protocol):
    def __call__(self, settings: Settings, *, budget: ModelBudget) -> ModelGateway: ...


class WarehouseFactory(Protocol):
    def __call__(self, settings: Settings, *, limits: QueryLimits) -> WarehouseTurn: ...


@dataclass(frozen=True)
class TurnVersions:
    field_policy: int = FIELD_POLICY_VERSION
    compiler: int = COMPILER_VERSION
    source_binding: str = PUBLIC_BINDING.version
    approved_source_bindings: tuple[str, ...] = field(default_factory=lambda: tuple(
        binding.version for binding in source_schema.APPROVED_BINDINGS))
    metric: int = METRIC_VERSION
    playbooks: tuple[tuple[str, int], ...] = field(default_factory=lambda: tuple(
        (name, definition.version) for name, definition in sorted(REGISTRY.items())))
    artifact_schemas: tuple[int, ...] = (1, 2)
    rendering: int = 1


@dataclass(frozen=True)
class TurnContext:
    owner: str
    conversation_id: str
    turn_id: str
    trace: TurnTrace
    clock: Clock
    reference_date: date
    analytical_timezone: str
    budget: ModelBudget
    deadline: float
    versions: TurnVersions
    query_limits: QueryLimits = QueryLimits()
    max_repairs: int = 2
    max_dry_runs: int = 12


@dataclass(frozen=True)
class TurnAuthority:
    """Resolved exclusively from the trusted identity/catalog boundary."""
    scope: BrandScope
    catalog: Catalog


@dataclass(frozen=True)
class CompletedAnalysis:
    """Safe provenance from released evidence, retained if a later node fails."""
    job_id: str
    billed_bytes: int
