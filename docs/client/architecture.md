# Board Client architecture

Design draft · [Board Client](README.md) · [System map](../README.md) · [Development lifecycle](../engineering/lifecycle.md)

The Board Client is a presentation and control-plane interface over `CompanyDaemon`. It translates authorized projections into operator views and forwards explicit human decisions to the daemon's local Unix domain socket (UDS) Control Plane. The company operates, trades, reconciles, and recovers from crashes without the client installed or running.

## 1. Ownership boundaries

| Layer | Component | Authoritative ownership | Hard prohibitions |
| --- | --- | --- | --- |
| **Presentation** | React 19 + `@openboa/yogurt` (`0.2.0-local.1`) + `@ui-sdk/react` (`0.1.0-beta.13`) | Active route, `DomainModule` dynamic mounting (`domainKey: "trading"`), local table sort/filter, `LinkState`, host-lock privacy shield, form drafts, inline `ControlledInlineConfirm` expansion state, `CommandOutboxPanel` display | Zero direct SQLite access; zero venue/exchange/owner keys or secrets in renderer; zero client-side P&L/return calculation; zero synthetic command receipts; zero right slide-over drawers (`Y.Sheet` prohibited); zero popup confirmation modals for routine approvals |
| **Host / Local Bridge** | `ClientWebBridge` (`sourdough client-web`, `src/sourdough/ipc/web_bridge.py`) | Loopback HTTP session auth (`sd_sess_<port>_<instance_id>`), `Host`/`Origin`/`Sec-Fetch-Site` pinning, UDS transport framing, `/api/local/pending` (`0o600` `O_NOFOLLOW` `(company_id, principal_id)` `board-pending` persistence + `403` stale-file quarantine) | Never mutates SQLite directly; never mints a second `command_id` for an unresolved request; never exposes browser tokens in URLs or `localStorage` |
| **Control Plane** | `CompanyDaemon` (`src/sourdough/ipc/server.py`, `control-plane-v1`) | UDS kernel peer verification (`LOCAL_PEERCRED` / `LOCAL_PEERPID` / `SO_PEERCRED`), `board_binding` & `role_assignment` RBAC, `18` `/v1/*` routes (`GET /v1/meta`, `14` `GET /v1/projections/*` across `15` revisions, `GET /v1/events`, `POST /v1/commands`, `GET /v1/commands/{command_id}?operation=...`), `12` commands (`8` Core + `4` Trading gated `422` when absent) | Never assumes a UI click equals an applied transition; never grants `board_control` solely from same-UID socket access; never routes Board 1:1 chat to non-CEO Agents; never exposes Trading routes/commands on core-only stores |
| **Domain Owners** | `ResidentCompanyService`, `WorkKernel`, `DutyRegistry`, `DomainPlugin` (`TradingDomainPlugin`), Evaluator Store | SQLite canonical `v1` (`core` `52` tables + opt-in `trading` `16` tables, `STRICT` + `WAL` + `WriterLease`), unified `trading_book_*` & `execution_environment_binding`, `IndependentProcessGuard` / `sender_lease` | Zero imports of client code or UI packages; zero trading imports or assumptions in Core Company OS; zero reliance on UI state for admission, reconciliation, or recovery |

Per-click CLI subprocesses (`sourdough client-bridge` or direct-store `inspect-work` / `read-command-receipt`) are prohibited in the client. All reads and writes traverse `CompanyDaemon`'s resident UDS `/v1/*` Control Plane (`0o700` socket directory, `0o600` socket, `<= 103`-byte path limit).

## 2. Host topology and `ClientWebBridge` HTTP-to-UDS security

`sourdough client-web` (or `sourdough run-company ... --web-port <port>`) serves the compiled React 19 `@openboa/yogurt` bundle via `ClientWebBridge` (`src/sourdough/ipc/web_bridge.py`), a local HTTP-to-UDS bridge forwarding exclusively to `CompanyDaemon`'s UDS socket:

