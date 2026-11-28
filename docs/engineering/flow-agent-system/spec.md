# Company Agent System: Private Desk, Collaboration, Proactive Discovery & Recursive Learning (`flow-agent-system`)

Status: Accepted specification · Target branch: `feat/flow-agent-system` · Owner: Core Company OS, Agent Harness, Work Kernel & Offline Evaluation.

## 1. Intent

Deliver Sourdough's Core Company OS agent execution system across runtime hardening and all four agent capability pillars, strictly preserving Core vs. `DomainPlugin` separation and single-owner SQL boundaries:

1. **Private Agent Space (Agent Desk):** [Agent Desk intent](../../employee/agent/desk/intent.md) · [desk/reopen](../../employee/agent/desk/reopen/spec.md) · [desk/publish](../../employee/agent/desk/publish/spec.md)
2. **Multi-Agent Collaboration:** [Collaborate intent](../../company/systems/work/collaborate/intent.md) · [collaborate/respond](../../company/systems/work/collaborate/respond/spec.md) · [collaborate/handoff](../../company/systems/work/collaborate/handoff/spec.md) · [Conversations intent](../../company/systems/work/conversations/intent.md) · [conversations/publish](../../company/systems/work/conversations/publish/spec.md) · [conversations/notify](../../company/systems/work/conversations/notify/spec.md)
3. **Proactive Work Discovery & Continuity:** [Discover-work intent](../../company/systems/work/discover-work/intent.md) · [discover-work/activate](../../company/systems/work/discover-work/activate/spec.md) · [discover-work/decide](../../company/systems/work/discover-work/decide/spec.md) · [Resume-work intent](../../company/systems/work/resume-work/intent.md) · [resume-work/supervise](../../company/systems/work/resume-work/supervise/spec.md) · [resume-work/recover-shift](../../company/systems/work/resume-work/recover-shift/spec.md)
4. **RSI & Recursive Learning:** [Methods intent](../../employee/agent/learning/methods/intent.md) · [methods/distill](../../employee/agent/learning/methods/distill/spec.md) · [methods/evaluate](../../employee/agent/learning/methods/evaluate/spec.md) · [Evaluate intent](../../employee/agent/learning/evaluate/intent.md) · [evaluate/run-trials](../../employee/agent/learning/evaluate/run-trials/spec.md) · [Recursive learning acceptance](../../acceptance/recursive-learning/spec.md)

With this specification, a company operates continuously with zero Board Clients attached while preserving strict private custody, bounded multi-agent coordination, deterministic work continuity, and falsifiable recursive learning.

## 2. Authority, Core OS vs. `DomainPlugin` Isolation & Data Boundaries

- **Core Company OS (`installed_domains = {'core'}`) vs. `DomainPlugin` Isolation:**
  - All four subsystems—Agent Desk, Multi-Agent Collaboration, Proactive Work Discovery & Continuity, and RSI / Recursive Learning—belong exclusively to Core Company OS.
  - Core modules (`src/sourdough/*.py`, `agent_runtime/`, `ipc/`, `runtime/`, `storage/`) contain zero imports from `sourdough.trading` or domain-specific profile tables.
  - Domain plugins attach only through `DomainPlugin` and `DutyRuntimeServices` in `src/sourdough/runtime/domains.py` (including an optional `validate_decision_evidence(conn, company_id, agent_id, decision_at_ns, evidence)` hook for domain-typed `DecisionEvidence`).
- **100% `STRICT` `company.sqlite` (Zero `FTS5` Virtual Tables in Company Store):**
  - Every table inside `company.sqlite` MUST be declared as a `STRICT` table in `src/sourdough/storage/schema/v1/core/*.sql`. Zero inline DDL is permitted in Core runtime modules.
  - SQLite `FTS5` virtual tables and their non-`STRICT` shadow tables MUST NEVER be created inside `company.sqlite` because shadow table layout depends on physical insert order and breaks logical watermark determinism (`compute_logical_watermark` in `src/sourdough/storage/checkpoint.py`).
  - Derived episode search indexes live in a physically separate, rebuildable SQLite file at `.sourdough-indexes/<company_id>/episodes_fts.sqlite` managed by `src/sourdough/episode_index.py` (registered in `SEPARATE_DB_MODULES` in `src/sourdough/storage/ownership.py`), strictly outside `company.sqlite` and outside all Agent Desk directories.
  - Offline evaluation state (`evaluator.sqlite`) remains physically separate in `src/sourdough/trial_registry.py` (`SEPARATE_DB_MODULES`), never exposed via `OuterToolBroker` or mounted into any Agent Desk.
