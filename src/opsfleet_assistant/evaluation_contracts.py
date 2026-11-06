"""Evaluation identities, durable reservations, independent assertions and redaction.

Private checkpoint is authoritative; public results are a sanitized projection.
No result is promoted to independent acceptance by these implementation checks.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

STATUSES = ('PASS','FAIL','PROVIDER_FAILURE','BLOCKED_ENVIRONMENT','NOT_RUN')
MAX_CAPS = {'turns':120, 'provider_attempts':240, 'warehouse_executions':40,
            'billed_bytes':5*1024**3, 'active_seconds':2700}


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(files):
    return hashlib.sha256(''.join(f'{files[p]}  {p}\n' for p in sorted(files)).encode('utf-8')).hexdigest()


def verify_source(root, manifest):
    if canonical(manifest['files']) != manifest['revision']: raise ValueError('source_aggregate_mismatch')
    for p, h in manifest['files'].items():
        target = (root/p).resolve()
        if not target.is_relative_to(root.resolve()) or digest(target) != h:
            raise ValueError('source_path_mismatch')
    return manifest['revision']


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.next')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


class StopScheduling(Exception):
    pass


class Accounting:
    def __init__(self, path, *, identity=None, caps=None, clock=time.monotonic):
        self.path, self.clock = Path(path), clock
        self.tick = clock()
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if identity != self.state['identity'] or (caps is not None and caps != self.state['caps']):
                raise ValueError('resume_identity_or_caps_mismatch')
            # In-flight dispatch is never replayed after a crash. Its billing is
            # unknown; retain its reservation and halt all further scheduling.
            if self.state['inflight']:
                self.state['stop'] = 'interrupted_dispatch_unknown_billing'
            if self.state.get('active_turn'):
                self.state['used']['active_seconds'] += self.state.pop('wall_reservation', 0)
                self.state['stop'] = self.state['stop'] or 'interrupted_turn_requires_report'
        else:
            chosen = caps or dict(MAX_CAPS)
            if set(chosen) != set(MAX_CAPS) or any(type(chosen[k]) not in (int,float) or
                not math.isfinite(chosen[k]) or chosen[k] <= 0 or chosen[k] > MAX_CAPS[k] or
                (k != 'active_seconds' and type(chosen[k]) is not int) for k in MAX_CAPS):
                raise ValueError('invalid_caps')
            self.state = {'schema_version':1,'identity':identity,'caps':chosen,
                'used':dict.fromkeys(MAX_CAPS,0),'stop':None,'inflight':{},'events':[],
                'turns':[], 'active_turn':None, 'wall_reservation':0}
        self.save()

    def save(self): atomic_json(self.path, self.state)

    def elapsed(self):
        now = self.clock(); self.state['used']['active_seconds'] += max(0,now-self.tick); self.tick = now

    def check(self, name=None, amount=0):
        self.elapsed()
        if self.state['stop']: raise StopScheduling(self.state['stop'])
        if self.state['used']['active_seconds'] >= self.state['caps']['active_seconds']:
            self.state['stop']='active_time_cap'; self.save(); raise StopScheduling('active_time_cap')
        if name and self.state['used'][name]+amount > self.state['caps'][name]:
            self.state.setdefault('denials',[]).append({'turn':self.state['active_turn'],'reason':name+'_cap'})
            self.save(); raise StopScheduling(name+'_cap')

    def begin_turn(self, turn_id, *, record=None):
        self.check('turns',1)
        self.state['used']['turns'] += 1
        self.state['active_turn'] = turn_id
        self.state['wall_reservation'] = min(120,self.state['caps']['active_seconds']-self.state['used']['active_seconds'])
        if record is not None:
            record.update(status='BLOCKED_ENVIRONMENT', reason='started_outcome_pending', attempts=[{
                'ordinal':1, 'attempt_id':f"{self.state['identity']['run_id']}:{turn_id}:1",
                'outcome':'STARTED', 'elapsed_seconds':None,
                'observed':{'category':'started_outcome_pending'}, 'assertions':[], 'events':[]}])
        self.save()

    def reconcile_interrupted(self, manifest):
        """Recover started work even when an older checkpoint has a NOT_RUN row.

        Dispatch events and the active marker are durable evidence of an attempt.
        Neither an empty result row nor a previous batch selection can erase it.
        """
        definitions = {t['id']:(c,t) for c in manifest['scenarios'] for t in c['turns']}
        rows = {r['turn_id']:r for r in self.state['turns']}
        started = {r['turn_id'] for r in rows.values()
                   if any(a.get('outcome')=='STARTED' for a in r['attempts'])}
        if self.state.get('active_turn'): started.add(self.state['active_turn'])
        started.update(e['turn'] for e in self.state['events']
                       if e.get('turn') in definitions and not rows.get(e['turn'],{}).get('attempts'))
        for key in started:
            case, turn = definitions[key]
            row = rows.setdefault(key, {'scenario':case['id'],'turn_id':key,'mode':case['mode'],
                                        'expected':turn['assertions'],'attempts':[]})
            if row['attempts'] and row['attempts'][0].get('outcome') not in {'STARTED',None}:
                continue
            row.update(status='BLOCKED_ENVIRONMENT',reason='interrupted_turn_not_replayed')
            row['attempts'] = [{'ordinal':1,'attempt_id':f"{self.state['identity']['run_id']}:{key}:1",
                'outcome':'INTERRUPTED','elapsed_seconds':None,
                'observed':{'category':'interrupted_unknown_outcome','usage':None,'cost':None},
                'assertions':[], 'events':[e['id'] for e in self.state['events'] if e['turn']==key]}]
            self.state['stop'] = self.state['stop'] or 'interrupted_turn_requires_report'
        self.state['turns'] = list(rows.values())
        self.state['active_turn'] = None
        self.state['wall_reservation'] = 0
        self.save()

    def end_turn(self):
        self.elapsed()
        if self.state['inflight']: self.state['stop']='unsettled_dispatch_unknown_outcome'
        self.state['active_turn']=None; self.state['wall_reservation']=0; self.save()

    def reserve(self, kind, **details):
        counter = 'provider_attempts' if kind == 'provider' else 'warehouse_executions'
        self.check(counter,1)
        if kind == 'warehouse':
            available = self.state['caps']['billed_bytes']-self.state['used']['billed_bytes']-sum(
                e.get('cap',0) for e in self.state['inflight'].values() if e['kind']=='warehouse')
            if available <= 0:
                self.state.setdefault('denials',[]).append({'turn':self.state['active_turn'],'reason':'billed_bytes_cap'})
                self.save(); raise StopScheduling('billed_bytes_cap')
            details['cap'] = min(details['cap'],available)
        event = {'id':len(self.state['events'])+1,'turn':self.state['active_turn'],
                 'kind':kind,'outcome':'dispatched','usage':None,'cost':None,**details}
        self.state['used'][counter] += 1
        self.state['events'].append(event)
        self.state['inflight'][str(event['id'])] = event
        self.save()  # durable before network dispatch
        return event

    def finish(self, event, *, billed=None, **details):
        if event['kind']=='warehouse' and str(event['id']) not in self.state['inflight']: return
        event.update(details)
        if event['kind']=='warehouse':
            if type(billed) is not int or billed < 0:
                self.state['stop']='unknown_warehouse_billing'
                event['billed_bytes']=None
            else:
                self.state['used']['billed_bytes'] += billed
                event['billed_bytes']=billed
                if billed > event['cap']: self.state['stop']='warehouse_billing_exceeded_reservation'
        self.state['inflight'].pop(str(event['id']),None)
        self.elapsed(); self.save()


class Redactor:
    def __init__(self, secrets=()): self.secrets=tuple(s for s in secrets if s)
    def __call__(self, value):
        if isinstance(value, dict):
            return {k: ('<REDACTED>' if re.search(r'(api_key|authorization|token_digest|confirmation_token|raw_error)',k,re.I)
                        else self(v)) for k,v in value.items()}
        if isinstance(value,(tuple,list)): return [self(v) for v in value]
        if not isinstance(value,str): return value
        for s in self.secrets: value=value.replace(s,'<REDACTED>')
        value=re.sub(r'(?<![\w-])[A-Za-z0-9_-]{43}(?![\w-])','<TOKEN>',value)
        value=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '<EMAIL>',value)
        value=re.sub(r'(?i)(?:bearer\s+|sk-[A-Za-z0-9_-]*|AIza[A-Za-z0-9_-]*)[^\s"<>]*', '<CREDENTIAL>',value)
        value=re.sub(r'\b\d{3}-\d{2}-\d{4}\b|\+\d[\d ()-]{8,}\d', '<PII>',value)
        for s in ('Synthetic Person Sentinel','123 Synthetic Street'): value=value.replace(s,'<PII>')
        return value


def lookup(obj,path):
    if not path: return obj
    for part in path.split('.'):
        obj = obj[int(part)] if isinstance(obj,(tuple,list)) else obj[part]
    return obj


def assertions(expected, observed, previous=None):
    results=[]
    for rule in expected:
        actual=None
        try:
            condition=rule.get('when')
            if condition:
                value=lookup(observed,condition['path'])
                applies=(value==condition['eq'] if 'eq' in condition else value>condition['gt'])
                if not applies:
                    results.append({'rule':rule,'passed':True,'actual':None,'applicable':False})
                    continue
            actual=lookup(observed,rule['path'])
            if 'catalog_products' in rule:
                target=[observed['catalog_bindings'][key] for key in rule['catalog_products']]
            else:
                target = lookup(previous,rule['previous']) if 'previous' in rule else rule.get('value')
            op=rule['op']
            if op=='eq': ok=actual==target
            elif op=='in': ok=actual in target
            elif op=='contains': ok=target in actual
            elif op=='absent': ok=target not in actual
            elif op=='max': ok=actual <= target
            elif op=='min': ok=actual >= target
            elif op=='set_eq': ok=sorted(actual)==sorted(target)
            elif op=='decimal': ok=actual is not None and abs(Decimal(str(actual))-Decimal(target)) <= Decimal(rule.get('tolerance','0'))
            elif op=='finding':
                ok=any(f['selection']['operation']==target['operation'] and
                       f['selection']['current']['metric']==target['metric'] and
                       all(f['selection'][side][key]==value for side in ('current','baseline')
                           for key,value in target.get(side,{}).items()) and
                       all(f.get(key) is not None and Decimal(f[key])==Decimal(target[key])
                           for key in ('current_value','baseline_value') if key in target) and
                       (f.get('value') is None if target['value'] is None else
                        f.get('value') is not None and abs(Decimal(f['value'])-Decimal(target['value'])) <= Decimal(rule.get('tolerance','0')))
                       for f in actual)
            else: raise ValueError('unknown_assertion')
        except (KeyError,IndexError,TypeError,ValueError,ArithmeticError): ok=False
        results.append({'rule':rule,'passed':bool(ok),'actual':actual})
    return results


def totals(rows):
    turns=Counter(r['status'] for r in rows)
    cases={}
    priority = ('FAIL','PROVIDER_FAILURE','BLOCKED_ENVIRONMENT','NOT_RUN','PASS')
    for row in rows: cases.setdefault(row['scenario'],[]).append(row)
    scenario_counts=Counter(next(s for s in priority if any(r['status']==s for r in rs)) for rs in cases.values())
    return {'turns':{s:turns[s] for s in STATUSES}, 'scenarios':{s:scenario_counts[s] for s in STATUSES},
            'planned_turns':len(rows),'attempted_turns':sum(bool(r['attempts']) for r in rows),
            'planned_scenarios':len(cases),'attempted_scenarios':sum(any(r['attempts'] for r in rs) for rs in cases.values())}