- **Loopback bind only**: Listens strictly on `127.0.0.1:<ephemeral|port>` (never `0.0.0.0` or wildcard interfaces).
- **Strict `Host`, `Origin`, and `Sec-Fetch-Site` pinning**: Rejects any request whose `Host` header is not the exact bound `127.0.0.1:<port>` or `localhost:<port>`, or whose `Origin` / `Sec-Fetch-Site` indicates a cross-origin or DNS-rebinding caller, failing closed with `403`.
- **Port-bound session cookie & CSP nonce**: Serves `index.html` with a per-response CSP style nonce and issues a port-bound `HttpOnly; SameSite=Strict` cookie (`sd_sess_<port>_<instance_id>`) bound to `(port, instance_id)` so co-hosted loopback ports never collide. Rejects URL query-parameter tokens (`?token=`) and never persists tokens in `localStorage` or `sessionStorage`.
- **`fetch()` NDJSON streaming (never `EventSource`)**: Consumes `GET /v1/events?after_seq=<n>&limit=<k>&wait_ms=<ms>` (`application/x-ndjson`, `wait_ms <= 5000`, `Connection: close`, `Cache-Control: no-store`) via `fetch()` `ReadableStream` / bounded long-poll so auth cookies, structured `401`/`403`/`503` disconnect payloads, and `clearData()` synchronize cleanly.
- **Bridge-owned `0o600` `board-pending` persistence (`/api/local/pending`)**: `ClientWebBridge` (`web_bridge.py:432-545`) owns write-ahead `LocalCommandIntent` persistence (`{company_id, principal_id, command_id, command_type, contract_version, preconditions, payload, label, sent_ns}`) inside an `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` (`0o700`) directory using atomic `O_CREAT | O_EXCL | O_NOFOLLOW` (`0o600`) files, `fsync(fd)`, and `fsync(dir_fd)`, keyed strictly by `(company_id, principal_id)`. Persisting canonical `preconditions` plus `contract_version` ensures cold-start `Retry same ID` resubmits the exact original request without reading newer live revisions; `403` on startup recovery quarantines/purges stale foreign files safely.
- **Explicit `LOCAL_PEERPID` collapse boundary**: `CompanyDaemon` verifies kernel peer identity on the UDS socket via `LOCAL_PEERCRED` + `LOCAL_PEERPID` (macOS) or `SO_PEERCRED` (Linux). When `ClientWebBridge` proxies browser HTTP over UDS, connected browser tabs share the bridge process PID, so `board_control` requires explicit company-enrolled `board_binding` + `role_assignment` and bridge-enforced user confirmation.

## 3. Disposition of `apps/board/` v0 and preserved invariants

The repository retains `apps/board/` (a minimal read-only Zig `0.16.0` / Native SDK `0.11.1` v0 skeleton) solely as a superseded historical artifact. `apps/client/` (`React 19` + `@openboa/yogurt` `0.2.0-local.1` + `ClientWebBridge`) supersedes `apps/board/` completely:

| Artifact / Mechanism in `apps/board/` | Disposition | Reason & Active Replacement |
| --- | --- | --- |
| `src/app.native`, `prepare-ui.sh` (`--target native`), `dependencies.lock` | **Superseded** | `@openboa/yogurt` `0.2.0-local.1` supports `--target web` only; replaced by `apps/client/` React 19 + `@openboa/yogurt` web bundle. |
| `src/main.zig` (`.gpu_surface`, Metal backend) | **Superseded** | Replaced by `ClientWebBridge` (`sourdough client-web`). |
| `src/timeline.zig` per-click `client-bridge` subprocesses & `work-timeline-v1` pin | **Superseded** | Replaced by persistent UDS HTTP/1.1 + `work-timeline-v2` (`ETag`, `If-None-Match`, `event_seq`) + `GET /v1/events` NDJSON streaming. |
| `src/pending.zig` company-only pending path (`{base_dir}/{company_id}.json`) | **Superseded** | Replaced by `(company_id, principal_id)` keying + `403` stale-file quarantine in `ClientWebBridge` `/api/local/pending`. |
| **Data State vs Link State** model, `controls_locked`, `clearData()` triggers, and atomic parse swap | **Preserved** | Implemented in `apps/client/src/state/store.tsx` (see §6 and [present/spec.md](inspect/present/spec.md)). |
| **8-variant `CommandLifecycle`**, `0o600` `O_NOFOLLOW` `fsync`, `payload_sha256`, and `not_found_retry_choice` | **Preserved + Extended** | Implemented across `ClientWebBridge` and `CommandOutboxPanel` (see [submit/spec.md](commands/submit/spec.md)). |

