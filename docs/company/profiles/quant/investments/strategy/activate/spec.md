# Activate, supervise and roll back one program

Design draft · Verification **UNRUN** · Responsible component: Deployment service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Take candidate ID, environment, deployment grant, custodian and risk envelope. Mint a revocable program identity for the synthetic venue; its tool calls use the shared admission path.

Input contracts: [Register a reproducible strategy candidate](../register/spec.md), deployment authority, an eligible account binding and the risk/reconciliation service contracts. An adopted [portfolio target](../../portfolio-target/adopt/spec.md) is an input only for a program that consumes one; a newly activated program may itself propose the first target. There need not be an existing order or [reconciliation receipt](../../../trading/recover-order/reconcile/spec.md) before first activation; any outstanding effect encountered during replacement must still be reconciled.

## Design decision and review

Grant a separate, revocable deployment identity instead of executing code with its author's credentials.

Apply the cross-domain [company change activation](../../../../../systems/operations/manage-change/activate/spec.md) contract; this strategy-specific service owns its economic and trading eligibility. Independent functional verification is not proof of improved net return. Direct Agent-originated orders use ordinary risk admission without requiring a program deployment.

This service records the single effective strategy deployment and receipt. Operations supervises that deployment by reference; it does not maintain a second authoritative activation. Strategy evaluation, deployment and subsequent order admission remain separate decisions.

**Concern for the responsible component:** Verify rollback fences the old revision even with a live worker and partial fills; new risk cannot migrate until the old writer is fenced.

## Functional and quality requirements

- **FR.** Record the activation revision and its first observed run. A rollback stops new intents from the retired revision and hands its open exposures to reconciliation.
- **NFR.** An author grant cannot start trading. Rotating code cannot reset risk usage or erase orders.
- **FR.** An authorized company actor references the applicable evaluation and effective operating revision when activating a candidate; unsupported, incomplete or unapproved comparison evidence cannot be represented as a demonstrated improvement. A bounded initial paper revision can be activated for its first absolute observation without inventing a prior incumbent.

## Acceptance and independent evidence

**Oracle:** Join venue intent and fill history to the originating deployment identity and revision. Compare the last old-revision admission and first new one with the effective rollback receipt.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Activate a candidate and trace one simulated program intent to its exact version, account and custodian. Hand over supervision to a successor. |
| Failure or contention | Roll back while an order is partially filled. Reject old-generation intents and keep the original remainder under the existing reservation. |
| First deployment | Starting with a verified flat paper account, no target and no orders, activate an eligible program that proposes a target. Risk separately admits any resulting order; activation alone cannot place one. |

## Operations and change review

Monitor program heartbeat, stale revision calls and unresolved orders. Suspend new risk on a failing deployment.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
