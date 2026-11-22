# Rehydrate a private desk under current grants

Design draft · Verification **UNRUN** · Responsible component: Workspace service

[Feature intent](../intent.md) · [MVP map](../../../../README.md) · [SDLC handoff](../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [company-assets/assign](../../../../company/systems/resources/company-assets/assign/spec.md) · [authorize-actor/check](../../../../company/systems/authority/authorize-actor/check/spec.md).

## Design decision and review

Resolve company/Agent to a durable desk manifest, private notes, work references and separate ephemeral scratch. Mount only resources allowed at open time; shared documents retain their own ACLs.

**Concern for the responsible component:** Private means limited visibility inside company custody, not Agent ownership or automatic visibility to every colleague.

## Functional and quality requirements

- **FR.** Reopen the personal manifest by company Agent ID, resolve current grants and initialize separate scratch.
- **NFR.** No provider cache, secret or stale mount becomes a transferable company asset simply by being present. Revoked resources must not reappear after restore.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Restart the pinned runtime on an authorized machine, reopen by authenticated Agent identity and verify the same authorized notes and outstanding duties. A colleague sees only an explicitly shared asset. |
| Failure or contention | Revoke a shared project between sessions. Its old reference cannot yield content; a successor Agent on another runtime cannot enumerate the predecessor’s desk even when assigned the same duty. |

## Operations and change review

**Oracle:** Compare authorized inventory and actual listings under both identities; detach stale mounts without deleting company records.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.

Reopening a desk restores authorized private state, not a public share. [Publication](../publish/spec.md) requires a separate, audited disclosure decision. A receiver cannot follow a published work link to private notes or provider-native caches.
