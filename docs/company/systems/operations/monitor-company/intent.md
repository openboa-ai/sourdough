# Surface an unattended incident and its owner

Design draft · [Operations, oversight and recovery](../README.md) · [Company MVP map](../../../../README.md) · [Development lifecycle](../../../../engineering/lifecycle.md)

Business situation: [Detect interruption and continue](../../../../capabilities.md#continuity).

## Operating need

An offline board must be able to return to truthful operational state even when a feed, Agent or adapter is unavailable.

## Observable outcome

Each unresolved incident has last observation time, affected scope, owner, next check and closure evidence in its incident record, whether or not a client is connected.

## Scope of discretion

Operational checks can run independently of Agent inference and its budget.

## Development changes

- [Detect, contain and close an operating incident](incident/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
