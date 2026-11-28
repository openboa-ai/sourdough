# Flow Spec: P0 Kernel & Backend Modularization (`flow-p0-kernel-modularization`)

## 1. Intent & Scope

Decompose Sourdough's remaining oversized backend modules into cohesive, acyclic domain
subpackages, eliminate high-duplication transaction/command code, enforce `WriterLease`
fencing across all Board Control writes, and consolidate legacy `DutyClaim` branching behind
a shared `ExecutionClaim` protocol — while preserving 100% behavioral, schema, redaction,
and import compatibility across all existing flows and callers.

### In Scope
1. **Work Kernel Package (`src/sourdough/runtime/kernel/`):**
   - Split `runtime/work_kernel.py` into 8 acyclic modules (`types`, `responsibilities`,
     `peer_auth`, `items`, `conditions`, `runs`, `decisions`, `peer_requests`) behind a
     100%-compatible `runtime/work_kernel.py` facade.
   - Decompose `evaluate_conditions` into per-trigger evaluator functions and
     `_validate_core_evidence_entry` into an evidence-kind dispatch table.
2. **Method Learning Package (`src/sourdough/learning/`):**
   - Split `method_learning.py` into 6 acyclic modules (`models`, `redaction`, `lineage`,
     `lifecycle`, `projections`, `dreaming`) behind a 100%-compatible `method_learning.py`
     facade.
3. **Trial Runner Package (`src/sourdough/trials/`):**
   - Split `trial_runner.py` into 5 acyclic modules (`sandbox_workspace`, `evidence_grading`,
     `slot_executor`, `blind_judge`, `recursive_cycle`) behind a 100%-compatible
     `trial_runner.py` facade.
   - Decompose `run_recursive_learning_cycle` into explicit stage helpers and a top-level
     Gen-2 arm execution helper.
4. **Core Deduplication & Write-Lease Hardening:**
   - `trading/ledger.py`: extract shared `_apply_fill_to_position_in_tx` and
     `_execute_book_order_in_tx` kernels for sandbox/paper/live ledger accounting.
   - `board_control.py`: route all writer connections through `write_uow(writer)` (enforcing
     `lease=writer` at every write boundary) and extract `_run_idempotent_board_command` for
     idempotency/revision/audit envelope parity across board commands.
   - `storage/migration.py`: decompose `_migrate_trading_tables_impl` into ordered, named
     phase helpers while keeping single-module DDL ownership in `SCHEMA_MIGRATION_MODULES`.
   - `ipc/projections/` + `ipc/redaction.py`: extract board redaction helpers and split
     read projections by domain (`agents`, `comms`, `governance`, `system`) behind the
     `sourdough.ipc.projections` package interface.
5. **Legacy `DutyClaim` Phase 1–2 Consolidation:**
   - Define `ExecutionClaim` Protocol in `duty_run.py`; isolate legacy `DutyClaim` and
     `start_run` in `legacy_duty.py` (re-exported from `duty_run.py` for compatibility).
   - Remove direct `DutyClaim` type branching in core modules that only depend on
     `ExecutionClaim` / `resolve_run`, and separate trading policy constants cleanly in
     `trading/duty.py`.
6. **SQLite Write Ownership Registry (`src/sourdough/storage/ownership.py`):**
   - Register each newly extracted subpackage module's exact SQLite table write ownership so
     static AST ownership checks (`test_storage_ownership.py`) remain strictly enforced.

### Out of Scope
- Any SQLite schema DDL changes (all tables remain 100% `STRICT` and schema-identical).
- Breaking removal of `DutyClaim` or `start_run` (Flow-01..10 compatibility is retained).
- Frontend client refactoring (covered separately by `flow-p0-client-modularization`).
- New UI projections or external connectors (covered separately by P1/P2 flows).

## 2. Authority, Privacy & Data Boundaries

- **Single-Writer Fencing:** Every write in `board_control.py` must execute inside
  `write_uow(writer)` with `lease=writer`, refusing stale or released writer leases before
  mutating `company.sqlite`.
- **Single-Owner SQLite Table Writes:** Each split module under `runtime/kernel/`,
  `learning/`, and `legacy_duty.py` may mutate only the tables explicitly authorized in
  `storage/ownership.py`. Projection and redaction modules under `ipc/` remain strictly
  read-only.
- **Board Privacy & Redaction Parity:** Extracting `ipc/redaction.py` must preserve all
  `one_agent` method opaque hashing, `work_runs.outcome` trace/proposal/pattern redaction,
  `/v1/events` `entity_id` parity, and off-transaction `_count_private_drafts` isolation.
- **Core / Domain Separation:** Core packages (`runtime/kernel/`, `learning/`, `trials/`,
  `ipc/`, `legacy_duty.py`) must maintain zero imports from `sourdough.trading.*`.

## 3. Allowed & Prohibited Behavior

- **Allowed:** Importing any public or compatibility-preserved private symbol from
  `sourdough.runtime.work_kernel`, `sourdough.method_learning`, `sourdough.trial_runner`,
  `sourdough.ipc.projections`, or `sourdough.duty_run`, as well as directly from the new
  subpackages.
- **Prohibited:**
  - Opening `open_writer(writer.store)` without `lease=writer` in `board_control.py`.
  - Introducing circular imports inside `runtime/kernel/`, `learning/`, `trials/`, or
    `ipc/projections/`.
  - Weakening `WorkRunClaim` fencing, idempotency conflict detection, or `one_agent`
    redaction during refactoring.

## 4. Lifecycle Failures & Compatibility

- **Stale / Lost Writer Lease:** `board_control.py` commands executing through `write_uow`
  fail closed with `LeaseLost` / `StorageRefused` without partial writes.
- **Idempotency Replay & Conflict:** `_run_idempotent_board_command` replays identical
  `(command_id, payload_sha256)` receipts deterministically and raises `BoardControlConflict`
  on payload digest mismatch or stale preconditions.
- **Legacy Flow Compatibility:** All existing `DutyClaim` callers (including historical
  `flow-01`..`flow-10` suites) continue executing seamlessly through `legacy_duty.py`
  re-exports and `ExecutionClaim`.

## 5. Acceptance Evidence

1. **Deterministic Unit & Ownership Tests:**
   - Full `tests/unit` suite passes, including `test_storage_ownership.py` verifying every
     split module's exact SQL table ownership and zero unauthorized cross-domain writes.
   - New unit tests (`tests/unit/test_p0_kernel_modularization.py`) verifying acyclic
     subpackage imports, `ExecutionClaim` protocol conformance, `board_control` stale-lease
     fencing & idempotent command replay/conflict, `trading/ledger` unified sandbox/paper
     fill parity, and `ipc/redaction` isolation.
2. **Connected End-to-End Flow (`tests/flows/flow-p0-kernel-modularization/test_flow.py`):**
   - `ConnectedFlow.test_end_to_end` exercises the modularized `runtime/kernel`, `learning`,
     `board_control` (`write_uow`), `trading/ledger` (sandbox + company paper fill), and
     `ipc/projections` + `ipc/redaction` end-to-end on a live SQLite company store.
3. **Regression & Hygiene Gates:**
   - All existing flow suites (`flow-agent-system`, `flow-p0-hardening`,
     `flow-p0-shift-runtime`, `flow-operator-trading-onboarding`) pass without regression.
   - `ruff==0.14.10` check + format, `reference_guard.py staged`, and `gitleaks` pass cleanly.
