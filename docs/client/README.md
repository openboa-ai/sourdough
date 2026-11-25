# U1 · Board Client

[System map](../README.md) · [Client architecture](architecture.md) · [Client build contract](build.md) · [Usage scenarios](scenarios.md) · [Vocabulary](../glossary.md)

The Board Client is the optional presentation and control-plane UI for Sourdough. It owns local interaction state, not read-model truth or command authority. A company runs, trades on paper, reconciles, and recovers without this process installed or connected, and a generic client inspects a core-only company without requiring investment panels.

## Cross-feature architecture and verification

- [Client architecture](architecture.md) — Ownership boundaries, host topologies (`sourdough client-web` loopback HTTP-to-UDS bridge and probe-gated `zero://app` WebView option), `apps/board/` v0 disposition, 7-area domain Information Architecture, role visibility matrix, and host-lock privacy shield.
- [Client build contract](build.md) — React 19, `@openboa/yogurt` (`0.2.0-local.1`), and `@ui-sdk/react` (`0.1.0-beta.13`) dependency isolation, visual baseline, and clean-build verification.
- [Usage scenarios](scenarios.md) — Nine end-to-end operational scenarios covering normal operation, transient disconnect, command receipt recovery, scope-switch wipe, host lock, and paper/evaluation continuity.

## Projection presentation

### [Inspect authorized company state](inspect/intent.md)

- [present](inspect/present/spec.md) — Show only the authorized projection at its actual revision, with readable missing, stale, and locked states.
- [investment-panel](inspect/investment-panel/spec.md) — When the quant profile is enabled, inspect strict paper-only decision lineage, swappable `PaperExecution` adapter telemetry, and Two-Layer P&L without recalculating missing figures.
- [screens](inspect/screens/spec.md) — Complete screen inventory, route taxonomy, role gates, redaction rules, and required UI states across all seven domain areas.
- [flow-observability](inspect/flow-observability/spec.md) — End-to-end observability matrix mapping Sourdough Flows `01`–`13` to client screens, live event streams, causal lineage, and Desk opacity rules.

## Control-plane interaction

### [Request a governance change](commands/intent.md)

- [submit](commands/submit/spec.md) — Send an explicit typed command with optimistic concurrency preconditions. Resolve lost acknowledgments through the original `(company_id, principal_id)`-scoped receipt, never a second command.
