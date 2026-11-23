# Compose an immutable execution profile

Design draft · Verification **UNRUN** · Responsible component: Agent Configuration

[Feature intent](../intent.md) · [MVP map](../../../../README.md) · [SDLC handoff](../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: A versioned configuration schema and a declared adapter manifest describing supported provider/model choices. Validating the declaration is local and has no side effects. An active registry, company, account or model call is not needed to prepare a profile.

## Design decision and review

Return an immutable profile reference for the explicit runtime implementation, adapter revision, provider, model revision and pinned `sandbox_backend` (local macOS sandbox enforcement required, with no automatic fallback to unsandboxed or alternate OS backends). Do not settle mutable aliases or sandbox defaults silently: if the selection or sandbox backend cannot be pinned, report it as unresolved. Configuration owns the description; organization binds one profile to each new Agent ID. The harness checks actual eligibility, sandbox availability, current consent, grants and budget before launch. A profile can be reused, not rewritten.

## Functional and quality requirements

- **FR.** Compose supported alternatives without hardwiring a vendor. Pin `sandbox_backend` as part of the immutable profile; a new model, runtime or `sandbox_backend` selection yields another profile rather than rebinding an existing Agent. A fresh session or machine on the same binding does not constitute a profile change.
- **NFR.** Record the effective selection without secrets. Replaying an identical declaration yields the same reference; changing any pinned field (including `sandbox_backend`) cannot yield that reference. Unknown compatibility or an unavailable local macOS sandbox backend fails closed before launch.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Declare two supported models for one adapter and pinned macOS `sandbox_backend`; compose profiles P and Q, then repeat P. Expect two distinct references and one stable P, zero hires and zero provider starts. Organization can independently hire A and B on P without collapsing their identities. |
| Failure or contention | Declare an unsupported pair, an unresolved model alias or an unavailable `sandbox_backend`; expect no eligible profile and no fallback launch. Attempt to bind Q under an existing A/P session; admission denies the mismatch and no tool runs. A predecessor's receipts still identify the predecessor. |

**Oracle:** Compare canonical fields, profile references and roster binding with independent registry/start and action logs. Test the separate [real adapter qualification](../../harness/adapters/qualify/spec.md) before claiming provider support.

## Operations and change review

Deprecation blocks new launches under the withdrawn profile; it does not rewrite past evidence. Block the affected Agent and retain company recovery duties. When a successor is needed, use an authorized hire or existing eligible Agent, fresh authority and a recorded work handoff.
