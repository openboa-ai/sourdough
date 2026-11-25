# U1 · Board Client

[System map](../README.md) · [Client architecture](architecture.md) · [Client build contract](build.md) · [Usage scenarios](scenarios.md) · [Vocabulary](../glossary.md)

The Board Client is the optional presentation and control-plane UI for Sourdough. It owns local interaction state, not read-model truth or command authority. A company runs, trades on paper, reconciles, and recovers without this process installed or connected, and a generic client inspects a core-only company without requiring investment panels.

## Cross-feature architecture and verification

- [Client architecture](architecture.md) — Ownership boundaries, host topologies (`sourdough client-web` loopback HTTP-to-UDS bridge and probe-gated `zero://app` WebView option), `apps/board/` v0 disposition, **5-area Executive Information Architecture + quiet lateral `SYSTEM` section**, strict 4-element global header, zero right slide-over drawers, 3-tier progressive disclosure, Board-vs-CEO governance boundary, host-lock privacy shield, `v1` provenance taxonomy (`LIVE_UDS`, `V1_SQLITE_NEEDS_UDS`, `BACKEND_GAP #1–#8`), and design-system interface contracts.
- [Client build contract](build.md) — React 19, `@openboa/yogurt` (`0.2.0-local.1`), and `@ui-sdk/react` / `@ui-sdk/core` (`0.1.0-beta.13`) dependency isolation, visual baseline, Executive IA symbol binding, and clean-build verification.
- [Usage scenarios](scenarios.md) — Ten end-to-end operational scenarios covering normal trading/office/CEO operation, transient disconnect, command receipt recovery, scope-switch wipe, host lock, CEO-delegated offboarding custody, and paper/evaluation continuity.

## Projection presentation

### [Inspect authorized company state](inspect/intent.md)

- [present](inspect/present/spec.md) — Show only the authorized projection at its actual revision, with readable missing, stale, and locked states, 3-tier progressive disclosure, strict 4-element global header telemetry rules, and pure-read `Since Last Visit` digest semantics.
- [investment-panel](inspect/investment-panel/spec.md) — Primary entry view (`/`, **Live Trading & Portfolio**) when the quant profile is enabled: 3-second 4-KPI top band, grouped causal execution tape, `cancel != flat`, Two-Layer P&L reconciliation bridge, `0%`/`$0` baseline charts, `"Unavailable"` / `"No reading"` honesty, single collapsible `Since Last Visit` brief, and core-only fallback.
- [office-floor-map](inspect/office-floor-map/spec.md) — Secondary living office view (`/office`, **Office Floor Map**): 4 fixed flat presentation zones, semantic density scaling (`1–4` / `5–9` / `10+`), calm ledger-first collaboration pills + cross-highlighting, Day 0 architectural bays, `.desk/drafts/*` opacity, 2D `role="grid"` spatial keyboard navigation, and full-screen Tier 1 workstation/run drill-down.
- [screens](inspect/screens/spec.md) — Complete Executive IA screen inventory (`20` route table rows covering `24` URL patterns across the 5-area Executive IA + `/bootstrap`, `/restrictions`, and quiet `SYSTEM`: `/` -> `/office` -> `/ceo` -> `/company/*` + `/restrictions` + quiet `/system/*` + `/bootstrap`, where `/system/evaluations` covers `/suites`, `/grading`, and `/lineage`), zero right slide-over drawers, role gates, redaction rules, required 5 UI states, and `v1` (`6694fa9`) provenance tags.
- [flow-observability](inspect/flow-observability/spec.md) — End-to-end observability matrix mapping Sourdough Flows `01`–`13` to reconciled Executive IA routes, live event streams, causal lineage, Desk opacity rules, and `LIVE_UDS` / `V1_SQLITE_NEEDS_UDS` / `BACKEND_GAP #1–#8` provenance.

## Board-vs-CEO governance and approvals

### [Govern through CEO Direct Line and Board approvals](governance/intent.md)

