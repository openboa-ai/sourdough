# Reconcile statements and lock a period

Design draft · Verification **UNRUN** · Responsible component: Company accounting

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [general-ledger/post-journal](../../general-ledger/post-journal/spec.md).

## Design decision and review

Compare opening balances plus movements with independent statement and usage evidence at a cutoff. Record unmatched items and issue a versioned close only under the stated closing policy.

**Concern for the responsible component:** Closing does not authorize a payment, order or edit to historical entries.

The initial close operates on the existing journal's inclusive accounting dates, one cash account, one currency and one fixed chart revision. It compares an independently supplied opening/closing balance pair and a declared operating-cost inventory with the journal in a single transaction; it does not import statements, add entries or convert currencies. Each expected cost names its original source, expense account and responsible owner. A verified zero must have an explicit source receipt; absence does not mean zero. Any operating expense in a declared account but outside the inventory is unresolved.

An immutable close receipt preserves the independent statement revision, the journal entries used, discrepancies, and the input evidence. Replaying the same request returns its original close. A changed close must explicitly supersede the latest revision; it may add sources but cannot remove an expected source or change the chart or expense accounts within the same period. Posting a late actual and issuing a corrected close do not rewrite the previous close or any journal lines. This accounting receipt is an input to the fund statement, not a claim that trading P&L or a prospective return was verified.

## Functional and quality requirements

- **FR.** Return a clean close at the declared cutoff or discrepancies with evidence and owners. Preserve locked history.
- **NFR.** An unexplained cash difference blocks a clean close. Late events require linked adjustments under policy; locked lines cannot be rewritten.
- **FR.** Record the expected billing and usage sources for the period, their cutoff and the completeness of each. A missing expected receipt blocks complete company-cost coverage, with an owner and next check. Independently reconcile actual expenses to billing and usage evidence even when the company made no trades.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Open at 1000, post a paid cost of 30, match a statement ending at 970 and close. Recompute independently. |
| Failure or contention | Supply a statement ending at 960 or repeat the close. Show the 10 discrepancy or return the original receipt; no fabricated balancing entry. |
| Missing cost | On a no-trade period omit an expected compute receipt. Close remains incomplete for company costs; after a late receipt create a linked corrected close, posting the expense once. |

## Operations and change review

**Oracle:** Track unmatched age and reopened periods. Retain statement and journal revisions for corrections.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
