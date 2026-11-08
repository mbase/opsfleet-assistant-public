# Version-bound acceptance report template

Verdict: INCOMPLETE / awaiting owner review. This template is not a passing acceptance decision.

- Application HEAD/tree, input revision and manifest SHA-256:
- Committed/working-tree status and excluded narrative/output files:
- Interpreter, platform, exact command, dependency metadata/constraints hashes:
- Driver, prompt, policy, metric, playbook, artifact/rendering versions and fixture/scenario hashes:
- Reviewer identity/date and independent paraphrase review reference:

| Layer/gate | Status | Planned / executed / reviewed denominator | Evidence and limits |
| --- | --- | --- | --- |
| Deterministic security/privacy | NOT_RUN | | All mandatory cases; zero observed escapes required |
| Deterministic numerical correctness | NOT_RUN | | Independent source rows/tolerances; undefined stays undefined |
| Persistence/deletion | NOT_RUN | | Exact-save/owner/stale/replay/expiry/atomicity |
| Recovery | NOT_RUN | | Expected bounded outcome; no ambiguous replay |
| Python 3.12 install | NOT_RUN | | Fresh local and CI recorded separately |
| Python 3.13 install | NOT_RUN | | Fresh local and CI recorded separately |
| Optional privacy | NOT_RUN | | Import, NLP model and actual runtime distinguished |
| Model fixture interpretation | NOT_RUN | 45 / 0 / 0 | >=95%, zero silent population/scope substitution |
| Live warehouse | BLOCKED_ENVIRONMENT | 15 / 0 / 0 | Same-snapshot reference, expected journey states |
| Human usability | NOT_RUN | 5 proposed participants / 0 | Consent; unaided retrieval/deletion and exact target comprehension |

Attach immutable sanitized per-case records: status; first attempt; every later attempt/repair; failures/provider failures; calls/jobs/dry runs; known/unknown bytes/cost; per-case latency. Publish failures and not-run cases. Use individual observations for small samples; do not label them p95. Record cleanup success/failure and remaining private-state retention without exposing its content.

Record original failed freezes/runs separately if code or expectations change; bind any rerun to a new input revision. Finite passing observations do not establish universal correctness, privacy, reliability or general model accuracy. Owner acceptance, independent review and implementation readiness are separate decisions.
