# Read the market as it was known then

Design draft · [Quant research and portfolio management](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Choose and perform work](../../../../../capabilities.md#judgment).

## Operating need

Agents need the prices, positions and working orders actually available at a decision time. Backtests must be able to reconstruct that view.

## Observable outcome

A snapshot returns source, instrument, units, observation and arrival times, coverage and account evidence at a named cut.

Prospective evaluation also needs an immutable, continuous record of what arrived after its registration, including gaps and corrections. Fault-test prices do not establish market performance.

Research can acquire authorized market and non-market inputs directly through the company connector boundary without depending on trading execution. Source identity, observation and availability time remain attached; read-only access confers no account write permission.

## Scope of discretion

The Agent decides how much weight to give an incomplete observation. The service enforces policy on stale or missing risk inputs.

## Development changes

- [Publish immutable as-of observations](snapshot/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
