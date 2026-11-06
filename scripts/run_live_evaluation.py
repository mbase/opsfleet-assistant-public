"""Bounded full evaluation under the user's explicit review waiver; dry by default."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from opsfleet_assistant.config import Settings
from opsfleet_assistant.evaluation_contracts import Accounting, MAX_CAPS, Redactor, atomic_json, digest, totals, verify_source
from opsfleet_assistant.evaluation_runner import execute, safe_private, validate_manifest

CAPS = dict(MAX_CAPS)


def batches(manifest):
    fixture = [case['id'] for case in manifest['scenarios'] if case['mode'] == 'live_fixture']
    result = [(f'language-{i//12+1}', fixture[i:i+12]) for i in range(0,len(fixture),12)]
    for mode in ('live_bigquery','controlled_regression'):
        selected = [case['id'] for case in manifest['scenarios'] if case['mode'] == mode]
        if selected: result.append((mode,selected))
    assert sorted(key for _,group in result for key in group) == sorted(case['id'] for case in manifest['scenarios'])
    return result


def fingerprints():
    return {str(p.relative_to(ROOT)):{'sha256':digest(p),'bytes':p.stat().st_size}
        for p in (ROOT/'runtime').rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--select',nargs='+',help='Whole scenario chains under fresh isolated state; rejects unknown IDs.')
    for name,maximum in MAX_CAPS.items():
        parser.add_argument('--'+name.replace('_','-'),type=float if name=='active_seconds' else int,default=maximum)
    parser.add_argument('--resume',type=Path,help='Marked private directory printed by this run; counters never reset.')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,79}',args.run_id): parser.error('invalid_run_id')
    source = json.loads((ROOT/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json').read_text())
    revision = verify_source(ROOT,source)
    manifest_path = ROOT/'docs/remediation/SCENARIO_MANIFEST.json'
    if digest(manifest_path) != source['scenario_sha256']: parser.error('scenario_hash_mismatch')
    manifest = validate_manifest(json.loads(manifest_path.read_text()))
    caps={name:getattr(args,name) for name in MAX_CAPS}
    selection=sorted(set(args.select)) if args.select else None
    if selection:
        if not set(selection).issubset({c['id'] for c in manifest['scenarios']}): parser.error('unknown_scenario')
        manifest={**manifest,'scenarios':[c for c in manifest['scenarios'] if c['id'] in selection]}
        selected_turns={t['id'] for c in manifest['scenarios'] for t in c['turns']}
        if any(d not in selected_turns for c in manifest['scenarios'] for t in c['turns'] for d in t['depends_on']):
            parser.error('missing_prerequisite_scenario')
    groups = batches(manifest)
    output = ROOT/'docs/remediation/runs'/args.run_id
    settings = Settings.load() if args.live else None
    redactor = Redactor((settings.gemini_api_key,settings.openrouter_api_key) if settings else ())
    identity = {'run_id':args.run_id,'source_revision':revision,'scenario_sha256':source['scenario_sha256'],
        'scenario_version':manifest['version'],'live':args.live,'operator_driver_sha256':digest(Path(__file__)),
        'selection':selection,'review_status':'waived_by_user' if args.live else 'dry_plan','review_sha256':None}
    if args.resume:
        private = safe_private(args.resume)
        if not output.is_dir() or not (output/'plan.json').is_file(): parser.error('missing_original_output_plan')
    else:
        output.mkdir(mode=0o700,exist_ok=False)
        private = Path(tempfile.mkdtemp(prefix='opsfleet-full-evaluation-'))
        os.chmod(private,0o700)
        (private/'evaluation-private-marker').write_text('isolated synthetic evaluation state\n')
    with (private/'runner.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        account = Accounting(private/'checkpoint.json',identity=identity,caps=caps)
        if args.resume:
            plan = json.loads((output/'plan.json').read_text())
            if plan['identity'] != identity or plan['caps'] != caps: parser.error('resume_plan_mismatch')
            before = plan['runtime_before']
        else:
            before = fingerprints()
            plan = {'identity':identity,'caps':caps,'runtime_before':before,
                'selected_model':settings.openrouter_model if settings else None,
                'planned_scenarios':len(manifest['scenarios']),
                'planned_turns':sum(len(case['turns']) for case in manifest['scenarios']),
                'batches':[{'label':label,'scenarios':group} for label,group in groups],
                'created_at_utc':datetime.now(timezone.utc).isoformat(),
                'provider_turn_retries':0,'independent_acceptance':False,
                'authorization':'User requested bounded live evaluation under the existing review waiver.' if args.live else 'Dry plan; no dispatch.'}
            atomic_json(output/'plan.json',redactor(plan))
        if settings and plan['selected_model'] != settings.openrouter_model: parser.error('model_changed_on_resume')
        if fingerprints() != before: parser.error('runtime_changed_before_execution')
        account.state.setdefault('completed_batches',[])
        def save_result(label):
            after = fingerprints()
            verify_source(ROOT,source)
            result = redactor({'kind':'user_authorized_full_evaluation' if args.live else 'dry_plan_no_execution',
                'identity':identity,'caps':caps,'used':account.state['used'],'events':account.state['events'],
                'denials':account.state.get('denials',[]),'stop':account.state['stop'],
                'dry_runs':account.state.get('dry_runs',0),'results':account.state['turns'],
                'counts':totals(account.state['turns']),'runtime_after':after,'runtime_unchanged':after==before,
                'selected_model':plan['selected_model'],'completed_batches':account.state['completed_batches'],
                'cost':None,'limits':['User review waiver is not independent acceptance.',
                    'Fixture warehouse and controlled regressions are not live BigQuery evidence.',
                    'Unknown usage/cost stays unknown; interrupted attempts are never replayed.']})
            # Each projection is immutable; a resumed run can only add snapshots.
            number = 1
            while (output/f'results-batch-{number:03}.json').exists(): number += 1
            atomic_json(output/f'results-batch-{number:03}.json',result)
            atomic_json(output/'results.json',result)
            print(json.dumps({'batch':label,'output':str(output/'results.json'),
                'private_resume_directory':str(private),'counts':result['counts'],'used':result['used'],
                'stop':result['stop'],'denials':result['denials'],'runtime_unchanged':result['runtime_unchanged']}),flush=True)
        print(json.dumps({'plan':str(output/'plan.json'),'private_resume_directory':str(private),
            'live':args.live,'planned_scenarios':plan['planned_scenarios'],'planned_turns':plan['planned_turns'],
            'caps':caps}),flush=True)
        if not args.live:
            execute(account,manifest,private,live=False)
            save_result('dry-plan')
        else:
            for label,selection in groups:
                if label in account.state['completed_batches']: continue
                if account.state['stop']: break
                completed = False
                try:
                    execute(account,manifest,private,live=True,settings=settings,selection=set(selection))
                    account.state['completed_batches'].append(label)
                    completed = True
                finally:
                    account.save()
                    save_result(label if completed else label+'-interrupted')
            if not (output/'results.json').exists(): save_result('stopped')
    return 0


if __name__ == '__main__': raise SystemExit(main())
