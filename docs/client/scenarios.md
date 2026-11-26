# Board Client usage scenarios and state transitions

Design draft · [Board Client](README.md) · [Client architecture](architecture.md) · [Client screens and navigation](inspect/screens/spec.md) · [Agents & Activity](inspect/office-floor-map/spec.md) · [Company Messages](governance/ceo-direct-line/spec.md) · [Flow observability](inspect/flow-observability/spec.md) · [Command submission](commands/submit/spec.md)

This walkthrough specifies ten concrete end-to-end operational scenarios across the implemented Modular IA (`Approvals`, dynamic `Trading`, `Company`, `System`), `ClientWebBridge`, `CompanyDaemon`, and `ResidentCompanyService`. Cross-check against [Acceptance episodes](../acceptance/episodes.md) and [Hired-Agent paper walkthrough](../acceptance/hired-agent-continuity/paper-walkthrough.md).

---

## Scenario 1: Company Founding (`bootstrap_locked` Owner Registration) & Core-Only Readiness

- **Flows covered:** Flow 01, Flow 11
- **Actors & Preconditions:** Local company store initialized via `sourdough found` (`installed_domains = {'core'}`, `schema_version = 1`, `charter_revision = "c0"`, zero `board_binding` rows).

### Step-by-step state transitions

1. **Discovery:** Client opens on `/` -> `GET /v1/meta` returns `{company_id, store_key, schema_version: 1, active_domains: ["core"], board_access: {bootstrap_locked: true, bootstrap_challenge: ...}}`. `App.tsx` renders `<BootstrapView />` on `/bootstrap`, locking non-bootstrap commands.
2. **Challenge & Confirmation:** Operator enters principal binding details and confirms `"Register Owner Binding"`. UI mounts `ControlledInlineConfirm` (`SDKButton` rejecting `details?.source === "agent"`, `data-agent-confirm="Register owner binding"`) contrasting *What changes* (enrolls owner principal with `board_control`) vs *What stays untouched* (charter and autonomous runtime remain intact), with `company_id`, `store_key`, `charter_revision`, and `bootstrap_challenge` collapsed under `"Technical details"` (`OwnerProof` secrets stay outside DOM and logs).
3. **Commit (`POST /v1/commands` `register_owner`):** On human confirm, `ClientWebBridge` persists `0o600` `O_NOFOLLOW` `/api/local/pending` scoped to `(company_id, principal_id)` -> submits `POST /v1/commands` with `Idempotency-Key = command_id`. `CompanyDaemon` atomically consumes the challenge, inserts `board_binding` (`binding_revision = 1`) + `role_assignment` (`board_control`) + `command_receipt(applied)` + `audit_event`, clears `bootstrap_locked`, and notifies `/v1/events`. UI deletes the pending file, transitions `CommandLifecycle` to `applied` in inline `CommandOutboxPanel`, refreshes `GET /v1/meta`, and routes `/` -> `/agents` (when `"trading"` is absent) or `TradingDeskView` (when `"trading"` is installed).

- **Verifiable Assertions:** Two concurrent bootstrap attempts yield at most one `board_binding` (`binding_revision = 1`); `OwnerProof` never appears in DOM/storage.

---

## Scenario 2: Dual-Entry Approvals (`/approvals`), Contextual Ask CEO (`/messages`) & Refused Domain Actions (`application_status = 'failed'`)

- **Flows covered:** Flow 06, Flow 10, Paper-to-Live Migration
- **Actors & Preconditions:** `ResidentCompanyService` has escalated a reserved `decision_question` (e.g., `owner_authority` hiring approval or `live_promotion` migration approval, carrying `question_id`, `revision`, `payload_hash`, `respond_by_ns`).

### Step-by-step state transitions

