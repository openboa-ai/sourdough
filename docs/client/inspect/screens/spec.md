# Present client screens and navigation

Design draft · Verification **UNRUN** · Responsible component: Board Client (`apps/client/src/App.tsx`, `apps/client/src/components/AppShell.tsx`, `apps/client/src/domains/registry.ts`)

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Agents & Activity](../office-floor-map/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [Approvals & Custody](../../governance/approvals-and-custody/spec.md) · [Flow observability](../flow-observability/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Define the complete Modular IA screen inventory (`17` mounted route branches in `apps/client/src/App.tsx` across `Approvals`, dynamic `Trading`, `Company`, `System`, and `/bootstrap`), the strictly 4-slot `<header className="sd-topbar">` + `SidebarFooter` diagnostics (`-> /audit?tab=daemon`), zero-drawer navigation model, 3-tier progressive disclosure, role gates, redaction rules, and required UI states. All screens consume `CompanyDaemon`'s `18` `/v1/*` routes (`GET /v1/meta`, `14` `GET /v1/projections/*` endpoints across `15` contract revisions, `GET /v1/events`, `POST /v1/commands`, `GET /v1/commands/{id}`) via `ClientWebBridge`, routing mutating actions through [Command submission](../../commands/submit/spec.md) and `ControlledInlineConfirm`.

Input contracts: [Base presentation](../present/spec.md) · [Trading Desk & Strategies](../investment-panel/spec.md) · [Agents & Activity](../office-floor-map/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [Approvals & Custody](../../governance/approvals-and-custody/spec.md) · [Command submission](../../commands/submit/spec.md) · [Client architecture](../../architecture.md). These are public service and client contracts, not imports of service implementations.

## Design decision and review

### 1. Global Header (strictly 4 slots), `SidebarFooter` Diagnostics, Sub-Header Regions & Zero-Drawer Shell

| Surface / ID | Placement & Purpose | Projection / Endpoint Source | Content & Disclosure Rules | Role Gate & Redaction |
| --- | --- | --- | --- | --- |
| **Top Bar (`sd-topbar`, 4 Slots)** | Persistent top header across all routes. | `GET /v1/meta` | **Slot 1 — Brand:** `SidebarTrigger` + Company link (`homeMode === "core_only" ? "/agents" : "/"`, zero `"Paper"` badge).<br>**Slot 2 — Approvals Pill:** Conditional `<Button tone="brand">Approvals ({pendingApprovals})</Button>` when `pendingApprovals > 0` -> `/approvals`.<br>**Slot 3 — Command Palette (`⌘K`):** `<Dialog title="Board Client Command">` wrapping `@ui-sdk/react/command` (`nav:*`, `action:theme`, `action:lock`).<br>**Slot 4 — Theme Toggle:** `<IconButton icon={isDark ? "sun" : "moon"} />`. | Always rendered; protected identifiers conceal behind Privacy Shield (`data-agent-hidden`) when `host_locked = true`. |
| **Left Sidebar & `SidebarFooter`** (`SidebarLayout`) | Persistent left navigation rail. | `GET /v1/meta`, `GET /v1/events` | **Top:** `Overview` (when 2+ modules), `Approvals` (`/approvals`, pending `Badge`).<br>**Dynamic `Trading` (`domainKey: "trading"`):** `Live Trading` (`/` `G L`), `Paper Trading` (`/trading/paper` `G P`), `Strategies` (`/trading/strategies` `G S`).<br>**`Company`:** `Agents` (`/agents` `G A`), `Messages` (`/messages` `G M`, unread dot), `Finance` (`/finance`), `Reports` (`/reports`).<br>**`System`:** `Controls` (`/controls` `G C`, active badge), `Backups` (`/backups`), `Audit` (`/audit`), `Settings` (`/settings` `G ,`).<br>**`SidebarFooter`:** Principal role (`Board Control` / `Read-Only Viewer` / `Bootstrap Locked` / `Unbound`), active module badge (`Trading` or `Core OS`), and `<Status>` diagnostics button (`data-focus-id="sidebar-diagnostics-trigger"`) -> **`/audit?tab=daemon`**. | Core-only companies (`"trading"` absent) hide `Trading` section and `g+l`/`g+p`/`g+s` shortcuts cleanly. |
| **Sub-Header Notices & `CommandOutboxPanel`** | Mounted **strictly below** `sd-topbar`. | `GET /v1/events`, `GET /v1/projections/restrictions`, `GET /v1/commands/{id}`, `/api/local/pending` | Stale disconnect `<Notice>` (`"Live connection interrupted — showing last known snapshot"`), active restriction `<Notice>` (`"Operational restrictions are currently active"` -> `/controls`), and persistent inline `<CommandOutboxPanel />` (`pending_age`, `Receipt` with `onLookup` strictly when `unknown`, `Retry same request` / `Discard draft` strictly when `not_found_retry_choice`). | Read-only for `board_viewer`; interactive for `board_control` when `!controls_locked`; concealed under `host_locked`. |
| **`ControlledInlineConfirm`** | In-place inline expandable card for every mutating Board action (**zero popup modals and zero right drawers**). | `POST /v1/commands` | Built on `@ui-sdk/react/button` (`SDKButton`) rejecting `details?.source === "agent"`, setting `data-agent-confirm="<consequence>"` solely on confirm CTA. Contrasts **What changes** (`<dl>`) vs **What stays untouched** (`<dl>`), captures `payload.reason` where required, and collapses technical preconditions inside `"Technical details"`. | Disabled unless `role === "board_control"` (or `register_owner` under `bootstrap_locked`) and `!controls_locked`. |
| **Host-Lock Privacy Shield** | Full-viewport opaque overlay activated by Privacy Lock (`⌘K` or `/settings`). | `GET /v1/meta` + route re-verification on unlock | Conceals protected financial, decision, agent, message, and outbox DOM (`data-agent-hidden`). **Does not** mutate `as_of_ns` or mark fresh data stale. | Unlock re-verifies `GET /v1/meta`. |

**Zero-Drawer Rule & Tier 1 Navigation Ceiling:** Right-edge slide-over drawers (`Y.Sheet` and right-side inspector panes) are **banned across 100% of screens and breakpoints**. Selecting any order, agent, or approval transitions the main workspace view in-place (`Tier 0 -> Tier 1`, max depth `2`) with structural `<nav aria-label="Breadcrumb">` and deterministic focus restoration on Back/`Escape`.

---

### 2. Complete `17`-Route Modular IA Matrix (`apps/client/src/App.tsx`)

#### Gate & Dynamic `Trading` Module (`domainKey: "trading"` — mounted only when `"trading"` in `active_domains`)

| # | Route Pattern | Component | Projection / Command Source | Primary Content, Role Gate & States |
| --- | --- | --- | --- | --- |
| 1 | `/bootstrap` | `BootstrapView` | `GET /v1/meta`, `register_owner` (`rbac-command-v1`) | Mounted when `meta.board_access.bootstrap_locked` (or `!meta && path === "/bootstrap"`). Enrolls initial `board_control` owner via `ControlledInlineConfirm`; hides operational data until unlocked. |
| 2 | `/` | `TradingDeskView(mode="live")` \| `CompanyOverviewView` \| `AgentsActivityView` | `trading-desk-v1` (single `"trading"`) or redirect to `/agents` (core-only) | Single `"trading"` module opens `TradingDeskView(mode="live")`; 2+ workload modules open `CompanyOverviewView`; core-only (`active_domains = ["core"]`) redirects (`replace: true`) to `/agents`. |
| 3 | `/trading/live` | `TradingDeskView(mode="live")` | `GET /v1/projections/trading-desk?environment=live`, `promote_execution_environment`, `demote_live_environment` | Gated on `tradingActive`. 4-KPI top band, `PortfolioChart` (`0%`/`$0` baseline + `null`-gap breaks + `Shadow Benchmark`), `pnl_reconciliation_bridge`, `open_positions`, `execution_tape` (linking to `/orders/:id?env=live`), Live Standby vs active Live governance (`promote_live` success events, `ContextualApprovalCard`). |
| 4 | `/trading/paper` | `TradingDeskView(mode="paper")` | `GET /v1/projections/trading-desk?environment=paper&book_id=...` | Gated on `tradingActive`. Concurrent `company_paper` book selector (`book_id`), symmetric 4-KPI band, `PortfolioChart`, `open_positions`, `execution_tape` (linking to `/orders/:id?env=paper`), `paper_soak_ready` + `company_paper_soak_ready` compat alias. |
| 5 | `/orders/:orderId` | `OrderTraceView` | `GET /v1/projections/orders/{order_id}` (`order-trace-v1`) | Gated on `tradingActive`; `?env=live\|paper`. Causal lineage over `trading_book_*`, `execution_environment_binding`, `work_runs`, `external_actions`, `shared_publication`; environment-aware `back_link` (`/trading/live`, `/trading/paper`, `/agents/:actor_id`); `"Ask CEO"` (`kind: "trading_order"`). |
| 6 | `/trading/strategies` | `StrategiesView` *(in `views/trading/`)* | `GET /v1/projections/strategies` (`strategies-v1`), `activate_strategy_release`, `rollback_strategy_deployment` | Gated on `tradingActive`. `trading_strategy_release`, `trading_pit_catalog_manifest`, `trading_challenger_trial` (`PROMOTE\|REJECT\|INCONCLUSIVE`), `trading_strategy_deployment`; non-self-activation; forward rollback preserves open orders/fills/costs. |

#### Universal Core Company OS Routes (`Approvals`, `Company`, `System` — always available)

| # | Route Pattern | Component | Projection / Command Source | Primary Content, Role Gate & States |
| --- | --- | --- | --- | --- |
| 7–8 | `/approvals`, `/approvals/:questionId` | `ApprovalsView` | `GET /v1/projections/approvals` (`approvals-v1`), `submit_decision_answer` (`decision-command-v1`) | Dual-entry `DecisionQuestion` inbox & deep-linked question focus (`ContextualApprovalCard`), equal-weight `Approve` / `Decline` / `"Ask CEO"`, clean `application_status = 'failed'` surfacing when domain action refuses, and conditional `promote_candidates` when `"trading"` is installed. |
| 9 | `/agents` | `AgentsActivityView` | `GET /v1/projections/agents-graph` (`agents-graph-v1`) | `Graph \| List` toggle (`default_view_mode`), status, budget, opaque `.desk` draft count (`private_draft_count` only), conditional `sandbox_activity` badge, clickable collaboration edges -> `/messages`. |
| 10 | `/agents/:agentId` | `AgentDetailView` | `GET /v1/projections/agents/{agent_id}` (`agent-detail-v1`), `interrupt_run`, `cancel_work` (`work-command-v1`) | Breadcrumb + 4 Core tabs (`Work Activity`, `Interactions & Talks` read-only, `Published Reports`, `Peer Requests & Handoffs`) + conditional `Trading Sandbox` tab (shown only when `"trading.agent_sandbox"` / `"quant.agent_sandbox"` or `agent_sandbox` portfolio activity exists). |
| 11 | `/messages` | `MessagesView` | `GET /v1/projections/messages` (`messages-v1`), `send_ceo_message` (`ceo-chat-command-v1`) | Writable 1:1 `Founder · CEO` thread + read-only `internal_channels` (`project_message`) + `?context_kind=&context_id=` (`ALLOWED_ASK_CONTEXT_KINDS`). Zero non-CEO composer; zero inline command buttons. |
| 12 | `/finance` | `FinanceView` | `GET /v1/projections/finance` (`finance-v1`) | Core `account_balances`, `journals`, `accounting_closes`, `open_close_issues` (`"Unavailable"` / `"No reading"` on missing sources); conditionally renders `retained_statements` (`Retained Window Statements`) and `funding_settlements` when `trading_enabled`. |
| 13 | `/reports` | `OrgReportsView` | `GET /v1/projections/org` (`org-v1`) | `Roster`, `Published Reports` (`publications` from `shared_publication`), and read-only `Delegated HR Custody` (`"Managed by CEO · Board view is read-only"`, zero Board `retire_employee` write buttons). |
| 14 | `/controls` | `RestrictionsView` | `GET /v1/projections/restrictions` (`restrictions-v1`), `pause_company`, `resume_company` (`restriction-command-v1`) | Core company restrictions (`active_causes`, `history`) + conditional `data.trading_risk` (`execution:all`, `execution:paper`, `execution:live` + `15s` quote freshness telemetry) gated on `"trading"`. |
| 15 | `/backups` | `SystemSafetyView` | `GET /v1/projections/system-safety` (`system-safety-v1`), `cut_checkpoint` (`checkpoint-command-v1`) | Checkpoints (`sourdough-checkpoint-v2`), `restore_receipts`, `store_retired`, `active_restrictions`, and 5-step `safe_custody_hold` readiness checklist (with Trading-specific `reconciliation` and `next_actions` step labels when `"trading"` is installed). |
| 16 | `/audit` | `AuditHubView` | `system-audit-v1`, `system-evals-v1`, `/v1/meta` | Tabbed hub (`?tab=log\|evals\|daemon`, default `"log"`): `events` + `conditions` + `active_fences` + `board_bindings` (`log`), `qualifications` + `activations` (`blast_radius_descendants`) + Flow 13 `offline_evaluator` (`evals`), and daemon diagnostics (`daemon`, linked from `SidebarFooter`). |
| 17 | `/settings` | `SettingsView` | Local UI prefs + `GET /v1/meta` + `REGISTERED_DOMAIN_MODULES` | Appearance (`light\|dark\|auto`), Material (`neutral\|paper\|mist`), Host Session & Privacy Lock (`host_locked`), and Installed Business Modules (`TRADING_DOMAIN_MODULE`). |

*(Fallback: unmatched routes render `<Result title="Page not found" />`.)*

---

### 3. Required UI states on every screen

Every route above implements all five standard states consistently:
1. **`empty`**: Domain-accurate empty state (e.g., `Live Execution Standby`, `"No open decisions"`). Never substitutes `$0.00` for missing closes or unmounted domains.
2. **`loading`**: Initial fetch `Skeleton` preserving the 4-slot header and `SidebarLayout`; never replaces already-verified `Data State` during background refreshes.
3. **`error`**: Explicit failure `Notice` on `incompatible` contract version or `401`/`403` access denial (with immediate `clearData()` on `401`/`403`/scope mismatch).
4. **`stale`**: Retains last verified `Data State` intact beneath `"Live connection interrupted — showing last known snapshot"` while disabling mutating controls.
5. **`locked`**: **`controls_locked`** (`bootstrap_locked`, `board_viewer`, `stale`, or unresolved command) vs **`host_locked`** (full-viewport opaque Privacy Shield `data-agent-hidden` without mutating `as_of_ns` or `stale`).

### 4. Design System & Component Binding Alignment Contract

[Tokens and layout](../../design-system/tokens-and-layout/spec.md), [Verified Yogurt component bindings](../../design-system/yogurt-components/spec.md), and [Client build contract](../../build.md) enforce:
1. **Modular IA & capability negotiation parity**: All `17` `App.tsx` route branches gated by `GET /v1/meta` (`active_domains`, `supported_projections`, `supported_commands`).
2. **4-Slot Header, `SidebarFooter` Diagnostics & Zero-Drawer invariant**: Strictly 4 `sd-topbar` slots (`Brand`, `Approvals`, `⌘K`, `Theme Toggle`), `<Status>` diagnostics in `SidebarFooter` -> `/audit?tab=daemon`, zero `Y.Sheet`/right drawers, and focus recovery on Tier 1 -> Tier 0 return.
3. **Core Compositions**: `AppShell` Modular IA, `AgentsActivityView` Graph/List, `0%`/`$0` baseline `PortfolioChart` (`null`-gap breaks + `Shadow Benchmark`), and `ControlledInlineConfirm` (*What changes* / *What stays untouched* + collapsed `"Technical details"`).
4. **Governance & Craft Discipline**: Zero Board employee-retirement confirm bindings, `#175174` reserved for human commitment CTAs, `0` gradients/static card shadows, `tabular-nums`, and `createYogurtAgent` safety guards.

## Functional and quality requirements

- **FR.** Provide deterministic route patterns, disclosure tiers, role gates, projection sources, confirmation boundaries, and 5-state specifications across all `17` mounted `App.tsx` route branches.
- **NFR.** Enforce the 4-slot header + `SidebarFooter` diagnostics, zero right slide-over drawers, Board-only 1:1 CEO dialogue, CEO-delegated offboarding (zero Board `retire_employee` write), keyboard navigability with focus recovery, and immediate DOM concealment on `clearData()` or `host_locked`.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Modular IA & zero-drawer nav | Open `/`, click an execution tape row -> `/orders/:orderId?env=...` -> `"Ask CEO"` -> `/messages`. Confirm zero `Y.Sheet`/right drawers mount, `<nav aria-label="Breadcrumb">` updates at depth `<= 2`, and Back/`Escape` restores focus. |
| Core-only vs Trading domain gating | Boot a core-only store (`active_domains = ["core"]`) vs a Trading store (`["core", "trading"]`). Confirm core-only redirects `/` -> `/agents` with zero Trading nav/routes/controls, while Trading mounts `/`, `/trading/live`, `/trading/paper`, `/trading/strategies`, and `/orders/:orderId`. |
| Role gating & delegated HR | Open `/controls`, `/approvals`, and `/reports` as `board_viewer` vs `board_control`. Confirm mutating controls gate strictly on unlocked `board_control` and `/reports` displays `"Managed by CEO · Board view is read-only"` with zero `retire_employee` write controls. |
| Five-state honesty | Exercise `empty`, `loading`, `error`, `stale`, and `locked` (`controls_locked` and `host_locked`). Confirm zero blank-wipes on `stale`, instant DOM clearance on `403`, and non-mutated `as_of_ns` under `host_locked`. |

**Oracle:** Compare route branches, DOM visibility across roles/locks, `SidebarFooter` link, and keyboard focus traces against `apps/client/src/{App,components/AppShell,domains/registry}.tsx`, [Flow observability](../flow-observability/spec.md), [Command submission](../../commands/submit/spec.md), and [Usage scenarios](../../scenarios.md).

## Operations and change review

Any new business domain module registered in `apps/client/src/domains/registry.ts` MUST gate its routes and navigation items strictly on `meta.active_domains`, preserve Core Company OS routes (`/approvals`, `/agents`, `/messages`, `/finance`, `/reports`, `/controls`, `/backups`, `/audit`, `/settings`), and keep the 4-slot `sd-topbar` header invariant intact.
