# Recover a due or interrupted shift

Design draft · [Work and Agent communication](../README.md) · [Company MVP map](../../../../README.md) · [Development lifecycle](../../../../engineering/lifecycle.md)

Business situation: [Detect interruption and continue](../../../../capabilities.md#continuity).

## Operating need

When a process dies, the company must know which work was claimed and who can finish it.

## Observable outcome

A product supervisor reopens the original company; an overdue claim is recovered and a later event initiates a distinct shift.

## Scope of discretion

The scheduler chooses eligible work. The Agent decides its substance.

## Development changes

- [Relaunch the service without a board or test driver](supervise/spec.md)
- [Fence interrupted claims and redelivered wakes](recover-shift/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
