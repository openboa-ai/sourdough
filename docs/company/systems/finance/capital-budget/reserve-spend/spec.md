# Reserve, release or settle a work cost

Design draft · Verification **UNRUN** · Responsible component: Company finance

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [capital-budget/allocate](../allocate/spec.md) · [general-ledger/post-journal](../../../accounting/general-ledger/post-journal/spec.md).

## Design decision and review

Reserve an estimate under a unique cost ID. Settle against actual usage and a linked accounting source receipt; release only confirmed unused capacity. Missing usage leaves a follow-up duty.

**Concern for the responsible component:** An allocation is permission to incur cost, not liquid cash, a posted expense or trading headroom.

## Functional and quality requirements

- **FR.** Transition an estimate to released, settled or unresolved and link actual usage to its unique accounting source.
- **NFR.** A reservation is not an expense. Repeated usage cannot settle twice. Record an actual overrun as an exception rather than discarding the charge.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | With 10 available reserve 6 and settle an actual cost of 4; verify 6 available and one expense. |
| Failure or contention | Lose the usage reply and retry. Retain the unresolved reservation; match a later receipt to the original obligation. |

## Operations and change review

**Oracle:** Compare commitments, available budget and posted source IDs. At exhaustion block new costs while handling existing obligations.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
