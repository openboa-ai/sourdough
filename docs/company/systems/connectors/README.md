# C6 · External resource connectors

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Owns how the company binds to outside resources, not what each business result means. A repository or dataset reader and a venue can implement different capabilities of the same connector envelope. An Agent Runtime implements a separate execution port.

## Capability registry and invocation

### [Connect an external resource by capability](connect-resource/intent.md)

The company needs typed access to outside resources without baking a venue or repository API into company workflows.

- [register](connect-resource/register/spec.md) — Register a scoped connector profile. Attempt to use a dataset grant with the venue or replace a test destination with a live one under the same revision. Deny and record the mismatch.
- [invoke](connect-resource/invoke/spec.md) — Dispatch and observe a connector operation. Lose the write response. An independent destination log shows one dispatch; if lookup is unavailable keep outcome unknown and assign a recovery duty.
