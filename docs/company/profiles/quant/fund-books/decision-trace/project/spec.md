# Project the investment decision trace

Design draft · Verification **UNRUN** · Responsible component: Quant evidence projection service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Given authorized company and episode references, return a versioned trace of status, amounts, units, evidence IDs, timestamps and gaps. Do not mutate source records or return privileged links to other principals.

Input contracts: [General board timeline](../../../../../systems/operations/work-timeline/read/spec.md) · [Agent decision](../../../../../systems/work/discover-work/decide/spec.md) · [Fill posting](../../book-fills/post/spec.md).

## Design decision and review

Use a read-only company projection with evidence links rather than building account state in the client.

**Concern for the responsible component:** A delayed projection may be behind the latest fill. Return its actual as-of revision, not a claim that exposure is current.

## Functional and quality requirements

- **FR.** Support current position → fill → original decision → follow-up navigation and work → order drill-down. Display hold, internal denial, venue rejection, open, partially filled, stale and unknown distinctly.
- **NFR.** Reads produce zero operational effects. Preserve distinct stale, unknown, missing and access-limited outcomes without leaking restricted data.

## Acceptance and independent evidence

**Oracle:** Compare identifiers, amounts, as-of times and gaps in the query result with an independently sealed trace and the command log.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | After the first integrated run, query the position with no client process. Join the same receipts as the independent observer. |
| Failure or contention | Remove a source link or delay a projection. Return a gap or last observation time; repeating the query issues zero trading commands. |

## Operations and change review

Monitor missing joins and projection lag. Restore the read model from source records without producing a financial entry.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
