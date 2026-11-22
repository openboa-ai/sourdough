# Produce a cost-inclusive economic statement

Design draft · Verification **UNRUN** · Responsible component: Quant performance service (company accounting supplies posted actuals)

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Freeze account and period, start/end marks, signed fills, fees, booked funding, external flows and observed operating costs. Return totals, sources, missing items and a report revision.

Before a complete company-net verdict consume the source-coverage receipt from [non-fill reconciliation](../reconcile-sources/spec.md). The investment book owns venue account movements; accounting owns posted operating actuals; this service owns the aligned valuation and return calculation.

Input contracts: [Fill postings](../../book-fills/post/spec.md) · [Journal mapping](../../book-fills/journal-source/spec.md) · [As-of market inputs](../../../investments/market-snapshot/snapshot/spec.md) · [Reconciled company accounts](../../../../../systems/accounting/accounting-close/reconcile-period/spec.md).

## Design decision and review

Keep realized cash, marked exposure, external flows and operating costs separate rather than treating a change in balance as profit.

The managed cash-spot valuation cut preserves the observed bid and ask separately. The period's pinned mark policy chooses the measure. The independent venue cash already reflects execution charges; do not deduct those charges again when joining the accounting close. An individual usable cut is not proof of coverage for the period.

For an explicitly selected `spot-best-bid-v1` policy, value a cash-spot cut as original venue cash plus long lots times the observed bid per lot, in the venue's quote atoms. Pin the capture ID, binding revision, policy and exact independent observation in an immutable fund evidence receipt. Exact retry returns that receipt even if market prices advance. Do not relabel restarts, expired observations, or changed binding terms as the original cut. The receipt is historical measurement input, never a second source of current account state or proof of the final operating result.

To use a cut in the first operating result, join it to the immutable window receipt. Require the same bound account, valuation policy and source revision (`binance-public-spot-quote-v1` for managed spot). The quote's original event and availability times, its supporting trade event and the account cut must all fall inside the window. Preserve the two receipt revisions on that input. One eligible cut does not attest to opening or closing coverage, feed continuity, or operating expenses.

The first managed cash-spot statement uses the provisioned cash as opening NAV only when the first scheduled cut shows that full cash, zero lots and an empty fill prefix. Require all scheduled mark slots, an elapsed window and the final slot's pinned bid still within the managed quote freshness limit at the closing instant. Reopen the venue after the cutoff and require the exact final fill prefix and corroborated balance, so an unobserved later effect is not projected into the close. Reconcile original fill movements between the boundaries. This produces a marked trading result, not a proof of continuous feed availability or an investment verdict. The company-net series additionally requires a compatible, complete accounting close for that window and exact same-currency units. Subtract only attributable operating costs; the marked venue cash already includes venue execution fees. Missing evidence leaves the affected series unavailable while preserving the captured observations and reasons. The read-only statement does not admit orders, allege realized live returns, or decide a mandate or comparison verdict.

After the window closes, a writer may retain the statement under an exact close request ID, even if the result is unavailable. Seal the company, window, request terms, assessment time and full report together. An exact retry reopens that receipt without requiring the venue to remain online; it never recomputes its result. A changed computation needs a new request that supersedes the latest retained report for the same window. Earlier incomplete reports remain inspectable by ID after late cost evidence arrives. Report retention does not write to the venue or the general ledger.

**Concern for the responsible component:** Choose valuation cut, cost currency and conversion source before comparing periods; missing costs prohibit a fully net claim.

## Functional and quality requirements

- **FR.** Separate net flows from returns. Distinguish posted cash amounts from marks or estimates. Link corrected reports to the superseded computation.
- **NFR.** Missing costs remain unknown. Count a fee included in venue net P&L only once. Do not post unverified funding predictions as cash.

## Acceptance and independent evidence

**Oracle:** Build a separate period worksheet from opening and closing balances, marked positions, flows, funding, fees and company cost receipts. Reconcile each component before net return.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Fixture: start equity 1000, deposit 100, gross gain 10, fee 2, compute cost 3, zero funding. Report trading equity 1108 and net company result +5 with compute paid separately. |
| Failure or contention | Remove a required cost or mark and rerun. Output incomplete rather than a definitive net result; supply the missing source and reproduce the correction. |

## Operations and change review

Track unresolved cost coverage and reconciliation differences. Preserve earlier report revisions and expose their as-of time.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.

Report amounts as amounts, not percentages. The [return calculator](../total-return/spec.md) consumes the reconciled scope, external flow timestamps and coherent valuation cuts; the [board projection](../projection/spec.md) displays its revision. Label costs paid outside the trading account but inside the company perimeter rather than deducting them twice.
