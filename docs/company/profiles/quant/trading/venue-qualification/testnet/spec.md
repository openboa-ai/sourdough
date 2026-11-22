# Qualify the selected testnet adapter

Design draft · Verification **UNRUN** · Responsible component: Venue integration

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

First pin the selected product, units, venue capabilities, testnet-only destination, finite budget, observer and approved credentials route. Map order, fill, account and time evidence to company identities.

Pin and attest the same core artifact and domain operation contracts used for paper, while binding a separately authorized non-production execution destination. Market-data source qualification is separate. A test environment may update in real time without matching the production price, book depth or fills; do not substitute it for a registered current-production-feed paper run without separately establishing the required data and simulation properties.

Input contracts: [Verified read-only profile](../observe/spec.md) · [Reconcile cancellation and late fills](../../cancel-order/cancel/spec.md) · [Produce a cost-inclusive economic statement](../../../fund-books/fund-economics/close-period/spec.md) · [Detect, contain and close an operating incident](../../../../../systems/operations/monitor-company/incident/spec.md).

## Design decision and review

Qualify the selected external adapter separately rather than claiming simulator behavior applies to another venue.

**Concern for the responsible component:** Obtain explicit testnet-only scope and limits before any submission. Unobserved expiry, rejection or partial-fill cases remain unqualified.

## Functional and quality requirements

- **FR.** Under separate authorization observe submit, query, fill or partial status, cancellation and restored reads on the exact tested profile. Retain missing cases as unverified.
- **NFR.** Simulator-only guarantees, including sealing an absent ID, cannot be assumed externally. An inconclusive lookup keeps dependent exposure blocked.

## Acceptance and independent evidence

**Oracle:** Use independent read-only venue history, balance and fill observations, not merely the submit client's response. Publish a per-case observed, unsupported or unrun disposition.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Match product and independent testnet observations for an authorized bounded order and its reconciled account result. Record the tested adapter and exact destination. |
| Failure or contention | At missing authorization, wrong destination or unsupported identity guarantees, refuse dispatch and mark qualification blocked. Synthetic success belongs to the first integration only. |
| Core continuity | Compare the actual core digest and normalized authorized request/receipt fields with the paper qualification; a separate simplified execution path does not satisfy shared-core conformance. Missing external permission or an unrun observation remains blocked. |
| Venue-originated effects | On isolated fixtures and, only where supported and separately authorized, an external test profile, verify account reconciliation for venue-generated or unattributed effects. Missing position or fill history remains unknown, not a fabricated flat state; record unsupported cases. |

## Operations and change review

Monitor adapter divergence and outstanding testnet effects, reconcile and retire credentials when qualification ends. Live activation needs a separate decision.

Record testnet authority and destination before any external effect. Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
