# U1 · Board Client

[System map](../README.md) · [Client architecture](architecture.md) · [Client build contract](build.md) · [Usage scenarios](scenarios.md) · [Vocabulary](../glossary.md)

The Board Client is the optional presentation and control-plane UI for Sourdough. It owns local interaction state, not read-model truth or command authority. A company runs, trades, reconciles, and recovers without this process installed or connected, and a core-only company (`installed_domains = {'core'}`) operates cleanly without any Trading domain modules mounted.

## Cross-feature architecture and verification

- [Client architecture](architecture.md) — Ownership boundaries (`Core Company OS` vs `Trading` `DomainModule` with `domainKey: "trading"`), `sourdough client-web` (`ClientWebBridge` loopback HTTP-to-UDS bridge), `apps/board/` v0 disposition, **Modular IA** (`Approvals`, dynamic `Trading`, `Company`, `System`), strict 4-slot global header + `SidebarFooter` diagnostics (`-> /audit?tab=daemon`), zero right slide-over drawers, Board-vs-CEO governance boundary, host-lock privacy shield, and design-system interface contracts.
- [Client build contract](build.md) — React 19, `@openboa/yogurt` (`0.2.0-local.1`), and `@ui-sdk/react` / `@ui-sdk/core` (`0.1.0-beta.13`) dependency isolation, CSS `@layer` cascade, visual baseline, Modular IA binding, and clean-build verification.
- [Usage scenarios](scenarios.md) — Ten end-to-end operational scenarios covering owner bootstrap, approvals & CEO messages, scoped restrictions & `15s` Trading resume cut, checkpoints & Safe Custody Hold, strategies & evals, `TradingDeskView` (`live` & `paper`) + `OrderTraceView` + `promote_live` / Shadow Benchmark, outbox recovery, disconnect vs wipe vs host lock, `AgentsActivityView` / `AgentDetailView` (`Trading Sandbox`), and core-only fallback (`/agents`).

## Projection presentation

### [Inspect authorized company state](inspect/intent.md)

- [present](inspect/present/spec.md) — Show only authorized projections at their actual revision, with readable missing, stale, and locked states, 4-slot header + `SidebarFooter` telemetry rules, and `GET /v1/events` NDJSON streaming.
- [investment-panel](inspect/investment-panel/spec.md) — Dynamic Trading module views (`/`, `/trading/live`, `/trading/paper`, `/orders/:orderId`, `/trading/strategies`) gated on `active_domains.includes("trading")`: symmetric `TradingDeskView(mode="live" | "paper")` on `trading-desk-v1` (4-KPI top band, `PortfolioChart`, `shadow_benchmark`, `pnl_reconciliation_bridge`, `open_positions`, `execution_tape`, `promote_live` / `demote_to_paper` / `suspend_live`), `OrderTraceView` on `order-trace-v1`, `StrategiesView` on `strategies-v1`, and core-only redirect to `/agents`.
- [office-floor-map](inspect/office-floor-map/spec.md) — Core Company OS agent inspection (`/agents`, `/agents/:agentId`): `AgentsActivityView` (`Graph | List` toggle, `agents-graph-v1`, clickable edges to `/messages`) and `AgentDetailView` (`agent-detail-v1` with `Work Activity`, `Interactions & Talks`, `Published Reports`, `Peer Requests & Handoffs`, conditional `Trading Sandbox` tab on `"trading.agent_sandbox"` / `"quant.agent_sandbox"`, `.desk/drafts/*` opacity, and `interrupt_run` / `cancel_work`).
- [screens](inspect/screens/spec.md) — Complete Modular IA screen matrix (`17` route branches in `App.tsx` across `Approvals`, dynamic `Trading`, `Company`, `System`, and `/bootstrap`), zero right slide-over drawers, role gates, redaction rules, and required UI states.
- [flow-observability](inspect/flow-observability/spec.md) — End-to-end observability matrix mapping Sourdough Flows `01`–`13` to Core Company OS vs Trading modules, `14` implemented projection endpoints (`15` contract revisions), `12` supported commands, `audit_event` streams, and the `17` canonical Modular IA routes.

## Board-vs-CEO governance and approvals

### [Govern through Board approvals, controls, and CEO dialogue](governance/intent.md)

