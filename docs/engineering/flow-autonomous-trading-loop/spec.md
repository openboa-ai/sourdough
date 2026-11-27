# Autonomous Trading Execution Loop, Market Quotes & Live Reconciliation

Status: Scoped design specification (`flow-autonomous-trading-loop`, base `main@7cad0b25`)

## 1. Intent & Product Outcome

Close the operational loop between the autonomous resident scheduler (`ResidentCompanyService`), multi-provider Agent shifts (`AgentAdapterRegistry` + `OuterToolBroker`), real-time market prices, unified `TradingBook` ledgers (`company_paper` and Board-approved `company_live`), and external venue reconciliation (`ExternalVenueAdapter` / `BinanceVenueAdapter`):

1. **Client-free autonomous operation:** With the Board Client closed, an authorized Trading Agent wakes on a `quant.paper_decision` (or `trading.decision`) shift, queries authoritative market quotes from the host, inspects active Company Paper / Live books, submits paper or Board-approved live orders through server-bound broker tools, optionally proposes a soak-ready Paper Book for Board-approved Live migration, and arms its own next timed wake condition (`work_conditions`).
2. **Real mark-to-market valuations:** A scheduled or triggered `quant.reconcile` (or `trading.reconcile`) duty reads fresh prices from the configured `MarketQuoteProvider` and records `trading_book_valuation` cuts across active `company_paper` books (including Shadow Benchmarks) and active `company_live` books so equity curves, drift readouts, and `QUOTE_FRESHNESS_MAX_NS` stay continuously current.
3. **Crash-safe external Live order routing & reconciliation:** Live orders go through a two-phase durable `external_actions` outbox (`prepared` → `in_flight` → `confirmed` | `rejected` | `unknown`) before touching the external venue network. Network timeouts settle to `unknown` (never duplicate-submitting; blocking live risk resumption until reconciled via `query_order` / `query_fills`). Unattributed external fills or positions detected during `reconcile_external` automatically record a scoped `restriction_cause` (`execution:live`) halting live execution fail-closed until the Board investigates and clears it.

## 2. Authority & Data Boundaries

- **Core Company OS isolation preserved:** Zero trading imports or table references are added to Core modules (`core/`, `runtime/engine.py`, `runtime/handlers.py`, `ipc/server.py`, `ipc/projections.py`). All trading logic stays inside `src/sourdough/trading/`.
- **Single-owner SQL writes preserved (`storage/ownership.py`):**
  - `trading_book_*` mutations remain exclusively in `trading/ledger.py`.
  - `external_actions` mutations remain exclusively in `storage/kernel.py` (`prepare_external_action`, `settle_external_action`).
  - `restriction_cause` mutations remain exclusively in `board_control.py` (via a new system-attributed `record_system_restriction` helper with no principal spoofing).
  - `work_responsibilities` and `work_conditions` mutations remain exclusively in `runtime/work_kernel.py`.
- **Server-bound Agent identity:** `CompanyTradingTools` fixes `agent_id`, `company_id`, `run_id`, and `grant_scope` from the host `DutyContext`. Any tool argument containing `agent_id`, `company_id`, `actor_id`, `run_id`, or `environment` override is rejected fail-closed.
- **No Agent-fabricated market prices:** `submit_company_paper_order` resolves `market_price_minor_units` and `quote_observed_at_ns` exclusively from the host `MarketQuoteProvider` and rejects missing or stale quotes (`> max_quote_age_ns`).
- **No Agent-initiated Live activation:** Agents may only call `propose_live_migration` (creating a pending Board `decision_question`); activating or promoting `company_live` remains strictly Board-only.
- **Live credential boundary:** Live adapters are resolved solely through host-configured `live_venue_resolver` using opaque `keychain:` or `envref:` handles (`trading/credentials.py`). Raw API keys never enter SQLite, tool responses, or Agent Desks.

## 3. Modules & Contracts

