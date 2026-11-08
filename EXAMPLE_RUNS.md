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

Ambiguous requests clarify; empty data is not invented zero revenue; unsafe requests fail closed; provider failures retain their provenance even when checked figures remain available. Schema recovery may recompile the unchanged semantic plan through a reviewed compatible source binding; unchanged persistent syntax defects stop. Both paths are tested offline by fault injection. Neither a simulation nor a fluent answer proves live model/warehouse correctness.

[README verification](README.md#offline-verification) states the offline/live scope and historical full-evaluation limits. [Technical design](TECHNICAL_DESIGN.md#7-evaluation-method-and-verification-limits) explains numerical/report checks and the proposed human UX study. No participant usability study was performed.

## Part 2 offline analytical investigations

These are synthetic demonstrations, not live business observations. Run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src OPSFLEET_LIVE_TESTS=0 .venv/bin/python scripts/part2_offline_cli_demo.py
```

The script injects structured interpretation into the real CLI, runs compiled SQL against independent SQLite source rows through the real warehouse adapter, and prints expected/actual findings. All source/report state and telemetry are disposable; sockets are blocked and settings are synthetic. Each CLI call opens/closes storage, so the subsequent save/replay validates a restart journey without adding navigation commands.

| Playbook / declared direction | Independent expected result | Actual offline result |
|---|---|---|
| Product 1 minus 2: R=1000/600, N=100/50 | Revenue difference 400; volume +550; average price -150 | 400; +550; -150 |
| Texas minus California: C=100/50, O=200/50, R=10000/4000 | Spend-per-purchaser difference 20; frequency +65; order value -45 | 20; +65; -45 |
| Current April 1 minus baseline January 1 inactivity cutoff | Rate .25 minus .14; mix .01, within .10 | +11 percentage points; +1 mix, +10 within |

The source churn fixture has baseline country eligibility 600/400 and inactivity 60/80; current eligibility 600/600 and inactivity 120/180. These cumulative populations preserve the reference shares/rates without making eligible purchasers disappear. Separate released-count tests also check the plan's original 600/400 → 500/500 reference. Neither fixture establishes individual churn transitions.

Three additional cases remove a qualifying product, withhold a state cohort below five purchasers, and introduce a small country. Product/state comparisons then have no measured decomposition. Churn omits all country detail together while preserving a safe headline comparison; residual and segment contributions are absent. Saves retain partial completeness through restart replay. Every ordinary source path uses three warehouse executions; save/replay adds only catalog authorization, no fresh business measurement. Proposed actions identify a checked component, investigation and measure, and make no claim about stockouts, campaigns or causal effects.

Illustrative live-style prompts (not executed during Part 2 implementation):

```text
Investigate the revenue difference between product 101 and product 202 in Q1 2025.
Investigate spend per qualifying purchaser in Texas versus California in Q1 2025.
Did 90-day inactivity churn rise at April 1, 2025 versus January 1, 2025? Investigate country components.
Save that exact analysis.
```

Products must be authorized catalog IDs. State is the only regional playbook dimension; country is the only churn segment. “All registered users” requires clarification rather than silently switching denominator. Actions are proposals, and current item statuses can restate historical results. See [implementation evidence and limits](docs/PART_2_IMPLEMENTATION_REPORT.md).

Part 2 review revisions keep the overall churn premise consistent with supporting facts: the synthetic source spike is +11 points, and a draft comparing baseline country B against baseline country A is rejected into that checked fallback. Product 2 minus product 1 produces -400 in both default revenue findings and the driver; Texas minus California produces +20 in both spend findings and the driver. Fully withheld populations display “No safely releasable observations,” never source-empty. Reports with missing mandatory playbook support refuse replay. These are offline regression results, awaiting re-review; they do not authorize Part 3 or live evaluation.

## Part 3 evaluator journey: historical reports and local confirmation

Use one synthetic disposable database throughout. These command templates may need a current catalog connection for report disclosure, but navigation never asks a model or reruns business analysis. The offline script below supplies injected adapters instead.

```sh
opsfleet ask 'Create a Q1 2025 revenue report' --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE --json
# Close the process, then reopen the same database:
opsfleet reports --user manager_apparel --limit 2 --db PATH_TO_DISPOSABLE_SQLITE --json
opsfleet reports --user manager_apparel --limit 2 --cursor NEXT_CURSOR --db PATH_TO_DISPOSABLE_SQLITE --json
opsfleet report REPORT_ID --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE --json
opsfleet conversations --user manager_apparel --limit 2 --db PATH_TO_DISPOSABLE_SQLITE --json
opsfleet chat --user manager_apparel --conversation CONVERSATION_ID --db PATH_TO_DISPOSABLE_SQLITE
```

Inspect exact stored content, original period/as-of, and unchanged completeness. Repeat with another owner: no ID/title/body is disclosed. A current access-check outage returns `access_check_unavailable`, not an empty library. Genuine legacy rows expose only their supported old text/completeness; missing playbook support is unavailable.

In human chat:

```text
/help
/reports --limit 2
/reports --limit 2 --cursor NEXT_CURSOR
/report REPORT_ID
/conversations --limit 2
/resume CONVERSATION_ID
Save that exact analysis
/new
/resume CONVERSATION_ID
Delete all the reports we made in this conversation
cancel
Delete all the reports we made in this conversation
confirm
/quit
```

`/new` persists an empty conversation immediately. `/resume` preserves valid original temporal anchors; expired/incompatible continuity requires a fresh analysis. Reading a historical report cannot make an expired save valid or replace active evidence. Failed resume preserves selection and clears any local deletion shortcut.

Pagination uses N=1..50 (default 20), with the same N on continuation. Opaque cursors bind owner, operation and limit for 24 hours in that database. A page can contain fewer than N items, including zero with `next_cursor`; no number of withheld candidates is disclosed. Report order is descending ID; conversation order is ascending ID. Concurrent insertion/deletion follows keyset positions, without duplicate traversal of unchanged records.

Bare human `confirm`/`cancel` refers only to the exact successfully displayed preview in that process, owner and conversation. Any intervening input—including `/help`, malformed commands, navigation or failed `/resume`—invalidates the shortcut. New preview, completion/error, five-minute expiry, EOF/interrupt and restart also invalidate it. Request a fresh preview or use its explicit token. No “yes” approval or latest-proposal lookup exists. Changed targets make the original set stale; new reports after preview are excluded. An unverifiable matched report makes the whole preview unavailable before titles/counts/token disclosure.

For scripts and JSON chat, read `deletion_preview.token` from the intended preview response, handle it privately, then use:

```sh
opsfleet ask 'confirm TOKEN_FROM_INTENDED_PREVIEW' --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE --json
# Or cancel TOKEN_FROM_INTENDED_PREVIEW; bare commands cannot resolve a JSON preview.
```

Tokens remain in the explicit preview contract, never telemetry, saved reports or public demo output. One JSON object is emitted per result, without terminal decoration; `/exit`/EOF terminates. Help/schema/conversation metadata use zero model/warehouse calls. Report list/read and nonempty deletion previews use zero model/business-analysis calls and at most one catalog execution; confirmation/cancellation use no warehouse calls.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src OPSFLEET_LIVE_TESTS=0 .venv/bin/python scripts/part3_offline_cli_demo.py
```

This real-CLI synthetic demonstration recreates process adapters/store connections, checks exact read/precision and resume/save, expired readable reports with blocked expired save, other-owner/revoked/corrupt/unavailable-catalog cases, and human preview/cancel/confirm. It prints assertion summaries with tokens redacted. It is offline verification, not live-provider or human usability certification.


Part 3 review revision: even pre-dispatch argument failures return structured JSON:

```sh
opsfleet report not-an-id --user manager_apparel --json
opsfleet reports --limit 0 --user manager_apparel --json
opsfleet conversations --unknown --user manager_apparel --json
```

Each exits 2 with one `invalid_command` envelope on stdout, without opening the store or invoking model/catalog dependencies. Non-JSON argument errors retain the normal usage convention. Saved report text/title must exactly match the versioned trusted format derived from its original validated support; changing only text plus its recomputable checksum makes read/list/replay/preview unavailable. Compatible pre-contract reports retain their original bytes and timing; unsupported text formats fail closed.


## Part 5 evaluator journey and recovery

From the documented editable runtime/test install:

```sh
python scripts/offline_check.py golden
python scripts/offline_check.py playbooks
python scripts/offline_check.py navigation
```

These commands need no provider credentials. All three playbooks show independent synthetic references: product difference 400 = volume 550 + price −150; purchaser-spend difference 20 = frequency 65 + value −45; churn change .11 = mix .01 + within .10 (11 percentage points). Partial cases withhold unavailable attribution; no missing denominator becomes zero. Navigation reopens exact saved content after restart, checks scope/expiry/corruption, and exercises cancellation then exact confirmation. Tokens remain redacted in the demo summary.

Sanitized supported recovery examples (controlled offline faults):

| Trigger | Observed bounded behavior | Useful next step |
| --- | --- | --- |
| Transient dry-run failure before submission | One classified retry; success counted after evidence release | Continue with the checked result |
| Approved synthetic price-field migration | One schema inspection, changed binding, unchanged scope/status/date semantics | Review the synthetic binding evidence |
| Persistent SQL template defect | Stop; no identical recompilation called a correction | Fix the trusted compiler, then start a new request |
| Ambiguous warehouse submission | Stop without replay; billing can remain unknown | Inspect the original job before another submission |
| Genuinely empty population | Preserve filters; no invented measurement | Choose another explicitly requested period or scope |

Reproduce the recovery matrix with `python scripts/offline_check.py tests tests/test_recovery_1a.py tests/test_recovery_1b.py`. The synthetic binding is not an observed public migration, and general SQL syntax self-correction remains unsupported.

Real-model, live warehouse and human results have not been collected for Part 5. See [owner-terminal commands and preflight requirements](docs/validation/part5-v1/COMMANDS.md); the default/dry paths make zero network calls.
