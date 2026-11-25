# Map fill economics into company accounting

Design draft · Verification **UNRUN** · Responsible component: Investment accounting bridge

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [Fill subledger](../post/spec.md) · [General journal](../../../../../systems/accounting/general-ledger/post-journal/spec.md).

## Design decision and review

Deliver the pending accounting source committed atomically with the fill, using venue/account/fill ID, currency and effective mapping revision. Map cash fees and realized amounts to balanced corporate journal entries; keep contract quantities in the investment subledger. A corporate journal is not trading authority.

The paper fill writer records the original destination and quote currency, fee-source mapping revision, and source registration time in the same transaction as the fill, fee and reservation change. The source references the booked fee; it does not copy the amount, derive the position or authorize a second effect. Source registration time is not an execution timestamp or an accounting effective cut. An older fill without this record remains without source coverage until independently reconciled; replay must not backdate or silently reconstruct it from a newer binding. Journal delivery must separately verify currency scale, effective cut and chart mapping before posting.

The fee bridge consumes an independently verified fee receipt for the original destination, account and fill, including the effective date, currency, unit scale and evidence revision. It matches the original booked fee and an admitted chart mapping before sealing the delivery terms and using the journal port. A mismatched scale, amount, source or chart is not converted or written. A positive fee debits expense and credits the scoped cash account with a stable source identity; no trade notional is posted. A witnessed zero fee records explicit delivery without a zero-value journal entry. A lost delivery acknowledgement repeats the original sealed terms and journal source identity, never a new order or another posting.

For managed paper, the execution adapter supplies the original fill event time and the quote currency and scale of its pinned instrument. The bridge dates the fee by the UTC day of that event, even if posting or recovery occurs later. Each partial fill has its own source. A legacy fill without an independently witnessed event time remains uncovered; do not substitute the company registration time, market quote time, or a replay result for current-market paper evidence.

**Concern for the responsible component:** While accounting is unavailable retain a durable unposted event and reconcile it by source ID on recovery. Do not erase the fill, issue an order or post the same fee twice.

## Functional and quality requirements

- **FR.** Deliver the replayable source event that was atomically persisted with the fill, using venue/account/fill ID, currency and effective mapping revision. Do not create the first durable source only after the fill commit.
- **NFR.** While accounting is unavailable retain a durable unposted event and reconcile it by source ID on recovery. Do not erase the fill, issue an order or post the same fee twice.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | For a two-contract derivative fill with a USD 0.04 fee, add two contracts in the position book; post the fee once as debit fee expense and credit simulated account cash, USD 0.04. The contract notional is not a cash purchase. |
| Failure or contention | Crash after journal commit before acknowledging the source. Replay it and independently verify one corporate fee posting, one quantity change and no second order. |
| Before bridge delivery | Crash after the fill and pending source commit but before delivery. Restart and independently observe one fill, one source and one fee journal, with no new venue submission. |
| Managed paper | Observe a fill before a UTC day boundary and reconcile it after the boundary. Verify the original event day and quote units from the independent execution source. An older observation without event time cannot establish complete fee coverage. |

## Operations and change review

**Oracle:** Compare the fill-to-journal mapping, unique source IDs and cash evidence at close. Track age of unposted sources; any fee already included in net venue P&L must not be deducted twice.
