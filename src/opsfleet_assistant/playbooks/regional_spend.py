"""Spend per qualifying purchaser: frequency / order-value identity v1."""
from decimal import localcontext
from .base import PRECISION, DriverAction, number, unavailable, finish

def investigate(evidence):
    e=evidence[0]
    values=e.metric_definition.get('demographic_values')
    direction=f'{values[0]} minus {values[1]}' if values and len(values)==2 else 'requested region A minus B'
    if not e.complete or len(e.rows)!=2:
        return unavailable('regional_spend',direction,'incomplete_evidence',
            'A requested purchaser cohort is absent or withheld. No zero, absence, hidden total, or comparison is inferred.')
    if not values or {r['demographic_value'] for r in e.rows}!=set(values):
        return unavailable('regional_spend',direction,'incompatible_evidence','The requested state cohorts do not match.')
    a,b=[next(i for i,r in enumerate(e.rows) if r['demographic_value']==v) for v in values]
    operands=[number(e,i,m) for i in (a,b) for m in ('customers','order_count','revenue')]
    ca,oa,ra,cb,ob,rb=[o.value for o in operands]
    if not all((ca,oa,cb,ob)):
        return unavailable('regional_spend',direction,'undefined_denominator','Spend/frequency/order value is undefined.')
    with localcontext() as ctx:
        ctx.prec=PRECISION
        fa,fb,va,vb=oa/ca,ob/cb,ra/oa,rb/ob
        parts={'order_frequency':(fa-fb)*(va+vb)/2, 'order_value':(va-vb)*(fa+fb)/2}
        dominant=max(parts,key=lambda k:abs(parts[k]))
        return finish('regional_spend',direction,operands,ra/ca-rb/cb,parts,
            f'Qualifying purchasers are the denominator: {ca} versus {cb}; spend per purchaser {ra/ca:.2f} versus {rb/cb:.2f}. '
            f'Separately, revenue difference {ra-rb:+.2f}; purchaser-count difference {ca-cb:+.0f}. '
            'The frequency/order-value identity is an arithmetic explanation, not a demographic cause. [E1]',
            [DriverAction(contribution=dominant,trigger=f'The measured {dominant} component has the largest absolute size.',
                proposal='Propose reviewing repeat-purchase patterns and scoped basket composition in these states.',
                measure='distinct qualifying orders per purchaser and scoped revenue per distinct qualifying order')])