1. **Dual-Entry Approval Card (`/approvals/:questionId` or inline `ContextualApprovalCard`):** Client renders plain-language summary, quantified impact (`impact`), linked evidence, `respond_by_ns` deadline, and equal-weight **`Approve`**, **`Decline`**, and **`"Ask CEO about this item"`** actions.
2. **Contextual Ask CEO:** Clicking `"Ask CEO about this item"` transitions in-place (zero right drawer) to `/messages?context_kind=decision_question&context_id=<question_id>` with a pinned read-only **Context Attachment Card** (`status = "open"`, zero inline `Approve` button) and posts a directional message via `send_ceo_message` (`ceo-chat-command-v1`). After `ceo.respond_message` commits the reply and triggers `/v1/events`, the Board navigates back to `/approvals/:questionId`.
3. **Confirm & Commit (`POST /v1/commands` `submit_decision_answer`):** Authenticated `board_control` principal confirms `Approve` via `ControlledInlineConfirm` (`data-agent-confirm`). `CompanyDaemon` atomically records the answer, invokes `ResidentCompanyService` (`board.apply_decision`), and updates `decision_question.application_status`.
4. **Fail-Closed Refusal Branch (`application_status = 'failed'`):** If a prerequisite changed between nomination and approval (e.g., an active `restriction_cause` or open `trading_book_order` blocks `live_promotion`), `board.apply_decision` catches `ExecutionTierRefused` / `DomainActionRefused`, keeps `company_live` inactive, and commits `decision_question.application_status = 'failed'` (permitted by `08_board_client.sql`) with the exact failure reason surfaced in `ApprovalsView` and `TradingDeskView`.
5. **Separate Application vs Onboarding Readiness (`/reports` & `/agents`):** Hiring approvals keep application status separate from `hire_onboarding.state` (`provisioning -> blocked -> ready -> accepted`).

- **Verifiable Assertions:** `"Ask CEO about this item"` attaches live read-only status with zero inline command execution buttons; refused domain actions record `application_status = 'failed'` cleanly without leaving partial live bindings.

---

## Scenario 3: Scoped Restrictions (`/controls`), Competing Causes & Trading `15s` Quote Freshness Resume

- **Flows covered:** Flow 11, Flow 03, Flow 05
- **Actors & Preconditions:** Authenticated `board_control` principal; Trading domain installed (`"trading" in active_domains`) with an active `execution_environment_binding` (`paper` or `live`).

### Step-by-step state transitions

1. **Company Pause or Trading Execution Pause (`/controls`, `pause_company` under `restriction-command-v1`):** Operator opens `/controls` (`RestrictionsView`) and selects either Core company pause (`scope_type = 'company'`) or Trading execution pause (`scope_ref IN ('execution:all', 'execution:paper', 'execution:live')`, `cause_category = 'trading_risk_pause'`). `ControlledInlineConfirm` contrasts *What changes* against *What stays untouched* (open fills, reconciliation, and research duties continue), capturing `payload.reason`.
2. **Domain-Gated Enforcement (`ipc/server.py`):** On core-only stores (`"trading"` absent), `pause_company` with `execution:*` or `trading_risk_pause` is rejected fail-closed with HTTP `422` and `RestrictionsView` omits `data.trading_risk` controls entirely. On Trading-enabled stores, active `execution:*` causes set `trading-desk-v1.risk_gate.suspended = true` and surface the sub-header `<Notice>` (`"Operational restrictions are currently active"` -> `/controls`).
3. **Competing Causes (`R1` vs `P2`):** Each restriction carries a distinct `restriction_id` and `expected_restriction_revision`. Clearing `rest-p1` leaves any active `rest-p2` intact; stale `expected_restriction_revision` returns `command_receipt(state="denied", denial_code="conflicting_revision")`.
4. **Trading Resume Precondition (`TradingRestrictionGuard`, `<= 15s` Quote Freshness):** `resume_company` targeting `execution:*` displays server-reported readiness telemetry (`unresolved_orders_count`, `unresolved_external_actions_count`, `settled_company_ok`, `server_freshness_note`) without a pre-fetched client countdown timer. `TradingRestrictionGuard` (`src/sourdough/trading/risk.py`) verifies zero open `trading_book_order` rows, zero unresolved `external_actions` for `live`, and `<= 15s` quote freshness before committing.
5. **Distinct Scoped Emergency Controls:** `interrupt_run` and `cancel_work` (`work-command-v1`) remain separate scoped controls on `/agents/:agentId`.

