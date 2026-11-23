# Rehydrate a private desk under current grants

Design draft · Verification **UNRUN** · Responsible component: Workspace service

[Feature intent](../intent.md) · [MVP map](../../../../README.md) · [SDLC handoff](../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [company-assets/assign](../../../../company/systems/resources/company-assets/assign/spec.md) · [authorize-actor/check](../../../../company/systems/authority/authorize-actor/check/spec.md).

## Design decision and review

Keep SQLite `private_desk` as the authoritative store for Desk metadata, revision history and grant resolution, while permitting OS-sandboxed read/write inside an isolated per-Agent Desk directory and per-run scratch directory under the Agent's pinned macOS `sandbox_backend` (zero outbound network egress; company SQLite store, credentials and other Agents' Desks unreadable). Mount only resources allowed at open time; shared documents retain their own ACLs.

Bundle consistent per-Agent Desk filesystem snapshots alongside SQLite `private_desk` revisions inside verified company checkpoints ([restore contract](../../../../company/systems/operations/restore-company/restore/spec.md)) and clone permitted Desk snapshots into isolated arm namespaces for paired operating-revision and offline learning comparisons so trial arms cannot mutate the live company Desk or read another arm's state.

**Concern for the responsible component:** Private means limited visibility inside company custody, not Agent ownership or automatic visibility to every colleague.

## Functional and quality requirements

- **FR.** Reopen the personal manifest by company Agent ID from SQLite `private_desk`, resolve current grants, bind the OS-sandboxed per-Agent Desk directory under the pinned `sandbox_backend` and initialize isolated per-run scratch.
- **FR.** Snapshot and restore per-Agent Desk files atomically with the matching `private_desk` revision during company checkpoint/restore and isolated comparison-arm cloning, excluding revoked mounts and ephemeral scratch unless explicitly registered.
- **NFR.** No provider cache, secret or stale mount becomes a transferable company asset simply by being present. Revoked resources must not reappear after restore, and raw private Desk contents must not be written into diagnostic logs.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Restart the pinned runtime on an authorized machine, reopen by authenticated Agent identity and verify the same authorized SQLite `private_desk` revision, sandboxed Desk files and outstanding duties. Confirm zero outbound network egress and unreadable company DB/secrets from inside Desk execution. A colleague sees only an explicitly shared asset. |
| Checkpoint and arm clone | Restore a verified company checkpoint (or clone an isolated comparison arm) and verify the Agent's Desk files match the restored `private_desk` revision without leaking revoked mounts or mutating the live company Desk. |
| Failure or contention | Revoke a shared project between sessions. Its old reference cannot yield content; a successor Agent on another runtime cannot enumerate the predecessor’s desk even when assigned the same duty. |

## Operations and change review

**Oracle:** Compare authorized inventory and actual listings under both identities; detach stale mounts without deleting company records.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.

Reopening a desk restores authorized private state, not a public share. [Publication](../publish/spec.md) requires a separate, audited disclosure decision. A receiver cannot follow a published work link to private notes or provider-native caches.
