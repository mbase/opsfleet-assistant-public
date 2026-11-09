"""Provider-independent, fail-closed recovery policy for increment 1A."""
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class SubmissionState(StrEnum):
    NOT_SUBMITTED = 'not_submitted'
    SUBMITTED = 'submitted'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class FailureContext:
    category: str
    stage: str
    submission: SubmissionState = SubmissionState.UNKNOWN
    retries: int = 0
    budget_available: bool = False
    deadline_available: bool = False
    schema_inspection_available: bool = False
    syntax_correction_available: bool = False


@dataclass(frozen=True)
class RecoveryDecision:
    action: Literal["stop", "retry_before_submission", "inspect_schema", "recompile_supported_binding", "correct_syntax"]
    reason: str


@dataclass(frozen=True)
class RecoveryOutcome:
    action: Literal["stop", "retry_before_submission", "inspect_schema", "recompile_supported_binding", "correct_syntax"]
    reason: str
    outcome: Literal["attempted", "success", "failed", "unsupported", "exhausted"]
    inputs_changed: bool = False


def decide_recovery(context: FailureContext) -> RecoveryDecision:
    if context.category == "billing_unknown":
        return RecoveryDecision("stop", "billing_uncertain")
    if (context.category in {"turn_timeout", "warehouse_timeout", "query_count_limit", "dry_run_limit",
                             "turn_byte_limit", "job_byte_limit", "result_or_billing_limit", "model_request_limit"}
            or not context.budget_available or not context.deadline_available):
        return RecoveryDecision('stop', 'exhausted')
    if context.submission != SubmissionState.NOT_SUBMITTED:
        return RecoveryDecision('stop', 'submission_unsafe')
    if (context.category == 'query_syntax_error' and context.stage == 'dry_run'
            and context.syntax_correction_available):
        return RecoveryDecision('correct_syntax', 'syntax')
    if context.category == 'query_schema_error' and context.schema_inspection_available:
        return RecoveryDecision('inspect_schema', 'schema_rejection')
    if context.category in {'query_schema_error', 'query_syntax_error', 'compiler_syntax_error'}:
        return RecoveryDecision('stop', 'correction_unavailable')
    if (context.category == 'provider_transient' and context.stage in {'dry_run', 'metadata'}
            and context.retries >= 2):
        return RecoveryDecision('stop', 'retry_limit')
    if (context.category == 'provider_transient' and context.stage in {'dry_run', 'metadata'}
            and context.retries < 2):
        return RecoveryDecision('retry_before_submission', 'transient_before_submission')
    return RecoveryDecision('stop', 'not_retryable')


def terminal_outcome(decision: RecoveryDecision) -> str:
    if decision.reason in {'exhausted', 'retry_limit'}:
        return 'exhausted'
    if decision.reason == 'correction_unavailable':
        return 'unsupported'
    return 'failed'
