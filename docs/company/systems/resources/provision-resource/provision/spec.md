# Provision and retire a scoped assignment

Design draft · Verification **UNRUN** · Responsible component: Resource service

[Feature intent](../intent.md) · [Asset assignment](../../company-assets/assign/spec.md) · [Authority check](../../../authority/authorize-actor/check/spec.md) · [Budget reservation](../../../finance/capital-budget/reserve-spend/spec.md)

## Boundary and dependencies

Accept an authenticated requester, intended user, asset or capability, purpose, environment, duration, estimated cost, request identity and expected revision. Authority owns grants; finance owns spending envelopes; resource service owns inventory, assignment and provisioned binding. The connector registry owns any external endpoint binding. No service acquires an external subscription merely from a request.

## Functional and quality requirements

- **FR-1:** Check current delegated authority and available budget before creating or changing an assignment. Within existing grants, provision without a Client or board interaction; otherwise record denial or a bounded unresolved need.
- **FR-2:** Use the existing asset assignment as the sole authoritative assignment; associate request identity, scope, expiry, finance reservation reference and usable binding with it. Return the same disposition for the same request and payload; conflict on changed payload. Do not expose raw credentials to an Agent.
- **FR-3:** On expiry, revocation or offboarding, coordinate Authority fencing and retirement of the binding. Finance settles outstanding costs, Assets transfers custody and Work accepts unfinished obligations; retain pending references until their owners complete them. Preserve recovery access to already incurred effects.
- **NFR:** Fail closed for unknown grant, budget or destination; preserve existing unrelated assignments. Provisioning is not evidence that the external resource is currently available.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Scoped provision | An Agent requests an already permitted read-only source. With Client closed, inspect the grant, reservation, inventory and successful read; an attempted write is denied. |
| Denial and race | Exhaust budget or revoke access before commit; no new usable binding or duplicate charge appears on retry. |
| Departure | Archive the Agent while a resource remains; personal access ends and the resource remains in company custody. The archived Agent receives no further duty. |

**Oracle:** Compare authority, finance, asset inventory, connector receipt and actual access outcomes, not a request acknowledgement. All cases are unrun.
