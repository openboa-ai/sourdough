# C5 · Company resource inventory and custody

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Owns durable asset identity, assignments and custody records. Authority owns effective grants, finance owns spending commitments and Work owns accepted duties. The Agent Desk resolves a permitted personal working set from the inventory; it does not own the underlying company assets.

### [Provision a scoped resource](provision-resource/intent.md)

- [provision](provision-resource/provision/spec.md) — Resolve a request through authority and finance, create a usable scoped assignment and retire it without losing company custody.

## Company inventory and shared custody

### [Keep workspaces, code and obligations in company custody](company-assets/intent.md)

An Agent leaving or losing a session must not strand a repository, a strategy or an open duty.

- [assign](company-assets/assign/spec.md) — Register one company workspace or shared asset assignment under the effective grant.
- [transfer](company-assets/transfer/spec.md) — Transfer asset custody while Authority fences the former custodian and Work transfers unfinished duties.
