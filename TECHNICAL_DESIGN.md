# Technical design and implementation explanation

**Scope:** this explains the implemented local prototype and separately describes production substitutions. The production architecture is a design in [HLD.md](HLD.md); it is not deployed or implemented by this repository.

**Verification:** offline tests exercise the prototype controls and contracts. Bounded historical live observations and their limits are summarized in [README](README.md#offline-verification); this public packaging revision is not a new live acceptance.

## 1. Technology choices and tradeoffs

| Technology | Why it is used here | Cost, limit and alternative |
|---|---|---|
| Python 3.12 (supported 3.12–3.13) | BigQuery/model SDK support, fast iteration and a compact typed service | Dynamic language needs runtime validation and pin discipline; a JVM service offers stronger static contracts at higher implementation cost |
| Typer + Rich | Discoverable CLI commands, typed options, readable interactive panels and JSON result mode | Local CLI only; a web client and authenticated API are production work |
| LangGraph | Explicit nodes and state for policy, planning, analysis, reporting, recovery and save steps; visible branching and bounded turns | Adds dependency and state-machine concepts; a small hand-written workflow would be simpler but custom orchestration can become opaque as flows grow. Prototype compiles/invokes per turn and persists selected validated context, not durable node checkpoints |
| Pydantic | Validates model plans, report drafts, identities, evidence and boundary contracts | Schema validity is not business correctness or authorization; trusted code must still verify invariants |
| Trusted Python SQL compiler + SQLGlot | Model proposes a typed plan; identifiers/expressions come from reviewed code; SQLGlot parses/inspects generated GoogleSQL before sealing | Supports a bounded operation vocabulary rather than arbitrary SQL. SQLGlot is not BigQuery authorization or a complete database validator; dry-run and live BigQuery execution remain necessary |
| `google-cloud-bigquery` | ADC, BigQuery dry-run, bound parameters, job metadata and execution of the assignment dataset | Requires Google account/project/billing setup and network access. Prototype uses read-only operations and per-turn caps |
| `google-genai` Gemini + `httpx` OpenRouter | Gemini structured generation is the preferred model route; OpenRouter Gemini is an authorized transient-failure fallback | Both observed routes used Gemini, so fallback does not cover a Gemini-wide outage. Extra intermediary has data-handling implications; production route requires explicit provider review and evaluation |
| SQLite | Zero-service durable local reports, conversation context and deletion proposals | Single-machine/demo durability, local file access, no production HA or multi-instance coordination; production design uses Cloud SQL PostgreSQL |
| JSON authored Golden corpus | Transparent metadata filtering and a small reproducible method demonstration | Not a real analyst corpus; no ingestion, scalable semantic search, approval service or production index. Production design starts with PostgreSQL full-text + pgvector |
| pytest + injected fakes + opt-in BigQuery tests | Deterministic policy, lifecycle, recovery and CLI coverage with separate bounded live evidence | Fakes cannot prove source behavior; live public data is mutable. Separate independent reference fixtures are still needed for broader numerical certification |
| Local JSONL telemetry | Best-effort sanitized event trail and low-cardinality local counters | Not durable, managed, tamper-resistant or suitable as a production audit trail; production design exports to managed telemetry |

Dependencies are pinned in `pyproject.toml`; `requirements.txt` installs the editable project. Python bounds are `>=3.12,<3.14`. The optional `privacy` extra installs Presidio, but deterministic field/PII rules are primary and Presidio runtime availability was not verified.

## 2. Source schema, grain and metrics

Prototype data source: `bigquery-public-data.thelook_ecommerce`. The implementation reads these fields:

| Table | Role and relevant fields |
|---|---|
| `order_items` | Fact grain: one transaction line; `order_id`, `product_id`, item `status`, `sale_price`, `created_at` |
| `orders` | Order/customer relationship: `order_id`, `user_id`, order status metadata |
| `products` | Product authorization and label: `id`, `brand` |
| `users` | Demographics for aggregate-only segments: `id`, state/city/country, age, gender, traffic source |

The trusted compiler joins `order_items.order_id = orders.order_id` and `order_items.product_id = products.id`; demographic operations additionally join `orders.user_id = users.id`. Revenue and item counts aggregate at item grain. Order counts use distinct order IDs. The brand restriction is applied to each product line before aggregation, so a mixed-brand basket contributes only the authorized lines. Customer spend groups by permitted internal `user_id`; that identifier is allowed for customer business metrics, while names, email, street/postal address and exact coordinates are unavailable. Demographic row-level values/IDs never reach the model; released groups have at least five customers plus complementary and repeated-query protections in the production design.

Revenue default: sum non-null item `sale_price` for item statuses `Shipped`, `Complete`, `Processing`; exclude `Cancelled`, `Returned`, and null price. Time is `order_items.created_at` in UTC, start inclusive and end exclusive; quarters are calendar quarters. Including `Processing` is an analysis convention, not accounting recognition. Current order-item status can restate historical period results; the public dataset is not an event-sourced status history. Coverage reports observed source-item timestamps but do not prove ingestion completeness or profile freshness. Churn is a 90-day inactivity snapshot among prior purchasers in the effective brand scope; it is not automatically newly churned users during a month.

The source mapping and exact metric definitions are centralized in `compiler.py`, `metrics.py`, `identity.py`, `privacy.py`, and `contracts.py`. The typed analysis plan selects an operation, metric, period, dimension and optional product IDs/limits. Unsupported operations, ambiguous years, missing state-spend metric or missing product IDs receive clarification or a safe refusal. This is dynamic SQL construction from a controlled vocabulary; it is not arbitrary natural-language SQL generation.

## 3. Component contracts and request path

```text
CLI ask/chat
  ├─ local seeded identity selector (demo only)
  ├─ safety screen: direct PII, forged authority, attack/SQL patterns
  ├─ catalog authorization + reauthorized prior-plan context
  ├─ ModelGateway: structured InterpretationResult / RequestSpec; shared call/deadline budget
  ├─ trusted temporal resolution -> ResolvedRequest -> bounded AnalysisPlans
  ├─ Golden filter: interpreted purpose/metric, approved method/schema, effective scope
  ├─ policy recheck: brands, fields, metric, cohort and product membership
  ├─ trusted compiler -> sealed parameterized CompiledQuery
  ├─ BigQueryTurn: catalog/dry-run/execute/coverage and byte/time caps
  ├─ Evidence validator: schema, values, membership, completeness, PII, coverage
  ├─ report drafting from safe evidence only; numeric output rendered/checked in code
  ├─ SQLite: safe follow-up state, evidence references, report, deletion proposal
  └─ local JSONL trace: fixed vocabulary and bounded safe metadata
```

### Contracts

- **Identity/brand scope:** resolved from the selected `config/demo_users.json` row and current product catalog. Manager brands are exact canonical values; CEO scope is all catalog brands. A request cannot add brands. CLI `--user` is a demo selector and is not authentication.
- **InterpretationResult / RequestSpec:** `ready`, `needs_clarification` or `unsupported`, with a partial or complete retail goal and bounded `QuerySpec` choices. No SQL or grants. Structured interpretation supplies semantic choices; trusted code retains dates, policy and execution authority. Required details, ambiguity and conflicting instructions are model interpretation responsibilities; schema validity does not prove understanding.
- **TemporalExpression / ResolvedRequest:** calendar month/quarter/year, explicit interval, relative calendar window, to-date interval or bounded role reference. Absolute/relative/context years are explicit. Python resolves an application-supplied UTC date, validates fields/order/reference dependencies, and never invents a bare month's year. To-date includes the reference day; explicit ends stay exclusive. Analysis/comparison/action roles remain separate; query steps cannot select action.
- **AnalysisPlan:** Pydantic-constrained operation, metric, period, dimensions, optional product IDs and bounded row limit. It contains no SQL, identity/role, policy override or arbitrary tool call.
- **CompiledQuery:** trusted compiler output with bound parameters, metric definition, result contract, effective scope, operation and HMAC seal. Adapter rejects tampered/untrusted objects and re-resolves the current demo scope.
- **Evidence:** validated columns/types/values, finite metrics, scope reference/version, metric definition, period/as-of/coverage, BigQuery job IDs and completeness. No raw SQL or unfiltered source rows are passed to model prose generation.
- **DeletionProposal:** persisted owner, selector, exact report ID/version snapshot, expiry, token digest and lifecycle state. Token plaintext is shown only in the preview. The model is not involved in selection approval or confirmation.
- **TelemetryEvent:** fixed event/stage/category/route/status names, UUID correlation, bounded numeric counters and validated job/trio references. Arbitrary user/provider strings are discarded or mapped to unknown.

### Normal request/response

The CLI constructs a trace/turn ID, loads settings and SQLite, and screens safety before model access. A bounded catalog lookup authorizes identity and any prior plan before it enters the interpretation prompt. One structured gateway call proposes the RequestSpec and analytical choices; Python resolves temporal roles and builds existing AnalysisPlans. Missing/ambiguous periods clarify before analytical queries (catalog/model calls may already have occurred). temporal comparisons use two matching aggregate plans with separate analysis/comparison periods and checked deltas; the existing two-snapshot churn comparison binds both cutoffs. Cross-period demographic/time-series comparisons are unsupported.

Golden retrieval consumes interpreted trend purpose, metric and choices with trusted effective requested scope. Approved method code can refine a compatible aggregate into monthly time series; no second planning model call is needed. Raw example SQL and report prose never enter the model prompt. Trusted code rechecks that methodology preserves resolved dates, metrics, statuses, products and scope, then compiles and executes within the same turn budget.

Evidence validation and existing report drafting remain. Answers disclose selected metrics, actual analysis/comparison/action intervals and scope. Action periods label recommendations and never query or assert future results. JSON adds `interpretation_status` and `resolved_periods`. Saved report content retains displayed temporal metadata. The workflow grounds factual sentences in typed references and deterministic calculations; prose screening alone is not the factual authority. The store persists the bounded safe context described below. Report composition/completeness and executive CLI presentation are described below.

Schema-valid interpretation is not proof of language understanding. Offline fixtures and bounded live observations have distinct limits; see [evaluation methodology](#7-evaluation-method-and-verification-limits).

### Requested report composition

A requested scalar comparison requires distinct analytical choices, rather than multiple query objects. Repeating the same metric, population, status definition and period does not specify a baseline. Set-valued filters and current authorized effective brand scopes are normalized; scalar row limits do not create choices. Such requests clarify before analytics/save and keep the pending report/action metadata. Group/product/demographic/monthly choices within evidence and explicit temporal pairs remain supported, including equal results across distinct choices. Supplying a missing baseline can bind a redundant pending scalar query to the comparison role while all other continuation constraints and paired-plan validation remain enforced. Ambiguity and temporal-pair compatibility share the same normalized query-choice key. Pair validation uses authorized effective brand sets; compatible scalar aggregates may retain different row limits because each produces one scalar row. Each plan still exactly matches its own requested limit and original filter encoding; grouped limits must match across the pair. Status-filter ordering does not change metric-definition compatibility, while the original definitions and parameters remain intact for disclosure and saved replay.

The interpretation contract includes a bounded `ReportSpec`: purpose, requested summary/performance-table/trend/comparison/limitations/recommendations sections, presentation preference and detail. It has no separate dates or authority. `ReportComposition` binds the spec to the canonical `ResolvedRequest` containing analysis/comparison/action periods; trusted code adds section finding/evidence references after execution. Report mode accepts one or two suitable queries. A temporal report comparison requires the same compatible, nonoverlapping aggregate pair as ordinary comparison; product/demographic comparisons may use one evidence object. Unresolved analytical roles clarify before execution. Sections never authorize extra warehouse jobs or establish causation.

Complete reports require a supported executive finding/observations, scope/dates/definitions/completeness disclosure, complete released evidence and at least one validated relevant feasible action when requested. Bare reports request actions by default. Trends need at least two observed monthly points; requested temporal comparisons need compatible observations covering both released group sets. Missing support yields `partial` with named gaps and recovery. Checked factual grounding and report completeness are independent. Plain section composition honors requested order, table/other presentation and concise versus full row detail; The Rich view uses the same validated findings and evidence, as described below.

Action headings use the resolved quarter/year or explicit interval, with generic **Recommended actions** when no target exists. Pending continuations and metric/status/product/year corrections retain unrelated report choices and role anchors across restart. `InterpretationResult.clear_fields` is a bounded correction-only signal: null action clears its target and null report resets preferences to mode defaults. Omitted nulls inherit; non-null replacements are explicit corrections. Switching to independent analysis uses a new request. Completed continuations and non-temporal corrections retain unchanged temporal expressions with their original application-owned inputs. This preserves an omitted relative action target without converting its symbolic role reference to an unrelated absolute interval. Explicit analysis changes/refreshes keep reference-based roles coherent with their targets; independently anchored unchanged action expressions keep their own original clock. Explicit replacements/clears and new requests remain possible. Explain/summary/save reauthorize and reconstruct the original snapshot without querying for missing report sections. A saved partial report stays visibly partial on those followups.

SQLite migration adds `report_completeness` (default `legacy`) and `report_composition_json`; schema `user_version` advances to 4 without lowering future versions. Existing report contents/versions/proposals are unchanged and never retroactively verified complete. Complete/partial saves, structured grounding/composition and snapshot linkage share one transaction. Exact retries preserve stored content and structured status after current authorization; deleted artifacts cannot be resurrected. Schema 4 also adds `report_support_json` (a versioned bounded released snapshot with original temporal resolution inputs) and `report_binding_digest` (an unkeyed SHA-256 corruption check over the saved artifact inputs). Replay checks the checksum, then independently validates original-clock request/plan binding, released rows under current compiled scope/policy, reconstructed findings/actions, and recomposed section references/completeness against the durable support. The expiring conversation snapshot is not required, and neither model calls nor analytical jobs regenerate a saved report. Checksummed but semantically inconsistent metadata is rejected. This is corruption detection rather than authentication against a local database operator. Pre-binding artifacts keep their content/IDs/versions and expose completion metadata as unavailable legacy; no migration fabricates supporting evidence. Partial outcomes have distinct payload-free telemetry and `outcome.partial` metrics. JSON adds the composition/completeness fields while preserving existing fields; CLI treats useful partial results as nonfatal and displays the saved completeness label. The report tests cover completeness, durable support and replay boundaries.

### Executive CLI presentation

`presentation.py` is a terminal view over released results. The workflow builds additive `TurnResult.presentation` schema version 1 only after successful validation/save, from the bounded (maximum two) released evidence objects, trusted headline/selection provenance, documented defaults and contextual/empty-data state. It contains full evidence precision. The CLI never interprets analytical meaning from `TurnResult.text`. Existing JSON fields and the original plain text remain unchanged. Presentation adds no model/warehouse stage, request parser or budget.

Default output leads with the trusted answer and displays released rows, typed findings, actual temporal roles and effective brand/product filters, then reviewed actions and material definitions/limitations. Every evidence object retains its own statuses/metric meaning, completeness note, original query-as-of and coverage/freshness warning. Null/zero-baseline semantics are preserved. Monetary cells use Decimal `ROUND_HALF_EVEN` to two places; counts are integers, raw churn fractions display as percentages and typed percentage-point findings keep that unit. There is no established currency code, so no currency symbol is inferred. JSON serialization bypasses Rich entirely and retains exact strings for Decimal values. `--verbose` on ask/chat adds routes, turn ID, jobs, bytes and detailed per-evidence metadata with the same trace reference. Titles, names, findings and errors are Rich `Text` objects, preventing markup and hyperlink interpretation. Small screens stack records, and wide tables fold labels without ellipses or row omission.

This is an ephemeral schema, not a database migration or new factual authority. On exact retry, the existing durable artifact checks first validate current authorization, original plans/clocks, released evidence, findings and composition. The view is then rebuilt from that validated support; saved content/IDs/versions/as-of/bindings are unchanged. Existing schema-4 artifacts need no rewrite. Pre-binding/legacy artifacts have `presentation=null`: the CLI shows exact original text and explicitly unavailable structured completeness instead of extracting data from prose. Saved support does not retain the original empty-diagnosis execution outcome; replay discloses that unavailable diagnostic state and possible budget stop, while retaining exact original notes in JSON `text`. The view itself is never read from SQLite or accepted from the model.

Deletion previews retain their entire deterministic plain-text payload, including exact targets/versions/titles, selector, expiry, confirm/cancel commands and soft-delete notice. Their capability tokens are never copied into presentation metadata, model context or telemetry. Errors retain safe application text and gain category-specific recovery guidance; raw provider errors do not enter display. `ask` error exit behavior and interactive recovery remain. `chat --json` supports newline-delimited input/results without prompts; `/schema` emits a separate schema-help object.

Behavioral coverage is in [tests/test_executive_output.py](tests/test_executive_output.py), alongside security, lifecycle and execution regressions. No human usability study or independent final acceptance is claimed.

## 4. Policy and privacy boundaries

The prototype implements schema-versioned bounded continuity in SQLite: screened user messages, trusted assistant clarifications, typed pending requests, and a maximum-two-result released-evidence snapshot with typed grounded selections. Raw input, raw warehouse rows, arbitrary model source phrases, credentials and confirmation tokens are excluded from this new state. The default window is six messages/2,400 characters and 2,000 characters per input; compact typed choices remain when old messages leave the window. No automatic history summarization is used. Snapshots and pending requests expire within 30 minutes, without sliding renewal for continued clarification/explanation, and expired payloads are removed on access. These defaults are configurable within hard bounds in `Settings`.

Current snapshots use schema version 2, with the original analytical reference date and prior period/cutoff needed for symbolic resolution, plus an integrity digest over request, plans and resolution inputs. Request metadata is checked against original-clock resolution and validated executed plans, including only supported Golden transforms and the exact derived investigation plan. This detects inconsistent cached metric/date/role/filter metadata before it enters a model prompt. Ordinary requested/executed limits must match exactly. The only limit exception is the supported ungrouped monthly-revenue trend method, with its exact `min(20, max(12, requested_limit))` result; an operation change to monthly time series must use that transformed limit. Under-cap mismatches do not establish compatibility. The digest is a corruption check, not authentication or a replacement for policy checks; snapshots from version 1 lack these fields and become unavailable. Trusted clarification conversion retains the model's context action so continuation does not renew pending expiry or discard the active exchange. Pending and completed temporal roles retain validated per-role application reference dates and prior period/cutoff inputs. Completed corrections select these inputs from the reauthorized, semantically validated snapshot; compatible version-2 snapshots without overrides supply their original global inputs. Unchanged expressions keep those anchors across continuation/restart and corrections to other fields; newly supplied or changed temporal fields use the reply clock, including a newly supplied relative year. The interpretation's bounded `temporal_updates` tuple identifies an explicit same-concept temporal refresh; source provenance alone does not refresh it. Clocks/anchors remain application-owned. Reference expressions resolve through their target role's anchor, and completed snapshot digests/semantic checks cover the used anchor map. Existing completed version-2 snapshots with no overrides retain their original global-clock behavior. Legacy pending date expressions without anchors require restatement before model/analytical execution; lifecycle timestamps never substitute for missing analytical inputs. Pending comparison/action roles cannot silently disappear during slot completion. Deterministic temporal comparisons retain their actual selected/refused/fallback provenance on first release and reuse.

Before older context enters a model prompt or answer, the workflow reloads the catalog and identity, checks identity-file/catalog fingerprints, owner, role, full effective brands and authorization version, recompiles saved plans and checks metric/field-policy/execution-scope metadata. Catalog or authorization dependency failure fails closed within the same query budget. Cached rows pass the structural release validator again; factual and suggestion text is regenerated from selections. Legacy plan-only rows have unavailable reuse metadata. Explanations and summaries label original as-of time and use no fresh analytical jobs; exact snapshot saves never silently substitute new figures. Report IDs link to snapshots in the save transaction; deletion invalidates linked context, and access checks active report state. Demographic provenance is conservative across pending and intervening requests; a new conversation is required for customer ranking after demographic context.

The analytical reference clock is `AssistantWorkflow(today=...)` or the UTC current day. Context/deletion lifecycle time is `SQLiteStore(clock=...)` or real UTC time, independently of analytical date arithmetic. A restarted test/evaluation must inject the same store clock again. Warehouse completion timing stays in released evidence and is not a promise of source freshness. Natural cancellation affects pending analysis only; deletion cancellation requires its exact preview token and remains model-independent. Local SQLite/WAL/backup bytes follow local retention, not a production purge guarantee. Context and lifecycle tests exercise these boundaries using injected clocks.

Input screening refuses direct PII requests, demographic membership requests, forged role claims and detected instruction attacks before model access. The structured model interpreter classifies unrelated/unsupported tasks, without a semantic keyword gate. Known prohibited fields are omitted structurally. Free-text PII detection is supplemental and not a proof of absence. Product/brand names are kept out of generic person-name checks where possible to reduce false positives. Safety is enforced again on model drafts and evidence before display/save.

Brand authorization is deterministic and precedes aggregation. Explicit unauthorized product/brand requests fail without widening scope. Product IDs are resolved through the current catalog and bound as parameters. Customer IDs are permitted only in the customer metrics shape and checked against scoped membership; demographic data releases aggregate values only. The local identity file is trusted configuration, not an authentication mechanism. Anyone with access to the machine/database can choose a different demo user or alter local files.

Threat limits: five-customer group size does not by itself stop repeated-query differencing; production needs complementary suppression and a cross-query privacy budget. The CLI cannot defend against a malicious local operator. OpenRouter is an external intermediary; live use was authorized for this exercise but does not automatically approve production processing. No raw PII or credentials belong in model requests, reports or logs.

## 5. Failure, recovery and budgets

Per turn: 120 seconds active time; at most 6 model requests across direct/fallback/repair/report drafting; at most 2 interpretation/plan repairs; at most 8 BigQuery executions; at most 12 dry runs including diagnostic attempts; 512 MiB estimated bytes per query; 1 GiB cumulative bytes per turn; 30 seconds per warehouse job. Model request lengths and outputs are bounded. SDK/provider retries are disabled or included in the shared count. OpenRouter is used only on classified transient direct Gemini failure, not policy or permission failure.

| Failure | Behavior |
|---|---|
| Missing period/metric/product | `needs_clarification` before analytical execution; authorized catalog and model calls can precede it |
| Invalid interpretation/plan contract | Bounded structural repair; stop after two repairs; provider/malformed-output failures remain errors |
| Unauthorized brand/product or PII request | Fail closed before model/warehouse evidence release |
| Compiler syntax error | Regenerate once from the identical validated plan and scope; parse/dry-run again; persistent defect returns safe trace ID |
| BigQuery syntax/schema rejection | One same-plan sealed-query regeneration after bounded metadata refresh; no raw model SQL fallback |
| True empty data | Say no matching data; do not widen filters or turn `COALESCE` zero into measured revenue |
| Dry-run transient failure | At most a bounded retry within dry-run and time budgets |
| Submitted job ambiguous failure | Do not resubmit; cancellation/billing state may be uncertain |
| Per-job/cumulative budget/deadline exceeded | Stop before further work; disclose incomplete evidence if already verified |
| Gemini transient | Attempt OpenRouter Gemini if configured and within the same request/time cap |
| Both routes/refusal/malformed output | Checked-evidence response or safe failure; never fixtures or invented values |
| SQLite unavailable/save failure | Do not claim a report save or deletion succeeded |
| Unknown BigQuery billing metadata | Stop; represent billed bytes as unknown and reserve budget conservatively |
| Telemetry write failure | Continue best-effort only where safe; telemetry is not a correctness dependency |

The production design adds service-level deadlines, durable queues/outboxes, permission version refresh, cross-service idempotency, managed secrets, database backups, and dead-letter handling. The prototype's local JSONL log is best-effort and is not an audit trail.

## 6. Prototype evidence and production substitutions

| Capability | Implemented prototype | Production substitution/design |
|---|---|---|
| Identity | Seeded manager/CEO JSON + required `--user` | OIDC JWT validation, auth-version/revocation check, verified gateway context |
| Query access | Local adapter directly calls public BigQuery with ADC | Separate Data Access Service and private curated views; gateway owns warehouse identity |
| Operational data | Local SQLite reports, conversation context and proposals | Cloud SQL PostgreSQL with transactions, migrations, PITR, private network and roles |
| Golden | Three authored synthetic JSON trios; only `GK-001 v1` approved global methodology | Versioned GCS sources + PostgreSQL full-text/pgvector approved retrieval; candidate worker and reviewer/promotion rules |
| Model | `google-genai` direct Gemini; authorized transient fallback via `httpx` OpenRouter Gemini | Prefer Vertex Gemini; any second provider/model requires privacy/legal approval, same contracts and independent failure evaluation |
| PII | Structural field allowlist and deterministic rules; optional Presidio extra | Same structural boundary plus DLP controls, privacy review and measured false-negative/positive monitoring |
| Observability | Sanitized local JSONL and derived counters | OpenTelemetry collector, Cloud Logging/Monitoring, protected dashboards and alerting |
| Preferences/persona | No persisted preference system or nondeveloper editor | Per-user preference records; approved versioned persona UI with preview and rollback without deployment |
| Deletion | Owner-bound soft delete in local SQLite; no purge | PostgreSQL transaction + audit/outbox; GCS/index purge and backup expiry under approved retention policy |

No production deployment, private warehouse, cloud identity, managed service, purge, vector search, approval automation, persona administration, charts, email or web extension is present in this repository.

## 7. Evaluation method and verification limits

Offline tests use explicit reference rows, injected clocks, deterministic model/warehouse adapters, temporary SQLite and isolated telemetry. They test mixed-brand baskets, metric grain, status/null/date boundaries, scope leakage, PII sentinels, finite/released values, zero denominators, context expiry/reauthorization, report grounding/completeness, exact saves and deletion ownership/confirmation/replay/rollback. Presentation tests retain full precision in JSON, literal markup and narrow-terminal behavior. The optional BigQuery CTE suite specifies expected synthetic totals independently of the compiler. Offline fixtures do not prove live interpretation quality or production availability.

The prepared public copy passed 998 offline tests / two opt-in integration skips / two existing Typer/Click warnings. The original and public `tests/` collections contain identical 1,000 test nodes; internal archived review cases are not part of the public copy. The public copy retains the same application, tests, fixture/scenario bytes and selected drivers, with consolidated documentation and a derived source manifest. A prior fresh same-machine environment passed installation and 860 tests on an earlier source; neither an independent-machine nor a current-public-revision fresh installation is established.

Historical bounded live checks exercised representative model/BigQuery analyses, role corrections, reports and exact saves. Latest complete targeted coverage spans nine workflow scenarios / 20 turns plus eight interpretation probes across two adjacent application freezes; four workflow turns belong to the last application freeze. The original full evaluation remains 63/72 scenarios PASS and 85 PASS / 7 FAIL / 2 PROVIDER_FAILURE / 5 NOT_RUN turns. It is not rewritten as a passing final-source evaluation. Public-source mutability and current-status restatement limit comparisons. Successful repair of an initially expanded unchanged exact-save interpretation is offline-tested; live exact save used a compact initial interpretation. Safe rejection of explicit report modifications was observed live.

For report intent, compare metric/population/date/baseline/action roles against the request, independently recompute numerical fixture findings, require evidence references for factual claims and actions, and inspect completeness/caveats. Score unsupported causation or missing sections as failures rather than accepting fluent prose. LLM judging may triage wording but is not the numerical authority. Fixed-snapshot paraphrase consistency has not been comprehensively measured.

For UX, the proposed production gate uses 5–8 representative executives, with neutral tasks for analysis, clarification/correction, reporting/actions, exact save, deletion preview/cancel/confirm and a recoverable failure. Measure completion without moderator rescue, clarification count, time to useful answer, metric/date/scope/completeness understanding and deletion-target understanding. No human study, measured task timing or production acceptance occurred for this prototype. [Examples](EXAMPLE_RUNS.md) are an inspection walkthrough, not participant evidence.

## 8. Framework experience supplied by applicant

Applicant statement, supplied for this submission: “I have experience with LangGraph and have built a few production-ready projects with it.” The applicant has not specified the projects or whether they can be described publicly; no further detail is inferred here.

The implementation uses LangGraph's state graph to make turn stages explicit. Tests exercise graph invocation and CLI-level behavior, but graph node-level crash resume and production checkpointing were not verified. This is implementation evidence, not a claim about the applicant's prior experience.

## 9. Relevant implementation references

- CLI and setup: [`src/opsfleet_assistant/cli.py`](src/opsfleet_assistant/cli.py), [`README.md`](README.md), [`pyproject.toml`](pyproject.toml), [`requirements.txt`](requirements.txt)
- Interpretation and temporal resolution: [`contracts.py`](src/opsfleet_assistant/contracts.py), [`intent.py`](src/opsfleet_assistant/intent.py), [`periods.py`](src/opsfleet_assistant/periods.py)
- Workflow, model and policy: [`workflow.py`](src/opsfleet_assistant/workflow.py), [`model_gateway.py`](src/opsfleet_assistant/model_gateway.py), [`safety.py`](src/opsfleet_assistant/safety.py)
- Identity, metrics, privacy and SQL: [`identity.py`](src/opsfleet_assistant/identity.py), [`metrics.py`](src/opsfleet_assistant/metrics.py), [`privacy.py`](src/opsfleet_assistant/privacy.py), [`compiler.py`](src/opsfleet_assistant/compiler.py)
- Warehouse, evidence, state, deletion, Golden, telemetry: [`bigquery_adapter.py`](src/opsfleet_assistant/bigquery_adapter.py), [`evidence.py`](src/opsfleet_assistant/evidence.py), [`store.py`](src/opsfleet_assistant/store.py), [`deletion.py`](src/opsfleet_assistant/deletion.py), [`golden.py`](src/opsfleet_assistant/golden.py), [`telemetry.py`](src/opsfleet_assistant/telemetry.py)
- Usage and verification: [README](README.md), [example interactions](EXAMPLE_RUNS.md), and the retained [tests](tests/).

## Submission behavior and evidence boundary — 6 November 2026

The exact-save route compares typed/resolved analysis and report choices with the released snapshot before persistence. Date, metric, filter, status, grouping, action-horizon or report-preference changes return clarification, preserve the original snapshot and save nothing. Request changes separately, review the result, then request an unchanged save. If only report choices differ, one remaining interpretation slot may distinguish explicit changes from defaults added by the model; any unresolved difference still clarifies. The comparator cannot detect a constraint entirely omitted by interpretation. Full modification-and-save orchestration is absent.

Interpretation input reserves essential typed context within 12,000 characters and drops only optional oldest whole history messages; essential overflow clarifies before dispatch. Interpretation output is bounded to 5,000 tokens, explanation selection to 2,000 on both routes. Truncation retains provider-failure provenance. Request/time/retry limits and grounding remain unchanged. See [model_gateway.py](src/opsfleet_assistant/model_gateway.py), [workflow.py](src/opsfleet_assistant/workflow.py) and their regression tests. These are bounded reliability controls, not availability guarantees.

## 10. Assignment requirement coverage

| Requirement | Prototype and production treatment |
| --- | --- |
| Hybrid intelligence | Authored synthetic Golden retrieval/method demo in [golden.py](src/opsfleet_assistant/golden.py); production approved-trio ingestion, validation, versioning, retrieval and rollback in [HLD §6](HLD.md#6-golden-candidate-approval-retrieval-and-rollback). No real analyst corpus was supplied. |
| Safety and PII | Structural field/result contracts, deterministic scope before aggregation, safe evidence release and input/output checks; production verified identity, separate data gateway, curated views and privacy budget in [HLD §§2–3](HLD.md#2-deployment-and-trust-boundaries). |
| Destructive oversight | Owner-bound exact preview, explicit one-time confirmation and transactional version recheck; production audit/outbox and retention cleanup in [HLD §5](HLD.md#5-confirmed-report-deletion-flow). |
| Continuous improvement | Persistent user preferences and production system learning are design-only. Explicit/inferred preference provenance, correction/reset and controlled Golden promotion are in [HLD §§6–7](HLD.md#7-operational-state-learning-retention-and-extension). |
| Resilience | Shared turn/call/byte budgets, bounded same-plan SQL recovery, honest empty data, transient fallback and safe failures; production retry ownership and distributed recovery in [HLD §7](HLD.md#7-operational-state-learning-retention-and-extension). |
| Quality assurance | Offline reference fixtures/regressions, opt-in integrations, bounded live observations, report rubric and proposed human UX gate in §7 above and [HLD §8](HLD.md#8-evaluation-and-release-gates). |
| Observability | Sanitized trace IDs/stages/routes/jobs/counters locally; production OpenTelemetry, dashboards, alerts, retention and access controls in [HLD §7](HLD.md#7-operational-state-learning-retention-and-extension). |
| Persona management | Nondeveloper editing, preview/evaluation, approved version pointer, request pinning and rollback without deployment are design-only in [HLD §7](HLD.md#7-operational-state-learning-retention-and-extension). |
