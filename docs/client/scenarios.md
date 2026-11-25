# Board Client usage scenarios and state transitions

Design draft · [Board Client](README.md) · [Client architecture](architecture.md) · [Client screens and navigation](inspect/screens/spec.md) · [Flow observability](inspect/flow-observability/spec.md) · [Command submission](commands/submit/spec.md)

This walkthrough specifies nine concrete end-to-end operational scenarios covering normal operation, transient disconnect, contention failure, security wipe, privacy lock, and crash recovery. Every step distinguishes **`[Live origin/main]`** (implemented in the `origin/main` backend/schema) from **`[M3 Extension]`** (UDS projection, RBAC, and schema extensions specified for the full control plane). Cross-check against [Acceptance episodes](../acceptance/episodes.md) and [Hired-Agent paper walkthrough](../acceptance/hired-agent-continuity/paper-walkthrough.md).

---

## Scenario 1: Company Founding (`bootstrap_locked` Owner Registration) & Charter Amendment (`expected_revision`)

- **Flows covered:** Flow 01, Flow 11
- **Actors & Preconditions:** Local company store initialized via `sourdough found` (`[Live origin/main]` `company(singleton=1, schema_revision=1, charter_revision="c0")`, `founding_receipt`, `company.founded` audit event) with zero `board_binding` rows (`[M3 Extension]`).

### Step-by-step state transitions

1. **Discovery (`[M3 Extension]`):** Client opens on `/overview` -> `GET /v1/meta` returns `{company_id, store_key, schema_version: 1, binding_state: "bootstrap_locked"}`. Router isolates the UI on `/overview/bootstrap`, hiding operational company views and locking all non-bootstrap commands (`controls_locked = true` for everything except `register_owner`).
2. **Challenge & Confirmation (`[M3 Extension]`):** Operator clicks `"Register Owner Binding"`. Host/daemon issues `OwnerChallenge(purpose="bootstrap", service_incarnation_id, nonce_digest)`. UI mounts `ControlledConfirmDialog` (`SDKButton`, rejecting `details?.source === "agent"`, `data-agent-confirm="Register owner binding"`) showing `company_id`, `store_key`, `charter_revision`, `principal_ref`, and `initial_scope_ref` (`OwnerProof` secrets stay outside DOM and logs).
3. **Commit (`POST /v1/commands` `register_owner`, `[M3 Extension]`):** On human confirm, dialog closes immediately (`onOpenChange(false)`) -> bridge persists `0o600` `O_NOFOLLOW` `LocalCommandIntent` scoped to `(company_id, principal_id)` -> submits `POST /v1/commands` with `Idempotency-Key = command_id`. Daemon atomically consumes `OwnerChallenge`, inserts `board_binding` (`binding_revision = 1`) + `role_assignment` (`board_control`) + `command_receipt(applied)` + `audit_event`, clears `bootstrap_locked`, and notifies `_event_cond`. UI removes pending file, transitions `CommandLifecycle` to `applied`, refreshes `GET /v1/meta`, and unlocks `/overview`.
4. **Charter Amendment (`amend_charter`, `[M3 Extension]`):** On `/governance/charter`, operator views `charter_revision = "c1"` (`expected_mandate_revision = 1`), lowers the document review cost ceiling, and enters a reason (`1..2000` chars). `ControlledConfirmDialog` latches composite `decision.revision = "c1:1"`.
5. **CAS Contention & Failure Path (`conflicting_revision`):** Two operators (or a stale tab) submit `amend_charter` with `expected_mandate_revision = 1`.
   - First request commits `mandate_revision = 2`, returning `command_receipt(state="applied")`.
   - Second request hits CAS mismatch: daemon persists `command_receipt(state="denied", denial_code="conflicting_revision")` and returns HTTP `200` `{state: "denied", denial_code: "conflicting_revision", effective_revision: 2}`.
   - UI deletes the pending file, transitions `CommandLifecycle: submitting -> denied` (`ReceiptRecord.state = "failed"` in `CommandOutboxPanel`), refreshes `/governance/charter` to `mandate_revision = 2`, and requires a brand-new human decision against revision `2` (never auto-rebasing).

- **Verifiable Assertions:** Two concurrent bootstrap attempts yield at most one `board_binding` (`binding_revision = 1`); `OwnerProof` never appears in DOM/storage; stale `amend_charter` records `denial_code = "conflicting_revision"` with zero silent mutation.

