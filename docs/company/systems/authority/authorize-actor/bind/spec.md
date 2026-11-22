# Bind a principal to a company identity

Design draft · Verification **UNRUN** · Responsible component: Identity service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [found-company/bootstrap](../../../organization/found-company/bootstrap/spec.md).

## Design decision and review

Exchange provisioned proof for a bounded company principal. Record issuer, company, subject, Agent or program ID, verified execution profile for an Agent, generation and expiry; never accept a caller-supplied role as proof.

**Concern for the responsible component:** Grants are scoped to company, resource, operation, environment, time and applicable budget.

## Functional and quality requirements

- **FR.** Resolve one stable principal from verified proof; issue a bounded session or a typed denial, never both.
- **NFR.** Reject a tenant, audience, expiry or generation mismatch before resolving grants.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Start two sessions on A’s original bound profile: one Agent, distinct sessions. Start B on another profile: a distinct verified Agent even if assigned A’s former role. |
| Failure or contention | Change only the asserted company, present B’s runtime binding as A, or replay a retired proof: zero issued credentials and zero resource reads. |

## Operations and change review

**Oracle:** Compare issuer and service audit IDs. Disable the affected binding on unexplained identity changes. Do not log raw proof.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
