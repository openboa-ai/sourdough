# Engineering Spec: Unified TradingBook Ledger & Symmetric Live/Paper Trading Desk

## Intent
Eliminate structural asymmetry between `Live Trading` and `Paper Trading` by unifying company trading books across `paper` and `live` environments under one canonical ledger, one projection contract (`GET /v1/projections/trading-desk`), and one shared Board Client surface (`TradingDeskView(mode="live" | "paper")`). The only difference between Paper and Live is the execution venue adapter (simulated venue vs external exchange), the Board activation/kill-switch authority boundary, and the environment-scoped capital ledger.

## Authority & Data Boundaries
- **Physical Environment Isolation:** Every row in `trading_book_order`, `trading_book_fill`, `trading_book_position`, and `trading_book_valuation` carries `environment TEXT NOT NULL CHECK (environment IN ('paper', 'live'))` and a composite foreign key `(binding_id, environment) REFERENCES execution_environment_binding(binding_id, environment)` so Paper rows can never attach to a Live binding or vice versa.
- **Explicit Environment Scoping:** `read_trading_desk_projection(store, company_id, *, environment, book_id=None)` strictly requires `environment in ("live", "paper")`. Paper capital and Live capital are never summed or commingled.
- **Per-Agent Sandbox Isolation Preserved:** Employee personal sandboxes (`agent_sandbox_*`) remain physically separate tables outside company books.
- **Zero Virtual Position Carryover:** Migrating a Paper Book to Live initializes the Live Book with a flat `0.0%` baseline `trading_book_valuation` (`positions_copied = 0`) and records `source_paper_binding_id` on the Live Book so Live performance can be compared directly against its `Shadow Benchmark` Paper Book.

## Allowed & Prohibited Behavior
- **Allowed:**
  - Recording orders, fills, positions, and time-series valuations (`trading_book_valuation`) identically for both `company_paper` and `company_live` books.
  - Recording real external exchange fills (`external_order_id`, `external_fill_id`) and periodic NAV valuations into an active `company_live` book via `record_live_book_order_fill` / `record_trading_book_valuation`.
  - Inspecting any single Paper Book (or all Paper Books aggregate) in `Paper Trading` (`/trading/paper`) with live interactive KPI cards, cumulative return chart, open positions, and execution tape.
  - Inspecting Live Trading (`/`) with the identical 4 KPI cards, `Live Cumulative Performance` chart (including optional `Shadow Benchmark` overlay curve comparing real Live execution vs migrated Paper Book drift), Live Open Positions, Live Execution Tape, and bottom/contextual Live Venue & Qualification governance controls.
  - Viewing `Live Trading` in `Paper-Only Safe Mode` (standby) with a clean `0%` baseline chart state explaining live capital is gated until a nominated Paper Book is approved.
- **Prohibited:**
  - Commingling `paper` and `live` NAV, P&L, fills, or positions in any KPI, chart, or reconciliation bridge.
  - Recording a `company_live` order or fill against a non-active, suspended, retired, or `paper` binding.
  - Bypassing Board promotion/migration gates to activate a `company_live` binding.

## Lifecycle Failures & Compatibility
- Backwards compatible with all legacy `paper_binding`, `paper_order`, `paper_fill`, `paper_valuation`, `company_paper_*`, `/v1/projections/live-trading`, and `/v1/projections/execution-tiers` contracts.
- Existing `open_company_paper_book` and `submit_company_paper_book_order` automatically record initial (`T0`) and post-fill `trading_book_valuation` cuts so every concurrent Paper Book immediately renders a rich cumulative return curve!
- `execute_paper_to_live_migration` automatically seeds the activated Live Book's flat `T0` baseline valuation (`return_pct = 0.0%`, `positions_copied = 0`) so Live Trading's equity curve begins cleanly at the exact moment of migration!

## Requirement-Linked Acceptance Evidence
- Unit tests (`tests/unit/test_unified_trading_desk.py`):
  - Composite `(binding_id, environment)` FK rejects attaching a `paper` order/fill/valuation to a `live` binding or vice versa.
  - `submit_company_paper_book_order` and `record_live_book_order_fill` both emit independent orders, fills, positions, and `trading_book_valuation` time-series points without commingling `paper` and `live` projections.
  - `read_trading_desk_projection(..., environment="live")` returns Live KPIs, Live `cumulative_return_series`, `shadow_benchmark_series` linked to the migrated Paper Book, Live positions, and Live execution tape; `read_trading_desk_projection(..., environment="paper", book_id=...)` switches cleanly between individual Paper Books and aggregate view.
- Connected Flow E2E (`tests/flows/flow-unified-trading-desk/test_flow.py`):
  - Boots `ResidentCompanyService` + `ClientWebBridge`, runs 3 concurrent Paper Books with valuations, migrates Book #1 to Live via Board approval, records real Live fills & valuations on the active Live Book plus parallel Shadow fills on Book #1, and verifies over HTTP `GET /api/v1/projections/trading-desk?environment=live` and `?environment=paper&book_id=...` that both environments return complete symmetric charts, KPIs, positions, execution tapes, and Live-vs-Shadow benchmark comparison without mixing capital.
- Client verification: `tsc --noEmit && vite build` and `yogurt check` pass with 0 errors.
