# Board Client 3-Tier Execution Accounts & Live Promotion UI (`flow-tier-ui`)

Status: **Superseded** by [flow-modular-ia](../flow-modular-ia/spec.md) and [flow-core-domain-cleanup](../flow-core-domain-cleanup/spec.md) · Original branch: `feat/flow-tier-ui` · Note: `ExecutionTiersView.tsx`, `/company/execution-tiers`, `/v1/projections/execution-tiers`, and `LiveTradingView.tsx` were removed and consolidated onto `TradingDeskView` (`/`, `/trading/live`, `/trading/paper`) and `trading-desk-v1`.

## 1. Intent

Connect Sourdough's three-tier execution architecture—Tier 1 per-Agent sandboxes (`agent_sandbox`), Tier 2 company paper (`company_paper`), and Tier 3 production live (`company_live`)—directly into the Board Client UI using `@openboa/yogurt` primitives and inline human confirmation (`ControlledInlineConfirm`).

Board operators can inspect every Agent's isolated Tier 1 sandbox portfolio, monitor Tier 2 company paper soak readiness and venue qualification receipts, and perform fail-closed Tier 3 live promotion or demotion directly from the browser without popup modals or client-side hash re-implementation.

## 2. Authority & Data Boundaries

- **Server-authoritative promotion digest:** `execution_tiers.read_execution_tiers_snapshot` enriches each `live_candidates` item with `matching_qualification_receipt_id` (newest passing receipt matching `connector_revision`, `instrument_rules_digest`, and `current_core_digest`) and `promotion_payload_hash` (computed via `build_promotion_payload_hash`), plus `charter_revision`. The frontend never synthesizes or bypasses promotion hashes.
- **Human-only inline confirmation:** Both `promote_execution_environment` and `demote_live_environment` (`execution-tier-command-v1`) execute strictly through `ControlledInlineConfirm` (`@ui-sdk/react/button` human-click guard, explicit `What changes` / `What stays untouched`, required audit `Decision reason`, and `CommandOutboxPanel` idempotency).
- **Read-only isolation for Tier 1 sandboxes:** `AgentDetailView` and `ExecutionTiersView` display per-Agent Tier 1 sandbox balances, fees, slippage, positions, and orders strictly as read-only projections; Agents' private Desks and sandboxes remain isolated from company funds.
- **Yogurt 0.2.0 design compliance:** All new UI surfaces compose strictly from `@openboa/yogurt` primitives, official CSS tokens (zero literal color fallbacks, zero gradients, zero static card shadows), `@container` responsive grids, and `tabular-nums` telemetry.

## 3. UI Surfaces Added / Updated

1. **Execution Tiers View (`/company/execution-tiers`, `apps/client/src/views/ExecutionTiersView.tsx`):**
   - 3-tier overview cards (Tier 1 Agent Sandboxes, Tier 2 Company Paper, Tier 3 Company Live).
   - Live Promotion Gate & Candidate bindings with explicit prerequisite indicators (Paper Soak, PASS Qualification Receipt, Trading Core Digest, Board Control Role) and inline `Promote to Live` / `Demote to Paper` confirmation panels.
   - Tier 1 Per-Agent Sandboxes table linking to each Agent's detail page.
   - Venue Qualification Receipts table and Promotion / Demotion audit history list.
2. **Agent Detail Tier 1 Sandbox Tab (`apps/client/src/views/AgentDetailView.tsx`):**
   - Renders `activeData.agent_sandbox` (account binding, cash, gross exposure, realized PnL, fees/slippage, open positions, and recent simulated orders).
3. **Navigation & Entry Points (`App.tsx`, `AppShell.tsx`, `LiveTradingView.tsx`):**
   - Sidebar item `Execution Tiers` under `Company`, Command Palette action (`G E`), and quick link from `LiveTradingView`.

## 4. Acceptance Evidence

- Frontend gates: `npm --prefix apps/client run build` (`tsc --noEmit && vite build`) and `npm --prefix apps/client run check:yogurt` pass cleanly.
- Python unit tests: `tests/unit/test_tier_ui_projection.py` verifying server-computed `promotion_payload_hash`, `matching_qualification_receipt_id`, `charter_revision`, and `agent_display_name` enrichment.
- Connected flow test: `tests/flows/flow-tier-ui/test_flow.py` (`ConnectedFlow.test_end_to_end`) verifying `ClientWebBridge` serves `/api/v1/projections/execution-tiers`, `/api/v1/projections/agents/{id}` (`agent_sandbox`), and executes live `promote_execution_environment` + `demote_live_environment` round-trips over HTTP-to-UDS.
