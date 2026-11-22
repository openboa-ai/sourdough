# Qualify and activate a company-owned change

Design draft · Verification **UNRUN** · Responsible component: Company operations

[Feature intent](../intent.md) · [Company assets](../../../resources/company-assets/intent.md) · [Current authorization](../../../authority/authorize-actor/check/spec.md) · [Strategy activation](../../../../profiles/quant/investments/strategy/activate/spec.md)

## Boundary and dependencies

Consume a company-owned immutable candidate, author and operating owner, intended scope, affected dependencies, effective baseline, rollback target, independent test evidence and current deployment grant. Assets own custody and revisions; the evaluator owns its verdict. Operations supervises activation and recovery; the applicable domain activation contract owns the single effective deployment record and its external obligations. For a strategy, that owner is the strategy activation service. This is a shared contract, not a second activation record or a required standalone deployment service.

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

**Oracle:** Compare immutable artifact and evaluator revisions, effective grant, operating receipts, independent process observations and destination history. The proposer’s message and process readiness are not verdicts. All cases are unrun.
