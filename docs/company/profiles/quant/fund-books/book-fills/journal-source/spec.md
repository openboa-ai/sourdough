# Map fill economics into company accounting

Design draft · Verification **UNRUN** · Responsible component: Investment accounting bridge

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [Fill subledger](../post/spec.md) · [General journal](../../../../../systems/accounting/general-ledger/post-journal/spec.md).

## Design decision and review

Deliver the pending accounting source committed atomically with the fill, using venue/account/fill ID, currency and effective mapping revision. Map cash fees and realized amounts to balanced corporate journal entries; keep contract quantities in the investment subledger. A corporate journal is not trading authority.

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

## Operations and change review

**Oracle:** Compare the fill-to-journal mapping, unique source IDs and cash evidence at close. Track age of unposted sources; any fee already included in net venue P&L must not be deducted twice.
