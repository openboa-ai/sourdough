# Withdraw delegated authority

Design draft · [Identity and authority](../README.md) · [Company MVP map](../../../../README.md) · [Development lifecycle](../../../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../../../capabilities.md#mandate).

## Operating need

Closing a UI or stopping a worker does not revoke its effective permissions.

## Observable outcome

Late use from a retired delegation cannot create a new effect; previously accepted work stays accounted for.

## Scope of discretion

Revoke only the affected scope. An already admitted external action must be resolved, not erased.

## Development changes

- [Fence a retired grant](retire/spec.md)
