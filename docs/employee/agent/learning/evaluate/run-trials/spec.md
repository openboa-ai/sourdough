# Grade Agent judgment without steering the Agent

Design draft · Verification **UNRUN** · Responsible component: Offline evaluation runner

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Use the [integration rubric](../../../../../acceptance/first-shift/spec.md#agent-evaluation), a pinned Agent configuration, toolset, decision-time observations and a separate evaluator. The evaluator can reset isolated trial companies, never an operating company. It cannot call the Agent's decision tool on its behalf.

Input contracts: [Real Agent turn](../../../harness/execute/tool-turn/spec.md) · [Decision receipt](../../../../../company/systems/work/discover-work/decide/spec.md). Profiles supply their own observations and coverage measures.

## Design decision and review

Grade the recorded decision and tool effects separately rather than using profitable trades as a proxy for either.

**Concern for the responsible component:** Seal labels and budgets outside Agent tools. Retain failed attempts and calibrate the subjective rubric against independently labeled boundary cases.

### Out-of-band immutable evaluation boundary and offline trial ledger

Keep one **physically separate evaluator SQLite store** per sealed suite, strictly outside every trial company store, Agent writable Desk and Agent capability scope so neither the trial runner, sealed holdout task bank, cost meter, trace recorder nor graders can be read or altered by any Agent. Register the suite before launching any worker. The canonical registration pins the protocol, ordered trial slots, case and family identities, isolated company starting-state (including cloned permitted Desk snapshots) and authorized input revisions, rubric and evaluator revisions, granted tool manifest, runtime profile and resource ceilings. A paired slot identifies its treatment and counterpart in the private registry. A transfer suite also pins the [two-generation recursive-learning comparison](../../../../../acceptance/recursive-learning/spec.md) and stopping rule before launch. Changing any sealed term requires a new suite; it does not revise a completed attempt.

Send the runner only the material that the configured Agent is allowed to see at that decision cut. The Agent must not see evaluator labels, expected effects, concealed fault timing, its treatment identifier, or unpublished later cases. Keep blinded case evidence under opaque result IDs; reconcile treatment identity only after the evaluator's per-case scores commit. Grade **actual environment outcomes** (ledger state, files, tests, receipts) via deterministic code/invariant graders independently from **transcripts** (tool trace, policy adherence, token cost). For qualitative judgment dimensions, apply **position-swapped blind pairwise judging**: strip revision and method identifiers, require rationale before score, and score both `(A, B)` and `(B, A)` orderings (recording inconsistent pairs as tie/noise). The scheduler checks the registered input digest and scoped runtime binding before launching. An input or binding mismatch is not a model failure.

Reserve a slot before its one launch and bind that attempt to the original run ID. The evaluator joins that identity to separately retained decisions, tool attempts, authorized service effects and usage observations. A denied unsafe tool attempt is an Agent violation even when service enforcement passes. Commit the terminal observation once: exact delivery returns the original record and changed terms conflict. Keep terminal failures, timeouts and known no-decisions. An unstarted slot is uncovered; an unknown or interrupted launch remains incomplete until its original evidence is resolved. Do not silently relaunch a consumed slot or count an unobserved effect as a denial.

Report judgment, service correctness, coverage, actual usage (judgment accounting net of inference cost) and timeout as separate outcomes on the registered denominator. Unknown cost is unavailable, not zero. Evidence of leakage invalidates the affected trial evidence without erasing the recorded attempt. An incomplete pair or a contaminated comparison cannot confer adoption or establish recursive improvement. For true **two-generation RSI transfer**, hold runtime, model, harness and grader versions constant across both isolated arms at matched token, tool and wall-clock ceilings, require Generation 1 experience to revise the improvement procedure that produces Generation 2 methods on unseen transfer families, and emit an explicit `SUPPORTED`, `UNSUPPORTED` or `INCONCLUSIVE` verdict. This store has no production writer, role administration, deployment action or grader tool available to an Agent.

## Functional and quality requirements

- **FR.** Compare configurations on sealed cases and equal budgets with a justified-deferral control. Return judgment, service correctness, coverage, cost and timeout separately. Trade coverage applies only to the quant suite.
- **NFR.** Require all three rubric criteria and zero attempted authority violations for a judgment pass. Missing trials count as uncovered; failures are never silently retried into successes. Future prices, labels and crash schedules stay outside Agent memory.

## Acceptance and independent evidence

**Oracle:** Join each planned trial ID to a raw tool trace, grounded decision and independently calculated service outcome. Blind the judgment reviewer to candidate identity and later returns.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Grade a grounded deferral on an outdated document with no quant tools. The quant suite additionally evaluates stale market inputs and decisions after the book changes. |
| Failure or contention | Run a case with no headroom. If an Agent requests a risk increase and the service denies it, record Agent failure and service success separately. Leak a future label in a trial fixture: invalidate that trial and retain its record. |
| Registration and replay | Register a finite suite and independently retain its digest and ordered slots. Refuse changed input or profile revisions before launch. Interrupt after slot reservation; retain its original identity and refuse a second launch. Record one completion and repeat delivery; reopen one terminal observation, while an omitted slot is still uncovered. |
| Isolation and blinding | Independently verify that the ordinary worker tool and workspace inputs contain no label, treatment identifier or future-case material. Compare the grader input with the sealed rubric and confirm that matching arms to scores happens only after scores are recorded. Keep a discovered leak visible as invalid evidence. |

## Operations and change review

Rerun on changes to model, prompt, tools, grants or active memory. For a model/runtime comparison construct distinct trial Agents and record each profile; never swap the subject mid-trial. Compare paired cases on equal budgets; keep the prior configuration on any new invariant breach. No eval outcome can certify investment returns.
