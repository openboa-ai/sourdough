# Serve a versioned performance projection

Design draft · Verification **UNRUN** · Responsible component: Quant board projection

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Take viewer grant, interval, reporting perimeter and revision. Return trading and company-net series, currency, period and cumulative returns, as-of time, external flows, P&L, coverage and only authorized evidence references.

Input contracts: [fund-economics/total-return](../total-return/spec.md) · [work-timeline/read](../../../../../systems/operations/work-timeline/read/spec.md).

## Design decision and review

Return the performance service’s values and status. A read is not a refresh command to the venue; a missing total is not zero.

**Concern for the responsible component:** Trading and company returns can disagree. Return explicit series identities, valuation policy, period and scope even when values are unavailable.

## Functional and quality requirements

- **FR.** Expose complete, partial, unavailable and superseded revisions and reason. An older view must not silently combine a corrected numerator with an old denominator.
- **NFR.** Opening, filtering and inspecting the view issue zero financial commands. A restricted cost source cannot disclose its protected payload.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | With the client stopped, compare both series, the coverage and the as-of revision with the independent P1 worksheet. |
| Failure or contention | Withhold a necessary valuation and then deliver a correction. Return unavailable rather than zero, then publish a new revision without overwriting the prior record. |

**Oracle:** Compare query results with the independent expected values, revision history and command counts; requesting the projection causes zero writes to financial records.

## Operations and change review

Track projection lag and missing joins. On disagreement show the stale revision or withhold the value, never recompute ad hoc.
