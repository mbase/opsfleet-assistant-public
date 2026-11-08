# Version-bound acceptance report

## Current evidence and limits (9 November 2026)

Part 5 remains incomplete. The latest recorded offline suite reports 1,538 passed and two opt-in skips; all three guarded CLI demos passed. A 45-case synthetic-input model trial recorded 45/45 first-semantic and final-workflow passes. A limited public-warehouse smoke check recorded 29/29 assertions, four executed jobs and 80 MiB billed; a selected legacy-behavior smoke check recorded 16/16 turns. These finite observations do not establish broad warehouse acceptance. Remote CI execution and representative human usability have not been verified. Overall Part 5 acceptance is incomplete; this summary is not production readiness.

| Gate | Recorded status | Evidence and limit |
| --- | --- | --- |
| Offline suite and CLI demos | PASS | 1,538 passed, two opt-in skips; all three guarded demos passed. |
| Fresh Python 3.12 and 3.13 installs | PASS locally | Local install results were recorded for the candidate; remote CI execution remains unverified. |
| Model fixture interpretation | OBSERVED, finite sample | 45/45 first-semantic and final-workflow passes on synthetic context; independent paraphrase review and owner acceptance remain separate gates. |
| Live warehouse | PARTIAL | One 29-assertion public-data smoke check; not the broader warehouse acceptance set. |
| Remote CI | NOT_RUN | Workflow definition is present; no remote run is claimed. |
| Human usability | NOT_RUN | Five-participant protocol is proposed; no participant study is claimed. |
| Optional privacy runtime | NOT_RUN | An import check does not establish NLP model/runtime availability. |

Keep source identities and per-run records separate. Do not treat finite passing observations as proof of universal correctness, privacy, reliability or general model accuracy. A provider reservation is not a provider-enforced tariff limit. Owner acceptance, independent review and implementation readiness are separate decisions.

## Candidate-specific review record

- Application HEAD/tree, input revision and manifest SHA-256:
- Committed/working-tree status and excluded narrative/output files:
- Interpreter, platform, exact command, dependency metadata/constraints hashes:
- Driver, prompt, policy, metric, playbook, artifact/rendering versions and fixture/scenario hashes:
- Reviewer identity/date and independent paraphrase review reference:

Attach sanitized per-case records for a candidate-specific review: status; first attempt; every later attempt/repair; failures/provider failures; calls/jobs/dry runs; known/unknown bytes/cost; per-case latency. Publish failures and not-run cases. Use individual observations for small samples; do not label them p95. Record cleanup success/failure and remaining private-state retention without exposing its content.

Record original failed freezes/runs separately if code or expectations change; bind any rerun to a new input revision. The current version-bound inputs and execution artifacts remain separate; do not overwrite historical identities or results.
