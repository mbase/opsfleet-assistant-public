"""Keep workflow diagnostics from appending to the user's runtime events."""

import pytest

from opsfleet_assistant import workflow
from opsfleet_assistant.telemetry import TurnTrace


@pytest.fixture(autouse=True)
def isolated_workflow_telemetry(tmp_path, monkeypatch):
    class IsolatedTrace(TurnTrace):
        def __init__(self, conversation_id, turn_id):
            super().__init__(conversation_id, turn_id, tmp_path / "workflow-events.jsonl")

    monkeypatch.setattr(workflow, "TurnTrace", IsolatedTrace)
