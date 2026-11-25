# Board Client usage scenarios and state transitions

Design draft · [Board Client](README.md) · [Client architecture](architecture.md) · [Client screens and navigation](inspect/screens/spec.md) · [Office Floor Map](inspect/office-floor-map/spec.md) · [CEO Direct Line](governance/ceo-direct-line/spec.md) · [Flow observability](inspect/flow-observability/spec.md) · [Command submission](commands/submit/spec.md)

This walkthrough specifies ten concrete end-to-end operational scenarios covering normal trading, Studio Floor Map drill-down, Board ↔ CEO 1:1 Direct Line, transient disconnect, contention failure, security wipe, privacy lock, CEO-delegated offboarding custody, and paper/evaluation continuity. Every step distinguishes **`[LIVE_UDS]`**, **`[V1_SQLITE_NEEDS_UDS]`**, and **`[BACKEND_GAP #1–#8]`** against `origin/main` (`6694fa9`). Cross-check against [Acceptance episodes](../acceptance/episodes.md) and [Hired-Agent paper walkthrough](../acceptance/hired-agent-continuity/paper-walkthrough.md).

---

## Scenario 1: Company Founding (`bootstrap_locked` Owner Registration) & Charter Amendment (`expected_mandate_revision`)

- **Flows covered:** Flow 01, Flow 11
- **Actors & Preconditions:** Local company store initialized via `sourdough found` (`[LIVE_UDS]` / `v1` `company(singleton=1, schema_revision=1, charter_revision="c0")`, `founding_receipt`, `company.founded` audit event) with zero `board_binding` rows (`[BACKEND_GAP #2]`).

### Step-by-step state transitions

1. **Discovery (`[BACKEND_GAP #2]`):** Client opens on `/` -> `GET /v1/meta` returns `{company_id, store_key, schema_version: 1, binding_state: "bootstrap_locked"}`. Router isolates the UI on `/bootstrap`, hiding operational company views and locking all non-bootstrap commands (`controls_locked = true` for everything except `register_owner`).
2. **Challenge & Confirmation (`[BACKEND_GAP #2]`):** Operator clicks `"Register Owner Binding"`. Host/daemon issues `OwnerChallenge(purpose="bootstrap", service_incarnation_id, nonce_digest)`. UI mounts the Universal Confirmation Sheet (`ControlledConfirmDialog` on `SDKButton`, rejecting `details?.source === "agent"`, `data-agent-confirm="Register owner binding"`) contrasting *What changes* (binds owner principal with `board_control`) vs *What stays untouched* (charter and autonomous runtime remain intact), with `company_id`, `store_key`, `charter_revision`, and `challenge_id` collapsed under `"Technical details"` (`OwnerProof` secrets stay outside DOM and logs).
3. **Commit (`POST /v1/commands` `register_owner`, `[BACKEND_GAP #2]`):** On human confirm, dialog closes immediately (`onOpenChange(false)`) -> `sourdough client-web` (`[BACKEND_GAP #1]`) persists `0o600` `O_NOFOLLOW` `LocalCommandIntent` scoped to `(company_id, principal_id)` -> submits `POST /v1/commands` with `Idempotency-Key = command_id`. Daemon atomically consumes `OwnerChallenge`, inserts `board_binding` (`binding_revision = 1`) + `role_assignment` (`board_control`) + `command_receipt(applied)` + `audit_event`, clears `bootstrap_locked`, and notifies `_event_cond`. UI deletes pending file, transitions `CommandLifecycle` to `applied` in inline `CommandOutboxPanel`, refreshes `GET /v1/meta`, and unlocks `/` (**Live Trading & Portfolio**).
4. **Charter Amendment (`amend_charter`, `[BACKEND_GAP #4]`):** On `/company/mandate`, operator views `charter_revision = "c1"` (`expected_mandate_revision = 1`), lowers the document review cost ceiling, and enters `payload.reason` (`1..2000` chars). Confirmation Sheet renders *What changes* (`Review cost ceiling updated`) vs *What stays untouched* (`Existing open work runs and booked positions`), latching `c1:1` in collapsed `"Technical details"`.
5. **CAS Contention & Failure Path (`conflicting_revision`):** Two operators (or a stale tab) submit `amend_charter` with `expected_mandate_revision = 1`.
   - First request commits `mandate_revision = 2`, returning `command_receipt(state="applied")`.
   - Second request hits CAS mismatch: daemon persists `command_receipt(state="denied", denial_code="conflicting_revision")` and returns HTTP `200` `{state: "denied", denial_code: "conflicting_revision", effective_revision: 2}`.
   - UI deletes the pending file, transitions `CommandLifecycle: submitting -> denied` (`ReceiptRecord.state = "failed"` in inline `CommandOutboxPanel`), refreshes `/company/mandate` to `mandate_revision = 2`, and requires a brand-new human decision against revision `2` (never auto-rebasing).

