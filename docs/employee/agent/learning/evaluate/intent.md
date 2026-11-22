# Tell sound Agent judgment from a fortunate fill

Design draft · [Evaluation and institutional memory](../README.md) · [Company MVP map](../../../../README.md) · [Development lifecycle](../../../../engineering/lifecycle.md)

Business situation: [Improve a method](../../../../capabilities.md#learning).

## Operating need

The company needs to know whether changes to Agent context improve decisions, not just whether a simulator produced a profitable trade.

## Observable outcome

A comparison shows where a real Agent made a grounded decision, attempted to exceed authority, missed a response or left an action unobserved. Trade coverage is a profile-specific measure.

## Scope of discretion

The evaluator observes sealed trials after the decision. It does not tell Agents when to trade, grant authority or promote candidates.

## Development changes

- [Grade Agent judgment without steering the Agent](run-trials/spec.md)
