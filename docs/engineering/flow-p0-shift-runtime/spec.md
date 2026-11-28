# P0-B Shift Runtime Decomposition, Declarative Tool Flow Classes, SQLite UoW & Duty/ToolKit Modularization

Status: Scoped design specification (`flow-p0-shift-runtime`, base `main@71f33716`)

## 1. Intent & Product Outcome

Eliminate the highest-complexity maintainability and SQLite I/O bottlenecks in Sourdough's Core shift execution path without altering any external authority, isolation, privacy, or compatibility contracts:

1. **Declarative Tool Information-Flow Metadata (`src/sourdough/agent_runtime/protocol.py`):**
   - Extend `ToolSpec` with an optional validated field `flow_class: ToolFlowClass = "neutral"` (`"neutral" | "private_desk_read" | "private_desk_write" | "peer_input" | "peer_outbound" | "external_write_sink"`).
   - Provide `ToolSpec.requires_desk_isolation() -> bool` as the single shared isolation predicate consumed by both `CodexCliAdapter` and `ShiftFlowGuard`, removing duplicated tool-name frozensets from `agent_runtime/codex_cli.py` and allowing future DomainPlugin and Connector tools to declare `flow_class="external_write_sink"` and automatically inherit information-flow and Desk isolation protections.
   - Annotate all Core Desk, Skill, Collaboration, and Trading write-sink `ToolSpec` declarations with their exact `flow_class`, while retaining a backward-compatible fallback table for unannotated legacy `ToolSpec`s constructed directly in tests.
2. **SQLite Read/Write Access Discipline (`src/sourdough/storage/db.py`):**
   - Add `verify_on_open: bool = True` to `open_writer(...)` so callers that immediately enter `write_tx(...)` (which runs `_verify_store_schema` and `_check_writable_state` atomically inside `BEGIN IMMEDIATE`) can skip the redundant pre-transaction `read_tx` verification pass.
   - Add `scoped_reader(store)` for short, local multi-query read phases using a single read-only connection and snapshot (never held across LLM, network, or subprocess I/O).
   - Add `write_uow(writer, *, tx_lock=None, ...)` to standardize `tx_lock` acquisition, `WriterLease.verify()`, `open_writer(..., lease=writer, verify_on_open=False)`, and `write_tx(...)` commit/rollback hygiene.
3. **Shift Runtime Modularization (`src/sourdough/runtime/shift/`):**
   - Decompose `ShiftRunner.run()` (`1,991` LOC) into five single-responsibility modules under `src/sourdough/runtime/shift/`:
     - `policy.py`: Declarative `flow_class`-first tool classification (`classify_tool_flow`), `is_authority_refusal_message`, and unified `redact_causal_trace_for_audit`.
     - `flow_guard.py`: Standalone, unit-testable `ShiftFlowGuard` + shift-memoized `ProvenanceIndex` reducing per-message N+1 `open_reader` opens to single-snapshot/cached lookups.
     - `hydration.py`: Shift startup hydration (`hydrate_shift_session`) consolidating prior receipts, peer receipts, recent episodes, active methods, and tool merging under a single `scoped_reader` snapshot.
     - `receipts.py`: `ShiftReceiptStore` encapsulating receipt payload/error sanitization, JSONL trace writes, output offloading, `tool_call_receipt` persistence via `write_uow`, and public offload slicing (updating `storage/ownership.py` table ownership for `tool_call_receipt` to `runtime/shift/receipts.py`).
     - `finalizer.py`: `finalize_shift_run` and `reconcile_shift_episode_outcome`, replacing silent `except Exception: pass` blocks with structured warning logs and unified audit trace redaction.
   - Retain `src/sourdough/runtime/shift_runner.py` as a clean facade preserving `ShiftRunner`, `ShiftRunResult`, `ActiveMethodsProvider`, `reconcile_shift_episode_outcome`, `is_authority_refusal_message`, and all test-referenced re-exports.
