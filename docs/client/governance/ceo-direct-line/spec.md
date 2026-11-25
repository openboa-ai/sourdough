# Present the CEO Direct Line and Contextual Ask CEO

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Command submission](../../commands/submit/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Provide the unified **Company Messages** workspace (`/messages`, aliasing `/ceo`) combining:
1. **Board ↔ CEO 1:1 Direct Channel (Active & Writable)**: Strict 1:1 advisory and directional communication pinned at the top of the channel list over `GET /v1/projections/ceo-thread` (`ceo-thread-v1`, `[BACKEND_GAP #3]`) and `POST /v1/commands` (`send_ceo_message`, `ceo-chat-command-v1`, `[BACKEND_GAP #3]`).
2. **Internal Company Channels (Observable Read-Only)**: All internal agent-to-agent project discussions (`project_message` + `project_delivery`, `[V1_SQLITE_NEEDS_UDS]`, e.g., `#strategy-v4-review`, `#paper-desk-ops`) listed directly beneath the CEO thread so the Board can inspect internal company interactions in one unified messenger view—with **strictly zero message composer** on any non-CEO channel. Direct 1:1 messaging from the Board to non-CEO Agents remains prohibited across both UI and control plane.

Input contracts: [Base presentation](../../inspect/present/spec.md) · [Command submission](../../commands/submit/spec.md) · [Meta and authorized read projections](../../control-plane/projections/spec.md) · [Event stream, runtime wakeups, and typed commands](../../control-plane/commands-and-events/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. Unified Company Messages layout & strict CEO-only write boundary (`[BACKEND_GAP #3]` + `[V1_SQLITE_NEEDS_UDS]`)

- **In-page 2-column thread list + active conversation** *(no right slide-over drawer)*:
  - **Pinned section 1 — `DIRECT (BOARD ↔ CEO)`**: Single designated recipient `ceo_agent_id` (`founder_id` from `founding_receipt` / `GET /v1/meta`) — the **sole thread** with an active Board message composer.
  - **Section 2 — `INTERNAL COMPANY CHANNELS (READ-ONLY)`**: Lists all authorized internal `project_message` channels and active peer-review discussions (also deep-linked when clicking any interaction edge pill in `/office`), rendered strictly read-only with a calm `"Discussion only · Read-only for Board"` banner and zero composer input.
- **Control-plane fail-closed check**: `send_ceo_message` (`ceo-chat-command-v1`) submits `preconditions: {ceo_agent_id, charter_revision}` and `payload: {target_agent_id, body, context_ref?}`. Server enforces `payload.target_agent_id === preconditions.ceo_agent_id === ceo_agent_id` (`founder_id`); any mismatch or attempt to address a non-CEO Agent fails closed (`400` / `403`).
- **Provenance honesty (`[BACKEND_GAP #3]`)**: In `origin/main` `6694fa9`, `conversations.py` implements Agent-to-Agent `project_message` (`sender_id REFERENCES agent(id)`) only and `founding_receipt` tracks `founder_id` without a Board-CEO chat table or CEO wakeup-on-message handler. When `GET /v1/meta` omits `ceo-thread` / `send_ceo_message`, the CEO thread renders an honest capability-pending empty state while internal read-only channels remain inspectable once wired.

### 2. `Contextual Ask CEO` pinned read-only Context Attachment Card

- **Entry points**: Triggering **`"Ask CEO about this item"`** from a `Decision Card` (`/company/approvals/:questionId`), `Order Trace` (`/orders/:orderId`), `Workstation Detail` (`/office/agents/:agentId`), `Published Report` (`/company/org/publications/:assetId`), `Strategy Trial` (`/company/strategy-trials`), `Restriction` (`/restrictions`), or `Day 0 Bay` (`/office`) transitions the main workspace view to `/ceo?context_kind=<kind>&context_id=<id>`.
- **Pinned read-only Context Attachment Card**:
  - Anchors cleanly above the composer (and attaches `context_ref` to the posted `ceo_message`), displaying item title, plain-language summary, and **live server resolution status** hydrated from projections (`open`, `applied`, `cancelled`, `expired`, etc.) so the Board never discusses an already-settled decision under stale impressions.
  - **Zero inline command execution buttons**: The Context Attachment Card and chat message stream MUST NEVER embed `Approve`, `Decline`, `Suspend Risk`, or `Activate` mutation buttons inside chat. A single `"View source item"` navigation link returns to the originating Tier 1 page where explicit governance confirmation occurs through the Universal Confirmation Sheet.

### 3. Advisory-only message lifecycle & privacy rules

- **Advisory semantics**: Posting a message via `send_ceo_message` records the durable `ceo_message`, wakes the CEO runtime duty (`ceo.board_message_posted` `audit_event`), and surfaces delivery status (`sent | delivered | read_by_ceo`) without bypassing `CommandOutboxPanel` idempotency (`Idempotency-Key == command_id`). Because `send_ceo_message` is advisory communication rather than a state-mutating governance decree, it does not force a blocking Confirmation Sheet modal per message, while displaying an explicit `"Advisory message to CEO · Does not mutate orders, risk, or mandates"` caption.
- **Visibility & lock compliance**:
  - `board_viewer` principals inspect the CEO Direct Line thread read-only (composer hidden/disabled with `"Read-only role (board_viewer)"`).
  - `controls_locked = true` (disconnected/stale) disables sending while preserving verified read messages under the stale banner.
  - `401`/`403`/scope switch runs `clearData()` immediately; `host_locked = true` conceals the entire thread behind the opaque Privacy Shield (`data-agent-hidden`) without mutating `as_of_ns`.

**Concern for the responsible component:** Never expose non-CEO 1:1 messaging, execute operational/governance commands inline from a chat bubble, or display stale resolved context as still open.

## Functional and quality requirements

- **FR.** Render the 1:1 Board ↔ CEO thread (`ceo-thread-v1`), `Contextual Ask CEO` pinned read-only Context Attachment Card with live resolution state, and `board_control`-gated composer (`send_ceo_message`), with zero inline command execution buttons and zero non-CEO 1:1 chat routes.
- **NFR.** Enforce `clearData()` on `401`/`403`/scope change, opaque Privacy Shield on `host_locked`, keyboard accessibility across thread navigation and context jump links, and honest capability-pending UI when `BACKEND_GAP #3` is absent from `GET /v1/meta`.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Strict CEO-only target | Inspect all navigation and `/ceo` UI, and submit a crafted `send_ceo_message` targeting a non-CEO `agent_id`. Confirm zero non-CEO chat UI paths exist and the control plane fails closed (`400`/`403`). |
| `Contextual Ask CEO` live status | Click `"Ask CEO about this item"` on an open `DecisionQuestion`, observe the pinned Context Attachment Card (`status = "open"`, zero inline `Approve` button), settle the decision on `/company/approvals/:questionId`, and return to `/ceo` to confirm the pinned card reflects `"applied"` live. |
| Role & host-lock privacy | Open `/ceo` as `board_viewer` (composer disabled) and trigger `host_locked` (thread concealed behind `data-agent-hidden` without mutating `as_of_ns`). |

**Oracle:** Cross-verify `/ceo` state, `GET /v1/projections/ceo-thread`, `POST /v1/commands` (`send_ceo_message`), and `ceo.board_message_posted` `audit_event`s against [Command submission](../../commands/submit/spec.md) and [Client screens and navigation](../../inspect/screens/spec.md).

## Operations and change review

Any extension adding structured CEO summaries or attachments MUST preserve the zero-inline-command rule inside chat and route all state-mutating approvals through `/company/approvals` and the Universal Confirmation Sheet.
