# Installation and owner-run commands

Run from a clone/source checkout on an existing Python 3.12 or 3.13. Wheels are not a supported distribution: configuration/default resources live beside the editable source tree.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --index-url https://pypi.org/simple -c requirements.part5.lock setuptools==82.0.1
python -m pip install --index-url https://pypi.org/simple --no-build-isolation -c requirements.part5.lock -e '.[test]'
python -m pip check
python scripts/offline_check.py help
python scripts/offline_check.py schema
python scripts/offline_check.py tests
python scripts/offline_check.py golden
python scripts/offline_check.py playbooks
python scripts/offline_check.py navigation
```

Use `python3.13` for the other supported interpreter. These commands do not require provider credentials. The offline wrapper drops inherited provider/project/ADC selectors, isolates HOME/telemetry, suppresses root dotenv reads and blocks socket/DNS transports. Dependency installation contacts public PyPI. Package version constraints pin runtime/test/build dependencies to the accepted observed resolution; artifact hashes are not a complete package supply-chain lock. Pip bootstrap comes from the chosen interpreter's ensurepip. Runtime and tests share this one supported constraint route; the test extra adds pytest. `requirements.txt` remains an unconstrained editable convenience entry and `requirements.step4.lock` remains historical.

Ordinary regression tests need no preliminary `--freeze`. The guarded wrapper
creates a temporary `LOCAL_TEST_IDENTITY` manifest with no approval grants from
the current inventory and Git identity (when present). The same workflow supports
source copies without `.git`, including the fresh-copy checker below. It never
rewrites the tracked candidate or archived live records, even when tests fail.
The SQL equality correction and temporary-manifest changes were checked offline
only; prior live observations do not validate this changed source.

Fresh isolated install/copy checks (never alter an existing environment or install an interpreter):

```sh
python scripts/check_fresh_environment.py --part5 --python python3.12 --output /tmp/opsfleet-install-312-new
python scripts/check_fresh_environment.py --part5 --python python3.13 --output /tmp/opsfleet-install-313-new
python scripts/check_fresh_environment.py --part5 --python python3.13 --privacy --output /tmp/opsfleet-privacy-new
```

Output directories must be new. The privacy trial installs/imports the separate optional extra, not an NLP model. It never initializes AnalyzerEngine (which can download a model). Optional transitive privacy dependencies are not frozen or certified; no scanner-runtime pass follows from a successful import.

## Freeze and zero-network review

The interpretation provider protocol now sends `request.playbook` as a nullable
`id|population|segment` enum token. `unspecified` preserves an absent choice;
the gateway reconstructs the existing version-1 playbook object before local
validation. Stored requests, plans and snapshots keep their existing format.
Array length constraints are enforced locally rather than in the provider
schema. Invalid/oversized replies still fail the interpretation contract; there
is no truncation, inferred population, schema fallback or automatic paid retry.
A recorded 45-case synthetic-input interpretation trial is summarized in `acceptance_report.md`. It is a finite observation, not general provider/model reliability evidence; independent paraphrase review and owner acceptance remain separate gates.

The provider budget charges known settled costs plus a worst-case reservation
for each in-flight call and the next proposed call. Unused reservations are
released only after a valid reported cost arrives. Unknown billing, an excessive
per-call charge or insufficient remaining total budget stops further dispatch.
Continuation experiments must deduct the previous experiment's reported spend,
provider calls and active time; a fresh state must not reset the owner's budget.

Previous manifests/results identify the old source. Do not resume those runs
or overwrite their manifests. A fresh manifest must reference the new freeze,
and a fresh live allowance must be reviewed: the original six-request pilot
allowance was consumed by the failed pilots and controlled diagnostics.

```sh
python scripts/prepare_part5_validation.py --audit-lock
python scripts/prepare_part5_validation.py --freeze
python scripts/run_live_evaluation.py --manifest docs/validation/part5-v1/candidate_manifest.json --run-id model-review --layer interpretation --dry-run
python scripts/run_live_evaluation.py --manifest docs/validation/part5-v1/candidate_manifest.json --run-id warehouse-review --layer warehouse --dry-run
python scripts/run_live_evaluation.py --manifest docs/validation/part5-v1/candidate_manifest.json --help
```

Audit-lock requires the existing local, unadopted `uv.lock`; on a clone without it, inspect the saved `lock_audit.json`. Freeze records the current complete filesystem input inventory and baseline Git identity; it does not commit anything. Verification rejects additional executable/config/test inputs, including untracked/ignored additions and root test configuration. In Git trees it also verifies the tracked application diff hash. The supported manifest script verifies this before avoidable application imports; help uses only the stdlib parser. This is identity checking, not a Python startup security sandbox. Narrative reports, archived `history/` evidence and generated results are explicitly excluded from recursive input hashes. Preserve each previous candidate manifest under a unique name before intentionally freezing changed inputs. Preparation does not establish an immutable Git commit for Part 5.

## Owner terminal only, after plan review

Codex must not run the following live commands. Review the exact model (`google/gemini-3.8-flash`, OpenRouter-only), billing project (currently **unset**), dataset (`bigquery-public-data.thelook_ecommerce`), snapshot (currently **unset**), scenario assertions, maximum call/byte/time ceilings, estimated cost (currently **unknown**), price/token reservation and data handling first. Have an independent reviewer check the 15 paraphrases. Set the following in a uniquely named **copy** of the manifest, retaining the original candidate:

- `provider.estimated_cost_usd`, `cost_limit_usd`, `maximum_cost_per_call_usd`: reviewed positive values based on provider tariff, interpretation input/output caps (12,000 characters/5,000 output tokens), drafting caps and repairs. Plan for the entire hard call ceiling, not only nominal calls.
- `review.owner_approved`: true only after explicit owner approval. `review.paraphrases_independently_reviewed`: true only after the documented review (required for interpretation).
- For warehouse: `warehouse.project`, fixed UTC `snapshot_timestamp`, `same_snapshot_authorized`: true only for confirmed available/authorized time travel. Stay within source retention for **every** table; otherwise keep the layer blocked. There is no alternate mutable-source route.

These manifest fields are public identifiers/estimates, never secrets. Configure variable **names** `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`; warehouse additionally uses `GOOGLE_CLOUD_PROJECT`, `BQ_LOCATION=US`, and ADC or `GOOGLE_APPLICATION_CREDENTIALS`. Do not place secret values in commands or reports. The model must match the reviewed manifest; direct Gemini fallback is disabled. The operator must review OpenRouter's data handling; synthetic interpretation trials send only synthetic context. Warehouse trials send bounded released business aggregates to the configured model. Raw checkpoint/results and SQLite state stay in mode-0700 temporary storage, never Git. Public output contains only allowlisted IDs, statuses, boolean checks and counters.

The following creates a *name* for a new disposable state directory without creating it; the driver must create it itself. Choose a fresh run ID and state for each layer. Use the owner-reviewed manifest copy rather than editing the preserved candidate.

```sh
STATE="$(python -c 'import pathlib,tempfile,uuid; print(pathlib.Path(tempfile.gettempdir()).resolve()/str(uuid.uuid4()))')"
python scripts/run_live_evaluation.py --manifest /path/to/owner-reviewed-manifest.json --run-id interpretation-owner-001 --layer interpretation --state "$STATE" --live --confirm-owner-run

