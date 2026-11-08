# Human review protocol v1 — NOT_RUN

No participants have been contacted. Owner approval and informed participant consent are required before any sessions. Suggested formative sample: five adults unfamiliar with implementation, with retail analytics responsibilities where feasible. Record actual sample/roles and selection limits. Five participants cannot establish representative executive usability or general model accuracy. A developer walkthrough is recorded separately and never counted as a participant.

Use one approved synthetic snapshot and disposable database per participant, with no real customer information. Explain recording, purpose, voluntary participation, withdrawal and retention before consent. Record pseudonymous IDs only; no names, contact details, raw transcripts or screen recordings in Git. Proposed retention: observations 30 days in restricted temporary storage; publish only consented aggregate/anonymized observations. Confirm the owner's retention policy before study.

Task cards (read exactly; do not teach commands first):

1. Find Q1 2025 revenue for your authorized brands. Explain which items/statuses and dates count. Identify one useful result and one limitation.
2. Change the period to Q2 2025 and use Complete items only. Explain what changed and whether a new result was calculated.
3. Investigate Texas versus California spend per qualifying purchaser. Explain the denominator and why components do not prove causes.
4. Save the result, close/restart, locate and reopen the same report. Explain whether reading it refreshes the data.
5. Preview deletion of the report made in this conversation. Before confirming, state the exact target and whether other reports will change. Cancel once, preview again and confirm. Explain soft deletion and retained bytes.

Do not rescue unless the participant asks to stop or the agreed timeout (five minutes/task) is reached. A rescue is any hint naming a command, correcting a target, or supplying interpretation. Record it and mark unaided completion false; never rewrite it as a success. Stop a deletion task before confirmation if target understanding is wrong, recording this as unsuccessful without rescue.

Observation sheet (one row/task): participant pseudonym; consent/approval reference; application/manifest hash; task; start/end UTC; time to useful answer in seconds; completion without rescue (yes/no); clarification count; wrong turns; rescue count; population/scope explanation correct (yes/no plus sanitized observation); exact deletion target understood (yes/no/not applicable); cancellation/confirmation result; participant difficulty (1–5); observer notes without private text. Record abandoned tasks and technical/provider failures separately.

Proposed gate: every required retrieval and deletion task completes without moderator rescue, every deletion target is correctly understood before confirmation, and scope/population explanations are accurate. Publish all five participants' task outcomes, actual denominators, exceptions and sample limitations. If any required task fails, usability acceptance remains unmet. Other analysis/correction tasks remain visible even when retrieval/deletion pass.
