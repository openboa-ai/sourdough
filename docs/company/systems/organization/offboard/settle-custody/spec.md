# Reassign obligations and certify retirement

Design draft · Verification **UNRUN** · Responsible component: Organization continuity

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Inventory open work, private desk custody, shared assets, deployment identities, unsettled costs and profile-owned obligations at the cutoff. Each owning service returns an idempotent disposition: accepted successor or a durable, authorized company recovery duty.

Input contracts: [offboard/fence](../fence/spec.md) · [company-assets/transfer](../../../resources/company-assets/transfer/spec.md) · [collaborate/handoff](../../../work/collaborate/handoff/spec.md) · [monitor-company/incident](../../../operations/monitor-company/incident/spec.md).

## Design decision and review

Use a persistent itemized retirement record. A company continuity service may contain and reconcile an effect but cannot acquire the departed Agent’s risk-taking or hiring grant.

**Concern for the responsible component:** Do not give a successor blanket access to private scratch. A still-running program needs its own effective grant and authorized custodian; otherwise suppress new side effects.

## Functional and quality requirements

- **FR.** Mark retired only when no old admission remains possible and every obligation has exactly one accountable owner, next check or terminal receipt. Preserve attribution to the original Agent.
- **NFR.** Restart or redelivery can neither lose ownership nor create two custodians. Unknown effects remain open even if retirement is complete under a verified recovery owner.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Interrupt after fencing and before handoff. Resume, observe acceptance of shared work, rejection of stale mutations and a read-only historical Agent record. |
| Failure or contention | Withhold successor acceptance and disable the standing recovery owner. Retirement remains pending and dependent work contained; it cannot claim success on timeout. |

**Oracle:** Compare pre-cutoff inventory with dispositions, new grants and outstanding duties, including profile-supplied receipts. Run [O1](../../../../../acceptance/episodes.md#o1-offboarding).

## Operations and change review

Expose unassigned count, oldest duty and pending cutoff. Never auto-delete an order or reset the company books to complete retirement.
