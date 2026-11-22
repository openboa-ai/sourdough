# Bind a runtime turn to company tools

Design draft · Verification **UNRUN** · Responsible component: Runtime adapter

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Resolve each tool request against its current actor generation and the responsible company or enabled business service. External operations use the company connector boundary and its current destination-scoped authorization; the harness cannot authorize a trade, change its own grants or write a financial result.

Launch with provisioned consent, budget, workspace and a company-issued work/generation grant. Deliver the decision snapshot; return run ID, tool receipts, terminal status and observed usage.

Input contracts: [Work generation](../../../../../company/systems/work/resume-work/recover-shift/spec.md) · [Registered runtime](../../adapters/register/spec.md) · [Scoped actor](../../../../../company/systems/authority/authorize-actor/check/spec.md) · [Scoped context manifest](../context-pack/spec.md).

## Design decision and review

Require an observed tool invocation and service-side receipt instead of inferring a tool action from model text.

**Concern for the responsible component:** Pin a runtime and verify unattended consent and cost bounds before accepting it. A blocked provider leaves judgment and first integration unverified.

## Functional and quality requirements

- **FR.** Expose ordinary company tools scoped to the grant. Record unexpected consent prompts or tool failures as blocked work.
- **NFR.** A completed turn requires a separately persisted effect receipt to establish an effect. A replacement session cannot use a revoked grant.

## Acceptance and independent evidence

**Oracle:** Join the provider-side turn and invocation ID to the company's authenticated tool receipt. A transcript that says an order was placed is insufficient.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | With the board closed, witness an actual tool call and the service receipt. Preserve the Agent and work identity across a new session on the original binding. Reject a different binding before any tool receives the request. |
| Failure or contention | Exercise a scripted peer separately. Revoking a generation before a late mutation must produce zero financial effects; a blocked prompt cannot be counted as progress. |

## Operations and change review

Monitor time to first tool receipt and budget exhaustion. Stop new billable turns at the ceiling while service-owned reconciliation continues.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.

**Tool mediation.** Validate the canonical schema and argument digest, bind provider call ID to company action ID, check current claim/grant at admission and persist the typed service receipt before returning an answer. A tool-request event is not financial evidence. Prevent unmediated native tools from reaching protected state. Test duplicate calls with changed arguments, stale callbacks, a consent prompt and a lost reply. Use the wire records in the [harness contract](../../protocol.md); closing the provider turn does not close outstanding effects.
