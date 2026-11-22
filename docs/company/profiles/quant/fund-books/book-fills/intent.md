# Book each economic fill once

Design draft · [Positions, fund performance and investment reporting](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Steward resources and explain outcomes](../../../../../capabilities.md#stewardship).

## Operating need

The company needs the position actually held, the fee paid and quantity still outstanding.

## Observable outcome

The company books each venue/account/fill identity once and matches the independently calculated position and fees.

Every committed fill must also leave recoverable accounting work, including if the company dies before the bridge runs.

## Scope of discretion

The venue owns execution evidence. The company owns accounting and its board projection.

The investment subledger owns the account-scoped reservation state throughout admission, partial fills and terminal release. Risk decides admission; reconciliation supplies confirmed evidence and does not keep a competing balance.

## Development changes

- [Post fills and update outstanding exposure](post/spec.md)
- [Map fill economics into company accounting](journal-source/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
