# Reconcile venue-originated or unattributed account effects

Design draft · Verification **UNRUN** · Owner: trading reconciliation coordinates; investment subledger posts confirmed account effects; authority and operations own containment. [Feature intent](../intent.md) · [Venue qualification](../../venue-qualification/intent.md) · [Fill posting](../../../fund-books/book-fills/post/spec.md)

## Boundary and requirements

The selected venue may report an account effect not initiated by a current company decision or program: for example an exchange-generated forced close, settlement, correction, or externally created order. Record original venue/account/order/fill identities, product, source, time and the available origin classification. This observation is not itself an Agent request, a new authorization grant or permission to send another order.

**FR-1:** Compare bounded venue account history, open orders and positions with the known company actions and existing reconciled cursor. An unfamiliar, unclassifiable or conflicting effect creates a durable incident and next lookup. Block affected new risk while source coverage or effective exposure is unknown; preserve cancel, lookup and separately authorized risk-reduction paths. A missing report is unknown, not an empty account.

**FR-2:** On independently authenticated evidence, pass each uniquely identified fill and fee to the account-scoped investment subledger. Preserve observed external provenance and related original orders if known; do not invent a research finding, Agent authorship or an approved request. Map cash and funding to company accounting under the existing source-identity rules. Accepting an observed fact is not approval of the action that caused it.

**FR-3:** Reconcile the resulting position and complete account event coverage before clearing the affected containment. If venue history cannot account for the observed position, retain an unresolved difference and accountable follow-up. Never auto-create an offsetting trade, discard the event or synthesize a fill to make the books look complete.

**NFR:** Duplicate stream and history deliveries, corrections, restart and late fees cannot double post or overwrite the original evidence. Tests use isolated fixtures or a separately authorized external test profile; no real-money order is authorized here.

## Independent cases

| Case | Expected observation |
| --- | --- |
| Forced effect | With an open paper position, inject a separately witnessed venue-generated close and fee without an Agent command. The subledger records it once with external provenance, updates exposure and schedules follow-up; no new order and no fictional Agent decision appear. |
| Unknown or missing history | Venue reports a changed position but omits the related fill. Keep the difference open, block dependent new risk, and retain the original receipt and next lookup. Later independently identified evidence reconciles once; missing history is not zero exposure. |
| Unattributed write and replay | Observe an order not in the company action log. Preserve the fact, contain affected risk and open an incident; duplicate delivery and restart do not create another order or posting. Do not characterize the origin as authorized until supported. |

Run this separately from founder and hired-Agent paper crash acceptance. A candidate framework's inferred or synthetic fill is not equivalent to independent venue evidence.
