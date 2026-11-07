"""User-facing local demo CLI. Identity is a trusted selector, not authentication."""

from __future__ import annotations

from enum import StrEnum
import json
from pathlib import Path

from rich.console import Console
import typer

from .config import Settings
from .store import SQLiteStore, StoreFailure
from .workflow import AssistantWorkflow, SCHEMA_HELP, TurnResult
from .telemetry import agent_metrics
from .presentation import render_result, render_navigation
from .navigation import NavigationResult, NavigationFailure
from .cli_commands import ChatSession
from .report_service import ReportService
from .conversation_service import ConversationService
from .cli_errors import JsonUsageGroup


class DemoIdentity(StrEnum):
    MANAGER_APPAREL = "manager_apparel"
    MANAGER_CASUAL = "manager_casual"
    CEO = "ceo"


app = typer.Typer(cls=JsonUsageGroup, help="Bounded retail analytics assistant for seeded demo identities.", no_args_is_help=True)
console = Console()


def _show(result: TurnResult, *, json_output: bool = False, verbose: bool = False) -> None:
    if isinstance(result, NavigationResult):
        if json_output:
            typer.echo(json.dumps(result.payload(), ensure_ascii=False))
        else:
            render_navigation(console, result)
        return
    if json_output:
        # No Rich layout, terminal width, markup or verbosity affects the data.
        from dataclasses import asdict
        payload = dict(result.__dict__)
        confirmation = payload.pop("confirmation", None)
        if confirmation is not None:
            payload["deletion_preview"] = asdict(confirmation)
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    render_result(console, result, verbose=verbose)


@app.command()
def schema() -> None:
    """Describe approved source tables and supported analyses."""
    console.print(SCHEMA_HELP)


@app.command()
def metrics(events: Path = typer.Option(Path("runtime/events.jsonl"), "--events",
                                     help="Local sanitized JSON event file.")) -> None:
    """Show derived local diagnostic counters without identity labels."""
    console.print_json(json.dumps(agent_metrics(events)))


