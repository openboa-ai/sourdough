# Reconcile cancellation and late fills

Design draft · Verification **UNRUN** · Responsible component: Execution service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Accept actor, immutable original order identity, request ID and expected revision. Query authoritative terminal status and fill history for that same identity.

Input contracts: [Resolve accepted, sealed-absent and unknown outcomes](../../recover-order/reconcile/spec.md).

## Design decision and review

Address one original order instead of replacing or flattening the entire position as a shortcut.

The local SDK bridge records a terminal cancellation event with the original order ID and cumulative filled quantity. The book releases the unfilled limit exposure only when its independently identified fills sum to that quantity. The terminal event and the release share one durable record; an incomplete observation leaves the reservation and lookup active. This applies to an SDK-initiated IOC cancellation and does not itself authorize a new cancel command.

A reconciled zero-fill cancellation is a work result, not a fill. Record the original order and event, the position already booked for that account and instrument, and the time the result became available. The same authorized Agent may review that receipt on its next scheduled duty. Recovery settles the original duty from the existing receipt without submitting again.

The SDK adapter uses the same reconciliation path for explicit rejection, denial, and expiry, but preserves the terminal reason. It matches the native event to the original cached order and cumulative fill quantity. A refusal is not published as conclusive to the company until independent SDK account balances corroborate the terminal history; balances must be observable even when the quote feed is stale. A timeout, incomplete event, contradicting fill, or missing balance does not release the original reservation or permit another send.

In the managed current-market virtual sandbox only, an SDK-accepted IOC that remains open after its queued events are applied is finished by canceling the same SDK order. This completes the original IOC instruction; it does not expose an Agent cancel tool or authorize cancellation on any external venue. Skip requests for orders already terminal or pending cancellation. An SDK event for that ID and a matching account checkpoint remain necessary to settle; submitting the cancel does not prove no fill. This fallback does not establish the fill timing or liquidity of a real IOC.

**Concern for the responsible component:** A cancellation response is not final fill history; obtain terminal evidence and account for late fills before releasing headroom.

## Functional and quality requirements

- **FR.** Persist the cancel intent, submit once under its venue contract, and reconcile fees, fills and remaining reservation. Pass confirmed fill and terminal evidence to the account-scoped investment subledger, the sole reservation writer; the cancel service does not release a second balance.
- **FR.** Before releasing the terminal unfilled remainder, reconcile the venue's cumulative executed quantity to complete identified fills. If history is incomplete, keep the affected exposure reserved and schedule the original order's next lookup even when cancellation appears terminal.
- **NFR.** Delayed cancellation must not affect a replacement order. Unknown cancellation leaves the original risk reserved.

## Acceptance and independent evidence

**Oracle:** Read order-specific venue history and account fills independently. Compare final filled and cancelled quantities with the company book after delayed and duplicate events.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Cancel after two of five are filled. Observe the linked terminal exchange record and release only the confirmed unfilled three. |
| Failure or contention | Deliver a fill during cancellation and duplicate the earlier cancel response. Apply the fill once and retain uncertainty until the terminal book reconciles. |
| Terminal refusal | Reject an admitted zero-fill order. Without a corroborated account receipt, the reservation remains held. With the valid receipt, confirm the original terminal reason, zero new position, one release and restart readiness; a timeout on another order does not take this path. |
| Current-market IOC with no match | Use the pinned live-data SDK sandbox and an admitted buy below the market. Observe one original order, an SDK cancellation with zero cumulative quantity, unchanged SDK cash and position, and one release in company books. Run a marketable original separately and confirm the fill is not replaced or canceled after it is terminal. |

## Operations and change review

Measure pending cancellation age. Show residual exposure and continue read-side reconciliation.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
