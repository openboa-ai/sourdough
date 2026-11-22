# Issue and revise spending envelopes

Design draft · Verification **UNRUN** · Responsible component: Company finance

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [authorize-actor/check](../../../authority/authorize-actor/check/spec.md).

## Design decision and review

Version an envelope by unit, period, purpose and custodian. Allocate child limits out of parent capacity without duplicating it.

**Concern for the responsible component:** An allocation is permission to incur cost, not liquid cash, a posted expense or trading headroom.

## Functional and quality requirements

- **FR.** Issue a bounded allocation from remaining parent capacity, or return the binding limit without changing any grant.
- **NFR.** Never convert units or reuse an expired allocation implicitly. Revisions preserve settled spend and open commitments.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Allocate 60 and 40 from a parent limit of 100 and observe no unallocated capacity. |
| Failure or contention | Race two allocations of 60 against a limit of 100. Only one can succeed; preserve existing commitments on denial. |

## Operations and change review

**Oracle:** Compare parent and child totals with grant evidence. Freeze new allocations when the available balance is unknown.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