4. **Core Duty Handlers & ToolKit Consolidation (`src/sourdough/runtime/duties/`, `src/sourdough/runtime/tool_kit.py`):**
   - Extract `src/sourdough/runtime/tool_kit.py` (`require_object`, `require_str`, `optional_str`, `require_int`, `optional_int`, `FencedToolBase`) to eliminate duplicated argument validation and fencing/UoW boilerplate across `collab_tools.py`, `proactive_tools.py`, `desk_tools.py`, and `method_tools.py`.
   - Decompose `build_production_duty_registry()` (`1,538` LOC) into `CoreDutyDeps` and four focused duty modules (`duties/{board,collaboration,discovery,dreaming}.py`) behind a thin compatible `src/sourdough/runtime/handlers.py` facade.

## 2. Authority & Data Boundaries

- **Strict Layering:** `agent_runtime/protocol.py` remains free of `runtime/` or `trading/` imports; Core remains strictly isolated from `trading/` (`test_core_domain_isolation.py`).
- **SQL Write Ownership:** `tool_call_receipt` ownership transfers cleanly from `runtime/shift_runner.py` to `runtime/shift/receipts.py` in `storage/ownership.py` (`test_storage_ownership.py`).
- **Information-Flow Invariants:** Every existing Desk/Peer/External-sink taint rule and audience-intersection lattice check in `ShiftFlowGuard` remains fail-closed, including for custom tools declaring `flow_class="external_write_sink"`.
- **Prompt-Cache & Wire Compatibility:** `ToolSpec.flow_class` is host-side metadata only; provider wire payloads and `compute_stable_prefix_sha256` remain byte-identical.

## 3. Lifecycle Failure Modes & Invariants

- **Invalid `flow_class`:** `ToolSpec.__post_init__` raises `AgentRuntimeRefused` fail-closed if `flow_class` is not in `VALID_TOOL_FLOW_CLASSES`.
- **Unresolved provenance:** `ProvenanceIndex` fails closed to `{agent_id}` whenever a message, shared asset, peer request, or prior offloaded peer receipt cannot be resolved cleanly.
- **Unheld locks across I/O:** `scoped_reader` and `write_uow` strictly close SQLite transactions before returning; no reader or writer transaction spans LLM execution or subprocess I/O.
- **Finalizer observability:** Episode recording / FTS index errors during shift finalization log structured warnings rather than being silently swallowed, while still preserving settlement and shift exception precedence.

## 4. Acceptance Evidence Plan

- **New unit suite (`tests/unit/test_shift_flow_guard_and_uow.py`):**
  - Verifies `ToolSpec.flow_class` validation, `requires_desk_isolation`, and automatic `ShiftFlowGuard` + `CodexCliAdapter` protection for custom `external_write_sink` tools.
  - Verifies `ProvenanceIndex` memoization avoids repeated `open_reader` opens on identical projects/assets/messages.
  - Verifies `scoped_reader` and `write_uow` (`verify_on_open=False` skips redundant pre-tx schema verify while `write_tx` still refuses retired/corrupt stores and enforces `WriterLease`).
  - Verifies `ShiftFlowGuard` directly on private-desk taint, peer-input taint, audience-lattice narrowing, cross-project exfiltration blocking, and `discover_project_references` filtering.
  - Verifies `finalize_shift_run` logs warnings instead of silently swallowing episode errors.
- **Connected Flow E2E (`tests/flows/flow-p0-shift-runtime/test_flow.py`):**
  - Runs a multi-agent company shift with a custom `ToolSpec(..., flow_class="external_write_sink")`, exercises `ShiftRunner`, `ShiftFlowGuard`, `CodexCliAdapter` isolation, `duties/*`, and `tool_kit`, proving custom write sinks inherit both flow-gate and Desk isolation protection end-to-end.
- **Full Regression Gates:** `ruff==0.14.10`, `tests/unit`, `flow-p0-shift-runtime`, `flow-agent-system`, `flow-operator-trading-onboarding`, `test_core_domain_isolation`, `test_storage_ownership`, `gitleaks`, and `reference_guard.py staged`.
