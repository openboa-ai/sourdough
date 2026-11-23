# Decide a scoped capability at use time

Design draft · Verification **UNRUN** · Responsible component: Authorization service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [authorize-actor/bind](../bind/spec.md) · [Role definitions and assignments](../manage-roles/spec.md).

## Design decision and review

Evaluate principal, action, resource, environment, current grant and policy revisions before the protected effect. Return a scoped decision and reason, not a reusable blanket permission.

A matching scoped role assignment grants an action only when its current resource and work constraints also hold. Read the current revisions from the owning stores; do not use the Client, a tool list, a token claim or an earlier allow as authority. The service that admits the action enforces the decision; an authorization check does not replace the responsible domain's validation.

**Concern for the responsible component:** Grants are scoped to company, resource, operation, environment, time and applicable budget.

## Functional and quality requirements

- **FR.** Return an allow or deny decision for the requested action with the evaluated grant and policy revisions; recheck for a new admission.
- **NFR.** Unknown scope denies; registration, job title, a prior allow and receipt replay cannot grant a new effect.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Give one Agent read access to a shared project but not writes. Compare a permitted read and a denied write against the signed grant. |
| Failure or contention | Use a stale revision or a valid Agent from another company; the independent resource audit shows zero protected effects. |

## Operations and change review

**Oracle:** Compare effective grants, decision receipts and resource logs. Disallow stale authorizations on policy-reader failure.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
