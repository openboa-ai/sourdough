# Settle a turn without losing its obligations

Design draft · Verification **UNRUN** · Responsible component: Agent Harness run supervisor

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume provider terminal events or independently observed timeout/exit. Track run status, work disposition, usage and pending effects separately. The work service closes a duty or installs the next owner/condition; finance settles usage by original cost event.

Input contracts: [execute/tool-turn](../tool-turn/spec.md) · [resume-work/recover-shift](../../../../../company/systems/work/resume-work/recover-shift/spec.md) · [capital-budget/reserve-spend](../../../../../company/systems/finance/capital-budget/reserve-spend/spec.md).

## Design decision and review

A finished model turn may leave blocked work, uncertain effects or delayed usage. Preserve each as a separate durable obligation rather than treating provider success as company success.

**Concern for the responsible component:** A cancel request acknowledges intent to stop, not provider termination or reversal of an already admitted action.

## Functional and quality requirements

- **FR.** Normalize completed, interrupted, failed, blocked-on-consent, budget-exhausted and lost-transport outcomes. Before replacement, fence the old generation and recover receipts and outstanding obligations. Late usage is attached to the original run, never billed anew as a retry.
- **NFR.** Bound retries/backoff and elapsed/tool/model cost by the profile. Unknown usage stays unknown, not zero. A terminal event cannot by itself authorize another billable run or clear a reserved external effect.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Lose the connection after a tool receipt, then receive both a terminal notification and delayed usage. Observe one work disposition, one actual charge and the original effect identity. |
| Failure or contention | Deliver a stale completion after handover or receive no terminal event. Reject obsolete writes, show the pending facts, and let the service reconcile independently of the Agent. |

**Oracle:** Compare provider observations, company tool/duty receipts and cost ledger by run/generation; record missing signals explicitly.

## Operations and change review

Track exit-to-fence time, unresolved runs, late usage and retry budget. Revert an adapter revision only after fencing its writers; preserve unknown effects. See the [harness contract](../../protocol.md).
