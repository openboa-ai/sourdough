# Connect an external resource by capability

Design draft · [External resource connectors](../README.md) · [Company MVP map](../../../../README.md) · [Development lifecycle](../../../../engineering/lifecycle.md)

Business situation: [Authorize and resolve an effect](../../../../capabilities.md#effects).

## Operating need

The company needs typed access to outside resources without baking a venue or repository API into company workflows.

## Observable outcome

One connector protocol can represent a read-only dataset and a separately admitted effectful venue while preserving each domain's own semantics.

## Scope of discretion

Registering a connector is not permission to act. Never infer order finality or global exactly-once delivery from a generic success response.

Only a current, authenticated and domain-admitted effect reaches an effectful connector. A recovery observation uses the domain's existing obligation and does not create fresh authority to send.

Fund research and reconciliation may use scoped external reads directly, independently of the managed trading runtime. Agent tools, company services and managed software use the same company-controlled boundary; ordinary external writes cannot bypass a domain's risk admission. Agents may propose connector extensions as company assets; [change activation](../../operations/manage-change/intent.md) governs their adoption.

## Development changes

- [Register a scoped connector profile](register/spec.md)
- [Dispatch and observe a connector operation](invoke/spec.md)