@app.command()
def ask(
    question: str = typer.Argument(..., help="Analytical question, follow-up, or report deletion command."),
    user: DemoIdentity = typer.Option(..., "--user", help="Seeded local demo identity."),
    conversation: str | None = typer.Option(None, "--conversation", help="Existing conversation ID."),
    turn_id: str | None = typer.Option(None, "--turn-id", help="Stable retry ID for idempotent report save."),
    db: Path = typer.Option(Path("runtime/opsfleet.sqlite"), "--db", help="Local SQLite state path."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured result for transcripts."),
    verbose: bool = typer.Option(False, "--verbose", help="Show routes, jobs, bytes and detailed evidence metadata."),
) -> None:
    """Run one analysis or owner-bound report-management turn."""
    try:
        store = SQLiteStore(db)
        try:
            result = AssistantWorkflow(Settings.load(), store).run(user.value, question,
                                                conversation_id=conversation, turn_id=turn_id)
        finally:
            store.close()
    except StoreFailure:
        if json_output:
            _show(NavigationResult("store", status="store_unavailable", message="Unable to open the local state store."), json_output=True)
        else:
            console.print("Unable to open the local state store.")
        raise typer.Exit(1) from None
    _show(result, json_output=json_output, verbose=verbose)
    if result.status == "error":
        raise typer.Exit(1)


@app.command()
def chat(
    user: DemoIdentity = typer.Option(..., "--user", help="Seeded local demo identity."),
    conversation: str | None = typer.Option(None, "--conversation", help="Existing conversation ID."),
    db: Path = typer.Option(Path("runtime/opsfleet.sqlite"), "--db", help="Local SQLite state path."),
    verbose: bool = typer.Option(False, "--verbose", help="Show routes, jobs, bytes and detailed evidence metadata."),
    json_output: bool = typer.Option(False, "--json", help="Emit one structured result per turn."),
) -> None:
    """Continue an interactive conversation across turns and restarts."""
    try:
        store = SQLiteStore(db)
    except StoreFailure:
        if json_output:
            _show(NavigationResult("store", status="store_unavailable", message="Unable to open the local state store."), json_output=True)
        else:
            console.print("Unable to open the local state store.")
        raise typer.Exit(1) from None
    try:
        workflow = AssistantWorkflow(Settings.load(), store)
        try:
            session = ChatSession(workflow, user.value, conversation, store=store)
        except StoreFailure:
            _show(NavigationResult("chat", status="store_unavailable", message="Conversation selection unavailable."), json_output=json_output)
            raise typer.Exit(1) from None
        if session.startup.status != "ok":
            _show(session.startup, json_output=json_output)
            raise typer.Exit(1)
        if not json_output:
            console.print("Ask a retail analysis question. /help lists commands; /exit quits. --user is a local demo selector.")
            _show(session.startup)
        while True:
            try:
                question = input() if json_output else typer.prompt("You")
            except (EOFError, KeyboardInterrupt, typer.Abort):
                break
            result = session.handle(question)
            if isinstance(result, NavigationResult) and result.operation in {"exit", "quit"}:
                break
            _show(result, json_output=json_output, verbose=verbose)
            if not json_output:
                session.displayed(result)
                if session.pending_confirmation is not None:
                    console.print("For this displayed preview, type confirm or cancel before any other input.")
    finally:
        if "session" in locals():
            session.clear_confirmation()
        store.close()


def _navigation(operation, user, db, json_output, verbose=False, limit=20, cursor=None, report_id=None):
    service = None
    try:
        store = SQLiteStore(db)
        try:
            if operation == "conversations":
                result = ConversationService(store).list(user.value, limit=limit, cursor=cursor)
            else:
                service = ReportService.from_workflow(AssistantWorkflow(Settings.load(), store))
                result = service.read(user.value, report_id) if operation == "report" else service.list(user.value, limit=limit, cursor=cursor)
        finally:
            store.close()
    except (StoreFailure, NavigationFailure) as exc:
        result = NavigationResult(operation, status=exc.code,
            query_executions=service.query_executions if service is not None else 0,
            message="Navigation unavailable; check command or retry dependency.")
    _show(result, json_output=json_output, verbose=verbose)
    if result.status != "ok":
        raise typer.Exit(1)


@app.command()
def reports(user: DemoIdentity = typer.Option(..., "--user"),
            db: Path = typer.Option(Path("runtime/opsfleet.sqlite"), "--db"),
            limit: int = typer.Option(20, "--limit", min=1, max=50),
            cursor: str | None = typer.Option(None, "--cursor"),
            json_output: bool = typer.Option(False, "--json"),
            verbose: bool = typer.Option(False, "--verbose")):
    """List currently readable historical saved reports."""
    _navigation("reports", user, db, json_output, verbose, limit, cursor)


@app.command()
def report(report_id: int = typer.Argument(..., min=1),
           user: DemoIdentity = typer.Option(..., "--user"),
           db: Path = typer.Option(Path("runtime/opsfleet.sqlite"), "--db"),
           json_output: bool = typer.Option(False, "--json"),
           verbose: bool = typer.Option(False, "--verbose")):
    """Read the original validated historical snapshot."""
    _navigation("report", user, db, json_output, verbose, report_id=report_id)


@app.command()
def conversations(user: DemoIdentity = typer.Option(..., "--user"),
                  db: Path = typer.Option(Path("runtime/opsfleet.sqlite"), "--db"),
                  limit: int = typer.Option(20, "--limit", min=1, max=50),
                  cursor: str | None = typer.Option(None, "--cursor"),
                  json_output: bool = typer.Option(False, "--json"),
                  verbose: bool = typer.Option(False, "--verbose")):
    """List owned conversation metadata without warehouse/model access."""
    _navigation("conversations", user, db, json_output, verbose, limit, cursor)


@app.command()
def preflight() -> None:
    """Run the existing connectivity and schema checks."""
    from .preflight import main as preflight_main
    raise typer.Exit(preflight_main(["--all"]))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
