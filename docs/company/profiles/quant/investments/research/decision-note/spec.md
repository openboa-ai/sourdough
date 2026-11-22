# Publish an Agent-authored research finding

Design draft · Verification **UNRUN** · Responsible component: Quant research

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

An authenticated Agent writes an immutable finding: company, author, producing work/run generation, hypothesis, input snapshot and source cut, comparison or baseline, calculation, cost assumption, result, uncertainty and revision. Return finding ID plus content digest. This is a bounded analysis, not a full backtest or a prediction of profit.

Input contracts: [market-snapshot/snapshot](../../market-snapshot/snapshot/spec.md) · [current work claim](../../../../../systems/work/resume-work/recover-shift/spec.md) · [current actor authorization](../../../../../systems/authority/authorize-actor/check/spec.md). The Agent may record a research-question decision first, but the finding must exist before the separate trade or hold decision that cites it. [Work decisions](../../../../../systems/work/discover-work/decide/spec.md) are the subsequent consumer, not a required prior trade decision.

## Design decision and review

The base work service stores opaque evidence references. Quant validates domain contents and admission consumes the finding; the note cannot reserve exposure.

**Concern for the responsible component:** A retrospective ID attached to an existing order is not evidence of decision-time use. Do not give the Agent expected labels, fault timing or a prepared recommendation.

## Functional and quality requirements

- **FR.** The finding must predate the decision and use only available observations. The decision cites that revision and explains its actual implication, including cost or contrary evidence, for the choice to act or wait.
- **NFR.** Reject stale generation, foreign company, mismatched instrument, missing input or a post-cut observation. A later correction creates a new version; prior decisions retain the old one.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | An actual Agent picks a question and uses two timestamped observations to compare a measured change with declared fees; independently reproduce its values. A different question/comparison is valid if independently checkable. |
| Failure or contention | Preseed a polished note, swap in an unrelated result with the same instrument, or cite an observation arriving later. None qualifies the founder-authored research-to-order gate. A well-supported hold is valid Agent judgment. |
| First finding | Starting with a live claim and no prior trading decision, publish the Agent's finding and only then record the citing decision. No placeholder decision, future finding ID or driver-authored result is allowed. |

**Oracle:** Join authenticated author and ordinary tool receipt to source observations; recompute the stated comparison independently and verify that the order decision uses the same result, not just a matching ID.

## Operations and change review

Measure stale or ungrounded evidence and rejected links. If evidence disappears, preserve the original record, stop dependent decisions and keep the investigation assigned.
