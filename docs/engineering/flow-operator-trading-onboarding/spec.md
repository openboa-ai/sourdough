# Operator Trading Onboarding, Default Live Venue Resolver & Day-2 Desk Controls

Status: Scoped design specification (`flow-operator-trading-onboarding`, base `main@802624bf`)

## 1. Intent & Product Outcome

Make an opt-in Sourdough Trading Company turnkey to bootstrap, qualify, monitor, and operate from both the CLI and the Board Client UI without custom glue scripts:

1. **Default Host Live Venue Resolver (`src/sourdough/trading/venue_resolver.py`):** When `sourdough run-company` loads `TradingDomainPlugin()` with live resolution enabled, active Board-approved `company_live` bindings automatically resolve to a pinned `BinanceVenueAdapter` backed by in-memory sign-time credential resolution (`EnvRefCredentialProvider` for `envref:...` and `MacOsKeychainCredentialProvider` for `keychain:...`), failing closed on unsupported or contradictory connectors/markets, non-active bindings (for new order submission), or environment-mismatched credentials. Reconciliation/recovery paths pass `allow_recovery_statuses=True` so suspended/retired live bindings preserve external lookup and settlement of unresolved obligations without re-enabling new order submission.
2. **Turnkey Operator Trading CLI (`src/sourdough/trading/cli.py`):** Provide script-free domain subcommands contributed via `TradingDomainPlugin` (preserving zero trading imports in Core `src/sourdough/cli.py`):
   - `enable-trading-domain`
   - `open-paper-book`
   - `register-live-candidate`
   - `qualify-live-venue` (`--mode emulator` for full 6-case loopback HMAC execution qualification issuing `PASS`, and `--mode binance-testnet` for read-only live Binance Testnet connectivity/isolation verification issuing `INCOMPLETE` with `cases_unrun=4` until separately authorized execution observations exist)
   - `arm-trading-schedule` (registers standing `quant.paper_decision` / `quant.reconcile` responsibilities, arms recurring timer conditions with persisted `cooldown_ns` intervals that rearm automatically upon shift settlement, and optionally enqueues immediate due shifts)
3. **Day-2 Board Desk Commands (`src/sourdough/trading/commands.py`, `src/sourdough/trading/plugin.py`):**
   - `open_company_paper_book` (`contract_version="execution-tier-command-v1"`): Allows an authorized `board_control` principal to open a new isolated Company Paper Book directly from the Board Client, committing the book binding, audit event, and idempotent `command_receipt` atomically inside a single `write_tx`.
   - `reconcile_trading_books` (`contract_version="execution-tier-command-v1"`): Allows an authorized `board_control` principal to run on-demand mark-to-market valuation and external live venue reconciliation directly from the Board Client.
4. **Board Client Operational Controls (`apps/client/src/views/trading/TradingDeskView.tsx`):**
   - **"Sync Quotes & Reconcile"** compact pill control in the Desk header dispatching `reconcile_trading_books` through the durable command outbox.
   - **"New Paper Book"** inline expandable Yogurt card (zero popup modal) in Paper Trading allowing the Board to launch concurrent paper strategies with explicit starting capital, exposure ceiling, fee/slippage bps, and optional strategy release ID.

## 2. Authority & Data Boundaries

- **Zero Core Company OS coupling:** Zero edits to `src/sourdough/cli.py`, `src/sourdough/ipc/server.py`, or any other Core module (`test_core_domain_isolation.py` remains green).
- **Single-owner SQL writes (`storage/ownership.py`):**
  - `execution_environment_binding` and `venue_qualification_receipt` mutations stay in `trading/accounts.py`.
  - `command_receipt` and `audit_event` mutations stay in `storage/kernel.py` (`admit_command_receipt`, `append_audit_event`).
  - `work_responsibilities`, `work_conditions`, and `work_items` mutations stay in `runtime/work_kernel.py`.
