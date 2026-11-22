# Transfer asset custody and unfinished obligations

Design draft · Verification **UNRUN** · Responsible component: Asset service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Take expected custody revision, successor and asset set. Record one custody transition and references to related strategy, outstanding work and order obligations. Work owns accepted duty transfer; the domain reconciler retains unresolved orders.

Input contracts: [Assign an isolated company workspace](../assign/spec.md) · [Transfer responsibility with evidence](../../../work/collaborate/handoff/spec.md).

## Design decision and review

Change the custodian through the asset service and coordinate with the authority service for effective grants instead of copying a workspace and leaving both identities active. A custody receipt cannot itself change a grant or accept a duty; retain the related receipts and outstanding duties if a cross-owner transition is incomplete.

**Concern for the responsible component:** Check grants retained outside the workspace as well. If the old operational writer cannot be fenced, stop activation at the successor.

## Functional and quality requirements

- **FR.** Validate successor scope first. Authority fences predecessor mutation access before successor mutation is enabled; assets records custody while Work explicitly accepts any duty handoff. Keep source history and the active strategy identifiable.
- **NFR.** During partial transfer keep the old responsibility or explicitly hold the affected action. Never allow both custodians to deploy a change.

## Acceptance and independent evidence

**Oracle:** Inspect old and new principals' effective access to the same asset revision and outstanding work. Test that the former custodian can no longer mutate it.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Replace the custodian of a shared repo. The successor recovers the same revision and unfinished review. In the quant profile also verify custody of the strategy and its outstanding orders. |
| Failure or contention | Deliver a late write with the old grant and replay the transfer. Expect zero unauthorized writes and one custody transition. |

## Operations and change review

Monitor assets with no effective custodian. Quarantine unowned mutation and assign from the recorded transfer.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
