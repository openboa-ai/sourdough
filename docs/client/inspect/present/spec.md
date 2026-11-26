# Present a work projection

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume read models and their authorization, coverage, and revision metadata over `CompanyDaemon`'s UDS Control Plane via `ClientWebBridge` (`sourdough client-web`). Bind fields to accessible client views organized under the Modular IA shell, a strictly 4-slot header (`Brand`, `Approvals`, `⌘K`, `Theme Toggle`), `SidebarFooter` diagnostics (`-> /audit?tab=daemon`), and zero right slide-over drawers.

Input contracts: [Authorized work timeline](../../../company/systems/operations/work-timeline/read/spec.md) · [Client architecture](../../architecture.md) · [Meta and authorized read projections](../../control-plane/projections/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. UDS read transport & anti-tear Data/Link separation (`apps/client/src/state/store.tsx`)

Consume projections and change notifications over `CompanyDaemon`'s versioned local Unix domain socket Control Plane (`GET /v1/meta`, `14` `GET /v1/projections/*` endpoints across `15` contract revisions, and `GET /v1/events`) served via ACL-filtered on-demand `open_reader()` indexed queries (`ETag`, `If-None-Match`, `X-Sourdough-Event-Seq`, `store_key`, `event_seq`). Never read SQLite directly or spawn one-shot CLI processes per click. `ClientWebBridge` (`127.0.0.1`, pinned `Host`/`Origin`/`Sec-Fetch-Site`, port-bound `HttpOnly; SameSite=Strict` cookie `sd_sess_<port>_<instance_id>`, and `fetch()` NDJSON streaming) proxies `/api/v1/*` and manages `(company_id, principal_id)` `/api/local/pending`.

Separate **Data State** (last verified snapshot stamped with `company_id`, `store_key`, `revision`, `as_of_ns`, `event_seq`) from **Link State** (`"connecting" | "live" | "stale" | "unauthorized" | "error"`) and parse incoming payloads cleanly before swapping state so transient disconnects never blank-wipe verified projections:
- **Transient disconnect (`stale`):** Retains verified `Data State` beneath the sub-header stale `<Notice>` (`"Live connection interrupted — showing last known snapshot"`) while setting `controls_locked = true` and updating `SidebarFooter` `<Status>` to `"Reconnecting"`.
- **Authorization loss (`401`/`403`) or scope switch (`company_id`, `store_key`, `principal_id` change):** Executes immediate `clearData()`—clearing cached projection state and in-memory command state without touching valid `/api/local/pending` entries (except `403` stale-file quarantine).
- **Host / Session Lock (`host_locked = true`):** Mounts an opaque full-viewport Privacy Shield (`data-agent-hidden`) over protected DOM without mutating `as_of_ns` or marking valid data stale; lifting the shield re-verifies `GET /v1/meta` and projections.

### 2. Strict 4-slot header, `SidebarFooter` diagnostics, zero drawers & 3-tier disclosure

- **Strict 4-slot `<header className="sd-topbar">`:** (1) `Brand` (`SidebarTrigger` + Company link, zero `"Paper"` badge), (2) `Approvals` pill (`pendingApprovals > 0`), (3) `⌘K` Command Palette `Dialog`, and (4) `Theme Toggle`.
- **`SidebarFooter` diagnostics:** Displays role (`Board Control` / `Read-Only Viewer` / `Bootstrap Locked` / `Unbound`), active module badge (`Trading` or `Core OS`), and `<Status>` diagnostics button navigating to `/audit?tab=daemon`.
- **Zero right slide-over drawers:** Never mount `Y.Sheet` or right-side inspector panes. Selecting any item transitions the main workspace view (`Tier 0 -> Tier 1`, max depth `2`) with structural `<nav aria-label="Breadcrumb">` and deterministic keyboard focus recovery on Back/`Escape`.
- **3-Tier Disclosure Ceiling:** Technical identifiers (`command_id`, `preconditions`, `payload_sha256`, `fencing_token`, daemon PIDs) collapse behind `"Technical details"` disclosures (`ControlledInlineConfirm`) or `/audit?tab=daemon`.

### 3. Live NDJSON invalidation & `Interrupt` / `Approval` / `Digest` routing

- **`GET /v1/events` NDJSON streaming:** `fetch()` streaming receives redacted 7-field `audit_event` notifications emitted on `ResidentCompanyService` and `CompanyDaemon` commits, refreshing active route projections tear-free.
- **Anti-fatigue signal routing:** Route active `restriction_cause` blocks, `401`/`403`, disconnect stale state, and unresolved `CommandOutboxPanel` states to **`Interrupt`**; route `decision_question` items and strategy candidates to **`Approval`** (`/approvals`); and surface completed runs/fills seamlessly in live projections.

Gate all projection and control surfaces by effective principal state (`bootstrap_locked` vs `board_viewer` vs `board_control`) and installed domain modules (`meta.active_domains`). Display the observation time returned by the service; a missing close/cost projection is `"Unavailable"` / `"No reading"`, never `$0.00`. Closing the client stops only its presentation.

**Concern for the responsible component:** A cached response is not proof of present access. Clear private state on sign-out, principal change, `company_id`/`store_key` switch or `401`/`403` denial, and conceal protected DOM behind the Host-Lock Privacy Shield on OS/session lock.

## Functional and quality requirements

- **FR.** Render authorized projections with `as_of_ns`, `revision`, `event_seq`, and evidence gaps; preserve the last verified snapshot across routine refreshes and transient disconnects with a visible stale indicator while disabling mutations; enforce the 4-slot header + `SidebarFooter` diagnostics, zero right drawers, and `GET /v1/events` live updates.
- **NFR.** Navigation, filtering, reopening, host-locking, and reconnecting issue zero operational commands. Present status in text and support keyboard navigation with deterministic focus restoration.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Run a company while the client is closed; reopen and inspect `work-timeline` and route projections. Confirm zero write queries or commands emitted. |
| Transient disconnect | Drop the local UDS connection after a verified load. Confirm the UI retains the last verified snapshot with `"Live connection interrupted — showing last known snapshot"`, `SidebarFooter` `"Reconnecting"`, and locked mutating controls (no blank-wipe), then recovers cleanly on reconnect. |
| Host/session lock | Trigger lock on a connected view. Confirm the opaque Privacy Shield conceals protected DOM without mutating `as_of_ns` or marking data stale, and re-verifies `GET /v1/meta` before reveal after unlock. |
| Auth loss / scope switch | Switch principal or receive `401`/`403`. Clear prior private data immediately (`clearData()`); do not leak content from the previous principal. |

**Oracle:** Capture the actual screen, keyboard path, query revision, DOM redaction/shield state, `SidebarFooter` diagnostics link, and service command log.

## Operations and change review

Record the client build and supported contract revisions. A failed client can be rolled back independently; see the [client build contract](../../build.md).