---

## Scenario 2: Hiring Approval (`DecisionQuestion` / `StaffingChoice`), Onboarding Readiness & Grant Revocation

- **Flows covered:** Flow 06, Flow 10
- **Actors & Preconditions:** `CompanyRuntime` / hiring manager (`[Live origin/main]` `staffing.py`) has staged `staffing_authority(slots, turns, allowed_scope, execution_profile)` and `staffing_need`, or escalated a reserved `DecisionQuestion(kind="owner_authority", question_id, revision, payload_hash, respond_by)` (`[M3 Extension]`).

### Step-by-step state transitions

1. **Decision Inbox Display (`/governance/decisions`, `[M3 Extension]` over `[Live origin/main]` `staffing.py`):** Client renders `question_id`, `revision`, `payload_hash`, `respond_by`, `allowed_scope`, `slots`, `turns`, `execution_profile`, and `alternatives` (`hire | reassign | defer`).
2. **Confirm & Commit (`POST /v1/commands` `submit_decision_answer`, `[M3 Extension]`):** Authenticated `board_control` principal selects `"hire"` (`turns <= remaining_turns`) and confirms inside `ControlledConfirmDialog` latching `${question_id}:${revision}:${payload_hash}` (`data-agent-confirm="Submit hiring decision"`). Host persists `(company_id, principal_id)` `LocalCommandIntent` (`command_id = answer_id`) and submits. Daemon validates `open` question revision/deadline and atomically writes `AcceptedAnswer`, `AnswerApplication`, `staffing_choice`, `hire_receipt` (`agent`, `standing_grant`), and `command_receipt(applied)`.
3. **Separate Application vs Onboarding Readiness (`/organization/staffing`, `[Live origin/main]` `onboarding.py`):** UI renders three independent statuses side-by-side (never collapsed into one `"Hired"` badge):
   - (a) **Answer Application:** `applied`.
   - (b) **Onboarding (`hire_onboarding.state`):** transitions `provisioning -> blocked` (if `runtime_unavailable` / `workspace_unavailable` / `project_access_unavailable`, showing `attempts/3` and `retry_at` backoff) -> `ready` (`private_desk` + `first_duty_id` provisioned) -> `accepted` (`onboarding_acceptance` recorded after hired Agent reads and accepts first duty).
   - (c) **Project Enrollment:** `enroll_project_participant` confirmation.
4. **Grant Revocation (`revoke_grant`, `[M3 Extension]`):** On `/organization/roster`, operator clicks `"Revoke Standing Grant"` for `agent_id` (`expected_authority_epoch = E`). Confirming submits `revoke_grant`. Daemon atomically sets `standing_grant.scope = 'revoked'`, increments `authority_epoch = E + 1`, revokes `work_responsibilities`, cancels armed `work_conditions`, and fences active `work_runs` (`has_active_grant == False` in `[Live origin/main]` `work_kernel.py`). Any delayed request citing `expected_authority_epoch = E` fails closed with `denial_code = "conflicting_revision"`.

- **Verifiable Assertions:** `hire_onboarding.state` remains distinct from `AnswerApplication`; blocked onboarding displays exact reason & `attempts/3`; revoked grant immediately fences active runs (`work.run_fenced`) and rejects stale epoch retries.

---

## Scenario 3: Emergency Stop / Paper Risk Suspension, Competing Stops (`P1`/`P2`) & Corroborated Resume (`QUOTE_MAX_AGE_NS = 15s`)

- **Flows covered:** Flow 11, Flow 03, Flow 05
- **Actors & Preconditions:** Authenticated `board_control` principal; active paper binding (`environment == "paper"`, `nautilus-spot-sandbox-v1`); at least one open `paper_order` in flight (`reserved_minor_units > 0`) alongside a running research duty.

### Step-by-step state transitions