- **Credential & Environment Isolation:**
  - `CompositeCredentialProvider` dispatches `envref:` -> `EnvRefCredentialProvider` and `keychain:` -> `MacOsKeychainCredentialProvider` strictly in memory at sign time.
  - `build_default_live_venue_resolver` refuses non-`active` bindings, non-`company_live` tiers, unrecognized connector revisions, and any credential handle violating `assert_handle_matches_environment`.
  - `qualify-live-venue --mode binance-testnet` strictly targets `ENV_EXTERNAL_TEST` (`https://testnet.binance.vision` / `https://testnet.binancefuture.com`) and rejects live credentials fail-closed.
- **RBAC Enforcement:** Both new Board commands (`open_company_paper_book` and `reconcile_trading_books`) enforce `make_board_authorizer` (`board_control` role required; `board_viewer` and unbound principals receive HTTP 403 `BoardControlForbidden`).

## 3. Modules & Contracts

### 3.1 Composite Credentials & Default Live Venue Resolver (`src/sourdough/trading/venue_resolver.py`)
- `CompositeCredentialProvider(env_provider=None, keychain_provider=None)` implements `CredentialProvider`.
- `infer_binance_market_kind(connector_revision, destination_id) -> str`: maps recognized Binance Spot (`spot`, `binance-rest-v1`, `binance-spot-*`) and USD-M Futures (`futures`, `usdm`, `fapi`, `binance-futures-*`) identifiers, refusing unrecognized connectors with `ExecutionTierRefused`.
- `build_default_live_venue_resolver(store, company_id, *, credential_provider=None, environment=ENV_LIVE, base_url_override=None, allow_loopback_override=False) -> Callable[[str], ExternalVenueAdapter | None]`: queries `execution_environment_binding` for an active `company_live` row matching `binding_id`, validates its credential handle and connector, and constructs a `BinanceVenueAdapter`.
- `TradingDomainPlugin` uses `build_default_live_venue_resolver` automatically when no explicit `live_venue_resolver` was injected and host live resolution is opted in via `auto_resolve_live_venues=True` or `SOURDOUGH_TRADING_LIVE_ENV=live|external_test` (preventing accidental outbound network traffic during offline/test runs while enabling zero-script production `sourdough run-company`).

### 3.2 Operator Trading CLI (`src/sourdough/trading/cli.py`)
- `enable-trading-domain <store> --company-id <id>`
- `open-paper-book <store> --company-id <id> --account-id <id> --book-name <name> --starting-minor <int> --exposure-limit-minor <int> [--currency USD] [--strategy-release <id>] [--fee-bps 4] [--slippage-bps 2]`
- `register-live-candidate <store> --company-id <id> --binding-id <id> --account-id <id> --destination-id <id> --connector-revision <rev> --instrument-rules-digest <d> --starting-minor <int> --exposure-limit-minor <int> --credential-handle <ref> [--currency USD] [--fee-bps 4]`
- `qualify-live-venue <store> --company-id <id> --instrument-rules-digest <d> [--mode emulator|binance-testnet] [--market spot|futures] [--symbol BTCUSDT] [--credential-handle <ref>] [--account-id <id>] [--destination-id <id>] [--receipt-id <id>]`
- `arm-trading-schedule <store> --company-id <id> --agent-id <id> [--decision-delay 300] [--reconcile-delay 60] [--enqueue-now]`

### 3.3 Day-2 Board Desk Commands (`src/sourdough/trading/commands.py`, `src/sourdough/trading/plugin.py`)
- `OPEN_COMPANY_PAPER_BOOK = "open_company_paper_book"` (`contract_version = "execution-tier-command-v1"`):
  - Preconditions: `{"charter_revision"}` (verified against current `company.charter_revision`; mismatch denies with `conflicting_revision`).
  - Payload: `account_id`, `book_name`, `starting_minor_units`, `exposure_limit_minor_units`, optional `currency` (`"USD"`), `strategy_release_id`, `fee_bps` (`4`), `slippage_bps` (`2`), `reason`.
  - Records idempotent `command_receipt` and `audit_event` (`trading.paper_book_opened_by_board`).
