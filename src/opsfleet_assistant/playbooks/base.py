"""Checked signed drivers. Warehouse nonnegative/probability rules stay separate."""
from decimal import Decimal, localcontext
from typing import Literal
from pydantic import Field, model_validator
from ..contracts import StrictModel

PRECISION = 50
ABS_TOLERANCE = Decimal('1e-12')
REL_TOLERANCE = Decimal('1e-12')

class Operand(StrictModel):
    evidence_index: int = Field(ge=0, le=1)
    row_index: int = Field(ge=0, le=19)
    metric: str
    value: Decimal

    @model_validator(mode='after')
    def valid(self):
        if not self.value.is_finite() or self.value < 0:
            raise ValueError('invalid_driver_operand')
        return self

class Contribution(StrictModel):
    name: str
    value: Decimal
    percentage: Decimal | None = None

    @model_validator(mode='after')
    def finite_signed(self):
        if not self.value.is_finite() or self.percentage is not None and not self.percentage.is_finite():
            raise ValueError('invalid_signed_contribution')
        return self

class DriverAction(StrictModel):
    contribution: str
    trigger: str
    proposal: str
    measure: str

class DriverFinding(StrictModel):
    schema_version: Literal[1] = 1
    playbook_id: Literal['product_revenue', 'regional_spend', 'churn_change']
    method_version: Literal[1] = 1
    direction: str
    operands: tuple[Operand, ...] = ()
    total_difference: Decimal | None = None
    contributions: tuple[Contribution, ...] = ()
    residual: Decimal | None = None
    absolute_tolerance: Decimal = ABS_TOLERANCE
    relative_tolerance: Decimal = REL_TOLERANCE
    tolerance: Decimal | None = None
    precision: Literal[50] = PRECISION
    complete: bool = False
    availability_reason: Literal['available', 'incomplete_evidence', 'missing_population', 'incompatible_evidence',
                                'undefined_denominator', 'reconciliation_failed', 'segment_unavailable']
    conclusion: str
    actions: tuple[DriverAction, ...] = ()

    @model_validator(mode='after')
    def signed(self):
        for value in (self.total_difference, self.residual, self.tolerance):
            if value is not None and not value.is_finite():
                raise ValueError('invalid_driver_value')
        if self.complete != (self.availability_reason == 'available'):
            raise ValueError('invalid_driver_completeness')
        if self.complete and (self.residual is None or self.tolerance is None or abs(self.residual) > self.tolerance):
            raise ValueError('unreconciled_driver')
        return self

def number(evidence, row, metric, index=0):
    value = Decimal(str(evidence.rows[row][metric]))
    return Operand(evidence_index=index, row_index=row, metric=metric, value=value)

def unavailable(id, direction, reason, text):
    return DriverFinding(playbook_id=id, direction=direction, availability_reason=reason,
                         conclusion=text)

def finish(id, direction, operands, difference, parts, conclusion, actions):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        residual = difference - sum(parts.values(), Decimal(0))
        tolerance = max(ABS_TOLERANCE, REL_TOLERANCE * max(abs(difference), *(abs(v) for v in parts.values())))
        okay = abs(residual) <= tolerance
        contributions = tuple(Contribution(name=k, value=v,
            percentage=v / difference * 100 if abs(difference) > tolerance else None) for k,v in parts.items())
    return DriverFinding(playbook_id=id, direction=direction, operands=tuple(operands),
        total_difference=difference, contributions=contributions, residual=residual, tolerance=tolerance,
        complete=okay, availability_reason='available' if okay else 'reconciliation_failed',
        conclusion=conclusion if okay else 'Attribution unavailable: reconciliation failed.',
        actions=tuple(actions) if okay else ())

def text(finding):
    lines = [f'{finding.playbook_id} v{finding.method_version}: {finding.direction}. {finding.conclusion}',
             f'Attribution: {finding.availability_reason}.']
    if finding.total_difference is not None and finding.contributions:
        scale = Decimal(100) if finding.playbook_id == 'churn_change' else Decimal(1)
        unit = ' percentage points' if scale == 100 else ''
        lines.append(f'Difference {finding.total_difference * scale:+.2f}{unit}; ' +
            '; '.join(f'{c.name}: {c.value * scale:+.2f}{unit}' for c in finding.contributions) +
            f'; residual {finding.residual}; tolerance {finding.tolerance}.')
        if any(c.percentage is None for c in finding.contributions):
            lines.append('Contribution percentages unavailable for a zero or near-zero difference.')
    lines += [f'Proposed next investigation [{a.contribution}]: {a.trigger} {a.proposal} Measure to check: {a.measure}.' for a in finding.actions]
    return '\n'.join(lines)
