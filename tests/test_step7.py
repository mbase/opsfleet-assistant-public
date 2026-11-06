"""Final CLI setup contract checks without external calls."""

from typer.testing import CliRunner

from opsfleet_assistant.cli import app
from opsfleet_assistant import preflight


def test_documented_cli_preflight_runs_all_checks(monkeypatch):
    seen = []

    def fake_main(argv=None):
        seen.append(argv)
        return 0

    monkeypatch.setattr(preflight, "main", fake_main)
    result = CliRunner().invoke(app, ["preflight"])
    assert result.exit_code == 0
    assert seen == [["--all"]]
