# Authorize an order against shared risk

Design draft · [Trade execution, market risk and venues](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Authorize and resolve an effect](../../../../../capabilities.md#effects).

## Operating need

The firm must give two independent decision makers a consistent view of available risk before committing exposure.

## Observable outcome

Every proposal gets a reasoned denial or one durable reservation for immutable terms. The first Agent trading path additionally joins a prior authenticated finding to its actual order decision on the bound synthetic account.

The account-scoped investment subledger is the sole writer of reservation state from creation through final release. Risk decides admission; reconciliation supplies venue evidence; neither maintains a second authoritative reservation balance.

## Scope of discretion

Agent-originated and company-program order requests share current account authority, risk checks and durable original-order reservation. Creating or activating a program is not order admission; a direct Agent order does not require a program. No generic external connector operation may bypass this boundary.

Trade judgment belongs to the actor. Authority and portfolio limits are enforced by the company.

## Development changes

- [Reserve exposure for an immutable intent](reserve/spec.md)
- [Admit paper orders in later unattended duties](continue-execution/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