- **Verifiable Assertions:** Two concurrent bootstrap attempts yield at most one `board_binding` (`binding_revision = 1`); `OwnerProof` never appears in DOM/storage; stale `amend_charter` records `denial_code = "conflicting_revision"` with zero silent mutation.

---

## Scenario 2: Hiring Approval (`DecisionQuestion` / `Contextual Ask CEO`), Onboarding Readiness & Grant Revocation

- **Flows covered:** Flow 06, Flow 10
- **Actors & Preconditions:** `CompanyRuntime` / CEO (`[V1_SQLITE_NEEDS_UDS]` `staffing.py`) has staged `staffing_authority` and escalated a reserved `DecisionQuestion(kind="owner_authority", question_id, revision, payload_hash, respond_by_ns)` (`[BACKEND_GAP #4]`).

### Step-by-step state transitions

1. **1-Page Approval Card (`/company/approvals/:questionId`, `[BACKEND_GAP #4]`):** Client renders plain-language summary, quantified impact (`slots`, `turns`, `allowed_scope`, `execution_profile`), linked evidence, `respond_by_ns` deadline, and equal-weight **`Approve`**, **`Decline`**, and **`"Ask CEO about this item"`** actions.
2. **Contextual Ask CEO (`[BACKEND_GAP #3]`):** Before deciding, the Board member clicks `"Ask CEO about this item"` -> main workspace transitions in-place (zero right drawer) to `/ceo?context_kind=decision&context_id=<question_id>` with a pinned read-only **Context Attachment Card** (`status = "open"`, zero inline `Approve` button) and posts a directional message via `send_ceo_message` (`ceo-chat-command-v1`). After the CEO replies, the Board clicks `"View source item"` back to `/company/approvals/:questionId`.
3. **Confirm & Commit (`POST /v1/commands` `submit_decision_answer`, `[BACKEND_GAP #4]`):** Authenticated `board_control` principal clicks `Approve`, confirms in the Universal Confirmation Sheet (*What changes* / *What stays untouched*, latching `${question_id}:${revision}:${payload_hash}` in collapsed `"Technical details"`, `data-agent-confirm="Approve hiring decision"`). Host persists `(company_id, principal_id)` `LocalCommandIntent` and submits. Daemon atomically writes `AcceptedAnswer`, `AnswerApplication`, `staffing_choice`, `hire_receipt` (`agent`, `standing_grant`), and `command_receipt(applied)`. Returning to `/ceo` shows the pinned card updated to `"applied"`.
4. **Separate Application vs Onboarding Readiness (`/company/org` & `/office`, `[V1_SQLITE_NEEDS_UDS]` `onboarding.py`):** UI renders three independent statuses side-by-side (never collapsed into one `"Hired"` badge):
   - (a) **Answer Application:** `applied`.
   - (b) **Onboarding (`hire_onboarding.state`):** transitions `provisioning -> blocked` (showing exact blocker + `attempts/3` + `retry_at_ns`) -> `ready` -> `accepted` (also visible as an onboarding stage pill on the `/office` Studio Floor Map card).
   - (c) **Project Enrollment:** `enroll_project_participant` confirmation.
5. **Emergency Grant Revocation (`revoke_grant`, `[BACKEND_GAP #4]`):** On `/company/org` (or `/office/agents/:agentId`), operator confirms `"Revoke Standing Grant"` (`expected_authority_epoch = E`). Daemon atomically sets `standing_grant.scope = 'revoked'`, increments `authority_epoch = E + 1`, revokes `work_responsibilities`, cancels armed `work_conditions`, and fences active `work_runs` (`work.run_fenced`).

- **Verifiable Assertions:** `hire_onboarding.state` remains distinct from `AnswerApplication`; `"Ask CEO about this item"` attaches live read-only status with zero inline command execution buttons; revoked grant immediately fences active runs.

---

## Scenario 3: Emergency Stop / Paper Risk Suspension, Competing Stops (`P1`/`P2`) & Corroborated Resume (`QUOTE_MAX_AGE_NS = 15s`)

