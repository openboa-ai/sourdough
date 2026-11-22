# Test a market hypothesis on held-out evidence

Design draft · [Quant research and portfolio management](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Choose and perform work](../../../../../capabilities.md#judgment).

## Operating need

An Agent needs a defensible basis for risking capital or abandoning an idea.

## Observable outcome

For the first shift, the acting Agent publishes its own bounded, reproducible finding and cites its implication in the later decision. A full experiment separately retains its benchmark, point-in-time dataset, costs and held-out results.

The separate [historical replay capability](../backtest/intent.md) runs the registered strategy and risk contracts against a pinned dataset; the experiment owns the hypothesis and split rather than another execution engine.

The first operating revision earns its own prospective company-net result before any claim of improvement. A later proposed revision is compared to that incumbent as a whole operating configuration, not as Agent versus code.

## Scope of discretion

Research can end in rejection or further observation. A favorable backtest grants no trading authority.

## Development changes

- [Register and replay a quant experiment](experiment/spec.md)
- [Register and compare operating revisions](compare-operating-revisions/spec.md)
- [Publish an Agent-authored research finding](decision-note/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
