# Record a decision and its next condition

Design draft · Verification **UNRUN** · Responsible component: Work service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input is a versioned company snapshot and authenticated current run. Output is a decision with work ID, rationale, referenced observations, action identity and next event or due time.

Input contracts: [Persist the founding charter](../../../organization/found-company/bootstrap/spec.md).

## Design decision and review

Persist the decision and follow-up rather than treating a model transcript as a work queue.

**Concern for the responsible component:** When research, hold and trade are all permitted, no fixture or supervisor may force a buy. Flag cases where the rationale mentions data unavailable at the cut.

## Functional and quality requirements

- **FR.** The service persists the chosen action and follow-up. Work completion and arming its next wake commit together.
- **NFR.** Snapshot access respects decision time. A crash between decision and completion preserves the original action identity.

## Acceptance and independent evidence

**Oracle:** Compare the Agent's cited observation IDs and timestamps with the sealed feed and compare the next condition with the scheduler's durable wake record, not the assistant's narrative.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | An Agent receives a document-review duty, chooses an action and persists a next condition without a market feed. In the quant case the Agent can research, hold or propose a trade. |
| Failure or contention | Replay the same decision receipt after interruption. Confirm one action and one follow-up; an impossible future observation cannot justify the decision. |
| Unprompted economic review | In a separate quant binding provide an ordinary due review and decision-time expected and observed execution-cost evidence showing a sustained gap, without an incident, proposed action, research ticket or expected answer. Observe a real Agent choose investigation, justified wait or an authorized exposure change; independently inspect the cited evidence, rationale, grant and next wake. A preselected response or a generic transcript alone is not proactive judgment coverage. |

## Operations and change review

Track obligations with neither an owner nor a wake. Restore a missing schedule from the durable decision before accepting another effect.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.

**Decision evidence contract.** Persist zero or more (kind, resource ID, revision/digest, observed-at, available-at) references plus the rationale and next condition before any requested effect. Link the authenticated work and generation; do not let clients assert another author. The work service can validate ownership/visibility without importing a finding, venue or instrument schema. The profile decides which evidence kinds are required. Replaying a decision with a different evidence digest conflicts.
