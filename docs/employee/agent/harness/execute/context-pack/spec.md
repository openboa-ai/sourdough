# Prepare a bounded Agent context pack

Design draft · Verification **UNRUN** · Responsible component: Harness context service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

For the current claim build a versioned manifest: mandate, Agent, work/generation, trigger, open duties and receipts, authorized evidence refs/cuts, active method revisions, allowed tools, workspace handles, budget and omissions. Optional resources resolve through scoped ports; bootstrap context does not require a complete Agent desktop.

Input contracts: [authorize-actor/check](../../../../../company/systems/authority/authorize-actor/check/spec.md) · [resume-work/recover-shift](../../../../../company/systems/work/resume-work/recover-shift/spec.md). A first claim may have no prior decision; where a decision or effect already exists, consume its durable receipt via [discover-work/decide](../../../../../company/systems/work/discover-work/decide/spec.md) before authorizing dependent effects. The decision is an output of the Agent turn, not a prerequisite for starting the first turn.

## Design decision and review

A native transcript may accelerate a turn but never overrides authoritative company records. Build a fresh pack on replacement; reconstruct durable decisions before considering further work.

The durable Desk and ephemeral run are separate. The Desk supplies company-owned, Agent-private drafts and references. A context pack may select company-approved instruction, skill and plug-in revisions and authorized memory; it does not import the developer's home, user configuration or credentials. Treat notes and ordinary retrieved content as data, not operating instructions. Installing or selecting an extension does not grant tools, secret access or permission to publish from a private Desk.

For the first company-only consumer, the company work service supplies the instruction and an explicit manifest of named tools, argument schemas and the terminal receipt. The Harness validates the current claim, binds the manifest to the original Agent's Desk, and rejects stale or unlisted calls before forwarding them. The runtime adapter translates that manifest without knowing the business workflow. Existing role-specific routes remain compatibility paths until separately migrated; this slice does not load executable plug-ins.

**Concern for the responsible component:** Mark retrieved messages, notes and documents as data. Omit future outcomes, evaluation labels, secrets, inaccessible references and revoked instructions; do not persist hidden model reasoning.

## Functional and quality requirements

- **FR.** Bound the pack by the declared token/time limit. Reserve space first for mandate, current grants, unresolved effects, obligations and evidence IDs. Drop optional context with an explicit omission reason. If required facts cannot fit or be verified, block the dependent action.
- **NFR.** Return manifest digest and effective method revision for audit without exposing private content. Never broaden visibility to make handover or compaction succeed.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Rehydrate a new session of the original pinned runtime on the same Agent duty with no provider session. Compare required fields, granted references, omissions and active method with company records. |
| Failure or contention | Remove access after pack creation; the later tool read must still deny it. Oversize optional history cannot evict an open-effect receipt or grant check. |
| First claim | Assemble the founder's first context from its founding receipt, current claim and grants with no previous decision. Start a real bounded turn and observe its new decision; missing prior decision alone must not block it. |
| System capability | Run a company-only duty through the manifest transport. Observe exactly the registered tool schemas in the process, one service-owned receipt, and denial of an invented tool or mismatched Desk/run. Substitute a different registered tool set without editing the adapter. Model judgment quality is a later evaluation. |

**Oracle:** Compare independently sealed ACL, duty and artifact inventories with what the runtime actually received. Exercise [W1](../../../../../acceptance/episodes.md#w1-personal-work) and [L1](../../../../../acceptance/episodes.md#l1-learning).

## Operations and change review

Track blocked hydration, stale manifests and omission counts. Disable a defective context builder without replaying external actions; see the [harness contract](../../protocol.md).
