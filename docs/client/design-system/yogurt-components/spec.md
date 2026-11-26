# Verified Yogurt component bindings, domain compositions, and safety

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Tokens and layout](../tokens-and-layout/spec.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Command submission](../../commands/submit/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Bind every global chrome surface, Modular IA route (`17` route branches in `App.tsx`), custom domain composition, inline confirmation gate (`ControlledInlineConfirm`), browser-agent policy, and telemetry chart in the Board Client strictly to verified `@openboa/yogurt` (`0.2.0-local.1`) and `@ui-sdk/react` / `@ui-sdk/core` (`0.1.0-beta.13`) APIs. All components consume service read models (`GET /v1/meta`, `14` `GET /v1/projections/*`, `GET /v1/events`) and submit commands solely via `POST /v1/commands` and `GET /v1/commands/{command_id}?operation=...`. Financial KPIs and `PortfolioChart` render service-projected figures only; no domain or control-plane service depends on UI component bindings.

Input contracts: [Tokens and layout](../tokens-and-layout/spec.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Trading Desk & Strategies](../../inspect/investment-panel/spec.md) · [Agents & Activity](../../inspect/office-floor-map/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [Approvals & Custody](../../governance/approvals-and-custody/spec.md) · [Flow observability](../../inspect/flow-observability/spec.md) · [Command submission](../../commands/submit/spec.md) · [Meta and authorized read projections](../../control-plane/projections/spec.md) · [Event stream, runtime wakeups, and typed commands](../../control-plane/commands-and-events/spec.md). These are public client and service contracts, not imports of service implementations.

## Design decision and review

### 1. Verified primitive catalog, banned components, and edge behaviors

Use **only** verified public exports from `@openboa/yogurt` (`0.2.0-local.1`) and `@ui-sdk/react` / `@ui-sdk/core` (`0.1.0-beta.13`):

- **Verified `@openboa/yogurt` (`0.2.0-local.1`) exports:**
  - *Foundation & layout:* `Yogurt` (`@openboa/yogurt/yogurt`), `ProjectMark` (`@openboa/yogurt/project-mark`), `Heading`, `Text`, `Icon`, `Stack`, `Inline`, `Grid`, `Surface`, `Card`, `Divider`, `ScrollArea` (requires accessible `label`)
  - *Actions & forms:* `Button` (`onClick` drops `@ui-sdk/react/button`'s 2nd `details` arg), `IconButton`, `ButtonGroup`, `TextField`, `SearchField`, `Textarea`, `Select`, `Combobox`, `Checkbox`, `RadioGroup`, `Switch`, `Slider`, `RangeSlider`, `Calendar`, `InputOTP`
  - *Navigation & collections:* `PageHeader` (root `@openboa/yogurt` export only; render `Breadcrumb` directly above `PageHeader`), `Link`, `Breadcrumb` (natively renders `<nav aria-label="Breadcrumb">`), `NavigationList`, `Tabs`, `SegmentedControl`, `Pagination`, `FilterBar`, `Count`, `List`, `ListRow`, `EmptyResult`, `SidebarLayout` (`SidebarTrigger`, `SidebarHeader`, `SidebarContent`, `SidebarFooter`, `SidebarGroup`, `SidebarSubmenu`, `SidebarItem`)
  - *Feedback, data & decisions:* `Badge` (pass `dot={false}` on non-pure-digit strings), `Status`, `Notice` (hardcodes `role={urgent ? "alert" : "note"}`), `Skeleton`, `Progress`, `Toast`, `ToastStack`, `Result`, `RecordFacts` (renders `<section className="yg-facts"><dl>...</dl></section>`), `Table`, `DataTable` (renders `"No reading"` on nullish cells), `Chart`, `Legend`, `Receipt`
  - *Layers, identity & operations:* `DisclosureRow` (`Disclosure`), `ActionMenu`, `Popover`, `Dialog`, `Drawer` (root export only; bottom-only if ever needed), `Tooltip`, `CodeBlock`, `ResourceRow`, `WorkEvent`, `Trace`, `AgentAvatar`, `AgentRow` (`<= 44px` height cap), `Transcript`, `Message`, `ReadingAnchor`, `Composer`, `TaskList` (`@openboa/yogurt/motion-task-list`), `createYogurtAgent` (`@openboa/yogurt/agent`)
- **Verified `@ui-sdk/react` & `@ui-sdk/core` (`0.1.0-beta.13`) subpath entries:**
  - *Stable (`@stable`):* `@ui-sdk/react/button` (`import { Button as SDKButton } from "@ui-sdk/react/button"`), `@ui-sdk/react/command` (Command Palette `⌘K` inside `Dialog`), `@ui-sdk/react/alert-dialog`, `@ui-sdk/react/meter`, `@ui-sdk/react/tree`, `@ui-sdk/react/resizable`, `@ui-sdk/react/number-input`, `@ui-sdk/react/date-input`, `@ui-sdk/react/time-input`, `@ui-sdk/react/date-picker`, `@ui-sdk/react/agent`, `@ui-sdk/react/primitive`, and `@ui-sdk/core` (`definePrimitive`, `createRovingFocus`, `createFocusRecovery`, `announce`).
  - *Experimental (`@experimental` in `0.1.0-beta.13`):* `@ui-sdk/react/observe` and `@ui-sdk/core/observe` (`withSource`), `@ui-sdk/core/button` / `@ui-sdk/react/button` `ButtonActionDetails`, `spatialMove`, `createGridFocus`, `definePrimitive({ agent })`, `@ui-sdk/react/waterfall`, and `@ui-sdk/react/timeline`.

#### Banned exports, patterns, and missing-symbol prohibitions

- **Prohibited Right Slide-Over Drawers (`Sheet` / `Y.Sheet` BANNED):** `Sheet` (`Y.Sheet`), `.yg-sheet`, and all right-hand inspector panes are **strictly prohibited across 100% of Sourdough Client routes and breakpoints**. Every item selection transitions the main workspace view (`Tier 0 -> Tier 1`) with `Breadcrumb` and deterministic focus recovery; confirmations use in-place `ControlledInlineConfirm` (zero modal confirmation popups).
- **Prohibited Template Personas & Missing-Export Prohibitions:** Never use template persona variants or non-existent symbol names (`Banner`, `Alert`, `Input`, `Breadcrumbs`, `Radio`, `NumericSpinner`, `DatePicker`, `Stat`, `Metric`, `KeyValue`). Never use `StatCard` (`@openboa/yogurt/motion-stat-card`) for financial or close-incomplete telemetry (`0`-coercion risk). On `AgentAvatar`, use strictly the 13 non-template silhouettes with explicit Sourdough labels.

### 2. Complete verified component map (`AppShell` chrome + `17` mounted `App.tsx` route branches)

#### 2.1 Global Chrome (`AppShell.tsx`, `CommandOutboxPanel.tsx`, `ControlledInlineConfirm.tsx`)

| Chrome Surface | Verified Primitive & Composition Binding |
| --- | --- |
| `sd-topbar` *(Strictly 4 Slots)* | **Slot 1 — Brand:** `SidebarTrigger` + `Link` (`Company <id>` or `"Sourdough"` -> `homeMode === "core_only" ? "/agents" : "/"`, zero `"Paper"` badge).<br>**Slot 2 — Approvals Pill:** Conditional `<Button tone="brand">Approvals ({pendingApprovals})</Button>` -> `/approvals` when `pendingApprovals > 0`.<br>**Slot 3 — Command Palette (`⌘K`):** `Button` (`aria-keyshortcuts="Meta+K Control+K"`) opening `<Dialog title="Board Client Command">` wrapping `@ui-sdk/react/command` (`nav:*`, `action:theme`, `action:lock`).<br>**Slot 4 — Theme Toggle:** `IconButton` (`sun` \| `moon`) toggling light/dark appearance. |
| `SidebarLayout` + `SidebarFooter` | `SidebarHeader` (`ProjectMark project="sourdough"` + `Sourdough` / `Board Client`), top unlabelled `SidebarGroup` (`Overview` when multi-module, `Approvals` with pending `Badge`), dynamic `Trading` `DomainModule` group (`Live Trading` `G L`, `Paper Trading` `G P`, `Strategies` `G S` when `"trading"` active), `Company` group (`Agents` `G A`, `Messages` `G M`, `Finance`, `Reports`), `System` group (`Controls` `G C`, `Backups`, `Audit`, `Settings` `G ,`), and **`SidebarFooter`** showing principal role, active module badge (`Trading` or `Core OS`), and diagnostics button (`data-focus-id="sidebar-diagnostics-trigger"`) wrapping `<Status tone={statusTone}>{statusLabel}</Status>` -> **`/audit?tab=daemon`**. |
| Sub-header `Notice` & `CommandOutboxPanel` | Mounted strictly below `sd-topbar`: disconnected stale `Notice` (`"Live connection interrupted — showing last known snapshot"`), active restriction `Notice` (`"Operational restrictions are currently active"` -> `/controls`), and inline `CommandOutboxPanel` (`Card`, `Notice`, `Receipt` with `onLookup` strictly when `command_state === "unknown"`, `ButtonGroup` `Retry same request` / `Discard draft` strictly when `not_found_retry_choice`, `tabular-nums` `pending_age`). |
| `ControlledInlineConfirm` | In-place expandable `Card`/`Surface` (zero popup modals, zero `Y.Sheet`): `Stack`, side-by-side `RecordFacts` (`<dl>`) wells (**What changes** vs **What stays untouched**), consequence note, optional `Textarea` (`payload.reason`), closed-by-default `DisclosureRow` `"Technical details"`, `Notice`/`.yg-error[role="status"]` mid-review change warning, `ButtonGroup` (`Cancel` `Button` + confirm `SDKButton` from `@ui-sdk/react/button` with `data-agent-confirm` and `details?.source !== "agent"` rejection). |
| Privacy Shield | Full-viewport opaque overlay (`Surface`, `Result`, `Icon`, `Button` unlock trigger, `data-agent-hidden` concealing protected subtree when `host_locked = true` without mutating `as_of_ns` or `LinkState`). |

#### 2.2 All `17` mounted `App.tsx` route branches

| # | Route Pattern | Component (`apps/client/src/views/`) | Domain Gate & Verified Bindings |
| --- | --- | --- | --- |
| 1 | `/bootstrap` | `BootstrapView` | `PageHeader`, `Result`, `RecordFacts` (`bootstrap_challenge`), `TextField`/`InputOTP` (`data-agent-secret`), `ControlledInlineConfirm` (`register_owner`). |
| 2 | `/` | `TradingDeskView(mode="live")` \| `CompanyOverviewView` \| `AgentsActivityView` | Single module `"trading"` -> `TradingDeskView(mode="live")`; 2+ modules -> `CompanyOverviewView`; core-only -> redirects (`replace: true`) to `/agents`. |
| 3 | `/trading/live` | `TradingDeskView(mode="live")` | Gated on `tradingActive`. 4-KPI `Card` band, `PortfolioChart` (`0%`/`$0` baseline + `null`-gap breaks + `Shadow Benchmark` overlay), `pnl_reconciliation_bridge` `RecordFacts`, `open_positions`, `execution_tape` `DataTable` (row links to `/orders/:orderId?env=live`), Live governance & `promote_live` / `demote_to_paper` / `suspend_live` via `ControlledInlineConfirm` + `ContextualApprovalCard`. |
| 4 | `/trading/paper` | `TradingDeskView(mode="paper")` | Gated on `tradingActive`. Multi-book `company_paper` selector (`book_id`), symmetric 4-KPI band, `PortfolioChart`, `open_positions`, `execution_tape` (row links to `/orders/:orderId?env=paper`), `paper_soak_ready` & nomination/migration readiness. |
| 5 | `/orders/:orderId` | `OrderTraceView` | Gated on `tradingActive`; `?env=live\|paper`. `Breadcrumb` (`Trading` back-link -> `/trading/live` \| `/trading/paper` \| `/agents/:actorId`), `@ui-sdk/react/timeline` + `RecordFacts` (`trading_book_order`, `trading_book_fill`, `trading_fee_delivery`, `trading_book_position`, `work_runs`, `external_actions`), `Button` (`"Ask CEO"` -> `/messages?context_kind=trading_order`). |
| 6 | `/trading/strategies` | `StrategiesView` *(in `views/trading/`)* | Gated on `tradingActive`. `PageHeader`, `Tabs` / `DataTable` (`trading_strategy_release`, `trading_pit_catalog_manifest`, `trading_challenger_trial`, `trading_strategy_deployment`), `ControlledInlineConfirm` (`activate_strategy_release`, `rollback_strategy_deployment`). |
| 7–8 | `/approvals`, `/approvals/:questionId` | `ApprovalsView` | Core OS. `Breadcrumb` (on `:questionId`), `FilterBar`, `List`, `ContextualApprovalCard` (`RecordFacts`, `ButtonGroup` `Approve` / `Decline` / `"Ask CEO"`, `ControlledInlineConfirm` `submit_decision_answer`). |
| 9 | `/agents` | `AgentsActivityView` | Core OS. `PageHeader`, `SegmentedControl` (`Graph \| List`), `Surface` + `Card` nodes (`AgentAvatar`, `Status`, opaque `.desk` draft count, conditional sandbox pill) + clickable collaboration edges -> `/messages`. |
| 10 | `/agents/:agentId` | `AgentDetailView` | Core OS. `Breadcrumb`, `PageHeader`, `Tabs` (`Work Activity` w/ `interrupt_run` & `cancel_work`, `Interactions & Talks`, `Published Reports`, `Peer Requests & Handoffs`, conditional `Trading Sandbox` tab on `"trading.agent_sandbox"` / `"quant.agent_sandbox"` or sandbox activity). |
| 11 | `/messages` | `MessagesView` | Core OS. 2-column thread list: pinned writable `Founder · CEO` (`send_ceo_message`) + read-only `internal_channels`, `ContextAttachmentCard` (`ALLOWED_ASK_CONTEXT_KINDS`), `Transcript` + `Message` + `Composer` (zero inline command buttons). |
| 12 | `/finance` | `FinanceView` | Core OS (`account_balances`, `journals`, `accounting_closes`, `open_close_issues`, `"Unavailable"` / `"No reading"` on missing close sources) + conditional `Retained Window Statements` & `funding_settlements` when `trading_enabled`. |
| 13 | `/reports` | `OrgReportsView` | Core OS (`Roster`, `Published Reports` `ResourceRow` + `CodeBlock`, read-only `Delegated HR Custody` `"Managed by CEO · Board view is read-only"` with zero Board `retire_employee` buttons). |
| 14 | `/controls` | `RestrictionsView` | Core OS (`pause_company` / `resume_company` on `restriction_cause` via `ControlledInlineConfirm`) + conditional `data.trading_risk` (`execution:all`, `execution:paper`, `execution:live` + `15s` quote freshness readiness telemetry). |
| 15 | `/backups` | `SystemSafetyView` | Core OS (`checkpoints`, `restore_receipts`, `store_retired`, 5-step `safe_custody_hold` `TaskList`, `ControlledInlineConfirm` `cut_checkpoint`). |
| 16 | `/audit` | `AuditHubView` | Core OS (`?tab=log\|evals\|daemon`). `system-audit-v1` table, `system-evals-v1` (`ThreeStageVerdict`, `blast_radius_descendants` `@ui-sdk/react/tree`, Flow 13 `offline-trial-registry-v1`), and Daemon Diagnostics. |
| 17 | `/settings` | `SettingsView` | Core OS. Appearance (`light\|dark\|auto`), Material (`neutral\|paper\|mist`), Host Session & Privacy Lock, Installed Business Modules (`REGISTERED_DOMAIN_MODULES`). |

### 3. `ControlledInlineConfirm`, `AgentReady`, and `createYogurtAgent` / WebMCP safety spec

- **Why `ControlledInlineConfirm` uses `@ui-sdk/react/button` (`SDKButton`):** Yogurt's `Button` drops `@ui-sdk/react/button`'s second argument `details: ButtonActionDetails` (`{ source: "mouse" | "touch" | "pen" | "keyboard" | "virtual" | "programmatic" | "agent" }`). `ControlledInlineConfirm` therefore renders an in-place expandable `Card`/`Surface` with `SDKButton` (`import { Button as SDKButton } from "@ui-sdk/react/button"`) styled via `.yg-button`.
- **Five mandatory safety invariants of `ControlledInlineConfirm`:**
  1. **Revision latch (`opened.current`):** Latches `{ id, revision }` when expanded; resets when collapsed.
  2. **Mid-review mutation detection (`changed`):** Detects `opened.current.id !== decision.id || opened.current.revision !== decision.revision`, renders inline `.yg-error[role="status"]` warning, and disables confirm `SDKButton`.
  3. **One-shot submit guard (`sent.current`) & `details.source` check:** Rejects `details?.source === "agent"`, prevents double submit (`sent.current`), collapses cleanly, and hands off tracking to `CommandOutboxPanel`. Propagates `details.source` across `await` via `withSource`.
  4. **Consequence-verb CTA & `#175174` / `danger` tone:** Uses specific consequence verbs (`"Approve"`, `"Pause"`, `"Resume"`, `"Cut Checkpoint"`, `"Promote to Live"`), reserving `#175174` for non-destructive commitments and `tone="danger"` for destructive actions.
  5. **Confirm-only `data-agent-confirm`:** Attaches descriptive `data-agent-confirm` solely on the confirm `SDKButton`.
- **`AgentReady`, `createYogurtAgent`, and DOM redaction:** `createYogurtAgent({ allow })` defaults to `allow: () => false`. `data-agent-hidden` conceals `host_locked` Privacy Shield subtrees, `401`/`403` screens, and locked controls; `data-agent-secret` redacts bootstrap credentials. Raw-HTML Yogurt sub-elements omitted from `snapshot()`/`query()` (`SidebarItem`, `AgentRow`, `DataTable` sort headers, `DisclosureRow`, `ListRow`, `ResourceRow`, `Link`, `TaskList`) require independent keyboard/Playwright accessibility verification.

### 4. `PortfolioChart` & `Chart` high-baseline framing and `null`-gap honesty

Inspected in `@openboa/yogurt` `0.2.0-local.1`: `Chart` starts its axis reducer at `0`, which flattens high-baseline raw NAVs. Therefore `TradingDeskView` (`PortfolioChart`) plots service-projected **Cumulative Return (`%`, `0%` baseline)** or **Cumulative P&L (`$0` baseline)** with a dashed zero reference line and optional `Shadow Benchmark` comparison curve, displaying raw NAV and capital flows separately in `RecordFacts` and `DataTable`. Points with `value === null || !Number.isFinite(value)` break the SVG `<path>` (`M...L...`), render `<small>No reading</small>`, and populate `"No reading"` inside the collapsed `DataTable` (`StatCard` is banned for financial/close-incomplete telemetry).

## Functional and quality requirements

- **FR.** Bind `AppShell` chrome and all `17` mounted `App.tsx` route branches to verified `@openboa/yogurt` (`0.2.0-local.1`) and `@ui-sdk/react` / `@ui-sdk/core` (`0.1.0-beta.13`) exports; ban `Y.Sheet` and right slide-over drawers across 100% of routes/breakpoints; enforce all five `ControlledInlineConfirm` safety invariants, `createYogurtAgent` default-deny (`allow: () => false`) and `data-agent-hidden`/`secret` policies, and `PortfolioChart` / `Chart` `0%`/`$0` framing with `null`-gap path breaks.
- **NFR.** Zero missing-export citations, zero template persona copying, zero `0`-coercion on missing financial telemetry (`StatCard` banned), zero Board employee-retirement write controls, and full keyboard/screen-reader verification across raw-HTML composites.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Primitive export & zero-`Sheet` audit | Run `npm --prefix apps/client run typecheck` and `npm --prefix apps/client run check:yogurt`. Confirm zero `Y.Sheet`/right-drawer usage, zero `StatCard` usage on financial/sparse telemetry, and zero Board employee-retirement buttons on `/reports`. |
| `ControlledInlineConfirm` & `details.source` | Open `ControlledInlineConfirm` on `/controls` or `/approvals`; mutate `decision.revision` while open and verify `.yg-error[role="status"]` warning + disabled confirm `SDKButton`. Invoke confirm with `details.source === "agent"` and verify rejection with zero `POST /v1/commands` calls. Double-click confirm and verify `sent.current` fires once and hands off to `CommandOutboxPanel`. |
| `AgentReady` redaction & focus recovery | Run `createYogurtAgent({ allow: () => false })` on `/bootstrap`, `/agents`, and under `host_locked`. Confirm `data-agent-secret` redacts secrets, `host_locked` DOM is absent from `snapshot()`/`query()` (`data-agent-hidden`), and navigating `/agents` -> `/agents/:agentId` restores focus on Back/`Escape`. |
| `PortfolioChart` `0%`/`$0` baseline & `null`-gap | Pass `[null, 1.25, null, -0.4]` percentage-return points on `TradingDeskView`. Confirm `PortfolioChart` frames on `0%` baseline with dashed zero line, SVG `<path>` breaks at both `null` entries, and `"No reading"` renders in readings `DataTable`. |

**Oracle:** Verify TypeScript types (`npm --prefix apps/client run typecheck`), Yogurt design gate (`npm --prefix apps/client run check:yogurt`), DOM assertions, and SVG/table output against [Trading Desk & Strategies](../../inspect/investment-panel/spec.md), [Agents & Activity](../../inspect/office-floor-map/spec.md), and [Usage scenarios](../../scenarios.md).

## Operations and change review

Whenever `@openboa/yogurt` or `@ui-sdk/react` / `@ui-sdk/core` is upgraded, re-verify custom wrapper bindings (`ControlledInlineConfirm`, `PortfolioChart`), `check:yogurt`, zero-drawer guarantees, and five confirmation invariants.
