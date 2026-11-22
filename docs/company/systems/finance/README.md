# C8 · Finance and treasury

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Owns spending limits, reservations and cash planning. It answers what the company can afford, using accounting evidence without rewriting it. Trading exposure is a different limit.

## Budgets and commitments

### [Allocate and consume operating budget](capital-budget/intent.md)

Cash in the bank does not authorize another Agent, model turn or workspace.

- [allocate](capital-budget/allocate/spec.md) — Issue and revise spending envelopes. Race two allocations of 60 against a limit of 100. Only one can succeed; preserve existing commitments on denial.
- [reserve-spend](capital-budget/reserve-spend/spec.md) — Reserve, release or settle a work cost. Lose the usage reply and retry. Retain the unresolved reservation; match a later receipt to the original obligation.

## Cash availability and forecast

### [Explain liquidity and near-term obligations](treasury/intent.md)

Even a profitable company can lack available cash when an obligation falls due.

- [forecast](treasury/forecast/spec.md) — Forecast cash availability and shortfall. Attach the same bill to an operating commitment: projected cash falls by 30 once. An unavailable current source gives an explicitly stale forecast.
