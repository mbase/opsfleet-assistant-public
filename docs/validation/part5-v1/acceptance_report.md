# Prototype verification summary

## Current evidence and limits (9 November 2026)

At application revision `681b855`, the offline suite passed **1,578 tests with two opt-in integrations skipped**. CLI help/schema and all three guarded demos passed. A fresh Python 3.13 editable installation from a source copy passed dependency checks, the installed CLI, demos and its offline suite: **1,576 passed, four skipped**. The additional skips require Git metadata, which is absent from the clean source copy. The checks used isolated temporary state with live model and warehouse transports disabled.

Earlier revisions recorded a 45-case synthetic-input model trial with 45/45 first-semantic and final-workflow passes, a public-warehouse smoke check with 29/29 assertions (four executed jobs, 80 MiB billed), and a selected behavior smoke check with 16/16 turns. Current-revision verification is offline. Historical results remain evidence for their original revisions.

Broader warehouse acceptance, remote CI execution and representative human usability remain unverified. This summary describes prototype verification, not production readiness.

| Gate | Recorded status | Evidence and limit |
| --- | --- | --- |
| Offline suite and CLI demos | PASS, current application revision | 1,578 passed, two opt-in integration skips; help/schema and all three guarded demos passed. |
| Fresh Python 3.13 install | PASS, current application revision | Documented constrained editable install, dependency check, installed CLI and all demos passed; 1,576 tests passed, four skipped in the source copy. |
| Fresh Python 3.12 install | HISTORICAL observation | Previously recorded locally; not repeated during this review. |
| Model fixture interpretation | HISTORICAL observation | 45/45 first-semantic and final-workflow passes on synthetic context at an earlier revision; independent paraphrase review remains outstanding. |
| Live warehouse | HISTORICAL, partial | One 29-assertion public-data smoke check; not the broader warehouse acceptance set. |
| Remote CI | NOT_RUN | Workflow definition is present; no remote run is claimed. |
| Human usability | NOT_RUN | Five-participant protocol is proposed; no participant study is claimed. |
| Optional privacy runtime | NOT_RUN | An import check does not establish NLP model/runtime availability. |

Keep source identities and per-run records separate. Do not treat finite passing observations as proof of universal correctness, privacy, reliability or general model accuracy. A provider reservation is not a provider-enforced tariff limit. Prototype verification, independent review and production acceptance are separate decisions.

## Recording future evaluations

- Record application HEAD/tree, input revision and manifest SHA-256.
- Record committed/working-tree status and excluded narrative/output files.
- Record interpreter, platform, exact command and dependency metadata/constraints hashes.
- Record driver, prompt, policy, metric, playbook, artifact/rendering versions and fixture/scenario hashes.
- Record reviewer identity/date and independent paraphrase review reference.

Attach sanitized per-case records for a candidate-specific review: status; first attempt; every later attempt/repair; failures/provider failures; calls/jobs/dry runs; known/unknown bytes/cost; per-case latency. Publish failures and not-run cases. Use individual observations for small samples; do not label them p95. Record cleanup success/failure and remaining private-state retention without exposing its content.

Record original failed freezes/runs separately if code or expectations change; bind any rerun to a new input revision. The current version-bound inputs and execution artifacts remain separate; do not overwrite historical identities or results.
