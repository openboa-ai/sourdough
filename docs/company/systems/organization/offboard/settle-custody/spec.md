# Preserve already admitted company effects

Design draft · Verification **UNRUN** · Responsible component: Effect-owning company service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Archiving ends the Agent's duties. It does not delete company records, shared assets, or effects accepted before the cutoff. Do not copy the former Agent's private workspace to another Agent.

Input contracts: [offboard/fence](../fence/spec.md) · [company assets](../../../resources/company-assets/intent.md).

## Design decision and review

Reuse the effect owner's existing reconciliation path and original effect ID. Do not create a retirement queue, restart the Agent, or make archiving wait for effect settlement. A company program without its own effective authorization cannot add new effects.

## Functional and quality requirements

- **FR.** Preserve original attribution. Reconcile an admitted effect without authorizing a new action or reopening the departed Agent's duties.
- **NFR.** Restart and redelivery cannot duplicate the effect. An unknown effect stays visible in its owning service and remains separate from the Agent's archived state.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Restart the company after archiving the Agent. Its existing effect owner recognizes a valid earlier receipt; shared records remain available to authorized readers. |
| Failure or contention | Withhold a receipt or replay an old Agent callback. The effect stays unresolved or is booked only once as appropriate. No new duty or send is created for the archived Agent. |

**Oracle:** Compare the archive, original action and effect-owner receipts. Run [O1](../../../../../acceptance/episodes.md#o1-offboarding).

## Operations and change review

Report unknown effects through the owning service. Never auto-delete an order or reset company books on departure.