## 4. Modular Company OS Information Architecture (`AppShell` & `DomainModule`)

Navigation cleanly separates the industry-neutral **Core Company OS** (`Approvals`, `Company`, `System`) from pluggable **Business Domain Modules** (`apps/client/src/domains/registry.ts`), keeping zero right slide-over drawers and strictly 4 top-level header slots.

### 4.1 Canonical Modular IA & 17 Mounted Route Branches (`apps/client/src/App.tsx`)

| Section / Scope | Route Pattern(s) | View Component | Domain Gate & Purpose |
| --- | --- | --- | --- |
| **Bootstrap Gate** | `/bootstrap` | `BootstrapView` | Owner binding (`register_owner`) when `meta.board_access.bootstrap_locked` (or `!meta && path === "/bootstrap"`). |
| **Approvals (Actionable Plane)** | `/approvals`, `/approvals/:questionId` | `ApprovalsView` | Dual-entry `DecisionQuestion` inbox + deep-linked question focus; pairs with in-place `ContextualApprovalCard` across domain and company views. |
| **Dynamic `Trading` Module** (`domainKey: "trading"`) | `/` *(single module)*, `/trading/live`, `/trading/paper`, `/orders/:orderId`, `/trading/strategies` | `TradingDeskView(mode="live"\|"paper")`, `OrderTraceView`, `StrategiesView` | Mounted dynamically **only** when `"trading"` is in `meta.active_domains` (`TRADING_DOMAIN_MODULE`). `TradingDeskView` serves both `live` (`/`, `/trading/live`) and `paper` (`/trading/paper`) on `trading-desk-v1`; `OrderTraceView` inspects `trading_book_order`/`trading_book_fill` across `paper`/`live`/`agent_sandbox`; `StrategiesView` renders `strategies-v1`. Core-only companies (`active_domains = ["core"]`) omit `Trading` nav/routes and redirect `/` -> `/agents`. *(When 2+ workload modules exist, `/` renders `CompanyOverviewView`.)* |
| **`Company` (Core OS)** | `/agents`, `/agents/:agentId` | `AgentsActivityView`, `AgentDetailView` | Core Company OS agent graph/list (`Graph \| List` toggle, `agents-graph-v1`, clickable message edges) and full-screen Agent Detail (`Work Activity`, `Interactions & Talks`, `Published Reports`, `Peer Requests & Handoffs`, plus conditional `Trading Sandbox` tab when `"trading.agent_sandbox"` / `"quant.agent_sandbox"` or sandbox activity exists). |
| **`Company` (Core OS)** | `/messages`, `/finance`, `/reports` | `MessagesView`, `FinanceView`, `OrgReportsView` | `/messages`: writable 1:1 `Founder · CEO` thread (`send_ceo_message`) + read-only internal channels + `?context_kind=&context_id=`. `/finance`: Core `account_balances`, `journals`, `accounting_closes`, `open_close_issues` + conditional `Retained Window Statements` & `funding_settlements` when `trading_enabled`. `/reports`: `Roster`, `Published Reports`, read-only `Delegated HR Custody` (`"Managed by CEO · Board view is read-only"`). |
| **`System` (Core OS)** | `/controls`, `/backups`, `/audit`, `/settings` | `RestrictionsView`, `SystemSafetyView`, `AuditHubView`, `SettingsView` | `/controls`: Core `pause_company` / `resume_company` + conditional Trading `execution:*` risk controls (`data.trading_risk`). `/backups`: checkpoints, restore receipts, `cut_checkpoint`, 5-step `safe_custody_hold`. `/audit`: `?tab=log\|evals\|daemon` (`Audit Log`, `Evaluations`, `Daemon Diagnostics`). `/settings`: Appearance (`light\|dark\|auto`), Material (`neutral\|paper\|mist`), Host Session & Privacy lock, Installed Business Modules. |

### 4.2 Strict 4-Slot `AppShell` Header, `SidebarFooter` Diagnostics & Sub-Header Regions