- **Verifiable Assertions:** Core-only companies never accept `execution:*` restrictions (`422`); clearing one `restriction_id` never clears another active stop; stale quote freshness (`> 15s`) or open orders fail closed while keeping the restriction active.

---

## Scenario 4: WAL-Normalized Checkpoints (`/backups`, `cut_checkpoint`) & 5-Step "Safe Custody Hold"

- **Flows covered:** Flow 11, Flow 05, Flow 03
- **Actors & Preconditions:** Active store (`schema_version = 1`) under `WriterLease`.

### Step-by-step state transitions

1. **Cut Checkpoint (`/backups`, `cut_checkpoint` under `checkpoint-command-v1`):** Operator confirms `cut_checkpoint` (`preconditions: {charter_revision}`) via `ControlledInlineConfirm`. Under `WriterLease`, backend stages & hashes `desk/`, emits `company.checkpoint_cut`, normalizes SQLite to `DELETE` mode with zero `-wal`/`-shm` sidecars, verifies `quick_check`/`foreign_key_check`, computes `LogicalWatermark`, and writes the `0o600` `sourdough-checkpoint-v2` archive.
2. **Restore Integrity & Domain Guard (`company_restore.py`, CLI/service):** Restoring a checkpoint enforces `RestoreDomainGuard` for every active domain (failing closed generically if a domain lacks a registered reconciler) and holds `sender_lease` (`.writer-lock`) during managed paper restore, recording `company_restore_receipt` and `company_store_retired` (`schema_revision = -1`).
3. **5-Step "Safe Custody Hold" View (`/backups`, `SystemSafetyView`):** Displays `checkpoints`, `restore_receipts`, `store_retired` status, and the 5-step **"Safe Custody Hold"** readiness diagnostics (`Data cut`, `Sole writer`, `Current authority`, `External reconciliation`, `Eligible next actions`).

- **Verifiable Assertions:** Concurrent `WriterLease` contention fails closed (`503`); post-restore UI displays `restore_receipts` and hold status without duplicating orders or erasing custody records.

---

## Scenario 5: Strategy Releases & Rollback (`/trading/strategies`) and Audit / Evaluations (`/audit`)

- **Flows covered:** Flow 09, Flow 12, Flow 13
- **Actors & Preconditions:** `board_control` principal; Trading domain installed for `/trading/strategies`; Core `/audit` available on all companies (`?tab=log|evals|daemon`).

### Step-by-step state transitions

1. **Strategy Releases & Challenger Trials (`/trading/strategies`, `strategies-v1`):** Displays `trading_strategy_release`, `trading_pit_catalog_manifest`, `trading_challenger_trial` (`PROMOTE | REJECT | INCONCLUSIVE`), and active `trading_strategy_deployment`.
2. **Non-Auto-Activation & Non-Self-Activation (`activate_strategy_release` / `rollback_strategy_deployment`):** `PROMOTE` never activates automatically; `INCONCLUSIVE` / `REJECT` disable activation; author self-activation fails closed. Authorized deployer confirms `activate_strategy_release` (`strategy-command-v1`, `preconditions: {binding_revision, expected_active_deployment_id?}`) or forward `rollback_strategy_deployment` (`preconditions: {expected_active_deployment_id}`), preserving open `trading_book_order` / `trading_book_fill` rows and costs.
3. **Audit Hub (`/audit?tab=log|evals|daemon`):**
   - `tab=log` (`system-audit-v1`): `events` (`audit_event`), `conditions`, `active_fences`, `board_bindings`.
   - `tab=evals` (`system-evals-v1`): `qualifications` (`ThreeStageVerdict`), `activations` (`blast_radius_descendants` DAG preview), and `offline_evaluator` Flow 13 read-only suite summaries (`revision`, `sealed_at_ns`, `observation_count`, `grade_count`, `pairwise_grade_count` from `trial_registry.py`).
   - `tab=daemon`: daemon PID, `store_key`, `service_incarnation_id`, `event_seq`, `active_domains`, and `board_access` diagnostics linked from `SidebarFooter`.

