# Present a work projection

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume a read model and its authorization, coverage, and revision metadata over `CompanyDaemon`'s UDS Control Plane. Bind fields to accessible client views organized under a 3-tier progressive disclosure model, a strict 4-element `48px` Global Header, zero right slide-over drawers, and a pure-read `Since Last Visit` digest.

Input contracts: [Authorized work timeline](../../../company/systems/operations/work-timeline/read/spec.md) · [Client architecture](../../architecture.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. UDS read transport & anti-tear Data/Link separation

Consume projections and change notifications over `CompanyDaemon`'s versioned local Unix domain socket Control Plane (`GET /v1/meta`, `GET /v1/projections/{name}`, `GET /v1/events`) served via ACL-filtered on-demand `open_reader()` indexed queries (preserving `work-timeline-v1` content-hash `revision` while adding `ETag`, `If-None-Match`, `X-Sourdough-Event-Seq`, `store_key`, and `event_seq` on `work-timeline-v2` and extended projections; see [Meta and authorized read projections](../../control-plane/projections/spec.md)). Never read SQLite directly or spawn one-shot CLI processes per click. The React 19 + `@openboa/yogurt` presentation layer accesses the UDS Control Plane through the authenticated `sourdough client-web` loopback HTTP-to-UDS bridge (`127.0.0.1:<ephemeral>`, pinned `Host`/`Origin`, Mode 3b in-memory bearer token or Mode 3a `(ephemeral_port, instance_id)`-bound `HttpOnly; SameSite=Strict` cookie, `fetch()` NDJSON streaming, `[BACKEND_GAP #1]`) or, once a pre-implementation Native SDK `0.11.1` capability probe passes, the `zero://app` desktop WebView host defined in [Client architecture](../../architecture.md).

Separate **Data State** (last verified snapshot stamped with `company_id`, `store_key`, `revision`, `as_of_ns`, `event_seq`) from **Link State** (`disconnected | connecting | connected | incompatible`) and parse incoming payloads into a fresh candidate state before swapping the presented snapshot so malformed or truncated replies never blank-wipe valid state. Routine refreshes and transient disconnects MUST NOT blank-wipe the last verified Data State; instead display an explicit Stale / Disconnected banner with elapsed age while locking all mutating controls fail-closed (`controls_locked = true`).

Clear cached Data State (`clearData()`) immediately—without touching valid on-disk `board-pending`—only on `company_id`/`store_key`/socket change, sign-out, principal change, `IncompatibleTimeline`, or `401`/`403` access denial. Keep **Host / Session Lock** strictly separate from both transient disconnect and `clearData()`: locking the host mounts an opaque full-viewport Privacy Shield (`data-agent-hidden`) over protected financial, decision, work, office, CEO thread, and outbox DOM without mutating `as_of_ns` or marking valid data stale, and lifting the shield requires OS/session unlock plus fresh `GET /v1/meta` and projection re-verification.

### 2. Strict 4-element `48px` Global Header, zero drawers & 3-tier progressive disclosure

- **Strict 4-element Global Header**: Render strictly 4 elements in the persistent `48px` header: (1) Company Identity, (2) Primary View Switcher (`Live Trading`, `Office Floor`, `CEO Direct Line`, `Company`), (3) Connection & Freshness Status Trigger (opening an on-demand diagnostics popover housing `LinkState`, `as_of_ns`, `revision`, `event_seq`, `store_key`, `service_incarnation_id`, `principal_id`, `role`, Lock Screen trigger, and `"System Diagnostics"` link to `/system/daemon`), and (4) Primary Safety / CEO Trigger. All breadcrumbs, urgent `Notice` banners, `CommandOutboxPanel`, and `/` `Since Last Visit` briefs render strictly **below** the Global Header.
- **Zero right slide-over drawers**: Never mount `Y.Sheet` or right-side inspector panes. Selecting any item transitions the main workspace view (`Tier 0 -> Tier 1`, max depth `2`) with structural `<nav aria-label="Breadcrumb">` and deterministic keyboard focus recovery on return.
- **3-Tier Disclosure Ceiling**: Keep Tier 0 executive views free of raw technical IDs (`charter_revision`, `expected_revision`, `restriction_id`, `payload_sha256`, `command_id`, `fencing_token`, `lease`, `event_seq`, `segment_sequence`, `connector_revision`, `denial_code`, SHA-256 digests); collapse those fields into in-place `"Technical details"` disclosures (`Tier 2`), Header Slot 3's popover, or quiet `/system/*` routes.

### 3. Pure-read `Since Last Visit` (`Digest`) & `Interrupt` / `Approval` / `Digest` routing

- **Pure-read `Digest` (`GET /v1/projections/company-overview?since_seq=<int>`, `[BACKEND_GAP #8]`)**: Maintain the last-seen `event_seq` watermark locally in the authenticated host/bridge scope per `(company_id, principal_id)` and query `since_seq` read-only over `open_reader()`. Reading `Since Last Visit` MUST NEVER execute a SQLite write or advance server state (preserving `docs/conventions.md:35` and `docs/glossary.md:53`); watermark advancement is strictly a client/bridge presentation preference updated explicitly when the operator dismisses/acknowledges the brief.
- **Anti-fatigue signal routing**: Route root-cause operational blocks (`RestrictionView` clustered under one `restriction_id`, `401`/`403`, disconnect stale state, `CommandOutboxPanel`) to **`Interrupt`**; route reserved `DecisionQuestion`s and strategy promotions calmly to **`Approval`** (`/company/approvals`, `/company/strategy-trials`); and fold overnight completions, settled orders, and resolved restrictions into `/` **`Digest`** (`Since Last Visit`).

Gate all projection and control surfaces by effective principal state (`bootstrap_locked` vs `board_viewer` vs `board_control` under `[BACKEND_GAP #2]` `board_binding` and `role_assignment`; `origin/main` CLI daemons bind `authorize_board=None` fail-closed `403` on commands). Display the observation time returned by the service; a missing investment projection is not a zero balance. Closing the client stops only its presentation. See [Client screens and navigation](../screens/spec.md) and [Flow observability](../flow-observability/spec.md) for the complete screen routes and 13-flow projection map.

**Concern for the responsible component:** A cached response is not proof of present access. Clear private state on sign-out, principal change, `company_id`/`store_key` switch or `401`/`403` denial, conceal protected DOM behind the Host-Lock Privacy Shield on OS/session lock, and never allow a `Since Last Visit` query to mutate server state.

## Functional and quality requirements

- **FR.** Render authorized projections with `as_of_ns`, `revision`, `event_seq`, and evidence gaps; preserve the last verified snapshot across routine refreshes and transient disconnects with a visible stale indicator while disabling mutations; enforce the 4-element `48px` Global Header (technical metadata inside Slot 3 popover / Tier 2 disclosures), zero right drawers, pure-read `?since_seq=` `Since Last Visit` digest, and `Interrupt`/`Approval`/`Digest` routing.
- **NFR.** Navigation, filtering, reopening, host-locking, `Since Last Visit` watermark reads, and reconnecting issue zero operational commands. Present status in text and support keyboard navigation with deterministic focus restoration.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Run a core-only company while the client is closed; reopen and inspect `/` `Since Last Visit` (`?since_seq=S0`) and `work-timeline` against the authorized projection. Confirm zero write queries or commands emitted. |
| Transient disconnect | Drop the local UDS connection after a verified load. Confirm the UI retains the last verified snapshot with an explicit Stale / Disconnected banner and locked mutating controls (no blank-wipe), then recovers cleanly on reconnect without issuing operational commands. |
| Host/session lock | Trigger OS/session lock on a connected view. Confirm the opaque Privacy Shield conceals protected DOM without mutating `as_of_ns` or marking data stale, and re-verifies `GET /v1/meta` before reveal after unlock. |
| Failure or contention | Delay a response, invalidate an attachment grant and switch principal or receive `401`/`403`. Clear prior private data immediately (`clearData()`); do not show an old record as current or leak content from the previous principal. |

**Oracle:** Capture the actual screen, keyboard path, query revision, DOM redaction/shield state, Header Slot 3 popover state, and service command log.

## Operations and change review

Record the client build and supported contract revisions. A failed client can be rolled back independently; see the [client build contract](../../build.md).
