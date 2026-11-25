# Present a work projection

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume a read model and its authorization, coverage and revision metadata. Bind fields to accessible client views.

Input contracts: [Authorized work timeline](../../../company/systems/operations/work-timeline/read/spec.md) · [Client architecture](../../architecture.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Consume projections and change notifications over `CompanyDaemon`'s versioned local Unix domain socket Control Plane (`GET /v1/meta`, `GET /v1/projections/{name}`, `GET /v1/events`) served via ACL-filtered on-demand `open_reader()` indexed queries (preserving `work-timeline-v1` content-hash `revision` while adding `ETag`, `If-None-Match`, `X-Sourdough-Event-Seq`, `store_key`, and `event_seq` on `work-timeline-v2` and `[M3 Extension]` projections; see [Meta and authorized read projections](../../control-plane/projections/spec.md)). Never read SQLite directly or spawn one-shot CLI processes per click. The React 19 + `@openboa/yogurt` presentation layer accesses the UDS Control Plane through the authenticated `sourdough client-web` loopback HTTP-to-UDS bridge (`127.0.0.1:<ephemeral>`, pinned `Host`/`Origin`, Mode 3b in-memory bearer token or Mode 3a `(ephemeral_port, instance_id)`-bound `HttpOnly; SameSite=Strict` cookie, `fetch()` NDJSON streaming) or, once a pre-implementation Native SDK `0.11.1` capability probe passes, the `zero://app` desktop WebView host defined in [Client architecture](../../architecture.md).

Separate **Data State** (last verified snapshot stamped with `company_id`, `store_key`, `revision`, `as_of_ns`, `event_seq`) from **Link State** (`disconnected | connecting | connected | incompatible`) and parse incoming payloads into a fresh candidate state before swapping the presented snapshot so malformed or truncated replies never blank-wipe valid state. Routine refreshes and transient disconnects MUST NOT blank-wipe the last verified Data State; instead display an explicit Stale / Disconnected banner with elapsed age while locking all mutating controls fail-closed (`controls_locked = true`).

Clear cached Data State (`clearData()`) immediately—without touching valid on-disk `board-pending`—only on `company_id`/`store_key`/socket change, sign-out, principal change, `IncompatibleTimeline`, or `401`/`403` access denial. Keep **Host / Session Lock** strictly separate from both transient disconnect and `clearData()`: locking the host mounts an opaque full-viewport Privacy Shield (`data-agent-hidden`) over protected financial, decision, work, and outbox DOM without mutating `as_of_ns` or marking valid data stale, and lifting the shield requires OS/session unlock plus fresh `GET /v1/meta` and projection re-verification.

Gate all projection and control surfaces by effective principal state (`bootstrap_locked` vs `board_viewer` vs `board_control` under `[M3 Extension]` `board_binding` and `role_assignment`; `origin/main` CLI daemons bind `authorize_board=None` fail-closed `403` on commands). Display the observation time returned by the service; a missing investment projection is not a zero balance. Keep local selection, sorting and intended control changes separate from observed company facts. Closing the client stops only its presentation. See [Client screens and navigation](../screens/spec.md) and [Flow observability](../flow-observability/spec.md) for the complete screen routes and 13-flow projection map.

**Concern for the responsible component:** A cached response is not proof of present access. Clear private state on sign-out, principal change, `company_id`/`store_key` switch or `401`/`403` denial, and conceal protected DOM behind the Host-Lock Privacy Shield on OS/session lock; never show sensitive cached contents after access loss.

## Functional and quality requirements

- **FR.** Render owner, next duty, `as_of_ns`, `revision`, `event_seq`, and evidence gaps with the server’s observation time and Link State. Preserve the last verified snapshot across routine refreshes and transient disconnects with a visible stale indicator while disabling mutations. Gate read/control panels strictly across `bootstrap_locked`, `board_viewer`, and `board_control`. An absent business profile is normal.
- **NFR.** Navigation, filtering, reopening, host-locking, and reconnecting issue zero operational commands. Present status in text and support keyboard navigation.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Run a core-only company while the client is closed; reopen and compare its timeline with the authorized projection and independent evidence. |
| Transient disconnect | Drop the local UDS connection after a verified load. Confirm the UI retains the last verified snapshot with an explicit Stale / Disconnected banner and locked mutating controls (no blank-wipe), then recovers cleanly on reconnect without issuing operational commands. |
| Host/session lock | Trigger OS/session lock on a connected view. Confirm the opaque Privacy Shield conceals protected DOM without mutating `as_of_ns` or marking data stale, and re-verifies `GET /v1/meta` before reveal after unlock. |
| Failure or contention | Delay a response, invalidate an attachment grant and switch principal or receive `401`/`403`. Clear prior private data immediately (`clearData()`); do not show an old record as current or leak content from the previous principal. |

**Oracle:** Capture the actual screen, keyboard path, query revision, DOM redaction/shield state, and service command log.

## Operations and change review

Record the client build and supported contract revisions. A failed client can be rolled back independently; see the [client build contract](../../build.md).