- **Single-Owner SQL Table Boundaries (`src/sourdough/storage/ownership.py`):**
  - Every Core table and separate-DB table is registered in `DOMAIN_TABLE_OWNERS`:
    - `agent_runtime_binding` → `company.py`, `staffing.py`, `board_control.py`
    - `work_decision`, `work_event_cursor`, `work_discovery_state` → `runtime/work_kernel.py`
    - `tool_call_receipt` → `runtime/shift_runner.py`
    - `agent_episode` → `memory.py`
    - `episodes_fts`, `episodes_fts_meta` → `episode_index.py` (`SEPARATE_DB_MODULES`)
    - `method_delta`, `method_qualification`, `method_activation` → `method_learning.py`
- **Strict Proactive Trigger Invariant:**
  - `work_conditions.trigger_kind` is permanently restricted to `('timer', 'shared_asset_revision', 'peer_response')`.
  - Direct conversation messages (`project_message`) and inbox deliveries (`project_delivery`) MUST NEVER auto-wake a sleeping runtime or assign a work item (zero `project_inbox` trigger kind).
- **Client Absence & Private Custody:**
  - Closing all Board Clients never halts autonomous work, supervision, or recovery.
  - Private Desk draft filenames and contents never leak into `company.sqlite` audit logs, Board projections, or another Agent's Desk.
- **Two-Layer Naming & Abstraction Model:**
  - **Layer 1 — Company & Employee Operating Layer:** Organization, ownership, work continuity, and collaboration are modeled and named after a real company and human employees (`Company`, `Employee`/`Agent`, `Desk`, `Shared Project`, `Publication`, `Conversation`, `Peer Request`, `Handoff`, `Standing Responsibility`, `Work Item`, `Shift`, `Authority`/`Grant`, `Board`, `Work Method`).
  - **Layer 2 — Canonical AI-Agent Technical Mechanism Layer:** Established AI-agent engineering mechanisms use clear, standard industry terminology (`Always-On`, `Sleep`/`Wake`, `Heartbeat`, `Lease`, `FencingToken`, `Memory`, `Tool`, `Skill`, `Plugin`, `Connector`, `LLM`/`Model`/`Provider`, `Harness`, `Context`, `Prompt Cache`, `Compaction`, `Learning`, `Dreaming`, `Evaluation`, `Sandbox`). Company `Work Method` records (`method_delta` / `method_activation`) project deterministically into each Agent Desk as Harness `Skill` files (`skills/<skill-name>/SKILL.md`) paired with `skill_*` broker tools.

## 3. Subsystem Specifications

### 3.0 Phase-0 Runtime Hardening & Unified `ShiftRunner`

1. **Guaranteed General Ledger Cost Settlement (`src/sourdough/agent_runtime/cost_accounting.py`):**
   - `settle_shift_cost_to_ledger` ALWAYS records actual incurred compute spend via `post_journal()` under optional `tx_lock` **before** checking `budget_ceiling_minor_units` and raising `AgentBudgetExceeded`.
   - `OuterToolBroker` accrues per-round `TokenUsage` incrementally (`accrued_usage`) across multi-round HTTP or CLI turns; shift execution settles accrued usage inside `try/finally` so mid-shift failures (`AgentTransportInterrupted`, `AgentBudgetExceeded`, `RunFenced`) never drop incurred LLM spend from `general_ledger`.
