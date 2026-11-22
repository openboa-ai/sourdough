# Register a reproducible strategy candidate

Design draft · Verification **UNRUN** · Responsible component: Strategy registry

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Register immutable source, artifact digest, inputs, evaluation reference, owner and prior revision. Return candidate ID and replay recipe.

Input contracts: [Assign an isolated company workspace](../../../../../systems/resources/company-assets/assign/spec.md) · [Register and replay a quant experiment](../../research/experiment/spec.md).

## Design decision and review

Register immutable source and dependency revisions instead of executing the tip of a shared branch.

**Concern for the responsible component:** Missing inputs or a nonreproducible candidate block activation; a successful research run confers no trading identity.

## Functional and quality requirements

- **FR.** Resolve source and dependencies at the recorded revision. Preserve both candidate and evaluation history.
- **NFR.** Editing a shared branch cannot alter a registered artifact or active deployment.

## Acceptance and independent evidence

**Oracle:** A fresh isolated checkout resolves the recorded revision and inputs after the shared branch changes. Compare artifact identity, not the candidate's display name.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Have two Agents contribute to a company repository. Register a tested revision and reproduce its artifact and sample decision. |
| Failure or contention | Change source after registration. Reject a digest mismatch and leave active revision unchanged. |

## Operations and change review

Monitor unreproducible candidates and mark them ineligible until corrected.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
