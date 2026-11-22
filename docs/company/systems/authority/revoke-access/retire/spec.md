# Fence a retired grant

Design draft · Verification **UNRUN** · Responsible component: Authorization service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [authorize-actor/check](../../authorize-actor/check/spec.md).

## Design decision and review

Commit revocation with an effective revision. Invalidate derived grants within the retired scope and gate new admissions on the current revision. Retain prior receipts.

**Concern for the responsible component:** Revoke only the affected scope. An already admitted external action must be resolved, not erased.

## Functional and quality requirements

- **FR.** Record the effective revocation and reject subsequent use of the affected grant and scopes derived from it.
- **NFR.** Order racing requests and revocation by their admission records. An effect already in flight may still complete.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Revoke a shared-project write grant during a session. Reads separately granted still work; a later write is denied. |
| Failure or contention | Submit an old delegated grant or delayed child request after revocation. Verify no new mutation; retain a duty for pre-revocation in-flight effects. |

## Operations and change review

**Oracle:** Reconcile revocation and admission sequences against the resource audit; contain the affected resource if enforcement lags.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