### 3.1 Read-Only Market Quote Port (`src/sourdough/trading/quotes.py`)
- `MarketQuote(instrument_id, price_minor_units, observed_at_ns, source)` with strict validation (`price_minor_units > 0`, `observed_at_ns > 0`, canonical symbol normalization e.g. `BTC-USDT` ↔ `BTCUSDT`).
- `MarketQuoteProvider` protocol (`latest_quote`, `latest_quotes`).
- Implementations:
  - `StaticQuoteProvider`: deterministic offline/test quotes.
  - `MarketJournalQuoteProvider(journal_path)`: reads latest trade from a `market_worker.py` `market.sqlite` journal.
  - `BinancePublicQuoteProvider(market_kind="spot", base_url_override=None, allow_loopback_override=False)`: queries Binance public price endpoint (`/api/v3/ticker/price` or `/fapi/v1/ticker/price`) without credentials.
  - `CompositeQuoteProvider`: ordered fallback across providers.

### 3.2 Paper Book Order Cancellation (`src/sourdough/trading/ledger.py`)
- `cancel_company_paper_book_order(writer, *, binding_id, actor_id, order_id)`: idempotently cancels an `open` resting order strictly within an active `company_paper` book (`environment='paper'`), refusing terminal orders and cross-environment IDs, and recording an immutable audit event.

### 3.3 System Safety Restriction Helper (`src/sourdough/board_control.py`)
- `record_system_restriction(writer, *, restriction_id, cause_category, scope_ref, reason, operator_releasable=True, actor_id="system", now_ns=None)`: idempotently activates a system-generated `restriction_cause` (e.g., on unattributed live exchange fills/positions) with monotonic `restriction_revision`, `last_command_id=NULL`, and an `audit_event`, without requiring a Board principal.

### 3.4 Two-Phase Live Execution & Reconciliation (`src/sourdough/trading/reconcile.py`)
- `submit_company_live_book_order(writer, *, binding_id, actor_id, action_id, instrument_id, side, quantity, limit_minor_units, adapter, quote_provider=None, time_in_force="IOC", run_id=None, now_ns=None)`:
  1. Verifies active `company_live` binding (`environment='live'`), active agent, `require_trading_risk_clear(..., ENV_LIVE)`, adapter environment (`live` or `external_test` only when explicitly injected for qualification/testing — strictly rejecting `paper`), cash/exposure ceiling, and fresh quote (`<= QUOTE_FRESHNESS_MAX_NS = 15s`) when `quote_provider` is configured.
  2. Persists `prepare_external_action(idempotency_key=f"live-order:{binding_id}:{action_id}", ...)` BEFORE network dispatch. Replays `confirmed` idempotently; refuses duplicate dispatch when `in_flight` or `unknown`, and refuses new live orders while any prior `submit_order` external action remains unresolved (`prepared`, `in_flight`, or `unknown`).
  3. Transitions outbox to `in_flight` with serialized request context, dispatches `adapter.submit_order(ExternalOrderRequest(...))`.
  4. On `ExternalOrderReplyUnknown`: settles outbox to `unknown` and returns `status="unknown"` without fabricating a fill or cancellation.
  5. On `ExternalVenueRefused`: settles outbox to `rejected` and raises `ExecutionTierRefused`.
  6. On venue confirmation: queries fills via `adapter.query_fills`, books each fill idempotently through `record_live_book_order_fill`, settles outbox to `confirmed` (terminal) or retains `in_flight` (resting/partial), and returns the authoritative receipt.
- `reconcile_trading_books(writer, *, quote_provider=None, live_venue_resolver=None, max_quote_age_ns=60*10**9, now_ns=None)`:
  1. **Live Outbox Recovery:** Inspects all unresolved `external_actions` (`status IN ('prepared', 'in_flight', 'unknown')`, `operation='submit_order'`) for active `company_live` bindings, queries `adapter.query_order` and `adapter.query_fills`, books any discovered fills via `record_live_book_order_fill`, and settles resolved actions (`confirmed` / `rejected`). Leaves `absent_unsealed` orders unresolved fail-closed.
  2. **External Drift Detection:** Calls `adapter.reconcile_external` for symbols held or traded on the active live binding. If `not report.clean` (unattributed fills or positions), automatically calls `record_system_restriction(..., scope_ref="execution:live", cause_category="trading_risk_pause")` and reports `drift_detected=True`.
  3. **Mark-to-Market Valuations:** For every active `company_paper` and `company_live` book with open positions (or shadow link), reads fresh quotes from `quote_provider` and appends a `trading_book_valuation` cut (`source="market_mark"`).

