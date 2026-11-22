# Present investment evidence

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Load only for the enabled quant profile. Render lineage and reported values; the panel has no venue credentials or financial writer.

Input contracts: [Base presentation](../present/spec.md) · [Quant trace projection](../../../company/profiles/quant/fund-books/decision-trace/project/spec.md) · [Return projection](../../../company/profiles/quant/fund-books/fund-economics/projection/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Consume versioned profiles, not a finance SDK or direct store connection.

**Concern for the responsible component:** Do not merge stale period values with a corrected cumulative revision. Unsupported profile or contract is a visible unavailable state, not zero return.

## Functional and quality requirements

- **FR.** Trace position to fill, original decision and next duty; display trading and company-net figures separately with units, coverage and as-of time.
- **NFR.** Drill-down respects the principal and scope of each evidence reference. Normal company operation continues if the panel is absent.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Open the client only after the next shift for G5. For [P1](../../../acceptance/episodes.md#p1-total-return), capture the running screen and compare both series with the independent worksheet. |
| Failure or contention | Disconnect and change the period. Do not label cached figures as current, issue a venue refresh or calculate missing values in the client. |

**Oracle:** Compare the rendered values and visible missing states to sealed query replies and the independent operational and finance records.

## Operations and change review

Monitor projection revision mismatch separately from venue and book health. Roll back presentation, not financial state.
