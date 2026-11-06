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
