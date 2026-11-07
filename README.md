# OpsFleet retail analytics assistant

A local CLI prototype for retail executives, with a separate [production architecture and diagrams](HLD.md) and [detailed technical explanation](TECHNICAL_DESIGN.md). The prototype uses LangGraph, structured Gemini interpretation, trusted SQL compilation, read-only BigQuery, grounded reports, SQLite state and confirmed owner-bound report deletion.

Start with setup below, then [example interactions](EXAMPLE_RUNS.md). The production services in the HLD are design targets; they are not deployed by this project.

## Requirements

- Python **3.12 or 3.13** (`>=3.12,<3.14`).
- Google Cloud Application Default Credentials (ADC), a billing project permitted to create query jobs, and read access to `bigquery-public-data.thelook_ecommerce`.
- A Gemini API key. Optional OpenRouter credentials enable the configured Gemini fallback.
- Network access for live model/BigQuery use. Offline tests and the fixture demo need no external credentials.

## Install and configure

Run from the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
```

Direct runtime dependencies are pinned in [pyproject.toml](pyproject.toml); `requirements.txt` installs this editable project and its `opsfleet` command. [requirements.step4.lock](requirements.step4.lock) records a historical resolved development environment, including tests; it is not a claim that every transitive dependency is locked by the default install. If your configured package index is unavailable, retry with `PIP_INDEX_URL=https://pypi.org/simple python -m pip install -r requirements.txt`.

Supply your own values in `.env`. Environment variables override `.env.local`, which overrides `.env`. Secret files are ignored by Git.

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Direct Gemini API key |
| `GOOGLE_CLOUD_PROJECT` | Billing project for BigQuery jobs |
| `BQ_LOCATION` | Defaults to `US` |
| `GEMINI_MODEL` | Defaults to `gemini-3.8-flash` |
| `OPENROUTER_API_KEY` | Optional fallback key |
| `OPENROUTER_MODEL` | Defaults to `google/gemini-3.8-flash` |

