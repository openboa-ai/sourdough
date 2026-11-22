# Qualify the external BTC futures boundary

Design draft · [Trade execution, market risk and venues](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Authorize and resolve an effect](../../../../../capabilities.md#effects).

## Operating need

The firm needs to know which external venue behaviors have actually been verified before relying on them.

## Observable outcome

A separately authorized testnet episode compares product history with independently read venue state and records unsupported or unobserved cases.

This is a functional integration qualification. Source freshness and price/market-depth equivalence to the production market are separate claims requiring independently timestamped observations of the selected product; an updating test environment is not by itself evidence that it matches the live market. It is not the current-market paper return trial.

## Scope of discretion

Binance is the target. Exact product, account mode, grants, risk limits and eligibility are owner decisions at this boundary.

## Development changes

- [Verify the external read-only observation profile](observe/spec.md)
- [Qualify the selected testnet adapter](testnet/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
