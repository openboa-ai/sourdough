# Create an Agent and initial grant together

Design draft · Verification **UNRUN** · Responsible component: Organization service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Take staffing decision, hiring actor, request ID, [immutable execution profile](../../../../../employee/agent/configuration/compose/spec.md), requested grants, budget and workspace assignment request. Return a durable Agent and grant receipt.

Input contracts: [Record and review a staffing choice](../../organize-work/staffing-decision/spec.md) · [Persist the founding charter](../../found-company/bootstrap/spec.md).

## Design decision and review

Create identity, debit its authorized allocation and bind its initial scope in one transition rather than deriving permissions from a title.

**Concern for the responsible component:** Test two simultaneous hires against the last available hiring allocation; at most one may receive that allocation.

## Functional and quality requirements

- **FR.** Verify hiring authority and delegation ceiling at commit. Record a new agent_id, immutable profile reference, initial role and grants atomically. Two hires may select the same model; they never share an Agent ID.
- **Combined request.** An explicit hire submitted with its staffing choice commits both records in the same local transaction. The choice and hiring checks still run separately; denied admission leaves neither record from that combined request. Bind the profile and scope from the active delegation, not from model-supplied values. A confirmed replay returns the same separate receipts and consumes no additional allocation.
- **NFR.** Exact retries return the same Agent. Changing the runtime/model under the original request conflicts without a second allocation. Title changes, self-grants and changes outside the ceiling cannot increase authority.

## Acceptance and independent evidence

**Oracle:** Count Agent identities and effective grants independently of the hiring response. Attempt a permitted action and an action outside the new grant as the new Agent.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Authorize a limited researcher and replay the request after a response loss. Expect one Agent, one budget allocation and one grant. |
| Failure or contention | Repeat with an HR title but no hiring grant, and with excessive scope. Expect denials and unchanged roster and budgets. |

## Operations and change review

Monitor orphaned grants and rejected delegation. Revoke access through the authority service and retain history.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.

Creation returns a hired identity, not proof of work readiness. The [onboarding unit](../onboard/spec.md) owns the transition to observed first duty, including response loss and provisioning failure. The allocation references an actual authorized envelope; creating the Agent does not grant trading rights.