- **Verifiable Assertions:** Self-activation fails closed; forward strategy rollback preserves open orders/positions; `/trading/strategies` returns `404` / is unmounted when `"trading"` is not installed.

---

## Scenario 6: Symmetric `TradingDeskView` (`/`, `/trading/live`, `/trading/paper`), `OrderTraceView` (`/orders/:orderId`), `promote_live` & Shadow Benchmark

- **Flows covered:** Flow 03, Flow 04, Flow 07, Flow 08, Flow 11, Paper-to-Live Migration, Unified Trading Desk
- **Actors & Preconditions:** Trading domain installed (`"trading" in active_domains`); `N >= 1` `company_paper` bindings and/or an active `company_live` binding in `execution_environment_binding`.

### Step-by-step state transitions

1. **Symmetric Live & Paper Cockpit (`TradingDeskView(mode="live" | "paper")`):**
   - `/` and `/trading/live` query `GET /v1/projections/trading-desk?environment=live`. When no `company_live` binding is active, Live renders `Live Execution Standby` with a `0%` baseline chart and eligible `live_candidates` (`paper_soak_ready`, passing `venue_qualification_receipt`, `promotion_payload_hash`).
   - `/trading/paper` queries `GET /v1/projections/trading-desk?environment=paper&book_id=...`, supporting switching between concurrent `company_paper` books and aggregate view.
   - Both modes render the identical 4-KPI top band, `PortfolioChart` (`0%` / `$0` baseline with `null`-gap breaks), `pnl_reconciliation_bridge` (`"Unavailable"` / `"No reading"` when close data is missing — never `$0.00` or `0.00%`), `open_positions`, and grouped `execution_tape` (`cancel != flat`).
2. **Board-Approved Migration & `promote_live` (`promote_execution_environment` / `live_promotion`):** After `live_promotion` succeeds (or direct `promote_execution_environment` under `execution-tier-command-v1`), `trading_live_migration_record` records `positions_copied = 0`, `company_live` activates flat at `0.0%`, `environment_promotion_record.action === "promote_live"` renders as a success event in `TradingDeskView`, and the linked `shadow` paper book overlays on Live's `PortfolioChart` via `shadow_benchmark`.
3. **Full-Screen Causal Order Trace (`/orders/:orderId?env=${mode}`, `order-trace-v1`):** Clicking any `execution_tape` row transitions in-place (zero `Y.Sheet`) to `OrderTraceView` reading `trading_book_order`, `trading_book_fill`, `trading_fee_delivery`, `trading_book_position`, `execution_environment_binding`, `work_runs`, `external_actions`, and `shared_publication`, with `Ask CEO` (`kind: "trading_order"`) and environment-aware back-links (`/trading/live`, `/trading/paper`, or `/agents/:actorId` for `agent_sandbox`).

- **Verifiable Assertions:** `paper` and `live` capital never commingle; `positions_copied = 0`; `rec.action === "promote_live"` renders as a promotion success; missing accounting close renders `"Unavailable"` / `"No reading"` rather than `0`.

---

## Scenario 7: Disconnect During `POST /v1/commands` & `(company_id, principal_id)` `/api/local/pending` Recovery

- **Flows covered:** Flow 11, Flow 05
- **Actors & Preconditions:** Authenticated `board_control` principal connected (`link_state = "connected"`, `!controls_locked`).

### Step-by-step state transitions

