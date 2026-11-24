# Publish a selected private work product

Design draft · Verification **UNRUN** · Responsible component: Workspace publication service

[Feature intent](../intent.md) · [MVP map](../../../../README.md) · [SDLC handoff](../../../../engineering/lifecycle.md)

## Boundary and dependencies

Take a private artifact revision, bounded selection, destination project, explicit audience, purpose, owner and expected destination revision. Check source right to disclose, destination write and audience policy. Produce a new company-owned shared artifact and receipt.

Input contracts: [desk/reopen](../reopen/spec.md) · [authorize-actor/check](../../../../company/systems/authority/authorize-actor/check/spec.md) · [company-assets/assign](../../../../company/systems/resources/company-assets/assign/spec.md).

## Design decision and review

Publish a reviewed selection rather than changing the ACL of the entire desk. Private provenance stays protected; public-facing metadata must not expose restricted source names or excerpts.

The initial local project binds its audience to an explicit membership revision under an existing company grant. An Agent publishes a bounded character range of its own current draft as a plain-text asset, not a file path or active attachment. A project reader must be in both the selected audience and the current membership. The public receipt contains no private draft path, name or surrounding text. No publication changes duty ownership.

**Concern for the responsible component:** Reading a source does not grant redistribution; referenced attachments retain independent ACLs. Reject disallowed embedded content or publish an explicitly redacted selection.

## Functional and quality requirements

- **FR.** Persist immutable shared content, version and intended audience. Bind the output receipt to its work; sharing alone cannot transfer ownership of a duty.
- **NFR.** Repeated publication with identical identity returns one revision. A changed payload conflicts. Revocation before commit blocks publication; never mount one Agent’s whole desk into another runtime.
- **Discovery.** The recipient can page through all authorized shared references without an author receipt. Each page and cursor is checked against current project membership and the publication audience; listing never includes asset content.
- **Receipt recovery.** On reopening the same duty, the current authorized publisher can page through its original publication receipts alongside project references, even when it was not a recipient. A receipt includes only the asset, project, duty and original request IDs. It does not grant read access to the shared asset, disclose the private source or extend the original authority.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Publish a sanitized paragraph to a two-person project; the recipient sees the output but cannot read private notes or an unshared attachment. Reopen the author desk on a new session. |
| Failure or contention | Remove source disclosure rights before commit, retry a stale revision, and inspect from a third principal. Observe no unauthorized content or duplicate artifact. |
| Lost reply | Stop the worker after publication commits but before it returns. Reopen the same duty and Desk on a fresh run. The authorized author finds the original receipt without republishing; the new run does not gain access to material outside its audience. |

**Oracle:** Compare the actual bytes and access outcomes for author, recipient and outsider; verify stable receipt and original source ACL. Run [W1](../../../../acceptance/episodes.md#w1-personal-work).

## Operations and change review

Audit destination and audience changes without logging protected content. Withdraw access prospectively; do not claim to erase previously authorized copies.
