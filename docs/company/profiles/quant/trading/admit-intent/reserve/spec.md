# Reserve exposure for an immutable intent

Design draft · Verification **UNRUN** · Responsible component: Risk service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume actor grant, decision/action ID, side, units, size, price constraints and snapshot. Within the account writer check policy and open exposure, then request the investment subledger's atomic reservation and submission identity.

Input contracts: [Founding charter](../../../../../systems/organization/found-company/bootstrap/spec.md) · [Scoped permission](../../../../../systems/authority/authorize-actor/check/spec.md) · [Decision-time snapshot](../../../investments/market-snapshot/snapshot/spec.md) · an authorized [execution environment binding](../../execution-environment/bind/spec.md), with [paper](../../paper-account/bind/spec.md) as the first binding. Binding validation is an interface prerequisite, not a prior order or reservation. For the first paper trade, the scoped Agent action and a fixed or replayed paper price are sufficient; no live feed or research finding is required. The later research-backed qualification additionally consumes its prior [Agent finding](../../../investments/research/decision-note/spec.md). A program-originated intent instead carries its authorized, pinned deployment and its own input/decision lineage; it does not require a fresh Agent-authored finding on every order.

## Design decision and review

Serialize policy check and reservation per account rather than letting independent workers spend a cached available balance.

Risk decides under the current grant and mandate; the account-scoped investment subledger alone writes reservation creation, adjustment and terminal release. Admission, new reservation and submission identity commit atomically under the same account serialization. Reconciliation may request a transition with identified evidence but must not mutate a second balance.

The company books a fill by its original fill ID and executed quantity, not by the requested order quantity. Before terminal release, the exposure consumed by a buy is the sum of booked fill notionals plus the remaining order quantity at its admitted limit. Partial or duplicate observations cannot complete the order. Existing single-fill records retain their IDs, prices, and observation references.

**Concern for the responsible component:** Check side, unit and existing reservations under the same mandate revision, including dispatch racing a board suspension.

## Functional and quality requirements

- **FR.** Return an existing receipt on identical retry; reject changed terms under the same ID. Recheck the current mandate at dispatch admission.
- **NFR.** Concurrent proposals cannot spend the same headroom. Unknown orders retain their reservations.
- **FR.** Where the mandate defines a drawdown stop, evaluate it from flow-adjusted equity against its high-water mark at the required valuation cut. At or beyond the bound, or without a trustworthy required mark, deny new risk and preserve original-order lookups, posting and separately authorized risk reduction. Test an external withdrawal (not a loss), a price gap beyond the bound and stale marks. Record the inputs and denial; a stop on admissions is not a guarantee of realized exit price.

## Acceptance and independent evidence

**Oracle:** Recompute gross and signed pending exposure from independently enumerated reservations and venue submissions. Count denials by source and inspect the winning reservation.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Under a five-contract cap, submit two individually authorized four-contract buys from flat with no other reservations. Exactly one is admitted; the other records the binding limit. Four contracts are reserved in total. |
| Failure or contention | Replay an admitted intent after a grant change. It yields historical state and cannot authorize a fresh send. An out-of-scope actor produces zero venue submissions. |
| Lifecycle contention | Admit five units, observe a two-unit partial fill, then race terminal cancellation evidence, duplicate fill delivery and restart. The single reservation retains three until definitive cancellation, then releases exactly three once; venue fills, ledger position and available headroom agree. With unknown cancellation retain the remainder. |
| Program path | A pinned, scoped company program proposes an order with its deployment and input lineage, without a per-order Agent finding. Apply the same account risk and reservation checks. A missing or revoked deployment grant denies dispatch. |

## Operations and change review

Monitor over-limit denials and reservation age. Suspend new affected risk on a ledger discrepancy.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.

**First research-backed Agent order.** Freeze the evidence digest into the immutable action. Before admission verify the binding, same company/Agent, prior research receipt, decision-visible cut and permitted instrument. Missing, mismatched, post-dated or superseded-by-substitution evidence returns a denial and zero dispatches; retry cannot attach a different finding to obtain a second order. Check semantics offline against the real rationale and source calculation; do not require a reviewer or prescribe a side at runtime. Program orders remain attributed to a pinned deployment and its own authority.