1. **Draft & Confirm (`idle -> draft -> submitting`):** Operator confirms an action in `ControlledInlineConfirm` (`details?.source !== "agent"`), handing off tracking to inline `CommandOutboxPanel` strictly below the topbar.
2. **Write-Ahead Persist (`0o600` `O_NOFOLLOW` `fsync`):** Before/upon transmitting `POST /v1/commands`, `ClientWebBridge` persists `{company_id, principal_id, command_id, command_type, contract_version, preconditions, payload, label, sent_ns}` via `/api/local/pending` scoped to `(company_id, principal_id)`. `CommandOutboxPanel` shows `state = "pending"`, live `pending_age`, and **omits `Check status` (`onLookup`)**.
3. **Disconnect / Timeout (`submitting -> unknown`):** Daemon socket closes or times out. `command_state` becomes `"unknown"`, `controls_locked = true`, pending record is retained, and `CommandOutboxPanel` enables **`Check status` (`onLookup`) solely in this state**.
4. **Reconnect / Restart Receipt Resolution (`GET /v1/commands/{command_id}?operation=...`):** Without minting a second `command_id`, UI queries the original `command_id`:
   - **Branch A (`200` `applied`):** Delete `/api/local/pending`, transition `unknown -> applied` (`confirmed`), refresh projections, unlock controls.
   - **Branch B (`200` `denied` + `denial_code = "conflicting_revision"`):** Delete `/api/local/pending`, transition `unknown -> denied` (`failed`), require fresh review.
   - **Branch C (`200` `accepted`):** Retain pending record, transition `unknown -> accepted` (`pending`), keep `controls_locked = true`.
   - **Branch D (`404` `not_found`):** Retain pending record, transition `unknown -> not_found_retry_choice` (`unknown`, `onLookup` omitted), keep `controls_locked = true`, and render **`Retry same request`** (resubmitting exact persisted `command_id`, `command_type`, `contract_version`, `preconditions`, and `payload`) or **`Discard draft`**.
   - **Branch E (`403` on startup recovery of stale/foreign-principal pending file):** Quarantine/purge the stale file safely so the active principal is never deadlocked.

- **Verifiable Assertions:** Zero duplicate `command_id`s minted across timeout/restart; `Check status` absent except when `command_state === "unknown"`.

---

## Scenario 8: Transient Disconnect vs. `401`/`403`/Principal/Scope Switch vs. Host/Session Lock

- **Flows covered:** Flow 11, Flow 01, Flow 05
- **Actors & Preconditions:** Connected principal viewing projections (`as_of_ns = T0`, `event_seq = S0`).

### Three strictly disjoint state transitions

1. **Transient Disconnect / Daemon Restart (`ECONNREFUSED` / EOF / timeout / `503`):** Sets `link_state = "disconnected"`, `stale = true`, `controls_locked = true`. Last verified `Data State` stays visible beneath the sub-header stale `Notice` (`"Live connection interrupted — showing last known snapshot"`); `SidebarFooter` `<Status>` shows `"Reconnecting"`. Zero `clearData()`.
2. **Authorization Loss (`401`/`403`) or Company / `store_key` / Principal Switch:** Executes **immediate zero-grace `clearData()`** (`clearInMemoryCommandState()`, protected DOM wiped) and displays `"Access denied"` / empty state.
3. **Host / Session Lock (`host_locked = true`):** Mounts opaque full-viewport Privacy Shield (`data-agent-hidden`) over protected DOM without mutating `data_as_of_ns` (`T0`), `stale`, or `LinkState`. Unlocking re-verifies `GET /v1/meta` before lifting the shield.

- **Verifiable Assertions:** Transient disconnect never wipes verified data; `401`/`403`/principal switch wipes protected DOM immediately; host lock conceals DOM without marking `T0` stale.

---

