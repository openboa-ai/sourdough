# Present Agents & Activity and Agent Detail

Design draft · Verification **UNRUN** · Responsible component: Board Client (`apps/client/src/views/AgentsActivityView.tsx`, `apps/client/src/views/AgentDetailView.tsx`)

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../screens/spec.md) · [Flow observability](../flow-observability/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Render the Core Company OS agent organization views—**`AgentsActivityView`** (`/agents`, `agents-graph-v1`) and full-screen Tier 1 **`AgentDetailView`** (`/agents/:agentId`, `agent-detail-v1`)—across both core-only companies (`homeMode === "core_only"`, where `/` redirects to `/agents`) and Trading-enabled companies. Inspection issues zero operational commands and never exposes `.desk/drafts/*` bodies.

Input contracts: [Base presentation](../present/spec.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../screens/spec.md) · [Converse](../../../company/systems/work/conversations/intent.md) · [Handoff](../../../company/systems/work/collaborate/intent.md) · [Agent desk](../../../employee/agent/desk/intent.md) · [Offboard](../../../company/systems/organization/offboard/intent.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. `AgentsActivityView` (`/agents`, `agents-graph-v1`)

`AgentsActivityView` presents an interactive **Agent Node-and-Edge Interaction Graph** with a **`Graph | List`** toggle (respecting server `default_view_mode` from `agents-graph-v1`, e.g., `Graph` for typical small teams and `List` for larger teams):

- **Compact Agent Nodes**: Each active Agent node renders:
  - Agent display label (`display_name` / `agent_id`) and role badge (`CEO`, etc.), live activity state, and current work summary.
  - Budget summary, opaque `.desk/drafts/*` count (`"N drafts"`, never draft content), and conditional Tier 1 sandbox badge (`sandbox_activity` present **only** when `"trading"` is installed, the Agent holds an active trading standing grant, and has sandbox activity).
- **Interactive Collaboration Edges**: Active conversations, peer requests (`work_peer_requests`), and handoffs (`work_handoff`) render as connector edges whose clickable pills navigate directly to the corresponding channel in `/messages`.
- **Keyboard & Focus Recovery**: Supports full keyboard navigation across nodes and restores focus cleanly to the originating node when returning from `/agents/:agentId`.

### 2. Private Desk opacity & full-screen Tier 1 `AgentDetailView` (`/agents/:agentId`, `agent-detail-v1`)

- **Strict `.desk/drafts/*` opacity (`agent_desk.py`)**: Active `.desk/drafts/*` filenames and draft body contents are never streamed or rendered. Agent Detail displays only `desk_workspace.desk_manifest_revision` and numeric `desk_workspace.private_draft_count`.
- **Zero right slide-over drawers**: Selecting an Agent node **never** opens `Y.Sheet` or a right-side inspector pane. Activation transitions the full main workspace view (`Tier 0 -> Tier 1`) to `/agents/:agentId` with structural `<nav aria-label="Breadcrumb">` and deterministic focus recovery on Back/`Escape`.
- **Profile Header & Overview Cards**:
  - Header: Agent name, Role Badge, live status, `"Ask CEO about this agent"` -> `/messages?context_kind=agent&context_id=...`, and inline-confirmed emergency controls (`interrupt_run` / `cancel_work` via `ControlledInlineConfirm` for `board_control`).
  - Overview Cards: **`Desk & Workspace`** (opaque draft count, published report count) and **`Authority, Permissions & Budget`** (active `standing_grant`, monthly AI budget, turn ceiling).
- **Individual Activity Tabs (`4` Core Tabs + Conditional `Trading Sandbox` Tab)**:
  1. **`Work Activity`**: `work_items`, `work_runs`, `fencing_token`, `.desk/logs/` metadata, and `work_conditions`.
  2. **`Interactions & Talks`**: Read-only view of `project_message` threads (badged `"Discussion only · Does not assign or accept work"`, zero non-CEO Board chat composer).
  3. **`Published Reports`**: `shared_publication` excerpts authored by this Agent (`"Unavailable"` when audience lacks access).
  4. **`Peer Requests & Handoffs`**: Open/resolved `work_peer_requests` and `work_handoff` records.
  5. **`Trading Sandbox` *(Conditional)*:** Rendered **only** when `"trading"` is installed and the Agent has active sandbox eligibility or activity (`"trading.agent_sandbox"` or backward-compatible `"quant.agent_sandbox"` duty kind in `AgentDetailView.tsx:651-660`, or non-empty `agent_sandbox` portfolio from `trading_book_*` with `environment = 'agent_sandbox'`). Hidden completely for CEO, idle, or core-only agents.

**Concern for the responsible component:** Do not expose `.desk/drafts/*` bodies, render a Board chat input to non-CEO Agents, expose `Trading Sandbox` on core-only/CEO/non-trading agents, or open a right slide-over drawer.

## Functional and quality requirements

- **FR.** Render `/agents` (`Graph | List` toggle via `agents-graph-v1`, clickable edges -> `/messages`, opaque `.desk/drafts/*` counts, conditional `sandbox_activity`) and `/agents/:agentId` (`agent-detail-v1`, `4` Core tabs + conditional `Trading Sandbox` tab matching `"trading.agent_sandbox"` / `"quant.agent_sandbox"`, `interrupt_run` / `cancel_work` via `ControlledInlineConfirm`).
- **NFR.** Maintain minimum `12px` typography across Graph and List views, full legibility under `prefers-reduced-motion: reduce`, zero right drawers, deterministic focus recovery on Back/`Escape`, and zero Board-to-non-CEO 1:1 chat inputs.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| `Graph \| List` & conditional sandbox badge | Open `/agents` with CEO + active trading sandbox agent + idle non-trading agent. Confirm `Graph \| List` toggle works, `sandbox_activity` appears solely on the active trading agent, and clicking a collaboration edge opens `/messages`. |
| Desk opacity & read-only discussions | Stage `.desk/drafts/note.md` and an active `project_message`. Confirm `/agents` and `/agents/:agentId` show draft count only (zero draft text), badge discussions as read-only, and provide zero non-CEO Board chat composer. |
| Conditional `Trading Sandbox` tab & focus recovery | Open `/agents/:agentId` for an agent with `"trading.agent_sandbox"` / `"quant.agent_sandbox"` (tab visible with `trading_book_*` `agent_sandbox` rows) vs CEO/core-only agent (tab hidden). Press Back/`Escape` and confirm focus returns to `/agents`. |

**Oracle:** Cross-verify `AgentsActivityView.tsx`, `AgentDetailView.tsx`, `src/sourdough/ipc/projections.py`, and `src/sourdough/trading/plugin.py` against [Client screens and navigation](../screens/spec.md) and [Flow observability](../flow-observability/spec.md).

## Operations and change review

Zone and role derivation stays in `agents-graph-v1` / presentation mappers; never add rigid backend department tables or bypass `.desk/drafts/*` opacity.
