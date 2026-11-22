# Connect a conforming Agent Runtime

Design draft · [Agent execution and runtime adapters](../README.md) · [Company MVP map](../../../../README.md) · [Development lifecycle](../../../../engineering/lifecycle.md)

Business situation: [Match capacity to an obligation](../../../../capabilities.md#staffing).

## Operating need

The same company tools can serve distinct Agents created on different admitted runtimes. A provider outage cannot silently turn one Agent into another.

## Observable outcome

Each configuration can select a supported model when constructing an Agent. Different implementations translate their local or remote protocol into the same company-owned run, tool and settlement records. A published integration guide is not runtime qualification. See the [source comparisons and test ladder](../protocol.md).

## Scope of discretion

Registering a local or remote adapter is not evidence that it can perform the duty. Unsupported capability means ineligible for the requested job.

## Development changes

- [Register a versioned runtime contract](register/spec.md)
- [Qualify distinct Agents on different runtimes](qualify/spec.md)
