# Compare, adopt or revert a method

Design draft · Verification **UNRUN** · Responsible component: Evaluation service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Freeze case split, rubric, incumbent, candidate revision, permitted tools, budget and evaluator before blind comparison. Use fresh company state for each trial.

Input contracts: [Candidate memory](../distill/spec.md) · [Paired Agent trials](../../evaluate/run-trials/spec.md). Quant returns are optional domain evidence, never authority for promotion.

## Design decision and review

Keep evaluation and adoption as separate decisions instead of promoting whichever trial ended in profit. Gate every candidate method delta (`ADD`, `PATCH`, `DEPRECATE`) through a **three-stage pre-commit gate** before activation, and enforce **lineage DAG quarantine rollback** whenever a defective active method is reverted so descendant methods distilled while that parent was active cannot silently remain live.

**Concern for the responsible component:** Decide the promotion margin and cost budget before comparison. Reject regression on authority or bookkeeping regardless of the aggregate.

## Functional and quality requirements

- **FR (3-stage pre-commit gate).** Before activating any candidate method delta, pass all three stages:
  1. **Stage A — Structural and trigger validity:** Deterministic schema/unit validation plus **negative trigger checks** proving the method does not misfire on unrelated duties.
  2. **Stage B — Behavioral harmlessness:** Authority, sandbox and ledger invariant checks confirming zero privilege escalation, zero sandbox escape and zero regression on core company rules.
  3. **Stage C — Reliability and marginal gain:** Repeated regression execution **`pass^k` (`k >= 3`, where all `k` runs must pass)** plus non-negative marginal gain on sealed holdout checks at matched or lower token and latency budgets.
- **FR.** Include adverse episodes and a justified-hold control. Record outcome, costs, grounding, authority attempts and next action by case. Adoption is an explicit company-authorized revision event scoped to `audience_scope` (`one Agent | project | company`).
- **FR (Lineage DAG quarantine rollback).** Rolling back a defective method traverses `parent_method_ids` and `active_snapshot_id` in the provenance DAG and automatically sets all descendant methods distilled while that parent was active to `quarantined`, blocking their use until independently re-qualified.
- **NFR.** Future prices and grader labels stay outside Agent context inside the physically separate evaluator SQLite store. No aggregate score can waive a ledger or authority invariant.

## Acceptance and independent evidence

**Oracle:** Compare the evaluator's sealed trial ledger with the promotion receipt, lineage DAG quarantine state and the next Agent's effective method revision. A rejected or quarantined candidate must never become active.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Compare both versions on the same held-out episodes through all three pre-commit stages (`pass^k` with `k >= 3` and negative trigger checks) and retain every attempt. |
| Failure or contention | A candidate looks profitable but attempts unauthorized trading or fails a negative trigger check. Record the judgment defect and keep the incumbent. On later regression, roll back parent method M and verify every descendant method in M's lineage DAG transitions to `quarantined` and stays inactive until re-qualified. |

## Operations and change review

Track versioned regression rate and unexplained grade disagreements. Revert the method, retaining the original case as a regression.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.

**Adoption authority and use.** A personal-method grant can adopt only for that Agent. Shared project or company adoption requires an explicit promotion grant for that audience; drafting and grading confer neither. Persist the presealed rubric, numerical margin, cost ceiling, safety invariants, incumbent/candidate digests, paired results, approval scope, effective revision and rollback target. With no threshold, budget coverage or applicable grant keep the incumbent. Check a distinct real shift’s context digest and tool behavior against the deployed revision; after regression repeat for the restored version. Run [L1](../../../../../acceptance/episodes.md#l1-learning), including denied promotion and actual rollback.

An adopted work method or profitable strategy is not itself proof of RSI. A change to the improvement procedure and its transfer to a new problem must satisfy the separate [recursive-learning trial](../../../../../acceptance/recursive-learning/spec.md); it neither gates the prospective return clock nor follows automatically from it.
