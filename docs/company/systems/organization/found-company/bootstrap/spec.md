# Persist the founding charter

Design draft · Verification **UNRUN** · Responsible component: Company service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Accept setup identity, charter revision, founder scope and founder execution profile. Atomically return company, founder, grant and due-work IDs. Do not create a trading account.

Input contracts: One provisioned local environment and initial board mandate. Use the [immutable execution profile](../../../../../employee/agent/configuration/compose/spec.md); founding does not start a runtime or depend on an active registry. No industry profile or external account is required.

## Design decision and review

Use one atomic founding receipt rather than creating the founder, grant and first duty on separate retries.

**Concern for the responsible component:** Recovery tooling must distinguish an empty first-use directory from a missing existing store; reject an ambiguous reopen.

## Functional and quality requirements

- **FR.** Repeated setup with the same identity returns the original receipt. Reopening loads its store and due work.
- **NFR.** Reject incompatible founding replay. Keep one active company writer.

## Acceptance and independent evidence

**Oracle:** Compare independent store inventory and scheduler receipts with the original company, founder, grant and due-work IDs after reopen.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Found a company with no industry profile, close the UI and reopen. One founder and due item remain. In the quant integration separately bind the simulated account to that same company. |
| Failure or contention | Interrupt after commit before reply and retry: one founder, grant and due item. Changing the mandate under the same request conflicts. |

## Operations and change review

Track conflicting bindings. Quarantine the attempted open and preserve the original store.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