- **Top Bar (`<header className="sd-topbar">`, strictly 4 slots):**
  1. **Slot 1 — Brand:** `SidebarTrigger` + Company link (`Company <id>` or `Sourdough`), routing to `homeMode === "core_only" ? "/agents" : "/"` with zero `"Paper"` badge.
  2. **Slot 2 — Approvals Pill:** Conditional `<Button tone="brand">Approvals ({pendingApprovals})</Button>` when `pendingApprovals > 0` -> `/approvals`.
  3. **Slot 3 — Command Palette (`⌘K`):** `<Dialog title="Board Client Command">` powered by `@ui-sdk/react/command` (`nav:*`, `action:theme`, `action:lock`).
  4. **Slot 4 — Theme Toggle:** `<IconButton icon={isDark ? "sun" : "moon"} />` toggling light/dark appearance.
- **Left Sidebar (`SidebarLayout`) & `SidebarFooter`:**
  - Top unlabelled group (`Overview` when multi-module, `Approvals` with pending `Badge`), dynamic `Trading` group (`Live Trading` `G L`, `Paper Trading` `G P`, `Strategies` `G S`), `Company` group (`Agents` `G A`, `Messages` `G M`, `Finance`, `Reports`), and `System` group (`Controls` `G C`, `Backups`, `Audit`, `Settings` `G ,`).
  - **`SidebarFooter`:** Displays principal role (`Board Control` / `Read-Only Viewer` / `Bootstrap Locked` / `Unbound`), active module badge (`Trading` or `Core OS`), and a diagnostics trigger wrapping `<Status tone={statusTone}>{statusLabel}</Status>` (`Live · seq #N` / `Paused · seq #N` / `Reconnecting` / `Access denied` / `Connecting`) navigating directly to **`/audit?tab=daemon`**.
- **Sub-Header Dedicated Regions (strictly below the topbar):**
  - Disconnected stale `<Notice>` (`"Live connection interrupted — showing last known snapshot"`), active restriction `<Notice>` (`"Operational restrictions are currently active"` -> `/controls`), and persistent inline `<CommandOutboxPanel />`.
- **Zero Right Slide-Over Drawers & `ControlledInlineConfirm`:**
  - `Y.Sheet` and right inspector panes are prohibited across all routes. Drill-downs transition in-place (`Tier 0 -> Tier 1`) with breadcrumbs and focus restoration.
  - All state-mutating commands confirm inline via `ControlledInlineConfirm` (`@ui-sdk/react/button` `SDKButton`, rejecting `details?.source === "agent"`, `data-agent-confirm`, contrasting **What changes** vs **What stays untouched**, collapsed `"Technical details"`, and handoff to `CommandOutboxPanel`).

## 5. Role, principal, and Board-vs-CEO visibility matrix

`CompanyDaemon` enforces `board_binding` and `role_assignment` RBAC (`board_viewer` vs `board_control`; fail-closed `403` when unbound). Board 1:1 chat routes strictly to the designated CEO (`/messages`, `send_ceo_message`); routine HR offboarding is CEO-delegated (`retirement.py`) and read-only on `/reports`.

