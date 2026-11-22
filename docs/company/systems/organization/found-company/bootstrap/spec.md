# Persist the founding charter

Product-owner approval: 2026-11-22, requirements at `1fbf55d12dfdf6023869a70cb536d92f2c6147dc`. Implementation of this founding unit is admitted after the guide and approval record merge and the resulting main CI passes. Verification **UNRUN** · Responsible component: Company service

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
- **Store compatibility.** Record the company identity and schema revision in the founding transaction. Before scheduling on reopen, compare the expected identity and supported schema; an absent expected store, identity mismatch or unsupported revision must not create a replacement or start work. A later schema upgrade is an explicit storage operation, not a side effect of founding.

## Acceptance and independent evidence

**Oracle:** Compare independent store inventory and scheduler receipts with the original company, founder, grant and due-work IDs after reopen.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Found a company with no industry profile, close the UI and reopen. One founder and due item remain. In the quant integration separately bind the simulated account to that same company. |
| Failure or contention | Interrupt after commit before reply and retry: one founder, grant and due item. Changing the mandate under the same request conflicts. |
| Incompatible reopen | Open a copy stamped with a newer, unsupported schema. Observe a compatibility refusal, unchanged company records and zero duty starts; no second store is created. |

## Operations and change review

Track conflicting bindings. Quarantine the attempted open and preserve the original store.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
