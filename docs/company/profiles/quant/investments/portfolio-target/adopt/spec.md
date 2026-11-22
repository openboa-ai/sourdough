# Adopt a versioned target and derive executable delta

Design draft · Verification **UNRUN** · Responsible component: Portfolio service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume proposals, account/instrument, expiry, current position, outstanding signed quantity and expected target revision. Publish total desired contracts and a linked delta proposal.

Input contracts: [Publish immutable as-of observations](../../market-snapshot/snapshot/spec.md) and the account-scoped investment book's current position and any existing reservations. A first target may have no reservations. Its proposed delta subsequently goes through [risk admission](../../../trading/admit-intent/reserve/spec.md); a reservation for that delta is not an input to adopting the target.

## Design decision and review

Publish a total portfolio target and derive one execution delta instead of summing independently proposed orders.

**Concern for the responsible component:** Re-evaluate signed pending quantity on a version change; an unresolved order invalidates any target change that depends on its outcome.

## Functional and quality requirements

- **FR.** Adopt one current target per account/instrument. Compute incremental quantity net of existing position and known pending exposure; recheck risk at admission.
- **NFR.** Competing updates use version checks. Unknown pending exposure blocks a dependent increase until reconciliation.

## Acceptance and independent evidence

**Oracle:** Independently subtract the marked position and signed outstanding commitments from the adopted target. Compare the submitted delta, or the block if exposure is unknown.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | With +2 held, +1 open buy and target +5, the remaining proposed increase is +2 before risk checks. |
| Failure or contention | Concurrently adopt a conflicting revision or use an expired target. Reject stale admission and preserve original decisions and reservations. |
| First target | From a verified flat account with no open reservations, publish a valid target and compute its delta without a previously admitted order. The subsequent proposed order must independently pass risk admission. |

## Operations and change review

Track superseded targets with outstanding orders. Reconcile existing exposure before replanning.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