- **Flows covered:** Flow 11, Flow 03, Flow 05
- **Actors & Preconditions:** Authenticated `board_control` principal; active paper binding (`environment == "paper"`, `nautilus-spot-sandbox-v1`); at least one open `paper_order` in flight (`reserved_minor_units > 0`) alongside a running research duty.

### Step-by-step state transitions

1. **Suspend New Paper Risk (`P1`, `[LIVE_UDS]` UDS + `[BACKEND_GAP #2]` `authorize_board`):** Via Header Slot 4 (`Risk Gate`) or `/restrictions`, operator clicks `"Pause New Paper Orders"` -> Universal Confirmation Sheet (`data-agent-confirm="Pause new paper orders"`) contrasts *What changes* (`"New paper orders: Blocked immediately"`) vs *What stays untouched* (`"Open fills, position reconciliation, and research duties: Continue normally"`), latching `charter_revision` & `expected_revision = 0` inside collapsed `"Technical details"`, requiring `payload.reason`. Host writes `(company_id, principal_id)` `LocalCommandIntent`, sends `POST /v1/commands` (`suspend_new_risk`, `paper-risk-command-v1`).
2. **Partial Scope Verification (`[LIVE_UDS]` `paper_risk_state` & `[V1_SQLITE_NEEDS_UDS]` `paper_order.py`):** `paper_risk_state` transitions to `(suspended = 1, effective_revision = 1)`. `/` KPI 4 and `/restrictions` reflect the suspension. New `submit_paper_order` attempts are denied, **while** existing open order fills, `reconcile_paper_orders`, and non-order research duties continue visibly on `/` and `/office`.
3. **Competing Stop (`P2`) Race (`[LIVE_UDS]` vs `[BACKEND_GAP #7]` `RestrictionView`):** While operator prepares resume `R1` referencing `P1` (`expected_revision = 1` / `restriction_id = "rest-p1"`), a second stop `P2` lands (`effective_revision` becomes `2` in `v1`; distinct `restriction_id = "rest-p2"` row created in `RestrictionView`).
4. **Stale / Partial Resume (`R1`) Fails Closed:**
   - **`[LIVE_UDS]` (`paper-risk-command-v1`):** Submitting `R1` with `expected_revision = 1` commits `command_receipt(state="denied", denial_code="conflicting_revision")` and returns HTTP `200`. `CommandOutboxPanel` displays `failed` (`conflicting_revision`), `paper_risk_state.suspended` remains `1`, and operator must review revision `2` fresh.
   - **`[BACKEND_GAP #7]` (`RestrictionView`):** Submitting `R1` under `paper-risk-command-v2` (or `restriction-command-v1` for company pauses) targeting `{restriction_id: "rest-p1", expected_restriction_revision: 1, charter_revision}` clears `rest-p1` only; **`rest-p2` remains active** in `/restrictions` and the consolidated `Interrupt` `Notice`, keeping paper risk blocked until `R2` explicitly targets `rest-p2`.
5. **Corroborated Resume (`R2`) & Server-Side `15s` `QUOTE_MAX_AGE_NS` Cut (`[LIVE_UDS]` `paper_resume.py`):**
   - Operator opens `"Resume Paper Trading"`. Confirmation Sheet displays **non-binding** readiness telemetry (`settled_company_ok`, `unresolved_orders_count = 0`, `pending_binding_change = false`, `last_quote_age_ns`) and explains the server-side `15s` live-quote verification in plain language—with **zero pre-fetched 15-second client countdown timer**.
   - Confirm submits `POST /v1/commands` (`resume_new_risk`). Daemon executes Phase 1 (`_settled_company` + `venue.read_current_risk_cut()`, checking `15s` `QUOTE_MAX_AGE_NS`) -> Phase 2 (`BEGIN IMMEDIATE`, re-verifying `cut.observed_ns <= time_ns() <= cut.expires_ns`) -> commits `suspended = 0`, `effective_revision + 1`.
6. **Failure Branch (`15s` Expiry or Unsettled Order):** If >15s elapses between Phase 1 and Phase 2 or an unresolved order/missing fee exists, daemon returns HTTP `400`. `paper_risk_state.suspended` stays `1`; UI surfaces the exact blocker in `CommandOutboxPanel` / `Notice`.
7. **Distinct Scoped Emergency Controls (`[BACKEND_GAP #4, #7]`):** `pause_company` (`/restrictions`), `interrupt_run` & `cancel_work` (`/office/runs/:runId`), and `revoke_grant` (`/company/org`) remain separate scoped commands.

- **Verifiable Assertions:** `R1` never clears `P2`; server-side `15s` expiration fails closed with `suspended = 1`; existing fills/reconciliation continue while `suspended = 1`.

