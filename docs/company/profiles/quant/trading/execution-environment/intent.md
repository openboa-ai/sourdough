# Use one trading system with isolated environment bindings

Design draft · [Trading, risk and venues](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Authorize and resolve an effect](../../../../../capabilities.md#effects).

## Operating need

The company must research, decide, admit, dispatch, reconcile, book and report through the same trading system whether its destination is simulated or real. NautilusTrader is the selected planned trading runtime for market data, strategies, historical backtests, simulated execution and the exchange adapter. Sourdough launches, configures, observes and stops the runtime; it does not build another matching, backtesting or trading engine merely to run it. An Agent or company program uses the same contracts; it does not change its strategy logic to bypass controls for a different destination.

## Observable outcome

The company pins a specific environment binding per account and action. The same versioned trading core receives either a simulated or separately authorized external venue binding. It never silently upgrades a paper grant into real-money authority; paper, external test and live accounts, secrets, books, reservations and evidence remain isolated.

Market-data provenance and execution destination are independent bindings. A forward paper account can consume read-only current production market observations while every order goes only to its isolated simulated venue. A historical backtest consumes an archived, time-bounded source. An external non-production venue is a separate functional adapter test, not a substitute for either.

The company owns the operational lifecycle and configuration of the selected runtime, including approved dependency provisioning and status. Setup through a client is optional: a permitted Agent or company service can configure and start it within the mandate without a person manually launching a separate application. This is a planned integration, not evidence the runtime is installed or working.

## Scope of discretion

The board preauthorizes destinations and risk envelopes. A permitted company actor may select among already authorized bindings; an Agent cannot manufacture live authority. Changing a binding never moves an outstanding order, position or credential to another account. Unresolved prior effects remain owned and reconcilable. Unsupported capabilities block the affected action rather than faking venue equivalence.

## Development changes

- [Bind an execution environment to the shared trading core](bind/spec.md)
- [Launch and configure the selected trading runtime](launch/spec.md)
- [Qualify the trading-runtime integration against company contracts](qualify-engine/spec.md)

The [paper account contract](../paper-account/intent.md) remains the first isolated binding. The [parity acceptance](../../../../../acceptance/execution-parity/spec.md) measures common-core behavior separately from actual external venue qualification.
