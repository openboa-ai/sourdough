# Configure a new Agent

Design draft · [Agent Configuration](README.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

Business situation: [Match capacity to an obligation](../../../capabilities.md#staffing).

## Operating need

A company must be able to staff the same role with Agents built on different models or runtimes. It must also know exactly who produced a finding or exercised a grant. A role, runtime implementation and Agent ID are not interchangeable.

## Observable outcome

The founder or hiring actor selects a supported combination for each new Agent. Two Agents may even choose the same combination. A fresh session can resume the original binding; a different binding never authenticates as the original Agent. The company can transfer a duty without moving private state, permissions or authorship.

## Scope of discretion

Selection is constrained by capability, consent, cost and approved models. A supported profile is not proof of a working adapter, an approved hire or an executed turn.

## Development changes

- [Compose an immutable execution profile](compose/spec.md)
