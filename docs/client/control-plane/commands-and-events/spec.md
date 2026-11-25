# Event stream, runtime wakeups, and typed commands

Design draft · Verification **UNRUN** · Responsible component: Control Plane Events & Commands (`src/sourdough/ipc/server.py`, `src/sourdough/runtime/`, domain command handlers)

[Feature intent](../intent.md) · [Bridge and auth](../bridge-and-auth/spec.md) · [Meta and read projections](../projections/spec.md) · [Command submission](../../commands/submit/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Define the `GET /v1/events` redacted NDJSON stream, `CompanyRuntime` post-commit wakeup wiring and `audit_event` emissions across all domain modules (`<= 1s` live observability), and the generalized `POST /v1/commands` and `GET /v1/commands/{command_id}?operation=...` contract registry covering all board governance, authority, work, paper-fund, and continuity commands.

Input contracts: [Transport, loopback bridge, and role authorization](../bridge-and-auth/spec.md) · [Command submission](../../commands/submit/spec.md) · [Versioned charter command](../../../company/systems/organization/found-company/amend/spec.md) · [Board control](../../../company/profiles/quant/trading/board-control/apply-control/spec.md). These are public service contracts, not imports of client UI packages.

## Design decision and review

### 1. `GET /v1/events` NDJSON stream (`[Live origin/main]`)

- **Route and bounds (`server.py:469-474,583-661`, `protocol.py:26`)**:
  - `GET /v1/events?after_seq=<int>&limit=<int>&wait_ms=<int>`
  - Query validation: `after_seq >= 0`, `1 <= limit <= MAX_EVENTS_BATCH (500)` (client default `100`), `0 <= wait_ms <= _MAX_WAIT_MS (5000)`. Concurrent waiters are capped at `_MAX_EVENT_WAITERS = 8` (excess waiters fail closed immediately with HTTP `503`).
  - Response headers: `Content-Type: application/x-ndjson`, `Connection: close`, `Cache-Control: no-store`, `X-Sourdough-Company-Id: <company_id>`.
- **Redacted 7-field NDJSON envelope**:
  - Each line reads `audit_event` rows (`WHERE company_id = ? AND event_seq > ? ORDER BY event_seq ASC LIMIT ?`) via `open_reader()` and serializes exactly 7 fields:
    `{event_seq, company_id, domain, entity_type, entity_id, event_type, recorded_at_ns}`.
  - `actor_id`, `run_id`, `command_id`, and `payload_json` from `audit_event` (`01_storage_kernel.sql:13-49`) are **redacted** from the stream so `/v1/events` serves solely as a typed invalidation cursor triggering scoped projection refreshes.
- **RBAC gate (`[M3 Extension]`)**: Requires authenticated active `board_binding` + `board_viewer` or `board_control` role (`bootstrap_locked` fails closed `403`).

### 2. Fixing `_event_cond` wakeups and silent domain modules (`[M3 Extension]`)

**Concern for the responsible component:** In `origin/main` (`server.py:161,284,646,841`), `CompanyDaemon._event_cond.notify_all()` fires only inside `_handle_submit_command()` and `stop()`, `CompanyRuntime` (`runtime/engine.py`) acquires `hold_company_writer` independently with zero notification hook, and 15 domain/runtime areas emit zero `audit_event` rows (13 `company.sqlite` domain modules plus 2 writerless/venue-isolated runtime/planner modules).

1. **In-daemon `CompanyRuntime` wakeup hook (`on_commit`)**:
   - Inject an optional post-commit callback (`on_commit: Callable[[], None]`) into `CompanyRuntime`, `run_bound_handler`, and every command handler dispatched by `CompanyDaemon`. Whenever `write_tx(...)` commits a mutation (including `reclaim_expired_runs`, `wake_waiting_runs`, `evaluate_conditions`, `_claim_next`, `yield_run_to_wait`, `complete_work_run`, `fail_work_run`, and domain command handlers), invoke `with self._event_cond: self._event_cond.notify_all()` immediately after SQLite commit.
   - When `CompanyDaemon` is active, host/orchestrate `CompanyRuntime` work loops under `CompanyDaemon`'s writer coordination (sharing `WriterLease` cleanly rather than contending across processes), while retaining the `0.25s` fallback poll slice in `_handle_events` so external-writer transitions still surface promptly.
2. **Existing `audit_event` emitters vs. missing wakeup (Flows 01, 08, 09, 11, 12)**:
   - Sixteen modules (fifteen domain modules plus `storage/schema.py`, which emits `domain="ops"`, `event_type="domain.activated"` during `enable_domain_v1` at `storage/schema.py:303`) already call `storage/kernel.py:append_audit_event` in `origin/main` (including `accounting_close.py`, `paper_funding.py`, `paper_statement_store.py`, and `strategy_comparison.py` in Flows 08–09, alongside `company.py`, `storage/checkpoint.py`, `runtime/work_kernel.py`, `company_assets.py`, `review_qualification_store.py`, `review_tool_activation.py`, `general_ledger.py`, `paper_risk.py`, `paper_fee_bridge.py`, `paper_valuation.py`, `paper_window.py`, and `storage/schema.py`). Their M3 requirement is wiring runtime/handler commits to `_event_cond.notify_all()` (plus adding deployment/command `audit_event`s where missing), NOT inventing duplicate audit tables.
3. **Adding `append_audit_event` inside `write_tx` across the 13 silent `company.sqlite` modules and 2 coordinator-owned runtime/planner areas**:
   - The 13 `company.sqlite` domain modules below currently perform zero `audit_event` writes in `origin/main` and MUST append their facts inside the same `BEGIN IMMEDIATE` `write_tx` via `storage/kernel.py:append_audit_event` (sole `audit_event` owner in `storage/ownership.py:43`).
   - By contrast, `runtime/venue_coordinator.py` (`BoundedVenueCoordinator.plan_segments()` is a stateless planner without a `WriterLease`, `company_id`, or SQLite connection) and `nautilus_runtime.py` (`prepare_paper` writes solely to the separate venue SQLite after `writer.verify()`, and `supervise_paper` releases `hold_company_writer` before `supervise_worker`, deliberately avoiding `company.sqlite` `write_tx`) MUST NOT open ad-hoc `company.sqlite` `write_tx` blocks without `WriterLease`; instead, their segment/runtime lifecycle `audit_event`s (`paper.venue_segment_planned`, `paper.venue_segment_rotated`, `paper.runtime_prepared`, `paper.runtime_bound`, `paper.runtime_switched`, `paper.runtime_supervised`) MUST be emitted by the owning `CompanyRuntime` / `paper_account` / `paper_window` `write_tx` coordinator (under `WriterLease`):

| Module (or owning coordinator) | `domain` | `entity_type` | Required `event_type` fact(s) |
| --- | --- | --- | --- |
| `paper_account.py` | `paper` | `paper_binding`, `paper_binding_change` | `paper.binding_created`, `paper.binding_change_requested`, `paper.binding_changed` |
| `paper_order.py` | `paper` | `paper_order` | `paper.order_admitted` |
| `paper_books.py` | `paper` | `paper_fill`, `paper_cancellation`, `paper_position_observation` | `paper.fill_posted`, `paper.cancellation_posted`, `paper.position_captured` |
| `paper_decision.py` | `paper` | `paper_decision` | `paper.decision_recorded`, `paper.decision_settled` |
| `paper_research.py` | `paper` | `paper_research_decision` | `paper.research_staged`, `paper.research_published`, `paper.research_decided` |
| `paper_reconcile.py` | `paper` | `paper_reconciliation` | `paper.reconciliation_updated` |
| `paper_resume.py` | `paper` | `paper_risk_state` | `paper.risk_resume_refused` *(on Phase 1/2 failure)*; relies on `paper_risk` for `paper.risk_resumed` |
| `nautilus_runtime.py` *(via owning `CompanyRuntime` / `paper_account` / `paper_window` `write_tx` coordinator under `WriterLease`)* | `paper` | `paper_execution_runtime` | `paper.runtime_prepared`, `paper.runtime_bound`, `paper.runtime_switched`, `paper.runtime_supervised` |
| `runtime/venue_coordinator.py` *(via owning `CompanyRuntime` / `paper_account` / `paper_window` `write_tx` coordinator under `WriterLease`)* | `paper` | `venue_segment` | `paper.venue_segment_planned`, `paper.venue_segment_rotated` |
| `staffing.py` | `org` | `staffing_choice`, `hire_receipt` | `org.staffing_staged`, `org.staffing_chosen`, `org.agent_hired` |
| `onboarding.py` | `org` | `hire_onboarding` | `org.onboarding_updated`, `org.onboarding_accepted` |
| `conversations.py` | `work` | `project_message` | `work.project_message_posted` |
| `conversation_delivery.py` | `work` | `project_delivery` | `work.project_delivery_acknowledged` |
| `work_handoff.py` | `work` | `work_handoff` | `work.handoff_offered`, `work.handoff_accepted` |
| `work_peer_response.py` | `work` | `work_peer_requests` | `work.peer_request_opened`, `work.peer_request_read` *(alongside `work_kernel.py` `answered/declined/expired`)* |

   - **Disposition of the remaining 4 write modules (`abandoned_work.py`, `company_assessment.py`, `work.py`, `market_worker.py`)**:
     - `market_worker.py` writes strictly to its separate market-data journal (`market_scope`, `spot_instrument_terms`, `market_trade`), never `company.sqlite`, so it emits zero `company.sqlite` `audit_event`s.
     - `company_assessment.py` (`assessment_*` legacy tables) is internal/legacy without dedicated Client UI screens and remains silent unless surfaced on a future projection.
     - `work.py` (`schedule_follow_up`, `finish_first_duty`, `claim_follow_up`, `settle_review`, `mark_review_incomplete`) and `abandoned_work.py` (`requeue_uncommitted_work`) are legacy `duty` / `work_follow_up` `write_tx` modules predating `runtime/work_kernel.py`; whenever active legacy `duty` transitions must appear on `/work/timeline` within `<= 1s`, emit `work.duty_updated` / `work.duty_requeued` inside `write_tx` (or migrate those paths onto `runtime/work_kernel.py`, which already emits `audit_event`s).

4. **Flow 13 evaluator-store feed/poll adapter (`[M3 Extension]`)**:
   - Because `offline-trial-registry-v1` lives in a separate SQLite DB (`trial_registry.py`) with no `audit_event` table and triggers/views/indexes prohibited (`trial_registry.py:538-558`), `CompanyDaemon` maintains a read-only evaluator feed/poll adapter checking evaluator `mtime` and watermark counts across `trial_reservation`, `trial_observation`, `trial_invalidation`, `trial_grade`, and `trial_pairwise_grade` (or receiving an in-process evaluator callback) to surface `<= 1s` updates on `/evaluations/*` without writing to `company.sqlite`.

### 3. Generalized `POST /v1/commands` & `GET /v1/commands/{command_id}?operation=...`

#### 3.1 Request envelope, `preconditions` vs `preconditions_json`, and field types
- **`POST /v1/commands` headers & body (`protocol.py`, `server.py:708-860`)**:
  - Required header: `Idempotency-Key` (`1..128` printable ASCII chars; duplicate header rejected with `400`).
  - JSON object body (`<= 64 KiB`):
    - `company_id` (str, must equal daemon `self._company_id`, else `403`)
    - `command_id` (str, optional; if present MUST equal `Idempotency-Key`, else `400`)
    - `command_type` (str, registered in command registry; supports both `snake_case` and existing `review_tool.{activate,revert,quarantine}` dotted names)
    - `contract_version` (str, supported for `command_type`, else `422`)
    - `preconditions` (JSON object on the HTTP wire — stored canonicalized/serialized into SQLite `command_receipt.preconditions_json`, reconciling `preconditions` vs `preconditions_json`)
    - `payload` (JSON object): carries operation-specific arguments, including **`payload.reason`** (`str`, `1..2000` trimmed characters — **required** inside `payload` on operational/governance commands per the registry below, matching `src/sourdough/ipc/server.py:791` `payload.get("reason")`, `paper_risk.py:89,263`, and `review_tool_activation.py:544-548`; **omitted** on `register_owner` which carries `OwnerChallenge`/`OwnerProof` inside `payload`). Keeping `reason` inside `payload` guarantees `payload_sha256` covers `reason` for `CommandConflict` idempotency conflict detection (`storage/kernel.py:203`); **never** place `reason` as a separate top-level HTTP body key outside `payload`.
    - `payload_sha256` (optional/verified 64-hex SHA-256 of canonical `payload` including `payload.reason`; mismatch returns `400`)
- **Explicit revision types (never conflated)**:
  - `charter_revision`: `str` (e.g., `"c0"`, `"c1"`)
  - `mandate_revision` / `expected_mandate_revision`: `int` (monotonic charter amendment counter)
  - `effective_revision` / `expected_revision`: `int` (monotonic `paper_risk_state` / `review_tool_activation` / `DecisionQuestion` revision)
  - `restriction_revision` / `expected_restriction_revision`: `int` (per-`restriction_id` monotonic revision in `RestrictionView`)
- **Generalized `_ALLOWED_PRECONDITION_KEYS` & Command Dispatch Registry**:
  - Replace `origin/main` hardcoded `_ALLOWED_PRECONDITION_KEYS = frozenset({"charter_revision", "expected_revision"})` (`server.py:62,780`) and `_RISK_OPERATIONS` (`paper_risk.py`) with a declarative registry keyed by `(command_type, contract_version)`:
    Each entry specifies `(required_role, required_precondition_keys, optional_precondition_keys, reason_required, handler_fn)`. Any extra or missing precondition key returns HTTP `400` **before** calling `admit_command_receipt`.
- **Error & denial taxonomy**:
  - `200 OK` (`command_receipt` finalized — **`[M3 Extension]` unified 12-field receipt schema**): `{status: "found", company_id, command_id, command_type, contract_version, state: "applied" | "denied", disposition, denial_code: null | "conflicting_revision" | "<domain_code>", effective_revision, applied_seq, recorded_at_ns, peer_pid}`. *(This `[M3 Extension]` schema converges `[Live origin/main]` `POST /v1/commands` [`server.py:846-857`, 10 fields omitting `contract_version` and `applied_seq`] and `GET /v1/commands/{command_id}` [`paper_risk.py:553-570`, returning `created_at_ns`/`finalized_at_ns` without `contract_version`, `disposition`, `recorded_at_ns`, or `peer_pid`].)*
  - `400 Bad Request`: invalid JSON, missing/invalid `Idempotency-Key`, missing/invalid `?operation=` on `GET`, invalid precondition keys, invalid `payload.reason` length, `QUOTE_MAX_AGE_NS = 15s` expiry on `resume_new_risk`, or unsettled paper obligations blocking resume.
  - `401 Unauthorized`: missing/invalid/expired `client-web` session token or port-bound cookie.
  - `403 Forbidden`: `authorize_board=None`, unbound/revoked `board_binding`, `bootstrap_locked` on non-bootstrap routes/commands (rejected pre-query before existence check), `board_viewer` attempting `POST /v1/commands`, missing delegation scope, company mismatch, or `(receipt.actor_id, receipt.command_type) != (principal_id, operation)` (`CommandUnauthorized` in `storage/kernel.py:325-328`) on `GET /v1/commands/{command_id}`.
  - `404 Not Found`: unknown route or `command_id` absent (`read_command_receipt` returns `None` at `storage/kernel.py:322-323`) for an authenticated bound caller on `GET /v1/commands/{command_id}?operation=...` (mitigated by `uuid4()` entropy; optionally uniform `403`/`404` masking if strict ID-existence hiding among bound principals is enabled).
  - `409 Conflict`: `CommandConflict` — `Idempotency-Key` reused with differing `(actor_id, command_type, contract_version, payload_sha256, preconditions_json)` (`storage/kernel.py:154-253`).
  - `413 Payload Too Large`: request body `> 64 KiB` or response `> 4 MiB`.
  - `422 Unprocessable Entity`: unsupported `command_type` or `contract_version`.
  - `503 Service Unavailable`: `WriterRefused` (`WriterLease` contention) or `_MAX_IN_FLIGHT` / `_MAX_EVENT_WAITERS` exhaustion.

#### 3.2 `GET /v1/commands/{command_id}?operation=<command_type>`
- Query parameter `operation` (`command_type`) is **required** (`400` if omitted or empty).
- Authenticates caller `principal_id` and active `board_binding` first (`403` fail closed if unbound or `bootstrap_locked` before any SQLite query), then calls `storage/kernel.py:read_command_receipt(connection, company_id=..., command_id=..., principal=principal_id, operation=operation)` (`storage/kernel.py:314-329`), returning `200` `command_receipt` on match, `403` on `CommandUnauthorized` (existing foreign `(actor_id, command_type)`), or `404` on `not_found` (`None`).

#### 3.3 Separating `[Live origin/main]` `paper-risk-command-v1` from `[M3 Extension]` `RestrictionView`
- **`[Live origin/main]` `paper-risk-command-v1`** (`suspend_new_risk`, `resume_new_risk`):
  - Preserved untouched for backward compatibility: `preconditions == {"charter_revision", "expected_revision"}` (`paper_risk.py`).
  - `resume_new_risk` runs Phase 1 (`_settled_company` + `venue.read_current_risk_cut()` enforcing `QUOTE_MAX_AGE_NS = 15 * 10**9` (`15s`)) and Phase 2 (`BEGIN IMMEDIATE` re-verifying `cut.observed_ns <= time_ns() <= cut.expires_ns`) **server-side inside `POST /v1/commands`**. Expiry or unsettled obligations return HTTP `400` and leave `paper_risk_state.suspended = 1`.
- **`[M3 Extension]` `RestrictionView` (`restriction-command-v1` & `paper-risk-command-v2`)**:
  - Canonical `v1/core` table `restriction(restriction_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, cause_category TEXT NOT NULL CHECK (cause_category IN ('paper_risk', 'company_pause', 'restore_quarantine', 'maintenance_lock')), scope_ref TEXT NOT NULL, restriction_revision INTEGER NOT NULL, active INTEGER NOT NULL CHECK (active IN (0, 1)), operator_releasable INTEGER NOT NULL CHECK (operator_releasable IN (0, 1)), reason TEXT NOT NULL, recorded_by TEXT NOT NULL, recorded_at_ns INTEGER NOT NULL, cleared_at_ns INTEGER)` (`STRICT`, registered in `DOMAIN_TABLE_OWNERS`).
  - **`restriction-command-v1`** governs company-wide emergency controls (`pause_company`, `resume_company`) and **`paper-risk-command-v2`** governs cause-scoped paper risk (`suspend_new_risk`, `resume_new_risk`). Both accept `{restriction_id, expected_restriction_revision, charter_revision}` in `preconditions`.
  - Clearing `R1` (`restriction_id = "rest-p1"`) updates only `rest-p1` (`active = 0`) and increments its revision; any active competing stop `P2` (`restriction_id = "rest-p2"`) remains `active = 1` so `resume_new_risk` / `resume_company` keeps risk/company paused until every active cause is explicitly resolved.

#### 3.4 Complete M3 command contract registry

| `command_type` | `contract_version` | Role / Gate | `preconditions` keys | `reason` | Domain Owner, Handler & Key Invariants |
| --- | --- | --- | --- | --- | --- |
| `register_owner` | `bootstrap-command-v1` `[M3]` | `bootstrap_locked` only | `{company_id, store_key, charter_revision, challenge_id}` | No (`OwnerProof` in `payload`) | Authority bootstrap handler. Atomically consumes `board_owner_challenge`, verifies `OwnerProof`, creates `board_binding(binding_revision=1)` + `role_assignment('board_control', role_revision=1)`, emits `audit_event`. Concurrent second request fails closed. |
| `amend_charter` | `charter-command-v1` `[M3]` | `board_control` | `{charter_revision, expected_mandate_revision}` | Yes (`1..2000`) | Charter service (`found-company/amend`). Advances `mandate_revision` / `charter_revision` inside `write_tx`; CAS mismatch writes `denied` (`conflicting_revision`). |
| `bind_board_principal`, `assign_role` | `rbac-command-v1` `[M3]` | `board_control` (`delegation_limit > 0`) | `{principal_id, expected_binding_revision}` | Yes (`1..2000`) | Authority service (`authorize-actor/manage-roles`). `bind_board_principal` binds/updates `board_binding`; `assign_role` creates a new `role_assignment` row keyed by `{principal_id, expected_binding_revision}` (no prior `assignment_id` exists). Cannot self-escalate. |
| `replace_role`, `revoke_role` | `rbac-command-v1` `[M3]` | `board_control` (`delegation_limit > 0`) | `{assignment_id, expected_role_revision}` | Yes (`1..2000`) | Authority service (`authorize-actor/manage-roles`). Flat non-inherited per-assignment `role_revision`; mutating or revoking an existing assignment increments `role_revision` immediately and invalidates stale cached expectations. |
| `submit_decision_answer` | `decision-command-v1` `[M3]` | `board_control` | `{question_id, expected_revision, payload_hash}` | Optional (`0..2000`) | Decision/staffing handler. Validates `open` `DecisionQuestion` revision and `respond_by_ns`; writes `AcceptedAnswer`, `AnswerApplication`, `staffing_choice`, `hire_receipt`; `hire_onboarding` stays separate (`provisioning \| blocked \| ready \| accepted`). |
| `revoke_grant` | `authority-command-v1` `[M3]` | `board_control` | `{agent_id, expected_authority_epoch}` | Yes (`1..2000`) | Authority/work handler. Sets `standing_grant.scope = 'revoked'`, increments `authority_epoch`, revokes `work_responsibilities`, cancels armed `work_conditions`, fences active `work_runs` (`work.run_fenced`). |
| `suspend_new_risk`, `resume_new_risk` | `paper-risk-command-v1` `[Live]`, `paper-risk-command-v2` `[M3]` | `board_control` (`paper` installed) | `v1`: `{charter_revision, expected_revision}`; `v2`: `{restriction_id, expected_restriction_revision, charter_revision}` | Yes (`1..2000`) | `paper_risk.py` / `paper_resume.py`. Only gates `submit_paper_order`; fills/reconciliation/research continue; `resume_new_risk` enforces server-side `15s` `QUOTE_MAX_AGE_NS` Phase 1->2 cut. |
| `pause_company`, `resume_company` | `restriction-command-v1` `[M3]` | `board_control` | `{restriction_id, expected_restriction_revision, charter_revision}` | Yes (`1..2000`) | `RestrictionView` handler. Multi-cause pauses; `R1` cannot clear `P2`; `resume_company` refuses `restore_quarantine` restrictions (must use `release_restore_quarantine`). |
| `interrupt_run` | `work-command-v1` `[M3]` | `board_control` | `{run_id, expected_execution_epoch}` | Yes (`1..2000`) | `runtime/work_kernel.py`. Fences single active `work_run` (`work.run_fenced`) without cancelling unrelated runs. |
| `cancel_work` | `work-command-v1` `[M3]` | `board_control` | `{work_id, expected_work_version}` | Yes (`1..2000`) | `runtime/work_kernel.py`. Cancels single `work_items` row under CAS `work_version`. |
| `retire_employee` | `org-custody-command-v1` `[M3]` | `board_control` | `{agent_id, employment_revision}` | Yes (`1..2000`) | `retirement.py` + `work_kernel.py` wired with `admit_command_receipt`. Blocks retiring founder/acting manager. **Schema resolution (`03_work_kernel.sql:6,10-12`):** because `origin/main` `work_items.state` `CHECK` allows `('due','scheduled','running','waiting','settled','cancelled','blocked')` and excludes `'unassigned'`, `[M3 Extension]` preserves unsettled `work_items` cleanly by setting `state = 'blocked'` paired with a canonical `v1/core` `work_custody_transfer(work_item_id REFERENCES work_items(work_item_id), retired_agent_id, custody_status='awaiting_reassignment')` table (or adding `'unassigned'` to `03_work_kernel.sql` with matching `verify_v1_schema` pack hashes and `DOMAIN_TABLE_OWNERS` entry) rather than failing SQL `CHECK` constraints. |
| `change_paper_binding` | `paper-account-command-v1` `[M3]` | `board_control` (`paper` installed) | `{previous_revision, charter_revision}` | Yes (`1..2000`) | `paper_account.py:270` wired with `admit_command_receipt` + `peer_pid`. Enforces `environment == "paper"` and drain preconditions (`outstanding-company-effect`, `unverified-flat-destination`); fences orders/resume/deploy while pending. |
| `configure_paper_runtime`, `start_paper_runtime` | `paper-runtime-command-v1` `[M3]` | `board_control` (`paper` installed) | `{charter_revision, expected_binding_revision}` | Yes (`1..2000`) | `nautilus_runtime.py` wired with `admit_command_receipt`. Explicitly authorized paper-runtime configuration/start under `environment == "paper"`; company may also configure/start without Client; zero raw secrets returned. |
| `activate_strategy_release`, `rollback_strategy_deployment` | `strategy-deploy-v1` `[Live backend / M3 UDS]` | `board_control` (`!= owner_agent_id` for activate) | `{expected_active_deployment_id}` | Optional (`0..2000`) | `strategy_comparison.py:1845,2069` (`admit_command_receipt` already implemented). Forward `peer.pid` (replacing `os.getpid()`) and finalize `'denied'` receipts (`conflicting_revision` / `self_activation_refused`) instead of uncaught exceptions. |
| `cut_checkpoint` | `checkpoint-command-v1` `[M3]` | `board_control` | `{expected_company_seq}` | Optional (`0..2000`) | `storage/checkpoint.py` wired with `admit_command_receipt`. Acquires `WriterLease` (`503` on `WriterRefused`), produces `0o600` `sourdough-checkpoint-v2` zip. |
| `restore_checkpoint` | `restore-command-v1` `[M3]` | `board_control` | `{checkpoint_sha256, destination_id}` | Yes (`1..2000`) | `paper_restore.py` wired with `admit_command_receipt`. Checks `paper_sender_lease`, runs `_settle_candidate`, retires old store (`company_store_retired`), boots replacement store in `restore_quarantine`. |
| `release_restore_quarantine` | `restore-command-v1` `[M3]` | `board_control` | `{restriction_id, expected_restriction_revision, recovery_receipt_id}` | Yes (`1..2000`) | `paper_restore.py` / `RestrictionView` handler. Fails closed (`400` / `denied`) unless `RecoveryReceipt` dimensions 1–4 are `VERIFIED`; clears `restore_quarantine` restriction. |
| `review_tool.activate`, `review_tool.revert`, `review_tool.quarantine` | `review-tool-activation-v1` `[Live backend / M3 UDS]` | `board_control` | `activate`/`revert`: `{expected_revision, qualification_id}`; `quarantine`: `{faulted_asset_id}` | Optional/Yes (`quarantine`) | `review_tool_activation.py:333,473,689` (`admit_command_receipt` already implemented). Propagates `peer.pid` (replacing `None`); `quarantine` cascades `parent_asset_id` DAG and, iff active asset is inside quarantined subgraph, restores newest unquarantined row globally ordered by `revision DESC` (or leaves active asset unchanged with `restored_candidate_id = None` if outside). |

*(Note: Paper order cancellation and order reconciliation remain strictly autonomous Agent / runtime duties — `/paper/orders` and `/paper/reconciliation` are read-only surfaces with zero `cancel_paper_order` or `reconcile_paper_orders` board commands on `POST /v1/commands`.)*

## Functional and quality requirements

- **FR.** Stream redacted 7-field `audit_event` NDJSON rows on `GET /v1/events`, inject `on_commit` `_event_cond.notify_all()` wakeups into `CompanyRuntime` and command handlers, emit `append_audit_event` inside `write_tx` across the 13 silent `company.sqlite` domain modules (and via the owning `WriterLease` `write_tx` coordinator for `nautilus_runtime.py` and `runtime/venue_coordinator.py`), and attach a separate read-only evaluator feed/poll adapter for Flow 13 (`<= 1s` update target).
- **FR.** Replace hardcoded `_ALLOWED_PRECONDITION_KEYS` and `_RISK_OPERATIONS` with a versioned `(command_type, contract_version)` dispatch registry covering all governance, authority, work, paper, and continuity operations while preserving `[Live origin/main]` `paper-risk-command-v1` and enforcing `(principal_id, operation)` receipt scoping on `GET /v1/commands/{command_id}?operation=...`.
- **NFR.** Serialize conflicting mutations via `WriterLease` + `BEGIN IMMEDIATE`, enforce `QUOTE_MAX_AGE_NS = 15s` strictly server-side inside `POST /v1/commands`, and ensure `retire_employee` custody preservation complies with canonical `v1` SQLite schema verification.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Runtime wakeup & 15-module audit | Open `GET /v1/events?after_seq=S0&wait_ms=5000`, trigger a `CompanyRuntime` `paper_order` or `work_handoff` write, and verify the 7-field redacted NDJSON line arrives in `< 1s` with `actor_id`/`run_id`/`command_id`/`payload_json` omitted. |
| Competing stops (`P1`/`P2`) & `15s` cut | Submit `R1` against `rest-p1` under `paper-risk-command-v2` while `rest-p2` is active; confirm `rest-p2` stays active. Delay `resume_new_risk` Phase 1->2 >15s; confirm HTTP `400` and `suspended = 1`. |
| DAG quarantine & `retire_employee` custody | Quarantine faulted tool `A` via `review_tool.quarantine`; verify transitive descendants quarantine and sibling-branch `revision DESC` fallback activates iff active tool was inside subgraph. Retire an employee via `retire_employee`; verify unsettled `work_items` remain in `'blocked'` custody (`awaiting_reassignment`) without violating SQL `CHECK` constraints. |

**Oracle:** Inspect `src/sourdough/ipc/server.py`, `src/sourdough/runtime/engine.py`, `src/sourdough/storage/kernel.py`, `src/sourdough/storage/schema/v1/core/03_work_kernel.sql`, `src/sourdough/paper_resume.py`, `src/sourdough/strategy_comparison.py`, and `src/sourdough/review_tool_activation.py`.

## Operations and change review

Every newly wired command handler MUST execute `admit_command_receipt(..., peer_pid=peer.pid)` and `finalize_command_receipt` inside a single `BEGIN IMMEDIATE` `write_tx`, followed by `append_audit_event` and `_event_cond.notify_all()` on commit.
