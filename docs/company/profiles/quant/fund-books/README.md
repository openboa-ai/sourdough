# Q3 · Positions, fund performance and investment reporting

Design draft · [Company and quant architecture](../../../../README.md#system-hierarchy) · [Development lifecycle](../../../../engineering/lifecycle.md)

**Responsibility:** Owns fill-derived quantities, exposure, venue costs and investment returns. Supplies economic events to company accounting and reads actual operating costs; it is not a second corporate general ledger.

## Investment subledger

### [Post a fill exactly once](book-fills/intent.md)

Turn venue evidence into position, fees and remaining open exposure without counting a repeated notification.

- [post](book-fills/post/spec.md) — Commit the fill identity, signed quantity, fee and reservation adjustment together; two deliveries of the same fill leave one financial posting.
- [journal-source](book-fills/journal-source/spec.md) — Publish mapped cash effects to company accounting exactly once per economic source; quantities stay in the investment subledger.

## Performance and board evidence

### [Explain net fund performance](fund-economics/intent.md)

Distinguish investment gains from deposits, marked exposure and the cost of running the company.

- [close-period](fund-economics/close-period/spec.md) — Reconcile period flows, fills, fees, funding and operating costs. Keep estimates and missing costs distinct from booked cash.
- [reconcile-sources](fund-economics/reconcile-sources/spec.md) — Reconcile non-fill venue movements and company costs with source coverage before a complete company-net close.
- [total-return](fund-economics/total-return/spec.md) — Keep performance percentages separate from P&L and external capital flows.
- [performance projection](fund-economics/projection/spec.md) — Return versioned totals, units and coverage for the requested reporting perimeter; clients must not recompute returns.

### [Inspect a decision from its financial effect](decision-trace/intent.md)

Let the returning board answer why a position changed, who acted and what will happen next.

- [decision trace](decision-trace/project/spec.md) — Join the position, fill, decision and next work item; return gaps and stale evidence explicitly.
