# Recover a company from a verified checkpoint

Design draft · Verification **UNRUN** · Responsible component: Company storage and recovery

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Capture policy, work, asset manifests, relevant journals and an event watermark. Restore into isolation, fence old senders and verify later events. The quant profile supplies venue reconciliation; it is not needed by a company with no trading duties.

Input contracts: [Supervisor](../../../work/resume-work/supervise/spec.md), company asset inventory and current custody records. A custody [transfer](../../../resources/company-assets/transfer/spec.md) is required only if a duty or asset actually changes custodian; restoring the same owner does not require a successor or an accepted handoff. Each installed profile supplies its own obligation reconcilers.

## Design decision and review

Restore into an isolated shadow-staged candidate instead of overwriting the last good copy in place. Because the live company store operates in SQLite `WAL` mode (`autocommit=True` + `BEGIN IMMEDIATE`), both checkpoint snapshot creation and shadow-staged restore MUST stage via SQLite online backup, normalize the staged database to `PRAGMA journal_mode=DELETE`, verify `PRAGMA quick_check` and `PRAGMA foreign_key_check`, close the writer and confirm **zero `-wal` or `-shm` sidecar files**, and `fsync` before atomic publish.

Verify checkpoint and restore cuts using a **logical watermark** (`company_id`, `schema_version`, `max_event_seq` and canonical domain digest) rather than raw SQLite file byte SHA-256, because WAL checkpointing and journal normalization can alter physical database bytes without changing logical company state. Bundle matching per-Agent Desk snapshots alongside SQLite `private_desk` revisions inside each checkpoint so restored Agents reopen consistent private files without restoring revoked mounts.

The local checkpoint command requires the original company writer lease, publishes an immutable named archive without overwriting an existing destination, and records a `company.checkpoint_cut` audit event at capture. Inspection copies the verified member into a private temporary scope and validates size, digest, schema integrity, and logical watermark without promoting the archive.

For exact-cut core restore, hold the original company writer lease, verify the original matches the checkpoint cut and has no installed paper domain, stage the verified copy and stamp its restore receipt, and commit durable store retirement (`company_store_retired` with `replacement_sha256` and `schema_revision = -1`) before atomic publication. Interrupted publication after retirement resumes only for the exact original request revision and releases only its own staging alias. For managed paper restore, hold the original venue sender lock (`paper_sender_lease`) across `check_original` (pre-flight and inside the retirement transaction), bind `external_scope` into the restore request revision, and reconcile settled venue fills and balances into the shadow candidate without sending new orders.

**Concern for the responsible component:** Pin checkpoint retention, recovery objectives and fencing method before the drill. Refuse to open or promote any checkpoint whose `schema_version` (`PRAGMA user_version`) is not canonical `v1` (`user_version != 1`) or whose live schema/domain digest does not match the bundled canonical `v1` packs (fail-closed). If lost local authority or obligations cannot be reconstructed, remain isolated.

## Functional and quality requirements

- **FR.** Restore the same company, fence old senders, normalize the staged database to `PRAGMA journal_mode=DELETE` with zero `-wal`/`-shm` sidecars prior to publish, replay retained events and reconcile outstanding obligations. Release no reserve solely because the checkpoint predates an order.
- **FR.** Reopen assets and bundled per-Agent Desk snapshots under restored current grants without inventing a transfer. If a necessary successor cannot be verified, contain the affected obligation and retain its owner or explicit service recovery duty rather than transferring private access or blocking unrelated company work.
- **FR.** Verify pre- and post-restore equivalence via the logical watermark (`company_id`, `schema_version = 1`, `max_event_seq`, canonical domain digest) and fail closed on any `PRAGMA user_version != 1` or untrusted trigger/view/schema alteration.
- **NFR.** Record checkpoint age, last recoverable event and restore-to-ready elapsed time against budgets declared before the drill. Zero double dispatch or double posting; missing lineage blocks new dependent risk.

## Acceptance and independent evidence

**Oracle:** Keep the pre-loss logical watermark (`company_id`, `schema_version = 1`, `max_event_seq`, domain digest), inventory, post-checkpoint event IDs, venue history and expected writer generations outside the restore process. Compare the resulting book, bundled Desk snapshots and duty queue to those expectations.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Take a WAL-active checkpoint and restore into a shadow candidate before a shared-asset handoff. Verify zero `-wal`/`-shm` sidecars at publish, matching logical watermark (`company_id`, `schema_version = 1`, `max_event_seq`, domain digest), matching bundled Desk snapshots, grants, custody and next duty. In the quant case additionally restore across a fill and verify position and fees. |
| Failure or contention | Withhold a journal segment, corrupt the checkpoint, leave a stray `-wal`/`-shm` sidecar, supply a checkpoint with `PRAGMA user_version != 1` or tampered schema objects, or leave the old sender active. Do not promote the candidate; retain the surviving copy and a scoped incident. A venue balance alone cannot reconstruct a lost grant. |
| Local checkpoint boundary | Hold the original company writer lease and verify checkpoint creation is refused. After release, acquire one snapshot, retain an independent copy of the source history, and verify a scoped read-only reopen. Later original changes must not modify the archive; corruption, wrong company and accidental overwrite must be refused. |
| Exact-cut core replacement | Start a separate caller holding the original lease. Refuse replacement while it is active. After it exits, interrupt the first restore after durable retirement, verify the old process cannot regain write access, then retry the exact request to publish one replacement. Reopen the same Agent, grant and due work under the new writer. Later source events or trading obligations refuse this path without retiring the source. |
| Exact-cut paper replacement | Checkpoint an admitted paper command. Settle the original fill and balance only at the independent venue after the checkpoint. Refuse while its sender is active or its outcome unresolved. On a valid attempt, restore once, book the original fill and leave one next duty under the same Agent. Reopen the independent journal and compare its command history before and after; restore must not send. |

## Operations and change review

Monitor age of the last verified checkpoint and last successful restore drill. Fall back to an intact copy without erasing the failed candidate or unresolved obligations.
