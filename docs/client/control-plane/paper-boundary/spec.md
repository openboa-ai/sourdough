# Swappable paper-only execution boundary

Design draft · Verification **UNRUN** · Responsible component: Paper Execution & Control Plane Boundary (`src/sourdough/paper_execution.py`, `src/sourdough/paper_account.py`, `src/sourdough/paper_sender_lease.py`, `src/sourdough/runtime/venue_coordinator.py`)

[Feature intent](../intent.md) · [Meta and read projections](../projections/spec.md) · [Events and commands](../commands-and-events/spec.md) · [Investment evidence](../../inspect/investment-panel/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Define the narrow protocol and control-plane boundary isolating all simulated trading observability and operator controls strictly to `environment == "paper"` behind swappable `PaperDestination` and `PaperExecution` adapters. Zero live-trading commands, endpoints, exchange credentials, or capital-transfer controls exist anywhere in the Client UI or UDS Control Plane.

Input contracts: [Investment evidence](../../inspect/investment-panel/spec.md) · [Bind the simulated account](../../../company/profiles/quant/trading/paper-account/intent.md) · [Execution environment](../../../company/profiles/quant/trading/execution-environment/intent.md) · [Meta and read projections](../projections/spec.md) · [Events and commands](../commands-and-events/spec.md). These are public service contracts, not imports of client UI packages.

## Design decision and review

### 1. Hard `environment == "paper"` enforcement (`[Live origin/main]`)

- **SQLite DDL invariant (`storage/schema/v1/paper/01_paper_operations.sql:16`)**: `paper_binding` enforces `environment TEXT NOT NULL CHECK (environment = 'paper')`.
- **Service validation (`paper_account.py:65-92`)**: `account.environment != "paper"` raises `BindingRefused`; core-only companies without the `paper` domain reject all paper projections and paper commands before accessing paper tables.
- **Control-plane surface exclusion**: `control-plane-v1` exposes zero endpoints, projection fields, or command types for live-capital accounts, live venue keys, order placement by the board, or external capital deposits/withdrawals. `GET /v1/projections/paper-*` responses carry `"environment": "paper"` explicitly.

### 2. Swappable protocol boundary (`PaperDestination` / `PaperExecution`)

**Concern for the responsible component:** Swapping from a local fixed-quote simulator to an isolated `nautilus_trader` sandbox or replay engine must not alter projection schemas, command lifecycle semantics, or Two-Layer P&L accounting.

- **Protocols (`src/sourdough/paper_execution.py:75-106`)**:
  - `PaperDestination(AccountObserver, Protocol)`: `observe_account`, `check_order_terms`, `submit`.
  - `PaperExecution(PaperDestination, Protocol)`: `require_independent_from`, `observe_quote`, `observe_research_quote`, `observe_order_sizing`, `observe_fee_bps`, `read_fills`, `read_cancellation`, `read_order_position`.
  - **Shared DTOs (`paper_execution.py:10-74`)**: `PaperIntent`, `PaperOrderSizing`, `ReplayAvailability`, `VenueExecution`, `VenueCancellation`, `VenuePosition`, `TerminalOutcome` (`"cancelled" | "rejected" | "denied" | "expired"`).
  - **Journal independence (`require_separate_journal`, `paper_execution.py:128-134`)**: Venue execution state MUST live in a physically separate journal SQLite store distinct from `company.sqlite`.
- **Concrete swappable adapters**:
  1. `FixedQuoteVenue` (`src/sourdough/paper_venue.py`, `connector_revision = "fixed-quote-v1"`): Standalone paper venue journal (`venue_account`, `venue_execution`, `venue_fee_policy`, `venue_fill_fee`), `environment == "paper"`.
  2. `NautilusPaperVenue` (`src/sourdough/nautilus_paper.py`, backed by `nautilus_trader`):
     - `CONNECTOR = "nautilus-spot-sandbox-v1"` (`sandbox.sqlite`, virtual `BINANCE-001` sandbox account, `environment == "paper"`).
     - `REPLAY_CONNECTOR = "nautilus-offline-spot-replay-v1"` (`replay.sqlite`, deterministic replay journal, `environment == "paper"`).

### 3. Sender lease, bounded segments, `15s` quote freshness, and `cancel != flat`

- **Single-sender lock (`src/sourdough/paper_sender_lease.py:13-48`)**:
  - `hold_sender_lease(journal)` opens `journal.with_suffix(".writer-lock")` (e.g., `sandbox.writer-lock` for `sandbox.sqlite`; `0o600`, `O_NOFOLLOW`), pins `journal_key(journal)` `(st_dev, st_ino)`, and acquires non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)`.
  - `verify_sender_lease` / `verify_worker_sender` re-verify `(st_dev, st_ino)` and the lock before worker dispatch (`nautilus_runtime.py:189,215`) and before restore settlement (`paper_restore.py:93,101,113`). `GET /v1/projections/paper-execution` exposes read-only lock status (`locked`, `lock_path`, `journal_key`) without granting write access.
- **Bounded venue segments (`src/sourdough/runtime/venue_coordinator.py:9-102`)**:
  - `VenueSegmentPolicy` bounds `MIN_SEGMENT_SECONDS = 8`, `MAX_SEGMENT_SECONDS = 600`, `DRAIN_MARGIN_SECONDS = 5` (`drain_margin_seconds < segment_seconds`), yielding `work_seconds = active_seconds - drain_margin_seconds`.
  - `BoundedVenueCoordinator` verifies `venue.require_independent_from(store)` and partitions operating windows via `plan_segments(total_seconds)` (as a stateless planner without a `WriterLease` or SQLite connection); the owning `CompanyRuntime` / `paper_account` / `paper_window` `write_tx` coordinator emits `paper.venue_segment_planned` / `paper.venue_segment_rotated` (`[M3 Extension]`) under `WriterLease` for `/paper/adapter`.
- **Server-side quote freshness (`QUOTE_MAX_AGE_NS = 15 * 10**9`, `nautilus_paper.py:33,646`, `paper_resume.py:73-113`)**:
  - `read_current_risk_cut()` computes `expires_ns = min(event_ns, exchange_trade_ns) + 15_000_000_000`. `POST /v1/commands` (`resume_new_risk`) checks `cut.observed_ns <= time_ns() <= cut.expires_ns` in Phase 2 (`BEGIN IMMEDIATE`) and fails closed (`400`) on expiry without altering `paper_risk_state.suspended = 1`.
- **`cancel != flat` accounting invariant (`work_projection.py:218-232`, `paper_books.py`)**:
  - When a `VenueCancellation(filled_quantity > 0, outcome="cancelled")` settles, `paper_cancellation` releases only `open_reservation_minor_units = 0` for the cancelled unfilled remainder while `booked_position` continues summing cumulative `paper_fill` quantities (`ordered - cancelled_remainder > 0`). Projections MUST expose `status = "cancelled"`, `open_reservation_minor_units = 0`, and `booked_position > 0` distinctly.

### 4. `change_paper_binding` drain preconditions and runtime configuration/start

- **`change_paper_binding` (`paper_account.py:270-369`, `[M3 Extension]` UDS command)**:
  1. Enforces distinct `revision`, `account_id`, and `destination_id` under matching `charter_revision`; `_require_new_identity` forbids reselecting retired bindings, accounts, or destinations.
  2. Refuses with `BindingRefused` if `_has_unsubmitted_buy` is true (`"Finish the original buy decision before changeover"`).
  3. Inserts a pending row into `paper_binding_change` (`effective_at_ns = NULL`), which fences new `paper_order` admissions (`paper_order.py:104-105`), `resume_new_risk` (`paper_resume.py:39-41`), and `activate_strategy_release` (`strategy_comparison.py:1763-1766`).
  4. Evaluates `_open_effect(connection, current)` (`paper_order.reserved_minor_units != 0 OR duty.state = 'running'`): if open, keeps `effective_at_ns = NULL` and sets `blocked_by = "outstanding-company-effect"`.
  5. Evaluates `current_destination.observe_drained_account(...)` and `next_destination.observe_drained_account(...)`: if either is `None`, sets `blocked_by = "unverified-flat-destination"`.
  6. Only when both drain checks pass does it atomically replace `paper_binding` (`environment = 'paper'`) and populate `effective_at_ns`.
- **`configure_paper_runtime` / `start_paper_runtime` (`[M3 Extension]` under `environment == "paper"`)**:
  - Optional explicitly authorized `POST /v1/commands` operations (`paper-runtime-command-v1`) enabling an operator to configure or start the paper-trading runtime (`nautilus_runtime.py`) when the `paper` profile is installed.
  - Validation, adapter journal setup, and any connector handles remain strictly inside `nautilus_runtime.py` / `PaperExecution`; UDS commands and projections expose only handle identifiers, readiness (`ready | blocked | unknown`), and `blocked_by` statuses—never secret values or live credentials. Company operation never requires the Client UI to configure or run the paper runtime.

## Functional and quality requirements

- **FR.** Enforce `environment == "paper"` in SQLite DDL, runtime binding validation, `/v1/projections/paper-*`, and `/v1/commands`; never expose live-trading routes, commands, or credentials.
- **FR.** Keep `GET /v1/projections/paper-execution`, `paper-fund`, `paper-statements`, and `change_paper_binding` coupled solely to `PaperDestination` / `PaperExecution` DTOs, `paper_sender_lease`, `BoundedVenueCoordinator`, and `paper_binding_change` drain blockers (`outstanding-company-effect`, `unverified-flat-destination`).
- **NFR.** Preserve `cancel != flat` open positions after partial-fill remainder cancellations, enforce `QUOTE_MAX_AGE_NS = 15s` strictly server-side, and keep swapping between `FixedQuoteVenue`, `nautilus-spot-sandbox-v1`, and `nautilus-offline-spot-replay-v1` transparent to UI schemas.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Adapter swap transparency | Run equivalent order/fill/statement cycles under `fixed-quote-v1` and `nautilus-spot-sandbox-v1`. Confirm `/v1/projections/paper-fund` and `paper-statements` preserve identical DTO schemas while only `/paper-execution` reflects the swapped `connector_revision`. |
| Drain blockers on `change_paper_binding` | Submit `change_paper_binding` while an open reservation or unverified destination balance exists. Confirm `effective_at_ns` stays `null`, `blocked_by` surfaces `"outstanding-company-effect"` or `"unverified-flat-destination"`, and new orders/resume stay fenced. |
| Core-only exclusion | Query `/v1/projections/paper-execution` or submit `change_paper_binding` on a core-only store without `paper` domain. Confirm fail-closed `404` / `422` without accessing paper tables. |

**Oracle:** Inspect `src/sourdough/paper_execution.py`, `src/sourdough/paper_account.py`, `src/sourdough/paper_sender_lease.py`, `src/sourdough/runtime/venue_coordinator.py`, `src/sourdough/paper_venue.py`, `src/sourdough/nautilus_paper.py`, and `src/sourdough/storage/schema/v1/paper/01_paper_operations.sql`.

## Operations and change review

Adding any new simulated venue adapter MUST implement `PaperExecution`, enforce `require_separate_journal` and `environment == "paper"`, and acquire `hold_sender_lease` before dispatch.
