# Present Trading Desk, Order Trace, and Strategies

Design draft · Verification **UNRUN** · Responsible component: Board Client (`apps/client/src/views/trading/{TradingDeskView,OrderTraceView,StrategiesView}.tsx`, `apps/client/src/domains/registry.ts`)

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Agents & Activity](../office-floor-map/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Provide the dynamically mounted **Trading** domain views (`TRADING_DOMAIN_MODULE`, `domainKey: "trading"` in `apps/client/src/domains/registry.ts`), gated strictly on `active_domains.includes("trading")` in `GET /v1/meta`:
- **`TradingDeskView(mode="live" | "paper")`:** Mounted at `/` (when `"trading"` is the single active domain module) and `/trading/live` for Live (`?environment=live`), and `/trading/paper` for Paper (`?environment=paper&book_id=...`), backed by `GET /v1/projections/trading-desk` (`trading-desk-v1`).
- **`OrderTraceView`:** Mounted at `/orders/:orderId` (`?env=live|paper`), backed by `GET /v1/projections/orders/{order_id}` (`order-trace-v1`).
- **`StrategiesView`:** Mounted at `/trading/strategies`, backed by `GET /v1/projections/strategies` (`strategies-v1`).

When `"trading"` is absent from `active_domains` (core-only company), `TRADING_DOMAIN_MODULE` is not mounted, `/` redirects (`replace: true`) to `/agents`, and Trading projections return `404` on `CompanyDaemon`.

Input contracts: [Base presentation](../present/spec.md) · [3-tier execution environment boundary](../../control-plane/paper-boundary/spec.md) · [Quant trace projection](../../../company/profiles/quant/fund-books/decision-trace/project/spec.md) · [Return projection](../../../company/profiles/quant/fund-books/fund-economics/projection/spec.md) · [Strategy comparison](../../../company/profiles/quant/investments/research/compare-operating-revisions/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. Symmetric 3-tier isolation & Live Standby vs Active Live

- **No cross-tier commingling:** `trading-desk-v1` strictly scopes `environment` to `"live"` or `"paper"` (`400` otherwise), reading unified `execution_environment_binding` + `trading_book_{order,fill,position,valuation}` rows isolated by composite `(binding_id, environment)` foreign keys. Paper capital, Live capital, and Tier 1 `agent_sandbox` capital never sum or mix.
- **Multi-book Paper switching (`/trading/paper`):** Operators can inspect all concurrent `company_paper` books or filter to a specific `book_id` (`?environment=paper&book_id=...`).
- **Live Execution Standby vs Active Live (`/`, `/trading/live`):** When no `company_live` binding is active yet, `TradingDeskView(mode="live")` renders `Live Execution Standby` with a flat `0%` baseline chart, eligible `live_candidates` (`governance.paper_soak_ready` alongside backward-compatible `company_paper_soak_ready`, matching `PASS` `venue_qualification_receipt`, server-computed `promotion_payload_hash`), and `ContextualApprovalCard` for nominated `live_promotion` questions.
- **Board-gated promotion (`promote_live`) & `Shadow Benchmark`:** When promoted (`positions_copied = 0`), `TradingDeskView` renders `environment_promotion_record.action === "promote_live"` as a promotion success event, starts Live flat at `0.0%`, and overlays the source `shadow` paper book onto Live's `PortfolioChart` via `shadow_benchmark`. Live controls (`promote_execution_environment`, `demote_live_environment`) confirm inline via `ControlledInlineConfirm`.

### 2. Symmetric 4-KPI top band & `PortfolioChart` (`0%` / `$0` baseline with `null`-gap breaks)

Both `Live Trading` and `Paper Trading` render the identical 4-KPI top band (`tabular-nums` primary value + `source · as_of_ns · coverage` subtext), Two-Layer P&L reconciliation bridge (`pnl_reconciliation_bridge`: `Gross Trading Return -> Venue Fees -> Booked Funding -> Trading BOR Net -> Agent LLM & Operating Opex -> Company Net P&L`), `open_positions`, and `PortfolioChart`:
- **Honest missing coverage (`"Unavailable"` / `"No reading"`):** Whenever `CloseIssue`, missing funding, or missing cost receipts leave `pnl_minor === null`, `time_weighted_return === null`, or `operating_cost_atoms === null`, summary facts render `"Unavailable"` and table cells render `"No reading"`—never `$0.00` or `0.00%`.
- **`0%` / `$0` baseline cumulative chart with `null`-gap breaks:** `PortfolioChart` plots cumulative `%` (`0%` baseline) or `$0` P&L with a dashed zero reference line and optional `Shadow Benchmark` series, breaking SVG `<path>` segments across `null` intervals.

### 3. Grouped causal `execution_tape`, `cancel != flat` & `OrderTraceView` (`/orders/:orderId`)

- **Grouped `execution_tape` & `cancel != flat`:** Renders `Ordered`, `Filled`, `Remaining Open` (`0` once terminal), `Order Status`, `Released Reservation`, and `Resulting Net Position` (`> 0` highlighted when partial fills remain open after remainder cancellation).
- **Order trace links:** Each `execution_tape` row links to `/orders/${encodeURIComponent(o.order_id)}?env=${mode}` (zero right slide-over drawers).
- **`OrderTraceView` (`/orders/:orderId`, `order-trace-v1`):** Displays causal lineage across `trading_book_order`, `trading_book_fill`, `trading_fee_delivery`, `trading_book_position`, `execution_environment_binding`, `work_runs`, `external_actions`, and `shared_publication` for `paper`, `live`, and `agent_sandbox` orders. Uses `back_link` (`/trading/live`, `/trading/paper`, or `/agents/{actor_id}`) and `"Ask CEO"` (`AskContext` kind `"trading_order"` -> `/messages?context_kind=trading_order&context_id=...`), restoring keyboard focus on Back/`Escape`.

### 4. `StrategiesView` (`/trading/strategies`, `strategies-v1`)

Displays `trading_strategy_release`, `trading_pit_catalog_manifest`, `trading_challenger_trial` (`PROMOTE | REJECT | INCONCLUSIVE`), and `trading_strategy_deployment`. `PROMOTE` never auto-activates; author self-activation is blocked; `activate_strategy_release` and forward `rollback_strategy_deployment` confirm inline via `ControlledInlineConfirm` while preserving open orders, fills, and costs.

**Concern for the responsible component:** Never commingle `paper`, `live`, and `agent_sandbox` books; never treat missing close/cost figures as `$0.00`; never mask open partial-fill positions behind `"cancelled"`; and never render Trading views on core-only companies.

## Functional and quality requirements

- **FR.** Dynamically mount `TradingDeskView` (`/`, `/trading/live`, `/trading/paper`), `OrderTraceView` (`/orders/:orderId`), and `StrategiesView` (`/trading/strategies`) via `TRADING_DOMAIN_MODULE` (`domainKey: "trading"`), supporting multi-book Paper, Live Standby & active Live with `Shadow Benchmark`, `promote_live` promotion events, `cancel != flat`, `0%`/`$0` `null`-gap charts, and 3-tier `order-trace-v1` back-links.
- **NFR.** Enforce `"Unavailable"` / `"No reading"` honesty, clean redirect to `/agents` on core-only companies, and zero right slide-over drawers.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Live & Paper symmetry + `Shadow Benchmark` | Open `/trading/paper?book_id=...` and `/trading/live` (`/`). Verify symmetric 4-KPI cards, `PortfolioChart`, `pnl_reconciliation_bridge`, `open_positions`, and `execution_tape` with zero capital commingling; after `live_promotion` (`positions_copied = 0`), verify `promote_live` success event and `Shadow Benchmark` curve. |
| `OrderTraceView` & `cancel != flat` | Execute a partial fill + remainder cancellation; verify `Remaining Open = 0` with `Resulting Net Position > 0`. Click the tape row -> `/orders/:orderId?env=...`, verify `trading_book_*` lineage, `"Ask CEO"` (`kind: "trading_order"`), and environment-aware back-link. |
| Missing coverage & core-only exclusion | With an open `CloseIssue`, verify `"Unavailable"` / `"No reading"` and `null`-gap chart breaks (zero `$0.00`). On a core-only store (`active_domains = ["core"]`), verify `/` redirects to `/agents` and Trading nav/routes are absent. |

**Oracle:** Compare rendered values, `promote_live` records, `cancel != flat` positions, `0%`/`$0` chart segments, and `order-trace-v1` payloads against `src/sourdough/trading/projections.py` and `apps/client/src/views/trading/`.

## Operations and change review

Monitor projection revision mismatch separately from venue and book health. Roll back presentation, not financial state.
