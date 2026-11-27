# Observe all company and trading flows

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../screens/spec.md) · [Trading Desk & Strategies](../investment-panel/spec.md) · [Agents & Activity](../office-floor-map/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Map all thirteen Sourdough operational flows (`01`–`13`) plus Core/Trading domain separation onto the `17` canonical Modular IA routes, Core Company OS vs `src/sourdough/trading/` backend owners, the `14` implemented projection endpoints (`15` contract revisions), the `12` supported `POST /v1/commands` operations (`8` Core + `4` Trading plugin), `audit_event` + `notify_commit()` streams, Desk and blindness opacity rules, and honest `Unavailable` / `Missing` states.

Input contracts: [Base presentation](../present/spec.md) · [Trading Desk & Strategies](../investment-panel/spec.md) · [Agents & Activity](../office-floor-map/spec.md) · [Client screens and navigation](../screens/spec.md) · [Work packages](../../../engineering/work-packages.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. Live event streaming and projection refresh contract

- **Transport**: The client consumes `GET /v1/events?after_seq=<n>&limit=100&wait_ms=5000` (`application/x-ndjson`, 7-field redacted envelopes `{event_seq, company_id, domain, entity_type, entity_id, event_type, recorded_at_ns}`) via `fetch()` streaming (never `EventSource`), paired with conditional `GET /v1/projections/{name}` using `If-None-Match` (`ETag`) and `X-Sourdough-Event-Seq` for tear-free snapshot alignment.
- **Unified `ResidentCompanyService` wakeups**: `ResidentCompanyService` coordinates `CompanyRuntime` and `CompanyDaemon` under one `WriterLease` + `daemon.write_lock`, invoking `daemon.notify_commit()` after every SQLite commit so `/v1/events` pushes updates immediately across both Core Company OS and Trading domain mutations. Flow 13 (`offline-trial-registry-v1`) remains in a physically separate read-only evaluator SQLite store surfaced via `/v1/projections/system-evals`.

### 2. Complete 13-flow observability matrix (Modular IA: Core OS + Dynamic Trading)

| Flow | Title | Canonical Modular IA Route(s) | Authoritative Backend Source | Projection & Command Surface | Live Events, Lineage, Opacity & Honesty Rules |
| --- | --- | --- | --- | --- | --- |
| **01** | Company founding | `/bootstrap`, `/agents`, `/messages`, `/audit` | `company.py:found()` (`core` pack only), `board_control.py`, `08_board_client.sql` | `GET /v1/meta`, `messages-v1`, `system-audit-v1`, `register_owner`, `send_ceo_message` | Core-only store installs `'core'` only (`/` -> `/agents`). Event: `company.founded`, `ceo.*`. Opacity: owner secrets stay outside DOM/logs; non-CEO 1:1 chat absent. |
| **02** | First Agent duty | `/agents`, `/agents/:agentId` (`Work Activity`), `/audit` | `work_kernel.py`, `agent_desk.py` (`.desk/drafts/`), `harness.py` (`.desk/logs/`), `work_projection.py` | `work-timeline` (`v1`/`v2`), `agents-graph-v1`, `agent-detail-v1` | Event: `work.run_*`. Lineage: `Duty -> AgentSession -> work_run`. **Desk opacity:** `.desk/drafts/*` bodies hidden; only `private_draft_count` renders. |
| **03** | Trading order & books | `/` / `/trading/live`, `/trading/paper`, `/finance` | `storage/schema/v1/trading/01_execution_tiers_and_books.sql` (`execution_environment_binding`, `trading_book_*`), `trading/{accounts,ledger}.py` | `trading-desk-v1` (`?environment=live\|paper`), `finance-v1` (domain-gated) | Event: `domain="trading"` order/fill/fee `audit_event`s. Lineage: `execution_environment_binding -> trading_book_order -> trading_book_fill -> trading_book_position`. Composite `(binding_id, environment)` FK isolates tiers. |
| **04** | Causal order trace & agent sandbox | `/orders/:orderId`, `/agents/:agentId` (`Trading Sandbox`) | `trading/{sandbox_tools,projections}.py`, `trading_book_{order,fill,position}`, `work_runs`, `external_actions` | `order-trace-v1`, `agent-detail-v1`, `agents-graph-v1` | Lineage: `Duty -> work_run -> trading_book_order -> trading_book_fill -> trading_book_position`. Missing nodes render `"Unavailable"`; `order-trace-v1` emits environment-aware `back_link` (`/trading/live`, `/trading/paper`, `/agents/:actor_id`). |
| **05** | Continuing operation & reconciliation | `/agents/:agentId`, `/audit`, `/trading/paper` | `work_items`, `work_runs`, `work_conditions`, `trading/nautilus_runtime.py`, `work_projection.py` | `work-timeline` (`v1`/`v2`), `agent-detail-v1`, `system-audit-v1`, `interrupt_run`, `cancel_work` | Event: `work.run_fenced`, `work.self_authored_event_suppressed`. Honesty: `due_at_ns === null` renders `"Not scheduled"`; transient disconnect preserves Data State behind stale banner. |
| **06** | Hired-Agent continuity | `/approvals` (`:questionId`), `/reports`, `/agents` | `staffing.py`, `onboarding.py` (`hire_onboarding`), `board_control.py` | `approvals-v1`, `org-v1`, `agents-graph-v1`, `submit_decision_answer` | Lineage: `StaffingNeed -> decision_question -> hire_receipt -> hire_onboarding`. Honesty: `application_status` (`'applied'` \| `'failed'`) stays separate from `hire_onboarding.state`. |
| **07** | 3-Tier execution environments & live promotion | `/trading/live`, `/trading/paper`, `/approvals`, `/audit?tab=daemon` | `trading/{domain,migration,venue,nautilus_paper,sender_lease,venue_coordinator}.py`, `trading_live_migration_record`, `venue_qualification_receipt`, `environment_promotion_record` | `trading-desk-v1`, `approvals-v1`, `system-safety-v1`, `promote_execution_environment`, `demote_live_environment` | 3-tier isolation (`agent_sandbox`, `company_paper`, Board-gated `company_live`); `positions_copied = 0`; `promote_live` & `Shadow Benchmark`; `sender_lease` (`.writer-lock`) + `IndependentProcessGuard`. |
| **08** | Initial company-net return | `/` / `/trading/live`, `/trading/paper`, `/finance` | `trading/{fund_returns,window_costs,funding,valuation,window_statement}.py`, `accounting_close.py` | `trading-desk-v1` (`kpis`, `pnl_reconciliation_bridge`), `finance-v1` (`retained_statements`, `funding_settlements`) | **Two-Layer P&L**: missing `CloseIssue` / funding renders `"Unavailable"` / `"No reading"`, never `$0.00`; `PortfolioChart` frames on `0%`/`$0` baseline with `null`-gap breaks. |
| **09** | Candidate comparison & strategies | `/trading/strategies` | `trading/strategy_comparison.py`, `trading/{pit_catalog_manifest,strategy_release,challenger_trial,strategy_deployment}` | `strategies-v1`, `activate_strategy_release`, `rollback_strategy_deployment` | `INCONCLUSIVE`/`REJECT` block activation; non-self-activation; forward rollback preserves open `trading_book_*` rows and costs. |
| **10** | Employee lifecycle & custody | `/agents`, `/agents/:agentId`, `/messages`, `/reports` | `conversations.py`, `work_handoff.py`, `company_assets.py` (`shared_publication`), `retirement.py` (`TERMINATION_SCOPE = "retire-agent"`) | `agents-graph-v1`, `agent-detail-v1`, `messages-v1`, `org-v1` (`delegated_hr_custody`) | Opacity: `.desk/drafts/*` hidden; `project_message` read-only (`"Discussion only"`); `/reports` displays `"Managed by CEO · Board view is read-only"` with zero Board `retire_employee` writes. |
| **11** | Client, controls & restore | `/controls`, `/backups`, `/trading/*`, `/orders/:orderId` | `ipc/server.py`, `trading/risk.py` (`TradingRestrictionGuard`, `15s` quote check), `storage/checkpoint.py`, `company_restore.py` (`RestoreDomainGuard`) | `restrictions-v1`, `system-safety-v1`, `pause_company`, `resume_company`, `cut_checkpoint` | Scoped `restriction_cause` (`execution:*` gated on `"trading"`, `<= 15s` quote cut on resume); `cancel != flat`; 5-step **"Safe Custody Hold"** on `/backups`; generic `RestoreDomainGuard` fail-closed check. |
| **12** | Company changes & review tools | `/audit?tab=evals` | `evidence_review.py` (`ThreeStageVerdict` `pass^3`), `review_qualification_store.py`, `review_tool_activation.py` | `system-evals-v1` | Self-evaluation blocked; `blast_radius_descendants` DAG preview metadata surfaced read-only in `/audit?tab=evals`. |
| **13** | Judgment & offline learning | `/audit?tab=evals` | `trial_registry.py` (`offline-trial-registry-v1`, `BlindedTrial`, `(AB, BA)` swap, `RsiTransferSummary`), `trial_execution.py` | `system-evals-v1` (`qualifications`, `activations`, `offline_evaluator`) | Physically isolated evaluator SQLite DB (`offline_evaluator.suites` summary counts only); `BlindedTrial` strips `arm`, `slot_id`, `run_id`; `(AB, BA)` mismatch recorded as `tie_noise`; sealed `trial_suite` definitions remain immutable. |

### 3. 3-tier execution boundary & Core-only isolation

All trading observability and operator controls across Flows `03`, `04`, `07`, `08`, `09`, and `11` are isolated inside `TradingDomainPlugin` (`domainKey: "trading"`):
- **Core-only stores (`installed_domains = {'core'}`):** `/trading/*` and `/orders/:orderId` are unmounted, `/` redirects to `/agents`, Trading projections return `404`, and Trading commands / `execution:*` restrictions return `422`.
- **Trading-enabled stores (`installed_domains = {'core', 'trading'}`):** `execution_environment_binding` + `trading_book_*` isolate `agent_sandbox`, `paper`, and Board-promoted `live` via composite `(binding_id, environment)` foreign keys with `positions_copied = 0`.

## Functional and quality requirements

- **FR.** Map every flow `01`–`13` to the `17` canonical Modular IA routes, Core vs `src/sourdough/trading/` owners, `14` implemented projection endpoints (`15` contract revisions), `12` implemented commands, `audit_event` streams, opacity rules, and missing-data representations.
- **NFR.** Keep Flow 13 evaluator data physically isolated from operational company stores, enforce `.desk/drafts/*` opacity across all agent/org views, exclude Board `retire_employee` writes, and gate all Trading projections/commands cleanly on `"trading"`.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Full-flow coverage | Audit all 13 rows of the matrix against `docs/engineering/work-packages.md`, `src/sourdough/ipc/`, `src/sourdough/trading/`, and `apps/client/src/`. Confirm zero unassigned flows and zero stale legacy route or table names. |
| Opacity, blindness & delegated HR | Inspect `/agents`, `/agents/:id`, `/reports`, and `/audit?tab=evals` during active Desk drafts, CEO-executed retirement, and offline evaluations. Confirm zero draft body leakage, zero Board `retire_employee` write paths, and zero `arm`/`slot_id`/`run_id` leakage. |
| Core-only vs Trading 3-tier boundary | Verify core-only stores return `404`/`422` on Trading projections/commands and land on `/agents`, while Trading stores isolate `agent_sandbox`, `paper`, and `live` across `trading-desk-v1`, `order-trace-v1`, and `agent-detail-v1`. |

**Oracle:** Cross-verify projection payloads, `/v1/events` stream records, and rendered screens against [Client screens and navigation](../screens/spec.md) and [Usage scenarios](../../scenarios.md).

## Operations and change review

Any new flow projection or command added to Core Company OS or a `DomainPlugin` MUST update the matrix above alongside [Meta and authorized read projections](../../control-plane/projections/spec.md) and [Event stream, runtime wakeups, and typed commands](../../control-plane/commands-and-events/spec.md).
