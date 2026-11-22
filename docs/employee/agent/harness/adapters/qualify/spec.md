# Qualify distinct Agents on different runtimes

Design draft · Verification **UNRUN** · Responsible component: Runtime integration

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [adapters/register](../register/spec.md) · [execute/tool-turn](../../execute/tool-turn/spec.md) · [accepted duty handoff](../../../../../company/systems/work/collaborate/handoff/spec.md).

## Design decision and review

Normalize lifecycle, usage and typed tool events without normalizing away actor identity. Qualify profiles independently. Reopening A on its pinned profile is recovery; moving a company duty to B is a recorded handoff.

**Concern for the responsible component:** Registering a local or remote adapter is not evidence that it can perform the duty. Unsupported capability means ineligible for the requested job.

## Functional and quality requirements

- **FR.** Report fixture compatibility, actual execution on each profile and explicit succession as separate results. Record both Agent IDs, profile references, original effect IDs and accepted work ownership.
- **NFR.** A provider timeout or a new session cannot reissue an unknown effect. Recover the company receipt first; mark unavailable usage unknown, never zero.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Create A and B on distinct qualified runtime/model profiles. Observe A’s real tool call. Fence its work claim, transfer an outstanding duty with scoped references, and observe B accept it under B’s own grant. History still credits A; no order is submitted twice. |
| Failure or contention | Attempt to launch A on B’s profile, replay A’s callback after fencing, or open A’s private desk as B. Expect denial, zero unauthorized effects and no copied grants. If handoff is incomplete the company retains a visible duty. |

## Operations and change review

**Oracle:** Grade contract fixtures, each actual runtime and actual cross-runtime handover separately. Only observed configurations can be advertised as supported.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.

Run the [common discrimination tests](../../protocol.md#qualification-ladder-and-delivery-order) on each adapter revision. Qualify one genuine runtime first; a second is required only for a portability claim, not the first trading demo. Retain separate outcomes for conformance, actual single-runtime behavior, observed distinct-Agent handoff and cost coverage.
