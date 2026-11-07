"""Two prior-purchaser snapshots, complete aligned country attribution v1."""
from decimal import Decimal, localcontext
from ..identity import PolicyError
from .base import PRECISION, DriverAction, number, unavailable, finish

def validate_rows(rows):
    if not rows:
        return
    keys=[(r['cutoff_role'],r['segment_key']) for r in rows]
    if len(keys)!=len(set(keys)) or any(role not in {'current','baseline'} or
        not (key in {'H:','U:'} or key.startswith('C:') and len(key)>2) for role,key in keys):
        raise PolicyError('result_type_violation')
    heads={r['cutoff_role']:r for r in rows if r['segment_key']=='H:'}
    if set(heads)!={'current','baseline'}:
        raise PolicyError('result_type_violation')
    for row in rows:
        c,n=row['eligible_customers'],row['inactive_customers']
        if n<5 or c-n<5 or row['churn_rate'] is None:
            raise PolicyError('demographic_group_too_small')
        with localcontext() as ctx:
            ctx.prec=PRECISION
            if abs(Decimal(str(row['churn_rate']))-Decimal(n)/c)>Decimal('1e-12'):
                raise PolicyError('result_type_violation')
    segments=[r for r in rows if r['segment_key']!='H:']
    if segments:
        aligned=[{r['segment_key'] for r in segments if r['cutoff_role']==role} for role in ('current','baseline')]
        if aligned[0]!=aligned[1] or len(aligned[0])>9:
            raise PolicyError('incomplete_comparison')
        for role,head in heads.items():
            for metric in ('eligible_customers','inactive_customers'):
                if sum(r[metric] for r in segments if r['cutoff_role']==role)!=head[metric]:
                    raise PolicyError('incomplete_comparison')

def investigate(evidence):
    e=evidence[0]
    direction=f"current cutoff {e.metric_definition.get('cutoff_date')} minus baseline cutoff {e.metric_definition.get('baseline_cutoff_date')}"
    heads={r['cutoff_role']:(i,r) for i,r in enumerate(e.rows) if r['segment_key']=='H:'}
    if set(heads)!={'current','baseline'}:
        return unavailable('churn_change',direction,'missing_population','Safe headline comparison unavailable; no rate change established.')
    ci,cur=heads['current'];bi,base=heads['baseline']
    with localcontext() as ctx:
        ctx.prec=PRECISION
        current=Decimal(cur['inactive_customers'])/cur['eligible_customers']
        baseline=Decimal(base['inactive_customers'])/base['eligible_customers']
        delta=current-baseline
        premise='Inactivity rate rose.' if delta>0 else 'The claimed spike is not supported: rate was flat or falling.'
        conclusion=(f"{premise} Current {cur['inactive_customers']} inactive / {cur['eligible_customers']} eligible ({current*100:.2f}%); "
            f"baseline {base['inactive_customers']} / {base['eligible_customers']} ({baseline*100:.2f}%); change {delta*100:+.2f} percentage points. "
            f"Inactive-count change {cur['inactive_customers']-base['inactive_customers']:+d}; eligible-count change {cur['eligible_customers']-base['eligible_customers']:+d}. "
            'These cutoffs may contain different eligible populations; this does not track newly churned people, individual transitions, or causal retention effects. '
            'Inactivity is at least 90 days; current statuses can restate history. [E1]')
        head_operands=tuple(number(e,i,m) for i,_ in heads.values() for m in ('eligible_customers','inactive_customers'))
        if not e.complete or len(e.rows)<=2:
            result = unavailable('churn_change',direction,'segment_unavailable',conclusion+
                ' Country attribution unavailable: segments are empty, withheld, unmatched, or over the row limit. No segment operands or residual released.')
            return result.model_copy(update={'operands':head_operands, 'total_difference':delta})
        operands=[number(e,i,m) for i,_ in heads.values() for m in ('eligible_customers','inactive_customers')]
        segments={r['segment_key'] for r in e.rows if r['segment_key']!='H:'}
        mix=within=Decimal(0)
        for key in sorted(segments):
            pairs=[next((i,r) for i,r in enumerate(e.rows) if r['segment_key']==key and r['cutoff_role']==role) for role in ('current','baseline')]
            (ai,a),(bi,b)=pairs
            operands += [number(e,i,m) for i,_ in pairs for m in ('eligible_customers','inactive_customers')]
            sa=Decimal(a['eligible_customers'])/cur['eligible_customers'];sb=Decimal(b['eligible_customers'])/base['eligible_customers']
            ra=Decimal(a['inactive_customers'])/a['eligible_customers'];rb=Decimal(b['inactive_customers'])/b['eligible_customers']
            mix+=(sa-sb)*(ra+rb)/2
            within+=(ra-rb)*(sa+sb)/2
        parts={'population_mix':mix,'within_segment':within}
        dominant=max(parts,key=lambda k:abs(parts[k]))
        return finish('churn_change',direction,operands,delta,parts,conclusion,
            [DriverAction(contribution=dominant,trigger=f'The measured {dominant} component has the largest absolute size.',
                proposal='Propose examining repeat-purchase timing within the same country definitions and authorized scope.',
                measure='eligible population shares and 90-day inactivity rates at comparable cutoffs')])
