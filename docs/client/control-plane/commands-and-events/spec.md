# Event stream, runtime wakeups, and typed commands

Design draft · Verification **UNRUN** · Responsible component: Control Plane Events & Commands (`src/sourdough/ipc/server.py`, `src/sourdough/runtime/`, `src/sourdough/board_control.py`, `src/sourdough/trading/plugin.py`, `src/sourdough/trading/commands.py`, `src/sourdough/trading/risk.py`)

[Feature intent](../intent.md) · [Bridge and auth](../bridge-and-auth/spec.md) · [Meta and read projections](../projections/spec.md) · [Command submission](../../commands/submit/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [Approvals & Custody](../../governance/approvals-and-custody/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Define the `GET /v1/events` redacted NDJSON stream, `ResidentCompanyService` + `CompanyDaemon.notify_commit` post-commit wakeup wiring and `audit_event` emissions across Core Company OS and Trading modules, `Interrupt` / `Approval` / `Digest` routing classification, CEO-only 1:1 `send_ceo_message`, CEO-delegated offboarding boundary (`retirement.py`, zero Board `retire_employee` write command), and the `12`-command `POST /v1/commands` and `GET /v1/commands/{command_id}?operation=...` contract (`8` Core + `4` Trading plugin commands gated `422` when absent).

Input contracts: [Transport, loopback bridge, and role authorization](../bridge-and-auth/spec.md) · [Command submission](../../commands/submit/spec.md) · [Board control](../../../company/profiles/quant/trading/board-control/apply-control/spec.md) · [Offboard](../../../company/systems/organization/offboard/intent.md). These are public service contracts, not imports of client UI packages.

## Design decision and review

### 1. `GET /v1/events` NDJSON stream (`src/sourdough/ipc/server.py:971-1025`)

- **Route and bounds**: `GET /v1/events?after_seq=<int>&limit=<int>&wait_ms=<int>`, validating `after_seq >= 0`, `1 <= limit <= 500`, `0 <= wait_ms <= 5000`, and `_MAX_EVENT_WAITERS = 8` (`503` when exhausted).
- **Response headers**: `Content-Type: application/x-ndjson`, `Connection: close`, `Cache-Control: no-store`, `X-Sourdough-Company-Id: <company_id>`.
- **Redacted 7-field NDJSON envelope**: Reads `audit_event` rows (`WHERE company_id = ? AND event_seq > ? ORDER BY event_seq ASC LIMIT ?`) via `open_reader()` and emits `{event_seq, company_id, domain, entity_type, entity_id, event_type, recorded_at_ns}` (`actor_id`, `run_id`, `command_id`, and `payload_json` redacted so `/v1/events` acts purely as an invalidation cursor).
- **RBAC gate**: Requires authenticated active `board_binding` + `board_viewer` or `board_control` role (`bootstrap_locked` fails closed `403`).

### 2. `ResidentCompanyService` wakeups & `audit_event` emissions

- **Unified `WriterLease` & `notify_commit` (`src/sourdough/runtime/resident.py`)**: `ResidentCompanyService` hosts `CompanyDaemon` (which holds `WriterLease` and `write_lock`) and runs `CompanyRuntime.step_with_writer(writer, tx_lock=daemon.write_lock, on_commit=daemon.notify_commit)`. Every SQLite commit in `ResidentCompanyService` or `CompanyDaemon` triggers `_event_cond.notify_all()` immediately, while Board command commits wake `ResidentCompanyService` (`sync_board_triggers`) without polling delay.
- **Core + Domain `audit_event` hygiene**: All state-changing Core (`company`, `board_control`, `work_kernel`, `staffing`, `onboarding`, `retirement`, `conversations`, `general_ledger`, `checkpoint`, `company_restore`) and Trading (`trading/*` emitting `domain="trading"`) transactions call `storage/kernel.py:append_audit_event` inside `BEGIN IMMEDIATE` `write_tx`.
- **Anti-fatigue routing (`Interrupt` / `Approval` / `Digest`)**:
  - **`Interrupt` (`Notice[urgent]` below 4-slot header + `/controls`)**: Active `restriction_cause` rows, active `CommandOutboxPanel` states (`unknown`, `not_found_retry_choice`, `denied`), `401`/`403` security wipes, and transient disconnect stale state.
  - **`Approval` (`/approvals` & contextual `ContextualApprovalCard`)**: `decision_question` items (`owner_authority`, `live_promotion`, etc.) and `PROMOTE`-eligible `trading_challenger_trial`s route calmly without modal popups.
  - **`Digest`**: Completed work runs, fills, and resolved events update live projections smoothly via `/v1/events` invalidation.

### 3. Generalized `POST /v1/commands` & `GET /v1/commands/{command_id}?operation=...`

#### 3.1 Request envelope & 11-field receipt (`src/sourdough/ipc/server.py:1063-1389`)

- **Request headers & body**: Required `Idempotency-Key` (`1..128` printable ASCII chars) and JSON object (`<= 64 KiB`) containing `company_id`, `command_id` (optional, must match `Idempotency-Key` if present), `command_type`, `contract_version`, `preconditions` (persisted in `command_receipt.preconditions_json`), and `payload` (carrying `payload.reason` where required).
- **11-field `command_receipt` response**: `{status: "found", company_id, command_id, command_type, state, disposition, denial_code, effective_revision, recorded_at_ns, peer_pid, result}`.
- **Error taxonomy**:
  - `400 Bad Request`: invalid JSON, missing/invalid `Idempotency-Key`, missing `?operation=` on `GET`, invalid precondition keys, invalid `payload.reason`, or non-CEO target on `send_ceo_message`.
  - `401 Unauthorized`: missing/invalid `ClientWebBridge` session auth.
  - `403 Forbidden`: unbound/revoked `board_binding`, `bootstrap_locked` on non-bootstrap operations, `board_viewer` attempting `POST /v1/commands`, or `CommandUnauthorized` on foreign `GET /v1/commands/{id}`.
  - `404 Not Found`: unknown route or absent `command_id` on `GET /v1/commands/{id}?operation=...`.
  - `409 Conflict`: `CommandConflict` (`Idempotency-Key` reused with differing payload/preconditions).
  - `422 Unprocessable Entity`: unsupported `command_type` / `contract_version`, Trading plugin command on core-only store, or `execution:*` / `trading_risk_pause` restriction on core-only store (`server.py:1222-1235`).
  - `503 Service Unavailable`: `WriterRefused` (`WriterLease` contention) or `_MAX_IN_FLIGHT` / `_MAX_EVENT_WAITERS` exhaustion.

#### 3.2 Complete `12`-command registry (`8` Core + `4` Trading plugin)

| Scope | `command_type` | `contract_version` | Role / Gate | `preconditions` keys | Domain Owner & Key Invariants |
| --- | --- | --- | --- | --- | --- |
| **Core** | `register_owner` | `rbac-command-v1` | `bootstrap_locked` only | `{bootstrap_state}` | `board_control.py`: consumes bootstrap challenge, binds owner with `board_control`, emits `audit_event`. |
| **Core** | `send_ceo_message` | `ceo-chat-command-v1` | `board_control` | `{ceo_agent_id, charter_revision}` | `board_control.py`: enforces `target_agent_id === preconditions.ceo_agent_id === ceo_agent_id`; fails closed (`400`/`403`) on non-CEO target; wakes `ceo.respond_message`. |
| **Core** | `submit_decision_answer` | `decision-command-v1` | `board_control` | `{question_id, expected_revision, expected_payload_hash}` | `board_control.py`: records `decision_question` resolution and triggers `board.apply_decision`; refused domain actions commit `application_status = 'failed'`. |
| **Core** | `pause_company` | `restriction-command-v1` | `board_control` | `{charter_revision, restriction_id, expected_restriction_revision}` | `board_control.py`: inserts active `restriction_cause`; `server.py:1222-1235` rejects `execution:*` / `trading_risk_pause` with `422` unless `"trading"` is installed. |
| **Core** | `resume_company` | `restriction-command-v1` | `board_control` | `{charter_revision, restriction_id, expected_restriction_revision}` | `board_control.py` + `TradingRestrictionGuard` (`src/sourdough/trading/risk.py:91-208`): clearing `execution:*` requires zero open `trading_book_order`, zero unresolved `external_actions` for `live`, and `<= 15s` quote freshness. Clearing one `restriction_id` leaves competing stops active. |
| **Core** | `interrupt_run` | `work-command-v1` | `board_control` | `{run_id, expected_fencing_token}` | `runtime/work_kernel.py`: fences active `work_run` (`work.run_fenced`). |
| **Core** | `cancel_work` | `work-command-v1` | `board_control` | `{work_item_id, expected_state}` | `runtime/work_kernel.py`: cancels single `work_items` row under state precondition. |
| **Core** | `cut_checkpoint` | `checkpoint-command-v1` | `board_control` | `{charter_revision}` | `storage/checkpoint.py`: acquires `WriterLease`, writes WAL-normalized `0o600` `sourdough-checkpoint-v2` archive. |
| **Trading** | `activate_strategy_release` | `strategy-command-v1` | `board_control` + `"trading"` | `{binding_revision, expected_active_deployment_id?}` | `src/sourdough/trading/commands.py`: activates `trading_strategy_release` (`!= owner_agent_id`, `PROMOTE` required); returns `422` when `"trading"` absent. |
| **Trading** | `rollback_strategy_deployment` | `strategy-command-v1` | `board_control` + `"trading"` | `{expected_active_deployment_id}` | `src/sourdough/trading/commands.py`: forward-rolls back to prior release while preserving open `trading_book_order` / `trading_book_fill` rows. |
| **Trading** | `promote_execution_environment` | `execution-tier-command-v1` | `board_control` + `"trading"` | `{charter_revision, target_binding_id, qualification_receipt_id, expected_payload_hash}` | `src/sourdough/trading/commands.py`: promotes `company_live` after verifying soak, `PASS` `venue_qualification_receipt`, and `expected_payload_hash`. |
| **Trading** | `demote_live_environment` | `execution-tier-command-v1` | `board_control` + `"trading"` | `{charter_revision, target_binding_id}` | `src/sourdough/trading/commands.py`: atomically transitions `company_live` (`suspend_live` \| `demote_to_paper`) while keeping `company_paper` and `agent_sandbox` active. |

#### 3.3 Delegated HR & Offboarding Boundary (No Board `retire_employee` Write Command)

Routine employee retirement is **not** a Board `POST /v1/commands` mutation. Under `src/sourdough/retirement.py`, `retire_agent` (`TERMINATION_SCOPE = "retire-agent"`) runs inside `ResidentCompanyService` for CEO/manager execution only (blocking `founder_id` / `manager_id`), and `/reports` (`org-v1` `delegated_hr_custody`) renders the result strictly read-only (`"Managed by CEO · Board view is read-only"`).

## Functional and quality requirements

- **FR.** Stream redacted 7-field `audit_event` NDJSON rows on `GET /v1/events`, wire `ResidentCompanyService` `on_commit` wakeups to `CompanyDaemon.notify_commit()`, and enforce Core vs Trading command gating (`422` when `"trading"` is absent).
- **FR.** Enforce `(principal_id, operation)` scoping on `GET /v1/commands/{command_id}?operation=...`, CEO-only validation on `send_ceo_message`, `TradingRestrictionGuard` (`<= 15s` quote freshness) on `execution:*` resume, and zero Board `retire_employee` writes.
- **NFR.** Serialize conflicting mutations via `WriterLease` + `BEGIN IMMEDIATE` and keep Core IPC modules free of direct Trading imports.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Runtime wakeup & stream redaction | Open `GET /v1/events?after_seq=S0&wait_ms=5000`, trigger a `ResidentCompanyService` commit, and verify the 7-field redacted NDJSON line arrives promptly with `actor_id`/`run_id`/`command_id`/`payload_json` omitted. |
| Domain command gating (`422`) | Submit `promote_execution_environment` or `pause_company` (`scope_ref="execution:paper"`) on a core-only store -> verify fail-closed `422` and zero Trading entries in `GET /v1/meta.supported_commands`. |
| Competing stops & `15s` Trading resume | Trigger two `restriction_cause` rows (`rest-p1`, `rest-p2`); resume `rest-p1` -> verify `rest-p2` stays active. Attempt `resume_company` on `execution:paper` with stale quote (`> 15s`) or open `trading_book_order` -> verify fail-closed refusal. |
| CEO chat & delegated offboarding | Submit `send_ceo_message` to `ceo_agent_id` (succeeds) vs non-CEO `agent_id` (`400`/`403`). Verify `retire_employee` is absent from `supported_commands` and rejected (`422`). |

**Oracle:** Inspect `src/sourdough/ipc/server.py`, `src/sourdough/runtime/resident.py`, `src/sourdough/board_control.py`, `src/sourdough/retirement.py`, `src/sourdough/trading/plugin.py`, `src/sourdough/trading/commands.py`, and `src/sourdough/trading/risk.py`.

## Operations and change review

Every command handler registered in Core or via `DomainPlugin.contribute_commands` MUST record its `command_receipt` and `append_audit_event` inside a single `BEGIN IMMEDIATE` transaction and trigger `notify_commit()` after commit.
