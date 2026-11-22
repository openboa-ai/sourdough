# Calculate flow-adjusted fund and company returns

Design draft · Verification **UNRUN** · Responsible component: Quant performance service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Pin reporting currency, interval, valuation cut, consolidation perimeter and source revisions. Produce two labeled series: trading NAV net of venue fees and booked funding, and company net NAV after attributable operating expenses/liabilities. Eliminate transfers within the perimeter; classify capital contributed or withdrawn by owners as external. A cost paid from another in-scope account is not an investor withdrawal.

Input contracts: [fund-economics/close-period](../close-period/spec.md) · [accounting-close/reconcile-period](../../../../../systems/accounting/accounting-close/reconcile-period/spec.md) · [market-snapshot/snapshot](../../../investments/market-snapshot/snapshot/spec.md).

## Design decision and review

Use time-weighted return (TWR) for the proposed synthetic MVP, not a balance delta or profit divided by a convenient balance. Value immediately before and after every external flow in deterministic event order. For each flow-free interval r = ending pre-flow NAV / starting post-flow NAV − 1; geometrically link (1+r) across intervals and adjacent reporting periods, subtracting one at the end. No averaging of percentages.

**Concern for the responsible component:** A fee already included in a mark or receipt must not be subtracted again. Apply the same policy across periods; source correction invalidates dependent cumulative revisions. This is a product measurement convention, not a compliance claim.

## Functional and quality requirements

- **FR.** Return gross flows in/out, signed net flow, signed P&L, period and since-inception TWR by series, cutoff, status, method and source references. Attribute expense once when recognized; payment of an already recognized bill does not create another expense.
- **NFR.** Use unrounded values for linking; round only for display. A missing flow time, boundary mark, required operating cost, FX basis or nonpositive starting denominator suppresses the affected percentage. Do not bridge an uncovered interval; distinguish exact zero return from unknown.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Opening 1000 → 1100 immediately before a 100 inflow → 1200 after it → 1140 at close gives P&L +40 and TWR +4.50%, not +14% or +4%. The two interval returns are +10% and −5%. For an opening contribution of 100 to starting 1000, trading gain 10 less fee 2 and company cost 3 give +8/1100 trading and +5/1100 company. |
| Failure or contention | Move an identified deposit from the beginning to the middle without a usable pre-flow mark: preserve the amounts but withhold the percentage. Replay a fee or internal transfer: net consolidated results do not change. A missing company cost need not invalidate a complete trading-only series. |

**Oracle:** An independent worksheet uses timestamped flows, valuations, fees and cost receipts, not the report calculator. Compare both period and linked results under corrections. Run [P1](../../../../../../acceptance/episodes.md#p1-total-return).

## Operations and change review

Expose stale marks and missing sources by series. Recalculate under a new report revision; retain the superseded statement and never represent simulated returns as live.
