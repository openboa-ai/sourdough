# Run forward paper trading on current market data

Design draft · [Trading, risk and venues](../README.md) · [Company MVP map](../../../../../README.md)

Business situation: [Choose and perform work](../../../../../capabilities.md#judgment) and [authorize an effect](../../../../../capabilities.md#effects).

## Operating need

Agents need to evaluate decisions against market observations arriving now while never sending real-money orders. A functional exchange test environment, a deterministic crash fixture and a current-market paper account serve different purposes and have separate evidence.

## Observable outcome

The same company trading core receives read-only, current production market data and dispatches to an independently durable virtual venue/account. It makes and reconciles simulated orders continuously, records the execution and cost assumptions, survives interruption and reports company-net paper results. A stale or missing feed is not a valid current price.

## Scope of discretion

Agents choose strategy, hold and next work within the existing grant; the risk service enforces the same invariants in every environment. A simulated return is evidence about the configured model, not identical real fills or a realized return. The company launches and configures its selected runtime; the runtime's trading and simulation capabilities are reused rather than independently reimplemented.

## Development changes

- [Run and recover a current-market paper account](run/spec.md)

This feature is independent of a venue's test environment. Its [historical replay](../../investments/backtest/intent.md) counterpart uses archived data with the same trading contracts.