2. **Recoverable Domain Tool Refusals vs. Fatal Aborts (`OuterToolBroker`, `{anthropic,openai,gemini}_api.py`):**
   - Pre-checks (`fencing_check` raising `RunFenced`, argument spoofing on forbidden server-identity keys, `call_id` mismatch, unknown tool, `max_tool_calls` exhaustion) and system/runtime aborts (`RunFenced`, `AgentRuntimeRefused`, `AgentBudgetExceeded`, `AgentProfileMismatch`, `AgentTransportInterrupted`, `StorageRefused`, `WriterRefused`, `FoundingRefused`) ALWAYS fail fatally and propagate out of `OuterToolBroker.invoke`.
   - Recoverable domain refusals (`WorkKernelRefused` excluding `RunFenced`, `DutyRefused`, `BoardControlRefused`, `DomainActionRefused`, `JournalRefused`, `MemoryRefused`, `ValueError`, `TypeError`, `KeyError`, and plugin-contributed recoverable exception types) are caught inside `OuterToolBroker.invoke`, persisted as `status='refused'` receipts/traces, and returned to the adapter as structured JSON `{"ok": false, "error_type": ..., "error": ...}` (with `is_error: true` in Anthropic `tool_result`) so the model can self-correct without losing turn settlement.
   - A trajectory loop-breaker detects `>= 3` distinct `call_id` calls with identical `(tool_name, arg_digest)` in one shift, returning a structured refusal on first trip and aborting with `AgentRuntimeRefused` if repeated again.
3. **Bounded Retry & Error Classification (`src/sourdough/runtime/engine.py`, `src/sourdough/runtime/work_kernel.py`):**
   - `work_items` adds `attempt_count INTEGER NOT NULL DEFAULT 0` and `next_retry_at_ns INTEGER`.
   - `step_with_writer` executes dispatches sequentially outside SQLite transactions and `tx_lock` (zero `ThreadPoolExecutor` concurrency).
   - Fatal/configuration errors (`AgentRuntimeRefused` including `AgentProfileMismatch` and `AgentBudgetExceeded`, `RuntimeRefused`, `DutyRefused`, `WorkKernelRefused` excluding `RunFenced`, `JournalRefused`, `StorageRefused`, `FoundingRefused`, `BoardControlRefused`, `DomainActionRefused`) call `fail_work_run(..., return_to_due=False, increment_attempt=True, next_retry_at_ns=None)`, transitioning `work_items.state` directly to `'blocked'`.
   - Transient `AgentTransportInterrupted` errors increment `attempt_count` and schedule exponential `next_retry_at_ns` backoff (`min(base * 2^(attempt-1), max_backoff)`) up to `max_transient_attempts`, transitioning to `'blocked'` once exhausted.
   - `_claim_next` skips `due` items whose `next_retry_at_ns > now_ns`.
4. **Unified `ShiftRunner`, `DutyContext.tx_lock`, `ContextPack v2` & Founder-CEO Desk:**
   - `DutyContext` carries optional `tx_lock: threading.RLock | None` and `write_lock_ctx()` so handler and tool SQLite writes serialize cleanly with `CompanyDaemon.write_lock`.
   - `ShiftRunner` (`src/sourdough/runtime/shift_runner.py` attached to `DutyRuntimeServices`) unifies profile binding lookup, `ensure_private_desk`, 3-tier `.desk/{drafts,scratch/<run_id>,logs}` setup/cleanup, `ContextPack v2` hydration, `OuterToolBroker` construction (`fencing_check`, `tool_call_receipt`, `.sourdough-traces`, 4KB output offloading), `try/finally` GL cost settlement, and `$0` `agent_episode` persistence across both Core handlers and `TradingDomainPlugin`.
   - `ContextPack v2` adds `desk_manifest`, `active_methods`, `active_snapshot_id`, and `recent_episodes` (all hashed into `ContextPack.digest`). `hydrate_shift_context` compacts only `history_notes` and `recent_episodes` while never evicting `open_receipts`, `grant_scope`, `mandate_summary`, `active_methods`, or `active_snapshot_id`. `_http.py` separates `render_context_system_blocks(request)` into `(stable_prefix, dynamic_suffix)` and `anthropic_api.py` attaches `cache_control: {"type": "ephemeral"}` to `stable_prefix`.
   - `company.py:found()` idempotently initializes Founder-CEO's `private_desk` row via `ensure_private_desk`; `agent_runtime_binding` in `02_company_core.sql` persists `(provider, model, revision, sandbox_backend, credential_handle_ref, profile_id)` for Core runtime binding.

### 3.1 Private Agent Space (Agent Desk)

