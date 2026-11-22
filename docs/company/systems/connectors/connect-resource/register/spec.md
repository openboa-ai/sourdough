# Register a scoped connector profile

Design draft · Verification **UNRUN** · Responsible component: Connector registry

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [authorize-actor/check](../../../authority/authorize-actor/check/spec.md).

## Design decision and review

Bind company connector instance, kind, environment, endpoint identity, versioned operation schemas, read/write class, credential handle and recovery capabilities. A grant references explicit operations and destinations.

A company-owned adapter candidate is separately verified and activated under [company change management](../../../operations/manage-change/activate/spec.md). Registration does not enable a new external action or authorize its developer. The same registry can describe a model, research source, issue service or exchange without imposing exchange fields on unrelated operations.

**Concern for the responsible component:** Registering a connector is not permission to act. Never infer order finality or global exactly-once delivery from a generic success response.

## Functional and quality requirements

- **FR.** Pin operation schemas, credential handles and destination identity to the connector revision. Replacement produces a new revision.
- **NFR.** Reject unknown operations, incompatible upgrades and environment changes by default; never expose raw credentials to Agents.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Register a local dataset reader and a simulated-venue connector. Both expose typed capabilities, but only the venue can advertise submit and order lookup. |
| Failure or contention | Attempt to use a dataset grant with the venue or replace a test destination with a live one under the same revision. Deny and record the mismatch. |

## Operations and change review

**Oracle:** Compare registry, grant and resolved destination. Disable only the changed binding; preserve outstanding effect records.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
