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


@pytest.fixture(autouse=True)
def offline_external_boundaries(monkeypatch):
    """Offline tests cannot accidentally use a real transport or repo secrets.

    Explicitly opt-in integrations retain their existing authorization switch.
    Synthetic settings files under tmp_path remain testable.
    """
    import os
    if os.environ.get('OPSFLEET_LIVE_TESTS') == '1':
        return
    import socket
    from opsfleet_assistant import config

    def blocked(*args, **kwargs):
        raise RuntimeError('Offline verification: external network disabled')

    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    original = config._read_dotenv

    def isolated_settings(path):
        return {} if path.parent == config.ROOT and path.name in {'.env', '.env.local'} else original(path)

    monkeypatch.setattr(config, '_read_dotenv', isolated_settings)


@pytest.fixture(scope='session', autouse=True)
def current_test_manifest(tmp_path_factory):
    """Direct pytest also uses a disposable identity, never the archived candidate."""
    import importlib.util
    import os
    from pathlib import Path
    import json
    if 'OPSFLEET_TEST_MANIFEST' in os.environ:
        yield
        return
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('test_manifest_builder', root / 'scripts/prepare_part5_validation.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path_factory.mktemp('identity') / 'manifest.json'
    path.write_text(json.dumps(module.build_manifest(local_test=True)))
    os.environ['OPSFLEET_TEST_MANIFEST'] = str(path)
    try:
        yield
    finally:
        os.environ.pop('OPSFLEET_TEST_MANIFEST', None)
