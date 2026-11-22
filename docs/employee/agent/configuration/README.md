# A4 · Agent Configuration

[System map](../../../README.md#system-hierarchy) · [Domain glossary](../../../glossary.md)

Owns the execution profile format and its immutable revisions. It does **not** register adapters, hire an Agent, authorize spending or start a run. A profile may be used for several independent hires. A new option in the catalog never changes existing Agents.

### [Configure a new Agent](intent.md)

- [compose](compose/spec.md) — Pin a runtime, adapter, provider and compatible model before creating the Agent; reject an ambiguous or conflicting profile.
