# Register a versioned runtime contract

Design draft · Verification **UNRUN** · Responsible component: Runtime registry

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [authorize-actor/check](../../../../../company/systems/authority/authorize-actor/check/spec.md).

## Design decision and review

Register adapter identity, revision, protocol versions, start/cancel, native resume or fresh-context recovery, scoped tool calling, usage reporting and isolation constraints. Publish supported runtime/model combinations and constraints. Configuration chooses a combination before creation; at run time the harness checks the exact bound choice against capabilities and budget. It must not select a substitute.

**Concern for the responsible component:** Registering a local or remote adapter is not evidence that it can perform the duty. Unsupported capability means ineligible for the requested job.

## Functional and quality requirements

- **FR.** Return an immutable adapter revision and job eligibility reasons. Do not launch a provider during registration.
- **NFR.** Registration grants neither a company identity nor credential or spending authority. Reject incompatible versions or missing mandatory capabilities before launching.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Register two differently shaped adapter manifests with the same core contract; admit the requested compatible combination; reject a model not supported by its adapter. |
| Failure or contention | Supply a text-only adapter for an effectful turn, disable its revision or omit required consent: record blocked selection, no silent downgrade or provider fallback. |

## Operations and change review

**Oracle:** Compare registry selection to the sealed requirements and backend start count. Pin or withdraw the revision on drift.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.

**Admission profile:** Pin transport, provider destination, schema versions, effective native-tool policy, isolation evidence, usage coverage, cancellation behavior and approved cost path. An otherwise compatible adapter with unmediated access to protected resources is not eligible for effectful work. Return explicit reasons for ineligibility; do not silently fall back to another provider.
