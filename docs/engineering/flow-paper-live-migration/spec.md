# Engineering Spec: Multi-Book Company Paper Trading & Safe Paper-to-Live Migration

*(Updated by [flow-core-domain-cleanup](../flow-core-domain-cleanup/spec.md): `application_status='failed'` and `positions_copied=0` invariants are preserved; `company_paper_*` ledger tables, `paper_live_migration_record`, `execution_tiers.py`, and `/v1/projections/execution-tiers` were consolidated onto `trading_book_*`, `trading_live_migration_record`, `src/sourdough/trading/`, and `trading-desk-v1`.)*

## Intent
Enable an autonomous trading company to operate **multiple concurrent Company Paper Books** in parallel—comparing strategies with realistic fee/slippage simulation—and promote a nominated Paper Book into production **Live Trading** through a fail-closed Board-approved migration engine that never copies virtual positions into real capital.

## Authority & Data Boundaries
- **Company Paper Books (`company_paper` tier):** Multiple active `company_paper` bindings may run concurrently per company (unique per `(company_id, account_id)`), each backed by isolated rows in the unified ledger (`execution_environment_binding`, `trading_book_order`, `trading_book_fill`, `trading_book_position`, `trading_book_valuation`) owned by `src/sourdough/trading/` (superseding the retired `company_paper_*` tables and `execution_tiers.py`).
- **Single Active Production Live Binding (`company_live` tier):** At most one `company_live` binding may be `active` per company at any time.
- **Board Approval Gate:** Nominating a Paper Book (`propose_paper_to_live_migration`) creates a `decision_question(kind='live_promotion')` visible both contextually (`Paper Trading`, `Live Trading`) and in the unified `Approvals` inbox. Approving from either entry dispatches the identical `submit_decision_answer` command, triggering `board.apply_decision` -> `execute_paper_to_live_migration`.
- **Zero Position Copying Invariant:** `trading_live_migration_record.positions_copied` (renamed from `paper_live_migration_record`) is schema-constrained to `0`. Real capital always starts flat under the target live venue's exposure ceiling.

## Allowed & Prohibited Behavior
- **Allowed:**
  - Operating `N >= 1` active `company_paper` books simultaneously with distinct `book_name`, `strategy_release_id`, starting capital, exposure limits, fee bps, and slippage bps.
  - Nominating a soaked Paper Book for Live migration via `propose_paper_to_live_migration`.
  - Executing migration in `clean_start` (no active incumbent live binding) or `drain_incumbent` (safely retiring an active incumbent live binding only when zero unresolved/uncertain orders exist).
  - Transitioning the source Paper Book post-migration to `shadow` (`keep_shadow`, continuing as a live benchmark linked via `shadow_of_live_binding_id`) or `retired` (`retire_source`).
- **Prohibited:**
  - Copying virtual paper positions or paper fills into a real live venue account.
  - Migrating to Live while any company/execution/live `restriction_cause` is active.
  - Migrating to Live while any `trading_book_order` on the retiring bindings is in `'open'` / `'uncertain'` state (or `'working'`/`'pending_cancel'` during `drain_incumbent`).
  - Migrating without source book soak readiness, a matching `PASS` `external_test` qualification receipt for the current trading core digest, or a valid opaque `live` credential handle.
  - Allowing an agent to activate `company_live` without explicit Board approval.

## Lifecycle Failures & Compatibility
- If a risk restriction is raised or an uncertain order appears between nomination and Board approval, `execute_paper_to_live_migration` refuses fail-closed and `board.apply_decision` records `application_status='failed'` without activating Live.
- `trading-desk-v1.governance` emits `paper_soak_ready` alongside the backward-compatible `company_paper_soak_ready` alias and `migration_history` arrays.

## Requirement-Linked Acceptance Evidence
- Unit tests (`tests/unit/test_paper_live_migration.py`):
  - Multiple concurrent `company_paper` books execute independent orders/fills/cash/P&L without cross-contamination.
  - `propose_paper_to_live_migration` -> `execute_paper_to_live_migration` (`clean_start` + `keep_shadow`) activates Live, marks source book `shadow`, records `positions_copied=0`, and rejects migration when active restriction, uncertain order, missing soak, or active incumbent under `clean_start` is present.
  - `drain_incumbent` safely retires incumbent live binding and swaps when zero unresolved orders exist, while refusing if an unresolved order is present.
- Connected Flow E2E (`tests/flows/flow-paper-live-migration/test_flow.py`):
  - Boots `ResidentCompanyService` + `ClientWebBridge`, runs 3 concurrent Paper Books, nominates Book #2, approves via `submit_decision_answer` over HTTP bridge, verifies automatic `board.apply_decision` executes safe migration, activates Live, transitions Book #2 to `shadow` benchmark, and surfaces updated state in `/api/v1/projections/trading-desk` (superseding `/api/v1/projections/execution-tiers`).
- Client build & design gates: `npm --prefix apps/client run build` and `npm --prefix apps/client run check:yogurt` pass cleanly.
