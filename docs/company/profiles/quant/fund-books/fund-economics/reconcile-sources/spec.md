# Reconcile non-fill economic sources and coverage

Design draft · Verification **UNRUN** · Responsible components: investment subledger for venue account movements; company accounting for operating actuals; quant performance for coverage and the common valuation cut. [Feature intent](../intent.md) · [Cost-inclusive statement](../close-period/spec.md)

## Boundary and requirements

Before close, pin company/account, reporting currency, period, inclusive boundaries, venue statement revision, expected source inventory, mark coverage and company usage/billing evidence. For the declared instrument and environment, classify signed funding, other venue cash movements, external flows, fees and corrections by stable source identity and currency. Do not infer settled funding from a forecast. The investment subledger ingests or flags non-fill account movements exactly once and reconciles its opening balance plus movements to the independent venue statement; the accounting service journals mapped events and model, data and infrastructure actuals exactly once. Performance joins their receipts at the same cut and records outstanding sources, responsible owner and next check.

**FR-1:** Deduplicate redelivery by source identity, reject changed amounts under the same identity, retain a linked correction and recompute affected statements. Separate external capital from profit; remove internal transfers at consolidation. Include expected billing periods even when no usage receipt has arrived.

**FR-2:** Issue complete coverage only when the declared source inventory, venue statement, valuation and cost receipts reconcile to the cut. Missing statement ranges, unresolved mapping, unknown costs or unavailable FX make the affected company-net result unavailable or incomplete, never zero. Preserve a complete trading-only series where its own sources are sufficient.

**NFR:** All comparisons use exact currency units and unrounded amounts; each source is posted once even across restart or delayed acknowledgments. Retain source revisions and superseded close receipts.

## Independent cases

| Case | Evidence and expected result |
| --- | --- |
| No trade, cost and funding | With independently supplied opening equity 1,000, no fills, venue funding −2 and company compute expense −3 paid outside the venue account, the investment book falls to 998 and the company-net close reports −5. Compare venue statement, source IDs, journal and independent worksheet. |
| Missing and late | Omit the expected compute receipt: company-net is incomplete, not −2 or zero cost. Deliver the receipt after close and redeliver funding after restart: publish a linked corrected revision with one funding posting and one expense. |
| Conflict and flow | Send a changed amount under an existing ID and an internal transfer: flag the conflict; do not book it twice or classify the transfer as an external contribution. An authorized correction retains both versions. |

This is a proposed contract and no product observation. Numerical policies and eligible source types must be pinned for the selected paper account before its prospective window.
