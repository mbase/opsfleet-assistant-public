"""Shared durable replay/disclosure validation; no I/O or prose regeneration."""
from dataclasses import dataclass
import hashlib
import json

from .compiler import compile_plan
from .contracts import AnalysisPlan
from .conversation import validate_cached_evidence
from .identity import PolicyError, effective_brands, authorize_products
from .privacy import FIELD_POLICY_VERSION
from .report_artifact import ReportArtifact, artifact_binding_digest, validate_artifact
from .reporting import GroundedReport, ReportComposition


@dataclass(frozen=True)
class ValidatedReport:
    artifact: ReportArtifact | None
    grounded: GroundedReport | None
    composition: ReportComposition | None
    completeness: str


def validate_saved_report(fields, scope, catalog, *, field_policy_version=FIELD_POLICY_VERSION) -> ValidatedReport:
    """Current entire execution scope plus existing supported artifact readers.

    Genuine pre-binding ordinary rows retain exact text only when stored plans
    establish current scope. New versions cannot downgrade through missing support.
    """
    owner = fields['owner']
    plans = tuple(AnalysisPlan.model_validate(p) for p in json.loads(fields['plans_json']))
    if fields['scope_reference'] != f'demo:{owner}' or owner != scope.user_id or fields['scope_version'] != scope.authorization_version or not plans or len(plans) > 2:
        raise PolicyError('saved_scope_changed')
    queries = tuple(compile_plan(p, scope, catalog) for p in plans)
    digest = hashlib.sha256(json.dumps([list(q.effective_brands) for q in queries], separators=(',', ':')).encode()).hexdigest()
    if digest != fields['scope_digest']:
        raise PolicyError('saved_scope_changed')
    grounded = GroundedReport.model_validate_json(fields['grounded_report_json']) if fields['grounded_report_json'] else None
    durable = bool(fields['report_support_json'] or fields['report_binding_digest'])
    if not durable:
        definitions = json.loads(fields['metric_definitions_json'])
        composition = json.loads(fields['report_composition_json']) if fields['report_composition_json'] else {}
        if (any(p.version != 1 or p.playbook is not None or p.operation in {'regional_spend', 'churn_change'} for p in plans)
                or grounded and (grounded.schema_version != 1 or grounded.driver is not None)
                or any(d.get('playbook') is not None for d in definitions)
                or composition.get('request', {}).get('spec', {}).get('playbook') is not None):
            raise PolicyError('invalid_result_contract')
        return ValidatedReport(None, grounded, None, 'legacy')
    if not fields['report_support_json'] or artifact_binding_digest(fields) != fields['report_binding_digest']:
        raise PolicyError('invalid_result_contract')
    artifact = ReportArtifact.model_validate_json(fields['report_support_json'])
    composition = ReportComposition.model_validate_json(fields['report_composition_json'])
    validate_artifact(fields, artifact, composition, grounded)
    # Request operands also require authorization, even when derived execution
    # plans do not directly carry every requested choice.
    for operand in artifact.snapshot.request.spec.queries:
        authorize_products(operand.product_ids, effective_brands(scope, operand.requested_brands, catalog), catalog)
    for query, item in zip(queries, artifact.snapshot.evidence):
        if (item.metric_definition != query.metric_definition or item.columns != query.result_contract.columns
                or item.scope_reference != f'demo:{owner}' or item.scope_version != query.scope_version
                or item.field_policy_version != field_policy_version or item.execution_scope is None
                or item.execution_scope.brands != query.effective_brands
                or item.execution_scope.product_ids != query.requested_product_ids):
            raise PolicyError('invalid_result_contract')
        validate_cached_evidence(query, item)
    return ValidatedReport(artifact, grounded, composition, composition.completeness)
