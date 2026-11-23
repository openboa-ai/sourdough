# Recover a company from a verified checkpoint

Design draft · Verification **UNRUN** · Responsible component: Company storage and recovery

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Capture policy, work, asset manifests, relevant journals and an event watermark. Restore into isolation, fence old senders and verify later events. The quant profile supplies venue reconciliation; it is not needed by a company with no trading duties.

Input contracts: [Supervisor](../../../work/resume-work/supervise/spec.md), company asset inventory and current custody records. A custody [transfer](../../../resources/company-assets/transfer/spec.md) is required only if a duty or asset actually changes custodian; restoring the same owner does not require a successor or an accepted handoff. Each installed profile supplies its own obligation reconcilers.

## Design decision and review

Restore into an isolated shadow-staged candidate instead of overwriting the last good copy in place. Because the live company store operates in SQLite `WAL` mode (`autocommit=True` + `BEGIN IMMEDIATE`), both checkpoint snapshot creation and shadow-staged restore MUST normalize the staged SQLite database to `PRAGMA journal_mode=DELETE` with **zero `-wal` or `-shm` sidecar files** before publish, after passing `PRAGMA quick_check`, `PRAGMA foreign_key_check` and `fsync`.

Verify checkpoint and restore cuts using a **logical watermark** (`company_id`, `schema_version`, `max_event_seq` and canonical domain digest) rather than raw SQLite file byte SHA-256, because WAL checkpointing and journal normalization can alter physical database bytes without changing logical company state. Bundle matching per-Agent Desk snapshots alongside SQLite `private_desk` revisions inside each checkpoint so restored Agents reopen consistent private files without restoring revoked mounts.

**Concern for the responsible component:** Pin checkpoint retention, recovery objectives and fencing method before the drill. Refuse to open or promote a checkpoint whose `schema_version` (`PRAGMA user_version`) exceeds the running binary's maximum supported schema version (fail-closed). If lost local authority or obligations cannot be reconstructed, remain isolated.

## Functional and quality requirements

- **FR.** Restore the same company, fence old senders, normalize the staged database to `PRAGMA journal_mode=DELETE` with zero `-wal`/`-shm` sidecars prior to publish, replay retained events and reconcile outstanding obligations. Release no reserve solely because the checkpoint predates an order.
- **FR.** Reopen assets and bundled per-Agent Desk snapshots under restored current grants without inventing a transfer. If a necessary successor cannot be verified, contain the affected obligation and retain its owner or explicit service recovery duty rather than transferring private access or blocking unrelated company work.
- **FR.** Verify pre- and post-restore equivalence via the logical watermark (`company_id`, `schema_version`, `max_event_seq`, canonical domain digest) and fail closed on an unsupported future `PRAGMA user_version`.
- **NFR.** Record checkpoint age, last recoverable event and restore-to-ready elapsed time against budgets declared before the drill. Zero double dispatch or double posting; missing lineage blocks new dependent risk.

## Acceptance and independent evidence

**Oracle:** Keep the pre-loss logical watermark (`company_id`, `schema_version`, `max_event_seq`, domain digest), inventory, post-checkpoint event IDs, venue history and expected writer generations outside the restore process. Compare the resulting book, bundled Desk snapshots and duty queue to those expectations.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Take a WAL-active checkpoint and restore into a shadow candidate before a shared-asset handoff. Verify zero `-wal`/`-shm` sidecars at publish, matching logical watermark (`company_id`, `schema_version`, `max_event_seq`, domain digest), matching bundled Desk snapshots, grants, custody and next duty. In the quant case additionally restore across a fill and verify position and fees. |
| Failure or contention | Withhold a journal segment, corrupt the checkpoint, leave a stray `-wal`/`-shm` sidecar, supply a checkpoint with a future `PRAGMA user_version`, or leave the old sender active. Do not promote the candidate; retain the surviving copy and a scoped incident. A venue balance alone cannot reconstruct a lost grant. |

## Operations and change review

Monitor age of the last verified checkpoint and last successful restore drill. Fall back to an intact copy without erasing the failed candidate or unresolved obligations.
