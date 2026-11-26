# Submit a command and resolve its receipt

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [Approvals & Custody](../../governance/approvals-and-custody/spec.md) · [Usage scenarios](../../scenarios.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Human query and control access is through Client. Route a received command to its domain owner; a successful submission is not an applied change. Agent and service-originated operations remain independent of Client. Routine employee retirement (`retirement.py`, `TERMINATION_SCOPE = "retire-agent"`) is strictly CEO-delegated inside `ResidentCompanyService`—there is **no Board `retire_employee` write command**.

Send only after an explicit board action: authenticated principal, company, typed operation, request ID (`command_id` via `Idempotency-Key`), `contract_version`, expected `preconditions` (HTTP JSON `preconditions` object persisted in SQLite `command_receipt.preconditions_json`), and `payload` (carrying `payload.reason` where required; `register_owner` carries owner proof/challenge fields in `payload`). Trading-domain commands (`activate_strategy_release`, `rollback_strategy_deployment`, `promote_execution_environment`, `demote_live_environment`, and `execution:*` / `trading_risk_pause` restrictions) are registered by `TradingDomainPlugin` and rejected with HTTP `422` on core-only companies (`"trading"` not in `active_domains`).

Input contracts: [Event stream, runtime wakeups, and typed commands](../../control-plane/commands-and-events/spec.md) · [Board control](../../../company/profiles/quant/trading/board-control/apply-control/spec.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../../inspect/screens/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Submit commands over `CompanyDaemon`'s versioned HTTP/1.1 + JSON + NDJSON Unix domain socket Control Plane (`POST /v1/commands` with `Idempotency-Key` matching `command_id` and typed multi-field `preconditions` stored in `command_receipt.preconditions_json`, plus `GET /v1/commands/{command_id}?operation=<command_type>` scoped to the authenticated `(principal, operation)`).

### 1. Supported Command Registry (`12` Commands: `8` Core + `4` Trading Plugin)

| Scope | `command_type` | `contract_version` | Required `preconditions` keys |
| --- | --- | --- | --- |
| **Core** | `register_owner` | `rbac-command-v1` | `bootstrap_state` (`"locked"`) |
| **Core** | `send_ceo_message` | `ceo-chat-command-v1` | `ceo_agent_id`, `charter_revision` |
| **Core** | `submit_decision_answer` | `decision-command-v1` | `question_id`, `expected_revision`, `expected_payload_hash` |
| **Core** | `pause_company` | `restriction-command-v1` | `charter_revision`, `restriction_id`, `expected_restriction_revision` *(rejects `execution:*` / `trading_risk_pause` with `422` if `"trading"` not installed)* |
| **Core** | `resume_company` | `restriction-command-v1` | `charter_revision`, `restriction_id`, `expected_restriction_revision` *(runs `TradingRestrictionGuard` when clearing `execution:*`)* |
| **Core** | `interrupt_run` | `work-command-v1` | `run_id`, `expected_fencing_token` |
| **Core** | `cancel_work` | `work-command-v1` | `work_item_id`, `expected_state` |
| **Core** | `cut_checkpoint` | `checkpoint-command-v1` | `charter_revision` |
| **Trading** | `activate_strategy_release` | `strategy-command-v1` | `binding_revision` *(+ optional `expected_active_deployment_id`)* |
| **Trading** | `rollback_strategy_deployment` | `strategy-command-v1` | `expected_active_deployment_id` |
| **Trading** | `promote_execution_environment` | `execution-tier-command-v1` | `charter_revision`, `target_binding_id`, `qualification_receipt_id`, `expected_payload_hash` |
| **Trading** | `demote_live_environment` | `execution-tier-command-v1` | `charter_revision`, `target_binding_id` |

### 2. Host transport and `(company_id, principal_id)` `/api/local/pending` persistence

The React 19 + `@openboa/yogurt` Client UI dispatches commands through `ClientWebBridge` (`sourdough client-web`, `src/sourdough/ipc/web_bridge.py`). Upon dispatching an active `POST /v1/commands` request, the bridge persists `{company_id, principal_id, command_id, command_type, contract_version, preconditions, payload, label, sent_ns}` via `PUT /api/local/pending` inside an `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` (`0o700`) directory using atomic `O_CREAT | O_EXCL | O_NOFOLLOW` (`0o600`), `fsync(fd)`, atomic rename, and `fsync(dir_fd)` — **solely for already-transmitted unknown requests**, scoped strictly by **`(company_id, principal_id)`**. On startup recovery, if `GET /v1/commands/{command_id}` returns `403` for a stale or unowned pending file, the bridge quarantines/purges that stale file safely (`403` quarantine) so another principal is never deadlocked. `/api/local/pending` never stores offline queued mutations for automatic replay.

### 3. 8-variant `CommandLifecycle` and inline `CommandOutboxPanel` (`Receipt`)

Maintain the exact 8-variant `CommandLifecycle` state machine: `idle`, `draft`, `submitting`, `unknown`, `accepted`, `applied`, `denied`, `not_found_retry_choice`. Represent `conflicting_revision` strictly as `state = "denied"` with `denial_code = "conflicting_revision"`. Mount `CommandOutboxPanel` strictly as a **persistent, non-auto-dismissing inline banner below the 4-slot Global Header** (never inside the header and never inside a right slide-over drawer), wrapping `Receipt`:
- `submitting` and `accepted` render `ReceiptRecord.state = "pending"` with elapsed `pending_age` (computed from `sent_ns`) and **omit `onLookup` (`Check status`)**.
- `unknown` renders `ReceiptRecord.state = "unknown"` and **passes `onLookup` (`"Check status"` / `"Check original request"`) solely in this state**.
- `applied` deletes the pending file first (failing closed to `unknown` + `controls_locked` if delete fails) and renders `ReceiptRecord.state = "confirmed"`.
- `denied` deletes the pending file first and renders `ReceiptRecord.state = "failed"` with `denial_code` (requiring a brand-new human decision against the latest revision when `conflicting_revision`).
- `not_found_retry_choice` (`404` on receipt lookup) retains the pending file, omits `onLookup`, keeps `controls_locked = true`, and renders an explicit choice: **`Retry same request`** (resubmitting the exact persisted `command_id`, `command_type`, `contract_version`, `preconditions`, and `payload` without reading newer live revisions) or **`Discard draft`** (deleting the pending record and returning to `idle`).

### 4. Universal Inline Human-Language Confirmation (`ControlledInlineConfirm`)

Every state-mutating governance or safety command confirms **in-place via `ControlledInlineConfirm`** (`apps/client/src/components/ControlledInlineConfirm.tsx`, built on `@ui-sdk/react/button` `SDKButton`; **zero popup modals and zero right slide-over drawers**):
1. Rejects automated invocation (`if (details?.source === "agent") return;`) and places `data-agent-confirm="<consequence>"` solely on the confirm CTA.
2. Contrasts **`What changes`** (`<dl>` rows in plain executive language) against **`What stays untouched`** (`<dl>` rows listing unaffected operations).
3. States a plain-language **consequence / risk note**, uses a **consequence-verb CTA** (never generic `"OK"`), and captures `payload.reason` where required.
4. Collapses composite optimistic preconditions and technical identifiers (`command_id`, `command_type`, `contract_version`, `preconditions`) inside a **closed-by-default `"Technical details"` disclosure**.
5. Latches preconditions on expand (`opened.current`), detects mid-review updates (`changed`), prevents double-submission (`sent.current`), and hands off lifecycle tracking to `CommandOutboxPanel`.

### 5. Scoped restrictions & server-side `<= 15s` quote freshness (`resume_company`)

`pause_company` and `resume_company` (`restriction-command-v1`) bind a distinct `restriction_id` and `expected_restriction_revision` per cause so clearing one restriction never clears a competing active stop. For Trading `execution:*` causes, `TradingRestrictionGuard` (`src/sourdough/trading/risk.py`) verifies zero open `trading_book_order` rows, zero unresolved `external_actions` for `live`, and `<= 15s` quote freshness server-side inside `POST /v1/commands`. The Client displays non-binding readiness telemetry (`settled_company_ok`, `unresolved_orders_count`, `unresolved_external_actions_count`, `server_freshness_note`) without a pre-fetched 15-second client countdown.

### 6. Advisory Board ↔ CEO 1:1 Direct Line command (`send_ceo_message`)

`send_ceo_message` (`ceo-chat-command-v1`) allows an authenticated `board_control` principal to post advisory text (`preconditions: {ceo_agent_id, charter_revision}`, `payload: {target_agent_id, body, context_ref?}`) strictly to the designated CEO (`target_agent_id === preconditions.ceo_agent_id`), carrying `Idempotency-Key == command_id` without requiring a blocking confirmation sheet per chat turn. The control plane fails closed (`400`/`403`) on any non-CEO target, and chat bubbles never embed inline state-mutating execution buttons.

**Concern for the responsible component:** A timed-out click might already have committed. Query the original receipt under `(company_id, principal_id)` authorization; do not allocate a second request ID on reconnect, never auto-replay an offline draft, and never allow clearing one restriction cause to bypass another active stop.

## Functional and quality requirements

- **FR.** On stale revision (`conflicting_revision` persisted in `command_receipt`) require a new informed decision against the latest facts. An exact retry reuses the original `command_id`, arguments, and typed multi-field `preconditions`.
- **FR.** Persist only already-transmitted unknown requests in `(company_id, principal_id)`-scoped `/api/local/pending`; on reconnect query `GET /v1/commands/{command_id}?operation=...`. If `404`, transition to `not_found_retry_choice` (`Retry same request` vs `Discard draft`). If `403` on startup recovery of a stale file, quarantine/purge it safely.
- **FR.** Render `Receipt.onLookup` (`Check status`) strictly when `lifecycle === "unknown"`; mount `CommandOutboxPanel` inline strictly below the 4-slot Global Header; confirm every mutating Board command through `ControlledInlineConfirm`; exclude `retire_employee` from Board writes.
- **Compatibility.** Negotiate capabilities via `GET /v1/meta` (`supported_commands`, `active_domains`) before enabling domain mutations. Do not submit a Trading command when `"trading"` is absent from `active_domains`.
- **NFR.** Double-click, refresh, and reconnect cannot apply a control twice. While disconnected, stale, `host_locked`, or `unknown`, lock all mutating controls fail-closed (`controls_locked = true`).

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal & `ControlledInlineConfirm` | Submit an allowed change via `ControlledInlineConfirm`. Verify *What changes*, *What stays untouched*, collapsed `Technical details`, immediate handoff to inline `CommandOutboxPanel` (`pending`, no `Check status`), followed by `applied` and matching effective revision read from the service. |
| Failure or contention | Disconnect after commit but before response (`unknown` enables `Check status`). Reopen and recover the original `(company_id, principal_id)` receipt (`applied`, `denied(conflicting_revision)`, or `404` -> `not_found_retry_choice`). Verify `403` stale-file quarantine does not deadlock a different principal. |
| Competing stops, `15s` cut & domain gating | Trigger two restrictions; verify clearing `R1` leaves `R2` active. Verify Trading `execution:*` resume rejects stale quotes (`> 15s`) and core-only stores reject Trading commands (`422`). |
| CEO Direct Line & no `retire_employee` | Send `send_ceo_message` to `ceo_agent_id`; verify non-CEO targets fail closed (`400`/`403`) and zero `retire_employee` Board commands exist. |

**Oracle:** Compare user action, `command_id`, service admission (`command_receipt`), `(company_id, principal_id)` `/api/local/pending` state, and domain effective revision across [Usage scenarios](../../scenarios.md). Verify zero commands from merely browsing.

## Operations and change review

Expose `pending_age`; if a receipt cannot be obtained keep `unknown` visible and `controls_locked = true`. Closing the app never resumes suspended risk or clears an active restriction.
