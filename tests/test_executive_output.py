"""Presentation semantics over released fixtures and isolated CLI/workflow state.

Scripted interpretations test orchestration, not live language understanding.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from io import StringIO
import json
import re

import pytest
from rich.console import Console
from typer.testing import CliRunner

from opsfleet_assistant import cli
from opsfleet_assistant.contracts import (ActionSelection, FindingSelection,
    InterpretationResult as I, ObservationRef, QuerySpec as Q, ReportDraft,
    ReportSpec, RequestSpec as R, TemporalExpression as T, YearExpression as Y)
from opsfleet_assistant.model_gateway import ModelFailure
from opsfleet_assistant.periods import resolve_request
from opsfleet_assistant.presentation import display_payload, display_value, render_result
from opsfleet_assistant.reporting import compose_report, ground_report
from opsfleet_assistant.store import SQLiteStore, StoreFailure
from opsfleet_assistant.workflow import TurnResult
from test_grounded_findings import fixture_evidence
from test_report_completeness import harness, request, quarter, OWNER, NOW, ReportTurn


PRECISION = "123.456789012345678901234567890123456789"


def result_for(evidence=None, *, spec=None, draft=None, report=False):
    evidence = evidence or (fixture_evidence(rows=({"revenue": Decimal(PRECISION)},)),)
    spec = spec or R(mode="report" if report else "analysis", analysis=quarter(2),
        action=T(kind="reference", reference="analysis", unit="quarter", offset=1),
        queries=(Q(operation="aggregate", metric="revenue"),))
    resolved = resolve_request(spec, reference_date=date(2026, 11, 5), analytical_timezone="UTC")
    grounded = ground_report(draft or ReportDraft(recommendations=(
        ActionSelection(kind="review_performance", finding_index=0),)), evidence)
    composition = compose_report(resolved, evidence, grounded) if report else None
    return TurnResult("partial" if composition and composition.completeness == "partial" else "ok",
        "Original plain text remains unchanged [red]literal[/red].", "conversation-fixture", "turn-fixture",
        model_routes=("fixture_route",), model_requests=2, query_executions=3, billed_bytes=101,
        evidence_job_ids=tuple(e.job_id for e in evidence), trace_id="trace-fixture",
        report_id=17 if report else None,
        factual_status=grounded.factual_status,
        factual_findings=tuple(f.model_dump(mode="json") for f in grounded.findings),
        suggestions=tuple({"kind": kind, **s.model_dump(mode="json")} for kind in ("recommendations", "hypotheses")
            for s in getattr(grounded, kind)),
        resolved_periods={role: getattr(resolved, role).model_dump(mode="json") if getattr(resolved, role) else None
            for role in ("analysis", "comparison", "action")},
        report_completeness=composition.completeness if composition else None,
        report_composition=composition.model_dump(mode="json") if composition else None,
        presentation=display_payload(evidence, grounded, resolved))


def shown(result, width=120, verbose=False):
    output = StringIO()
    console = Console(file=output, width=width, color_system=None, force_terminal=False)
    render_result(console, result, verbose=verbose)
    return output.getvalue()


def flat(text):
    return re.sub(r"\s+", " ", text)


def compact(text):
    return re.sub(r"\s+", "", text)


def fake_cli(monkeypatch, results, width=120):
    calls = []
    queue = iter(results)
    class Completed:
        def __init__(self, *args, **kwargs): pass
        def run(self, *args, **kwargs):
            calls.append((args, kwargs))
            return next(queue)
    monkeypatch.setattr(cli, "AssistantWorkflow", Completed)
    monkeypatch.setattr(cli, "console", Console(width=width, color_system=None))
    return calls


def test_default_leads_with_answer_and_retains_material_disclosures():
    result = result_for(report=True)
    output = shown(result)
    assert output.startswith("Observed revenue: 123.46;")
    for value in ("Complete report", "2025-04-01", "2025-07-01", "2025-10-01",
                  "Calvin Klein", "Carhartt", "all products within these brands", "Q3 2025 actions",
                  "not observed outcomes", "2026-11-04T00:00:00+00:00", "not source freshness",
                  "Current item statuses", "Saved report: 17", "Conversation: conversation-fixture", "Trace: trace-fixture"):
        assert value in flat(output)
    assert output.index("123.46") < output.index("limitations and definitions")
    assert "Model routes:" not in output and "fixture_analysis" not in output and "billed bytes" not in output
    assert "$" not in output and PRECISION not in output


def test_verbose_adds_matching_diagnostics_without_removing_caveats():
    result = result_for()
    output = shown(result, verbose=True)
    for value in ("Trace: trace-fixture", "Turn: turn-fixture", "fixture_route", "requests: 2",
                  "BigQuery executions: 3", "billed bytes: 101", "fixture_analysis", "fixture_coverage",
                  "detailed metadata", "field_policy_version", "Current item statuses", "Original evidence as of"):
        assert value in flat(output)
    assert PRECISION not in output  # values stay rounded, full evidence lives in JSON


@pytest.mark.parametrize("column,value,expected", [
    ("revenue", "1.005", "1.00"), ("revenue", "1.015", "1.02"),
    ("average_order_value", PRECISION, "123.46"), ("customer_spend", "0", "0.00"),
    ("order_count", 1001, "1001"), ("customers", 0, "0"), ("item_count", "19", "19"),
    ("order_count", "1.5", "unavailable"),
    ("churn_rate", "0.125", "12.50%"), ("churn_rate", 0, "0.00%"),
    ("average_order_value", None, "unavailable"), ("churn_rate", None, "unavailable"),
    ("revenue", "NaN", "unavailable"), ("revenue", "Infinity", "unavailable"),
    ("product_id", 12, "12"), ("brand", "[bold]Brand[/bold]", "[bold]Brand[/bold]"),
])
def test_display_rounding_counts_rates_and_missing_values(column, value, expected):
    assert display_value(value, column) == expected


def test_count_tables_do_not_use_decimal_places_or_unrelated_money_definitions():
    item = fixture_evidence(metric="order_count", rows=({"order_count": 1234},))
    output = shown(result_for((item,)))
    assert "1234.00" not in output and "1234" in output
    assert "distinct orders" in output and "Revenue sums" not in output
    assert "accounting recognition" not in output and "Churn:" not in output


def test_rate_table_and_percentage_point_finding_are_distinct():
    def churn(inactive, cutoff):
        return fixture_evidence(operation="churn_snapshot", metric="churn_rate", cutoff=cutoff,
            rows=({"eligible_customers": 8, "inactive_customers": inactive,
                   "churn_rate": Decimal(inactive) / 8},))
    evidence = (churn(5, "2025-07-01"), churn(4, "2025-04-01"))
    selection = FindingSelection(operation="percentage_point_change",
        current=ObservationRef(evidence_index=0, row_index=0, metric="churn_rate"),
        baseline=ObservationRef(evidence_index=1, row_index=0, metric="churn_rate"))
    result = result_for(evidence, draft=ReportDraft(findings=(selection,), headline_index=0))
    output = flat(shown(result))
    assert "62.50%" in output and "50.00%" in output and "12.50 percentage points" in output
    assert "90-day inactivity" in output and "inactive / eligible purchasers" in output
    assert result.factual_findings[0]["value"] == "12.500"


@pytest.mark.parametrize("baseline,expected", [("100", "23.46%"), ("0", "Percentage change unavailable")])
def test_percentage_and_zero_baseline_semantics(baseline, expected):
    current = fixture_evidence(rows=({"revenue": Decimal(PRECISION)},))
    prior = fixture_evidence(start="2025-01-01", end="2025-04-01", rows=({"revenue": Decimal(baseline)},))
    selection = FindingSelection(operation="percentage_change",
        current=ObservationRef(evidence_index=0, row_index=0, metric="revenue"),
        baseline=ObservationRef(evidence_index=1, row_index=0, metric="revenue"))
    result = result_for((current, prior), draft=ReportDraft(findings=(selection,), headline_index=0))
    output = flat(shown(result))
    assert expected in output and "percentage points" not in output
    if baseline == "0":
        assert result.factual_findings[0]["value"] is None
        assert "baseline is zero, null or missing" in output


def test_undefined_churn_does_not_become_zero_percent():
    evidence = fixture_evidence(operation="churn_snapshot", metric="churn_rate", cutoff="2025-07-01",
        rows=({"eligible_customers": 0, "inactive_customers": 0, "churn_rate": None},))
    output = flat(shown(result_for((evidence,), draft=ReportDraft())))
    assert "unavailable" in output and "undefined rate" in output and "0.00%" not in output
    assert "Evidence incomplete" in output


@pytest.mark.parametrize("width", [24, 40, 80, 140])
def test_literal_markup_long_labels_and_all_rows_survive_width(width):
    label = "[red]Literal[/red][link=https://example.invalid]Label[/link]" + "Long" * 8
    item = fixture_evidence(dimension="brand", rows=({"brand": "Calvin Klein", "revenue": Decimal("1.235")},))
    # Display-only stress input; authorization itself is exercised in full workflow tests.
    item = item.model_copy(update={"rows": ({"brand": label, "revenue": "1.235"},),
        "execution_scope": item.execution_scope.model_copy(update={"brands": (label,)})})
    result = result_for((item,))
    output = shown(result, width=width)
    assert compact(label) in compact(output) and "1.24" in output
    assert "\x1b]8" not in output and "\x1b[" not in output
    assert all(len(line) <= width for line in output.splitlines())


@pytest.mark.parametrize("status", ["needs_clarification", "clarification", "unsupported", "error", "deleted"])
def test_nonanalytical_states_keep_literal_text_and_recovery(status):
    result = TurnResult(status, "[red]literal[/red] [link=https://example.invalid]no link[/link]",
        "conversation", "turn", trace_id="trace", error_category="provider" if status == "error" else None)
    output = flat(shown(result))
    assert result.text in output and "Conversation: conversation" in output and "Trace: trace" in output
    if status == "error":
        assert "Check provider/warehouse connectivity" in output


@pytest.mark.parametrize("command", ["ask", "chat"])
@pytest.mark.parametrize("verbose", [False, True])
def test_cli_default_and_verbose_use_same_validated_view(tmp_path, monkeypatch, command, verbose):
    result = result_for(report=True)
    fake_cli(monkeypatch, [result])
    arguments = [command, "--user", OWNER, "--db", str(tmp_path / "cli.sqlite")]
    if command == "ask": arguments.insert(1, "Fixture analysis")
    if verbose: arguments.append("--verbose")
    output = CliRunner().invoke(cli.app, arguments, input="Fixture analysis\n/exit\n" if command == "chat" else None)
    assert output.exit_code == 0, output.exception
    assert "123.46" in output.stdout and "Complete report" in output.stdout
    assert ("Model routes:" in output.stdout) is verbose


@pytest.mark.parametrize("command", ["ask", "chat"])
def test_json_precision_and_existing_fields_independent_of_width_and_verbose(tmp_path, monkeypatch, command):
    result = result_for()
    observed = []
    for width, verbose in ((24, False), (140, True)):
        fake_cli(monkeypatch, [result], width)
        args = [command, "--user", OWNER, "--db", str(tmp_path / "json.sqlite"), "--json"]
        if command == "ask": args.insert(1, "Fixture")
        if verbose: args.append("--verbose")
        run = CliRunner().invoke(cli.app, args, input="Fixture\n/exit\n" if command == "chat" else None)
        assert run.exit_code == 0, run.exception
        observed.append(json.loads(run.stdout))
    assert observed[0] == observed[1] == json.loads(json.dumps(result.__dict__))
    assert observed[0]["factual_findings"][0]["current_value"] == PRECISION
    assert observed[0]["presentation"]["evidence"][0]["rows"][0]["revenue"] == PRECISION
    assert observed[0]["trace_id"] == "trace-fixture" and observed[0]["text"] == result.text
    assert observed[0]["presentation"]["schema_version"] == 1


def test_ask_error_exits_one_and_chat_recovers_next_turn(tmp_path, monkeypatch):
    error = TurnResult("error", "Unable to complete this request: backup_transient.", "c-error", "t-error", error_category="provider")
    good = result_for()
    fake_cli(monkeypatch, [error])
    run = CliRunner().invoke(cli.app, ["ask", "Fixture", "--user", OWNER, "--db", str(tmp_path / "ask.sqlite"), "--json"])
    assert run.exit_code == 1 and json.loads(run.stdout)["status"] == "error"
    calls = fake_cli(monkeypatch, [error, good])
    run = CliRunner().invoke(cli.app, ["chat", "--user", OWNER, "--db", str(tmp_path / "chat.sqlite"), "--verbose"], input="First\nRetry\n/exit\n")
    assert run.exit_code == 0 and "Request failed" in run.stdout and "123.46" in run.stdout
    assert calls[1][1]["conversation_id"] == "c-error"


@pytest.mark.parametrize("command", ["ask", "chat"])
def test_store_errors_are_safe_and_preserve_exit_behavior(tmp_path, monkeypatch, command):
    def unavailable(*args, **kwargs):
        raise StoreFailure("store_unavailable")
    monkeypatch.setattr(cli, "SQLiteStore", unavailable)
    args = [command, "--user", OWNER, "--db", str(tmp_path / "unavailable.sqlite")]
    if command == "ask": args.insert(1, "Fixture")
    run = CliRunner().invoke(cli.app, args)
    assert run.exit_code == 1 and run.stdout.strip() == "Unable to open the local state store."


@pytest.mark.parametrize("outcome", ["complete", "partial", "refused", "fallback", "empty"])
def test_actual_workflow_views_and_replay_preserve_saved_artifact(harness, outcome):
    script, selections, store, flow, path = harness
    if outcome == "partial": selections[0] = ReportDraft()
    if outcome == "refused": selections[0] = ReportDraft(status="refused")
    if outcome == "fallback": selections[0] = ModelFailure("direct_failure")
    if outcome == "empty": ReportTurn.state = "empty"
    script["Display report"] = I(status="ready", request=request(action=T(kind="reference", reference="analysis", unit="quarter", offset=1)))
    first = flow().run(OWNER, "Display report", conversation_id="view", turn_id="fixed")
    assert first.status == ("ok" if outcome == "complete" else "partial")
    assert first.presentation is not None
    before = tuple(store.db.execute("SELECT * FROM reports WHERE id=?", (first.report_id,)).fetchone())
    output = flat(shown(first, width=50))
    assert ("Complete report" if outcome == "complete" else "Partial report") in output
    assert "Original evidence as of:" in output and "product IDs" in output
    if outcome in {"refused", "fallback"}:
        assert f"selection: {'refused' if outcome == 'refused' else 'fallback'}" in output
        assert "No model-selected actions" in output
    if outcome != "complete":
        assert "Missing requested content or support" in output and "Recovery:" in output
    if outcome == "empty":
        assert "aggregate zero is not an observed result" in output
    store.close()
    reopened = SQLiteStore(path, clock=lambda: NOW)
    try:
        reopened.db.execute("UPDATE conversations SET continuity_json='' WHERE owner=?", (OWNER,))
        replay = flow(reopened).run(OWNER, "Display report", conversation_id="view", turn_id="fixed")
        assert replay.reused_report and replay.model_requests == 0 and replay.query_executions == 1
        assert replay.text == first.text and replay.factual_findings == first.factual_findings
        assert replay.report_composition == first.report_composition and replay.resolved_periods == first.resolved_periods
        assert replay.presentation["evidence"] == first.presentation["evidence"]
        assert tuple(reopened.db.execute("SELECT * FROM reports WHERE id=?", (first.report_id,)).fetchone()) == before
        output = flat(shown(replay))
        assert "no model call was made" in output and "Original evidence as of:" in output
    finally:
        reopened.close()


def test_actual_context_views_retain_original_asof_roles_and_why_caveats(harness):
    script, _, _, flow, _ = harness
    script["Initial"] = I(status="ready", request=request())
    first = flow().run(OWNER, "Initial", conversation_id="context-view")
    for goal in ("explain", "why", "summary", "save"):
        script[goal] = I(status="ready", context_action="continue", request=R(intent=goal))
        result = flow().run(OWNER, goal, conversation_id="context-view")
        assert result.model_requests == result.query_executions == 1
        assert result.presentation["evidence"] == first.presentation["evidence"]
        output = flat(shown(result))
        assert "Reused original evidence" in output and "Original evidence as of:" in output
        if goal == "why":
            assert "do not establish a cause" in output and "Suggested investigation:" in output


def test_actual_legacy_replay_preserves_plain_artifact_without_fabricating_view(harness):
    script, _, store, flow, _ = harness
    script["Legacy"] = I(status="ready", request=request())
    first = flow().run(OWNER, "Legacy", conversation_id="legacy-view", turn_id="fixed")
    store.db.execute("UPDATE reports SET report_support_json='',report_binding_digest='',report_composition_json='',report_completeness='legacy' WHERE id=?", (first.report_id,))
    before = tuple(store.db.execute("SELECT * FROM reports WHERE id=?", (first.report_id,)).fetchone())
    replay = flow().run(OWNER, "Legacy", conversation_id="legacy-view", turn_id="fixed")
    assert replay.presentation is None and replay.text == first.text
    assert replay.report_completeness == "legacy" and replay.report_composition is None
    output = flat(shown(replay))
    assert "Legacy saved report" in output and "not reverified" in output
    assert compact(first.text) in compact(output)
    assert tuple(store.db.execute("SELECT * FROM reports WHERE id=?", (first.report_id,)).fetchone()) == before


@pytest.mark.parametrize("selector", ["literal", "conversation"])
def test_full_deletion_preview_is_literal_and_tokens_stay_out_of_history_and_events(harness, tmp_path, selector):
    _, _, store, flow, _ = harness
    titles = ("Client [red]literal[/red] report", "Client [link=https://example.invalid]literal[/link] report")
    ids = [store.save_report(owner=OWNER, conversation_id="delete-view", turn_id=f"save-{i}",
        content=title + "\nClient result", metric_definitions=[], scope_reference="demo:" + OWNER,
        scope_version=1, evidence_refs=[]) for i, title in enumerate(titles)]
    question = "Delete all reports mentioning Client" if selector == "literal" else "Delete all the reports we made in this conversation"
    result = flow().run(OWNER, question, conversation_id="delete-view")
    assert result.status == "confirmation_required" and result.model_requests == result.query_executions == 0
    output = compact(shown(result, width=26))
    # Only private synthetic state contains the generated capability. Never emit it.
    token = result.text.split("confirm ", 1)[1].splitlines()[0]
    expected = [f"ID {rid} (version 1): {title}" for rid, title in zip(ids, titles)]
    checks = [compact(value) in output for value in (*expected, "Expires:", "Matching rule:",
        "confirm " + token, "cancel " + token, "Deletion is soft:", "bytes remain in SQLite")]
    assert all(checks)
    assert "\x1b]8" not in shown(result) and len(store.list_reports(OWNER)) == 2
    events = (tmp_path / "workflow-events.jsonl").read_text()
    assert not (token in events)
    context = store.load_continuity(OWNER, "delete-view")
    assert context is None
    cancelled = flow().run(OWNER, "cancel " + token, conversation_id="delete-view")
    assert "cancelled" in shown(cancelled) and len(store.list_reports(OWNER)) == 2


def test_default_keeps_each_evidences_distinct_definitions():
    complete = fixture_evidence(statuses=("Complete",), rows=({"revenue": Decimal("12.375")},))
    returned = fixture_evidence(metric="order_count", statuses=("Returned",), rows=({"order_count": 7},))
    output = flat(shown(result_for((complete, returned))))
    first = output.split("E1 limitations and definitions", 1)[1].split("E2 limitations and definitions", 1)[0]
    second = output.split("E2 limitations and definitions", 1)[1]
    assert "Qualifying item statuses: Complete." in first and "Revenue sums" in first
    assert "Qualifying item statuses: Returned." in second and "distinct orders" in second
    assert "Revenue sums" not in second


def test_real_cli_chat_journey_clarify_report_why_json(harness, tmp_path, monkeypatch):
    script, _, _, flow, _ = harness
    script["Missing year"] = I(status="needs_clarification", request=R(mode="report", analysis=quarter(3, None),
        queries=(Q(operation="aggregate", metric="revenue"),)), issues=("missing_year",))
    script["2025"] = I(status="ready", context_action="continue", request=request())
    script["Why"] = I(status="ready", context_action="continue", request=R(intent="why"))
    monkeypatch.setattr(cli, "AssistantWorkflow", lambda settings, store: flow(store))
    run = CliRunner().invoke(cli.app, ["chat", "--user", OWNER, "--db", str(tmp_path / "real-chat.sqlite"),
        "--json", "--verbose"], input="Missing year\n2025\nWhy\n/exit\n")
    assert run.exit_code == 0, run.exception
    results = [json.loads(line) for line in run.stdout.splitlines()]
    assert [r["status"] for r in results] == ["needs_clarification", "ok", "ok"]
    assert len({r["conversation_id"] for r in results}) == 1
    assert results[0]["presentation"] is None
    assert results[1]["presentation"]["evidence"] == results[2]["presentation"]["evidence"]
    assert results[2]["presentation"]["context_goal"] == "why"
    assert results[2]["query_executions"] == results[2]["model_requests"] == 1


def test_corrupted_saved_support_never_releases_new_display_metadata(harness):
    from opsfleet_assistant.report_artifact import artifact_binding_digest
    script, _, store, flow, _ = harness
    script["Bound view"] = I(status="ready", request=request())
    first = flow().run(OWNER, "Bound view", conversation_id="bound-view", turn_id="fixed")
    row = store.report(OWNER, first.report_id)
    composition = json.loads(row["report_composition_json"])
    composition["request"]["analysis"] = {"start": "2020-07-01", "end": "2020-10-01"}
    composition["request"]["spec"]["analysis"]["year"]["value"] = 2020
    row["report_composition_json"] = json.dumps(composition)
    digest = artifact_binding_digest(row)
    store.db.execute("UPDATE reports SET report_composition_json=?,report_binding_digest=? WHERE id=?",
        (row["report_composition_json"], digest, first.report_id))
    replay = flow().run(OWNER, "Bound view", conversation_id="bound-view", turn_id="fixed")
    assert replay.status == "error" and replay.presentation is None
    assert replay.factual_findings == () and replay.resolved_periods is None and replay.report_id is None
    assert "2020-07-01" not in shown(replay)
    assert replay.model_requests == 0 and replay.query_executions == 1


def test_empty_diagnosis_budget_caveat_visible_by_default(tmp_path):
    from test_step6 import EmptyTurn, workflow
    class Exhausted(EmptyTurn):
        source_rows = 0
        exhaust_diagnosis = True
    result = workflow(tmp_path, turn=Exhausted).run(OWNER, "Revenue in 2025")
    output = flat(shown(result))
    assert result.status == "ok" and result.presentation["empty_diagnosis"] == "incomplete"
    assert "diagnosis could not finish within the remaining budget" in output
    assert "aggregate zero is not an observed result" in output


def test_scoped_products_comparison_period_and_missing_freshness_are_disclosed():
    first = fixture_evidence(products=(1,), start="2025-04-01", end="2025-07-01")
    second = fixture_evidence(products=(1,), start="2025-01-01", end="2025-04-01")
    spec = R(mode="temporal_compare", analysis=quarter(2), comparison=quarter(1),
        queries=(Q(operation="aggregate", metric="revenue", product_ids=(1,)),
                 Q(operation="aggregate", metric="revenue", product_ids=(1,), temporal_role="comparison")))
    output = flat(shown(result_for((first, second), spec=spec)))
    assert "Comparison: UTC [2025-01-01, 2025-04-01)" in output
    assert "product IDs [1]" in output and "Latest scoped item: unknown" in output


def test_synthetic_method_provenance_and_unknown_billing_are_truthful():
    result = replace(result_for(), golden_status="selected", golden_trio_id="GK-001", golden_method="monthly_revenue_trend", billed_bytes=None)
    output = flat(shown(result, verbose=True))
    assert "synthetic authored example GK-001" in output
    assert "billed bytes: unknown" in output
