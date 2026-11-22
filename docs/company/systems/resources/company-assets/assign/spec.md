# Assign an isolated company workspace

Design draft · Verification **UNRUN** · Responsible component: Asset service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Register company asset ID, storage or compute reference, custodian, access policy and source revision. Mount only the assigned scope for an Agent generation.

Input contracts: [Persist the founding charter](../../../organization/found-company/bootstrap/spec.md) · [Check current authorization](../../../authority/authorize-actor/check/spec.md). Initial asset assignment is provisioned under an authorized company actor; it does not require an Agent turn or an already-open desk. A running Agent accesses the assignment only through its current authenticated generation.

## Design decision and review

Use logical company ownership with scoped workspace access rather than tying durable assets to a particular host or model session.

**Concern for the responsible component:** Separate private scratch, shared project outputs and credential handles in both listings and reads; physical per-Agent hosts are not a prerequisite.

## Functional and quality requirements

- **FR.** Allow private Agent scratch and explicit shared project resources. Keep strategy source, durable outputs and key handles on company inventory.
- **NFR.** An Agent cannot enumerate another private workspace or obtain venue credentials via file access. Destroying a runtime preserves durable work products.

## Acceptance and independent evidence

**Oracle:** From two separately scoped runtime sessions, compare readable and writable assets against the inventory grants before and after replacing one session.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Run two identities, share one project and verify allowed collaboration on that revision. |
| Failure or contention | Attempt cross-workspace read and write under the other Agent identity. Both are denied and neither changes company grants. |
| First provisioning | From a founded company with no Agent run or desk, authorize and create the founder's assignment, open the desk and then start the first bounded turn. No later tool receipt is required to create the assignment. |

## Operations and change review

Observe denied access and unavailable assets. Disable a compromised binding while retaining company custody.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
