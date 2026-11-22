# Compare, adopt or revert a method

Design draft · Verification **UNRUN** · Responsible component: Evaluation service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Freeze case split, rubric, incumbent, candidate revision, permitted tools, budget and evaluator before blind comparison. Use fresh company state for each trial.

Input contracts: [Candidate memory](../distill/spec.md) · [Paired Agent trials](../../evaluate/run-trials/spec.md). Quant returns are optional domain evidence, never authority for promotion.

## Design decision and review

Keep evaluation and adoption as separate decisions instead of promoting whichever trial ended in profit.

**Concern for the responsible component:** Decide the promotion margin and cost budget before comparison. Reject regression on authority or bookkeeping regardless of the aggregate.

## Functional and quality requirements

- **FR.** Include adverse episodes and a justified-hold control. Record outcome, costs, grounding, authority attempts and next action by case. Adoption is an explicit company-authorized revision event.
- **NFR.** Future prices and grader labels stay outside Agent context. No aggregate score can waive a ledger or authority invariant.

## Acceptance and independent evidence

**Oracle:** Compare the evaluator's sealed trial ledger with the promotion receipt and the next Agent's effective method revision. A rejected candidate must never become active.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Compare both versions on the same held-out episodes; use the first integration spec's three-trial diagnostic profile and retain every attempt. |
| Failure or contention | A candidate looks profitable but attempts unauthorized trading. Record the judgment defect and keep the incumbent; exercise rollback after a later regression. |

## Operations and change review

Track versioned regression rate and unexplained grade disagreements. Revert the method, retaining the original case as a regression.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.

**Adoption authority and use.** A personal-method grant can adopt only for that Agent. Shared project or company adoption requires an explicit promotion grant for that audience; drafting and grading confer neither. Persist the presealed rubric, numerical margin, cost ceiling, safety invariants, incumbent/candidate digests, paired results, approval scope, effective revision and rollback target. With no threshold, budget coverage or applicable grant keep the incumbent. Check a distinct real shift’s context digest and tool behavior against the deployed revision; after regression repeat for the restored version. Run [L1](../../../../../acceptance/episodes.md#l1-learning), including denied promotion and actual rollback.

An adopted work method or profitable strategy is not itself proof of RSI. A change to the improvement procedure and its transfer to a new problem must satisfy the separate [recursive-learning trial](../../../../../acceptance/recursive-learning/spec.md); it neither gates the prospective return clock nor follows automatically from it.