---

## Scenario 4: WAL-Normalized Checkpoints (`sourdough-checkpoint-v2`, `WriterLease`) & 5-Step "Safe Custody Hold" Restore Quarantine

- **Flows covered:** Flow 11, Flow 05, Flow 03
- **Actors & Preconditions:** Active `nautilus-spot-sandbox-v1` paper company (`schema_version = 1`) with an admitted `paper_order` at checkpoint cut; after checkpoint, the venue settles the fill and balance while the company store lags or faults.

### Step-by-step state transitions

1. **Cut Checkpoint (`/system/checkpoints`, `[V1_SQLITE_NEEDS_UDS]` `storage/checkpoint.py` + `[BACKEND_GAP #7]` `cut_checkpoint`):**
   - If another writer holds `WriterLease`, `hold_company_writer` fails with `WriterRefused` -> UI displays `503` writer-lease contention notice without mutating the store.
   - Under `WriterLease`, backend stages & hashes `desk/`, emits `company.checkpoint_cut`, normalizes SQLite to `DELETE` mode, verifies `quick_check`/`foreign_key_check`, computes `LogicalWatermark`, and writes `0o600` `sourdough-checkpoint-v2` zip. UI surfaces `cut_id` and `captured_ns` with `sha256`/`desk_digest` collapsed in `"Technical details"`.
2. **Restore Pre-Flight & `paper_sender_lease` Fencing (`/system/checkpoints`, `[V1_SQLITE_NEEDS_UDS]` `paper_restore.py` + `[BACKEND_GAP #7]` `restore_checkpoint`):**
   - Operator selects archive and confirms `restore_checkpoint` via the Universal Confirmation Sheet.
   - Backend acquires `hold_sender_lease(venue.journal)` (`0o600` `.writer-lock`), verifies `user_version == 1`, zero stray WAL sidecars, `connector_revision == "nautilus-spot-sandbox-v1"`, and matching `destination_id`.
3. **Exact-Cut Settlement & Store Retirement (`_settle_candidate`, `company_store_retired`, `[V1_SQLITE_NEEDS_UDS]`):**
   - Restore reconciles shadow orders against `venue.read_restore_cut(account_id)` without sending venue orders, inserts `company_restore_receipt`, retires original store (`company_store_retired`, `schema_revision = -1`), and swaps in replacement DB + Desk (crash-safe via `_resume_retired_restore`).
4. **Post-Restore 5-Step "Safe Custody Hold" Quarantine Gate (`[BACKEND_GAP #7]`):**
   - Replacement `CompanyDaemon` boots with `RestrictionView` `cause_category = "restore_quarantine"` (`0` new paper orders allowed), routed as a root `Interrupt` banner.
   - `/system/checkpoints` (and the `release_restore_quarantine` Confirmation Sheet) renders the **5-step "Safe Custody Hold" checklist** (`RecoveryReceipt`): (1) Data cut, (2) Sole writer, (3) Current authority, (4) External fill/fee reconciliation, (5) Eligible next actions (`release_restore_quarantine` / `resume_new_risk` disabled until steps 1–4 verify `VERIFIED`).

- **Verifiable Assertions:** Restoring while sender lease is held fails closed; post-restore UI displays booked post-checkpoint fill with zero duplicate orders; new paper risk stays locked until all 5 "Safe Custody Hold" dimensions verify.

---

## Scenario 5: Strategy Promotion & Forward Rollback (Flow 09), Review Tool `pass^3` & DAG Blast-Radius Preview (Flow 12), Offline Evaluations (Flow 13)

- **Flows covered:** Flow 09, Flow 12, Flow 13
- **Actors & Preconditions:** `board_control` principal inspecting candidate upgrades and evaluations across Company Management (`/company/strategy-trials`) and quiet `SYSTEM` views (`/system/tools`, `/system/evaluations/*`).

### Step-by-step state transitions

