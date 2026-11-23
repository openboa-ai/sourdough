# Qualify and activate a company-owned change

Design draft · Verification **UNRUN** · Responsible component: Company operations

[Feature intent](../intent.md) · [Company assets](../../../resources/company-assets/intent.md) · [Current authorization](../../../authority/authorize-actor/check/spec.md) · [Strategy activation](../../../../profiles/quant/investments/strategy/activate/spec.md)

## Boundary and dependencies

Consume a company-owned immutable candidate, author and operating owner, intended scope, affected dependencies, effective baseline, rollback target, independent test evidence and current deployment grant. Assets own custody and revisions; the evaluator owns its verdict. Operations supervises activation and recovery; the applicable domain activation contract owns the single effective deployment record and its external obligations. For a strategy, that owner is the strategy activation service. This is a shared contract, not a second activation record or a required standalone deployment service.

## Initial company-only adapter

Use one company-owned evidence review tool to exercise the shared contract before deploying a stateful program. Its candidate is a versioned, bounded list of required evidence classes. The tool reports missing classes for an admitted work result; it cannot publish evidence, change the owning duty, or authorize a downstream action. The company owns the evaluator cases, the list vocabulary and the candidate format. Do not execute candidate code, import files from the proposing Agent's desk, or open an external connector to evaluate this tool. The Quant strategy adapter implements the same admission contract but retains its own evaluation, worker fencing and order obligations.

1. **Propose.** A current authorized duty publishes the immutable candidate as a company asset. Freeze the canonical candidate digest, adapter and format revisions, expected effective revision and rollback target. For the first tool activation use explicit absence for the previous tool revision, not an invented incumbent or prior verdict. Bind the contributor and operating owner from authority records, not the candidate payload. An identical proposal retry returns the original candidate reference; reuse with changed terms conflicts.
2. **Evaluate.** A company-controlled evaluator runs pinned, independent cases against that exact candidate and the sealed baseline. It records the evaluator, criteria and input revisions, results and verdict. Neither the proposal nor the activation request accepts a caller-supplied pass. A changed candidate, baseline or criteria invalidates the verdict for new activation.
3. **Activate.** In the owning domain's short local transaction, recheck the current deployment authorization, sealed verdict, effective revision, scope and compatible rollback target. Commit the effective revision and its activation receipt together. The receipt is the stable identity for delivery to the current work owner through collaboration; a notification retry must not replay the activation. Store digest and receipt references, not a mutable link into an Agent workspace. A call under a retired tool revision cannot accept new work; a pure inspection already completed under it remains attributed to that revision.
4. **Use or revert.** A separately authorized consuming duty invokes the current tool through its normal scoped tool binding. The result identifies the effective revision and input work-result revision. A revert is a new versioned activation of the prior eligible artifact under current authority, never a deletion of the superseded version or its results. Keep the current revision when the revert is stale or incompatible. For this synchronous read-only tool there is no external sender to replace; do not use its revert as evidence of writer fencing for a stateful deployment.

Keep the tool adapter, state transitions and independent evaluator inside the existing company service. No background worker, deployment framework, generic rule language or additional runtime is needed for this first path.

## Functional and quality requirements

- **FR-1:** Record a candidate and independent test verdict without allowing its author to modify the evaluator, grant or criteria for that run. Recheck authorization, compatibility, destination, cost and outstanding effects immediately before activation. A candidate cannot self-grant.
- **FR-2:** Have the owning activation service record one auditable receipt, scoped execution identity and effective revision. Operations references that receipt for supervision rather than writing a competing effective revision. Duplicate activation requests resolve to the original receipt; changes to inputs conflict. No routine board command or Client connection is required within standing authority.
- **FR-3:** Observe health and domain-specific acceptance, notify the current duty owner via collaboration, and suspend affected new effects on failure. Roll back only when state and dependencies permit it; otherwise preserve the old and new version identities and assign a recovery duty. Rollback never erases external effects.
- **FR-4:** Functional safety does not establish economic improvement or recursive learning. Investment adoption refers to its own registered comparison; learning adoption refers to independent transfer evaluation. A first bounded paper version needs no invented incumbent.
- **NFR:** Fence the retired writer before allowing its replacement to create effects. Preserve unresolved original effects and restrict only the affected scope. Version and evidence records survive restart.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Eligible | An Agent proposes a scoped company tool; an independent evaluator verifies it, the company activates it without Client, and another authorized Agent uses the recorded version. |
| Rejected | Inject a failing test, revoked grant and author-modified verdict in separate attempts; each prevents activation without changing the effective version. |
| Interrupted | Kill during transition with an outstanding effect. On restart, at most one writer can issue new effects, the original effect remains reconcilable and rollback occurs only if safe. |
| Initial adapter | With Client closed, observe a proposing Agent publish a new review rule, an independently recorded verdict, and another authorized Agent use the effective rule on a result missing one required evidence class. Reopen the service, revert under the current revision, and verify both original results and the restored rule independently. Also try a changed candidate, stale revision and revoked grant; none applies a change. |
| Initial adapter reply loss | Stop the activation caller after the commit but before receipt delivery. Reopen, retry the exact request and observe one effective transition and one notification. A different request against the old effective revision refuses. |

**Oracle:** Compare immutable artifact and evaluator revisions, effective grant, operating receipts, independent process observations and destination history. The proposer’s message and process readiness are not verdicts. All cases are unrun.
