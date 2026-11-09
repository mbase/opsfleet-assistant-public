"""Credential-free, network-disabled verification entry point (also used by CI)."""
import argparse
import json
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def isolated_env(private):
    # An allowlist avoids inheriting ADC, cloud selectors, tokens or index credentials.
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
    env.update(HOME=str(private), CLOUDSDK_CONFIG=str(private / 'gcloud'),
               OPSFLEET_LIVE_TESTS='0', PYTHONDONTWRITEBYTECODE='1',
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PIP_CONFIG_FILE=os.devnull,
               PIP_DISABLE_PIP_VERSION_CHECK='1')
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('check', choices=('tests', 'help', 'schema', 'golden', 'playbooks', 'navigation'))
    args, extra = parser.parse_known_args(argv)
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
    def blocked(*a, **kw):
        raise RuntimeError('offline_transport_disabled')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = blocked
    with tempfile.TemporaryDirectory(prefix='opsfleet-offline-') as directory:
        private = Path(directory)
        env = isolated_env(private)
        os.environ.clear()
        os.environ.update(env)
        from opsfleet_assistant import config, workflow
        from opsfleet_assistant.telemetry import TurnTrace
        original = config._read_dotenv
        config._read_dotenv = lambda p: {} if p.parent == config.ROOT else original(p)
        class Trace(TurnTrace):
            def __init__(self, c, t):
                super().__init__(c, t, private / 'events.jsonl')
        workflow.TurnTrace = Trace
        if args.check == 'tests':
            from prepare_part5_validation import build_manifest
            manifest = private / 'local-test-manifest.json'
            manifest.write_text(json.dumps(build_manifest(local_test=True)))
            os.environ['OPSFLEET_TEST_MANIFEST'] = str(manifest)
            import pytest
            return pytest.main(['-q', '-p', 'no:cacheprovider', *extra])
        if extra:
            parser.error('unexpected_arguments')
        if args.check in {'help', 'schema'}:
            from typer.testing import CliRunner
            from opsfleet_assistant.cli import app
            result = CliRunner().invoke(app, ['--help'] if args.check == 'help' else ['schema'])
            print(result.stdout, end='')
            return result.exit_code
        script = {'golden': 'step6_offline_cli_demo.py', 'playbooks': 'part2_offline_cli_demo.py',
                  'navigation': 'part3_offline_cli_demo.py'}[args.check]
        runpy.run_path(str(ROOT / 'scripts' / script), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
