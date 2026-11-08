"""Fixed orchestration messages and safe failure codes."""
SCHEMA_HELP = (
    "Available sources: orders (order ID, customer ID, status), order_items "
    "(order ID, product ID, item status, sale price, creation time), products "
    "(product ID, brand), and users (aggregate state, city, country, age, gender, "
    "traffic source). Orders join to items by order ID, items to products by "
    "product ID, and orders to users by customer ID. Supported analyses include "
    "scoped revenue, customer ranking, monthly trends, product comparison, "
    "aggregate demographics, and inactivity churn. Investigations: product revenue price/volume; "
    "state qualifying-purchaser spend frequency/order value; two-cutoff inactivity churn with safe country attribution. "
    "These measured components do not establish causes. Names, contact details, "
    "addresses, exact coordinates, and demographic membership IDs are unavailable."
)


SAFE_ERROR_CODES = frozenset({
    "aggregate_only_source_field", "backup_failure", "backup_transient", "backup_unconfigured",
    "billing_unknown", "brand_not_available", "compiler_syntax_error", "coverage_invalid",
    "customer_id_violation", "demographic_complement_violation", "demographic_group_too_small",
    "demographic_membership_blocked", "demographic_membership_violation", "direct_failure",
    "direct_transient", "direct_unconfigured", "dry_run_limit", "empty_model_output",
    "evidence_scope_mismatch", "idempotency_conflict", "incomplete_comparison",
    "instruction_attack_blocked", "internal_failure", "invalid_catalog", "invalid_identity_config", "invalid_job_metadata",
    "invalid_model_json", "invalid_interpretation_contract", "invalid_parameter", "invalid_plan_contract", "invalid_proposal",
    "invalid_question", "invalid_report_contract", "invalid_request", "invalid_result_contract",
    "invalid_selector", "job_byte_limit", "missing_project", "model_request_limit",
    "permission_denied", "pii_request_blocked", "plan_intent_mismatch", "plan_investigation_mismatch",
    "plan_limit_mismatch", "plan_metric_mismatch", "plan_period_mismatch", "plan_product_mismatch",
    "plan_repair_limit", "plan_scope_mismatch", "plan_status_mismatch", "product_not_available",
    "prohibited_source_field", "prompt_limit", "provider_failure", "provider_transient",
    "query_count_limit", "query_provider_rejected", "query_schema_error", "query_syntax_error",
    "recovery_scope_changed", "report_deleted", "result_empty_violation", "result_limit",
    "result_null_violation", "result_or_billing_limit", "result_schema_violation", "result_scope_violation",
    "result_type_violation", "saved_scope_changed", "scope_changed_start_new_conversation",
    "selection_too_broad", "store_unavailable", "timeout_cancellation_requested", "truncated_model_output",
    "turn_byte_limit", "turn_timeout", "warehouse_timeout", "unauthorized_brand", "unauthorized_scope",
    "unknown_identity", "unsafe_report_text", "unsafe_result_text", "unsupported_cohort_reference",
    "unsupported_operation", "unsupported_recommendation", "unsupported_request", "untrusted_compiled_query",
})