1. **Suspend New Paper Risk (`P1`, `[Live origin/main]` UDS + `[M3 Extension]` `board_binding`):** On `/paper/risk-controls`, operator clicks `"Suspend new paper risk"` -> `ControlledConfirmDialog` (`data-agent-confirm="Suspend new paper risk orders"`, latching `charter_revision` and `expected_revision = 0`, requiring `reason`). Host writes `(company_id, principal_id)` `LocalCommandIntent`, sends `POST /v1/commands` (`command_type = "suspend_new_risk"`, `contract_version = "paper-risk-command-v1"`).
2. **Partial Scope Verification (`[Live origin/main]` `paper_risk_state` & `paper_order.py`):** `paper_risk_state` transitions to `(suspended = 1, effective_revision = 1)`. UI renders active suspension in Global Status Bar and `/paper/risk-controls`. New `submit_paper_order` attempts are denied (`"New paper risk is suspended"`), **while** existing open order fills (`post_confirmed_fill`), `reconcile_paper_orders`, and non-order research duties continue visibly without interruption.
3. **Competing Stop (`P2`) Race (`[Live origin/main]` vs `[M3 Extension]` `RestrictionView`):** While operator prepares resume `R1` referencing `P1` (`expected_revision = 1` / `restriction_id = "rest-p1"`), a second stop `P2` lands (`effective_revision` becomes `2` in `origin/main`; distinct `restriction_id = "rest-p2"` row created in `[M3 Extension]` `RestrictionView`).
4. **Stale / Partial Resume (`R1`) Fails Closed:**
   - **`[Live origin/main]`:** Submitting `R1` with `expected_revision = 1` commits `command_receipt(state="denied", denial_code="conflicting_revision")` and returns HTTP `200` `{state: "denied", denial_code: "conflicting_revision", effective_revision: 2}`. `CommandOutboxPanel` displays `failed` (`conflicting_revision`), `paper_risk_state.suspended` remains `1`, and operator must review revision `2` fresh.
   - **`[M3 Extension]` `RestrictionView`:** Submitting `R1` targeting `{restriction_id: "rest-p1", expected_restriction_revision: 1, charter_revision}` clears `rest-p1` only; **`rest-p2` remains active** in `/overview/restrictions` and the Global Status Bar pill, keeping paper risk blocked until `R2` explicitly targets `rest-p2`.
5. **Corroborated Resume (`R2`) & Server-Side `15s` `QUOTE_MAX_AGE_NS` Cut (`[Live origin/main]` `paper_resume.py`):**
   - Operator opens `"Resume paper risk"`. UI displays **non-binding** readiness telemetry (`[M3 Extension]`: `settled_company_ok`, `unresolved_orders_count = 0`, `pending_binding_change = false`, `last_quote_age_ns`), **never** a pre-fetched 15-second modal countdown timer.
   - Confirm submits `POST /v1/commands` (`resume_new_risk`). Daemon executes Phase 1 (`_settled_company` + `venue.read_current_risk_cut()`, checking `0 <= now_ns - event_ns <= 15s`, `0 <= now_ns - exchange_trade_ns <= 15s`, `expires_ns = min(event_ns, exchange_trade_ns) + 15_000_000_000`) -> Phase 2 (`BEGIN IMMEDIATE`, re-verifying `_settled_company` and `cut.observed_ns <= time_ns() <= cut.expires_ns`) -> commits `suspended = 0`, `effective_revision + 1`.
6. **Failure Branch (15s Expiry or Unsettled Order):** If >15s elapses between Phase 1 and Phase 2 or an unresolved order/missing fee exists, daemon returns HTTP `400` (`"Current paper evidence expired before commit"` / `"Company has unresolved original paper obligations"`). `paper_risk_state.suspended` stays `1`; UI surfaces the exact blocker in `CommandOutboxPanel` / `Notice` and allows fresh confirmation.
7. **Distinct Scoped Emergency Controls (`[M3 Extension]`):** `pause_company`, `interrupt_run` (`expected_execution_epoch`), `cancel_work` (`expected_work_version`), and `revoke_grant` (`expected_authority_epoch`) remain separate scoped commands under `/overview/restrictions`, `/work/runs`, `/work/timeline`, and `/organization/roster`.

- **Verifiable Assertions:** `R1` never clears `P2`; server-side `15s` expiration fails closed with `suspended = 1`; existing fills/reconciliation continue while `suspended = 1`.

---

## Scenario 4: WAL-Normalized Checkpoints (`sourdough-checkpoint-v2`, `WriterLease`) & Exact-Cut Paper Restore Quarantine

