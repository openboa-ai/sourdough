# Present the Modern Studio Floor Map and workstation drill-down

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../screens/spec.md) · [Flow observability](../flow-observability/spec.md) · [CEO Direct Line](../../governance/ceo-direct-line/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Render the secondary living office view (`/office`) and its full-screen Tier 1 drill-downs (`/office/agents/:agentId` and `/office/runs/:runId`) strictly over authorized UDS read projections (`GET /v1/projections/floor-map`, `GET /v1/projections/roster`, `GET /v1/projections/collaboration`, `GET /v1/projections/work-runs`, `GET /v1/projections/work-runs/{run_id}`, `GET /v1/projections/offboarding`). Inspection issues zero operational commands and never peeks inside `.desk/drafts/*` bodies.

Input contracts: [Base presentation](../present/spec.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../screens/spec.md) · [Converse](../../../company/systems/work/conversations/intent.md) · [Handoff](../../../company/systems/work/collaborate/intent.md) · [Agent desk](../../../employee/agent/desk/intent.md) · [Offboard](../../../company/systems/organization/offboard/intent.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. Interactive Agent Node-and-Edge Interaction Graph (`Graph | List` toggle)

Per [Design conventions](../../../conventions.md) and [Manage role definitions](../../../company/systems/authority/authorize-actor/manage-roles/spec.md), Sourdough does not instantiate rigid backend department entities (`"Companies can compose roles as work requires them; the product does not instantiate fixed departments"`). Rather than rendering oversized empty architectural room boxes, `/office` (**Agents & Activity**) presents a compact, scannable **Agent Node-and-Edge Interaction Graph** (defaulting to `Graph` for typical `1–8` agent teams, with a header `Graph | List` toggle and automatic list-density option at `9+` agents):

- **Compact Agent Nodes**: Each active Agent renders as a crisp flat node card on a subtle dotted white canvas displaying:
  - Agent display label (`display_name` / `agent_id`) and an explicit **Role Badge** (`CEO`, `Quant Research`, `Trading & Execution`, `Risk & Settlement`, `Operations & QA` — derived strictly in the presentation mapper from `founding_receipt.founder_id`/`ceo_agent_id`, `standing_grant.scope`, `execution_profile`, and `work_items.duty_kind`).
  - Live status indicator (`Running`, `Reviewing`, `Waiting on Peer`, `Onboarding`, `Scheduled`, `Idle`) and concise 1-line current task summary.
  - Compact footer showing monthly AI budget usage, opaque `.desk/drafts/*` count (`"N drafts"`, never draft content), and `"Details ->"` trigger.
- **Interactive Collaboration Edges**: Active conversations, peer review requests (`work_peer_requests`), handoffs (`work_handoff`), and escalated updates render as clean, non-crossing connector lines linking the interacting Agent nodes, each carrying a clickable edge pill label (e.g., `"Peer review · v4 fees"`, `"Mandate check"`, `"Order #041 update"`). Idle/scheduled links use subtle dashed muted lines. Clicking an edge pill navigates directly to that conversation channel inside `/messages`.
- **Header live pulse summary**: Compact status chips beside the page title summarize the entire organization at a glance (`N Agents · X Running · Y Reviewing · Z Active Talks`).
- **Backend provenance (`[BACKEND_GAP #6]` + `[V1_SQLITE_NEEDS_UDS]` + `[BACKEND_GAP #8]`)**: In `origin/main` `6694fa9`, `agent` (`02_company_core.sql:10-14`) stores only `(id, company_id, execution_profile)` with zero display names or role titles, and `staffing`/`onboarding`/`conversations`/`work_handoff`/`work_peer_response` emit zero `audit_event`s. When `floor-map-v1` is absent, fallback gracefully to `roster` / `work-timeline` metadata with role-derived labels.

### 2. Density scaling (`Graph` vs `List`, minimum `12px` text)

The workspace adapts cleanly as workforce size grows without shrinking any readable text below `12px`:

1. **`1–8` Agents (Default `Graph` view)**: Renders the spacious non-crossing node-and-edge interaction graph with compact agent cards and clickable edge pills; Day 0 (`1` Agent) displays the active Founder/CEO node alongside calm dashed hiring-slot hints (`"Ask CEO about first hire"` -> `/messages` CEO thread, `"View Mandate"` -> `/company/mandate`).
2. **`9+` Agents or operator `List` toggle (`List` view)**: Pins `blocked`, `waiting`, and incomplete `onboarding` exception rows at the top above an organization health bar (`running / waiting / blocked / onboarding / idle` counts in `tabular-nums`) and compact scannable rows showing role badge, current task, active interaction counterpart, and budget/desk counts.

### 3. Private Desk opacity & full-screen Tier 1 `Agent Detail` (`/office/agents/:agentId`)

- **Strict `.desk/drafts/*` opacity (`agent_desk.py`, `[V1_SQLITE_NEEDS_UDS]`)**: Active `.desk/drafts/*` filenames and draft body contents are never streamed or rendered. Graph nodes and Agent Detail display only `private_desk.manifest_revision`, numeric `draft_count`, and `.desk/logs/` offload metadata.
- **Zero right slide-over drawers**: Selecting an Agent node **never** opens `Y.Sheet` or a right-side inspector pane. Activation transitions the full main workspace view (`Tier 0 -> Tier 1`) to `/office/agents/:agentId` with structural `<nav aria-label="Breadcrumb">` (`Agents & Activity / <Agent>`) and deterministic keyboard focus recovery back to the originating graph node on Back/`Escape`.
- **Full Individual Profile (`/office/agents/:agentId`)**:
  - **Profile Header**: Agent name, Role Badge, live status, active task, `"Ask CEO about <Agent>"` -> `/messages`, and emergency inline-confirmed `"Stop Current Task"` (`interrupt_run`).
  - **Side-by-Side Overview Cards**:
    1. **`Desk & Workspace`**: Sandbox isolation state, private draft count (`"N private drafts — content kept private until published"`), published report count, and trace log entry count.
    2. **`Authority, Permissions & Budget`**: Active `standing_grant` scopes, explicit prohibited capabilities (e.g., live execution, self-activation, external network), monthly AI budget bar, and turn ceiling usage.
  - **4 Individual Activity Tabs**:
    1. **`Work Activity`**: `work_items`, `work_runs`, active `fencing_token` lease, `ScopedRun` trace metadata, `.desk/logs/` index, and `work_conditions` (`"Not scheduled"` when `null`).
    2. **`Interactions & Talks`**: Read-only view of all `project_message` threads this Agent participates in (badged `"Discussion only · Does not assign or accept work"`, linking to `/messages`), zero non-CEO Board chat composer.
    3. **`Published Reports`**: `shared_publication` excerpts authored by this Agent, cross-linking to `/company/org/publications/:assetId` and related `/orders/:orderId`.
    4. **`Peer Requests & Handoffs`**: Open/resolved `work_peer_requests` and `work_handoff` records involving this Agent.
- **Work Run Trace Detail (`/office/runs/:runId`)**: Focused `ScopedRun` span trace and `.desk/logs/` metadata view (`[V1_SQLITE_NEEDS_UDS]`), with emergency `interrupt_run` (`expected_execution_epoch`) and `cancel_work` (`expected_work_version`) gated behind `board_control` and the Universal Confirmation Sheet (`[BACKEND_GAP #7]`).
- **Bidirectional causal cross-links**: `Order (/orders/:orderId)` <-> `Published Report (/company/org/publications/:assetId)` <-> `Workstation Detail (/office/agents/:agentId)` <-> `Decision Card (/company/approvals/:questionId)`, preserving breadcrumb history and exact origin focus restoration.

### 5. 2D spatial keyboard grid accessibility

- Implement `/office` as a single-Tab-stop `role="grid"` where each architectural zone is a `role="row"` and each workstation card (or Day 0 bay) is a `role="gridcell"` with 2D roving `tabindex`:
  - `ArrowLeft` / `ArrowRight` move across workstations within the zone row; `ArrowUp` / `ArrowDown` move across the 4 zone rows; `Home` / `End` jump to first/last cell in the row.
  - `Enter` (or primary card activation) transitions the main view to `/office/agents/:agentId`.
  - `Escape` exits any inner control or returns from `/office/agents/:agentId` to `/office` with focus restored to the originating `role="gridcell"`.

**Concern for the responsible component:** Do not imply fixed backend departments, expose `.desk/drafts/*` bodies, render a Board chat input to non-CEO Agents, or open a right slide-over drawer.

## Functional and quality requirements

- **FR.** Render the 4 fixed presentation zones, semantic density tiers (`1–4`, `5–9`, `10+`), Day 0 dashed bays, calm peer/handoff/onboarding pills with hover/focus cross-highlighting, `.desk/drafts/*` opacity (`draft_count` only), and full-screen Tier 1 drill-downs (`/office/agents/:agentId` 3-tab view and `/office/runs/:runId`) over authorized UDS projections (`floor-map-v1`, `work-runs-v1`, `collaboration-v1`, `roster-v1`).
- **FR.** Tag screen capabilities cleanly with `V1_SQLITE_NEEDS_UDS`, `BACKEND_GAP #6`, and `BACKEND_GAP #8`, rendering honest `"Unavailable"` / capability-pending copy when `GET /v1/meta` omits `floor-map`.
- **NFR.** Maintain minimum `12px` typography across all density scales, full legibility under `prefers-reduced-motion: reduce`, single-Tab-stop `role="grid"` 2D keyboard navigation with deterministic focus restoration, and zero Board-to-non-CEO 1:1 chat inputs.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Day 0 & density scaling | Open `/office` with 1 Agent (`CEO Office` active, 3 Day 0 bays), then scale to 4, 7, and 12 Agents per zone. Confirm Day 0 bays offer `"Ask CEO about first hire"` / `"View Mandate"`, text stays `>= 12px`, and `10+` renders Zone Health Bar + pinned exceptions + `+N nominal` chips. |
| Desk opacity & read-only discussions | Stage `.desk/drafts/note.md` (`manifest_revision = 3`) and an active `project_message` + `work_peer_requests` link. Confirm Floor Map and `/office/agents/:agentId` show `"Private Desk · 1 draft (not shared)"` with zero draft text, badge discussions `"Discussion only · Does not assign or accept work"`, and provide zero Board chat composer. |
| 2D keyboard grid & zero-drawer drill-down | Navigate `/office` with `Arrow` keys, press `Enter` on a workstation cell, verify main-view transition to `/office/agents/:agentId` with `<nav aria-label="Breadcrumb">` (zero `Y.Sheet` drawer), and press Back/`Escape` to confirm focus returns to the originating `role="gridcell"`. |

**Oracle:** Cross-verify rendered Floor Map DOM, keyboard focus traces, `.desk/drafts/*` redaction, and `GET /v1/projections/floor-map` / `collaboration` payloads against [Client screens and navigation](../screens/spec.md) and [Flow observability](../flow-observability/spec.md).

## Operations and change review

Zone derivation rules stay strictly in the Client / `floor-map-v1` projection mapper; never add rigid backend department tables or bypass `.desk/drafts/*` opacity.
