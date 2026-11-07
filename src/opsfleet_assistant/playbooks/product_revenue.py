"""Symmetric item-volume / average-item-price identity, version 1."""
from decimal import localcontext
from .base import PRECISION, DriverAction, number, unavailable, finish

def investigate(evidence):
    e = evidence[0]
    ids = e.execution_scope.product_ids if e.execution_scope else None
    direction = f'product {ids[0]} minus product {ids[1]}' if ids and len(ids)==2 else 'requested A minus B'
    if not e.complete:
        return unavailable('product_revenue', direction, 'incomplete_evidence', 'Complete product evidence is required; no measured decomposition.')
    if not ids or len(e.rows)!=2 or {r['product_id'] for r in e.rows} != set(ids):
        return unavailable('product_revenue', direction, 'missing_population', 'Both requested products must have qualifying items.')
    a,b = [next(i for i,r in enumerate(e.rows) if r['product_id']==id) for id in ids]
    operands = [number(e,i,m) for i in (a,b) for m in ('revenue','item_count')]
    ra,na,rb,nb = [o.value for o in operands]
    if na == 0 or nb == 0 or any(e.rows[i].get('average_item_price') is None for i in (a,b)):
        return unavailable('product_revenue', direction, 'undefined_denominator', 'Average item price is undefined; no measured decomposition.')
    with localcontext() as ctx:
        ctx.prec=PRECISION
        pa,pb=ra/na,rb/nb
        parts={'item_volume': (na-nb)*(pa+pb)/2, 'average_item_price': (pa-pb)*(na+nb)/2}
        dominant=max(parts,key=lambda k:abs(parts[k]))
        action=DriverAction(contribution=dominant, trigger=f'The measured {dominant} component has the largest absolute size.',
            proposal='Propose checking assortment availability and acquisition patterns.' if dominant=='item_volume' else 'Propose checking the qualifying price and item mix.',
            measure='qualifying item counts and average item prices in the same scope and UTC period')
        relation = ('Higher qualifying item volume offsets lower average item price. ' if na > nb and pa < pb and ra > rb
                    else 'Lower qualifying item volume is offset by higher average item price. ' if na < nb and pa > pb and ra < rb else '')
        return finish('product_revenue',direction,operands,ra-rb,parts,
            relation + 'Item volume and average item price arithmetically explain the revenue difference; opposing components can exceed the net difference. This does not establish a cause. [E1]',[action])
