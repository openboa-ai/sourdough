# Admit paper orders in later unattended duties

Review draft · Verification **NOT_RUN** · Owners: Work and Quant admission

[Trading intent](../intent.md) · [Reservation](../reserve/spec.md) · [Normal continuation](../../../../../systems/work/discover-work/continue-after-result/spec.md)

## Boundary

The first company start can already produce a first fill and a separate position review. Do not enable trading in that review tool. Add a distinct, policy-eligible paper trading duty. The company owns the timer, scope and risk gate; the Agent reads the current position and available evidence and chooses whether to research, hold or propose an order. If its proposal is accepted, use the existing account reservation, destination and books, not a second order path. There is no approval prompt per duty or order.

The normal evidence uses one local synthetic paper account and its declared clock and observations. It grants no live order path, production data requirement, new subscription or NautilusTrader compatibility claim. Research-backed execution and post-fill recovery keep the separate [founder episode](../../../../../../acceptance/first-shift/spec.md).

## Design decisions

- Use one durable claim authority across the opening and later duties. Consolidate existing first-duty and follow-up run identities without changing founding, decision, order or fill IDs. Migrate rooted links before accepting new effects; validate foreign keys and historical receipt replay. Retain earlier run generations for audit without granting them new actions. A duty may carry review, research or trading scope; a review claim is never a paper order grant.
- The scheduler consumes a durable eligible occurrence, rechecks same Agent binding and mandate, and supplies the appropriate tool set. A same-profile session restart preserves the principal. Authority to wake or inspect is not dispatch authority. Bound observation time and the number of Agent turns; reaching either bound stops new launches but preserves scheduled work and outstanding effects.
- Persist the chosen action, evidence revision, follow-up condition and stable decision identity before dispatch. Admission is serialized per account, counts existing position and outstanding reserved exposure, and preserves the original submission identity across acknowledgment loss or a replacement claimant. An identical retry returns the existing receipt; a changed action, stale claim, unknown destination result, or changed evidence cannot authorize a replacement send.
- Terminal acknowledgement, actual book state and run completion are separate. A hold or request for research still supplies a reason and future responsibility. An unknown outcome has an explicit reconciliation owner; timing alone cannot unlock a dependent risk decision. Evaluate later reviews against authorized book observations, not an old provider transcript.
- After admission, a destination reply timeout carries the original order identity back to the company. Opening and later duties use the same lookup path without relaunching the trading Agent or resending the order. The active worker uses its remaining observation budget to query the original order. If still unknown, return the pending reconciliation and retain the reservation; only independently observed terminal execution and position evidence may settle the duty.

## Acceptance

| Requirement | Independent observation |
| --- | --- |
| FR-1 · Unattended recurrence | Start the installed company once. Let the Clock/Work boundary make another paper trading duty eligible. Observe a distinct work/run and a fresh real Agent decision referring to current evidence, followed by the next persisted duty. An operator reopening a command at each due time does not pass. |
| FR-2 · Repeat paper execution | Observe two decisions under their own trading duties and two separately admitted and booked orders after one activation. A supported hold is valid Agent judgment but is not a second trade. Either side and amount must follow the sealed fixture and mandate; the driver supplies no recommendation or scripted Agent response in the actual-runtime trial. Verify venue, books, scope and action-to-order links independently. |
| NFR-1 · One route and one unit of effect | Replay a wake, action and fill; revoke the grant while work is due; attempt over-limit and review-scope orders. Check no additional destination write, book application or next wake is produced by a replay or denial. |
| NFR-2 · Compatibility and containment | Upgrade populated previous stores and compare stable historical rows and identifiers. Intentionally exhaust the run budget with another duty due and introduce an unknown result. Inspect the preserved duty and reservation without a new Agent launch or resend. |
| NFR-3 · Lost destination reply | Lose the reply after dispatch and withhold execution evidence through the first lookup. Reveal it on a bounded retry during the same activation. An independent journal shows one submission; the company posts one result and continues the same Agent. If evidence never arrives, the activation reports an unresolved order and retains its reservation without a dependent duty. |

Use deterministic tests for denial, contention and bounded timing, plus a separate actual-runtime observation on the final candidate. Keep the already-qualified first two-duty observation and founder recovery results distinct. Roll back only before any new effect under the new schema; otherwise preserve books and repair forward.
