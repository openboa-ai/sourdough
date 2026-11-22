# Inspect what the company actually did

Design draft · [Positions, fund performance and investment reporting](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Explain what happened](../../../../../capabilities.md#accountability).

## Operating need

On return the board needs to see a real Agent's thesis, execution evidence, unresolved obligations and next responsibility.

## Observable outcome

A single read-only drill-down joins the observed position to venue fills, admission, Agent decision and next shift with its as-of time.

## Scope of discretion

The quant service owns trace meaning, authorization and as-of coverage; the client only presents the returned records.

## Development changes

- [Project the investment decision trace](project/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