- **Flows covered:** Flow 11, Flow 05, Flow 03
- **Actors & Preconditions:** Active `nautilus-spot-sandbox-v1` paper company (`schema_version = 1`) with an admitted `paper_order` at checkpoint cut; after checkpoint, the venue settles the fill and balance while the company store lags or faults.

### Step-by-step state transitions

1. **Cut Checkpoint (`/operations/checkpoints`, `[Live origin/main]` `storage/checkpoint.py` + `[M3 Extension]` `cut_checkpoint`):**
   - If another writer holds `WriterLease`, `hold_company_writer` fails with `WriterRefused` -> UI displays `503` writer-lease contention notice without mutating the store.
   - Under `WriterLease`, backend stages & hashes `desk/` (`desk_digest`), appends `company.checkpoint_cut` to `audit_event`, normalizes SQLite via `sqlite3_backup` to `PRAGMA journal_mode = DELETE`, verifies `PRAGMA quick_check` and `PRAGMA foreign_key_check` with zero `-wal`/`-shm` sidecars, computes `LogicalWatermark(company_id, schema_version=1, max_event_seq, domain_digest)`, and writes `0o600` `sourdough-checkpoint-v2` zip (`manifest.json`, `company.sqlite`, `desk/*`). UI displays `cut_id`, `max_event_seq`, `sha256`, `desk_digest`, and `captured_ns`.
2. **Restore Pre-Flight & `paper_sender_lease` Fencing (`/operations/restore`, `[Live origin/main]` `paper_restore.py` + `[M3 Extension]` `restore_checkpoint`):**
   - Operator selects checkpoint archive, inspects `inspect_checkpoint` manifest, and confirms `restore_checkpoint` via `ControlledConfirmDialog`.
   - Backend acquires `hold_sender_lease(venue.journal)` (`0o600` `.writer-lock`, refusing with `CheckpointRefused` if an active worker sender holds the lock), verifies `PRAGMA user_version == 1`, zero stray WAL sidecars, `connector_revision == "nautilus-spot-sandbox-v1"`, and matching `destination_id`.
3. **Exact-Cut Settlement & Store Retirement (`_settle_candidate`, `company_store_retired`, `[Live origin/main]`):**
   - Without sending any order to the venue, restore reconciles shadow orders against `venue.read_restore_cut(account_id)` (`order_ids`, `fills`, `paper_fill_fee`, `position_lots == cut.balance.position_lots`).
   - Backend inserts `company_restore_receipt` in `DELETE` mode, atomically retires original store (`INSERT INTO company_store_retired`, `UPDATE company SET schema_revision = -1`), and swaps in replacement DB + Desk.
   - **Crash / Mid-Publish Recovery (`_resume_retired_restore`):** If interrupted after `company_store_retired` commits, the retired store refuses writes (`_require_writable_store`) and retrying the exact restore finishes publication read-only without reactivating the retired store.
4. **Post-Restore `restore quarantine` 5-Dimension Gate (`[M3 Extension]`):**
   - Replacement `CompanyDaemon` boots with `RestrictionView` cause `cause_category = "restore_quarantine"` (`0` new paper orders allowed).
   - `/operations/restore` renders the **5-dimension `RecoveryReceipt` Readiness Panel**: (1) Data cut (`checkpoint_sha256`, `LogicalWatermark`), (2) Sole writer (`WriterLease` + `company_store_retired`), (3) Current authority (`board_binding` / `standing_grant` verified), (4) External reconciliation (`paper_sender_lease` + zero unresolved fills/fees), (5) Eligible next actions (`resume_company` / `resume_new_risk` disabled until dimensions 1–4 are `VERIFIED`).

- **Verifiable Assertions:** Restoring while sender lease is held fails closed; post-restore UI displays booked post-checkpoint fill with zero duplicate orders; new paper risk stays locked until all 5 `RecoveryReceipt` dimensions verify.

---

## Scenario 5: Strategy Promotion & Forward Rollback (Flow 09), Review Tool `pass^3` & Lineage Quarantine (Flow 12), Offline Evaluations (Flow 13)

- **Flows covered:** Flow 09, Flow 12, Flow 13
- **Actors & Preconditions:** `board_control` principal inspecting candidate upgrades and evaluations across Paper Fund, Operations, and Offline Evaluations.

### Step-by-step state transitions

