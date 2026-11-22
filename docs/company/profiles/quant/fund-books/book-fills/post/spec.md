# Post fills and update outstanding exposure

Design draft · Verification **UNRUN** · Responsible component: Ledger

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume order and fill identity, side, contract multiplier, price, quantity and fee. In one transaction store the fill and fee and adjust an existing linked reservation when the effect has one. A venue-originated effect with no company reservation must not create a fictitious reservation or release. A balance derived for accounting is not the trading position: operational position observations come from the [bound execution runtime](../../../trading/execution-environment/launch/spec.md).

An independently confirmed venue-originated or otherwise unattributed account effect uses its own external provenance and original venue identity; it does not require a fabricated Agent decision or a prior company-created order. Such effects follow [external-effect reconciliation](../../../trading/recover-order/reconcile-external/spec.md), including containment while coverage is unresolved.

In that same transaction persist a pending accounting source keyed by venue/account/fill and mapping revision. The bridge publishes and acknowledges this source independently; an unavailable corporate ledger cannot erase or duplicate it.

The account-scoped investment subledger is the sole reservation-state writer. It creates a reservation atomically with an approved admission, adjusts it atomically on a unique fill and releases the confirmed remainder once on uniquely identified terminal or sealed-absent evidence. Terminal release requires the reported cumulative filled quantity to reconcile to complete identified fills; an unknown remainder remains conservatively reserved. Risk decides admission; reconciliation and cancellation supply evidence and retain the resulting receipts, not separate reservation balances.

Input contracts: an authenticated, independently queryable venue observation bound to the original venue/account/order identity and instrument terms. The [simulated venue](../../../trading/simulated-venue/orders/spec.md) is the first acceptance binding, not a prerequisite or identity format imposed on every external connector.

## Design decision and review

Key postings by venue, account and fill identity rather than order identity; one order can fill more than once.

**Concern for the responsible component:** Pin monetary rounding and currency units before admitting a new instrument. A mismatch must not silently alter an existing posting.

## Functional and quality requirements

- **FR.** Partial fills reduce remaining quantity; a duplicate gives the original posting receipt.
- **NFR.** Use exact contract and currency units. A contradictory event under the same fill ID raises an unresolved case and preserves the prior posting.

## Acceptance and independent evidence

**Oracle:** Compare booked quantity, fees and remaining reservation against independently captured venue fills and declared terms. For the operational position, compare the result with the runtime's scoped account observation; a tally of booked fills is not the oracle.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | For a five-contract buy, a single two-contract fill at 50,000 USD per BTC and 0.001 BTC per contract yields +2 contracts, three open and USD 0.04 in fees at four basis points. |
| Failure or contention | Deliver that same fill from account sync and order history, then reopen the store. Expect one posting and unchanged totals. |
| Publication cut | Kill the company after fill commit and before bridge delivery. After automatic restart compare the venue journal, subledger, pending source and independent general ledger: one fill, one fee journal, no second order and no lost pending source. |

## Operations and change review

Observe per-account book discrepancies. Freeze dependent risk and keep rejected conflicting evidence for reconciliation.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
