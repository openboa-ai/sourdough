# Provide an independently observable venue

Design draft · Verification **UNRUN** · Responsible component: Venue adapter and simulator

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Send a previously admitted identity and frozen terms over the ordinary adapter boundary. The venue owns a separate durable account, order and fill store plus a read-only test observer.

Input contracts: [Original authorized intent](../../admit-intent/reserve/spec.md) · [Connector invocation](../../../../../systems/connectors/connect-resource/invoke/spec.md).

## Design decision and review

Keep durable venue state outside the process under test rather than mocking a fill callback inside the company.

**Concern for the responsible component:** Witness the venue commit separately from company delivery. An in-process exception cannot demonstrate the specified crash cut.

## Functional and quality requirements

- **FR.** Report receipt, rejection, open quantity, individual fills and terminal status. Withhold responses at a declared fault cut while preserving the venue commit.
- **NFR.** The company cannot write venue audit records or read the expected answers. Conflicting reuse of an identity is rejected.
- **FR.** A prospective paper binding pins the feed revision, submission/observation timing, eligible liquidity and fill policy, including partial or absent fills, spread, slippage, fees and funding assumptions. A fill may use observations arriving after order submission but not observations unavailable at the simulated fill time; unavailable inputs produce an uncovered or unfilled outcome, not an invented price. Label fixed-price fault fixtures and exclude them from prospective economic evidence. Record any model assumptions and limitations; simulated fills are not actual market executions.

## Acceptance and independent evidence

**Oracle:** Use a read-only observer of the venue's own journal, outside the company process. Compare accepted identity, fill ID, quantity and time with the observer's cut marker.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Submit an authorized order, fill it and query history by original identity after restarting the company only. Observe one recorded submission and its fill. |
| Failure or contention | Return a venue rejection after company admission and contrast it with an internal denial. The first reaches the venue once; neither changes position. |
| Economic simulation | With pinned quotes and liquidity, independently reproduce a partial fill, fee and unfilled remainder. Withhold the required quote or expose a later quote: no hindsight fill; report the coverage gap. Replay the registered policy and compare the venue journal. |

## Operations and change review

Monitor unknown response counts and adapter errors. Preserve original identities and route uncertainty to reconciliation.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
