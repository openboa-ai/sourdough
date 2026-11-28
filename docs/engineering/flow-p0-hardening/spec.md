# P0 Privacy, Projection Honesty & Hardening Test Suite Modularization

Status: Scoped design specification (`flow-p0-hardening`, base `main@96425ce4`)

## 1. Intent & Product Outcome

Eliminate four concrete live privacy, projection-accuracy, and data-honesty defects discovered across the IPC control plane, read projections, and Trading Desk client, and break up the `13,215`-LOC monolithic hardening regression file into domain-scoped test modules so every subsystem can be verified and maintained independently:

1. **`one_agent` Redaction in `work_runs.outcome` (`src/sourdough/ipc/projections.py`):** Extend `_redact_run_outcome_for_board` to opaque-map `one_agent`-scoped method IDs and delta IDs inside `causal_method_trace.{available, retrieved, invoked}`, `proposals[*].{method_id, delta_id}`, and `patterns[*].method_id`, using `load_one_agent_method_sets(db, company_id)` inside `read_agent_detail_projection` while keeping `company`-scoped method identifiers readable.
2. **`/v1/events` Audit Stream Parity (`src/sourdough/ipc/server.py`, `src/sourdough/ipc/projections.py`):** Introduce a single shared helper (`redact_audit_entity_id_for_board`) used by both `read_system_audit_projection` and `CompanyDaemon._read_events_after` so `method_delta`, `method_activation`, and `method_qualification` audit events streamed over `/v1/events` never disclose raw `one_agent` method or delta identifiers.
3. **5-State Work Handoff Projections & UI (`src/sourdough/ipc/projections.py`, `apps/client/src/api/types.ts`, `apps/client/src/views/AgentDetailView.tsx`):** Project the authoritative 5-state `work_handoff.state` (`offered`, `accepted`, `declined`, `expired`, `withdrawn`) along with `expires_at` and `successor_work_item_id` in `read_agents_graph_projection` (`active = state == "offered"`) and `read_agent_detail_projection` (`accepted = state == "accepted"`), and render the true state in `AgentDetailView` so declined, expired, and withdrawn handoffs are never presented as active or waiting.
4. **No Filesystem I/O Inside SQLite `read_tx` (`src/sourdough/ipc/projections.py`):** Execute `_count_private_drafts` after closing the SQLite `read_tx` snapshot in `read_agents_graph_projection`, `read_agent_detail_projection`, and `read_org_projection`, and narrow `_count_private_drafts` exception handling from bare `Exception` to `(OSError, DutyRefused)`.
5. **Trading Desk Data Honesty (`apps/client/src/views/trading/TradingDeskView.tsx`):** Render `Unavailable` instead of coercing `null` `port.return_pct` to `0.00%`.
6. **Hardening Test Modularization (`tests/unit/hardening/`):** Split all `101` tests from `tests/unit/test_m5_tier5_hardening.py` into 7 focused domain modules with shared fixtures in `_fixtures.py` and `__init__.py` for `unittest` discovery, plus dedicated regression tests for items 1–4.

## 2. Authority & Data Boundaries

- **Board Privacy Invariant:** `one_agent` method identifiers, bodies, lineage, and private Desk draft filenames never appear in Board HTTP/UDS projections (`/v1/projections/*`) or event streams (`/v1/events`).
- **Read-Only Projections:** All IPC projections remain strictly read-only; filesystem draft counting occurs outside SQLite `read_tx` without opening or reading any draft file contents.
- **Zero Core/Domain Coupling:** Changes stay within existing IPC projection/server modules, Client UI views/types, and test suites; no schema or ownership boundaries are widened.

## 3. Lifecycle Failure Modes & Invariants

- **Mixed-scope method IDs:** `load_one_agent_method_sets` distinguishes `(method_id, revision_sha256)` pairs from `one_agent`-only method IDs so `company`-scoped entries remain visible while `one_agent` entries are deterministically opaque-mapped.
- **Unreachable private Desk directories:** `_count_private_drafts` handles `OSError` cleanly by returning `0` without swallowing programming errors or holding SQLite locks.
- **Missing valuation telemetry:** `TradingDeskView` renders `"Unavailable"` when `return_pct === null`, preserving data-honesty invariants.
- **Test discovery parity:** `tests/unit/hardening/__init__.py` guarantees `unittest.TestLoader().discover("tests/unit", pattern="test_*.py")` collects every split module with zero dropped tests (`>= 530` unit tests total).

## 4. Acceptance Evidence Plan

- **Unit suite (`tests/unit/hardening/`):** All 101 migrated hardening tests plus 4 new PR-1 regression tests (`test_p0_agent_detail_redacts_one_agent_ids_in_run_outcome_trace_proposals_and_patterns`, `test_p0_events_stream_redacts_one_agent_method_entity_ids`, `test_p0_handoff_five_states_projected_in_agents_graph_and_agent_detail`, `test_p0_private_draft_counting_runs_outside_sqlite_read_tx`) pass via `python -I .github/scripts/run_required_tests.py tests/unit`.
- **Connected Flow E2E (`tests/flows/flow-p0-hardening/test_flow.py`):** Boots `CompanyDaemon` + `ClientWebBridge`, seeds a `one_agent` method + `company` method + run outcome trace/proposals/patterns + 5-state work handoffs + private drafts, and verifies `/api/v1/projections/agents-graph`, `/api/v1/projections/agents/<id>`, `/api/v1/projections/system-audit`, and `/api/v1/events` end-to-end.
- **Client & Hygiene Gates:** `npm --prefix apps/client run typecheck && build && check:yogurt`, `ruff==0.14.10`, `reference_guard.py staged`, and `gitleaks --full-history`.
