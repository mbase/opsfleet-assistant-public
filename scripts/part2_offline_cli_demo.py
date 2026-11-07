"""Synthetic Part 2 demonstration through real CLI/workflow/compiler/adapter.

No provider, ADC or live warehouse. Every CLI invocation opens/closes disposable
SQLite state, including save/restart/replay. Requires the documented test extra.
"""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import os
import socket
import sys
from tempfile import TemporaryDirectory
os.environ['OPSFLEET_LIVE_TESTS']='0'
def offline(*args,**kwargs):raise RuntimeError('Offline demonstration: network disabled')
socket.create_connection=offline
socket.socket.connect=offline
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from typer.testing import CliRunner
from opsfleet_assistant import cli,workflow as workflow_module
from opsfleet_assistant.bigquery_adapter import BigQueryTurn
from opsfleet_assistant.workflow import AssistantWorkflow
from opsfleet_assistant.telemetry import TurnTrace
from test_playbooks import PlaybookGateway,SETTINGS,spec,regional_spec,churn_spec
from playbook_source import product_source,regional_source,churn_source


def main():
    from types import SimpleNamespace
    cli.Settings=SimpleNamespace(load=lambda:SETTINGS)
    runner=CliRunner()
    with TemporaryDirectory(prefix='opsfleet-part2-offline-') as directory:
        temp=Path(directory)
        class Trace(TurnTrace):
            def __init__(self,*args):super().__init__(*args,path=temp/'events.jsonl')
        workflow_module.TurnTrace=Trace
        for id,source,request,expected in [('product_revenue',product_source,spec,('400','550','-150')),
                ('regional_spend',regional_source,regional_spec,('20','65','-45')),
                ('churn_change',churn_source,churn_spec,('.11','.01','.10'))]:
            for partial in (False,True):
                client=source()
                if partial:
                    if id=='product_revenue':client.db.execute('DELETE FROM order_items WHERE product_id=2')
                    elif id=='regional_spend':client.db.execute("DELETE FROM users WHERE state='California' AND id>104")
                    else:client.db.execute("UPDATE users SET country='Tiny' WHERE id=1")
                class Gateway(PlaybookGateway):pass
                Gateway.spec=request()
                class OfflineWorkflow(AssistantWorkflow):
                    def __init__(self,settings,store):
                        super().__init__(SETTINGS,store,gateway_factory=Gateway,
                            turn_factory=lambda settings,limits:BigQueryTurn(settings,client=client,limits=limits),today=date(2026,11,6))
                cli.AssistantWorkflow=OfflineWorkflow
                db=temp/f'{id}-{partial}.sqlite'
                def invoke(question,*extra):
                    result=runner.invoke(cli.app,['ask',question,'--user','manager_apparel','--db',str(db),'--json',*extra])
                    if result.exit_code:raise RuntimeError(result.stdout or str(result.exception))
                    return json.loads(result.stdout)
                result=invoke('Offline synthetic investigation')
                driver=result['driver_finding']
                actual=(driver['total_difference'],*(c['value'] for c in driver['contributions']))
                if not partial:assert tuple(map(Decimal,actual))==tuple(map(Decimal,expected))
                else:assert not driver['complete'] and not driver['contributions'] and driver['residual'] is None
                saved=invoke('save','--conversation',result['conversation_id'])
                replay=invoke('save','--conversation',result['conversation_id'],'--turn-id',saved['turn_id'])
                assert saved['report_id'] and replay['reused_report'] and saved['text']==replay['text']
                assert replay['driver_finding']==driver
                print(json.dumps({'mode':'OFFLINE SYNTHETIC — injected interpretation, SQLite source rows',
                    'fixture_identity':'part2_sqlite_v1','playbook':id,'partial':partial,'expected':None if partial else expected,'actual':actual,
                    'availability_reason':driver['availability_reason'],'query_executions':result['query_executions'],
                    'saved_completeness':saved['report_completeness'],'restart_replay_validated':True,
                    'saved_report_id':saved['report_id'],'driver':driver},ensure_ascii=False))
                client.db.close()
if __name__=='__main__':main()
