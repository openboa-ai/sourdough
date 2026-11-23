# Record and review a staffing choice

Design draft · Verification **UNRUN** · Responsible component: Work service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Accept backlog, current assignments, capacity and cost observations from the acting Agent. Record the selected option and a review condition.

Input contracts: [Record a decision and its next condition](../../../work/discover-work/decide/spec.md).

## Design decision and review

Record alternatives and their costs when a need arises instead of provisioning a fixed organization chart.

Present assigned and unassigned capacity of existing staff separately from remaining hiring slots and delegated turn budget. Zero unassigned staff capacity is not zero hiring allowance. Preserve the identity of the decision-time snapshot; the hire command still enforces the current remaining allowance.

**Concern for the responsible component:** Grade a reasoned decision not to hire as potentially correct; verify hire capability separately if the free trial never uses it.

## Functional and quality requirements

- **FR.** Allow several responses to the same backlog. A decision to hire can be submitted to the separate hiring boundary.
- **Native submission.** One tool call can explicitly submit both the staffing choice and a bounded hire request, or submit a no-hire choice with no request. Preserve separate judgment and hire receipts. A recorded recommendation alone never creates an Agent or consumes an allocation.
- **NFR.** A recommendation cannot create Agents, grants or spending by itself.

## Acceptance and independent evidence

**Oracle:** Compare the staffing receipt with the backlog, grants and available budget at decision time. Check the Agent registry before and after to distinguish a recommendation from a hire.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Give a real Agent repeated reconciliation research and an available colleague. Grade the evidence and feasibility of its choice and follow-up. |
| Failure or contention | Repeat with an exhausted hiring allowance. The company may reassign or wait; a persuasive hiring recommendation creates zero Agents. |

## Operations and change review

Track unmet work and repeated staffing decisions with no observed outcome. Return the item to the responsible Agent.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
