# Forecast cash availability and shortfall

Design draft · Verification **UNRUN** · Responsible component: Treasury

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [accounting-close/reconcile-period](../../../accounting/accounting-close/reconcile-period/spec.md) · [capital-budget/reserve-spend](../../capital-budget/reserve-spend/spec.md).

## Design decision and review

Read reconciled cash, subsequent settled movements, restricted cash, dated bills and scenario assumptions. Show actual cash separately from forecast. Match commitments to invoices to avoid double subtraction.

**Concern for the responsible component:** Forecasts are not accounting entries or transfers. External banking and payroll are not implied.

## Functional and quality requirements

- **FR.** Return unrestricted cash now, dated obligations and projected surplus or shortfall with provenance and freshness.
- **NFR.** Unknown amounts and FX stay unknown. Do not treat unrealized gains, investments or unused budgets as liquid cash.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | With cash 100, restricted 20 and a bill of 30 due within the horizon, independently verify 80 available now and 50 projected after payment. |
| Failure or contention | Attach the same bill to an operating commitment: projected cash falls by 30 once. An unavailable current source gives an explicitly stale forecast. |

## Operations and change review

**Oracle:** Track freshness, unmatched obligations and projected shortfall dates. Alert the finance duty without initiating transfers or trades.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
