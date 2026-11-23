# Apply and verify a versioned control

Design draft · Verification **UNRUN** · Responsible component: Quant risk and governance service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Take an authenticated board command, expected charter revision, reason and request ID. Commit scope change or suspension at the risk admission boundary. Return the actual effective receipt.

Input contracts: [Charter amendment](../../../../../systems/organization/found-company/amend/spec.md) · [Scoped authorization](../../../../../systems/authority/authorize-actor/check/spec.md) · [Risk admission](../../admit-intent/reserve/spec.md).

## Design decision and review

Apply a versioned service command instead of treating the UI toggle as evidence that the risk gate changed.

**Concern for the responsible component:** Distinguish last observed, pending and applied; verify the order of an admission racing suspension using server receipts, not client timestamps.

### First managed-paper control

- **Authority.** The Client sends an explicit `suspend_new_risk` or `resume_new_risk` request (`risk.suspend` / `risk.resume`) over the authenticated local Unix domain socket Control Plane (macOS path under 104 bytes in a `0o700` directory, `0o600` socket, bound only under `WriterLease`). On every connection, the service verifies kernel peer credentials (`LOCAL_PEERCRED`/`LOCAL_PEERPID`) combined with an explicit company-enrolled `board_binding` and scoped [role assignments](../../../../../systems/authority/authorize-actor/manage-roles/spec.md) (`board_viewer` vs `board_control`; fail-closed when unbound). A principal string, ownership of the read-only projection, file access, same-UID socket access alone, or a running Client does not confer a command grant. Unbound peers fail closed; when an enrolled principal holds `board_viewer` without `board_control`, authorized read-only projections remain available and control commands fail closed. This unit creates no grant.
- **Command and receipt.** Use the company, command-contract revision, `Idempotency-Key` (`command_id`), operation, typed multi-field `preconditions_json` (`charter_revision` and `expected_control_revision`), and reason. The authenticated principal and peer PID are part of the `command_receipt` lineage, not caller-controlled input. Persist `conflicting_revision` denials in `command_receipt` alongside applied and denied outcomes. Scope `GET /v1/commands/{command_id}` receipt lookups strictly to the authenticated `(principal, operation)`. An exact, currently authorized lookup or retry returns the original disposition without another transition; reusing the ID with different terms conflicts. A stale new request cannot change effective state. An unauthenticated request has no company write. A lost reply is unknown until the original receipt is read.
- **Serialization.** Quant owns one effective company-level control for the managed paper profile, not one Client toggle or per-destination copy. Apply it through the current company writer, using the same `BEGIN IMMEDIATE` transaction ordering as the new risk check and reservation. The `command_receipt`, `audit_event` and effective revision commit together. Check the effective revision inside the existing reservation transaction after resolving historical requests but before inserting a new order. Thus a reservation either precedes the suspension or is denied after it, regardless of request arrival clocks. Switching an account binding, restarting a worker or closing the Client cannot clear suspension.
- **Continuing obligations.** Every new positive buy in this initial cash-spot profile is risk-increasing. A new Agent or program request, or a persisted decision that has not reserved exposure, cannot acquire a reservation while suspended. Existing reservations preserve their original identities and remain available for authorized dispatch and reconciliation; an already admitted order is not erased, silently resubmitted or presumed cancelled. Suspension does not stop authorized lookups, bookings, terminal release or independent research. There is no generic risk-reduction exception until its own admission contract exists.
- **Resume (two-phase boundary).** Explicit `risk.resume` requires the current `charter_revision` and `expected_control_revision` in `preconditions_json`, authorized `board_control` scope, and a fresh, independently corroborated view of the original bound account and outstanding effects. Execute in two distinct phases: (1) read current local obligation lineage and obtain external venue cut observations strictly **outside** any SQLite write transaction; (2) inside `BEGIN IMMEDIATE`, re-verify the account, binding, freshness, unchanged local obligation lineage and revision preconditions before committing the resume transition. Unknown effects, stale evidence or a changed binding leaves suspension in force. No successful result restores a retired writer or widens the mandate.
- **Compatibility.** Before accepting the first control, migrate from the prior writable schema under the existing writer exclusion and verify that a prior product build refuses to write the resulting schema (fail-closed on higher `PRAGMA user_version`). A restart or code revert must not allow a worker that omits the risk check to use a controlled store. Keep projections readable via `open_reader()` where compatible and preserve the last effective receipt.

## Functional and quality requirements

- **FR.** Return requested, applied, denied or conflicting-revision receipts. Resume requires current authority and fresh risk state; disconnecting the caller never rolls back an applied suspension.
- **NFR.** Once suspension takes effect there are zero newly admitted risk increases. In-flight orders remain visible and reconciliation continues.

## Acceptance and independent evidence

**Oracle:** Compare the effective revision and admission order with the authoritative command receipt and independent venue history; no client process is needed.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Suspend with a working order. Read the applied revision, observe another attempted increase denied, then inspect a late fill on the original order. |
| Failure or contention | After a newer suspension, submit a new resume request with a stale revision and separately retry an already applied resume. Reject the new request and return the original receipt for the exact retry. Neither changes the latest effective state or hides exposure. |
| Admission race | Pause a new request after destination preflight but before its reservation transaction; commit suspension first and verify zero new order or reservation. In the opposite ordering, preserve the existing reservation and reconcile the original fill without accepting another. |
| Authority and reply loss | Withhold the authorized reply after commit and reopen Client. Recover exactly one effective transition from the original ID. An unbound or revoked principal, unsupported contract revision or changed request cannot apply an effect. A supported service query still reports the effective revision without applying a command. |
| Compatibility and freshness | Reopen the suspended company with a previously supported writer, and reject its write attempt. Refuse resume using stale, mismatched or unresolved original-account evidence. Under verified current conditions, apply exactly one new revision and admit an independent eligible request under it. |

## Operations and change review

Track requested controls lacking receipts. Keep the last known effective control visible and retry reads before another change.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
