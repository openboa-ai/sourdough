# Change direction and suspend new risk

Design draft · [Trade execution, market risk and venues](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../../../../capabilities.md#mandate).

## Operating need

The board must be able to change the company's mandate and see when that change took effect, including remaining obligations.

## Observable outcome

The service returns the command disposition, effective revision, actor, effective time, remaining exposure and continuing reconciliation. A client may display the receipt; it cannot apply it.

## Scope of discretion

The board controls the envelope. Agents act without board presence during ordinary operation.

## Development changes

- [Apply and verify a versioned control](apply-control/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
