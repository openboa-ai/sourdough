# Evaluate a company strategy on historical observations

Design draft · [Quant research and portfolio management](../README.md) · [Company MVP map](../../../../../README.md)

Business situation: [Choose and perform work](../../../../../capabilities.md#judgment).

## Operating need

Before risking capital the company must replay a strategy against historical observations and determine where its hypothesis fails, without using information that was unavailable at each decision.

## Observable outcome

An Agent registers a pinned historical dataset and strategy, runs the common company trading and risk contracts against an isolated virtual account with an explicit execution/cost model, and receives reproducible positions, paper costs and returns with provenance and gaps. The experiment service owns hypothesis, split and evaluation registration; this feature owns time-ordered replay and virtual execution, not a second return definition.

## Scope of discretion

An Agent can reject, revise or pursue an idea. A backtest is retrospective simulated evidence, not current-market paper operation, proof of sustainable returns, or authority for live trading.

## Development changes

- [Replay a pinned strategy and historical feed](replay/spec.md)
