# Register and replay a quant experiment

Design draft · Verification **UNRUN** · Responsible component: Research service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Freeze a hypothesis, strategy revision, data cuts, benchmark, tuning window, holdout window, fill assumptions and cost model before evaluation.

Input contracts: [Publish immutable as-of observations](../../market-snapshot/snapshot/spec.md) · [Assign an isolated company workspace](../../../../../systems/resources/company-assets/assign/spec.md).

## Design decision and review

Register an episode split and baseline before replay rather than selecting a window after seeing its return.

**Concern for the responsible component:** Record excluded or missing observations and their timing. Exposure to a later correction invalidates the comparison rather than improving the score.

## Functional and quality requirements

- **FR.** Replay chronologically using observations available at each decision. Report sample size, exposure, return after costs, drawdown, baseline difference and exclusions.
- **NFR.** Keep failed trials and original windows. Revisions require a new experiment ID and fresh comparison with leakage checks.
- **FR.** Link related variants, including abandoned ones, under one research question. For a revision comparison, register the incumbent and candidate company artifacts, Agent authorship, decision cadence, intended difference, cost assumptions and stopping criteria. Both arms may execute Agent-written code. A revised candidate may not use an already observed period as new prospective evidence. Forward company economics is a [separate trial](../../../../../../acceptance/net-return/spec.md).

## Acceptance and independent evidence

**Oracle:** An independent replay on the pinned input manifest must recover the same eligible observations, costs and baseline comparison; retain the separate held-out result even if negative.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Run a candidate and a hold baseline on identical periods. Independently reproduce an example fill, fee and result; retain an adverse outcome. |
| Failure or contention | Insert a favorable correction published after the decision or expose a holdout label in Agent context. Exclude the observation or mark the evaluation invalid. |

## Operations and change review

Track missing coverage and reused holdouts. Prevent promotion from an invalid run and retain the failed record.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
