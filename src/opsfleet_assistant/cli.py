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
from .presentation import render_result


class DemoIdentity(StrEnum):
    MANAGER_APPAREL = "manager_apparel"
    MANAGER_CASUAL = "manager_casual"
    CEO = "ceo"


app = typer.Typer(help="Bounded retail analytics assistant for seeded demo identities.", no_args_is_help=True)
console = Console()


def _show(result: TurnResult, *, json_output: bool = False, verbose: bool = False) -> None:
    if json_output:
        # No Rich layout, terminal width, markup or verbosity affects the data.
        typer.echo(json.dumps(result.__dict__, ensure_ascii=False))
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
        console.print("Unable to open the local state store.")
        raise typer.Exit(1) from None
    try:
        workflow = AssistantWorkflow(Settings.load(), store)
        if not json_output:
            console.print("Ask a retail analysis question or request report deletion. Type /schema for source help or /exit to quit. --user is a local demo selector, not authentication.")
        while True:
            try:
                question = input() if json_output else typer.prompt("You")
            except (EOFError, KeyboardInterrupt):
                break
            if question.strip().casefold() in {"/exit", "/quit"}:
                break
            if question.strip().casefold() in {"/schema", "/help"}:
                if json_output:
                    typer.echo(json.dumps({"schema_help": SCHEMA_HELP}))
                else:
                    console.print(SCHEMA_HELP)
                continue
            result = workflow.run(user.value, question, conversation_id=conversation)
            conversation = result.conversation_id
            _show(result, json_output=json_output, verbose=verbose)
    finally:
        store.close()


@app.command()
def preflight() -> None:
    """Run the existing connectivity and schema checks."""
    from .preflight import main as preflight_main
    raise typer.Exit(preflight_main(["--all"]))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
