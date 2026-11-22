# Detect, contain and close an operating incident

Design draft · Verification **UNRUN** · Responsible component: Operations service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume heartbeats, feed ages, unresolved reservation ages and budget states. Record incident identity, scope, severity, owner, next check and evidence of recovery.

Input contracts: founded company and authorized source health/effect observations. A [work decision](../../../work/discover-work/decide/spec.md) may link an incident to a duty when one exists; monitoring is active before an Agent's first decision and when there is no current duty. The projection consumes incidents, not the reverse.

## Design decision and review

Key the incident to an affected obligation and source rather than opening another ticket for each repeated alarm.

**Concern for the responsible component:** Detect loss of telemetry independently of a healthy observation; a quiet dashboard is not a recovery signal.

## Functional and quality requirements

- **FR.** Deduplicate repeated observations. Close only when the source shows recovery and outstanding obligations have a recorded disposition.
- **NFR.** Stale monitoring remains visibly stale. Independent research can continue during a bounded trading incident.

## Acceptance and independent evidence

**Oracle:** A separate observer records the fault interval and telemetry gap. Compare incident identity, assigned duty, containment scope and closure with the restored source.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Interrupt a document connector during a review. Observe one incident, affected work and automatic recheck. With the quant profile also exercise loss of venue history and prove dependent risk stays reserved. |
| Failure or contention | With the client disconnected, drop the heartbeat source. Persist the last observation and unknown state; repeated alerts cannot mark the incident resolved. A later read returns the persisted status. |
| No prior work | Drop the required heartbeat before the founder's first turn. Create an incident and recovery obligation without requiring a pre-existing decision or client; close only after the source is verified healthy. |

## Operations and change review

Record time to detection, containment and verified recovery. Repeated breaches feed a development intent with sanitized evidence.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
