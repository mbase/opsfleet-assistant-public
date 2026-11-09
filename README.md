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
python -m pip install --index-url https://pypi.org/simple -c requirements.part5.lock setuptools==82.0.1
python -m pip install --index-url https://pypi.org/simple --no-build-isolation -c requirements.part5.lock -e '.[test]'
test -f .env || cp .env.example .env
```

The supported installation is an editable checkout on Python 3.12 or 3.13 using the [runtime and test constraints](requirements.part5.lock). Install the pinned build backend first, then install the project without build isolation as shown above. Configuration and resources are source-relative, so standalone wheel installation is unsupported. The constraints pin package versions without artifact hashes. `requirements.txt` is an editable convenience entry; `requirements.step4.lock` is a historical dependency snapshot.

Offline checks need no cloud credentials. Configure the following for live model and BigQuery use. The [validation specification](docs/validation/part5-v1/SCENARIOS.md) describes installation, interpretation, warehouse and usability checks separately.

Supply your own values in `.env`. Environment variables override `.env.local`, which overrides `.env`. Secret files are ignored by Git.

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Direct Gemini API key |
| `GOOGLE_CLOUD_PROJECT` | Required billing project **ID** for live BigQuery jobs |
| `BQ_LOCATION` | Defaults to `US`; use `US` for the public dataset |
| `GEMINI_MODEL` | Defaults to `gemini-3.8-flash` |
| `OPENROUTER_API_KEY` | Optional fallback key |
| `OPENROUTER_MODEL` | Defaults to `google/gemini-3.8-flash` |

For BigQuery, set these entries in `.env` using your own billing project ID:

```env
GOOGLE_CLOUD_PROJECT=your-billing-project-id
BQ_LOCATION=US
```

Use the project **ID**, such as `my-billing-project`, rather than its display name, numeric project number or `projects/NUMBER` resource path. This existing variable selects the project that pays for query jobs; the data is read from `bigquery-public-data.thelook_ecommerce`. Set it explicitly even when local Google credentials are already available.

Authenticate separately with `gcloud auth application-default login`, or use another ADC-supported identity. Gemini/OpenRouter API keys do not authenticate BigQuery. The ADC identity needs permission to create jobs in the billing project and read the public dataset. Keep credential files outside the repository. See [Google's public-dataset access and billing documentation](https://docs.cloud.google.com/bigquery/public-data).

For bounded live validation with `--env-file .env`, the project ID must also match `warehouse.project` in the reviewed execution manifest. See the [live configuration instructions](docs/validation/part5-v1/COMMANDS.md).

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

Follow the installation commands above, then run the five-minute synthetic evaluator journey:

```sh
python -m pip check
python scripts/offline_check.py help
python scripts/offline_check.py schema
python scripts/offline_check.py tests
python scripts/offline_check.py golden
python scripts/offline_check.py playbooks
python scripts/offline_check.py navigation
```

The test wrapper builds a temporary source manifest automatically; no preparation step is required. It works in both Git checkouts and source copies. For a version-bound live evaluation, use the explicit preparation commands in the [validation guide](docs/validation/part5-v1/COMMANDS.md).

The wrapper forces `OPSFLEET_LIVE_TESTS=0`, drops inherited provider/project/ADC selectors, isolates local state/telemetry, suppresses repository dotenv reads and blocks network transports. The demos exercise the real CLI/workflow with injected interpretation/warehouse adapters and disposable synthetic state. Playbooks show independently checked complete/partial components; navigation saves, restarts, reads, resumes and confirms/cancels deletion. They do not establish model accuracy, live warehouse behavior or human usability.

On 9 November 2026, the offline suite at application revision `681b855` passed **1,578 tests with two opt-in integrations skipped**. A fresh Python 3.13 installation also passed dependency checks, CLI help/schema, all three demos and its offline suite (1,576 passed, four skipped; the source copy omits Git metadata). The [offline CI workflow](.github/workflows/offline.yml) defines Python 3.12/3.13 jobs; remote execution remains unverified.

Earlier revisions recorded a 45-case synthetic-input model trial with 45 first-semantic and final-workflow passes, a 29-assertion public-warehouse smoke check, and a 16-turn behavior smoke check. These are historical observations, not live verification of the current revision. Broad warehouse acceptance and representative human usability remain unverified. See the [verification summary](docs/validation/part5-v1/acceptance_report.md) and [bounded live evaluation commands](docs/validation/part5-v1/COMMANDS.md).

The repository includes validation code, schemas, authored scenarios, synthetic fixtures and the offline CI workflow. Generated run output stays outside version control. Evaluation records bind results to their source revision so historical observations remain distinguishable from current checks.

## Definitions and limits

- Revenue sums non-null item sale prices for `Shipped`, `Complete` and `Processing` by default; excludes `Cancelled` and `Returned`. Including `Processing` is an analytical convention, not accounting recognition. UTC periods are start-inclusive/end-exclusive. No currency is inferred.
- Churn is 90-day inactivity among prior purchasers in the selected brands. Current statuses can restate history; coverage/as-of does not prove ingestion freshness. Empty populations and zero baselines are not fabricated measurements.
- Each turn is bounded to 120 seconds, six model requests, two interpretation/plan repairs, eight warehouse executions, 512 MiB per job and 1 GiB per turn. Interpretation input is at most 12,000 characters; output allowances are 5,000 tokens for interpretation and 2,000 for explanation selection. Truncated replies retain failure provenance.
- The Golden corpus contains authored synthetic trios; only `GK-001 v1` is an approved global method example. It is not supplied analyst history. Fresh business figures always come from scoped warehouse evidence; fixtures are never a live fallback.
- Supported operations are bounded. General cross-period demographic/time-series comparisons, arbitrary SQL and complete prevention of repeated demographic differencing are absent.
- OpenRouter Gemini shares the model family and is not independent provider redundancy. Optional Presidio runtime remains unverified. Sanitized JSONL telemetry is best effort, not a durable audit log; `opsfleet metrics` derives local counters.
- Persistent preference learning, a production Golden pipeline, nondeveloper persona administration and chart/email/web tools are [production designs](HLD.md), not implemented prototype features.

Warehouse recovery: only classified transient dry-run failures before submission receive a bounded retry. Submitted or ambiguous jobs are never automatically replayed; unknown billing remains unknown. A pre-submission schema rejection permits one metadata inspection cycle (at most four table requests) and a correction only through a reviewed compatible source binding. Production ships only the public v1 binding; unknown schemas and unsupported or persistent syntax defects stop safely. Empty populations keep the original filters. A synthetic approved migration demonstrates schema correction offline; no live schema migration is claimed.


SQL recovery supports one narrow representation correction: compiler-owned equality
spelled `==` can become Standard SQL `=` after an analytical dry-run syntax
rejection with positively known non-submission. Token-aware replacement must
match a fresh authorized compilation structurally and preserve every bound
parameter and result contract. The corrected query is sealed, checked and dry-run
again under the original budgets. A turn permits one schema **or** syntax
correction across all analyses, without recursive recovery. Unchanged SQL,
unsupported grammar (including compiler parse failures), changed analysis,
ambiguous/submitted jobs and later diagnostic failures stop safely. This is not
general SQL repair. Empty and privacy-withheld populations retain their existing
coverage interpretation. This recovery path was verified offline with synthetic
source rows only; real provider/dialect behavior on this revision is unverified,
and previous live results describe their original source versions.

Recovery preserves separate analysis and diagnostic provenance in JSON; `--verbose` shows safe stage, submission, job reference and known/unknown billing. Submitted or uncertain timeout guidance asks you to inspect job status before another submission. SDK page requests share the remaining job/turn deadline, and recovery success is counted once after validated evidence. Unaffected queries may reuse an inspected binding without claiming another correction. General SQL syntax self-correction and production readiness remain outside the verified prototype scope.

## Bounded analytical investigations

The prototype implements three version-1 analytical playbooks:

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

It invokes the real CLI/workflow/compiler/adapter using injected interpretation and independently populated SQLite source rows, disposable report storage, network blocking and isolated telemetry. Expected and actual components, partial cases, and save/restart replay checks are printed. This does not verify real-model interpretation or BigQuery dialect/type behavior. The independent expected values and offline CLI journey are documented in [synthetic analytical examples](EXAMPLE_RUNS.md#offline-analytical-investigations). No live calls or deployment are part of this synthetic demo.

Playbook findings follow the requested comparison direction, preserve supported saved-report versions, and fail closed when required support is unavailable. Withheld cohorts are described as absent or withheld, never as source-empty.

## Saved work navigation

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

It saves, recreates CLI/store instances, lists/reads, resumes/saves, checks expiry/revocation/unavailable catalog/corruption, then cancels and confirms in human chat. Output redacts capability values. See the [saved-work examples and offline journey](EXAMPLE_RUNS.md#saved-work-walkthrough-historical-reports-and-local-confirmation).
