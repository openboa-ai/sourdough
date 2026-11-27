# 3-tier execution environment boundary

Design draft · Verification **UNRUN** · Responsible component: Trading Domain & Control Plane Boundary (`src/sourdough/storage/schema/v1/trading/`, `src/sourdough/trading/domain.py`, `src/sourdough/trading/accounts.py`, `src/sourdough/trading/ledger.py`, `src/sourdough/trading/migration.py`, `src/sourdough/trading/risk.py`, `src/sourdough/trading/sender_lease.py`, `src/sourdough/trading/venue_coordinator.py`)

[Feature intent](../intent.md) · [Meta and read projections](../projections/spec.md) · [Events and commands](../commands-and-events/spec.md) · [Trading Desk & Strategies](../../inspect/investment-panel/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Define the unified 3-tier execution environment and control-plane boundary separating Core Company OS (`installed_domains = {'core'}`) from the opt-in **Trading** domain (`installed_domains` `'trading'`, `storage/schema/v1/trading/01..03.sql`), and isolating **Tier 1 `agent_sandbox`**, **Tier 2 `company_paper`**, and **Board-gated Tier 3 `company_live`** inside one unified schema (`execution_environment_binding` + `trading_book_{order,fill,position,valuation}`).

*(Supersedes the earlier paper-only lock specification: legacy tables `paper_binding`, `paper_order`, `paper_fill`, `paper_valuation`, `paper_risk_state`, `company_paper_*`, and `agent_sandbox_*` are retired and removed.)*

Input contracts: [Trading Desk & Strategies](../../inspect/investment-panel/spec.md) · [Bind the simulated account](../../../company/profiles/quant/trading/paper-account/intent.md) · [Execution environment](../../../company/profiles/quant/trading/execution-environment/intent.md) · [Meta and read projections](../projections/spec.md) · [Events and commands](../commands-and-events/spec.md). These are public service contracts, not imports of client UI packages.

## Design decision and review

### 1. Core-only exclusion & opt-in `'trading'` activation

- **Core Company OS isolation**: Founding a company (`found()`) installs `'core'` only (`52` `STRICT` tables in `storage/schema/v1/core/01..08.sql`). Core Python modules contain zero Trading imports, zero `trading_book_*` queries, and zero hardcoded trading/paper assumptions.
- **Fail-closed control-plane gating**: On core-only stores (`"trading"` not in `active_domains`), `CompanyDaemon` omits all Trading projections and commands from `GET /v1/meta`, returns `404` on `/v1/projections/trading-desk`, `/v1/projections/orders/*`, and `/v1/projections/strategies`, and rejects Trading commands and `execution:*` / `trading_risk_pause` restrictions with HTTP `422`.
- **Canonical domain key**: `storage/schema.py:canonical_domain_name` normalizes legacy `"paper"` API inputs to `"trading"` while new stores record `"trading"` exclusively.

### 2. Three-tier isolation via composite `(binding_id, environment)` foreign keys

All three execution tiers persist into the unified `trading_book_{order,fill,position,valuation}` ledger in `storage/schema/v1/trading/01_execution_tiers_and_books.sql` without ever commingling tiers or capital:

| Tier | `tier` | `environment` | Scope & SQLite / Service Isolation Invariant |
| --- | --- | --- | --- |
| **Tier 1 · Agent Sandbox** | `agent_sandbox` | `agent_sandbox` | Per-Agent virtual sandbox (`CHECK ((tier = 'agent_sandbox') = (owner_agent_id IS NOT NULL))`). Surfaced solely under `/agents/:agentId` (`AgentDetailView` `Trading Sandbox` tab) and `order-trace-v1` (`back_link = /agents/{actor_id}`). Zero writes to company paper/live books or corporate GL trading postings. |
| **Tier 2 · Company Paper** | `company_paper` | `paper` | Supports `N >= 1` concurrent company paper books (`book_name`, `strategy_release_id`, `book_role IN ('active', 'nominated', 'shadow', 'retired')`, `shadow_of_live_binding_id`), backed by simulated/replay venues (`fixed-quote-v1`, `nautilus-spot-sandbox-v1`, `nautilus-offline-spot-replay-v1`). |
| **Tier 3 · Company Live** | `company_live` | `live` | At most one `active` `company_live` binding per company. Activates **only** via fail-closed Board promotion (`promote_execution_environment` or `decision_question(kind='live_promotion')` -> `execute_paper_to_live_migration`), requiring passing `venue_qualification_receipt`, soak evidence, clean open orders/external actions, and `trading_live_migration_record.positions_copied = 0`. |

Every row in `trading_book_order`, `trading_book_fill`, `trading_book_position`, and `trading_book_valuation` carries `environment TEXT NOT NULL CHECK (environment IN ('agent_sandbox', 'paper', 'live'))` and composite `FOREIGN KEY (binding_id, environment) REFERENCES execution_environment_binding(binding_id, environment)`, enforcing strict tier-to-environment isolation at the SQLite kernel level.

### 3. Swappable venue adapters, `sender_lease`, `IndependentProcessGuard` & `15s` risk guard

- **Swappable execution adapters (`src/sourdough/trading/venue.py`, `nautilus_paper.py`)**: Simulated (`fixed-quote-v1`, `nautilus-spot-sandbox-v1`, `nautilus-offline-spot-replay-v1`) and qualified external (`external_test` / `live`) venues share the same `trading_book_*` ledger, order-admission, reconciliation, and `order-trace-v1` contracts while keeping venue journals physically separate from `company.sqlite` (`IndependentProcessGuard`: `require_independent_from(store)`).
- **Single-sender lock (`src/sourdough/trading/sender_lease.py:13-48`)**: `hold_sender_lease(journal)` / `verify_sender_lease` locks `journal.with_suffix(".writer-lock")` (`0o600`, `O_NOFOLLOW`, `fcntl.flock(LOCK_EX | LOCK_NB)`) keyed by `(st_dev, st_ino)` before worker dispatch and restore settlement.
- **Bounded venue segments (`src/sourdough/trading/venue_coordinator.py`)**: Partitions managed paper operating windows (`8..600s` segments, `5s` drain margin) without holding `WriterLease`.
- **Scoped risk gating & `15s` quote freshness (`src/sourdough/trading/risk.py`)**: `TradingRestrictionGuard` gates `execution:all`, `execution:paper`, and `execution:live` (`cause_category = 'trading_risk_pause'`) via core `restriction-command-v1`, verifying zero open `trading_book_order` rows, zero unresolved `external_actions` for `live`, and `<= 15s` quote freshness (`QUOTE_FRESHNESS_MAX_NS = 15 * 10**9`) inside `BEGIN IMMEDIATE` on resume.
- **`cancel != flat` & missing-coverage honesty**: Partial-fill remainder cancellations release unfilled reservation (`open_reservation_minor_units = 0`) while retaining open `trading_book_position` quantity (`ordered - cancelled_remainder > 0`). Missing close/cost figures serialize as JSON `null` (`"Unavailable"` / `"No reading"`), never `$0.00`.

## Functional and quality requirements

- **FR.** Enforce strict Core Company OS exclusion (`404` projections / `422` commands when `"trading"` is absent) and strict composite `(binding_id, environment)` isolation across `agent_sandbox`, `paper`, and `live` in SQLite and `trading-desk-v1`.
- **FR.** Enforce Board-gated `company_live` promotion (`venue_qualification_receipt`, soak evidence, clean open orders/external actions, `positions_copied = 0`, `environment_promotion_record.action IN ('promote_live', 'suspend_live', 'demote_to_paper')`), keeping `agent_sandbox` and `company_paper` active in parallel.
- **NFR.** Preserve `cancel != flat` accounting, `sender_lease` `.writer-lock` process isolation, `TradingRestrictionGuard` `<= 15s` server-side quote freshness, and zero client-side P&L fabrication.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Core-only exclusion | Query `/v1/projections/trading-desk?environment=live` or submit `promote_execution_environment` / `pause_company` (`scope_ref="execution:paper"`) on a store with `installed_domains = {'core'}`. Confirm `404` / `422` before any Trading table access. |
| Composite `(binding_id, environment)` FK isolation | Attempt to insert a `paper` `trading_book_order` against a `live` or `agent_sandbox` `binding_id`. Confirm SQLite FK rejection. |
| Zero position copy & `promote_live` | Promote a soaked `company_paper` book to `company_live` via `live_promotion` -> verify `trading_live_migration_record.positions_copied == 0`, Live starts flat at `0.0%`, `environment_promotion_record.action == "promote_live"`, and source paper book transitions cleanly to `shadow` or `retired`. |
| `TradingRestrictionGuard` `15s` cut | Attempt `resume_company` on `execution:live` with an open order, unresolved external action, or `> 15s` stale quote -> confirm fail-closed refusal keeping the restriction active. |

**Oracle:** Inspect `src/sourdough/storage/schema/v1/trading/01_execution_tiers_and_books.sql`, `src/sourdough/trading/{domain,accounts,ledger,migration,risk,sender_lease,venue_coordinator,plugin}.py`, and `src/sourdough/ipc/server.py`.

## Operations and change review

All Trading tables live under `storage/schema/v1/trading/` and are installed only via `enable_domain_v1(conn, "trading")`. Legacy pre-cleanup stores must run `sourdough migrate-store` (`migrate_v1_store_to_canonical`) atomically before opening.