1. **Part A — Flow 09 Strategy Promotion & Forward Rollback (`/company/strategy-trials`, `[V1_SQLITE_NEEDS_UDS]` `strategy_comparison.py`):**
   - Operator inspects `StrategyRelease` (15 fields, hashes collapsed in Tier 2), anti-lookahead `PitCatalogManifest`, and paired `ChallengerTrial` (`PROMOTE | REJECT | INCONCLUSIVE`, `alpha_effective`, `lower_bound_bps`, `upper_bound_bps`, shared cost split).
   - **Non-Auto-Activation & Non-Self-Activation:** `PROMOTE` never activates automatically. `INCONCLUSIVE` / `REJECT` disable activation with plain reason chips. If `principal === release.owner_agent_id`, `Activate` fails closed (`"Strategy author grant cannot self-activate"`).
   - **Activation & Forward Rollback:** Authorized deployer confirms `activate_strategy_release` via Confirmation Sheet -> inserts `StrategyDeployment(status='active', program_identity='prog-...')`. Forward `rollback_strategy_deployment` targets a prior `'superseded' | 'rolled_back'` release, inserting a NEW `'active'` row while **preserving all existing open orders, fills, and costs**.
2. **Part B — Flow 12 Review Tool `pass^3` & DAG Blast-Radius Tree Preview (`/system/tools`, `[V1_SQLITE_NEEDS_UDS]` `evidence_review.py`/`review_tool_activation.py` + `[BACKEND_GAP #7]`):**
   - UI renders `ThreeStageVerdict` (`stage1_structure_and_negative`, `stage2_harmlessness`, `stage3_pass_k_stability` with `k >= 3`) and verifies `authors.isdisjoint(actors)` (`self_evaluation_refused`).
   - Before confirming `review_tool.quarantine` on faulted asset `A`, the Universal Confirmation Sheet embeds a pre-computed indented DAG **Blast-Radius Tree Preview** listing all transitive descendants `{A, B, C}` via `parent_asset_id` plus the automatic global `revision DESC` fallback target (`restored_candidate_id`, or `"No change to active tool"` if the active tool lies outside the quarantined subgraph). Separate maintenance columns track `local incident != external issue != published release != regression retest`.
3. **Part C — Flow 13 Offline Blind Evaluations (`/system/evaluations/*`, `[V1_SQLITE_NEEDS_UDS]` `trial_registry.py` + `[BACKEND_GAP #8]`):**
   - Physically isolated workspace over `offline-trial-registry-v1` (linked via quiet bottom `SYSTEM` footer / Header Slot 3 `"System Diagnostics"`) displays `Outcome` vs `Transcript` digests (`/suites`), `BlindedTrial` (`arm`, `slot_id`, `run_id` stripped) + `(AB, BA)` `tie_noise` + `RsiTransferSummary` (`/grading`), and `AccessRecord` sealed holdout locks + `parent_method_ids` method DAG (`/lineage`).

- **Verifiable Assertions:** Self-activation and self-evaluation fail closed; forward strategy rollback preserves open orders/positions; Flow 12 DAG Blast-Radius Tree Preview matches actual cascaded quarantine; Flow 13 `AccessRecord` locks persist across rollback.

---

## Scenario 6: Live Trading Entry (`/`), S04 Partial Fill + Remainder Cancel (`cancel != flat`), Two-Layer P&L `Unavailable` & `0%`/`$0` Chart

- **Flows covered:** Flow 03, Flow 04, Flow 07, Flow 08, Flow 11
- **Actors & Preconditions:** Active `nautilus-spot-sandbox-v1` paper mandate (`environment == "paper"`).

### Step-by-step state transitions

1. **3-Second `/` Cockpit Scan & `Since Last Visit` (`[V1_SQLITE_NEEDS_UDS]` + `[BACKEND_GAP #5, #8]`):** Operator lands on `/` (**Live Trading & Portfolio**): 4-KPI top band displays `Paper Portfolio Value & Return %` (with `Paper` badge and sealed valuation `as_of_ns` + `"No live market tick"` when `BACKEND_GAP #5` is absent), `Trading BOR P&L`, `Agent LLM Opex`, and `Company Net P&L & Risk Gate`. Directly below sits the single canonical collapsible **`Since Last Visit`** brief (`?since_seq=S0`, zero SQLite writes).
2. **S04 Partial Fill + Remainder Cancel (`0.002 - 0.0008 - 0.0002 - 0.001 = 0.001 BTC`, `[LIVE_UDS]` `work_projection.py:218-232` & `[V1_SQLITE_NEEDS_UDS]` `paper_books.py`):**
   - Live tape coalesces updates in `100–250ms` frames (`<= 400ms` new-fill highlight) grouping child fills under parent `Decision -> Order`:
   - Entry order quantity: `20` lots (`0.002 BTC`); partial fills `8` lots (`0.0008 BTC`) + `2` lots (`0.0002 BTC`) = `0.001 BTC`; remainder `10` lots (`0.001 BTC`) cancels (`released = 1`).
   - **Observed UI State on `/`:** Tape displays six distinct columns: `Ordered = 0.002 BTC`, `Filled = 0.001 BTC`, `Remaining Open = 0`, `Order Status = "cancelled"`, `Released Reservation = 0`, and **`Resulting Net Position = 0.001 BTC` (ACTIVE OPEN EXPOSURE — `cancel != flat`)**.