## Scenario 9: `AgentsActivityView` (`/agents`), `AgentDetailView` (`/agents/:agentId`, `Trading Sandbox`), `.desk/drafts/*` Opacity & CEO-Delegated Custody (`/reports`)

- **Flows covered:** Flow 02, Flow 06, Flow 10, Modular IA
- **Actors & Preconditions:** Multiple hired Agents operating inside `ResidentCompanyService`.

### Step-by-step observability & custody transitions

1. **Agent Activity Graph & List (`/agents`, `agents-graph-v1`):** Renders `Graph | List` toggle (respecting server `default_view_mode`), compact agent nodes with status, current work, budget, `.desk/drafts/*` opacity (`"Private Desk · N drafts (not shared)"`), conditional Tier 1 sandbox badge (`sandbox_activity` present only when authorized and active), and clickable collaboration edges navigating to `/messages`.
2. **Agent Detail Drill-Down (`/agents/:agentId`, `agent-detail-v1`):** Selecting an agent transitions in-place (zero `Y.Sheet`) with `<nav aria-label="Breadcrumb">` and tabs: `Work Activity` (with `interrupt_run` / `cancel_work`), `Interactions & Talks` (read-only `project_message`, zero non-CEO Board composer), `Published Reports` (`shared_publication` excerpts), `Peer Requests & Handoffs`, and conditional **`Trading Sandbox`** tab shown only when `"trading.agent_sandbox"` / `"quant.agent_sandbox"` or sandbox portfolio activity is present (`trading_book_*` with `environment = 'agent_sandbox'`). Back/`Escape` restores focus to the originating agent node/row.
3. **CEO-Executed Retirement & Read-Only Board Custody Audit (`/reports`, `org-v1`):** CEO executes `retirement.py` (`TERMINATION_SCOPE = "retire-agent"`); `/reports` renders `"Managed by CEO · Board view is read-only"` above `delegated_hr_custody` with zero Board `retire_employee` writes.

- **Verifiable Assertions:** Private `.desk/drafts/` bodies never appear in UI projections; `project_message` never creates `work_items`; zero non-CEO Board chat composer exists; `/reports` offboarding custody ledger is strictly read-only.

---

## Scenario 10: Core-Only Company Fallback (`/agents`), Dynamic `DomainModule` Gating & `SidebarFooter` Diagnostics

- **Flows covered:** Flow 01, Flow 02, Flow 11, Flow 12, Flow 13
- **Actors & Preconditions:** Core-only company (`active_domains = ["core"]`, `"trading"` not installed).

### Step-by-step state transitions

1. **Core-Only Navigation & Redirect (`/` -> `/agents`):** `domains/registry.ts` finds zero active `DomainModule` entries (`homeMode = "core_only"`). Visiting `/` redirects (`replace: true`) to `/agents`; the sidebar renders only `Approvals`, `Company` (`Agents`, `Messages`, `Finance`, `Reports`), and `System` (`Controls`, `Backups`, `Audit`, `Settings`), omitting `Trading` completely.
2. **Core-Only View Honesty & Daemon Exclusion:** `AppShell` header renders its strict 4 slots (`Brand`, `Approvals`, `⌘K`, `Theme Toggle`) with zero `"Paper"` badge and zero trading risk CTAs. `FinanceView` shows core GL balances/journals/closes without trading statements; `RestrictionsView` omits `trading_risk` controls; `CompanyDaemon` returns `404` on `/v1/projections/trading-desk`, `/v1/projections/orders/*`, `/v1/projections/strategies` and `422` on Trading commands.
3. **`SidebarFooter` Diagnostics Reachability:** Clicking the `SidebarFooter` `<Status>` button (`data-focus-id="sidebar-diagnostics-trigger"`) navigates directly to `/audit?tab=daemon` from any viewport or collapsed sidebar state without adding a fifth header slot.

- **Verifiable Assertions:** Core-only companies operate with zero trading tables, zero `$0.00` trading placeholders, zero Trading nav/commands, and clean `/agents` landing.