1. **Part A — Flow 09 Strategy Promotion & Forward Rollback (`/paper/strategy-trials`, `[Live origin/main]` `strategy_comparison.py` + `[M3 Extension]` UDS):**
   - Operator inspects `StrategyRelease` exact 15 fields (`release_id`, `company_id`, `owner_agent_id`, `parent_release_id`, `strategy_source_sha256`, `parameters_json`, `nautilus_lock_sha256`, `instrument_rules_sha256`, `risk_config_sha256`, `fill_policy_sha256`, `pit_manifest_id`, `pit_manifest_sha256`, `research_trial_count_n`, `bundle_sha256`, `recorded_at_ns`), anti-lookahead `PitCatalogManifest` (`valid_through_ns <= decision_cut_ns`, `available_at_ns <= decision_cut_ns`), and paired `ChallengerTrial` (`status = 'completed'`, `verdict` in `PROMOTE | REJECT | INCONCLUSIVE`, `alpha_effective`, `lower_bound_bps`, `upper_bound_bps`, shared cost split).
   - **Non-Auto-Activation & Non-Self-Activation:** `PROMOTE` never activates automatically. `INCONCLUSIVE` / `REJECT` (including fast operational failures like `risk-invariant-breach`, `drawdown-limit-breach`) disable activation with plain reason chips. If `principal === release.owner_agent_id`, `Activate` is disabled/rejected (`"Strategy author grant cannot self-activate"`).
   - **Activation & Forward Rollback:** Authorized deployer confirms `activate_strategy_release` (`expected_active_deployment_id`) -> inserts `StrategyDeployment(status='active', program_identity='prog-...')`. On post-activation degradation, operator opens `ControlledConfirmDialog` for `rollback_strategy_deployment` targeting a previously `'superseded' | 'rolled_back'` release (never an untested candidate). Daemon marks current deployment `'rolled_back'` with `retired_at_ns` and inserts a NEW `'active'` `StrategyDeployment` row with a fresh `deployment_id` and `program_identity`, **preserving all existing open orders, fills, and costs**.
2. **Part B — Flow 12 Review Tool `pass^3` & `parent_asset_id` DAG Quarantine (`/operations/review-tools`, `[Live origin/main]` `evidence_review.py`/`review_tool_activation.py` + `[M3 Extension]` UDS):**
   - UI renders `ThreeStageVerdict` (`stage1_structure_and_negative`, `stage2_harmlessness`, `stage3_pass_k_stability` with `PASS_K_TRIALS >= 3`) and verifies `authors.isdisjoint(actors)` (`ops.review_self_evaluation_blocked` prevents self-evaluation).
   - Before confirming `review_tool.quarantine` on faulted asset `A`, `ControlledConfirmDialog` embeds an `@ui-sdk/react/tree` blast-radius preview listing all transitive descendants `{A, B, C}` via `parent_asset_id` plus the automatic fallback target (newest unquarantined ancestor with valid `EVALUATOR_REVISION`). Confirming marks `{A, B, C}` `quarantined = 1` and activates the fallback revision. Separate `/operations/maintenance` view tracks `local incident != external issue != published release != regression retest`.
3. **Part C — Flow 13 Offline Blind Evaluations (`/evaluations/*`, `[Live origin/main]` `trial_registry.py` + `[M3 Extension]` UDS & schemas):**
   - Physically separate workspace over `offline-trial-registry-v1` displays `Outcome` (`completed | no-decision | failed | timed-out`) vs `Transcript` digests, `BlindedTrial` (`arm`, `slot_id`, `run_id` stripped), `(AB, BA)` position-swapped outcomes (`consistent` vs `tie_noise`), and `RsiTransferSummary` (`SUPPORTED | UNSUPPORTED | INCONCLUSIVE`, forcing `UNSUPPORTED` on hard authority/service invariant failures before invalidation masking).
   - `[M3 Extension]` displays `AccessRecord` sealed holdout exposure locks (confirming renaming or rolling back a method never un-exposes previously seen holdouts) and `parent_method_ids` method DAG rollback lineage.

- **Verifiable Assertions:** Self-activation and self-evaluation fail closed; forward strategy rollback preserves open orders/positions; Flow 12 DAG quarantine cascades to all descendants; Flow 13 `AccessRecord` locks persist across rollback.

---

## Scenario 6: Paper-Fund Intent-to-Outcome, S04 Partial Fill + Remainder Cancel (`cancel != flat`) & Two-Layer P&L `Unavailable`

