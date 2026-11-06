"""Terminal wrapper budgets, stop conditions and no replay; no live requests."""
import json
import subprocess
import sys

import pytest

from test_full_validation_driver import load_script,copied_workspace,forbidden


@pytest.mark.parametrize('mode',['pass','dry','journey_failure','probe_failure','timeout','interrupt'])
def test_terminal_wrapper_bounds_stages_and_stops_safely(tmp_path,monkeypatch,mode):
    wrapper=load_script('run_provider_validation_terminal');copied_workspace(tmp_path)
    monkeypatch.setattr(wrapper,'ROOT',tmp_path)
    monkeypatch.setattr(wrapper,'__file__',str(tmp_path/'scripts/run_provider_validation_terminal.py'))
    monkeypatch.setattr('socket.getaddrinfo',forbidden)
    live=mode!='dry';calls=[]
    source=json.loads((tmp_path/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json').read_text())
    def child(command,**kwargs):
        calls.append(command)
        if mode=='timeout':raise subprocess.TimeoutExpired(command,300)
        if mode=='interrupt':raise KeyboardInterrupt()
        stage=wrapper.stages('control-terminal',live)[len(calls)-1]
        assert command==stage['command'] and kwargs['timeout']==stage['active_seconds']+60
        assert kwargs['cwd']==tmp_path
        assert ('--live' in command)==live
        output=tmp_path/'docs/remediation/runs'/stage['run_id'];output.mkdir()
        statuses=['NOT_RUN' if not live else 'PASS']*stage['cases']
        if mode=='journey_failure':statuses[0]='PROVIDER_FAILURE'
        if mode=='probe_failure' and stage['name']=='defaults':statuses[0]='FAIL'
        value={'identity':{'source_revision':source['revision'],'live':live},'runtime_unchanged':True,
            'results':[{'status':s} for s in statuses],'used':{'provider_attempts':stage['cases'] if live else 0},'stop':None}
        (output/'results.json').write_text(json.dumps(value))
        return subprocess.CompletedProcess(command,1 if mode=='probe_failure' and stage['name']=='defaults' else 0)
    monkeypatch.setattr(wrapper.subprocess,'run',child)
    monkeypatch.setattr(sys,'argv',['wrapper','--run-id','control-terminal']+(['--live'] if live else []))
    assert wrapper.main()==(0 if mode in {'pass','dry'} else 1)
    output=tmp_path/'docs/remediation/runs/control-terminal'
    plan=json.loads((output/'plan.json').read_text());result=json.loads((output/'results.json').read_text())
    assert len(calls)==(3 if mode in {'pass','dry'} else 2 if mode=='probe_failure' else 1)
    assert plan['provider_attempts_total_cap']==48 and plan['actual_bigquery_jobs_cap']==8
    assert plan['active_seconds_total_cap']==1080 and plan['provider_turn_retries']==0
    assert result['runtime_unchanged'] and result['all_stages_passed']==(mode=='pass')
    if mode not in {'pass','dry'}:
        assert result['stages'][-1]['status']=='NOT_RUN'
        assert result['stages'][-1]['reason']=='prior_stage_did_not_pass'
    # Repeating the ID cannot spend any more requests or overwrite prior evidence.
    with pytest.raises(SystemExit):wrapper.main()
    assert len(calls)==(3 if mode in {'pass','dry'} else 2 if mode=='probe_failure' else 1)
