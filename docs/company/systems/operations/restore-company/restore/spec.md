# Recover a company from a verified checkpoint

Design draft · Verification **UNRUN** · Responsible component: Company storage and recovery

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Capture policy, work, asset manifests, relevant journals and an event watermark. Restore into isolation, fence old senders and verify later events. The quant profile supplies venue reconciliation; it is not needed by a company with no trading duties.

Input contracts: [Supervisor](../../../work/resume-work/supervise/spec.md), company asset inventory and current custody records. A custody [transfer](../../../resources/company-assets/transfer/spec.md) is required only if a duty or asset actually changes custodian; restoring the same owner does not require a successor or an accepted handoff. Each installed profile supplies its own obligation reconcilers.

## Design decision and review

Restore into an isolated candidate instead of overwriting the last good copy in place.

**Concern for the responsible component:** Pin checkpoint retention, recovery objectives and fencing method before the drill. If lost local authority or obligations cannot be reconstructed, remain isolated.

## Functional and quality requirements

- **FR.** Restore the same company, fence old senders, replay retained events and reconcile outstanding obligations. Release no reserve solely because the checkpoint predates an order.
- **FR.** Reopen assets under restored current grants without inventing a transfer. If a necessary successor cannot be verified, contain the affected obligation and retain its owner or explicit service recovery duty rather than transferring private access or blocking unrelated company work.
- **NFR.** Record checkpoint age, last recoverable event and restore-to-ready elapsed time against budgets declared before the drill. Zero double dispatch or double posting; missing lineage blocks new dependent risk.

## Acceptance and independent evidence

**Oracle:** Keep the pre-loss inventory, post-checkpoint event IDs, venue history and expected writer generations outside the restore process. Compare the resulting book and duty queue to those expectations.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Restore a checkpoint predating a shared-asset handoff. Compare company, grants, custody and next duty to independent receipts. In the quant case additionally restore across a fill and verify position and fees. |
| Failure or contention | Withhold a journal segment, corrupt the checkpoint, or leave the old sender active. Do not promote the candidate; retain the surviving copy and a scoped incident. A venue balance alone cannot reconstruct a lost grant. |

## Operations and change review

Monitor age of the last verified checkpoint and last successful restore drill. Fall back to an intact copy without erasing the failed candidate or unresolved obligations.
