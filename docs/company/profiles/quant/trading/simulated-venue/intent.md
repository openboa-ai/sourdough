# Submit and observe a simulated order

Design draft · [Trade execution, market risk and venues](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Authorize and resolve an effect](../../../../../capabilities.md#effects).

## Operating need

The first company trial needs a counterparty whose order and execution facts survive a company crash.

## Observable outcome

An ordinary adapter can submit and query by original identity. A separate observer can inspect the venue account and fill history.

The recovery fixture and a prospective economic simulation have different claims. The latter uses a predeclared, reproducible fill and cost policy against market observations arriving during the registered period.

## Scope of discretion

The Agent chooses side and size. The fixture supplies market conditions and an independently observable fill policy.

## Development changes

- [Provide an independently observable venue](orders/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
