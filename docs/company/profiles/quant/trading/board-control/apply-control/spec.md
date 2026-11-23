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

- **Authority.** The Client sends an explicit `suspend_new_risk` or `resume_new_risk` request over the authorized local service boundary. The service derives the principal from its authenticated session and rechecks the existing board control grant for this company. A principal string, ownership of the read-only projection, file access or a running Client does not confer a command grant. When the service has no authorized board binding, queries still work where authorized and controls stay unavailable. This unit creates no grant.
- **Command and receipt.** Use the company, command-contract revision, request ID, operation, expected charter and control revisions, and reason. The authenticated principal is part of the receipt lineage, not caller-controlled input. An exact, currently authorized lookup or retry returns the original disposition without another transition; reusing the ID with different terms conflicts. A stale new request cannot change effective state. An unauthenticated request has no company write. A lost reply is unknown until the original receipt is read.
- **Serialization.** Quant owns one effective company-level control for the managed paper profile, not one Client toggle or per-destination copy. Apply it through the current company writer, using the same transaction ordering as the new risk check and reservation. The control receipt and effective revision commit together. Check the effective revision inside the existing reservation transaction after resolving historical requests but before inserting a new order. Thus a reservation either precedes the suspension or is denied after it, regardless of request arrival clocks. Switching an account binding, restarting a worker or closing the Client cannot clear suspension.
- **Continuing obligations.** Every new positive buy in this initial cash-spot profile is risk-increasing. A new Agent or program request, or a persisted decision that has not reserved exposure, cannot acquire a reservation while suspended. Existing reservations preserve their original identities and remain available for authorized dispatch and reconciliation; an already admitted order is not erased, silently resubmitted or presumed cancelled. Suspension does not stop authorized lookups, bookings, terminal release or independent research. There is no generic risk-reduction exception until its own admission contract exists.
- **Resume.** Explicit resume requires the current control and charter revisions, authorized scope, and a fresh, independently corroborated view of the original bound account and outstanding effects. Obtain external observations before the local write transaction, then verify their account, binding, freshness and unchanged local obligation lineage when applying. Unknown effects, stale evidence or a changed binding leaves suspension in force. No successful result restores a retired writer or widens the mandate.
- **Compatibility.** Before accepting the first control, migrate from the prior writable schema under the existing writer exclusion and verify that a prior product build refuses to write the resulting schema. A restart or code revert must not allow a worker that omits the risk check to use a controlled store. Keep projections readable where compatible and preserve the last effective receipt.

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