1. **Three-Tier Desk Filesystem Layout (`src/sourdough/agent_desk.py`):**
   - `<store_parent>/desks/<agent_id>/.desk/drafts/<name>.json` (+ `.md` mirror): authoritative versioned private work bounded by `DeskLimits` (`LEGACY_DESK_LIMITS` default for compatibility; `PRODUCTION_DESK_LIMITS` `max_drafts=64`, `max_content_bytes=65_536`, `max_total_bytes=4_194_304`, `max_scratch_bytes=16_777_216`).
   - `<store_parent>/desks/<agent_id>/.desk/scratch/<run_id>/`: ephemeral per-run scratch allocated by `allocate_run_scratch` and removed by `cleanup_run_scratch`; excluded from digests, checkpoints, and arm clones.
   - `<store_parent>/desks/<agent_id>/.desk/logs/`: offloaded large tool output slices (`offload_large_output`), excluded from drafts and checkpoints.
   - `delete_draft` unlinks an authorized draft under `O_NOFOLLOW` + symlink verification and recomputes `compute_agent_desk_digest`.
   - `Selection` upper bound in `src/sourdough/company_assets.py` extends to `65_536` bytes, and `discover_project_references` filters explicitly on `p.company_id = :company`.
2. **Checkpoint v2 & Comparison-Arm Cloning (`storage/checkpoint.py`, `company_checkpoint.py`, `company_restore.py`, `board_control.py`, `agent_desk.py`):**
   - `checkpoint_company` and `board_control.execute_cut_checkpoint_command` pass `desk_root=desks_root(store)` when present, and `execute_cut_checkpoint_command` forwards its held `writer` into `checkpoint_store` to avoid `WriterLease` double-acquisition collisions.
   - `_stage_pinned_desk` rejects symlinks (checked before exclusions), rejects hard-link aliases (`st_nlink != 1`), and excludes `.desk/scratch/`, `.desk/logs/`, `.desk/mounts/`, and `.tmp-*` staging files.
   - `CheckpointManifest` and `company.checkpoint_cut` record `agent_desk_digests` mapping each agent's `.desk/` directory to its canonical `.desk/drafts/` SHA-256 digest and verify those digests against `private_desk` rows in `company.sqlite` during capture, inspection, and restore (`restore_core_checkpoint` forwards `desk_destination`).
   - `clone_desk_snapshot` copies only `.desk/drafts/` into isolated evaluation/arm directories via `O_NOFOLLOW` reads, verifies digest equality, initializes empty `.desk/scratch/` and `.desk/logs/`, and omits retired or revoked agents.
3. **Episodic Memory & Separate FTS5 Index (`src/sourdough/memory.py`, `src/sourdough/episode_index.py`):**
   - `memory.py` records `$0` `agent_episode` rows idempotently on `(company_id, run_id)` and enforces strict `(company_id, agent_id)` isolation on `read_episode` and `list_recent_episodes`.
   - `episode_index.py` maintains `.sourdough-indexes/<company_id>/episodes_fts.sqlite` with `rebuild_episodes_index`, `sync_episodes_index`, `upsert_episode_entry`, and agent-scoped `search_episodes` using safe tokenized FTS5 queries.
