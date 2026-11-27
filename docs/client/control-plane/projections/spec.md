# Meta and authorized read projections

Design draft · Verification **UNRUN** · Responsible component: Control Plane Read Projections (`src/sourdough/ipc/server.py`, `src/sourdough/ipc/projections.py`, `src/sourdough/work_projection.py`, `src/sourdough/trading/plugin.py`, `src/sourdough/trading/projections.py`)

[Feature intent](../intent.md) · [Bridge and auth](../bridge-and-auth/spec.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Agents & Activity](../../inspect/office-floor-map/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [Flow observability](../../inspect/flow-observability/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Expose read-only company, governance, work, agent, financial, operations, evaluation, and domain-gated Trading projections over `CompanyDaemon`'s UDS Control Plane (`GET /v1/meta` and `14` `GET /v1/projections/*` endpoints across `15` contract revisions). Reads NEVER mutate operational state, NEVER execute SQLite writes, NEVER read `.desk/drafts/*` bodies, and NEVER compute synthetic client-side financial figures when domain coverage is missing.

Input contracts: [Transport, loopback bridge, and role authorization](../bridge-and-auth/spec.md) · [Authorized work timeline](../../../company/systems/operations/work-timeline/read/spec.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Flow observability](../../inspect/flow-observability/spec.md). These are public service contracts, not imports of client UI packages.

## Design decision and review

### 1. Reconciled `/v1/*` endpoint inventory (`18` total routes: `14` projection endpoints, `15` contract revisions)

`CompanyDaemon` (`src/sourdough/ipc/server.py:618-669`) serves **`18` total `/v1/*` routes**:

- **`1` capability/identity endpoint:** `GET /v1/meta`
- **`14` `GET /v1/projections/*` endpoints (`15` projection contract revisions):**
  - `1` Core work-timeline endpoint serving `2` contract revisions: `GET /v1/projections/work-timeline` (`work-timeline-v1`, `work-timeline-v2`)
  - `10` Core Client projections (`CORE_CLIENT_PROJECTIONS` in `src/sourdough/ipc/projections.py:46-57`): `agents-graph` (`agents-graph-v1`), `agents/{agent_id}` (`agent-detail-v1`), `messages` (`messages-v1`), `approvals` (`approvals-v1`), `finance` (`finance-v1`), `org` (`org-v1`), `restrictions` (`restrictions-v1`), `system-safety` (`system-safety-v1`), `system-evals` (`system-evals-v1`), `system-audit` (`system-audit-v1`)
  - `3` Trading domain projections (`TradingDomainPlugin.contribute_projections` in `src/sourdough/trading/plugin.py:783-802`, `requires_domain="trading"`): `trading-desk` (`trading-desk-v1`), `orders/{order_id}` (`order-trace-v1`), `strategies` (`strategies-v1`)
- **`1` event stream endpoint:** `GET /v1/events`
- **`2` command/receipt endpoints:** `POST /v1/commands` and `GET /v1/commands/{command_id}?operation=<command_type>`

`ALL_CLIENT_PROJECTIONS` (`13` = `10` Core + `3` Trading) plus `work-timeline-v1` and `work-timeline-v2` equals `15` projection contract revisions. Retired legacy trading projection endpoints are removed completely.

### 2. Headers, ETag caching, common envelope metadata, and anti-tear snapshot invariant

- **Request headers**: `If-None-Match` accepts exact quoted or weak ETags; wildcard `*` never matches and returns `200`, never `304`.
- **Response headers**: `Connection: close`, `Cache-Control: no-store`, `X-Sourdough-Company-Id: <company_id>`, `X-Sourdough-Event-Seq: <int>`, and `ETag` (`"{digest}:{event_seq}"` on `ALL_CLIENT_PROJECTIONS`; `"{revision}:{seq}"` on `work-timeline-v2`; `"{revision}"` on `work-timeline-v1`). `304 Not Modified` returns an empty body with headers.
- **Common JSON envelope fields**: Client projections (`ALL_CLIENT_PROJECTIONS`) return `{projection, company_id, as_of_ns, event_seq}` (with `charter_revision` also included on `messages-v1`, `approvals-v1`, `restrictions-v1`, `system-safety-v1`, and `trading-desk-v1`); `work-timeline` returns `{company_id, contract_revision, revision, as_of_ns}` (`+ event_seq, store_key` on `v2`); and `GET /v1/meta` returns `{store_key, service_incarnation_id, ...}`.
- **Single-`read_tx` + dual-`stat` anti-tear invariant (`work_projection.py`)**: Opens `open_reader(store)` (`mode=ro`, `PRAGMA query_only=ON`), verifies `initial_stat` vs `post_tx_stat` (`st_dev`, `st_ino`, `st_uid`), and fails closed if the store inode swapped mid-read.
- **RBAC & Domain Gating**: Under `bootstrap_locked`, operational projections fail closed (`403`). On core-only stores (`"trading"` not in `active_domains`), the 3 Trading domain projections fail closed with `404` before touching Trading tables, and the 10 Core projections emit Core OS data only without invoking Trading enrichers (`_apply_core_enrichers`).

### 3. `GET /v1/meta` capability and authority negotiation (`ipc/server.py:821-893`)

`GET /v1/meta` executes a single `read_tx` and returns:

| Field | Type & Semantics |
| --- | --- |
| `control_plane_version` | `"control-plane-v1"` |
| `company_id` | Store company identifier |
| `charter_revision` | Current company charter revision |
| `founder_id` | Designated CEO / founder `agent_id` (or `null`) |
| `store_key` | `"{st_dev}:{st_ino}"` |
| `service_incarnation_id` | Per-daemon-boot UUID |
| `schema_version` | `1` (`PRAGMA user_version`) |
| `active_domains` | Sorted installed domains (`["core"]` or `["core", "trading"]`) |
| `daemon_pid` | OS PID of `CompanyDaemon` |
| `event_seq` | Latest `audit_event.event_seq` for `company_id` |
| `board_access` | `{bootstrap_locked, role, principal_id, binding_revision, bootstrap_challenge?}` |
| `pending_approvals_count` | Count of open `decision_question` rows |
| `active_restrictions_count` | Count of active `restriction_cause` rows |
| `supported_projections` | Active projection revision list (`12` on core-only [`work-timeline` `v1`/`v2` + `10` Core]; `15` when `"trading"` is installed) |
| `supported_commands` | Active command registry map (`8` Core commands, plus `4` Trading plugin commands when `"trading"` is installed) |

*(Note: Legacy `paper_risk_suspended` and `paper_risk_effective_revision` fields are removed from `/v1/meta`.)*

### 4. All `14` implemented `GET /v1/projections/*` contracts

#### A. Work Timeline (`1` endpoint, `2` contract revisions)

| Endpoint | Contract Revisions | Visibility & Query Params | Schema & Domain Enrichment |
| --- | --- | --- | --- |
| `GET /v1/projections/work-timeline` | `work-timeline-v1`, `work-timeline-v2` | `board_viewer`+ (`?contract_revision=work-timeline-v1\|work-timeline-v2`) | Core `work_projection.py` emits `company_id`, `access`, `duties` (with `duty_id`, `owner_id`, `state`, `due_at`, `run_id`, and nested `decision` from `work_review_decision`), `domains`, `contract_revision`, `revision`, `as_of_ns` (`+ event_seq`, `store_key` on `v2`), and delegates `domains["trading"]` enrichment to `TradingDomainPlugin.contribute_work_timeline` (`trading/plugin.py:659-671`, emitting `paper` & `risk_control` only when `"trading"` is installed). |

#### B. Core Company OS Projections (`10` endpoints, `CORE_CLIENT_PROJECTIONS`, enriched when `"trading"` installed)

| Endpoint | Contract Revision | Primary Source & Core vs Trading Enrichment |
| --- | --- | --- |
| `GET /v1/projections/agents-graph` | `agents-graph-v1` | Core: `default_view_mode` (`"graph"`/`"list"`), `nodes[]` (including opaque `private_draft_count` — zero draft bodies, `sandbox_authorized: false`, `sandbox_activity: null`), and `edges[]`. Trading hook updates `role_label`, sets `sandbox_authorized`, and attaches `sandbox_activity` strictly when the Agent holds an active trading standing grant and active sandbox state. |
| `GET /v1/projections/agents/{agent_id}` | `agent-detail-v1` | Core: `profile` (with `active_run`), `sandbox_authorized: false`, `agent_sandbox: null`, `desk_workspace` (`execution_profile`, `desk_manifest_revision`, `private_draft_count` — opaque count only, `private_draft_notice`, `published_count`, `work_run_count`), `authority_budget` (`grant_id`, `grant_scope`, `can_hire`, `hiring_slots`, `hiring_turns`, `monthly_budget_minor`, `allocated_turns`, `prohibitions`), and `tabs` (`work_activity`, `work_items`, `conditions`, `interactions_and_talks`, `published_reports`, `peer_requests`, `handoffs`). Trading hook attaches `agent_sandbox` (`trading_book_*` for `environment = 'agent_sandbox'`), sets `sandbox_authorized`, updates `profile.role_label`, and appends live-money prohibition. |
| `GET /v1/projections/messages` | `messages-v1` | Core: `charter_revision`, writable 1:1 `ceo_thread` (`channel_id: "ceo-direct"`, `ceo_agent_id`, `ceo_display_name`, `ceo_role_label`, `ceo_avatar_variant`, `writable`, `advisory_notice`, `messages[]` with `context_ref: {kind, id, label} \| null` from `ceo_message`), and read-only `internal_channels` (`project_message`). `messages-v1` has **no** server-side Trading enricher hook (`_apply_core_enrichers` is not invoked); `MessagesView` (`apps/client/src/views/MessagesView.tsx:146-158`) resolves `"trading_order"` `context_ref` links client-side via `GET /v1/projections/orders/{order_id}` (`order-trace-v1`) when `"trading"` is installed. |
| `GET /v1/projections/approvals` | `approvals-v1` | Core: `charter_revision`, `pending_count`, `pending[]`, `history[]` (open & historical `decision_question` rows: `question_id`, `kind`, `title`, `summary`, `impact`, `options`, `revision`, `payload_hash`, `state`, `respond_by_ns`, `application_status` including `'failed'`, `delivery_status`), and `promote_candidates: []`. Trading hook populates `promote_candidates` (`trading_challenger_trial` rows with `verdict = 'PROMOTE'`) when `"trading"` is installed. |
| `GET /v1/projections/finance` | `finance-v1` | Core: `trading_enabled: false`, `account_balances`, `journals`, `accounting_closes`, `open_close_issues`, `funding_settlements: []`, and `retained_statements: []`. Trading hook sets `trading_enabled: true` and populates `funding_settlements` (`trading_funding_settlement`) and `retained_statements` (`trading_statement`). |
| `GET /v1/projections/org` | `org-v1` | Core: `roster` (including `private_draft_count`), `publications` (`shared_publication` assets), and read-only `delegated_hr_custody` (`board_can_retire: false`, `policy_notice: "Managed by CEO · Board view is read-only."`, `archived_employees`). |
| `GET /v1/projections/restrictions` | `restrictions-v1` | Core: `charter_revision`, `active_causes`, `history` from `restriction_cause`, and `trading_risk: null`. Trading hook populates `trading_risk` (`paper_suspended`, `live_suspended`, `effective_revision`, `unresolved_orders_count`, `unresolved_external_actions_count`, `settled_company_ok`, `server_freshness_note` for `execution:all` / `execution:paper` / `execution:live`). |
| `GET /v1/projections/system-safety` | `system-safety-v1` | Core: `charter_revision`, `checkpoints` (`sourdough-checkpoint-v2`), `restore_receipts`, `store_retired`, 5-step `safe_custody_hold` readiness diagnostics (`data_cut`, `sole_writer`, `authority`, `reconciliation`, `next_actions`), and `active_restrictions`. Trading hook updates `safe_custody_hold` step labels (`reconciliation` -> `"External trading reconciliation complete"`, `next_actions` -> `"Eligible for normal trading release"`) and reflects `execution:*` / `trading_risk_pause` pauses in `next_actions.status` (attaches no `sender_lease` field). |
| `GET /v1/projections/system-evals` | `system-evals-v1` | Core: `qualifications` (`review_qualification` `ThreeStageVerdict`), `activations` (`review_tool_activation`, including `blast_radius_descendants` and `fallback_candidate_id`), and `offline_evaluator` (`{connected, isolated_notice, suites: [{revision, sealed_at_ns, observation_count, grade_count, pairwise_grade_count}]}` read from the separate read-only Flow 13 evaluator SQLite store). |
| `GET /v1/projections/system-audit` | `system-audit-v1` | Core: `events` (`event_seq`, `domain`, `entity_type`, `entity_id`, `event_type`, `recorded_at_ns`), `conditions`, `active_fences`, and `board_bindings` (`principal_id`, `role_name`, `scope_ref`, `binding_revision`, `active`; daemon telemetry is served separately by `GET /v1/meta`). |

#### C. Trading Domain Projections (`3` endpoints, gated on `requires_domain="trading"`, `404` when absent)

| Endpoint | Contract Revision | Query Params & Canonical Schema Rules |
| --- | --- | --- |
| `GET /v1/projections/trading-desk` | `trading-desk-v1` | Query: `?environment=live\|paper&book_id=...` (`environment` restricted strictly to `"live"` or `"paper"`, `400` otherwise). Returns `{projection, company_id, charter_revision, as_of_ns, event_seq, active_domains, trading_enabled: true, environment, selected_book_id, selected_book, books[], kpis, cumulative_return_series, shadow_benchmark, open_positions, execution_tape, pnl_reconciliation_bridge, exposure_cap, close_count, open_close_issues, risk_gate: {suspended, effective_revision, active_restriction_count, active_restrictions[]}, governance: {company_live, live_candidates[], qualification_receipts[], promotion_history[], migration_history[], company_paper_soak_ready, paper_soak_ready, paper_books_count, soak_ready_books_count, current_core_digest}}`. Reads unified `execution_environment_binding` + `trading_book_{order,fill,position,valuation}` without commingling `paper` and `live` capital. Missing close/cost figures serialize as `null` (`"Unavailable"` / `"No reading"`, never `$0.00`). |
| `GET /v1/projections/orders/{order_id}` | `order-trace-v1` | Queries `trading_book_order`, `trading_book_fill`, `trading_fee_delivery`, `trading_book_position`, `execution_environment_binding`, `work_runs`, `external_actions`, and `shared_publication` across `paper`, `live`, and `agent_sandbox`, returning `order`, `causal_steps`, and `related_publications`. Emits environment-aware `order.back_link`: `"/trading/live"` (live), `"/trading/paper"` (paper), or `f"/agents/{actor_id}"` (`agent_sandbox`). |
| `GET /v1/projections/strategies` | `strategies-v1` | Returns `trading_enabled`, `active_deployment` (object or `null`), `deployments[]` (`trading_strategy_deployment`), `releases[]` (`trading_strategy_release`, each containing `pit_manifest_id`), `trials[]` (`trading_challenger_trial`, `PROMOTE \| REJECT \| INCONCLUSIVE`), and `adapter` (active `company_paper` `execution_environment_binding` summary, or `null`). |

## Functional and quality requirements

- **FR.** Serve `GET /v1/meta` and all `14` `GET /v1/projections/*` endpoints (`15` contract revisions across `18` total `/v1/*` routes) with `ETag`, `If-None-Match` (`304`, ignoring `*`), `X-Sourdough-Event-Seq`, and single-`read_tx` + dual-`stat` anti-tear checks.
- **FR.** Isolate Core Company OS projections (`CORE_CLIENT_PROJECTIONS`) from Trading (`TradingDomainPlugin`): Core projections emit neutral Core OS fields on core-only stores, while Trading projections fail closed (`404`) when `"trading"` is absent.
- **NFR.** Preserve strict missing-data honesty (`null` -> `"Unavailable"` / `"No reading"`, never `0`), strict Desk opacity (zero `.desk/drafts/*` leakage), read-only `/reports` custody (`board_can_retire: false`), and zero direct client SQLite reads.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Anti-tear, `304` & domain gating | Request `GET /v1/projections/work-timeline?contract_revision=work-timeline-v2`, verify `ETag` `"{revision}:{seq}"`, replay `If-None-Match` (`304`), verify `*` returns `200` (never `304`). Query `/v1/projections/trading-desk?environment=live` on a core-only store -> verify `404` before any table access. |
| Desk opacity & missing coverage | Stage drafts in `.desk/drafts/` and query `agents-graph` and `agents/{id}` -> verify only `private_draft_count` appears. Query `finance` / `trading-desk` with an open `CloseIssue` -> verify affected net P&L/TWR serialize as `null` (`"Unavailable"`). |
| `order-trace-v1` 3-tier back-links | Query `/v1/projections/orders/{id}` for `live`, `paper`, and `agent_sandbox` orders -> verify `trading_book_*` lineage and `back_link` (`/trading/live`, `/trading/paper`, `/agents/{actor_id}`). |

**Oracle:** Inspect `src/sourdough/ipc/server.py`, `src/sourdough/ipc/projections.py`, `src/sourdough/work_projection.py`, `src/sourdough/trading/plugin.py`, and `src/sourdough/trading/projections.py`.

## Operations and change review

Every projection registered in `CompanyDaemon` MUST register its `contract_revision` in `GET /v1/meta`, bind domain-specific projections through `DomainPlugin` with `requires_domain`, and execute exclusively on `open_reader()` (`mode=ro`, `PRAGMA query_only=ON`).
