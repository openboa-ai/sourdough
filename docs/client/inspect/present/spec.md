# Present a work projection

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume a read model and its authorization, coverage and revision metadata. Bind fields to accessible client views.

Input contracts: [Authorized work timeline](../../../company/systems/operations/work-timeline/read/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Consume projections and change notifications over the daemon's versioned local Unix domain socket Control Plane (`GET /v1/meta`, `GET /v1/projections/{name}`, `GET /v1/events`) served via ACL-filtered on-demand `open_reader()` indexed queries (preserving `work-timeline-v1` content-hash `revision` while adding `ETag` and `event_seq` on `work-timeline-v2`). Never read SQLite directly or spawn one-shot CLI processes per click. The Zig Client's internal transport adapter is gated on a pre-Stream-B2 Native SDK 0.11.1 capability probe.

Separate **Data State** (last verified snapshot stamped with `company_id`, `store_key`, `revision`, `as_of_ns`) from **Link State** (`disconnected | connecting | connected | incompatible`). Routine refreshes and transient disconnects MUST NOT blank-wipe the last verified Data State; instead display an explicit Stale / Disconnected banner with elapsed age while locking all mutating controls fail-closed. Clear cached Data State only on `company_id`/`store_key` change, sign-out, principal change or `401`/`403` access denial. Display the observation time returned by the service; a missing investment projection is not a zero balance. Keep local selection, sorting and intended control changes separate from observed company facts. Closing the client stops only its presentation.

**Concern for the responsible component:** A cached response is not proof of present access. Clear private state on sign-out, principal change, `company_id`/`store_key` switch or `401`/`403` denial; never show sensitive cached contents after access loss.

## Functional and quality requirements

- **FR.** Render owner, next duty and evidence gaps with the server’s as-of time and Link State. Preserve the last verified snapshot across routine refreshes and transient disconnects with a visible stale indicator while disabling mutations. An absent business profile is normal.
- **NFR.** Navigation, filtering, reopening and reconnecting issue zero operational commands. Present status in text and support keyboard navigation.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Run a core-only company while the client is closed; reopen and compare its timeline with the authorized projection and independent evidence. |
| Transient disconnect | Drop the local UDS connection after a verified load. Confirm the UI retains the last verified snapshot with an explicit Stale / Disconnected banner and locked mutating controls (no blank-wipe), then recovers cleanly on reconnect without issuing operational commands. |
| Failure or contention | Delay a response, invalidate an attachment grant and switch principal or receive `401`/`403`. Clear prior private data immediately; do not show an old record as current or leak content from the previous principal. |

**Oracle:** Capture the actual screen, keyboard path, query revision and service command log.

## Operations and change review

Record the client build and supported contract revisions. A failed client can be rolled back independently; see the [client build contract](../../build.md).
