# Example interactions

Commands below are usage templates unless explicitly labeled as historical observations or offline fixtures. Replace uppercase conversation/token/path placeholders with returned values. Live analytical commands require credentials and can incur model/warehouse charges. Public dataset values change over time; historical numbers are not expected test constants.

## Local help and offline demo

After [installation](README.md#install-and-configure):

```sh
opsfleet --help
opsfleet schema
PYTHONDONTWRITEBYTECODE=1 python scripts/step6_offline_cli_demo.py
```

Help/schema are local. The demo injects model/warehouse fixtures and uses temporary synthetic SQLite plus isolated telemetry. It exercises four cases: approved Golden match, no match, retired example exclusion and unauthorized example exclusion. Printed routes, jobs and values are simulations. See [test_step6.py](tests/test_step6.py) for independently specified behavior.

## Analysis and contextual correction

Illustrative commands; use the conversation ID returned by the first turn and the same disposable database throughout:

```sh
opsfleet ask 'Revenue in 2025' --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE --json
opsfleet ask 'Use Complete status only' --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE --conversation CONVERSATION_ID --json
opsfleet ask 'Use February 2024 instead' --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE --conversation CONVERSATION_ID --json
opsfleet ask 'Why did that happen?' --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE --conversation CONVERSATION_ID
```

Inspect the selected metric/statuses, effective brand/product scope, UTC analysis/comparison/action roles, original evidence as-of and completeness. A month without a usable year clarifies. An explanation cannot turn correlation into established causation. Corrections retain unrelated choices; expired context requests a fresh analysis. Default output is an executive view; `--verbose` adds diagnostics and `--json` retains structured full-precision findings.

Historical live observation, 3 November 2026: `Revenue in 2025` for `manager_apparel` returned revenue `65782.89991760254`, scoped UTC period `[2025-01-01, 2026-01-01)`, six model requests, three BigQuery executions and 62,914,560 billed bytes after transient direct Gemini errors and OpenRouter Gemini fallback. This is a dated observation, not a guaranteed current total or provider-availability result.

## Report and action horizon

```sh
opsfleet ask 'Create a Q1 2025 revenue report with Q2 2025 actions' --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE
```

Inspect the requested summary/table/trend/comparison/actions and the difference between measured Q1 evidence and recommended Q2 actions. Missing support produces a partial report with named gaps, rather than invented sections or future observations.

Historical live report observation, 3 November 2026, in the apparel manager scope:

```text
Analysis: UTC [2025-01-01, 2025-04-01)
Calvin Klein revenue: 7821.440022468567
Carhartt revenue: 6508.779977798462
Monthly revenue: January 5452.729999542236;
                 February 4114.749987602234;
                 March 4762.740013122559
Q2 actions cited the released evidence and were labeled recommendations.
The report was saved; two analyses used 115343360 billed bytes in the turn.
```

These are historical figures and a summary of observed behavior, not verbatim current terminal output or proof of causal explanations.

## Exact save and modified-save boundary

```sh
opsfleet ask 'Save that exact analysis' --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE --conversation CONVERSATION_ID --turn-id UNIQUE_SAVE_TURN_ID --json
```

An unchanged save uses the valid released snapshot. Inspect `report_id`, reuse/completeness and original dates/figures. A later six-turn live journey verified a February 2024 correction and exact save; the initial save interpretation was compact.

If the save also requests a different year, filter, metric, status, action horizon or report layout, the prototype clarifies and saves nothing. Request the changed analysis or report separately, review it, then exact-save. A report-only difference can cause one bounded reinterpretation; explicit or unresolved changes remain unsaved. Final application-source narrow/zero live checks verified four complete turns and 151 assertions, including both safe modified-save clarifications. Successful repair of an expanded unchanged-save interpretation is offline-tested only. See [save guard tests](tests/test_modified_save_guard.py) and [repair tests](tests/test_save_interpretation_repair.py).

## Owner-bound deletion

The following summarized transcript was observed on 3 November 2026 against a temporary database seeded only with synthetic reports. All model and warehouse counts were zero. Token placeholders replace the actual capability tokens; the private report library was not used.

```text
Seed: manager_apparel owns reports 1, 2, 3; manager_casual owns report 4.
Delete all reports mentioning Client X
  -> confirmation_required: owned IDs 1 and 2, version 1;
     exact literal rule, five-minute expiry, confirm TOKEN_1 or cancel TOKEN_1.
confirm TOKEN_1
  -> Soft-deleted reports 1 and 2.
     Active apparel reports: [3]. Other owner's report 4 remained.
Delete all the reports we made in this conversation [synthetic c-three]
  -> confirmation_required: owned ID 3; confirm TOKEN_2 or cancel TOKEN_2.
cancel TOKEN_2
  -> No reports changed.
```

For future CLI use, always pass the disposable database path and correct owner/conversation:

```sh
opsfleet ask 'Delete all reports mentioning Client X' --user manager_apparel --db PATH_TO_SYNTHETIC_SQLITE
opsfleet ask 'confirm TOKEN_FROM_PREVIEW' --user manager_apparel --db PATH_TO_SYNTHETIC_SQLITE
```

“yes” alone cannot confirm. Changed targets, wrong owners, expiry and token replay fail safely. Deletion is soft; bytes remain in SQLite/WAL/backups, with no purge/restore command. [Deletion tests](tests/test_step5.py) exercise preview, cancellation, confirmation, restart and rollback. A fresh database has no saved reports until they are created or explicitly seeded; the template commands do not seed it.

## Failure and evaluation boundaries

Ambiguous requests clarify; empty data is not invented zero revenue; unsafe requests fail closed; provider failures retain their provenance even when checked figures remain available. SQL repair recompiles the same trusted plan and is tested by fault injection. Neither a simulation nor a fluent answer proves live model/warehouse correctness.

[README verification](README.md#offline-verification) states the offline/live scope and historical full-evaluation limits. [Technical design](TECHNICAL_DESIGN.md#7-evaluation-method-and-verification-limits) explains numerical/report checks and the proposed human UX study. No participant usability study was performed.