3. **Zero-Drawer Full-Screen Causal Drill-Down (`/orders/:orderId`):** Clicking the S04 row replaces the main workspace view (zero `Y.Sheet`) to `/orders/:orderId` (`Duty -> DutyRun -> ResearchFinding/Decision -> Order -> Fill -> Fee -> Cancellation -> Position -> Next Duty`) with `<nav aria-label="Breadcrumb">`, bidirectional jump links to authoring `/office/agents/:agentId` and `/company/org/publications/:assetId`, and `"Ask CEO about this item"`. Back/`Escape` restores exact keyboard focus to the tape row.
4. **Two-Layer P&L Reconciliation Bridge, `0%`/`$0` Chart & `Unavailable != 0` (`/`, `/performance`, `[V1_SQLITE_NEEDS_UDS]`):**
   - Period `PaperStatement` has complete Trading BOR (`trading.missing = ()`, `trading.pnl_minor != null`) while `accounting_close` has `CloseIssue("missing-cost-receipt")` (`operating_cost_atoms = null`, `company.pnl_minor = null`, `company.time_weighted_return = null`).
   - **Observed UI State:** Trading BOR renders exact numeric values, while Agent LLM Opex and Company Net display **`"Unavailable"`** in summary facts and **`"No reading"`** in table cells alongside `"missing-cost-receipt"` — **never `$0.00` or `0.00%`**. Cumulative Return % (`0%` baseline) / Cumulative P&L (`$0` baseline) chart breaks SVG line segments cleanly across `null` gaps.

- **Verifiable Assertions:** `0.002 - 0.0008 - 0.0002 - 0.001 = 0.001 BTC` active position remains visible despite `Order Status === "cancelled"`; missing accounting close renders `"Unavailable"` / `"No reading"` with `null`-gap chart breaks rather than `0`.

---

## Scenario 7: Disconnect During `POST /v1/commands` & `(company_id, principal_id)` `board-pending` Recovery

- **Flows covered:** Flow 11, Flow 05
- **Actors & Preconditions:** Authenticated `board_control` principal connected (`link_state = "connected"`, `!controls_locked`), viewing `effective_revision = 2` (`paper_risk_state`), `charter_revision = "c1"`.

### Step-by-step state transitions

1. **Draft & Confirm (`idle -> draft -> submitting`):** Operator confirms `"Pause New Paper Orders"` in the Universal Confirmation Sheet (`details?.source !== "agent"`). Modal closes immediately (`onOpenChange(false)`) handing off to inline `CommandOutboxPanel` strictly below the 4-element Global Header.
2. **Write-Ahead Persist (`0o600` `O_NOFOLLOW` `fsync`, `[BACKEND_GAP #1]`):** Before transmitting `POST /v1/commands`, `sourdough client-web` computes `payload_sha256`, mints `command_id = uuid4()`, atomically writes `{base_dir}/{company_id}/{principal_id_sha256}.json` with `{company_id, principal_id, command_id, command_type, contract_version: "paper-risk-command-v1", preconditions: {charter_revision: "c1", expected_revision: 2}, payload, payload_sha256, sent_ns}`, and sets `command_state = "submitting"`, `controls_locked = true`. `CommandOutboxPanel` shows `ReceiptRecord.state = "pending"`, live `pending_age`, and **omits `Check status` (`onLookup`)**.
3. **Disconnect / Timeout (`submitting -> unknown`):** Daemon socket closes or times out. `command_state` becomes `"unknown"`, `controls_locked = true`, pending file is retained, and `CommandOutboxPanel` renders `ReceiptRecord.state = "unknown"` with **`Check status` (`onLookup`) enabled solely in this state**.
4. **Reconnect / Restart Receipt Resolution (`GET /v1/commands/{command_id}?operation=...`, `[LIVE_UDS]` + `[BACKEND_GAP #2]`):** Without minting a second `command_id`, host queries the original `command_id`:
   - **Branch A (`200` `applied`):** Delete pending file (`fsync(dir_fd)`), transition `unknown -> applied` (`confirmed`), refresh projection (`effective_revision = 3`), recompute `controls_locked = false`.
   - **Branch B (`200` `denied` + `denial_code = "conflicting_revision"`):** Delete pending file, transition `unknown -> denied` (`failed`), display calm explanation requiring fresh review.
   - **Branch C (`200` `accepted`):** Retain pending file, transition `unknown -> accepted` (`pending`), keep `controls_locked = true`.
   - **Branch D (`404` `not_found`):** Retain pending file, transition `unknown -> not_found_retry_choice` (`unknown`, `onLookup` **omitted**), keep `controls_locked = true`, and render **`Retry same request`** (resubmitting exact persisted `command_id`, `command_type`, `contract_version`, `preconditions`, and `payload` without reading newer live revisions) or **`Discard draft`** (deleting pending file -> `"idle"`).
   - **Branch E (`403` on startup recovery of stale/revoked-principal pending file, `[BACKEND_GAP #1]`):** Quarantine/purge unowned pending file with an explicit security notice so the active principal is never deadlocked.

