"""Reusable LangGraph outline and application composition root."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import uuid4
from typing import cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .bigquery_adapter import BigQueryTurn
from .config import ROOT, Settings
from .report_text import render_v1 as _render
from .deletion import parse_deletion
from .privacy import FIELD_POLICY_VERSION
from .identity import resolve_demo_scope
from .model_gateway import GeminiGateway, ModelBudget
from .store import SQLiteStore
from .golden import retrieve
from .telemetry import TurnTrace

from .turn_types import TurnState, TurnResult, DeletionTurnResult
from .workflow_helpers import validate_draft as _validate_draft, scope_digest as _scope_digest
from .workflow_contracts import SCHEMA_HELP, SAFE_ERROR_CODES
from .turn_context import TurnContext, SystemClock, GatewayFactory, WarehouseFactory, Clock, TurnVersions
from .turn_session import TurnSession
from .deletion_service import DeletionService

class AssistantWorkflow:
    def __init__(self, settings: Settings, store: SQLiteStore,
                 *, gateway_factory: GatewayFactory = GeminiGateway, turn_factory: WarehouseFactory = BigQueryTurn,
                 today: date | None = None, analytical_timezone: str = "UTC", clock: Clock | None = None):
        if analytical_timezone != "UTC":
            raise ValueError("unsupported_analytical_timezone")
        self.analytical_timezone = analytical_timezone
        self.settings, self.store = settings, store
        self.gateway_factory, self.turn_factory = gateway_factory, turn_factory
        self.today = today
        self.clock = clock or SystemClock(lambda: self.store._now())
        self.graph = self._build_graph()

    def run(self, owner: str, question: str, *, conversation_id: str | None = None,
            turn_id: str | None = None) -> TurnResult:
        supplied_conversation = conversation_id
        conversation_id = conversation_id or str(uuid4())
        turn_id = turn_id or str(uuid4())
        trace = TurnTrace(conversation_id, turn_id)
        trace.emit("turn_start", status="start")
        budget = ModelBudget(clock=self.clock.monotonic)
        budget.telemetry = trace
        context = TurnContext(owner, conversation_id, turn_id, trace, self.clock,
            self.today or self.clock.now().date(), self.analytical_timezone,
            budget, budget.started + budget.seconds, TurnVersions(field_policy=FIELD_POLICY_VERSION))
        session = TurnSession(settings=self.settings, store=self.store, context=context,
            gateway_factory=self.gateway_factory, turn_factory=self.turn_factory,
            graph=self.graph, supplied_conversation=supplied_conversation,
            identity_root=ROOT, resolve_scope=resolve_demo_scope, retrieve_method=retrieve)
        command = parse_deletion(question)
        result = DeletionService().run(command, session) if command else session.run(question)
        category = result.error_category or ("success" if result.status == "ok" else result.status)
        trace.finish(result.status, category=category, requests=result.model_requests,
                     executions=result.query_executions, billed_bytes=result.billed_bytes)
        return replace(result, trace_id=trace.trace_id)

    def _build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(TurnState)
        graph.add_node("privacy_screen", self.screen)
        graph.add_node("intent_metric_resolution", self.resolve)
        graph.add_node("structured_planning", self.plan)
        graph.add_node("authorization", self.authorize)
        graph.add_node("step3_execution", self.execute)
        graph.add_node("evidence_checks", self.evidence_check)
        graph.add_node("explanation_drafting", self.draft)
        graph.add_node("claim_validation", self.claim_validation)
        graph.add_node("display_save", self.display_save)
        graph.add_edge(START, "privacy_screen")
        graph.add_edge("privacy_screen", "intent_metric_resolution")
        graph.add_conditional_edges("intent_metric_resolution", self.route_interpretation,
            {"help": "display_save", "analyze": "structured_planning", "stop": END})
        chain = ["structured_planning", "authorization", "step3_execution", "evidence_checks",
                 "explanation_drafting", "claim_validation", "display_save"]
        for source, target in zip(chain, chain[1:]):
            graph.add_edge(source, target)
        graph.add_edge("display_save", END)
        return graph.compile()

    @staticmethod
    def screen(state: TurnState) -> TurnState:
        turn = cast(TurnSession, state["runtime"])
        return turn.screen(state)

    @staticmethod
    def resolve(state: TurnState) -> TurnState:
        turn = cast(TurnSession, state["runtime"])
        return turn.resolver.resolve(state, turn)

    @staticmethod
    def plan(state: TurnState) -> TurnState:
        turn = cast(TurnSession, state["runtime"])
        return turn.executor.plan(state, turn)

    @staticmethod
    def authorize(state: TurnState) -> TurnState:
        turn = cast(TurnSession, state["runtime"])
        return turn.executor.authorize(state, turn)

    @staticmethod
    def execute(state: TurnState) -> TurnState:
        turn = cast(TurnSession, state["runtime"])
        return turn.executor.execute(state, turn)

    @staticmethod
    def evidence_check(state: TurnState) -> TurnState:
        turn = cast(TurnSession, state["runtime"])
        return turn.executor.evidence_check(state, turn)

    @staticmethod
    def draft(state: TurnState) -> TurnState:
        turn = cast(TurnSession, state["runtime"])
        return turn.draft(state)

    @staticmethod
    def claim_validation(state: TurnState) -> TurnState:
        turn = cast(TurnSession, state["runtime"])
        return turn.claim_validation(state)

    @staticmethod
    def display_save(state: TurnState) -> TurnState:
        turn = cast(TurnSession, state["runtime"])
        return turn.reports.persist_turn(state, turn)

    @staticmethod
    def route_interpretation(state: TurnState) -> str:
        if state.get("content") == SCHEMA_HELP or state.get("contextual_only"):
            return "help"
        if state["interpretation"].status != "ready":
            return "stop"
        return "analyze"
