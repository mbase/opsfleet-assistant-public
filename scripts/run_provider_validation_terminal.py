"""Run bounded provider validation from a normal terminal; dry by default."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from opsfleet_assistant.evaluation_contracts import atomic_json,digest,verify_source


def stages(run_id,live):
    common=[str(ROOT/'.venv/bin/python')]
    workflow=common+[str(ROOT/'scripts/run_live_evaluation.py')]
    flag=['--live'] if live else []
    return [
        {'name':'journey','run_id':run_id+'-journey','cases':6,'active_seconds':240,
         'command':workflow+flag+['--run-id',run_id+'-journey','--turns','6','--provider-attempts','12',
             '--warehouse-executions','1','--billed-bytes','1','--active-seconds','240','--select','journey']},
        {'name':'defaults','run_id':run_id+'-defaults','cases':8,'active_seconds':180,
         'command':common+[str(ROOT/'scripts/probe_interpretation_defaults.py')]+flag+['--run-id',run_id+'-defaults']},
        {'name':'affected','run_id':run_id+'-affected','cases':14,'active_seconds':660,
         'command':workflow+flag+['--run-id',run_id+'-affected','--turns','14','--provider-attempts','28',
             '--warehouse-executions','8','--billed-bytes','1073741824','--active-seconds','660','--select',
             'ambiguity-spend','report-empty','novel-leap-negation','novel-role-layout','novel-narrow-save',
             'novel-unauthorized-recovery','live-products','novel-zero-save']},
    ]


def fingerprints():
    return {str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'runtime').rglob('*') if p.is_file()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Dispatch within the existing user review waiver.')
    parser.add_argument('--run-id',default='provider-terminal-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6])
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,64}',args.run_id):parser.error('invalid_run_id')
    source=json.loads((ROOT/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json').read_text())
    revision=verify_source(ROOT,source)
    if digest(ROOT/'docs/remediation/SCENARIO_MANIFEST.json')!=source['scenario_sha256']:parser.error('scenario_hash_mismatch')
    selected=stages(args.run_id,args.live)
    runs=ROOT/'docs/remediation/runs'
    if any((runs/stage['run_id']).exists() for stage in selected) or (runs/args.run_id).exists():
        parser.error('run_id_already_exists_no_automatic_replay')
    output=runs/args.run_id;output.mkdir(mode=0o700)
    before=fingerprints()
    identity={'run_id':args.run_id,'source_revision':revision,'scenario_sha256':source['scenario_sha256'],
        'driver_sha256':digest(Path(__file__)),'live':args.live,'review_status':'waived_by_user' if args.live else 'dry_plan'}
    atomic_json(output/'plan.json',{'identity':identity,'stages':selected,'runtime_before':before,
        'provider_attempts_total_cap':48,'active_seconds_total_cap':1080,
        'actual_bigquery_jobs_cap':8,'billed_bytes_cap':1073741824,
        'note':'Journey fixture one-job/one-byte allowances are unused; all real warehouse work is in affected stage.',
        'provider_turn_retries':0,'stop_on_failed_or_interrupted_stage':True})
    rows=[{'stage':s['name'],'run_id':s['run_id'],'status':'NOT_RUN','reason':'not_started'} for s in selected]
    def save():
        after=fingerprints()
        atomic_json(output/'results.json',{'identity':identity,'stages':rows,'runtime_unchanged':before==after,
            'all_stages_passed':args.live and all(r['status']=='PASS' for r in rows),
            'limits':['Driver completion is not independent acceptance.','Original and failed runs are never replayed/reset.']})
    save()
    print('Validation plan: '+str(output/'plan.json'),flush=True)
    print('Live dispatch: '+str(args.live)+'; maximum 48 provider attempts, 8 real BigQuery jobs, 1 GiB, 1080 active seconds.',flush=True)
    try:
        for stage,row in zip(selected,rows):
            verify_source(ROOT,source)
            if fingerprints()!=before:raise ValueError('runtime_changed')
            row.update(status='RUNNING',reason=None);save()
            print('Starting '+stage['name']+' ('+stage['run_id']+')',flush=True)
            try:
                child=subprocess.run(stage['command'],cwd=ROOT,
                    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},timeout=stage['active_seconds']+60)
                result_path=runs/stage['run_id']/'results.json'
                row['results_path']=str(result_path.relative_to(ROOT))
                row['exit_code']=child.returncode
                if not result_path.is_file():
                    row.update(status='FAIL',reason='missing_stage_results')
                else:
                    result=json.loads(result_path.read_text())
                    identity_ok=result['identity']['source_revision']==revision and result['identity']['live']==args.live
                    records=result['results']
                    integrity=identity_ok and result.get('runtime_unchanged') is True and len(records)==stage['cases']
                    row['counts']={status:sum(r['status']==status for r in records) for status in sorted({r['status'] for r in records})}
                    row['used']=result.get('used')
                    if not args.live:
                        ok=child.returncode==0 and integrity and result['used']['provider_attempts']==0
                        row.update(status='DRY_PLAN' if ok else 'FAIL',reason='no_dispatch' if ok else 'dry_plan_failed')
                    else:
                        ok=child.returncode==0 and integrity and not result.get('stop') and all(r['status']=='PASS' for r in records)
                        row.update(status='PASS' if ok else 'FAIL',reason=None if ok else 'stage_did_not_pass')
            except subprocess.TimeoutExpired:
                row.update(status='INTERRUPTED',reason='stage_timeout_no_replay')
            save()
            print(stage['name']+': '+row['status'],flush=True)
            if row['status'] not in {'PASS','DRY_PLAN'}:break
    except KeyboardInterrupt:
        for row in rows:
            if row['status']=='RUNNING':row.update(status='INTERRUPTED',reason='interrupted_no_replay')
    except Exception:
        for row in rows:
            if row['status']=='RUNNING':row.update(status='FAIL',reason='local_validation_failure')
    finally:
        failed=any(r['status'] not in {'PASS','DRY_PLAN','NOT_RUN'} for r in rows)
        for row in rows:
            if row['status']=='NOT_RUN':row['reason']='prior_stage_did_not_pass' if failed else 'not_started'
        save()
        print('Return this results file: '+str(output/'results.json'),flush=True)
    return 0 if all(r['status'] in {'PASS','DRY_PLAN'} for r in rows) else 1

if __name__=='__main__':raise SystemExit(main())
