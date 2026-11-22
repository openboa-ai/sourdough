# Draft an evidence-backed memory correction

Design draft · Verification **UNRUN** · Responsible component: Learning service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Compare an Agent note with a decision, tool receipt and observed outcome. Save a candidate with source references, intended audience, uncertainty and superseded revision.

Input contracts: [Record a decision and its next condition](../../../../../company/systems/work/discover-work/decide/spec.md) · [Assign an isolated company workspace](../../../../../company/systems/resources/company-assets/assign/spec.md).

## Design decision and review

Publish an evidence-linked candidate instead of directly editing the Agent's active memory.

**Concern for the responsible component:** Recheck source access when retrieving a note; company ownership does not authorize every Agent to read the original work.

## Functional and quality requirements

- **FR.** Store personal and company knowledge under scoped access. A candidate stays separate from the active method until evaluated.
- **FR.** For an improvement-procedure candidate, retain the predecessor procedure, earlier attempted changes (including rejections), predicted versus observed effects, evaluation conditions and the proposed change in how the next candidate will be produced. Reference the company-owned record, not hidden model state.
- **NFR.** A summary cannot overwrite venue or ledger facts or transfer private workspace access.

## Acceptance and independent evidence

**Oracle:** Resolve each candidate statement back to source records under the intended reader's grant. Compare the live method revision before and after drafting; it must not change.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Correct a note that mistakes a work offer for an accepted duty using the actual work receipt. In the quant case correct a claimed full fill from the partial-fill evidence. Neither edits the live method. |
| Failure or contention | Remove the original receipt. Preserve a disputed claim for follow-up instead of adopting a correction as fact. |

## Operations and change review

Track disputed and unsupported candidates. Quarantine an incorrect active note by revision.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.

**Scope at drafting:** record intended audience (one Agent, project or company), allowed source access, proposed use and what would falsify the claim. A candidate from private material cannot be broadened by merely relabeling the audience; redact or withhold restricted content before wider evaluation.