### 3.5 Server-Bound Trading Broker Tools (`src/sourdough/trading/company_tools.py`)
- `CompanyTradingTools` exposes scoped tools on `quant.paper_decision` / `trading.decision`:
  - `read_market_quote`
  - `read_trading_books`
  - `submit_company_paper_order`
  - `cancel_company_paper_order`
  - `submit_company_live_order`
  - `propose_live_migration`
  - `schedule_next_shift` (bounded `delay_seconds` in `[60, 86400]` over allowed trading duties via `register_responsibility` + `set_condition`)
- `TradingDomainPlugin` accepts optional `quote_provider`, `live_venue_resolver`, and `max_quote_age_ns` (honoring optional `SOURDOUGH_TRADING_QUOTE_SOURCE` env var such as `binance-spot`, `binance-futures`, or `journal:<path>` while keeping `src/sourdough/cli.py` 100% Core-isolated and zero-arg backwards compatible), equips `quant.paper_decision` / `trading.decision` with `CompanyTradingTools`, wires `quant.reconcile` / `trading.reconcile` to `reconcile_trading_books`, and enriches CEO read-only tools with `read_trading_books` (and `read_market_quote` when a quote provider is configured).
- Trading CLI (`src/sourdough/trading/cli.py`) exposes `reconcile-trading-books --quote-source ... --max-quote-age-seconds ...` so operators can run mark-to-market and reconciliation on demand.

## 4. Lifecycle Failure Modes & Invariants

- **Stale or missing market quote:** `submit_company_paper_order` and `submit_company_live_order` refuse fail-closed; `reconcile_trading_books` skips stale instruments without inventing synthetic prices.
- **Active risk restriction (`execution:paper`, `execution:live`, `execution:all`, `company:all`):** Blocks orders and preconditions fail-closed.
- **External live timeout (`ExternalOrderReplyUnknown`):** Outbox transitions to `unknown`; duplicate retries and new live orders are refused fail-closed until `reconcile_trading_books` resolves the order from the venue; live resumption remains blocked while `unknown` exists.
- **Unattributed live exchange activity:** Automatically triggers `execution:live` restriction pause and surfaces in Board Client Controls & Live Trading immediately.
- **Environment isolation:** Paper orders never call external adapters; live orders require an `active` `company_live` binding with a resolved adapter matching its environment.

## 5. Acceptance Evidence Plan

- **Unit tests (`tests/unit/test_autonomous_trading_loop.py`):**
  - Quote provider normalization, `MarketJournalQuoteProvider`, `BinancePublicQuoteProvider` (loopback HTTP), and stale-quote refusal.
  - `CompanyTradingTools` server-bound identity rejection, paper order submission/cancellation with host quotes, migration proposal, and bounded next-shift timer scheduling.
  - Two-phase live execution against `BinanceWireEmulator` (normal fill, idempotent replay, injected timeout → `unknown` → duplicate and concurrent new live orders blocked → `reconcile_trading_books` recovery → fill booked & outbox `confirmed`).
  - Unattributed external fill on `BinanceWireEmulator` → `reconcile_trading_books` auto-applies `execution:live` restriction pause and blocks subsequent live orders.
  - Mark-to-market valuation cuts updating both active Live and Shadow Paper books simultaneously.
- **Connected Flow E2E (`tests/flows/flow-autonomous-trading-loop/test_flow.py`):**
  - Real `ResidentCompanyService` + `ClientWebBridge` + loopback LLM mock + loopback `BinanceWireEmulator` + `TradingDomainPlugin(quote_provider=..., live_venue_resolver=...)`:
    1. Autonomous Quant shift runs with Client closed: reads quote, submits Company Paper order, proposes Paper→Live migration, schedules next reconcile shift.
    2. Board approves migration via HTTP `POST /api/v1/commands` (`submit_decision_answer`).
    3. Autonomous Quant shift runs on active Live book: submits real HMAC-signed order through 2-phase outbox to `BinanceWireEmulator`, records reconciled live fill & valuation.
    4. Autonomous `quant.reconcile` shift runs: marks both Live book and Shadow Benchmark to updated quote and verifies `GET /api/v1/projections/trading-desk?environment=live` returns multi-point Live curve, Shadow Benchmark overlay, Live Open Positions, and Live Execution Tape with `external_order_id`.
- **Regression & hygiene gates:** `ruff format --check`, `ruff check`, `test_core_domain_isolation.py`, `test_storage_ownership.py`, full `tests/unit`, `reference_guard.py staged`, and `gitleaks`.
