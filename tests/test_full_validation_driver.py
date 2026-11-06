"""Full validation orchestration controls; no real external execution."""
from copy import deepcopy
import importlib.util
import json
import shutil
import subprocess
import sys

import pytest

from opsfleet_assistant.config import ROOT, Settings
from opsfleet_assistant.evaluation_contracts import Accounting


def load_script(name):
    spec = importlib.util.spec_from_file_location(name,ROOT/f'scripts/{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copied_workspace(tmp_path):
    frozen = json.loads((ROOT/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json').read_text())
    for relative in (*frozen['files'],'docs/remediation/EVALUATION_SOURCE_MANIFEST.json'):
        target = tmp_path/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/relative,target)
    (tmp_path/'docs/remediation/runs').mkdir(exist_ok=True)
    (tmp_path/'runtime').mkdir()
    (tmp_path/'runtime/sentinel').write_text('unchanged')


def forbidden(*args,**kwargs): pytest.fail('unexpected external access')


def test_batches_cover_all_original_scenarios_once():
    driver = load_script('run_live_evaluation')
    manifest = json.loads((ROOT/'docs/remediation/SCENARIO_MANIFEST.json').read_text())
    groups = driver.batches(manifest)
    assert len(groups)==6 and [len(group) for _,group in groups]==[12,12,12,11,2,23]
    assert len(set(key for _,group in groups for key in group))==72
    assert sum(len(case['turns']) for case in manifest['scenarios'])==99
    assert driver.CAPS=={'turns':120,'provider_attempts':240,'warehouse_executions':40,
        'billed_bytes':5*1024**3,'active_seconds':2700}


def test_dry_full_plan_never_loads_settings_or_dispatches(tmp_path,monkeypatch,capsys):
    driver = load_script('run_live_evaluation');copied_workspace(tmp_path)
    monkeypatch.setattr(driver,'ROOT',tmp_path)
    monkeypatch.setattr(driver,'__file__',str(tmp_path/'scripts/run_live_evaluation.py'))
    monkeypatch.setattr(Settings,'load',forbidden)
    monkeypatch.setattr('socket.getaddrinfo',forbidden)
    monkeypatch.setattr('subprocess.run',forbidden)
    monkeypatch.setattr(sys,'argv',['driver','--run-id','dry-control'])
    assert driver.main()==0
    result=json.loads((tmp_path/'docs/remediation/runs/dry-control/results.json').read_text())
    assert result['counts']['planned_turns']==99 and result['counts']['turns']['NOT_RUN']==99
    assert result['used']['provider_attempts']==result['used']['warehouse_executions']==0
    assert result['runtime_unchanged'] and result['identity']['review_status']=='dry_plan'


@pytest.mark.parametrize('interrupted',[False,True])
def test_live_shared_caps_snapshots_and_resume_no_replay(tmp_path,monkeypatch,capsys,interrupted):
    driver=load_script('run_live_evaluation');copied_workspace(tmp_path)
    monkeypatch.setattr(driver,'ROOT',tmp_path)
    monkeypatch.setattr(driver,'__file__',str(tmp_path/'scripts/run_live_evaluation.py'))
    monkeypatch.setattr(Settings,'load',lambda:Settings('fixture-project','US','unused',openrouter_api_key='synthetic-key'))
    calls=[]
    def simulated_execute(account,manifest,private,*,live,settings,selection):
        assert live and account.state['caps']==driver.CAPS
        calls.append(sorted(selection))
        account.begin_turn('batch-'+str(len(calls)))
        ticket=account.reserve('provider',route='openrouter_gemini')
        account.finish(ticket,outcome='response_received')
        account.end_turn()
        if interrupted:
            account.state['stop']='interrupted_turn_requires_report'
            raise KeyboardInterrupt()
        return []
    monkeypatch.setattr(driver,'execute',simulated_execute)
    monkeypatch.setattr(sys,'argv',['driver','--live','--run-id','live-control'])
    if interrupted:
        with pytest.raises(KeyboardInterrupt):driver.main()
    else:assert driver.main()==0
    output=tmp_path/'docs/remediation/runs/live-control'
    first=json.loads((output/'results.json').read_text())
    snapshots={p.name:p.read_bytes() for p in output.glob('results-batch-*.json')}
    lines=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    private=lines[0]['private_resume_directory']
    assert len(calls)==(1 if interrupted else 6)
    assert first['used']['provider_attempts']==len(calls) and first['runtime_unchanged']
    assert first['identity']['review_status']=='waived_by_user' and first['identity']['review_sha256'] is None
    monkeypatch.setattr(sys,'argv',['driver','--live','--run-id','live-control','--resume',private])
    assert driver.main()==0
    assert len(calls)==(1 if interrupted else 6)
    for name,content in snapshots.items():assert (output/name).read_bytes()==content
    saved=json.loads((output/'results.json').read_text())
    assert saved['used']['provider_attempts']==first['used']['provider_attempts']


@pytest.mark.parametrize('failure_step',[None,'install'])
def test_fresh_environment_is_temporary_and_does_not_touch_existing_venv(tmp_path,monkeypatch,capsys,failure_step):
    driver=load_script('check_fresh_environment');copied_workspace(tmp_path)
    monkeypatch.setattr(driver,'ROOT',tmp_path)
    monkeypatch.setattr(driver,'__file__',str(tmp_path/'scripts/check_fresh_environment.py'))
    calls=[]
    def simulated(command,**kwargs):
        calls.append(command)
        assert kwargs['env']['OPSFLEET_LIVE_TESTS']=='0'
        assert kwargs['env']['PIP_CONFIG_FILE']
        assert all(str(tmp_path/'.venv') not in part for part in command)
        if 'install' in command:
            assert 'https://pypi.org/simple' in command and '--retries' in command
        return subprocess.CompletedProcess(command,1 if failure_step=='install' and 'install' in command else 0)
    monkeypatch.setattr(driver.subprocess,'run',simulated)
    monkeypatch.setattr(sys,'argv',['fresh','--run-id','fresh-control'])
    assert driver.main()==(1 if failure_step else 0)
    result=json.loads((tmp_path/'docs/remediation/runs/fresh-control/results.json').read_text())
    assert result['status']==('INCOMPLETE' if failure_step else 'PASS')
    assert len(calls)==(2 if failure_step else 6)
    assert result['runtime_unchanged'] and result['source_unchanged'] and result['live_calls']==0