| Surface / Capability | `bootstrap_locked` *(Unbound New Store)* | `board_viewer` *(Bound Read-Only Principal)* | `board_control` *(Bound Control Principal)* | `controls_locked` *(Disconnected / Unknown / Stale)* | `host_locked` *(Privacy Lock)* |
| --- | --- | --- | --- | --- | --- |
| **Projections (`GET /v1/meta`, `/v1/projections/*`, `/v1/events`)** | Company identity (`company_id`, `store_key`, `schema_version`) and bootstrap prompt only | Full authorized projections (`as_of_ns`, `revision`, `event_seq`) | Full authorized projections (`as_of_ns`, `revision`, `event_seq`) | Last verified `Data State` preserved under stale banner (`stale = true`) | **Concealed** behind opaque Privacy Shield (`data-agent-hidden`); `as_of_ns` unchanged |
| **Commands (`POST /v1/commands`)** | `register_owner` only | Hidden or disabled (`"Read-only role (board_viewer)"`); daemon enforces `403` | Enabled iff `link_state === "connected" && !stale && scopeMatchesData() && command_state in {"idle","applied","denied"}` | **Fail-closed locked** (`controls_locked = true`) | Hidden + disabled (`data-agent-hidden`); requires unlock + fresh `GET /v1/meta` |
| **Messages (`/messages`)** | Hidden | Read-only thread (`send_ceo_message` disabled) | 1:1 advisory chat strictly to `ceo_agent_id` (`send_ceo_message`) + `Contextual Ask CEO` | Composer disabled; verified messages retained | Concealed behind Privacy Shield |
| **Private Desk Drafts (`.desk/drafts/*`)** | Never exposed | Never exposed (`private_desk` counts/metadata only) | Never exposed | Never exposed | Never exposed |
| **Pending Outbox (`/api/local/pending`)** | Empty | Read-only status if prior `(company_id, principal_id)` record exists | Scoped strictly to `(company_id, principal_id)`; `403` quarantines foreign stale file | Retains `LocalCommandIntent`; queries `GET /v1/commands/{command_id}?operation=...` on reconnect | Concealed behind shield without deleting persisted pending state |
| **Auth Loss / Scope Switch (`401`, `403`, Principal / `company_id` / `store_key` change)** | N/A | Instant `clearData()` wipe | Instant `clearData()` wipe + `clearInMemoryCommandState()` | Instant `clearData()` wipe | Instant `clearData()` wipe if session revokes while locked |

## 6. Link state, data wipe, and host-lock privacy shield

Three failure/privacy states remain strictly disjoint:

1. **Transient Disconnect / Daemon Restart (`ECONNREFUSED`, EOF, timeout, `503`):** Sets `link_state = "disconnected"`, `stale = true`, `controls_locked = true`, preserving verified `Data State` beneath a sub-header stale `Notice` without calling `clearData()`. Reconnect runs `GET /v1/meta` and refreshes projections.
2. **Authorization Loss (`401`/`403`) or Scope/Principal Switch:** Executes **instant zero-grace `clearData()`**, wiping cached projection state, clearing protected DOM, and resetting in-memory command state while quarantining stale `403` pending files safely.
3. **Host / Session Lock (`host_locked = true`):** Mounts an opaque full-viewport Privacy Shield (`data-agent-hidden`) over protected UI subtrees without mutating `data_as_of_ns`, `stale`, or `LinkState`. Unlocking re-verifies `GET /v1/meta` before lifting the shield.

## 7. Design-system interface contracts

[Design system intent](design-system/intent.md), [Tokens and layout](design-system/tokens-and-layout/spec.md), [Verified Yogurt component bindings](design-system/yogurt-components/spec.md), and [Client build contract](build.md) bind `@openboa/yogurt` (`0.2.0-local.1`) and `@ui-sdk/react` (`0.1.0-beta.13`) to:

1. **Modular IA Route Map & Capability Negotiation:** All `17` `App.tsx` route branches gated by `GET /v1/meta` (`active_domains`, `supported_projections`, `supported_commands`).
2. **Strict 4-Slot Header + `SidebarFooter` Diagnostics:** Header slots strictly `Brand`, `Approvals`, `⌘K`, and `Theme Toggle`; connection `<Status>` lives in `SidebarFooter` linking to `/audit?tab=daemon`.
3. **Zero Right Slide-Over Drawers:** No `Y.Sheet` or right-side inspector panes; in-place `Tier 0 -> Tier 1` navigation with breadcrumbs and focus restoration.
4. **Core Domain Compositions:** 4-slot `AppShell` header + `SidebarLayout`, `AgentsActivityView` interactive graph/list, `0%`/`$0` baseline `PortfolioChart` with `null`-gap breaks and `Shadow Benchmark` overlay, and `ControlledInlineConfirm` (`@ui-sdk/react/button` `SDKButton` with **What changes** / **What stays untouched** `<dl>` rows and collapsed `"Technical details"`).
5. **Governance & Template Discipline:** Zero Board employee-retirement confirm bindings, `#175174` commitment-CTA reservation, `0` gradients, `0` static card shadows, `tabular-nums`, and `createYogurtAgent` safety guards.

[Inspect authorized company state](inspect/intent.md) · [Govern through Board approvals, controls, and CEO dialogue](governance/intent.md) · [Request a governance change](commands/intent.md) · [Usage scenarios](scenarios.md)