- **Flows covered:** Flow 03, Flow 04, Flow 07, Flow 08, Flow 11
- **Actors & Preconditions:** Active `nautilus-spot-sandbox-v1` paper mandate (`environment == "paper"`).

### Step-by-step state transitions

1. **Intent-to-Outcome Lineage (`/paper/decision-trace/:orderId`, `[Live origin/main]` + `[M3 Extension]`):** Operator clicks an order/position and traces `duty -> duty_run -> paper_research_decision / paper_decision -> paper_order (quantity = 20 lots / 0.002 BTC, reserved_minor_units) -> paper_fill -> paper_fill_fee -> paper_position_observation -> paper_cancellation`.
2. **S04 Partial Fill + Remainder Cancel (`0.002 - 0.0008 - 0.0002 - 0.001 = 0.001 BTC`, `[Live origin/main]` `work_projection.py:218-232` & `paper_books.py`):**
   - Entry order quantity: `20` lots (`0.002 BTC`).
   - Partial fill 1 books `8` lots (`0.0008 BTC`); partial fill 2 books `2` lots (`0.0002 BTC`) -> total filled `10` lots (`0.001 BTC`) with recorded `paper_fill_fee`.
   - Remainder `10` lots (`0.001 BTC`) cancels: `VenueCancellation(filled_quantity=10, outcome="cancelled")` -> `paper_cancellation(filled_quantity=10, released=1, outcome='cancelled')`, releasing only the unfilled `10`-lot reservation (`open_reservation_minor_units = 0`).
   - **Observed UI State on `/paper/orders`:** Order row renders `ordered_quantity: 0.002 BTC`, `filled_quantity: 0.001 BTC` (`0.0008 + 0.0002`), `remaining_quantity (cancelled): 0.001 BTC`, `open_reservation_minor_units: 0`, `order.status: "cancelled"`, **AND** Fund Position Summary highlights **`booked_position: 0.001 BTC` (ACTIVE OPEN EXPOSURE — `cancel != flat`)**.
3. **Two-Layer P&L & Missing Coverage Honesty (`/paper/performance`, `[Live origin/main]` `paper_window_statement.py`):**
   - Period `PaperStatement` has complete Trading BOR (`trading.missing = ()`, `trading.pnl_minor != null`, rendered in a percentage-return / cumulative-P&L-from-zero `Chart`) while `accounting_close` has an open `CloseIssue("missing-cost-receipt")` (`WindowCosts.missing` non-empty, `operating_cost_atoms = null`, `company.pnl_minor = null`, `company.time_weighted_return = null`).
   - **Observed UI State:** Trading BOR displays exact numeric return and P&L + `as_of` + `window_revision`, while Operating Costs and Company Net display **`"Unavailable"`** in `RecordFacts` and **`"No reading"`** in `DataTable` alongside `"missing-cost-receipt"` — **never `$0.00` or `0.00%`**.

- **Verifiable Assertions:** `0.002 - 0.0008 - 0.0002 - 0.001 = 0.001 BTC` active position remains visible despite `order.status === "cancelled"`; missing accounting close renders `"Unavailable"` / `"No reading"` rather than `0`.

---

## Scenario 7: Disconnect During `POST /v1/commands` & `(company_id, principal_id)` `board-pending` Recovery

- **Flows covered:** Flow 11, Flow 05
- **Actors & Preconditions:** Authenticated `board_control` principal connected (`link_state = "connected"`, `!controls_locked`), viewing `effective_revision = 2`, `charter_revision = "c1"`.

### Step-by-step state transitions