Configure ADC with `gcloud auth application-default login`, or use another ADC-supported identity. The identity needs permission to create jobs in the billing project and read the public dataset. Keep credential files outside the repository. See [Google's public-dataset access and billing documentation](https://docs.cloud.google.com/bigquery/public-data).

`opsfleet preflight` checks model and warehouse connectivity and can make paid provider/BigQuery requests. Narrow checks are `python -m opsfleet_assistant.preflight --bigquery` or `--models`. Query costs and account quotas still apply.

## CLI usage

```sh
opsfleet --help
opsfleet schema
opsfleet chat --user manager_apparel
opsfleet ask 'Monthly revenue in 2025' --user manager_apparel
opsfleet ask 'Use February 2024 instead' --user manager_apparel --conversation CONVERSATION_ID
opsfleet ask 'Create a Q1 2025 revenue report with Q2 2025 actions' --user ceo
```

Replace `CONVERSATION_ID` with the ID returned by the preceding analysis. `chat` keeps that context automatically; `/schema` shows supported data and `/exit` ends the session. Local state is stored in `runtime/opsfleet.sqlite` by default. Pass `--db PATH_TO_DISPOSABLE_SQLITE` to isolate a demonstration.

Seeded identities are `manager_apparel`, `manager_casual` and `ceo`, configured in [demo_users.json](config/demo_users.json). **`--user` is a local demo selector, not authentication.** Brand/product restrictions are enforced in trusted code before aggregation; local machine/database access is outside this trust boundary.

Default output leads with checked findings, readable values and actions, followed by scope, UTC roles, definitions, as-of timing and caveats. `--verbose` adds routes, jobs and byte counters; `--json` emits structured results with full-precision evidence. `chat --json` accepts one input line and emits one JSON result per turn. Model-generated explanations can fail independently of checked figures; the application reports that failure rather than inventing results.

## Reports and exact save

Reports can contain summary, performance table, trend, comparison, limitations and recommended actions. Action periods never cause future warehouse queries. Missing requested support produces a visibly partial report with named gaps; a saved partial report remains partial. Legacy saved content retains its original text with unavailable structured completeness.

```sh
opsfleet ask 'Save that exact analysis' --user manager_apparel --conversation CONVERSATION_ID
```

An exact save reuses a valid released snapshot. **A save that also changes dates, filters, metrics, statuses, grouping, action horizon or report preferences clarifies and saves nothing.** Request the change separately, review the result, then save. One bounded reinterpretation may distinguish model-added report defaults from explicit changes; unresolved differences remain unsaved. Full compound modification-and-save orchestration is not implemented, and a constraint entirely omitted by interpretation remains a limitation.

Context retains screened messages (default six / 2,400 characters), typed choices and at most two released results. Inputs are capped at 2,000 characters. Reusable context expires within 30 minutes and is checked against current ownership, scope and policy before reuse. Expired context requires a fresh analysis. SQLite and backups contain released business results and screened user text.

## Confirmed report deletion

Preview the exact owned targets, then confirm or cancel the returned one-time token:

```sh
opsfleet ask 'Delete all reports mentioning Client X' --user manager_apparel
opsfleet ask 'confirm TOKEN_FROM_PREVIEW' --user manager_apparel
# Alternatively:
opsfleet ask 'cancel TOKEN_FROM_PREVIEW' --user manager_apparel
opsfleet ask 'Delete all the reports we made in this conversation' --user manager_apparel --conversation CONVERSATION_ID
```

Matching is a case-insensitive literal substring of title/body, or an exact conversation selector. Tokens bind owner and snapshotted report IDs/versions, expire after five minutes and are single-use. Changed targets require a new preview; “yes” alone cannot confirm. Deletion is soft: bytes remain in SQLite/WAL/backups. There is no purge or restore command. Use a disposable synthetic database for demonstrations; see [examples](EXAMPLE_RUNS.md#owner-bound-deletion).

## Offline verification

Install the test extra before running pytest in a fresh environment:

```sh
python -m pip install -e '.[test]'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m pytest -q -p no:cacheprovider
python -m pip check
PYTHONDONTWRITEBYTECODE=1 python scripts/step6_offline_cli_demo.py
```

Keep `OPSFLEET_LIVE_TESTS` unset or `0` for offline testing. Two BigQuery integration tests are opt-in. The fixture CLI demo exercises the real CLI/workflow with injected model/warehouse adapters, temporary synthetic SQLite and isolated telemetry. Its four cases and printed business values are simulations.

The prepared public copy passed **998 offline tests, with two opt-in integration skips and two existing Typer/Click warnings**. All tests under `tests/` are retained. It covers safety/authorization, dates/context, numerical grounding, report completeness, persistence, deletion, bounded failures and presentation. Historical bounded live checks cover nine targeted scenarios / 20 workflow turns plus eight interpretation probes across two adjacent application freezes; only four workflow turns ran on the last application freeze. The original full evaluation remained 63/72 scenarios PASS, with 85 PASS / 7 FAIL / 2 PROVIDER_FAILURE / 5 NOT_RUN turns. These are not a single passing public-revision live evaluation. No additional live testing is performed to prepare this repository.

A fresh local Python 3.12.8 install previously passed on an earlier source, including 860 offline tests. An independent-machine or current-public-revision fresh installation has not been established. No human UX study or independent final acceptance is claimed. [Technical design](TECHNICAL_DESIGN.md#7-evaluation-method-and-verification-limits) explains the QA and UX approach.

The two JSON files in `docs/remediation/` support the retained regression/evaluation tooling. The source manifest identifies this public copy; it is distinct from historical evaluated source identities. Evaluation drivers are retained for test dependencies. Live modes require explicit execution and configured credentials; do not infer authorization from an old driver description or historical review waiver.

## Definitions and limits

- Revenue sums non-null item sale prices for `Shipped`, `Complete` and `Processing` by default; excludes `Cancelled` and `Returned`. Including `Processing` is an analytical convention, not accounting recognition. UTC periods are start-inclusive/end-exclusive. No currency is inferred.
- Churn is 90-day inactivity among prior purchasers in the selected brands. Current statuses can restate history; coverage/as-of does not prove ingestion freshness. Empty populations and zero baselines are not fabricated measurements.
- Each turn is bounded to 120 seconds, six model requests, two interpretation/plan repairs, eight warehouse executions, 512 MiB per job and 1 GiB per turn. Interpretation input is at most 12,000 characters; output allowances are 5,000 tokens for interpretation and 2,000 for explanation selection. Truncated replies retain failure provenance.
- The Golden corpus contains authored synthetic trios; only `GK-001 v1` is an approved global method example. It is not supplied analyst history. Fresh business figures always come from scoped warehouse evidence; fixtures are never a live fallback.
- Supported operations are bounded. General cross-period demographic/time-series comparisons, arbitrary SQL and complete prevention of repeated demographic differencing are absent.
- OpenRouter Gemini shares the model family and is not independent provider redundancy. Optional Presidio runtime remains unverified. Sanitized JSONL telemetry is best effort, not a durable audit log; `opsfleet metrics` derives local counters.
- Persistent preference learning, a production Golden pipeline, nondeveloper persona administration and chart/email/web tools are [production designs](HLD.md), not implemented prototype features.

Recovery increments 1A–1B: only classified transient dry-run failures before submission receive a bounded retry. Submitted or ambiguous jobs are never automatically replayed; unknown billing remains unknown. A pre-submission schema rejection permits one metadata inspection cycle (at most four table requests) and a correction only through a reviewed compatible source binding. Production ships only the public v1 binding; unknown schemas and persistent syntax defects stop with correction unavailable. Empty populations keep the original filters. A synthetic approved migration demonstrates correction offline; no live schema migration or general SQL syntax self-correction is claimed.

Part 1 review revisions preserve separate failing-diagnostic and completed-analysis provenance in JSON; `--verbose` shows safe stage, submission, job reference and known/unknown billing. Submitted/uncertain timeout guidance asks you to inspect job status before another submission. SDK page requests share the remaining job/turn deadline, and recovery success counts once per turn only after validated evidence. The latest Part 1 re-review recommends accepting both recovery increments within their agreed scope.

The latest Part 1 revisions also preserve known analysis job provenance after unexpected validation failures and allow unaffected queries to reuse an inspected binding without claiming another correction. The Part 2 instruction authorizes analytical playbooks from that reviewed baseline; implementation awaits review.

## Bounded analytical investigations (Part 2)

Part 2 implements three version-1 playbooks, awaiting review:

- **Product revenue:** “Investigate why product 101 revenue differs from product 202 in Q1 2025.” IDs are illustrative: both must exist in the authorized catalog. Compares the first product minus the second and measures qualifying item-volume and average-item-price components. Higher volume can offset lower price; no currency or business cause is inferred.
- **Regional purchaser spend:** “Investigate the spend-per-purchaser difference between Texas and California in Q1 2025.” State only; denominator is qualifying purchasers, with distinct orders containing scoped qualifying items. All registered users are unsupported and require clarification. Separates order frequency/order value from revenue and population-size differences.
- **Inactivity churn change:** “Did 90-day inactivity churn rise at the April 1, 2025 cutoff versus January 1, 2025? Investigate country components.” Reports both eligible/inactive populations, rates and percentage-point change; rejects flat/falling spike premises. Country definitions are fixed within one query, including tagged unknown values. These are population snapshots, not newly churned membership or causal retention effects.

Each normal path uses catalog + analysis + coverage = **three warehouse executions**. Existing recovery/deadline/byte ceilings stay unchanged. Actions are proposals tied to a checked component, with an observed trigger and measure to check. Provider drafting failure retains checked components and deterministic proposals.

Regional cohorts below five purchasers are withheld without complementary counts or invented zeros. Churn releases all aligned countries together (at most nine), only when every country's inactive and active cells at both cutoffs have at least five purchasers. Otherwise it omits the entire country breakdown; safe headline-only results remain partial. If headline cells are unsafe, nothing is released. This is conservative bounded release, not a general repeated-query privacy system.

Reports persist unrounded released operands, signed contributions, method version, residual/tolerance, choices and proposals. Exact save preserves those findings and original timing. Changed playbook/population/segment/period/product/status/report choices cannot save old evidence. New artifact v2 / snapshot v3 / grounded-report v3 readers coexist with ordinary artifact v1 / snapshot v2 / grounded-report v1 (valid playbook grounded-report v2 selections also remain readable); old content is never assigned a new method. Continuity still expires after 30 minutes.

Run the reproducible synthetic demonstration (test dependencies required):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src OPSFLEET_LIVE_TESTS=0 .venv/bin/python scripts/part2_offline_cli_demo.py
```

It invokes the real CLI/workflow/compiler/adapter using injected interpretation and independently populated SQLite source rows, disposable report storage, network blocking and isolated telemetry. Expected and actual components, partial cases, and save/restart replay checks are printed. This does not verify real-model interpretation or BigQuery dialect/type behavior. See [Part 2 implementation report](docs/PART_2_IMPLEMENTATION_REPORT.md) for the acceptance matrix, formulas, budgets and current verification results. No live calls or deployment are included.

Part 2 review revisions (R1–R4) await re-review. Churn change headlines must compare the overall current cutoff with the overall earlier cutoff; country temporal findings must match countries and identify them. New product/state comparisons follow the requested ordered pair. Missing durable support cannot downgrade a playbook report to legacy. Fully withheld output says “No safely releasable observations; absent or withheld,” without claiming source absence or counting a recovered observation. Valid saved v2 selections retain their original explicit operands/text; new v3 reports use corrected defaults and labels. Invalid saved churn references fail closed. Part 3 remains on hold pending re-review and owner acceptance.

## Saved work navigation (Part 3)

```sh
opsfleet reports --user manager_apparel --limit 5 --db PATH_TO_DISPOSABLE_SQLITE --json
opsfleet reports --user manager_apparel --limit 5 --cursor CURSOR_FROM_PREVIOUS_PAGE --db PATH_TO_DISPOSABLE_SQLITE --json
opsfleet report REPORT_ID --user manager_apparel --db PATH_TO_DISPOSABLE_SQLITE --json
opsfleet conversations --user manager_apparel --limit 5 --db PATH_TO_DISPOSABLE_SQLITE --json
opsfleet chat --user manager_apparel --conversation CONVERSATION_ID --db PATH_TO_DISPOSABLE_SQLITE
```

In human chat, `/help` lists capabilities and these exact commands:

```text
/reports [--limit N] [--cursor CURSOR]
/report REPORT_ID
/conversations [--limit N] [--cursor CURSOR]
/resume CONVERSATION_ID
/new
/schema
/help
/exit or /quit
```

Limits are 1–50, default 20. Repeat the same limit with a returned cursor. Report pages scan at most N candidates plus one lookahead, ordered by descending ID. Conversation pages use ascending ID. Pages may be sparse or empty with a continuation; keep following the cursor. No hidden counts or inaccessible IDs are returned. Cursors are opaque, scoped to owner/operation/limit, expire after 24 hours, and require the same SQLite database. Insertions before the current position appear on a new traversal; deletions are skipped. Pages are a moving view rather than a frozen library snapshot.

Every report command rechecks current trusted demo identity and the entire saved execution scope, with at most one bounded current product catalog execution. It validates saved evidence, supported versions, bindings, findings, drivers, composition and exact agreement of saved text with its trusted rendering contract before disclosing titles or content. A recomputed checksum cannot bless independently changed report text. Supported pre-contract formats are checked against the original support and returned unchanged; unsupported formats remain unavailable. Revoked, corrupt, unsupported, deleted and other-owner reports are unavailable. Catalog failure is `access_check_unavailable`, never an empty library. Genuine old ordinary reports need enough saved plan/scope metadata; their original text can be read with `legacy` completeness and unknown analytical details. Missing support never downgrades a playbook into that reader.

Navigation uses zero model requests and zero fresh business-analysis queries. Help/schema and conversation metadata/selection use zero warehouse calls. A catalog lookup authorizes disclosure of a historical snapshot; it does not update its figures or prose. Report reads preserve exact original text, numerical precision, analytical period, evidence as-of and completeness. Browsing does not inject evidence into the active conversation or extend its 30-minute continuity lifetime. Expired or incompatible context requires fresh analysis before an unchanged save. Resuming valid context preserves its original date anchors and rechecks current authorization on analytical reuse.

`/new` immediately persists an empty fresh conversation and selects it. Previous work remains. `/resume` and `chat --conversation` select only an existing owned ID; blank/arbitrary/other-owner IDs fail safely. Failed interactive selection leaves the previous conversation active. Conversation listings disclose IDs, update times and continuity availability, without message/report text.

Human chat supports bare `confirm` or `cancel` immediately after a deletion preview has successfully displayed. It resolves that exact persisted proposal through the explicit-token operation. A new preview, any other input (including help, invalid commands or failed resume), conversation change, error, expiry, completion, EOF/interrupt or restart clears the local shortcut. Clearing it does not cancel a persisted proposal. “Yes” and model output cannot approve deletion. Another terminal's proposal cannot replace the displayed one. Preview titles now use the same current disclosure gate as browsing; an unverifiable matched set fails before titles, counts or tokens are shown. Explicit confirmation still uses original IDs/versions atomically, with no reselection or catalog/business query.

Scripts, `ask`, and `chat --json` require explicit `confirm TOKEN` / `cancel TOKEN`. JSON previews retain usable commands in `text` and add `deletion_preview` with typed targets, expiry and the intended token; protect these capability values. Navigation responses contain `operation`, safe `status`, `items`, `content`, `active_conversation`, `next_cursor`, `message`, and model/query/business-query counters. JSON chat emits one object per result with no prompts/banner; exit/EOF terminates without a result. Errors preserve machine-readable status. With `--json`, malformed arguments, missing operands/identity and unknown flags/commands emit one sanitized `invalid_command` envelope and exit 2 before dependencies open; non-JSON usage errors retain standard help/stderr behavior. `--verbose` never changes JSON precision. Store-open errors also produce JSON when requested.

Run the isolated evaluator journey without credentials or live calls:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src OPSFLEET_LIVE_TESTS=0 .venv/bin/python scripts/part3_offline_cli_demo.py
```

It saves, recreates CLI/store instances, lists/reads, resumes/saves, checks expiry/revocation/unavailable catalog/corruption, then cancels and confirms in human chat. Output redacts capability values. Part 3 is implemented and awaiting review; Part 2 independent re-review remains pending. See [Part 3 implementation evidence](docs/PART_3_IMPLEMENTATION_REPORT.md).