STATE="$(python -c 'import pathlib,tempfile,uuid; print(pathlib.Path(tempfile.gettempdir()).resolve()/str(uuid.uuid4()))')"
python scripts/run_live_evaluation.py --manifest /path/to/owner-reviewed-manifest.json --run-id warehouse-owner-001 --layer warehouse --state "$STATE" --live --confirm-owner-run
```

The sanitized preflight is printed before dispatch. Missing approval/configuration/oracle/cost estimates fail before credentials/ADC/clients load. Select a whole scenario with `--select warehouse-report-lifecycle` or `--select product-p1-r1`; dependency chains are indivisible. Resume only explicitly selected scenarios with the exact original manifest, state and run ID:

```sh
python scripts/run_live_evaluation.py --manifest /path/to/owner-reviewed-manifest.json --run-id interpretation-owner-001 --layer interpretation --state "$STATE" --resume --select product-p1-r1 --live --confirm-owner-run
```

Save each layer's state path separately before starting another layer. Resume does not rerun any attempted case, including failures or interrupted attempts. Unknown dispatch/billing halts the run. Public immutable result snapshots are under ignored `docs/validation/part5-v1/runs/RUN_ID/`; private checkpoints remain at the explicit state path. Preserve failed first attempts; intentional reruns need a new experiment ID.

Cleanup: after reviewing/exporting the sanitized records, close all processes and remove **only the printed disposable state directory**, using the owner's normal file manager or carefully checked terminal command. Removal destroys report-state/checkpoints and the ability to resume; record cleanup status explicitly. No source/warehouse mutations require rollback. Soft-deleted report bytes exist only inside that disposable SQLite/WAL state. Do not delete the user's `runtime/`, credentials, `.venv`, original lockfile or historical evaluation records.


## Explicit dotenv file for live execution

Add `--env-file .env` to the reviewed manifest command to read the project's existing dotenv file. No shell `source` or manual key export is needed. Only confirmed live mode reads this file; default/help/dry-run and unapproved plans do not read credentials. Values present in the explicitly chosen file override corresponding exported variables, preventing a stale shell key/model from silently winning. Missing fields may still come from exported variables. `OPENROUTER_MODEL` defaults to the reviewed manifest when absent; `BQ_LOCATION` defaults to US. A conflicting model/project fails closed. API credentials are never printed or saved to result files. The parser reads literal `KEY=value` lines with optional matching quotes; it does not execute shell expressions or interpolate variables.

The model trial requires `OPENROUTER_API_KEY` in the file or environment; a `GEMINI_API_KEY` is a different service credential and is not a substitute. The file can also supply `OPENROUTER_MODEL`, `GOOGLE_CLOUD_PROJECT` and `BQ_LOCATION`. Warehouse ADC remains configured through the existing supported ADC/environment method; the dotenv option does not import arbitrary environment variables or credential files. Keep dotenv files ignored by Git.

```sh
python scripts/run_live_evaluation.py --manifest "$MANIFEST" --env-file .env --run-id "$RUN_ID" --layer interpretation --select product-p1-r1 --state "$STATE" --live --confirm-owner-run
```

The terminal now prints a final `results_saved` summary with selected case statuses, counters and the immutable sanitized result path after execution. The earlier preflight summary is not the final execution result. Missing/unreadable dotenv files, missing OpenRouter keys and model/project mismatches use fixed diagnostic codes without values. This option does not fix or diagnose a provider HTTP400 by itself.
