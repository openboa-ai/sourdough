# Cancel only the intended working order

Design draft · [Trade execution, market risk and venues](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Authorize and resolve an effect](../../../../../capabilities.md#effects).

## Operating need

When a thesis expires or a limit changes the company needs to reduce outstanding risk while respecting fills in flight.

## Observable outcome

The venue and ledger agree on the terminal status and unfilled remainder of the exact original order.

## Scope of discretion

An authorized Agent chooses whether to cancel. A cancel request alone cannot release risk.

## Development changes

- [Reconcile cancellation and late fills](cancel/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