- `RECONCILE_TRADING_BOOKS = "reconcile_trading_books"` (`contract_version = "execution-tier-command-v1"`):
  - Preconditions: `{"charter_revision"}` (verified against current `company.charter_revision`).
  - Payload: optional `max_quote_age_seconds` (default `60`), `reason`.
  - Runs `reconcile_trading_books` using the plugin's configured `quote_provider` and `live_venue_resolver`, records idempotent `command_receipt` and `audit_event` (`trading.reconciled_by_board`), and returns structured reconciliation counts.

### 3.4 Board Client UI (`apps/client/src/views/trading/TradingDeskView.tsx`)
- Header pill button `Sync Quotes & Reconcile` (shown when `reconcile_trading_books` is in `meta.supported_commands`, disabled unless `canWrite`, submits idempotent command and pushes result toast).
- Paper Trading inline expandable card `New Paper Book` (shown when `open_company_paper_book` is in `meta.supported_commands`, uses Yogurt `Card`, `TextInput` / controlled inputs, `Button`, `Notice`, inline validation, and `ControlledInlineConfirm` / direct Board command dispatch with full Yogurt 0.2.0 visual compliance — zero popup modals, zero gradients, zero literal color fallbacks).

## 4. Lifecycle Failure Modes & Invariants

- **RBAC refusal:** `board_viewer` or unbound caller receives HTTP 403 for both `open_company_paper_book` and `reconcile_trading_books` with zero database writes.
- **Stale charter revision:** Submitting either Board command with an outdated `charter_revision` returns deterministic `state="denied"`, `denial_code="conflicting_revision"`, recorded in `command_receipt`.
- **Idempotency & conflict:** Replaying the same `command_id` with identical payload returns the cached `BoardCommandOutcome`; reusing `command_id` with a different payload raises `BoardControlConflict` (HTTP 409).
- **Unknown connector or mismatched credentials:** `build_default_live_venue_resolver` fails closed (`ExecutionTierRefused` / `CredentialRefused`) and never routes live orders to an unverified endpoint.
- **Live Testnet credential safety:** `qualify-live-venue --mode binance-testnet` unconditionally rejects `live` credential handles.

## 5. Acceptance Evidence Plan

- **Unit tests (`tests/unit/test_operator_trading_onboarding.py`):**
  - `CompositeCredentialProvider` dispatch (`envref:` + `keychain:` scheme validation + environment mismatch refusal).
  - `build_default_live_venue_resolver` resolves active `company_live` Binance Spot/Futures bindings, ignores inactive/paper bindings, and rejects unknown connectors.
  - All 5 new Trading CLI commands (`enable-trading-domain`, `open-paper-book`, `register-live-candidate`, `qualify-live-venue --mode emulator`, `arm-trading-schedule`) run cleanly through `sourdough.cli`.
  - Board commands `open_company_paper_book` and `reconcile_trading_books`: normal apply, idempotent replay, payload conflict (409), stale charter denial, and `board_viewer` RBAC refusal (403).
- **Connected Flow E2E (`tests/flows/flow-operator-trading-onboarding/test_flow.py`):**
  - Bootstraps a brand-new company purely through CLI subcommands (`found`, `bind-board`, `enable-trading-domain`, `open-paper-book`, `register-live-candidate`, `qualify-live-venue --mode emulator`, `arm-trading-schedule`), starts `ResidentCompanyService` + `ClientWebBridge`, exercises HTTP `POST /api/v1/commands` for `open_company_paper_book` and `reconcile_trading_books`, verifies `GET /api/v1/projections/trading-desk?environment=paper` reflects both books and fresh mark-to-market valuations, and verifies `board_viewer` receives HTTP 403.
- **Client UI & Visual Verification:**
  - `npm run typecheck && npm run build && npm run check:yogurt` in `apps/client`.
  - Live browser visual verification across Light & Dark modes (Desktop `1440px` + Mobile `390px`) confirming `Sync Quotes & Reconcile` and `New Paper Book` inline form craft and interactive flow.
- **Regression & Hygiene Gates:** `ruff`, `test_core_domain_isolation.py`, `test_storage_ownership.py`, full `tests/unit`, `reference_guard.py staged`, `gitleaks`.