- **Verifiable Assertions:** Zero duplicate `command_id`s minted across timeout/restart; `Check status` absent except when `command_state === "unknown"`; `403` stale pending file quarantined cleanly without blocking a second principal.

---

## Scenario 8: Transient Disconnect vs. `401`/`403`/Principal/Scope Switch vs. Host/Session Lock

- **Flows covered:** Flow 11, Flow 01, Flow 05
- **Actors & Preconditions:** Connected `board_control` or `board_viewer` principal viewing `/` and `/performance` projections (`as_of_ns = T0`, `event_seq = S0`).

### Three strictly disjoint state transitions

1. **Transient Disconnect / Daemon Restart (`ECONNREFUSED` / EOF / timeout / `503`):**
   - State: `link_state = "disconnected"`, `stale = true`, `controls_locked = true`.
   - UI: Last verified `Data State` (`as_of_ns = T0`, `event_seq = S0`) **stays visible** beneath a consolidated sub-header `Notice` (`"Disconnected · Showing last verified snapshot · Controls locked"` + elapsed duration); Header Slot 3 status trigger reflects `"Reconnecting"`. Zero `clearData()`. Reconnect runs `GET /v1/meta` + conditional `If-None-Match` `GET /v1/projections/*`, clearing `stale = false` and unlocking controls (unless an unresolved command exists).
2. **Authorization Loss (`401`/`403`) or Company / `store_key` / Socket / Principal Switch:**
   - State: **Immediate zero-grace `clearData()`** (`available = false`, projection arenas wiped, in-flight requests cancelled, `clearInMemoryCommandState()` called while retaining valid on-disk `(company_id, principal_id)` `board-pending`, `controls_locked = true`).
   - UI: Zero cached company numbers, duties, CEO messages, or decisions remain in the DOM; displays `"Access denied"` or `"Switching scope"` empty state.
3. **Host / Screen / Session Lock (`host_locked = true`):**
   - State: UI privacy state `host_locked = true`. **Does NOT mutate `data_as_of_ns` (`T0`), `stale`, or `LinkState`.**
   - UI: Mounts opaque full-viewport Privacy Shield (`data-agent-hidden`) over all protected financial, decision, office, CEO thread, and outbox DOM. Unlocking requires OS/session verification plus a fresh `GET /v1/meta` and projection re-verification before lifting the shield.

- **Verifiable Assertions:** Transient disconnect never wipes verified data; `401`/`403`/principal switch wipes protected DOM in zero ticks; host lock conceals DOM without rewriting `as_of_ns = T0` to stale.

---

## Scenario 9: Modern Studio Floor Map (`/office`), Workstation Drill-Down, `.desk/drafts/*` Opacity & CEO-Delegated Offboarding Custody

- **Flows covered:** Flow 02, Flow 06, Flow 10
- **Actors & Preconditions:** Multiple hired Agents operating inside `CompanyRuntime` (`[V1_SQLITE_NEEDS_UDS]` `agent_desk.py`, `company_assets.py`, `conversations.py`, `conversation_delivery.py`, `work_handoff.py`, `work_peer_response.py`, `retirement.py`, `runtime/work_kernel.py` + `[BACKEND_GAP #6, #8]`).

### Step-by-step observability & custody transitions