4. **Opt-In `desk_run_command` Sandbox (`src/sourdough/harness.py`):**
   - Gated strictly by explicit server-authoritative `desk:exec` grant AND `sandbox_backend == "local-macos"` on Darwin with `/usr/bin/sandbox-exec` (fail-closed otherwise).
   - Executes with `broker_socket=None` (total `(deny network*)`, zero access to `company.sqlite`, sibling Desks, or `.sourdough-traces/`), tier-2 `scratch_dir`, `DeskCommandLimits` (`timeout_seconds`, `RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, plus platform-supported memory/process bounds), and bounded stdout/stderr byte capture caps.
5. **`WorkRunClaim` Bridge, Broker Tools & `CodexCliAdapter` MCP Bridge:**
   - `WorkRunClaim` (`src/sourdough/duty_run.py`) bridges canonical `work_runs` (`assert_run_fencing`) and active `standing_grant` checks into `agent_desk.py`, `company_assets.py`, `harness.py`, `conversations.py`, `conversation_delivery.py`, and `work_handoff.py` without requiring legacy `duty`/`duty_run` rows or breaking existing `DutyClaim` callers.
   - `src/sourdough/runtime/desk_tools.py` exposes the 11 Core Desk, Asset, and Memory tools (`desk_list_drafts`, `desk_read_draft`, `desk_save_draft`, `desk_delete_draft`, `desk_read_offloaded_log`, `desk_run_command` when authorized, `desk_publish_selection`, `discover_project_references`, `read_shared_asset`, `memory_search_episodes`, `memory_read_episode`) over `OuterToolBroker`.
   - `CodexCliAdapter` (`src/sourdough/agent_runtime/codex_cli.py`) supports real `codex exec --json` via an embedded stdio MCP-to-UDS bridge (`--mcp-bridge`) with `confinement_arguments()`, preserved `CODEX_HOME`, `/opt/homebrew/bin` `PATH`, and `item.completed`/`turn.completed` JSONL parsing while maintaining backward compatibility for test shims.

### 3.2 Multi-Agent Collaboration

- **Scoped Threads & Bidirectional `peer_responders`:**
  - `conversations.py` and `conversation_delivery.py` accept `WorkRunClaim` alongside `DutyClaim` and enforce optional `expected_membership_revision` fencing.
  - `company_assets.py:_sync_project_peer_routes` authorizes policy-configured `peer_responders` for bidirectional intra-project peer requests while keeping cross-project unauthorized requests fail-closed.
- **Peer Request Circuit Breakers & Stale-Thread Fencing (`work_kernel.py`, `work_peer_response.py`):**
  - `work_peer_requests` adds `hop_count`, `chain_depth`, `root_work_item_id`, and `delegation_path_json`.
  - `open_peer_request` enforces `chain_depth`/`hop_count <= 4`, rejects ancestry repeats, runs transitive BFS mutual-wait deadlock detection across `state = 'open'` edges, enforces sliding-window pairwise rate limits, and `resolve_peer_request` executes atomic conditional updates fencing late responses after deadline expiry or prior settlement.
- **Explicit Handoff CAS State Machine (`05_collaboration_and_custody.sql`, `work_handoff.py`):**
  - `work_handoff` adds `state IN ('offered', 'accepted', 'declined', 'expired', 'withdrawn')` and `state_version >= 1`.
  - `offer_handoff`, `accept_handoff`, `decline_handoff`, `withdraw_handoff`, and `expire_handoffs` execute atomic Compare-And-Swap (`WHERE state = 'offered' AND state_version = ?`), preserving single accountable ownership.
- **Production Collaboration Handlers & Tools (`runtime/handlers.py`):**
  - Registers `work.peer_response`, `work.assessment_review`, and `work.handoff_followup` with `OuterToolBroker` tools (`append_message`, `read_messages`, `read_inbox`, `acknowledge_delivery`, `request_peer_response`, `respond_peer_request`, `offer_handoff`, `discover_handoffs`, `accept_handoff`, `decline_handoff`, `org_list_roster`).

### 3.3 Proactive Work Discovery & Continuity

- **Durable Event Propagation & Per-Asset Author Resolution (`work_event_cursor`, `engine.py`, `resident.py`):**
  - `publish_selection` and `resolve_peer_request` emit durable `audit_event`s drained sequentially up to batch bounds via `work_event_cursor.last_event_seq` inside `step_with_writer`.
  - `evaluate_conditions` resolves asset revisions and producer `author_id` per asset directly from `shared_publication` (preserving per-asset anti-self-wake even when multiple Agents publish between sweeps).
- **Coalescing, Hysteresis Cooldowns, Wake Budgets & Condition Fencing (`work_discovery_state`, `work_conditions`):**
  - `work_conditions` adds `cooldown_ns`, `max_fires`, and `fire_count` while keeping `trigger_kind IN ('timer', 'shared_asset_revision', 'peer_response')`.
  - Mandatory `peer_response` wakes and item-bound `timer`s bypass discretionary backoff; discretionary `work.discovery_review` triggers coalesce onto open `due`/`scheduled`/`running` items, apply exponential cooldown hysteresis on consecutive `defer`/`noop` decisions, and respect per-responsibility discretionary wake budgets.
  - `work_items.payload_json` stamps `(condition_id, condition_revision)` to fence stale updates.
- **Core `work.discovery_review`, `work_decision` & Supervision (`handlers.py`, `work_kernel.py`, `supervisor.py`, `cli.py`):**
  - `work.discovery_review` exposes `list_conditions`, `set_condition`, `replace_condition`, `cancel_condition`, and `record_work_decision`.
  - `work_decision` and `DecisionEvidence` enforce `0 <= available_at_ns <= observed_at_ns <= decision_at_ns` (future-leak guard), authoritative digest/permission checks (delegating domain kinds via `DomainPlugin.validate_decision_evidence`), and `(company_id, agent_id, action_id)` idempotency/conflict checks.
  - `sourdough supervise-company` + `run-company --ready-fd --store-key` supervise `ResidentCompanyService` with bounded restart backoff, `WriterLease` readiness signaling, and DB `agent_runtime_binding` loading.

### 3.4 RSI & Recursive Learning

- **Itemized `method_delta` & Private-to-Shared Redaction Gate (`06_evidence_review.sql`, `method_learning.py`):**
  - `method_delta` stores content-addressed `ADD | PATCH | DEPRECATE` items (`item_kind IN ('work_method', 'improvement_procedure', 'helper_script')`) with `trigger_spec_json`, `parent_method_ids_json`, `active_snapshot_id`, `source_refs_json`, `procedure_meta_json`, and `redaction_passed`.
  - `distill_method_delta` disputes unverified/contradicted source citations and enforces `redact_for_shared_scope` (stripping private paths/secrets, screening instruction-override patterns, and re-verifying surviving evidence under target `audience_scope`) before admitting `'project'` or `'company'` candidates.
- **3-Stage Pre-Commit Gate, Lineage DAG Quarantine & `ContextPack v2` Binding (`method_learning.py`):**
  - `qualify_method_delta` enforces Stage A (schema + negative triggers), Stage B (authority/sandbox/ledger harmlessness), and Stage C (`pass^k`, `k >= 3`, over held-out and negative-trigger cases).
  - `activate_method_delta` promotes qualified revisions under `command_receipt` and audience grant; `quarantine_method_lineage` executes transitive BFS over `(parent_method_ids_json, active_snapshot_id)` to quarantine faulted descendants and restore the latest clean incumbent.
  - `resolve_active_methods` injects `active_methods` and `active_snapshot_id` into `ContextPack v2` and persists `active_snapshot_id` onto `work_runs`, `work_decision`, and `agent_episode` for 4-step causal tracking (`available -> retrieved -> invoked -> outcome_delta`).
- **Offline Trial Runner, Blind Judge, Small-N Statistical Gate & Two-Generation RSI (`trial_runner.py`, `trial_registry.py`):**
  - `trial_runner.py` provisions isolated arms (`clone_desk_snapshot`), executes real `AgentRuntimeAdapter`s under equal `TrialCeiling`s, grades persisted SQLite outcomes deterministically (`grade_trial_observation`), runs position-swapped `(AB, BA)` blind pairwise judging (`commit_position_swapped_pairwise_grade`), and orchestrates the 2-generation `run_recursive_learning_cycle` (Gen-1 -> arm fork -> Gen-2 -> adoption -> shift witness -> quarantine rollback).
  - `trial_registry.py:summarize_rsi_transfer` aggregates repetitions by `family_id`, computes a deterministic family-clustered paired bootstrap lower confidence bound (`lcb_alpha`) and paired sign-flip permutation `perm_p`, and requires zero hard invariant violations alongside `lcb_alpha > min_worthwhile_margin` for `SUPPORTED`.
- **Read-Only Client Projections (`src/sourdough/ipc/projections.py`):**
  - Extends `PROJECTION_AGENT_DETAIL` (`active_methods`, `work_decisions`, `armed_conditions`, episode summaries while keeping private Desk draft names/contents opaque) and `PROJECTION_SYSTEM_EVALS` (`method_deltas`, `method_qualifications`, `method_activations`, `rsi_transfer_summaries`).

### 3.5 Always-On Agent Lifecycle, Wake Priority, Dreaming & Desk Harness Projections

1. **Single-Process Always-On Sleep/Wake Telemetry (`src/sourdough/runtime/resident.py`, `src/sourdough/ipc/server.py`):**
   - `ResidentCompanyService` runs a local-first single-process sleep/wake loop (zero additional daemon processes) over one `WriterLease` and `CompanyRuntime`.
   - `ResidentHealth` exposes explicit sleep/wake observability: `sleep_state` (`"sleeping"` | `"awake"`), `wake_count`, `last_wake_reason` (`"startup"` | `"trigger"` | `"ipc_commit"` | `"timer"`), `last_tick_at_ns`, `last_good_tick_at_ns`, `last_error`, and `recovered_runs_count`. Failing ticks update `last_tick_at_ns` and `last_error` without overwriting `last_good_tick_at_ns`; successful ticks update `last_good_tick_at_ns` and clear `last_error = None`.
   - `CompanyDaemon._handle_meta` (`/v1/meta`) optionally attaches live `resident_health` via a `health_provider` callback while `src/sourdough/ipc/projections.py` remains pure deterministic SQLite `read_tx` snapshots.
   - Rapid duplicate wake signals coalesce cleanly at three layers: `ResidentCompanyService.trigger_now` (`threading.Event`), `drain_work_event_cursor` (folding rapid `asset.published` events per asset), and `evaluate_conditions` (coalescing repeated `work.discovery_review` and `work.dreaming` condition triggers onto open `due`/`scheduled` items).
2. **Deterministic Wake Priority Ordering (`src/sourdough/runtime/engine.py`, `src/sourdough/runtime/work_kernel.py`):**
   - `CompanyRuntime._claim_next` and `evaluate_conditions` enforce deterministic priority tiers (`ORDER BY CASE ... ASC, created_at_ns ASC, work_item_id ASC`):
     - Tier `0`: Direct Board / human directives (`ceo.respond_message`, `board.apply_decision`).
     - Tier `1`: Blocking peer responses (`work.peer_response`).
     - Tier `2`: Mandatory work items, child continuations (`parent_work_item_id IS NOT NULL`, `result_dep:*`), and non-discretionary duties.
     - Tier `3`: Discretionary proactive discovery (`work.discovery_review`).
     - Tier `4`: Bounded idle-time episodic consolidation (`work.dreaming`).
3. **Crash-Mid-Shift Recovery & Receipt Preservation (`src/sourdough/runtime/shift_runner.py`, `work_kernel.py`):**
   - Each tool execution persists an immediate `tool_call_receipt` in an independent `write_tx` before returning to the model.
   - On worker crash or lease expiry, `recover-shift` (`reclaim_expired_runs`, also invoked synchronously on `ResidentCompanyService.start()`) fences expired `running` `work_runs` (`status = 'fenced'`), rejects stale fencing tokens with `RunFenced`, and returns the `work_item` to `'due'`.
   - On the resumed shift (`ShiftRunner.run`), bounded prior durable `tool_call_receipt` rows for `work_item_id` (`run_id != current_run_id`, latest 32 in chronological order) are hydrated into `ContextPack.open_receipts` (dynamic suffix, immune to compaction, preserving `stable_prefix_sha256`) so the resumed Employee observes prior completed/refused effects without duplicating idempotent side effects.
4. **Progressive Disclosure & Prompt-Cache Stability (`agent_runtime/protocol.py`, `_http.py`, `runtime/shift_runner.py`, `runtime/method_tools.py`):**
   - `ContextPack v2` projects active methods into a compact, deterministically sorted Skill Index (`project_skill_index`: `method_id`, `skill_name`, `item_kind`, short `when_to_use`, `skill_path`, `revision_sha256`, `audience_scope`) without embedding full multi-KB skill bodies in the system prompt.
   - Employees load full `SKILL.md` instructions on demand via `skill_read` and record causal usage via `skill_record_invocation`.
   - `ShiftRequest` deterministically sorts `tools` by name and `active_methods` by `(method_id, revision_sha256, audience_scope)`, and `render_context_system_blocks` records `stable_prefix_sha256` across turns to verify prompt-cache prefix stability.
5. **Bounded Idle-Time `Dreaming` Duty (`work.dreaming`, `method_learning.py`, `runtime/handlers.py`):**
   - `work.dreaming` runs only during idle windows (`_dreaming_precondition` yields whenever any mandatory `due` item exists company-wide or any non-dreaming `due` item exists for the same Agent) under explicit `window_ns`, `max_runs_per_window`, and `cooldown_ns` limits (honoring explicit item payload overrides before `work_responsibilities` scope fallbacks).
   - `audit_episodes_for_dreaming` inspects recent `agent_episode` records without deleting raw episodes or mutating L0 ledger/work tables, and drafts candidate `method_delta` (`ADD`/`PATCH`/`DEPRECATE`) proposals routed strictly through the 3-Stage Pre-Commit Gate.
6. **Host-Projected Desk `AGENTS.md` & `skills/<skill-name>/SKILL.md` (`method_learning.py`, `agent_desk.py`, `harness.py`):**
   - Host Harness projections materialize read-only `AGENTS.md` and `skills/<skill-name>/SKILL.md` inside each Agent Desk on `rehydrate_desk`, activation, BFS DAG quarantine/rollback, and pre/post `ShiftRunner.run` reconciliation.
   - `AGENTS.md` embeds both `## Desk Harness Custody Rules` and `## Operating Discipline`: (a) **Cheapest-First Execution Ladder**, (b) **Untrusted External Input Isolation**, (c) **Lazy Skill Loading** (`skills/<skill-name>/SKILL.md` + `skill_read` + `skill_record_invocation`), and (d) **Selective Silence & Draft-Before-Irreversible-Action**.
   - Both Python path guards (`assert_writable_desk_relative_path`) and Seatbelt profiles (`build_desk_sandbox_profile`) block Agent overwrites of `AGENTS.md` and `skills/` so pre-commit evaluation cannot be bypassed.

## 4. Allowed & Prohibited Behavior

- **Allowed:**
  - Reopening an Agent's own Desk under current grant; publishing bounded plain-text selections into authorized projects; searching only the requesting Agent's own `agent_episode` records via `episode_index.py`; executing `desk_run_command` only with explicit `desk:exec` grant and `local-macos` sandbox; delegating authorized intra-project peer requests within `chain_depth <= 4` and non-cyclic wait graphs; promoting redacted, 3-stage-qualified `method_delta` revisions within audience grant.
- **Prohibited:**
  - Any `FTS5` virtual table inside `company.sqlite`; any `sourdough.trading` import inside Core modules; any `project_inbox` wake trigger on `work_conditions`; exposing `evaluator.sqlite` to `OuterToolBroker` or Agent Desks; bypassing `DeskLimits`, `O_NOFOLLOW`, hardlink checks, or `sandbox_backend` gates; suppressing incurred LLM costs on `AgentBudgetExceeded`; auto-retrying fatal config/authority errors; misrepresenting deterministic/scripted fixtures as real-Agent execution evidence.

## 5. Lifecycle Failures, Schema Compatibility & Migration

- Pre-release `PRAGMA user_version = 1` schema remains authoritative. `src/sourdough/storage/schema.py` computes `pack_sha256` and `sqlite_schema_sha256` dynamically from `src/sourdough/storage/schema/v1/core/*.sql`.
- `src/sourdough/storage/migration.py:rebuild_core_v1_tables` (`migrate-store`) upgrades existing v1 stores atomically by rebuilding `CORE_REBUILD_TABLES` and altered Core tables (`work_items`, `work_conditions`, `work_peer_requests`, `work_handoff`) with column defaults preserved, creating any missing `CORE_NEW_TABLES` (`agent_runtime_binding`, `work_decision`, `work_event_cursor`, `work_discovery_state`, `tool_call_receipt`, `agent_episode`, `method_delta`, `method_qualification`, `method_activation`), refreshing `installed_domains` digests, and verifying `PRAGMA foreign_key_check` + `verify_v1_schema`.

## 6. Requirement-Linked Acceptance Evidence

Evidence is strictly partitioned into two classes that MUST NEVER be conflated:

1. **Deterministic Automated Verification (Fast CI):**
   - `tests/unit/` (`test_founding.py`, `test_storage_schema.py`, `test_storage_ownership.py`, `test_core_domain_isolation.py`, `test_schema_migration_v1.py`, `test_storage_checkpoint.py`, `test_company_restore.py`, `test_harness.py`, `test_agent_desk.py`, `test_memory_episodes.py`, `test_company_runtime.py`, `test_agent_runtime_adapters.py`, `test_resident_runtime.py`, and M2–M4 unit suites).
   - `tests/flows/flow-agent-system/test_flow.py` (`ConnectedFlow.test_end_to_end` + feature/boundary/pairwise suites) and existing regression flows (`flow-06`, `flow-10`, `flow-agent-adapters`, `flow-resident-runtime`).
2. **Real-Agent Execution & Rubric Verification (Live Smoke / Acceptance):**
   - Executed separately against real `AgentRuntimeAdapter` backends (`CodexCliAdapter` / HTTP providers) across Agent Desk, Multi-Agent Collaboration, Proactive Work Discovery, and `docs/acceptance/recursive-learning/spec.md`, recording actual store state transitions, `tool_call_receipt`, `agent_episode`, `work_decision`, `active_snapshot_id`, and `evaluator.sqlite` transfer summaries tagged explicitly with adapter, model, and live run identifiers.
