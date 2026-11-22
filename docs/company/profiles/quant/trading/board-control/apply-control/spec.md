# Apply and verify a versioned control

Design draft · Verification **UNRUN** · Responsible component: Quant risk and governance service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Take an authenticated board command, expected charter revision, reason and request ID. Commit scope change or suspension at the risk admission boundary. Return the actual effective receipt.

Input contracts: [Charter amendment](../../../../../systems/organization/found-company/amend/spec.md) · [Scoped authorization](../../../../../systems/authority/authorize-actor/check/spec.md) · [Risk admission](../../admit-intent/reserve/spec.md).

## Design decision and review

Apply a versioned service command instead of treating the UI toggle as evidence that the risk gate changed.

**Concern for the responsible component:** Distinguish last observed, pending and applied; verify the order of an admission racing suspension using server receipts, not client timestamps.

## Functional and quality requirements

- **FR.** Return requested, applied, denied or conflicting-revision receipts. Resume requires current authority and fresh risk state; disconnecting the caller never rolls back an applied suspension.
- **NFR.** Once suspension takes effect there are zero newly admitted risk increases. In-flight orders remain visible and reconciliation continues.

## Acceptance and independent evidence

**Oracle:** Compare the effective revision and admission order with the authoritative command receipt and independent venue history; no client process is needed.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Suspend with a working order. Read the applied revision, observe another attempted increase denied, then inspect a late fill on the original order. |
| Failure or contention | Replay an old resume request after a newer suspension. Reject stale revision without changing policy or hiding exposure. |

## Operations and change review

Track requested controls lacking receipts. Keep the last known effective control visible and retry reads before another change.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