1. **Draft & Confirm (`idle -> draft -> submitting`):** Operator confirms `"Suspend new paper risk"` in `ControlledConfirmDialog` (`details?.source !== "agent"`, `decision.revision = "c1:2"`). Modal closes immediately (`onOpenChange(false)`) handing off to `CommandOutboxPanel`.
2. **Write-Ahead Persist (`0o600` `O_NOFOLLOW` `fsync`):** Before transmitting `POST /v1/commands`, host/bridge computes `payload_sha256`, mints `command_id = uuid4()`, atomically writes `{base_dir}/{company_id}/{principal_id_sha256}.json` (`[M3 Extension]` fixing company-only pending path) with `{command_id, command_type, payload, payload_sha256, expected_revision: 2, sent_ns}`, and sets `command_state = "submitting"`, `controls_locked = true`. `CommandOutboxPanel` shows `ReceiptRecord.state = "pending"`, live `pending_age`, and **omits `onLookup`**.
3. **Disconnect / Timeout (`submitting -> unknown`):** Daemon socket closes or request times out after transmission. `command_state` becomes `"unknown"`, `controls_locked = true`, on-disk pending file is retained, and `CommandOutboxPanel` renders `ReceiptRecord.state = "unknown"` with `onLookup` (`"Check original request"`) **enabled solely in this state**.
4. **Reconnect / Restart Receipt Resolution (`GET /v1/commands/{command_id}?operation=...`):** Without minting a second `command_id`, host automatically queries the original `command_id`:
   - **Branch A (`200` `applied`):** Delete pending file (`fsync(dir_fd)`), transition `unknown -> applied` (`confirmed`), refresh projection (`effective_revision = 3`), recompute `controls_locked = false`.
   - **Branch B (`200` `denied` + `denial_code = "conflicting_revision"`):** Delete pending file, transition `unknown -> denied` (`failed`), display `denial_code: conflicting_revision` + notice requiring fresh review.
   - **Branch C (`200` `accepted`):** Retain pending file, transition `unknown -> accepted` (`pending`), keep `controls_locked = true`.
   - **Branch D (`404` `not_found`):** Retain pending file, transition `unknown -> not_found_retry_choice` (`unknown`, `onLookup` **omitted**), keep `controls_locked = true`, and render `ButtonGroup`: **`Retry same ID`** (resubmits exact `command_id` + `payload` + `preconditions_json` -> `"submitting"`) or **`Discard draft`** (deletes pending file -> `"idle"`, unlocks controls).
   - **Branch E (`403` on startup recovery of stale/revoked-principal pending file, `[M3 Extension]`):** Quarantine/purge the unowned pending file with an explicit security notice so the active principal is never deadlocked.

- **Verifiable Assertions:** Zero duplicate `command_id`s minted across timeout/restart; `onLookup` absent except when `command_state === "unknown"`; `403` stale pending file quarantined cleanly without blocking a second principal.

---

## Scenario 8: Transient Disconnect vs. `401`/`403`/Principal/Scope Switch vs. Host/Session Lock

- **Flows covered:** Flow 11, Flow 01, Flow 05
- **Actors & Preconditions:** Connected `board_control` or `board_viewer` principal viewing live `/overview` and `/paper/performance` projections (`as_of_ns = T0`, `event_seq = S0`).

### Three strictly disjoint state transitions

1. **Transient Disconnect / Daemon Restart (`ECONNREFUSED` / EOF / timeout / `503`):**
   - State: `link_state = "disconnected"`, `stale = true`, `controls_locked = true`.
   - UI: Last verified `Data State` (`as_of_ns = T0`, `event_seq = S0`, work timeline, paper figures) **stays visible** beneath an urgent `Notice` (`"Disconnected · Showing last verified snapshot · Controls locked"` + disconnect duration). Zero `clearData()`. Reconnect runs `GET /v1/meta` + conditional `If-None-Match` `GET /v1/projections/*`, clearing `stale = false` and unlocking controls (unless an unresolved command exists).
2. **Authorization Loss (`401`/`403`) or Company / `store_key` / Socket / Principal Switch:**
   - State: **Immediate zero-grace `clearData()`** (`available = false`, projection arenas wiped, in-flight requests cancelled, `clearInMemoryCommandState()` called while retaining valid on-disk `(company_id, principal_id)` `board-pending`, `controls_locked = true`, `link_state = "incompatible"` or `"connecting"`).
   - UI: Zero cached company numbers, duties, or decisions remain in the DOM; displays `"Access denied"` or `"Switching scope"` empty state.
3. **Host / Screen / Session Lock (`host_locked = true`):**
   - State: UI privacy state `host_locked = true`. **Does NOT mutate `data_as_of_ns` (`T0`), `stale`, or `LinkState`.**
   - UI: Mounts opaque full-viewport Privacy Shield (`data-agent-hidden`) over all protected financial, decision, work, and outbox DOM, showing only neutral lock instructions. Unlocking requires OS/session verification plus a fresh `GET /v1/meta` and projection re-verification before lifting the shield.

