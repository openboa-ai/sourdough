# Present Company Messages and Contextual Ask CEO

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Command submission](../../commands/submit/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Provide the unified **Company Messages** workspace (`MessagesView`, `/messages`, `messages-v1`) combining:
1. **Board ↔ CEO 1:1 Direct Channel (Active & Writable)**: Strict 1:1 advisory and directional communication pinned at the top of the channel list over `GET /v1/projections/messages` (`messages-v1`) and `POST /v1/commands` (`send_ceo_message`, `ceo-chat-command-v1`).
2. **Internal Company Channels (Observable Read-Only)**: All internal agent-to-agent project discussions (`project_message` + `project_delivery` in `messages-v1.internal_channels`) listed beneath the CEO thread so the Board can inspect internal company interactions in one unified messenger view—with **strictly zero message composer** on any non-CEO channel. Direct 1:1 messaging from the Board to non-CEO Agents remains prohibited across both UI and control plane.

Input contracts: [Base presentation](../../inspect/present/spec.md) · [Command submission](../../commands/submit/spec.md) · [Meta and authorized read projections](../../control-plane/projections/spec.md) · [Event stream, runtime wakeups, and typed commands](../../control-plane/commands-and-events/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. Unified Company Messages layout & strict CEO-only write boundary

- **In-page 2-column thread list + active conversation** *(no right slide-over drawer)*:
  - **Pinned section 1 — `Founder · CEO` (`DIRECT`):** Single designated recipient `ceo_agent_id` (`founder_id` from `messages-v1` / `GET /v1/meta`) — the **sole thread** with an active Board message composer.
  - **Section 2 — `INTERNAL COMPANY CHANNELS (READ-ONLY)`:** Lists authorized internal `project_message` channels and active peer-review discussions (also deep-linked when clicking any interaction edge pill in `/agents`), rendered strictly read-only with a `"Discussion only · Read-only for Board"` notice and zero composer input.
- **Control-plane fail-closed check**: `send_ceo_message` (`ceo-chat-command-v1`) submits `preconditions: {ceo_agent_id, charter_revision}` and `payload: {target_agent_id, body, context_ref?}`. Server enforces `payload.target_agent_id === preconditions.ceo_agent_id === ceo_agent_id`; any mismatch or attempt to address a non-CEO Agent fails closed (`400` / `403`).

### 2. `Contextual Ask CEO` pinned read-only Context Attachment Card

- **Entry points & `ALLOWED_ASK_CONTEXT_KINDS` (`apps/client/src/state/store.tsx:99-113`):** Triggering **`"Ask CEO about this item"`** from a view transitions in-place to `/messages?context_kind=<kind>&context_id=<id>` using one of the supported context kinds:
  - **Core Company OS kinds:** `"agent"` (`/agents/:id`), `"shared_publication"` (`/reports`), `"decision_question"` (`/approvals/:id`), `"restriction"` / `"restriction_cause"` (`/controls`), `"work_run"` (`/agents/:id`), `"company"` (`/agents`), `"account_balance"` (`/finance`), `"audit_event"` (`/audit`).
  - **Trading domain kinds (when `"trading"` installed):** `"trading_order"` (`/orders/:orderId`), `"strategy_release"` / `"challenger_trial"` / `"strategy_trial"` (`/trading/strategies`).
- **Pinned read-only Context Attachment Card**:
  - Anchors above the composer (and attaches `context_ref` to the posted `ceo_message`), displaying item title, plain-language summary, and **live server resolution status** hydrated from projections (`open`, `applied`, `cancelled`, `expired`, etc.).
  - **Zero inline command execution buttons**: The Context Attachment Card and chat message stream MUST NEVER embed `Approve`, `Decline`, `Suspend Risk`, or `Activate` mutation buttons inside chat. A single `"View source item"` navigation link returns to the originating canonical Modular IA route.

### 3. Advisory-only message lifecycle & privacy rules

- **Advisory semantics**: Posting a message via `send_ceo_message` records the durable `ceo_message`, wakes the `ceo.respond_message` duty (`ResidentCompanyService`), and surfaces delivery status (`recorded | delivered | replied`) without bypassing `CommandOutboxPanel` idempotency (`Idempotency-Key == command_id`). Because `send_ceo_message` is advisory communication rather than a state-mutating governance decree, it does not force a blocking confirmation sheet per message, while displaying an explicit `"Advisory message to CEO · Does not mutate orders, risk, or mandates"` caption.
- **Visibility & lock compliance**:
  - `board_viewer` principals inspect `/messages` read-only (composer hidden/disabled with `"Read-only role (board_viewer)"`).
  - `controls_locked = true` (disconnected/stale) disables sending while preserving verified read messages under the stale banner.
  - `401`/`403`/scope switch runs `clearData()` immediately; `host_locked = true` conceals the entire thread behind the opaque Privacy Shield (`data-agent-hidden`) without mutating `as_of_ns`.

**Concern for the responsible component:** Never expose non-CEO 1:1 messaging, execute operational/governance commands inline from a chat bubble, or display stale resolved context as still open.

## Functional and quality requirements

- **FR.** Render the 1:1 Board ↔ CEO thread and read-only internal channels (`messages-v1`), `Contextual Ask CEO` pinned read-only Context Attachment Card (`ALLOWED_ASK_CONTEXT_KINDS`, including `"trading_order"`), and `board_control`-gated composer (`send_ceo_message`), with zero inline command execution buttons and zero non-CEO 1:1 chat routes.
- **NFR.** Enforce `clearData()` on `401`/`403`/scope change, opaque Privacy Shield on `host_locked`, and keyboard accessibility across thread navigation and context jump links.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Strict CEO-only target | Inspect all navigation and `/messages` UI, and submit a crafted `send_ceo_message` targeting a non-CEO `agent_id`. Confirm zero non-CEO chat UI paths exist and the control plane fails closed (`400`/`403`). |
| `Contextual Ask CEO` live status | Click `"Ask CEO about this item"` on an open `DecisionQuestion` (`/approvals/:id`) or `TradingOrder` (`/orders/:id`), observe the pinned Context Attachment Card (`status = "open"`, zero inline `Approve` button), settle the decision on `/approvals/:id`, and return to `/messages` to confirm the pinned card reflects `"applied"` live. |
| Role & host-lock privacy | Open `/messages` as `board_viewer` (composer disabled) and trigger `host_locked` (thread concealed behind `data-agent-hidden` without mutating `as_of_ns`). |

**Oracle:** Cross-verify `MessagesView` (`/messages`), `GET /v1/projections/messages`, `POST /v1/commands` (`send_ceo_message`), and `ALLOWED_ASK_CONTEXT_KINDS` against [Command submission](../../commands/submit/spec.md) and [Client screens and navigation](../../inspect/screens/spec.md).

## Operations and change review

Any extension adding structured CEO summaries or attachments MUST preserve the zero-inline-command rule inside chat and route all state-mutating approvals through `/approvals` and `ControlledInlineConfirm`.
