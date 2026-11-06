"""Run the real Typer CLI with explicitly injected offline model/warehouse fixtures."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from opsfleet_assistant import cli, golden, workflow as workflow_module  # noqa: E402
from opsfleet_assistant.workflow import AssistantWorkflow  # noqa: E402
from opsfleet_assistant.telemetry import TurnTrace, agent_metrics  # noqa: E402
from test_step6 import TrendGateway, TrendTurn  # noqa: E402


class OfflineWorkflow(AssistantWorkflow):
    def __init__(self, settings, store):
        super().__init__(settings, store, gateway_factory=TrendGateway,
                         turn_factory=TrendTurn)


def main() -> None:
    cli.AssistantWorkflow = OfflineWorkflow
    original_retrieve = workflow_module.retrieve
    corpus = json.loads((ROOT / "config" / "golden_demo.json").read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="opsfleet-step6-offline-") as directory:
        temp = Path(directory)
        db = temp / "state.sqlite"
        events = temp / "events.jsonl"

        class OfflineTrace(TurnTrace):
            def __init__(self, conversation_id, turn_id):
                super().__init__(conversation_id, turn_id, events)

        workflow_module.TurnTrace = OfflineTrace
        runner = CliRunner()
        cases = [
            ("matched", "Show the sales trajectory across 2025", None),
            ("no_match", "Show sales in 2025", None),
            ("retired_excluded", "Show the sales trajectory across 2025", [corpus[1]]),
            ("unauthorized_excluded", "Show the sales trajectory across 2025", [corpus[2]]),
        ]
        for label, question, records in cases:
            if records is None:
                workflow_module.retrieve = original_retrieve
            else:
                corpus_path = temp / f"{label}.json"
                corpus_path.write_text(json.dumps(records), encoding="utf-8")

                def local_retrieve(request, scope, *, path=corpus_path):
                    return golden.retrieve(request, scope, corpus=path)

                workflow_module.retrieve = local_retrieve
            args = ["ask", question, "--user", "manager_apparel", "--db", str(db), "--json"]
            result = runner.invoke(cli.app, args)
            if not result.stdout.strip():
                raise RuntimeError(f"CLI produced no JSON for {label}: exit {result.exit_code}")
            value = json.loads(result.stdout)
            print(json.dumps({
                "case": label, "mode": "OFFLINE SIMULATION",
                "command": f"opsfleet ask {json.dumps(question)} --user manager_apparel --db <isolated temp SQLite> --json",
                "exit_code": result.exit_code, "status": value["status"],
                "trace_id": value["trace_id"], "golden_status": value["golden_status"],
                "trio_id": value["golden_trio_id"], "method": value["golden_method"],
                "model_routes": value["model_routes"], "model_requests": value["model_requests"],
                "query_executions": value["query_executions"], "billed_bytes": value["billed_bytes"],
                "job_ids": value["evidence_job_ids"], "answer": value["text"],
            }, separators=(",", ":")))
        print(json.dumps({"case": "metrics", "mode": "OFFLINE SIMULATION", **agent_metrics(events)},
                         separators=(",", ":")))
    workflow_module.retrieve = original_retrieve


if __name__ == "__main__":
    main()