- [ceo-direct-line](governance/ceo-direct-line/spec.md) — Dedicated `MessagesView` (`/messages`, `messages-v1`) hosting the writable 1:1 **`Founder · CEO`** thread (`send_ceo_message`), read-only internal channels, `Contextual Ask CEO` pinned `ContextRef` (`ALLOWED_ASK_CONTEXT_KINDS`), zero inline command buttons inside chat, and strict exclusion of non-CEO 1:1 chat.
- [approvals-and-custody](governance/approvals-and-custody/spec.md) — Dual-entry `DecisionQuestion` approval cards (`/approvals`, `/approvals/:questionId`, `ContextualApprovalCard`, `decision_question.application_status = 'failed'` on refused domain actions), read-only CEO-delegated HR custody (`/reports`, `"Managed by CEO · Board view is read-only"`), scoped controls (`/controls`), checkpoints (`/backups`, 5-step "Safe Custody Hold"), and evaluations (`/audit?tab=evals`, DAG Blast-Radius preview).

## Control-plane interaction

### [Request a governance change](commands/intent.md)

- [submit](commands/submit/spec.md) — Submit the `12` implemented typed commands (`8` Core + `4` Trading plugin) with optimistic concurrency preconditions through `ControlledInlineConfirm` (*What changes* vs *What stays untouched* + consequence note + collapsed `Technical details`), resolve lost acknowledgments via `(company_id, principal_id)`-scoped `/api/local/pending` receipts (`CommandOutboxPanel`), and enforce advisory-only `send_ceo_message` vs state-mutating commands.

## Control-plane service contracts

### [Serve the local control plane](control-plane/intent.md)

- [bridge-and-auth](control-plane/bridge-and-auth/spec.md) — UDS transport, `ClientWebBridge` (`sourdough client-web`) loopback HTTP-to-UDS security (`sd_sess_<port>_<instance_id>`, `/api/local/pending`), `apps/board/` v0 disposition, `board_binding`/`role_assignment` RBAC (`register_owner`), fail-closed non-CEO 1:1 chat enforcement, and `(principal, operation)` receipt scoping.
- [projections](control-plane/projections/spec.md) — `GET /v1/meta` capability negotiation (`active_domains`, `active_restrictions_count`) and all `14` implemented `GET /v1/projections/*` endpoints (`15` contract revisions across `18` total `/v1/*` routes: `work-timeline` `v1`/`v2`, `10` Core projections with conditional Trading enrichment, and `3` Trading domain projections).
- [commands-and-events](control-plane/commands-and-events/spec.md) — `GET /v1/events` redacted NDJSON stream, `ResidentCompanyService` + `CompanyDaemon.notify_commit` wakeups, `audit_event` emissions, `12`-command registry (`8` Core + `4` Trading plugin gated `422` when absent), `TradingRestrictionGuard` (`<= 15s` quote check), and `POST /v1/commands` / `GET /v1/commands/{command_id}?operation=...` contracts.
- [paper-boundary](control-plane/paper-boundary/spec.md) — 3-tier Execution Environment Boundary (`agent_sandbox`, `company_paper`, Board-gated `company_live`) on `execution_environment_binding` + unified `trading_book_{order,fill,position,valuation}` isolated by composite `(binding_id, environment)` FK, `positions_copied = 0`, `sender_lease` (`.writer-lock`), and strict core-only (`404`/`422`) exclusion.

## Design system and component alignment

*(Interface contracts defined in [Client architecture](architecture.md#7-design-system-interface-contracts) and [Client screens and navigation](inspect/screens/spec.md#4-design-system--component-binding-alignment-contract).)*

### [Align Client presentation with the Yogurt design system](design-system/intent.md)

- [tokens-and-layout](design-system/tokens-and-layout/spec.md) — `@openboa/yogurt` (`0.2.0-local.1`) CSS `@layer` cascade, `21 x 11` palette, typography, radii, container-query layout grammar, 4-slot header + `SidebarFooter`, zero-drawer discipline, and 5-state surface rules.
- [yogurt-components](design-system/yogurt-components/spec.md) — Verified `@openboa/yogurt` (`0.2.0-local.1`) + `@ui-sdk/react` / `@ui-sdk/core` (`0.1.0-beta.13`) primitive bindings, domain compositions, zero-`Y.Sheet` enforcement, `ControlledInlineConfirm`, `AgentReady`, and `PortfolioChart` / `Chart` behavior.
