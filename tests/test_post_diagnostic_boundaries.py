"""Offline boundary probes: no live entry point or credentials are used."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
import pytest
from opsfleet_assistant.config import Settings
from opsfleet_assistant import preflight

@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo', lambda *a, **k: pytest.fail('live DNS'))
    spec = importlib.util.spec_from_file_location('terminal_probe', Path(__file__).parents[1]/'scripts/openrouter_terminal_diagnostic.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.mark.parametrize('content,finish,category,success', [('OK','stop','usable_response',True),('OK ','stop','other_response',False),('OK','length','truncated_response',False),('', 'stop','other_response',False)])
def test_terminal_exact_response(terminal, content, finish, category, success):
    class Response:
        status=200
        headers={'x-request-id':'unsafe secret\n'}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self,n):
            assert n==1_000_000
            return json.dumps({'choices':[{'message':{'content':content},'finish_reason':finish}], 'usage':{'cost':-1,'total_tokens':True}}).encode()
    calls=[]
    def dispatch(req,timeout):
        calls.append(req)
        assert timeout==30 and json.loads(req.data)['max_tokens']==64
        return Response()
    result=terminal.request_case('synthetic-key',64,SimpleNamespace(open=dispatch))
    assert len(calls)==1 and result['success'] is success and result['category']==category
    assert result['usage'] is None and result['cost'] is None
    assert 'synthetic-key' not in json.dumps(result) and result['trace_id'] is None

@pytest.mark.parametrize('code,category',[(401,'authentication'),(402,'quota_or_billing'),(429,'UNKNOWN_429_quota_or_rate_limit'),(503,'other_service_failure'),(302,'other_http_failure')])
def test_terminal_http(terminal,code,category):
    def dispatch(*a,**k): raise HTTPError('https://secret',code,'secret',{},None)
    result=terminal.request_case('secret',10,SimpleNamespace(open=dispatch))
    assert result['category']==category and not result['success']
    assert 'secret' not in json.dumps(result)
    assert terminal.NoRedirect().redirect_request(None,None,302,'',{},'https://secret') is None

@pytest.mark.parametrize('body',[b'',b'{',b'[]',b'{"choices":[null]}',b'{"choices":[{"message":null}]}'])
def test_terminal_malformed(terminal,body):
    class Response:
        status=200; headers={}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self,n): return body
    result=terminal.request_case('secret',64,SimpleNamespace(open=lambda *a,**k:Response()))
    assert result['category']=='invalid_or_unrecognized_response' and not result['success']


def test_terminal_missing_credentials(terminal,monkeypatch,tmp_path):
    monkeypatch.delenv('OPENROUTER_API_KEY',raising=False)
    monkeypatch.setattr(terminal,'getpass',lambda *a:'')
    monkeypatch.setattr(terminal,'make_output_dir',lambda:tmp_path)
    monkeypatch.setattr(terminal,'dns_check',lambda:{'status':'failed'})
    monkeypatch.setattr(terminal,'build_opener',lambda *a:pytest.fail('unexpected transport'))
    assert terminal.main()==1
    data=json.loads((tmp_path/'results.json').read_text())
    assert all(c['category']=='credentials_unavailable' for c in data['requests'])
    assert (tmp_path/'results.json').stat().st_mode & 0o777==0o600

@pytest.mark.parametrize('choice,status', [({'finish_reason':'length'},'truncated_output'),({'message':{'content':''}},'empty_output'),({'message':{'content':'bad'}},'invalid_json'),({'message':{'content':'{"status":"ok"}'}},'ok'),({'message':{'content':'{}'}},'unexpected_output')])
def test_preflight_responses(monkeypatch,choice,status):
    class Client:
        def __init__(self,**kw): assert kw=={'timeout':20.0,'follow_redirects':False}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def post(self,*a,**kw): return SimpleNamespace(status_code=200,json=lambda:{'choices':[choice]})
    monkeypatch.setattr('httpx.Client',Client)
    assert preflight.openrouter_preflight(Settings(None,'US','unused',openrouter_api_key='secret'))['status']==status


def test_preflight_absence_and_query_bounds(monkeypatch):
    monkeypatch.setattr('httpx.Client',lambda **k:pytest.fail('credentials absent'))
    assert preflight.openrouter_preflight(Settings(None,'US','unused'))['status']=='missing_api_key'
    assert preflight.bigquery_preflight(Settings(None,'US','unused'))['status']=='missing_project'
    calls=[]
    class Job:
        total_bytes_processed=12; total_bytes_billed=20; job_id='synthetic'
        def result(self,**kw):
            assert kw==dict(timeout=35,max_results=150,retry=None,job_retry=None)
            return []
    def query(sql,**kw): calls.append(kw); return Job()
    result=preflight._run_query(SimpleNamespace(query=query),'SELECT 1','US')
    assert result['billed_bytes']==20 and len(calls)==2
    assert calls[0]['job_config'].dry_run and int(calls[1]['job_config'].job_timeout_ms)==30000
    assert all(c['retry'] is None and c['job_retry'] is None and c['timeout']==20 for c in calls)

@pytest.mark.parametrize('fault,expected',[(TimeoutError('secret'),'network_or_timeout'),(RuntimeError('secret'),'UNKNOWN')])
def test_terminal_transport_errors(terminal,fault,expected):
    def dispatch(*a,**k): raise fault
    result=terminal.request_case('secret',64,SimpleNamespace(open=dispatch))
    assert result['category']==expected and 'secret' not in json.dumps(result)


def test_telemetry_hostile_metadata(tmp_path):
    from opsfleet_assistant.telemetry import TurnTrace
    trace=TurnTrace('synthetic','synthetic',tmp_path/'events')
    trace.emit('model_result',failure_kind={'secret':'value'},transport_type='Bearer secret',http_status=True,dns_errno=10001,raw_error='secret')
    result=json.loads((tmp_path/'events').read_text())
    assert result['failure_kind']==result['transport_type']=='unknown'
    assert not {'http_status','dns_errno','raw_error'} & result.keys()
    assert 'secret' not in json.dumps(result)

@pytest.mark.parametrize('kind',['dns_resolution','http','timeout','unknown'])
def test_batch_failure_resume_and_dependencies(tmp_path,monkeypatch,kind):
    from opsfleet_assistant.evaluation_contracts import Accounting
    from opsfleet_assistant.evaluation_runner import ProviderObserver,execute
    a=Accounting(tmp_path/'checkpoint.json',identity={'run_id':'synthetic'})
    calls=[]
    def dispatch(case,turn,*args):
        calls.append(turn['id'])
        if turn['id']=='a1':
            observer=ProviderObserver(a)
            event=observer.begin('synthetic')
            observer.failed(event,diagnostics={'failure_kind':kind})
        return {'status':'ok'}
    monkeypatch.setattr('opsfleet_assistant.evaluation_runner.run_workflow',dispatch)
    turn=lambda name,deps:dict(id=name,depends_on=deps,assertions=[])
    manifest={'scenarios':[dict(id='a',mode='live_fixture',turns=[turn('a1',[]),turn('a2',['a1'])]),dict(id='b',mode='live_fixture',turns=[turn('b1',[])])]}
    rows=execute(a,manifest,tmp_path,live=True,settings=Settings(None,'US','unused'))
    assert [r['status'] for r in rows]==(['BLOCKED_ENVIRONMENT','NOT_RUN','NOT_RUN'] if kind=='dns_resolution' else ['PROVIDER_FAILURE','NOT_RUN','PASS'])
    before=json.loads(json.dumps(a.state))
    a=Accounting(tmp_path/'checkpoint.json',identity={'run_id':'synthetic'})
    execute(a,manifest,tmp_path,live=True,settings=Settings(None,'US','unused'))
    assert calls==(['a1'] if kind=='dns_resolution' else ['a1','b1'])
    assert a.state['used']['provider_attempts']==1
    assert a.state['used']['turns']==before['used']['turns']
