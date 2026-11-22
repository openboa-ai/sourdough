# Bind the fund to its simulated account

Design draft · [Trade execution, market risk and venues](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Authorize and resolve an effect](../../../../../capabilities.md#effects).

## Operating need

A company can exist without a venue; the fund must know which independent account it may use.

## Observable outcome

Before the first shift a verified, synthetic-only binding names the company, venue account, connector revision, instrument terms and standing risk envelope. Reopen resolves that same binding.

Synthetic-only qualifies the execution account, not the market-data source: a paper binding may consume separately authorized read-only current production observations under [forward paper](../live-paper/intent.md). It never obtains production order authority.

## Scope of discretion

Setup is authorized once. It is not approval of each order. No automatic replacement account, live fallback or dependency on external testnet.

## Development changes

- [Admit and reopen a paper mandate](bind/spec.md)