- **Verifiable Assertions:** Transient disconnect never wipes verified data; `401`/`403`/principal switch wipes protected DOM in zero ticks; host lock conceals DOM without rewriting `as_of_ns = T0` to stale.

---

## Scenario 9: Internal Activity Observability (Private Desk, Publications, Discussions, Peer Requests, Handoffs & Retirement Custody)

- **Flows covered:** Flow 02, Flow 06, Flow 10
- **Actors & Preconditions:** Multiple hired Agents operating inside `CompanyRuntime` and `Two-Plane Agent Harness` (`[Live origin/main]` `agent_desk.py`, `company_assets.py`, `conversations.py`, `collaboration.py`, `work_handoff.py`, `retirement.py`, `work_kernel.py` + `[M3 Extension]` UDS projections & custody preservation).

### Step-by-step observability & custody transitions

1. **Private Desk Opacity (`/organization/roster` & `/work/runs/:runId`, `[Live origin/main]` `agent_desk.py`):** While Agent A iterates inside `/desks/A/.desk/drafts/`, UI exposes only `private_desk.manifest_revision`, `draft_count` (`<= 6`), and `.desk/logs/` offload metadata — **never** draft body text (`"Private Desk — unshared"`).
2. **Selective Publication (`/organization/publications`, `[Live origin/main]` `publish_selection`):** Agent A publishes character range `[start:end]` of `draft_name@draft_revision` to `project_id` + `audience`. `company.asset_published` streams via `/v1/events`. UI renders immutable `asset_id`, `excerpt_sha256`, and `audience`.
3. **Project Discussion vs Delivery (`/organization/discussions`, `[Live origin/main]` `conversations.py`):** Agent A sends `project_message` (`<= 1600B`, optionally citing `asset_id`) creating `project_delivery` rows. UI displays messages under **`"Discussion only · Does not assign or accept work"`**, separating `delivery_id` / `read_run_id` from `work_items`. If viewer loses `audience` access, cited `asset_id` renders `"Unavailable"`.
4. **Blocking Peer Request (`/organization/peer-requests`, `[Live origin/main]` `work_peer_requests`):** Agent A opens a blocking peer inquiry (`request_key`, `question`, `expected_decision`, `shared_publication:<asset_id>@<sha256>`, `deadline_ns`) to Agent B; A's `work_runs` yields to `waiting` (`wait_event_key = "peer_response:{request_id}"`) and B receives a `work.peer_response` item. UI traces `requester_work_item_id -> response_work_item_id -> state (open | answered | declined | expired) -> resumed_run_id`.
5. **Work Handoff (`/organization/handoffs`, `[Live origin/main]` `offer_handoff` / `accept_handoff`):** Agent A offers duty (`offer_id`, `asset_id`, `expires_at`). Prior to acceptance, UI renders `"Pending offer — A retains duty"`. Upon CAS `accept_handoff`, successor `duty` + `work_items` (`source_kind='handoff'`) activate for B, A's duty becomes `'handed_off'`, and `successor_work_item_id` + `accepted_run_id` link both records without erasing A's authorship.
6. **Retirement & Custody Handover (`/organization/offboarding`, `[Live origin/main]` vs `[M3 Extension]`):**
   - Operator confirms retirement (`retire_employee`, `[M3 Extension]`, against `employment_revision`; founder/acting manager blocked).
   - Backend inserts `agent_archive` (`cutoff`), revokes `standing_grant.scope = 'revoked'`, sets `duty.state = 'archived'`, fences running `work_runs`, revokes `work_responsibilities`, cancels armed `work_conditions`, and expires/declines open `work_peer_requests` (waking non-retired requesters).
   - **Custody boundary distinction:** In **`[Live origin/main]`** (`work_kernel.py:390-395`), `retire_agent_work` sets unsettled `work_items` to `'cancelled'`; in **`[M3 Extension]`**, unsettled `work_items` transition to **`'unassigned'` / `'blocked'`** (`custody_status = "awaiting_reassignment"`) and surface in `/organization/offboarding` so active obligations transfer cleanly rather than disappearing.

- **Verifiable Assertions:** Private `.desk/drafts/` bodies never appear in UI projections; `project_message` never creates a `work_items` row; handoff preserves original authorship; `[M3 Extension]` retirement preserves unsettled `work_items` for explicit custody reassignment.
