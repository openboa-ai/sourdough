# Project a read-only work timeline

Design draft · Verification **UNRUN** · Responsible component: Company work projection service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: founded company, current work state and [authorize-actor/check](../../../authority/authorize-actor/check/spec.md). A [decision](../../../work/discover-work/decide/spec.md) is projected when it exists; an initial due duty without a decision is a valid state, not an error or fabricated decision.

## Design decision and review

Project company, work, decision, allowed evidence, owner, next condition and as-of time via ACL-filtered on-demand indexed queries on `open_reader()` (`mode=ro`, `PRAGMA query_only=ON`) exposed through `/v1/projections/{name}` on the local UDS Control Plane. Preserve the `work-timeline-v1` content-hash `revision` contract for existing consumers while exposing `ETag` and monotonic `event_seq` on `work-timeline-v2` (supporting `If-None-Match` conditional reads). Optional profile links do not introduce mandatory quant fields.

The local Client uses the read-only service entry point `inspect-work`, specifying the company and the supported `work-timeline-v1` contract revision. The response reports that contract separately from the content revision and observation time. A request for an unsupported contract rejects before accessing the store. The local store-owner check remains in the service; the Client gets neither database access nor operational authority through this query.

An optional `risk_control` observation reports the effective trading risk revision, suspension state and last applied request reference, operation and time from the [risk control owner](../../../../profiles/quant/trading/board-control/apply-control/spec.md) (delegated through the `'trading'` domain plugin). When the trading domain is not installed, no trading risk observation is emitted; a controlled store with incomplete evidence returns unavailable, not unsuspended. A conflicting request never becomes the last applied receipt. Unsuspended does not imply a current grant or permission to submit an order. Earlier compatible Clients may ignore the observation.

**Concern for the responsible component:** Authorize every read at current access scope. Do not expose restricted evidence through titles, aggregates or stale cached projections.

## Functional and quality requirements

- **FR.** Project the as-of work timeline, accountable actor and next condition while suppressing unauthorized evidence.
- **NFR.** Missing evidence remains missing; a restricted reference is not a channel for revealing private content. No reads can emit an operational command.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | With no client and no investment profile, query the timeline for a document-review duty and compare the owner and next condition against independent work receipts. |
| Failure or contention | Delay the projection and withdraw access to an attachment. Show as-of time and limited access; refreshing does not wake the worker or alter work. |

## Operations and change review

**Oracle:** Compare each field to work history and command-origin logs. Rebuild a disposable projection without modifying the source records.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
