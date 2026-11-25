# Meta and authorized read projections

Design draft · Verification **UNRUN** · Responsible component: Control Plane Read Projections (`src/sourdough/ipc/server.py`, `src/sourdough/work_projection.py`, domain readers)

[Feature intent](../intent.md) · [Bridge and auth](../bridge-and-auth/spec.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Flow observability](../../inspect/flow-observability/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Expose read-only company, governance, work, organization, paper-fund, operations, and separated offline-evaluation projections over `CompanyDaemon`'s UDS Control Plane (`GET /v1/meta` and `GET /v1/projections/*`). Reads NEVER mutate operational state, NEVER execute SQLite writes, NEVER read `.desk/drafts/*` bodies, and NEVER compute synthetic client-side financial figures when domain coverage is missing.

Input contracts: [Transport, loopback bridge, and role authorization](../bridge-and-auth/spec.md) · [Authorized work timeline](../../../company/systems/operations/work-timeline/read/spec.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Flow observability](../../inspect/flow-observability/spec.md). These are public service contracts, not imports of client UI packages.

## Design decision and review

### 1. Reconciled `/v1/*` endpoint inventory (`33` concrete endpoints)

Across all 33 UI routes in [Client screens and navigation](../../inspect/screens/spec.md), the UDS Control Plane defines **`33` concrete `/v1/*` endpoints** (served via `GET /v1/meta`, the generic `GET /v1/projections/{name}` router pattern, `GET /v1/events`, `POST /v1/commands`, and `GET /v1/commands/{command_id}`):

- `1` capability/identity endpoint: `GET /v1/meta` (`[Live origin/main]` + `[M3 Extension]` RBAC/incarnation fields)
- **`29` concrete `GET /v1/projections/*` endpoints** (`1` `[Live origin/main]` `work-timeline` + `28` `[M3 Extension]` endpoints, including `3` two-segment paths `/evaluations/{suites,grading,lineage}` and `2` parameterized paths `work-runs/{run_id}`, `decision-trace/{order_id}`)
- `1` event stream endpoint: `GET /v1/events`
- `2` command/receipt endpoints: `POST /v1/commands` and `GET /v1/commands/{command_id}?operation=...`

*(Earlier summaries citing `27` `/v1/*` endpoints omitted the three two-segment `/evaluations/*` paths, parameterized sub-resources, or split command/receipt endpoints; `33` is the canonical concrete endpoint count covering all `33` UI routes.)*

### 2. Headers, ETag caching, common envelope metadata, and anti-tear snapshot invariant

- **Request headers**:
  - `If-None-Match`: Exact quoted ETag (`"..."`) or weak `W/"..."` in comma-separated format. Wildcard `*` MUST be rejected (`400` / mismatch per `server.py:78-85`), never returning `304`.
  - Bridge-forwarded auth (`Authorization: Bearer` or port-bound `HttpOnly; SameSite=Strict` cookie) + `Host`/`Origin` pinning per [Bridge and auth](../bridge-and-auth/spec.md).
- **Response headers (all `GET /v1/meta` & `GET /v1/projections/*` replies)**:
  - `Connection: close`, `Cache-Control: no-store`, `X-Sourdough-Company-Id: <company_id>`, `X-Sourdough-Event-Seq: <int>`.
  - `ETag`: `"{revision}:{event_seq}"` for `work-timeline-v2` and all `[M3 Extension]` projections (`"{revision}"` preserved for legacy `work-timeline-v1`).
  - `304 Not Modified`: Empty body with `ETag`, `X-Sourdough-Event-Seq`, `X-Sourdough-Company-Id`, `Connection: close`, `Cache-Control: no-store`.
- **Common JSON envelope fields (`[Live origin/main]` `v2` + `[M3 Extension]`)**:
  - Every projection response embeds `{company_id, store_key, contract_revision, revision, as_of_ns, event_seq, service_incarnation_id}` where `store_key = "{dev}:{ino}"`, `revision = sha256(canonical_json(payload_body))`, `as_of_ns = time_ns()`, `event_seq = max_event_seq(connection, company_id)`, and `service_incarnation_id` (`[M3 Extension]`) uniquely identifies the daemon incarnation (`uuid4()` per startup).
- **Single-`read_tx` + dual-`stat` anti-tear invariant (`work_projection.py:28-87`)**:
  - Every `company.sqlite` projection opens a single `open_reader(store)` (`mode=ro`, `PRAGMA query_only=ON`) `read_tx` snapshot, verifies `initial_stat` vs `post_tx_stat` via `os.stat(..., follow_symlinks=False)` (`st_dev`, `st_ino`, `st_uid`), computes `as_of_ns` and `event_seq` inside the same snapshot, and fails closed if the underlying store inode changed mid-read (such as during a checkpoint restore swap).
  - Unsupported projection names (`404` / `422`) or unsupported `contract_revision` query values (`400`) fail closed **before** accessing the SQLite store.
- **RBAC gating**: Under `bootstrap_locked` (zero active `board_binding` rows), all `GET /v1/projections/*` fail closed with `403`. Bound `board_viewer` and `board_control` principals receive projections filtered strictly by their active `role_assignment.scope_ref` and `audience`; unauthorized scope/entity access fails closed (`403`) or omits restricted evidence with `"Unavailable"` placeholders rather than leaking titles or metadata.

### 3. `GET /v1/meta` capability and authority negotiation

`GET /v1/meta` executes a single `read_tx` verifying `_identity(db) == self._company_id` and returns:

| Field | Status | Type & Semantics |
| --- | --- | --- |
| `control_plane_version` | `[Live origin/main]` | `"control-plane-v1"` |
| `company_id` | `[Live origin/main]` | Store company identifier |
| `store_key` | `[Live origin/main]` | `"{st_dev}:{st_ino}"` |
| `schema_version` | `[Live origin/main]` | `1` (`PRAGMA user_version`) |
| `active_domains` | `[Live origin/main]` | Sorted installed domains (`["core"]` or `["core", "paper"]`) |
| `daemon_pid` | `[Live origin/main]` | OS PID of `CompanyDaemon` |
| `event_seq` | `[Live origin/main]` | Latest `audit_event.event_seq` for `company_id` |
| `service_incarnation_id` | `[M3 Extension]` | Per-daemon-boot UUID; changes across restarts/restores |
| `binding_state` | `[M3 Extension]` | `"bootstrap_locked"` (zero active `board_binding`s) \| `"bound"` |
| `principal_id` | `[M3 Extension]` | Authenticated caller `principal_id` (`null` when `bootstrap_locked` or unbound) |
| `role` | `[M3 Extension]` | `"bootstrap_locked"` \| `"board_viewer"` \| `"board_control"` |
| `session_class` | `[M3 Extension]` | `"direct_uds"` \| `"bridge_session"` |
| `supported_projections` | `[Live origin/main]` + `[M3 Extension]` | Map of projection name -> supported `contract_revision` array (e.g. `"work-timeline": ["work-timeline-v1", "work-timeline-v2"]`) |
| `supported_commands` | `[Live origin/main]` + `[M3 Extension]` | Map of `command_type` -> supported `contract_version` array (omits `paper` commands when `paper` domain absent; omits `resume_new_risk` if `engine_directory is None`) |
| `evaluator_store_attached` | `[M3 Extension]` | Boolean indicating whether a separate Flow 13 `offline-trial-registry-v1` store is mounted read-only |

### 4. All 29 concrete `GET /v1/projections/*` endpoint contracts

#### Group A — Overview, Restrictions, Incidents & Governance (Flows 01, 05, 06, 11)

| Endpoint | Contract Revision & Tag | Visibility & Query Params | Exact Schema & Missing-Data / Honesty Rules |
| --- | --- | --- | --- |
| `GET /v1/projections/company-overview` | `company-overview-v1` `[M3 Extension]` | `board_viewer`+ | `{...meta, charter_revision, mandate_revision, active_restrictions_count, active_restrictions_summary[], open_decisions_count, open_incidents_count, paper_risk_state?, work_summary: {due, running, waiting, blocked}, coverage_warnings[], recent_audit_events[]}`. |
| `GET /v1/projections/restrictions` | `restrictions-v1` `[M3 Extension]` | `board_viewer`+ (`?include_resolved=bool`) | `{...meta, restrictions: [{restriction_id, cause_category ('paper_risk' \| 'company_pause' \| 'restore_quarantine' \| 'maintenance_lock'), scope_ref, restriction_revision, active, operator_releasable, reason, recorded_by, recorded_at_ns, precheck?}]}`. Competing causes (`rest-p1`, `rest-p2`) remain distinct rows. |
| `GET /v1/projections/incidents` | `incidents-v1` `[M3 Extension]` (spec-only `monitor-company/incident`) | `board_viewer`+ (`?state=open\|all`) | `{...meta, incidents: [{incident_id, severity, owning_system, state, summary, recorded_at_ns, next_recovery_duty_id?, correlated_event_seqs[]}]}`. Missing fields serialized as `null` (rendered `"Unavailable"`). |
| `GET /v1/projections/decisions` | `decisions-v1` `[M3 Extension]` | `board_viewer`+ (`?status=open\|all`) | `{...meta, questions: [{question_id, kind ('owner_authority' \| 'clarification' \| 'provider_permission'), revision, payload_hash, respond_by_ns, alternatives[], context_ref, staffing_summary?: {slots, remaining_turns, allowed_scope, execution_profile}, status}], ledger: [{question_id, accepted_answer?, answer_application?, answer_delivery?}]}`. `AcceptedAnswer`, `AnswerApplication`, and `AnswerDelivery` remain separate fields. |
| `GET /v1/projections/charter` | `charter-v1` `[M3 Extension]` | `board_viewer`+ | `{...meta, charter_revision, mandate_revision, ceilings: {review_cost_minor?, daily_budget_minor?}, permitted_profiles[], history: [{charter_revision, mandate_revision, amended_by, reason, recorded_at_ns, diff_summary}]}`. |
| `GET /v1/projections/roles` | `roles-v1` `[M3 Extension]` | `board_viewer`+ | `{...meta, bindings: [{principal_id, binding_revision, issuer_kind, subject_ref, session_class, state, created_at_ns}], assignments: [{assignment_id, principal_id, role_name, role_revision, scope_ref, delegation_limit, assigned_by, expires_at_ns, revoked_at_ns}]}`. **Zero** raw tokens, nonces, or secret proofs exposed. |

#### Group B — Work, Runs, Fences & Conditions (Flows 02, 05, 10)

| Endpoint | Contract Revision & Tag | Visibility & Query Params | Exact Schema & Missing-Data / Honesty Rules |
| --- | --- | --- | --- |
| `GET /v1/projections/work-timeline` | `work-timeline-v1` `[Live origin/main]`, `work-timeline-v2` `[Live origin/main]` + `[M3 Extension]` fields | `board_viewer`+ (`?contract_revision=work-timeline-v1\|work-timeline-v2`, `?state=...`, `?owner_agent_id=...`) | Preserves `v1`/`v2` top-level fields (`company_id`, `access`, `duties` [with nested `decision`, wake/follow-up, and active run fields], `paper`, `risk_control`, `contract_revision`, `revision`, `as_of_ns`, plus `event_seq` and `store_key` in `work-timeline-v2`; `work_projection.py:64-86`, `server.py:555-561`). **M3 upgrade to `work_projection.py`:** alongside legacy `duties`, project canonical `work_items` (`work_id`, `state`, `owner_agent_id`, `source_kind`, `work_version`, `next_condition`), `work_runs`, `work_conditions`, and `work_peer_requests`. Missing `risk_control` on non-paper stores is `null`; controlled store error returns `{"state": "unavailable", "effective_revision": null, "last_applied": null}`. |
| `GET /v1/projections/work-runs` | `work-runs-v1` `[M3 Extension]` | `board_viewer`+ (`?status=...`, `?agent_id=...`) | `{...meta, runs: [{run_id, work_id, agent_id, status, execution_epoch, fencing_token, lease_owner, lease_expires_at_ns, budget_ceiling_minor, started_at_ns, finished_at_ns, trace_event_count, offloaded_log_count}]}`. `.desk/drafts/*` bodies are strictly excluded. |
| `GET /v1/projections/work-runs/{run_id}` | `work-run-detail-v1` `[M3 Extension]` | `board_viewer`+ | `{...meta, run: {...}, trace_calls: [{seq, recorded_at_ns, tool, status, arg_keys[], result_sha256, error_kind}], offloaded_logs: [{seq, tool, log_path, bytes, sha256, keys[]}], linked_publications: [{asset_id, excerpt_sha256}]}`. Derived strictly from `.sourdough-traces/<company_id>/<run_id>.jsonl` and `.desk/logs/` metadata; zero `.desk/drafts/*` text. |
| `GET /v1/projections/fences` | `fences-v1` `[M3 Extension]` | `board_viewer`+ | `{...meta, active_leases: [{run_id, fencing_token, lease_owner, lease_expires_at_ns}], recently_fenced: [{run_id, execution_epoch, fenced_reason, fenced_at_ns}]}`. |
| `GET /v1/projections/conditions` | `conditions-v1` `[M3 Extension]` | `board_viewer`+ | `{...meta, conditions: [{condition_id, work_id, condition_revision, trigger_kind ('timer' \| 'shared_asset_revision' \| 'peer_response'), status, due_at_ns, wait_until_ns, source_ref}], suppressed_self_wakes: [{event_seq, work_id, trigger_kind, recorded_at_ns}]}`. `due_at_ns === null` / `wait_until_ns === null` renders `"Not scheduled"`. |

#### Group C — Organization, Desk Metadata, Collaboration & Offboarding (Flows 06, 10)

| Endpoint | Contract Revision & Tag | Visibility & Query Params | Exact Schema & Missing-Data / Honesty Rules |
| --- | --- | --- | --- |
| `GET /v1/projections/roster` | `roster-v1` `[M3 Extension]` over `[Live origin/main]` `staffing`/`onboarding`/`agent_desk` | `board_viewer`+ | `{...meta, agents: [{agent_id, execution_profile, employment_revision, authority_epoch, standing_grant: {scope, active}, private_desk: {manifest_revision, draft_count, offloaded_logs[]}}]}`. **Strict Desk opacity:** `private_desk` (`04_staffing_and_assessment.sql:66-69`) and `.desk/drafts/*` (`<= 6` files, `<= 800B`) never expose draft filenames or bodies; `draft_count` is a numeric count only. |
| `GET /v1/projections/staffing` | `staffing-v1` `[M3 Extension]` over `[Live origin/main]` `staffing.py`/`onboarding.py` | `board_viewer`+ | `{...meta, authorities: [{authority_id, slots, turns_remaining, allowed_scope, execution_profile}], choices: [{choice_id, choice_kind ('hire' \| 'reassign' \| 'defer'), hire_receipt_id?}], onboarding: [{agent_id, state ('provisioning' \| 'blocked' \| 'ready' \| 'accepted'), attempts, max_attempts, retry_at_ns, reason, first_duty_id?, enrollment_confirmed}]}`. `hire_receipt` and `hire_onboarding.state` remain separate fields (`appointed != execution_ready`). |
| `GET /v1/projections/collaboration` | `collaboration-v1` `[M3 Extension]` over `[Live origin/main]` `conversations.py`, `work_peer_response.py`, `work_handoff.py`, `company_assets.py` | `board_viewer`+ (scoped by project `audience`) | `{...meta, messages: [{message_id, project_id, author_id, body, cited_asset_id?, asset_accessible, deliveries[]}], peer_requests: [{request_id, request_key, requester_work_item_id, response_work_item_id, state ('open' \| 'answered' \| 'declined' \| 'expired'), deadline_ns, evidence_ref?, resumed_run_id?}], handoffs: [{offer_id, duty_id, from_agent_id, to_agent_id?, asset_id, state, expires_at_ns, successor_work_item_id?, accepted_run_id?}], publications: [{asset_id, author_id, draft_name, draft_revision, start_offset, end_offset, excerpt, excerpt_sha256, audience, referencing_work_ids[]}]}`. If caller lacks `audience` access, `asset_accessible = false` and `excerpt` / `cited_asset_title` are omitted (`null`, rendered `"Unavailable"`). |
| `GET /v1/projections/offboarding` | `offboarding-v1` `[M3 Extension]` over `[Live origin/main]` `retirement.py` | `board_viewer`+ | `{...meta, archives: [{agent_id, employment_revision, retired_at_ns, cutoff_seq, retired_by}], custody_queue: [{work_id, previous_owner_agent_id, state ('blocked' \| 'unassigned' \| 'cancelled'), custody_status ('awaiting_reassignment' \| 'reassigned' \| 'closed'), reassigned_to_agent_id?}]}`. Preserves historical author IDs and published asset references. |

#### Group D — Paper Fund, Execution, Lineage, Statements, Risk & Strategy Trials (Flows 03, 04, 05, 07, 08, 09, 11)

All Group D projections fail closed with `404` / `422` (`"Paper domain not installed"`) on core-only companies and enforce `paper_binding.environment == "paper"` on every response:

| Endpoint | Contract Revision & Tag | Visibility & Query Params | Exact Schema & Missing-Data / Honesty Rules |
| --- | --- | --- | --- |
| `GET /v1/projections/paper-fund` | `paper-fund-v1` `[M3 Extension]` over `[Live origin/main]` `paper_account.py`/`paper_books.py` | `board_viewer`+ | `{...meta, environment: "paper", binding: {binding_revision, account_id, destination_id, limits}, cash_balances[], orders: [{order_id, duty_id, ordered_quantity, filled_quantity, remaining_quantity, open_reservation_minor_units, status, fills[], fees[], cancellation?}], positions: [{instrument, booked_position, open_exposure_flag}]}`. **Honesty (`cancel != flat`):** a cancelled partial-fill order keeps `status = "cancelled"`, `open_reservation_minor_units = 0`, and `booked_position > 0`. |
| `GET /v1/projections/paper-execution` | `paper-execution-v1` `[M3 Extension]` over `[Live origin/main]` Flow 07 | `board_viewer`+ | `{...meta, environment: "paper", connector_revision ('fixed-quote-v1' \| 'nautilus-spot-sandbox-v1' \| 'nautilus-offline-spot-replay-v1'), destination_id, account_id, instrument_rules_digest, sender_lease: {locked, lock_path, journal_key}, segments: [{sequence, active_seconds, drain_margin_seconds, work_seconds, state}], pending_binding_change?: {change_id, target_binding_revision, target_connector_revision, effective_at_ns, blocked_by ('outstanding-company-effect' \| 'unverified-flat-destination' \| null)}}`. |
| `GET /v1/projections/decision-trace/{order_id}` | `decision-trace-v1` `[M3 Extension]` over `[Live origin/main]` Flow 04 | `board_viewer`+ | `{...meta, environment: "paper", order_id, lineage: {duty?, duty_run?, research_plan?, research_snapshot?, research_finding?, research_decision?, paper_decision?, paper_order, fills[], fees[], cancellation?, position?, next_duty?}}`. Missing lineage nodes remain `null` (rendered `"Unavailable"`, never fabricated). |
| `GET /v1/projections/reconciliation` | `reconciliation-v1` `[M3 Extension]` over `[Live origin/main]` `paper_reconcile.py` | `board_viewer`+ | `{...meta, environment: "paper", pending_orders: [{order_id, attempts, max_attempts (6), next_query_at_ns, sources[]}], pending_no_fill_observations[], account_syncs: [{sync_id, observed_at_ns, status}]}`. |
| `GET /v1/projections/paper-statements` | `paper-statements-v1` `[M3 Extension]` over `[Live origin/main]` Flow 08 (`paper_window_statement.py`, `fund_returns.py`, `window_costs.py`, `accounting_close.py`, `paper_funding.py`) | `board_viewer`+ | `{...meta, environment: "paper", windows: [{window_id, window_revision, as_of_ns, trading: {opening_minor, closing_minor, pnl_minor, time_weighted_return: {numerator, denominator} \| null, missing[]}, operating_costs: {operating_cost_atoms, missing[]}, company: {pnl_minor, time_weighted_return: {numerator, denominator} \| null, missing[]}, close_issues: [{reason, owner, reference}], funding_settlement: {status ('booked' \| 'zero' \| 'unavailable'), funding_cashflow_atoms, unavailable_reason}, chart_series: {return_pct_points[], cumulative_pnl_points[]}}]}`. **Strict honesty:** whenever `missing` is non-empty, `CloseIssue` exists, or `funding_settlement.status == 'unavailable'`, affected `pnl_minor`, `time_weighted_return`, and `operating_cost_atoms` MUST be JSON `null` (never `0`). |
| `GET /v1/projections/paper-risk` | `paper-risk-v1` `[M3 Extension]` (extends `[Live origin/main]` `risk_control`) | `board_viewer`+ | `{...meta, environment: "paper", risk_state: {suspended, effective_revision, charter_revision, last_command_id, updated_at_ns}, readiness_precheck: {non_binding: true, settled_company_ok, unresolved_orders_count, pending_binding_change, last_quote_age_ns, quote_max_age_ns: 15000000000}, active_restrictions[], recent_receipts[]}`. `readiness_precheck` is strictly advisory telemetry; authoritative freshness runs server-side inside `POST /v1/commands`. |
| `GET /v1/projections/strategy-trials` | `strategy-trials-v1` `[M3 Extension]` over `[Live origin/main]` `strategy_comparison.py` | `board_viewer`+ | `{...meta, environment: "paper", releases: [{release_id, company_id, owner_agent_id, parent_release_id, strategy_source_sha256, parameters_json, nautilus_lock_sha256, instrument_rules_sha256, risk_config_sha256, fill_policy_sha256, pit_manifest_id, pit_manifest_sha256, research_trial_count_n, bundle_sha256, recorded_at_ns}], pit_manifests[], challenger_trials: [{trial_id, verdict ('PROMOTE' \| 'REJECT' \| 'INCONCLUSIVE'), status, alpha_effective, lower_bound_bps, upper_bound_bps, shared_cost_split, failure_reasons[]}], deployments: [{deployment_id, release_id, status ('active' \| 'superseded' \| 'rolled_back'), program_identity, activated_at_ns, retired_at_ns}], active_deployment_id}`. |

#### Group E — Operations, Continuity & Maintenance (Flows 11, 12)

| Endpoint | Contract Revision & Tag | Visibility & Query Params | Exact Schema & Missing-Data / Honesty Rules |
| --- | --- | --- | --- |
| `GET /v1/projections/checkpoints` | `checkpoints-v1` `[M3 Extension]` over `[Live origin/main]` `storage/checkpoint.py` | `board_viewer`+ | `{...meta, checkpoints: [{cut_id, format ('sourdough-checkpoint-v2'), schema_version, max_event_seq, domain_digest, sha256, desk_digest, captured_ns, archive_path_hint}], writer_lease_active: bool}`. |
| `GET /v1/projections/restore` | `restore-v1` `[M3 Extension]` over `[Live origin/main]` `paper_restore.py` | `board_viewer`+ | `{...meta, company_store_retired: bool, sender_lease_clear: bool, settle_candidate_summary?, recovery_receipt: {receipt_id, dimensions: {data_cut: 'VERIFIED' \| 'FAILED' \| 'PENDING', sole_writer: ..., current_authority: ..., external_reconciliation: ..., eligible_next_actions: ...}}, restore_quarantine_restriction_id?, restore_receipts[]}`. |
| `GET /v1/projections/review-tools` | `review-tools-v1` `[M3 Extension]` over `[Live origin/main]` `evidence_review.py`/`review_tool_activation.py` | `board_viewer`+ | `{...meta, qualifications: [{qualification_id, candidate_id, parent_asset_id, authors[], evaluator_revision, verdict: {stage1_structure_and_negative, stage2_harmlessness, stage3_pass_k_stability, k_trials}}], activations: [{activation_id, candidate_id, revision, state, quarantined, parent_asset_id, restored_candidate_id?}], active_candidate_id, active_revision}`. Includes computed DAG lineage metadata so UI can preview `review_tool.quarantine` blast radius and global `revision DESC` fallback (`restored_candidate_id = None` if active asset lies outside quarantined subgraph). |
| `GET /v1/projections/maintenance` | `maintenance-v1` `[M3 Extension]` (spec-only `request-maintenance/track`) | `board_viewer`+ | `{...meta, items: [{maintenance_id, local_incident_ref, external_connector_request_ref, published_release_digest, schema_compat_status, regression_retest_status, recorded_at_ns}]}`. Keeps external request status strictly separate from local incident closure and regression retest. |

#### Group F — Flow 13 Offline Evaluations (`offline-trial-registry-v1`, Physically Separate Read-Only Store)

| Endpoint | Contract Revision & Tag | Visibility & Query Params | Exact Schema & Missing-Data / Honesty Rules |
| --- | --- | --- | --- |
| `GET /v1/projections/evaluations/suites` | `evaluations-suites-v1` `[M3 Extension]` (separate evaluator SQLite DB) | `board_viewer`+ | `{...meta, registry_format: "offline-trial-registry-v1", suites: [{suite_id, ceiling: {turns, wall_seconds, tokens}, trials: [{result_id, outcome ('completed' \| 'no-decision' \| 'failed' \| 'timed-out'), observation_digest, decision_digest, tool_trace_digest, service_effects_digest}], invalidations: [{result_id, reason}]}]}`. **Hard isolation:** `trial_execution.py` forbids mounting evaluator store inside `trial_root` or referencing `company.sqlite`. |
| `GET /v1/projections/evaluations/grading` | `evaluations-grading-v1` `[M3 Extension]` (separate evaluator SQLite DB) | `board_viewer`+ | `{...meta, registry_format: "offline-trial-registry-v1", blinded_trials: [{result_id, rubric_digest, evaluator_digest, outcome, decision_digest, tool_trace_digest, service_effects_digest}], pairwise_grades: [{pair_id, first_result_id, second_result_id, ab_outcome, ba_outcome, resolution ('winner' \| 'tie' \| 'tie_noise'), consistent, winner_result_id?}], rsi_transfer: [{summary_id, verdict ('SUPPORTED' \| 'UNSUPPORTED' \| 'INCONCLUSIVE'), candidate_authority_violations, candidate_service_incorrect, masked_invalidations_count}]}`. **Blindness:** `arm`, `slot_id`, `run_id`, and raw inputs are stripped from `BlindedTrial`; candidate hard-invariant failures force `UNSUPPORTED` before invalidation masking. |
| `GET /v1/projections/evaluations/lineage` | `evaluations-lineage-v1` `[M3 Extension]` (separate evaluator store + versioned M3 extension) | `board_viewer`+ | `{...meta, methods: [{method_id, parent_method_ids[], rollback_eligible, recorded_at_ns}], access_records: [{access_id, method_id, holdout_id, exposure_kind, immutable_lock: true, recorded_at_ns}]}`. **Schema integrity rule:** because `trial_registry.py:538-570` checks exact `_REQUIRED_TABLES` equality on `offline-trial-registry-v1` and prohibits triggers, views, and indexes, `[M3 Extension]` `AccessRecord` and `parent_method_ids` MUST live either in a versioned `offline-trial-registry-v2` table set (with zero triggers/views/indexes) or in a dedicated companion evaluator SQLite store rather than mutating `offline-trial-registry-v1` ad hoc. |

## Functional and quality requirements

- **FR.** Support `GET /v1/meta` capability negotiation (`binding_state`, `principal_id`, `role`, `supported_projections`, `supported_commands`) and all `29` concrete `GET /v1/projections/*` paths with `ETag`, `If-None-Match` (`304`, rejecting `*`), `X-Sourdough-Event-Seq`, and single-`read_tx` + dual-`stat` anti-tear checks.
- **FR.** Upgrade `work_projection.py` to project `work_items`, `work_runs`, `work_conditions`, and `work_peer_requests` alongside legacy compatibility fields while preserving `work-timeline-v1` and `work-timeline-v2`.
- **NFR.** Preserve strict missing-data honesty (`None` / `null` -> `"Unavailable"` / `"No reading"`, never `0`), strict Desk opacity (zero `.desk/drafts/*` leakage), strict Flow 13 physical DB isolation and `BlindedTrial` stripping, and zero direct client SQLite reads.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Anti-tear & conditional `304` | Request `GET /v1/projections/work-timeline?contract_revision=work-timeline-v2`, receive `ETag` `"{revision}:{seq}"`, and replay with `If-None-Match`. Confirm `304` empty body with `X-Sourdough-Event-Seq`; repeat with `If-None-Match: *` and confirm non-`304` rejection. |
| Desk opacity & audience redaction | Stage 3 drafts in `.desk/drafts/` and publish 1 excerpt to `project_id`; query `/roster`, `/work-runs/{id}`, and `/collaboration` with and without `audience` access. Confirm zero draft bodies leak and revoked-audience excerpts return `asset_accessible = false` / `null`. |
| Missing coverage & Flow 13 blindness | Query `/paper-statements` with `CloseIssue("missing-cost-receipt")` and `/evaluations/grading` over an `(AB, BA)` mismatch. Confirm `company.pnl_minor === null`, `tie_noise` reported explicitly, and zero `arm`/`slot_id`/`run_id` fields in `BlindedTrial`. |

**Oracle:** Inspect `src/sourdough/ipc/server.py`, `src/sourdough/work_projection.py`, `src/sourdough/fund_returns.py`, `src/sourdough/window_costs.py`, `src/sourdough/accounting_close.py`, `src/sourdough/paper_funding.py`, `src/sourdough/agent_desk.py`, and `src/sourdough/trial_registry.py`.

## Operations and change review

Every new projection endpoint added to `CompanyDaemon` MUST register its `contract_revision` in `GET /v1/meta`, define its `ETag` digest canonicalization, and verify `audit_domain_write_ownership` / read-only `open_reader()` isolation.
