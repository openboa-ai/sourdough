# Post balanced entries and linked reversals

Design draft · Verification **UNRUN** · Responsible component: Company accounting

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [found-company/bootstrap](../../../organization/found-company/bootstrap/spec.md) · [authorize-actor/check](../../../authority/authorize-actor/check/spec.md).

## Design decision and review

Accept company, source event, evidence, date, currency, account mapping revision and lines in fixed minor units. Balance each currency and commit all lines under a unique source mapping. Correct by linked reversal and replacement.

**Concern for the responsible component:** The company owns its chart and posting rules. A fill subledger proves quantities and fees; it is not a second general ledger.

## Functional and quality requirements

- **FR.** Post all lines for a balanced source or none. Correct an error with separately linked entries under an authorized event.
- **NFR.** Identical replay returns the same receipt; conflicting amounts, unsupported conversion or unbalanced lines post nothing. Estimated spend is not an expense.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | For a paid 300-cent runtime cost debit compute expense 300 and credit cash 300. Replay the receipt: exactly one balanced journal remains. |
| Failure or contention | Try a 300/299 entry and a duplicate source with changed terms. Both fail distinctly; an authorized reversal preserves the original lines. |

## Operations and change review

**Oracle:** Recompute trial balance from immutable lines and independent source receipts. Hold unmatched items rather than plugging differences.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
