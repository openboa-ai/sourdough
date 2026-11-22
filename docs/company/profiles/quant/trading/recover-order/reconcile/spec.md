# Resolve accepted, sealed-absent and unknown outcomes

Design draft · Verification **UNRUN** · Responsible component: Reconciliation service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Operations restarts the company and fences stale senders; this service queries the independently surviving original source through its scoped connector and retains unknown outcomes. Investment books alone apply position and reservation transitions. Work resumes the same Agent on its original binding after the relevant recovered facts are available; Client is not in this path.

On restart load each unresolved submission and query original venue history. Distinguish witnessed receipt, definitive absence which fences delayed acceptance, and inconclusive lookup.

Input contracts: [Post fills and update outstanding exposure](../../../fund-books/book-fills/post/spec.md) · [Fence interrupted claims and redelivered wakes](../../../../../systems/work/resume-work/recover-shift/spec.md).

## Design decision and review

Query the original submission instead of replacing an order after an ambiguous timeout.

**Concern for the responsible component:** Require a documented exclusion of delayed acceptance before closing an absent order. Missing that guarantee, keep risk reserved even if a lookup is empty.

### First full-fill recovery slice

Start after writer acquisition and before a result-dependent turn can launch. Verify the bound destination instance and read pending reservations by original order ID. Ask the same independent venue for the original order and its fill evidence. Reuse the posting transaction for all financial writes; do not translate an empty reply into either absence or permission to call submit. Persist the original lookup duty and a finite retry deadline if evidence is unavailable. Resolve it automatically when conclusive evidence arrives. Unsupported release capabilities remain explicit; they do not qualify the sealed-absent or partial-fill cases.

The prospective test includes two delivery paths for the same fill: original-order history and account sync. They must normalize to the same identity. After both observations, independently compute position, fee and reservation from the venue's declared terms and signed execution evidence. Commit the original work disposition and its distinct saved follow-up once the resulting book revision is available. If the company cannot read a trustworthy fill or fee required by the test contract, report incomplete and keep the affected exposure contained.

## Functional and quality requirements

- **FR.** Pass confirmed fills to the investment subledger using the original venue/account/fill key. Only its posting transaction applies quantity, fee and fill-related reservation changes. Record its durable receipt (or recover it by the same key after response loss) before reporting this fill as reconciled to Work. Release any remaining reservation only against definitive terminal evidence; persist the next query and owner if unknown.
- **FR.** Submit definitive terminal or sealed-absent evidence to the same account-scoped subledger for a uniquely keyed, idempotent release of the confirmed unfilled remainder. Reconciliation records the returned receipt; it cannot directly write reservation state.
- **FR.** Terminal status alone is insufficient if the venue's cumulative executed quantity and complete fill history have not been reconciled. Apply all uniquely identified fills first; only then release the verified terminal unfilled remainder. When fill coverage or finality is unknown, keep the affected exposure conservatively reserved and the original lookup duty active. A sealed-absent release additionally requires proof that delayed acceptance is fenced.
- **NFR.** A timeout or empty lookup cannot authorize a resend. Preserve bounded retry and risk containment through another restart.

## Acceptance and independent evidence

**Oracle:** Match venue audit entries and send count to the company's original submission, posted fill IDs and outstanding reservations across both process IDs.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Kill the company after durable venue fill and before reply. Verify automatic restart, one posting and one venue submission; a second shift reads the recovered position. |
| Failure or contention | Delay the send, seal unused identity at simulator and then deliver it. It must reject the delayed send. Separately return unknown then confirmed and verify autonomous resolution. |
| Late fill after terminal | Observe a terminal cancellation with cumulative executed quantity three but only two identified fills available. Keep the missing quantity covered and query again. Deliver the third fill twice, then verify three uniquely posted units and a single release of only the confirmed unfilled remainder. Repeat with process death before and after release; no duplicate order or release. |

## Operations and change review

Measure exit-to-ready, ready-to-reconciled and age of unresolved exposure. Escalate lasting uncertainty as an exception; continue eligible work.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
