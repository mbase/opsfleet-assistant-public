"""Small trusted registry. No dynamic loading or model-authored methods."""
from dataclasses import dataclass
from ..contracts import Operation
from ..identity import PolicyError
from . import product_revenue, regional_spend, churn_change
from .base import DriverFinding, text

@dataclass(frozen=True)
class Definition:
    id: str
    version: int
    operation: Operation
    required: tuple[str, ...]
    findings: tuple[str, ...]
    completeness: str
    max_evidence: int = 1
    max_rows: int = 2
    normal_executions: int = 3

REGISTRY = {'churn_change': Definition('churn_change',1,Operation.CHURN_CHANGE,
    ('current_cutoff','earlier_cutoff','country'),('population_mix','within_segment'), 'full aligned safe country population; reconciled rates', max_rows=20),
    'regional_spend': Definition('regional_spend',1,Operation.REGIONAL_SPEND,
    ('two_ordered_states','period','qualifying_purchasers'),('order_frequency','order_value'), 'both released cohorts; complete evidence; reconciled identity'),
    'product_revenue': Definition('product_revenue',1,Operation.PRODUCT_COMPARISON,
    ('two_ordered_product_ids','period'),('item_volume','average_item_price'), 'both qualifying products; complete evidence; reconciled identity')}

def validate_request(request):
    spec=request.spec
    choice=spec.playbook
    definition=REGISTRY.get(choice.id)
    if (definition is None or len(spec.queries)!=1 or spec.mode not in {'analysis','report'}
            or spec.purpose!='standard' or spec.queries[0].operation!=definition.operation):
        raise PolicyError('plan_intent_mismatch')
    q=spec.queries[0]
    if choice.id=='churn_change' and (choice.segment!='country' or choice.population!='qualifying_purchasers'
            or spec.comparison is None or request.comparison is None or request.comparison.end>=request.analysis.end):
        raise PolicyError('plan_intent_mismatch')
    if choice.id=='regional_spend' and (choice.population!='qualifying_purchasers' or choice.segment
            or q.dimension!='state' or q.demographic_values is None or spec.comparison is not None):
        raise PolicyError('plan_intent_mismatch')
    if choice.id=='product_revenue' and (choice.population or choice.segment or q.product_ids is None
            or len(q.product_ids)!=2 or spec.comparison is not None):
        raise PolicyError('plan_intent_mismatch')

def investigate(evidence):
    choices = [e.metric_definition.get('playbook') for e in evidence]
    if not any(choices):
        return None
    if len(evidence)!=1 or not choices[0]:
        raise PolicyError('invalid_result_contract')
    id=choices[0]['id']
    if id=='churn_change':
        return churn_change.investigate(evidence)
    if id=='regional_spend':
        return regional_spend.investigate(evidence)
    if id=='product_revenue':
        return product_revenue.investigate(evidence)
    raise PolicyError('invalid_result_contract')
