# Fence interrupted claims and redelivered wakes

Design draft · Verification **UNRUN** · Responsible component: Scheduler

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Restarting a session on the same bound Agent preserves identity; replacing its runtime/model binding creates a different Agent and requires explicit handoff. A dependent trading duty consumes reconciled domain facts rather than transcript estimates. An unresolved external effect remains with its domain reconciler even when work is reassigned.

Claim a durable work/event ID using a lease and generation. Bind mutations to that generation. Process relaunch belongs to the supervisor; the scheduler scans on startup and decides what is due.

Input contracts: [Persist the founding charter](../../../organization/found-company/bootstrap/spec.md) · [Product-owned relaunch](../supervise/spec.md).

## Design decision and review

Keep a renewable monotonic claim generation (`fencing_token` in `work_runs`) inside the unified `CompanyRuntime` instead of trusting a provider session ID after restart. Partial unique indexes on `work_runs` enforce at most one active `running` execution per `agent_id` (protecting Desk integrity) and per `work_item_id` (preventing duplicate concurrent work). Long waits transition a run to a zero-compute durable `waiting` state (`wait_until_ns` and/or `wait_event_key`) so execution slots are released without holding worker threads or processes.

**Concern for the responsible component:** Confirm every mutating tool and domain commit verifies `WHERE run_id = ? AND fencing_token = ? AND status = 'running'`, not only the scheduler's completion endpoint.

## Functional and quality requirements

- **FR.** Reclaim an expired or demonstrably dead claim by atomically incrementing `fencing_token` under `CompanyRuntime` startup recovery (after acquiring `WriterLease`, sweeping expired `work_runs` leases, fencing prior tokens and reconciling `in_flight` rows in `external_actions` by original `idempotency_key` before admitting new risk). Replay existing receipts before allowing further actions.
- **FR.** Release execution slots cleanly on durable waits (`status = 'waiting'`) and transition back to `queued` atomically when `now >= wait_until_ns` or a matching domain event commits, while enforcing anti-self-wake (an Agent's own message never wakes that Agent).
- **NFR.** Only the current `fencing_token` can complete or mutate a work item. Sequence timestamps separately for process exit, restart and work resumption.

## Acceptance and independent evidence

**Oracle:** Correlate duplicate input delivery and both claimant generations with a count of durable work mutations and the unaffected next wake.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Kill a claimant before model start with the supervisor already active. Without a test-driver restart, verify one recovered work ID and a distinct later work ID from a new condition. |
| Failure or contention | Redeliver the wake and a delayed completion from the old claimant. Neither can create a second mutation or cancel the new wake. |

For a next duty that needs the result of an outstanding effect, the due timer makes it eligible but does not satisfy its data dependency. Retain the due occurrence until the owning domain supplies its durable disposition and result revision; admit the dependent decision once, against that revision or later. Test both arrival orders and a redelivery. Other eligible work continues. Work handles opaque result references, not order or fill schemas.

## Operations and change review

Observe oldest due work and stale claims. Back off restarts on repeated failure while keeping unresolved work visible.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
