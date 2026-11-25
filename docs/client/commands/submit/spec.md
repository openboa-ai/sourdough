# Submit a command and resolve its receipt

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [CEO Direct Line](../../governance/ceo-direct-line/spec.md) · [Approvals & Custody](../../governance/approvals-and-custody/spec.md) · [Usage scenarios](../../scenarios.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Human query and control access is through Client. Route a received command to its domain owner; a successful submission is not an applied change. Agent and service-originated operations remain independent of Client. Routine employee retirement (`retirement.py`, `TERMINATION_SCOPE = "retire-agent"`) is strictly CEO-delegated inside `CompanyRuntime`—there is **no Board `retire_employee` write command**.

Send only after an explicit board action: authenticated principal, company, typed operation, request ID (`command_id`), argument digest (`payload_sha256`), expected preconditions (HTTP JSON `preconditions` object persisted in SQLite `command_receipt.preconditions_json`), and `payload` (carrying `payload.reason` of `1..2000` chars covered by `payload_sha256` where required by the command registry; `register_owner` carries `OwnerChallenge`/`OwnerProof` in `payload`). A profile control such as `suspend_new_risk` or `resume_new_risk` uses its registered service contract; it is not required by the base client.

An optional paper-trading-runtime configuration or start command (`configure_paper_runtime` / `start_paper_runtime`, `paper-runtime-command-v1` `[BACKEND_GAP #7]`) uses the same public command/receipt boundary and only an explicit authorized client action (`environment == "paper"`). The service owns validation, credential handling, and actual setup. The base client need not have a trading profile, and the company can configure and start the runtime without any client.

Input contracts: [Event stream, runtime wakeups, and typed commands](../../control-plane/commands-and-events/spec.md) · [Versioned charter command](../../../company/systems/organization/found-company/amend/spec.md) · [Board control](../../../company/profiles/quant/trading/board-control/apply-control/spec.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../../inspect/screens/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Submit commands over `CompanyDaemon`'s versioned HTTP/1.1 + JSON + NDJSON Unix domain socket Control Plane (`POST /v1/commands` with `Idempotency-Key` matching `command_id` and typed multi-field `preconditions` stored in `command_receipt.preconditions_json`, plus `GET /v1/commands/{command_id}?operation=<command_type>` where `?operation=` is required under `(principal, operation)` scoping; the command registry explicitly enumerates both `snake_case` operations and dotted `review_tool.{activate,revert,quarantine}`).

### 1. Host transport and `(company_id, principal_id)` `board-pending` persistence (`[BACKEND_GAP #1, #2]`)

The React 19 + `@openboa/yogurt` Client UI dispatches commands through the authenticated `sourdough client-web` loopback HTTP-to-UDS bridge (`[BACKEND_GAP #1]`, or a capability-probed `zero://app` Native SDK `0.11.1` WebView host) described in [Client architecture](../../architecture.md). Upon dispatching an active `POST /v1/commands` request, the host/bridge atomically writes `LocalCommandIntent` (`{company_id, principal_id, command_id, command_type, contract_version, preconditions, payload, payload_sha256, sent_ns}`, storing the full canonical `preconditions` object/JSON — including `charter_revision`, `expected_revision`, and all extended preconditions — plus `contract_version`, keeping `expected_revision` only if derived for legacy display) inside an `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` (`0o700`) directory using `O_CREAT | O_EXCL | O_NOFOLLOW` (`0o600`), `fsync(fd)`, atomic rename, and `fsync(dir_fd)` — **solely for already-transmitted unknown requests** (clearing immediately if transport fails before request bytes are transmitted, and retaining it only when delivery or reply status is unknown). Scope `board-pending` storage strictly by **`(company_id, principal_id)`** (`{base_dir}/{company_id}/{principal_id_sha256}.json`), never by `company_id` alone; on startup recovery, if `GET /v1/commands/{command_id}` returns `403` for a stale or unowned pending file, quarantine/purge that stale file with an explicit security notice so another principal is never deadlocked. `board-pending` never stores offline queued mutations for automatic replay.

### 2. 8-variant `CommandLifecycle` and inline `CommandOutboxPanel` (`Receipt`)

Maintain the exact 8-variant `CommandLifecycle` state machine: `idle`, `draft`, `submitting`, `unknown`, `accepted`, `applied`, `denied`, `not_found_retry_choice`. Represent `conflicting_revision` strictly as `state = "denied"` with `denial_code = "conflicting_revision"` (never a 9th lifecycle variant). Mount `CommandOutboxPanel` strictly as a **persistent, non-auto-dismissing inline banner below the 4-element `48px` Global Header** (never inside the header and never inside a right slide-over drawer), wrapping `Receipt`:
- `submitting` and `accepted` render `ReceiptRecord.state = "pending"` with elapsed `pending_age` (computed from `sent_ns`) and **omit `onLookup` (`Check status`)**.
- `unknown` renders `ReceiptRecord.state = "unknown"` and **passes `onLookup` (`"Check status"` / `"Check original request"`) solely in this state**.
- `applied` deletes the pending file first (failing closed to `unknown` + `controls_locked` if delete fails) and renders `ReceiptRecord.state = "confirmed"`.
- `denied` deletes the pending file first and renders `ReceiptRecord.state = "failed"` with `denial_code` (requiring a brand-new human decision against the latest revision when `conflicting_revision`).
- `not_found_retry_choice` (`404` on receipt lookup) retains the pending file, omits `onLookup`, keeps `controls_locked = true`, and renders an explicit choice: **`Retry same request`** (resubmitting the exact persisted `command_id`, `command_type`, `contract_version`, `preconditions`, and `payload` without reading newer live revisions) or **`Discard draft`** (deleting the pending file and returning to `idle`).

### 3. Universal Inline Human-Language Confirmation (`ControlledInlineConfirm` on `@ui-sdk/react/button`, zero popup modals)

Every state-mutating governance or safety command confirms **in-place via an inline expandable confirmation card or sub-header safety strip** (`ControlledInlineConfirm` built on `@ui-sdk/react/button` `SDKButton`; **zero dimmed popup modals and zero right slide-over drawers**). The inline confirmation surface:
1. Rejects automated invocation (`if (details?.source === "agent") return;`) and places `data-agent-confirm="<consequence>"` solely on the confirm CTA (never on cancel or secondary navigation).
2. Contrasts **`What changes`** (`<dl>` rows in plain executive language) against **`What stays untouched`** (`<dl>` rows listing unaffected operations, e.g., for `suspend_new_risk`: open positions, in-flight fills, reconciliation, and non-order research duties continue undisturbed).
3. States a plain-language **consequence / risk note**, uses a **consequence-verb CTA** (e.g., `"Pause New Paper Orders"`, `"Resume Paper Trading"`, `"Promote Strategy v4"`, `"Confirm Quarantine"`, never generic `"OK"`), and captures `payload.reason` (`1..2000` chars) where required by the command registry.
4. Collapses composite optimistic preconditions and technical identifiers (`command_id`, `command_type`, `contract_version`, `charter_revision`, `expected_revision`, `expected_mandate_revision`, `expected_restriction_revision`, `payload_sha256`, `lease` / `fencing_token`) inside a **closed-by-default Tier 2 `"Technical details"` disclosure**.
5. Embeds specialized safety previews where required:
   - **`release_restore_quarantine` (`[BACKEND_GAP #7]`):** 5-step **"Safe Custody Hold"** readiness checklist (`Data cut`, `Sole writer`, `Current authority`, `External reconciliation`, `Eligible next actions`; confirm CTA disabled until steps 1–4 are `VERIFIED`).
   - **`review_tool.quarantine` (`[V1_SQLITE_NEEDS_UDS]`):** Pre-computed indented DAG **Blast-Radius Tree Preview** (`parent_asset_id` transitive descendants) + automatic global `revision DESC` fallback target (`restored_candidate_id` or `"No change to active tool"`).
6. Latches the composite preconditions on expand (`opened.current`), detects mid-review updates (`changed`), prevents double-submission (`sent.current`), and collapses cleanly upon human click to hand off execution state to `CommandOutboxPanel`.

### 4. Preconditions & competing-stop non-clearing (`[LIVE_UDS]` vs `[BACKEND_GAP #2, #7]`)

On `origin/main` (`6694fa9`), `POST /v1/commands` restricts `paper-risk-command-v1` strictly to `_ALLOWED_PRECONDITION_KEYS == {"charter_revision", "expected_revision"}` (`suspend_new_risk`, `resume_new_risk` — `[LIVE_UDS]`) and `serve-company-daemon` binds `authorize_board=None` (fail-closed `403`, `[BACKEND_GAP #2]`). Under `[BACKEND_GAP #2, #4, #7]`, `board_binding` and `role_assignment` enable scoped `board_control` commands, `GET /v1/commands/{command_id}?operation=...` is scoped to `(principal, operation)`, and multi-cause `RestrictionView` preconditions (`{restriction_id, expected_restriction_revision, charter_revision}`) use `restriction-command-v1` (`pause_company`, `resume_company`) and `paper-risk-command-v2` (cause-scoped `suspend_new_risk`, `resume_new_risk`) rather than overloading `paper-risk-command-v1`, assigning a distinct `restriction_id` per cause so clearing `R1` (`rest-p1`) never clears an active competing stop `P2` (`rest-p2`).

### 5. Server-side `QUOTE_MAX_AGE_NS = 15s` freshness (`resume_new_risk`, `[LIVE_UDS]`)

`resume_managed_paper_risk` evaluates `_settled_company` and `venue.read_current_risk_cut()` (`QUOTE_MAX_AGE_NS = 15s`) **server-side inside `POST /v1/commands`** across Phase 1 (lock-free read) and Phase 2 (`BEGIN IMMEDIATE` verifying `observed_ns <= time_ns() <= expires_ns`). The Client MUST NOT render a pre-fetched 15-second modal countdown prior to clicking Confirm; instead display non-binding settlement/quote readiness telemetry (`settled_company_ok`, `unresolved_orders_count`, `pending_binding_change`, `last_quote_age_ns`) alongside plain-language explanation of the server-side `15s` verification, and handle retryable server `400` expiry errors cleanly without mutating `paper_risk_state`.

### 6. Advisory Board ↔ CEO 1:1 Direct Line command (`send_ceo_message`, `[BACKEND_GAP #3]`)

`send_ceo_message` (`ceo-chat-command-v1`) allows an authenticated `board_control` principal to post advisory text (`preconditions: {ceo_agent_id, charter_revision}`, `payload: {target_agent_id, body, context_ref?}`, including optional read-only `context_ref` from `Contextual Ask CEO`) strictly to the designated CEO (`payload.target_agent_id === preconditions.ceo_agent_id === ceo_agent_id` / `founder_id`), carrying `Idempotency-Key == command_id` without requiring a blocking modal confirmation sheet per chat turn. The control plane fails closed (`400`/`403`) on any non-CEO target, and chat bubbles never embed inline state-mutating execution buttons.

### 7. Command provenance summary (`v1` `6694fa9`)

- **`LIVE_UDS`**: `suspend_new_risk` & `resume_new_risk` (`paper-risk-command-v1`, server `15s` cut), `POST /v1/commands` idempotency, `GET /v1/commands/{command_id}` (lacks `?operation=` / `principal_id` scoping and `cli.py` sets `authorize_board=None` until `BACKEND_GAP #2`).
- **`V1_SQLITE_NEEDS_UDS`**: `activate_strategy_release`, `rollback_strategy_deployment`, `review_tool.{activate,revert,quarantine}` (`admit_command_receipt` exists, needs UDS handler + `peer_pid`); `interrupt_run`, `cancel_work`, `change_paper_binding`, `configure_paper_runtime`, `start_paper_runtime`, `cut_checkpoint`, `restore_checkpoint` (mechanisms exist, need UDS command handlers in `BACKEND_GAP #7`).
- **`BACKEND_GAP #1–#4, #7`**: `#1` `sourdough client-web` & `(company_id, principal_id)` `board-pending`; `#2` `register_owner`, `rbac-command-v1`, `authorize_board`, `(principal, operation)` receipt lookup; `#3` `send_ceo_message`; `#4` `submit_decision_answer`, `amend_charter`, `revoke_grant`, declarative registry; `#7` `pause_company`, `resume_company`, `paper-risk-command-v2`, `release_restore_quarantine`, operational UDS wiring. (**`retire_employee` is excluded from Board writes** — CEO-only via `retirement.py`.)

**Concern for the responsible component:** A timed-out click might already have committed. Query the original receipt under `(company_id, principal_id)` authorization; do not allocate a second request ID on reconnect, never auto-replay an offline draft, and never allow clearing one restriction cause to bypass another active stop.

## Functional and quality requirements

- **FR.** On stale revision (`conflicting_revision` persisted in `command_receipt`) require a new informed decision against the latest facts. An exact retry, where allowed by contract, reuses the original `command_id`, arguments, and typed multi-field `preconditions` (persisted in `command_receipt.preconditions_json`).
- **FR.** Persist only already-transmitted unknown requests in `(company_id, principal_id)`-scoped `board-pending`; on reconnect query `GET /v1/commands/{command_id}?operation=...`. If `404`, transition to `not_found_retry_choice` with explicit `Retry same request` vs `Discard draft` controls. If `403` on startup recovery of a stale file, quarantine/purge it safely.
- **FR.** Render `Receipt.onLookup` (`Check status`) strictly when `lifecycle === "unknown"`; mount `CommandOutboxPanel` inline strictly below the 4-element Global Header; confirm every mutating Board command through the Universal Human-Language Confirmation Sheet (*What changes*, *What stays untouched*, consequence note, consequence-verb CTA, collapsed `Technical details` disclosure, `SDKButton` rejecting `details?.source === "agent"`); never display a pre-fetched 15s countdown before `resume_new_risk`; enforce advisory-only `send_ceo_message` strictly to `ceo_agent_id`; exclude `retire_employee` from Board writes.
- **Compatibility.** Negotiate capabilities via `GET /v1/meta` before enabling mutations. Do not submit a command whose type or contract revision the service does not support. Reconnecting after an upgrade never translates an outstanding request into a new command.
- **NFR.** Double-click, refresh, and reconnect cannot apply a control twice. Neither cached grants nor client validation override the server. While disconnected, stale, `host_locked`, or `unknown`, lock all mutating controls fail-closed (`controls_locked = true`).

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal & Confirmation Sheet | Submit an allowed change via the Universal Confirmation Sheet. Verify *What changes*, *What stays untouched*, collapsed `Technical details`, immediate modal close handoff to inline `CommandOutboxPanel` (`pending`, no `Check status`), followed by `applied` and matching effective revision read from the service. |
| Failure or contention | Disconnect after commit but before response (`unknown` enables `Check status`). Reopen and recover the original `(company_id, principal_id)` receipt (`applied`, `denied(conflicting_revision)`, or `404` -> `not_found_retry_choice` `Retry same request`/`Discard draft`). Verify `403` stale-file quarantine does not deadlock a different principal. |
| Competing stops, 15s cut & safety sheets | Trigger `P1` and `P2`; verify stale `R1` (`conflicting_revision`) leaves suspension active (and `[BACKEND_GAP #7]` `R1` clearing `rest-p1` leaves `rest-p2` active). Force >15s Phase 1->2 delay on `resume_new_risk`; verify server `400` preserves `suspended = 1` cleanly. Inspect `release_restore_quarantine` 5-step **"Safe Custody Hold"** and `review_tool.quarantine` DAG **Blast-Radius Tree Preview**. |
| CEO Direct Line & no `retire_employee` | Send `send_ceo_message` to `ceo_agent_id`; verify non-CEO targets fail closed (`400`/`403`) and zero `retire_employee` Board commands exist. |

**Oracle:** Compare user action, `command_id`, service admission (`command_receipt`), `(company_id, principal_id)` `board-pending` file state, and domain effective revision across [Usage scenarios](../../scenarios.md). Verify zero commands from merely browsing.

## Operations and change review

Expose `pending_age`; if a receipt cannot be obtained keep `unknown` visible and `controls_locked = true`. Closing the app never resumes suspended risk or clears an active restriction.
