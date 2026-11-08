"""Install and verify a new temporary environment; never replaces the project .venv."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def part5_main(argv):
    """Current clean-copy install; historical manifest path below remains intact."""
    from offline_check import isolated_env
    import shutil
    parser = argparse.ArgumentParser(description='Part 5 clean editable install, isolated from local state')
    parser.add_argument('--python', required=True, help='Existing supported interpreter; never downloaded')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--privacy', action='store_true', help='Separate optional-extra trial (not locked)')
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    private = Path(tempfile.mkdtemp(prefix='opsfleet-part5-install-'))
    source = private / 'source'
    source.mkdir()
    # Copy only public source inputs, including current uncommitted Part 5 files.
    names = subprocess.check_output(['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
    for name in set(names):
        if not name or name == 'uv.lock':
            continue
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    env = isolated_env(private)
    python = private / 'venv/bin/python'
    steps = [
        ('interpreter', [args.python, '-c', 'import sys; print(sys.version); assert (3,12) <= sys.version_info[:2] <= (3,13)'], 30),
        ('create', [args.python, '-m', 'venv', str(private / 'venv')], 60),
        ('build-backend', [str(python), '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
            '--retries', '0', '--timeout', '10', '-c', 'requirements.part5.lock', 'setuptools==82.0.1'], 180),
        ('install', [str(python), '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
            '--retries', '0', '--timeout', '10', '--no-build-isolation', '-c', 'requirements.part5.lock',
            '-e', '.[test,privacy]' if args.privacy else '.[test]'], 300),
        ('dependencies', [str(python), '-m', 'pip', 'check'], 30),
        ('installed-cli', [str(private / 'venv/bin/opsfleet'), '--help'], 30),
        *[(name, [str(python), 'scripts/offline_check.py', name], 240 if name == 'tests' else 90)
          for name in ('help', 'schema', 'tests', 'golden', 'playbooks', 'navigation')],
    ]
    if args.privacy:
        # Do not instantiate AnalyzerEngine: it can download an NLP model.
        # Optional package installation and NLP runtime readiness are distinct.
        steps = steps[:5] + [('privacy-import', [str(python), '-c',
            "import presidio_analyzer, importlib.util; print('presidio_import=PASS'); print('en_core_web_lg_installed=' + str(importlib.util.find_spec('en_core_web_lg') is not None))"], 90)]
    rows = []
    for name, command, timeout in steps:
        started = time.monotonic()
        try:
            proc = subprocess.run(command, cwd=source, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout)
            raw, code = proc.stdout, proc.returncode
        except (subprocess.TimeoutExpired, OSError) as exc:
            raw, code = (b'local_step_timeout' if isinstance(exc, subprocess.TimeoutExpired)
                         else b'interpreter_or_command_unavailable'), None
        # No inherited secrets reach these subprocesses; logs contain public install/test output.
        (args.output / (name + '.txt')).write_bytes(raw)
        row = {'step': name, 'exit_code': code, 'elapsed_seconds': time.monotonic() - started,
               'status': 'PASS' if code == 0 else 'BLOCKED_ENVIRONMENT', 'attempt': 1, 'retries': 0}
        rows.append(row)
        print(json.dumps(row), flush=True)
        if code != 0:
            break
    result = {'layer': 'optional_privacy_install' if args.privacy else 'clean_install',
              'status': 'PASS' if len(rows) == len(steps) and rows[-1]['status'] == 'PASS' else 'BLOCKED_ENVIRONMENT',
              'steps': rows, 'not_run': [n for n, _, _ in steps[len(rows):]],
              'private_copy': str(private), 'live_calls': 0,
              'optional_nlp_runtime': 'NOT_RUN: no model initialization or download authorized' if args.privacy else None,
              'source_files': {n: hashlib.sha256((source / n).read_bytes()).hexdigest()
                               for n in sorted(set(names)) if n and n != 'uv.lock'}}
    (args.output / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    return 0 if result['status'] == 'PASS' else 1


def main():
    if '--part5' in sys.argv:
        return part5_main([arg for arg in sys.argv[1:] if arg != '--part5'])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,79}',args.run_id): parser.error('invalid_run_id')
    digest = lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
    source = json.loads((ROOT/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json').read_text())
    aggregate = hashlib.sha256(''.join(f'{source["files"][p]}  {p}\n' for p in sorted(source['files'])).encode()).hexdigest()
    if aggregate != source['revision'] or any(digest(ROOT/p) != expected for p,expected in source['files'].items()):
        parser.error('source_changed')
    output = ROOT/'docs/remediation/runs'/args.run_id
    output.mkdir(mode=0o700,exist_ok=False)
    private = Path(tempfile.mkdtemp(prefix='opsfleet-fresh-environment-'))
    python = private/'venv/bin/python'
    before = {str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'runtime').rglob('*') if p.is_file()}
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', OPSFLEET_LIVE_TESTS='0', PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONPYCACHEPREFIX=str(private/'pycache'))
    for key in ('PYTHONPATH','PIP_EXTRA_INDEX_URL','PIP_INDEX_URL'): env.pop(key,None)
    steps = [
        ('create',[sys.executable,'-m','venv',str(private/'venv')],60),
        ('install',[str(python),'-m','pip','install','--index-url','https://pypi.org/simple',
            '--retries','0','--timeout','10','-e','.[test]'],300),
        ('dependencies',[str(python),'-m','pip','check'],60),
        ('entry-point',[str(private/'venv/bin/opsfleet'),'--help'],30),
        ('tests',[str(python),'-m','pytest','-q','-p','no:cacheprovider'],180),
        ('offline-cli',[str(python),'scripts/step6_offline_cli_demo.py'],60),
    ]
    rows = []
    plan = {'source_revision':source['revision'],'driver_sha256':digest(Path(__file__)),
        'temporary_environment':str(private/'venv'),'runtime_before':before,
        'steps':[{'name':name,'command':command,'timeout_seconds':timeout} for name,command,timeout in steps],
        'live_provider_or_warehouse_calls_permitted':0}
    (output/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    for name,command,timeout in steps:
        started = time.monotonic()
        with (output/f'{name}.log').open('w') as log:
            try:
                completed = subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
                row = {'step':name,'exit_code':completed.returncode,'elapsed_seconds':time.monotonic()-started}
            except subprocess.TimeoutExpired:
                row = {'step':name,'exit_code':None,'elapsed_seconds':time.monotonic()-started,'reason':'timeout'}
        rows.append(row)
        print(json.dumps(row),flush=True)
        if row['exit_code'] != 0: break
    after = {str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'runtime').rglob('*') if p.is_file()}
    result = {'source_revision':source['revision'],'steps':rows,
        'status':'PASS' if len(rows)==len(steps) and all(row['exit_code']==0 for row in rows) else 'INCOMPLETE',
        'not_run':[name for name,_,_ in steps[len(rows):]],'runtime_unchanged':before==after,
        'source_unchanged':all(digest(ROOT/p)==expected for p,expected in source['files'].items()),
        'temporary_environment':str(private/'venv'),'live_calls':0}
    (output/'results.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'output':str(output/'results.json'),'status':result['status'],
        'runtime_unchanged':result['runtime_unchanged']}),flush=True)
    return 0 if result['status']=='PASS' and result['runtime_unchanged'] and result['source_unchanged'] else 1


if __name__ == '__main__': raise SystemExit(main())