- [ceo-direct-line](governance/ceo-direct-line/spec.md) — Dedicated 1:1 Board ↔ CEO Direct Line (`/ceo`), `Contextual Ask CEO` pinned read-only Context Attachment Card with live resolution state, zero inline command buttons inside chat, and strict exclusion of non-CEO 1:1 chat.
- [approvals-and-custody](governance/approvals-and-custody/spec.md) — 1-page `DecisionQuestion` approval cards (`/company/approvals`), `Interrupt` / `Approval` / `Digest` routing, read-only CEO-delegated offboarding custody audit ledger (`"Managed by CEO · Board view is read-only"` with zero Board `retire_employee` writes), and plain-language safety explanations (`15s` server quote cut, 5-step "Safe Custody Hold", DAG Blast-Radius Tree Preview).

## Control-plane interaction

### [Request a governance change](commands/intent.md)

- [submit](commands/submit/spec.md) — Submit an explicit typed command with optimistic concurrency preconditions through the Universal Human-Language Confirmation Sheet (*What changes* vs *What stays untouched* + consequence note + collapsed `Technical details`), resolve lost acknowledgments via `(company_id, principal_id)`-scoped receipts (`CommandOutboxPanel`), and enforce advisory-only `send_ceo_message` vs state-mutating commands.

## Control-plane service contracts

### [Serve the local control plane](control-plane/intent.md)

- [bridge-and-auth](control-plane/bridge-and-auth/spec.md) — UDS transport, `sourdough client-web` loopback bridge security, Native SDK `0.11.1` probe verdict and `apps/board/` disposition, `board_binding`/`role_assignment` RBAC, fail-closed non-CEO 1:1 chat enforcement, and `(principal, operation)` receipt scoping.
- [projections](control-plane/projections/spec.md) — `GET /v1/meta` capability negotiation and all `31` concrete `GET /v1/projections/*` read contracts (`35` `/v1/*` endpoints total, including `floor-map-v1`, `ceo-thread-v1`, `?since_seq=` `Since Last Visit` on `company-overview-v1`, live-quote vs sealed-valuation honesty, and `LIVE_UDS` / `V1_SQLITE_NEEDS_UDS` / `BACKEND_GAP #1–#8` tags).
- [commands-and-events](control-plane/commands-and-events/spec.md) — `GET /v1/events` redacted NDJSON stream, `CompanyRuntime` post-commit wakeups and `audit_event` emissions across the 15 silent domain/runtime areas, `Interrupt` / `Approval` / `Digest` classification, `send_ceo_message` (`ceo-chat-command-v1`), CEO-only `retirement.py` boundary (no Board `retire_employee` write), and generalized `POST /v1/commands` / `GET /v1/commands/{command_id}?operation=...` contracts.
- [paper-boundary](control-plane/paper-boundary/spec.md) — Strict `environment == "paper"` boundary behind `PaperDestination` / `PaperExecution`, `paper_sender_lease`, `BoundedVenueCoordinator`, `paper_binding_change` drain preconditions, and sealed `paper_valuation` vs missing live intraday quote feed honesty.

## Design system and component alignment

*(Interface contracts defined in [Client architecture](architecture.md#8-design-system-interface-contracts) and [Client screens and navigation](inspect/screens/spec.md#4-design-system--component-binding-alignment-contract).)*

### [Align Client presentation with the Yogurt design system](design-system/intent.md)

- [tokens-and-layout](design-system/tokens-and-layout/spec.md) — `@openboa/yogurt` (`0.2.0-local.1`) CSS `@layer` cascade, palette, typography, radii, container-query layout grammar, 4-element global header, zero-drawer discipline, and 5-state surface rules.
- [yogurt-components](design-system/yogurt-components/spec.md) — Verified `@openboa/yogurt` (`0.2.0-local.1`) + `@ui-sdk/react` / `@ui-sdk/core` (`0.1.0-beta.13`) primitive bindings, 4 custom domain compositions, zero-`Y.Sheet` enforcement, `ControlledConfirmDialog`, `AgentReady`, and `PortfolioChart` / `Chart` behavior.
