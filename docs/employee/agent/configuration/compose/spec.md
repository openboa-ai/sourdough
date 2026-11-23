# Compose an immutable execution profile

Design draft · Verification **UNRUN** · Responsible component: Agent Configuration

[Feature intent](../intent.md) · [MVP map](../../../../README.md) · [SDLC handoff](../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: A versioned configuration schema and a declared adapter manifest describing supported provider, model and sandbox backend choices. Validating the declaration is local and has no side effects. An active registry, company, account or model call is not needed to prepare a profile.

## Design decision and review

Return an immutable profile reference for the explicit runtime implementation, adapter revision, provider, model revision and sandbox backend. The user selects the sandbox during Agent configuration, independently of the model. Local macOS is the required supported option; other backends are selectable only after qualification for the chosen runtime. Do not settle mutable aliases silently: if the selection cannot be pinned, report it as unresolved. Configuration owns the description; organization binds one profile to each new Agent ID. The harness checks actual eligibility, current consent, grants and budget before launch. A profile can be reused, not rewritten.

## Functional and quality requirements

- **FR.** Compose supported alternatives without hardwiring a vendor. A change of sandbox backend is a new profile selection, not a rebinding of an existing Agent. The Agent does not select or switch its sandbox at run time. A fresh session or machine on the same binding does not constitute a profile change.
- **NFR.** Record the effective selection without secrets. Replaying an identical declaration yields the same reference; changing any pinned field cannot yield that reference. Unknown compatibility is not approval.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Declare two supported models for one adapter; compose profiles P and Q, then repeat P. Expect two distinct references and one stable P, zero hires and zero provider starts. Organization can independently hire A and B on P without collapsing their identities. |
| Failure or contention | Declare an unsupported pair or an unresolved model alias; expect no eligible profile. Attempt to bind Q under an existing A/P session; admission denies the mismatch and no tool runs. A predecessor's receipts still identify the predecessor. |
| Sandbox selection | Select the qualified local backend and verify the effective backend matches on two runs. An unavailable or unqualified selection yields no launch and no fallback. With two qualified backends, composing the same runtime and model with the second backend yields a different profile; the existing Agent cannot launch on it. |

**Oracle:** Compare canonical fields, profile references and roster binding with independent registry/start and action logs. Test the separate [real adapter qualification](../../harness/adapters/qualify/spec.md) before claiming provider support.

## Operations and change review

Deprecation blocks new launches under the withdrawn profile; it does not rewrite past evidence. Block the affected Agent and retain company recovery duties. When a successor is needed, use an authorized hire or existing eligible Agent, fresh authority and a recorded work handoff.

For local Codex setup, require an explicit `employee-profile --sandbox local-macos` selection that matches the adapter declaration. Include it in the profile identity and check that identity before a bound run. The local executable adapter cannot declare a hosted backend for admission until that backend is qualified. Existing process fixtures without a sandbox declaration retain their previous profile identity for deterministic regressions; they do not qualify an operating sandbox.
