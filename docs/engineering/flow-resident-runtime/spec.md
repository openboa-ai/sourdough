# Unified Resident Company Runtime & Production Duty Handlers (`flow-resident-runtime`)

Status: Accepted specification · Target branch: `feat/flow-resident-runtime` · Owner: Unified runtime, IPC control plane, and Board operational workflows.

## 1. Intent

Unify Sourdough's UDS Control Plane (`CompanyDaemon`) and autonomous Work scheduler (`CompanyRuntime`) into one resident process (`ResidentCompanyService`) holding a single exclusive `WriterLease`, backed by production `DutyRegistry` handlers (`ceo.respond_message`, `board.apply_decision`, `quant.agent_sandbox`, `quant.paper_decision`, `quant.reconcile`) and a single `sourdough run-company` CLI entry point.

With this change, a Sourdough company runs continuously with **zero Board Clients connected**: Agents wake on due work items, execute real multi-provider LLM shifts over `OuterToolBroker`, settle compute expenses into `general_ledger`, and commit state cleanly. When a Board Client connects over UDS (or `ClientWebBridge`), it immediately receives live `/v1/events` push notifications and can converse with Founder·CEO or resolve inline approval decisions that trigger real authorized company execution.

## 2. Authority & Data Boundaries

- **Single WriterLease coordination:** `ResidentCompanyService` starts `CompanyDaemon` (which owns the single `WriterLease`) and runs `CompanyRuntime.step_with_writer(writer, tx_lock=daemon.write_lock, on_commit=daemon.notify_commit)` on a dedicated scheduler thread. Short SQLite write transactions serialize under `daemon.write_lock`; slow LLM/CLI shift execution runs outside any SQLite transaction or lock so IPC reads and Board commands remain non-blocking.
- **Strict domain table ownership:** Every SQLite mutation stays inside its registered module owner (`board_control.py`, `runtime/work_kernel.py`, `execution_tiers.py`, `general_ledger.py`). Zero cross-domain SQL writes.
- **Fail-closed risk restriction boundary:** Active `restriction_cause` rows (`active = 1`) block new company risk duties (`quant.paper_decision`) while still permitting Board advisory dialogue (`ceo.respond_message`), isolated Tier 1 per-Agent sandboxes (`quant.agent_sandbox`), and reconciliation (`quant.reconcile`).
- **Board-trigger intake (`sync_board_triggers`):** Idempotently maps unacknowledged Board CEO messages (`ceo_message.delivery_state = 'recorded'`) and approved actionable decisions (`decision_question.state = 'applied' AND resolution_choice = 'approve'`) into deduped `work_items` owned by Founder·CEO, catching up seamlessly after any restart.

## 3. Production Duty Handlers (`src/sourdough/runtime/handlers.py`)

1. `ceo.respond_message`: Hydrates the Founder·CEO's shift context with the Board's message and recent thread history, executes the CEO's pinned provider adapter over `OuterToolBroker` (read-only company/tier status tools), settles compute cost to `general_ledger`, posts the reply via `board_control.record_ceo_reply`, and marks the inbound Board message delivered via `board_control.mark_ceo_message_delivered`.
2. `board.apply_decision`: Executes the concrete domain effect declared in an approved `decision_question.impact_json` (e.g., `update_agent_budget` updating `agent_profile.monthly_budget_minor`, or `open_agent_sandbox` opening a Tier 1 sandbox), then seals the application audit receipt via `board_control.record_decision_application_result`.
3. `quant.agent_sandbox`: Runs an employee Agent's pinned provider adapter wired strictly to `AgentSandboxTools(writer, agent_id=owner_agent_id)` over `OuterToolBroker`, then settles compute spend into `general_ledger`. Never requires `company_live` and remains isolated from company funds.
4. `quant.paper_decision`: Guarded by a precondition verifying zero active company risk restrictions; executes the quant Agent's pinned provider adapter with read-only status tools and settles compute spend into `general_ledger`.
5. `quant.reconcile`: Audits active execution tiers (`read_execution_tiers_snapshot`) and records deterministic reconciliation summary even during active risk restrictions.

## 4. Lifecycle Failures, Wakeups, & Recovery

- Every `CompanyDaemon` Board command commit invokes an `on_commit` wakeup callback so `ResidentCompanyService` schedules the resulting CEO reply or decision application immediately without polling delay.
- Every autonomous runtime commit invokes `daemon.notify_commit()` so connected `/v1/events` subscribers wake immediately.
- Clean stop (`stop()` / `SIGINT` / `SIGTERM`) halts the scheduler thread before closing `CompanyDaemon` and releasing the `WriterLease`. Restarting `ResidentCompanyService` on the same store resumes all pending/due work and recovers expired runs via `reclaim_expired_runs` without duplicating settled effects.

## 5. Acceptance Evidence

- Unit tests: `tests/unit/test_resident_runtime.py` covering `step_with_writer`, `sync_board_triggers` idempotency, all 5 production duty handlers, risk restriction blocking `quant.paper_decision` while permitting `quant.agent_sandbox` and `quant.reconcile`, restart recovery, and `sourdough run-company` CLI help/wiring.
- Connected flow test: `tests/flows/flow-resident-runtime/test_flow.py` (`ConnectedFlow.test_end_to_end`) booting `ResidentCompanyService` with zero connected Clients to prove autonomous Tier 1 sandbox execution + GL cost settlement + clean restart; then connecting `ControlPlaneClient`, sending a Board CEO message and waiting on `/v1/events` to observe a real LLM CEO reply on `/v1/projections/messages`, and approving an inline Board decision to observe its real domain mutation (`agent_profile.monthly_budget_minor`) applied live.
