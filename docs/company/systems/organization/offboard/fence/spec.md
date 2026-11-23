# Archive the Agent and revoke access

Design draft · Verification **UNRUN** · Responsible component: Organization and authority

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Check company, decision maker, termination grant, reason, target, expected employment revision and request ID. Commit active → archived in one transition.

Input contracts: [revoke-access/retire](../../../authority/revoke-access/retire/spec.md) · [authorize-actor/check](../../../authority/authorize-actor/check/spec.md) · [resume-work/recover-shift](../../../work/resume-work/recover-shift/spec.md).

## Design decision and review

Enforce the employment fence at each new claim, turn, grant and tool admission; do not wait for every distributed revocation acknowledgment to block new effects.

The local cutoff consumes a distinct existing termination grant. In one transaction, record the permanent archive, revoke the grant, fence runs, and close the Agent's outstanding duties as archived. There is no intermediate retirement state, successor requirement, or return to active status. The founding owner is outside this local transition. Existing action receipts keep their original Agent and run IDs.

**Concern for the responsible component:** Do not erase historical receipts or hide effects already admitted. Human employment, personal data deletion and production account administration are outside scope.

## Functional and quality requirements

- **FR.** Suppress new work and billable turns, invalidate active generations and derived grants, and request cancellation of remaining sessions. Preserve the service-owned ability to reconcile accepted effects.
- **NFR.** Order races by the admission and cutoff sequences, not timestamps in provider callbacks. Duplicate retire requests have one effect. Unverified external fencing blocks new dependent actions.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Retire an Agent while one action is already admitted. The prior action may resolve; a later mutation, child grant and wake are all denied. |
| Failure or contention | Replay a stale tool call or attempt to dismiss the last accountable officer without coverage. Reject without silently appointing a more powerful Agent. |

**Oracle:** Compare roster cutoff, effective grants, work claims, provider starts and effect logs; distinguish pre-cutoff acceptance from late delivery.

## Operations and change review

Track revocation lag and unresolved external bindings separately. No restart can reactivate the archived Agent.
