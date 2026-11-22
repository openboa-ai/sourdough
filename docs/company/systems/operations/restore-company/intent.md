# Recover the company when its working store is lost

Design draft · [Operations, oversight and recovery](../README.md) · [Company MVP map](../../../../README.md) · [Development lifecycle](../../../../engineering/lifecycle.md)

Business situation: [Detect interruption and continue](../../../../capabilities.md#continuity).

## Operating need

A supervisor can restart a dead process; it cannot reconstruct a missing or damaged company store.

## Observable outcome

A verified local checkpoint and retained events recover the same company, obligations and custody. If evidence is incomplete the company refuses new dependent risk.

## Scope of discretion

Restore within the existing identity and authorized environment. A fresh company, reset balances or concurrent writer is not recovery.

## Development changes

- [Recover a company from a verified checkpoint](restore/spec.md)
