# Recover a company from a verified checkpoint

Design draft · Verification **UNRUN** · Responsible component: Company storage and recovery

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Capture policy, work, asset manifests, relevant journals and an event watermark. Restore into isolation, fence old senders and verify later events. The quant profile supplies venue reconciliation; it is not needed by a company with no trading duties.

Input contracts: [Supervisor](../../../work/resume-work/supervise/spec.md), company asset inventory and current custody records. A custody [transfer](../../../resources/company-assets/transfer/spec.md) is required only if a duty or asset actually changes custodian; restoring the same owner does not require a successor or an accepted handoff. Each installed profile supplies its own obligation reconcilers.

## Design decision and review

Restore into an isolated candidate instead of overwriting the last good copy in place.

The first local checkpoint command requires the original company writer lease. If an existing worker holds it, refuse to capture instead of copying a changing database. Take an SQLite snapshot, reopen that copy read-only and verify the company identity, supported schema and database integrity. Publish an immutable named archive without replacing an existing path. Store only the company database and a manifest identifying its company, schema, capture time, byte size and content digest. The archive itself is not a runnable company store or a copy of a venue journal.

Inspection copies the single expected database member into a private temporary scope, verifies size and digest against the manifest, then independently reopens it read-only. It does not establish event replay, fence an external sender, create an active store, or promote the archive. Those steps require separate known evidence and remain restore prerequisites.

For the initial core-company restore, the internal recovery operation admits a replacement only while it holds the original store lease, can verify the original against the checkpoint, and observes no installed paper binding or order history. A later source event refuses this exact-cut path; it must use independently reconciled replay instead. This operation has no direct Client or CLI control; it does not authorize a separate replacement when the original is missing or when an external sender must be reconciled.

Stage the verified copy on the replacement volume and stamp its restore receipt before retiring the original. Persist retirement on the original store before publishing the replacement. An active original lease prevents retirement. A retired original remains readable but cannot acquire a normal writer lease or open any standard write connection. If publication is interrupted after retirement, only the exact source, checkpoint and replacement request can resume publication; never reactivate the old writer. An existing replacement is accepted on retry only if its own durable receipt matches. These checks do not fence a separately running external venue; quantified restoration still requires its profile-specific sender proof.

Commit the retirement receipt and a reserved, unsupported schema marker in the same transaction. This also refuses startup by earlier product builds that only check their supported schema set. The retirement-aware reader obtains the original schema from the authenticated local store's receipt for inspection; the marker is not a migration of company records. Manual SQLite writes or independently copied files outside the managed service are not covered by this fence.

**Concern for the responsible component:** Pin checkpoint retention, recovery objectives and fencing method before the drill. If lost local authority or obligations cannot be reconstructed, remain isolated.

## Functional and quality requirements

- **FR.** Restore the same company, fence old senders, replay retained events and reconcile outstanding obligations. Release no reserve solely because the checkpoint predates an order.
- **FR.** Reopen assets under restored current grants without inventing a transfer. If a necessary successor cannot be verified, contain the affected obligation and retain its owner or explicit service recovery duty rather than transferring private access or blocking unrelated company work.
- **NFR.** Record checkpoint age, last recoverable event and restore-to-ready elapsed time against budgets declared before the drill. Zero double dispatch or double posting; missing lineage blocks new dependent risk.

## Acceptance and independent evidence

**Oracle:** Keep the pre-loss inventory, post-checkpoint event IDs, venue history and expected writer generations outside the restore process. Compare the resulting book and duty queue to those expectations.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Restore a checkpoint predating a shared-asset handoff. Compare company, grants, custody and next duty to independent receipts. In the quant case additionally restore across a fill and verify position and fees. |
| Failure or contention | Withhold a journal segment, corrupt the checkpoint, or leave the old sender active. Do not promote the candidate; retain the surviving copy and a scoped incident. A venue balance alone cannot reconstruct a lost grant. |
| Local checkpoint boundary | Hold the original company writer lease and verify checkpoint creation is refused. After release, acquire one snapshot, retain an independent copy of the source history, and verify a scoped read-only reopen. Later original changes must not modify the archive; corruption, wrong company and accidental overwrite must be refused. |
| Exact-cut core replacement | Start a separate caller holding the original lease. Refuse replacement while it is active. After it exits, interrupt the first restore after durable retirement, verify the old process cannot regain write access, then retry the exact request to publish one replacement. Reopen the same Agent, grant and due work under the new writer. Later source events or trading obligations refuse this path without retiring the source. |

## Operations and change review

Monitor age of the last verified checkpoint and last successful restore drill. Fall back to an intact copy without erasing the failed candidate or unresolved obligations.
