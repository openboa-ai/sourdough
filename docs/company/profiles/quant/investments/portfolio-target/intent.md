# Convert proposals into a shared portfolio target

Design draft · [Quant research and portfolio management](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Choose and perform work](../../../../../capabilities.md#judgment).

## Operating need

Several Agents may propose incompatible trades against one pool of capital. The company must select a single accountable target.

## Observable outcome

A current target states total desired exposure, validity, evidence and responsible actor; any incremental intent references the same book and capital limits.

## Scope of discretion

Agents choose the exposure and may decline a proposal. Shared headroom and current authority constrain execution.

## Development changes

- [Adopt a versioned target and derive executable delta](adopt/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
