# Modular Company OS Sidebar IA, Dual-Entry Contextual Approvals & Conditional Agent Sandbox (`flow-modular-ia`)

Status: Accepted specification · Target branch: `feat/flow-modular-ia` · Owner: Board Client IA (`apps/client/`) and IPC projections.

## 1. Intent

Separate Sourdough's industry-neutral **Company OS** from pluggable **Domain Workloads** (`Trading` today; future `Content`, `SaaS`, or multi-module companies tomorrow), remove internal jargon (`Execution Tiers`, `&`-stuffed menu labels) from navigation, make `Live Trading` the main `/` screen for Trading companies while separating `Paper Trading` to `/trading/paper`, surface **Approvals** as the universal actionable notification control plane with identical dual-entry approval execution from contextual screens, and show per-Agent personal paper sandboxes strictly inside `Agents` only when an Agent holds trading authority and has active sandbox activity.

## 2. Information Architecture & Domain Registry

- **Frontend Domain Module Registry (`apps/client/src/domains/registry.ts`):**
  - `AppShell` renders navigation dynamically from `meta.active_domains`:
    - Top actionable notification plane: **`Approvals`** (`/approvals`) with pending badge (plus topbar notification shortcut).
    - Dynamic Workload slot (`0..N` registered `DomainModule` sections):
      - When `active_domains` includes `"paper"` (`TRADING` module): `Live Trading` (`/`), `Paper Trading` (`/trading/paper`), `Strategies` (`/trading/strategies`).
      - When 2+ workload modules are active: auto-exposes `Overview` (`/`) above module sections.
      - When 0 workload modules are active (core-only company): hides `TRADING` cleanly and lands on `/agents`.
    - **`COMPANY`** (universal across all company types): `Agents` (`/agents`), `Messages` (`/messages`), `Finance` (`/finance`), `Reports` (`/reports`).
    - **`SYSTEM`** (universal governance & infrastructure): `Controls` (`/controls`), `Backups` (`/backups`), `Audit` (`/audit`, hosting Audit Log + Evaluations + Daemon Diagnostics tabs), `Settings` (`/settings`, hosting Appearance, Material, Active Modules, Board RBAC, and Privacy Lock).
  - All legacy paths (`/office`, `/office/agents/:id`, `/company/approvals`, `/company/strategy-trials`, `/company/finance`, `/company/org`, `/company/execution-tiers`, `/restrictions`, `/system/checkpoints`, `/system/evaluations`, `/system/audit`, `/system/daemon`) remain 100% compatible.

## 3. Universal Dual-Entry Approvals ("Control Plane of the Control Plane")

- `ContextualApprovalCard` (`apps/client/src/components/ContextualApprovalCard.tsx`) binds directly to authoritative `decision_question` rows via `submit_decision_answer` (`decision-command-v1`).
- Rendered both in the unified **`Approvals`** inbox AND inline inside matching contextual views (`Live Trading`, `Paper Trading`, `Agent Detail`, `Strategies`, `Controls`).
- Approving or declining from either entry point executes the exact same server command (`question_id`, `expected_revision`, `expected_payload_hash`) and synchronizes all surfaces immediately over `/v1/events`.

## 4. Conditional Per-Agent Sandbox Visibility

- `read_agents_graph_projection` attaches `sandbox_activity` to an Agent node ONLY when the Agent holds an active trading/paper standing grant (`scope == 'paper'`, not archived) AND has active sandbox state (fills, open orders/positions, or active `quant.agent_sandbox` run).
- `read_agent_detail_projection` attaches `sandbox_authorized: bool`.
- `AgentsActivityView` and `AgentDetailView` display the sandbox indicator and `Paper Sandbox` tab strictly when authorized and active (hidden for CEO or non-trading/idle agents).

## 5. Acceptance Evidence

- `npm --prefix apps/client run build` (`tsc --noEmit && vite build`) and `npm --prefix apps/client run check:yogurt` pass cleanly.
- `tests/unit/test_modular_ia_projection.py` verifies conditional `sandbox_activity` on `/v1/projections/agents` and `sandbox_authorized` on `/v1/projections/agents/{id}`.
- `tests/flows/flow-modular-ia/test_flow.py` (`ConnectedFlow.test_end_to_end`) verifies conditional sandbox visibility (shown for active quant, omitted for CEO/idle non-trading agent) and dual-entry approval idempotency over `ClientWebBridge`.