1. **Day 0 -> Growth on `/office` (`[BACKEND_GAP #6]`):** On Day 0 (Founder/CEO only), `CEO Office` renders active while `Research & Strategy`, `Trading & Execution`, and `Risk & Operations` render calm dashed architectural bays (`"Ask CEO about first hire"` / `"View Mandate"`). As Agents are hired, the 4 fixed presentation zones scale density cleanly (`1–4` Expanded Meso -> `5–9` Compact 2-line w/ exceptions pinned -> `10+` Zone Health Bar + pinned exceptions + `+N nominal` chips, `>= 12px` font).
2. **Calm Ledger-First Collaboration & Private Desk Opacity:** Active `work_peer_requests` (`state === "open"`), pending `work_handoff` offers (`"Pending offer — A retains duty"`), and `.desk/drafts/*` counts (`"Private Desk · 3 drafts (not shared)"`) render via static status pills and hover/focus cross-highlighting (`prefers-reduced-motion: reduce` safe). Draft filenames and bodies never leave disk (`agent_desk.py`).
3. **2D `role="grid"` Keyboard Drill-Down to `/office/agents/:agentId` (Zero Drawers):** Navigating via `Arrow` keys and pressing `Enter` transitions the main view (zero `Y.Sheet`) to `/office/agents/:agentId` with `<nav aria-label="Breadcrumb">` (`Office Floor > Zone > Agent`) and 3 internal tabs:
   - **`Work Run Logs`**: `work_runs`, `fencing_token`, `.sourdough-traces/` metadata, `.desk/logs/`.
   - **`Inter-Agent Discussions (Read-Only)`**: `project_message` (`<= 1600B`) badged **`"Discussion only · Does not assign or accept work"`**, `project_delivery` receipts, `work_peer_requests`, and `work_handoff` (**zero Board non-CEO chat composer**). Restricted `shared_publication` links render `"Unavailable"` when audience access is absent.
   - **`Published Reports`**: `shared_publication` excerpts (`[start:end]`, `excerpt_sha256`, `audience`), cross-linking to `/company/org/publications/:assetId` and `/orders/:orderId`.
   - Pressing Back/`Escape` restores exact focus to the originating `role="gridcell"`.
4. **CEO-Executed Retirement & Read-Only Board Custody Audit (`/company/org`, `[V1_SQLITE_NEEDS_UDS]` + `[BACKEND_GAP #8]`):**
   - Authorized CEO/manager executes `retirement.py:retire_agent` (`TERMINATION_SCOPE = "retire-agent"`, blocking `founder_id` / `manager_id`) inside `CompanyRuntime` — **zero Board `retire_employee` button or command exists**.
   - `/company/org` renders banner **`"Managed by CEO · Board view is read-only"`** above the `agent_archive` & unsettled work custody ledger (`'blocked'` + `work_custody_transfer` `custody_status = "awaiting_reassignment"` in `[BACKEND_GAP #8]` vs `'cancelled'` in `origin/main` `work_kernel.py:390-395`), preserving historical author attribution and published reports.

- **Verifiable Assertions:** Private `.desk/drafts/` bodies never appear in UI projections; `project_message` never creates a `work_items` row; zero non-CEO Board chat composer exists; `/company/org` offboarding ledger is strictly read-only.

---

## Scenario 10: Quiet `SYSTEM` Diagnostics Navigation & Core-Only Graceful Degradation

- **Flows covered:** Flow 01, Flow 02, Flow 11, Flow 12, Flow 13
- **Actors & Preconditions:** Core-only company (`active_domains = ["core"]`, no `paper` domain) and/or collapsed sidebar desktop layout.

### Step-by-step state transitions

1. **Core-Only Graceful Fallback (`/`):** On a company without `paper`, `/` renders `"Paper profile not enabled on this company"` (zero `$0.00` or `0.00%` placeholders) alongside the Company summary, `Since Last Visit` digest (`?since_seq=`), and direct navigation to `/office`, `/ceo`, and `/company/approvals`. Header Slot 4 presents `Ask CEO` instead of `Risk Gate`.
2. **Quiet `SYSTEM` Reachability in All Viewport Modes:** Whether the sidebar is expanded (showing the quiet bottom `SYSTEM` footer) or collapsed (`SidebarTrigger`), clicking Header Slot 3's **Connection & Freshness Status Trigger** opens the on-demand `Y.Popover` surfacing Tier 2 technical diagnostics (`store_key`, `service_incarnation_id`, `as_of_ns`, `revision`, `event_seq`, `principal_id`, `role`, `Lock Screen` button) and the **`"System Diagnostics"`** link navigating cleanly to `/system/daemon`, `/system/checkpoints`, `/system/tools`, `/system/evaluations`, and `/system/audit`.

- **Verifiable Assertions:** Core-only companies operate without paper tables or `$0.00` fabrications; `SYSTEM` views remain reachable via both sidebar footer and Header Slot 3 popover without adding a fifth header slot.
