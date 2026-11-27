# Align Client presentation with the Yogurt design system

Design draft · [Board Client](../README.md) · [Client architecture](../architecture.md) · [Client build contract](../build.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

A Board principal overseeing an autonomous Sourdough company—whether running a Core-only Company OS (`/agents`, `/approvals`, `/messages`, `/finance`, `/reports`, `/controls`, `/backups`, `/audit`, `/settings`) or an opt-in Trading company (`/`, `/trading/live`, `/trading/paper`, `/trading/strategies`, `/orders/:orderId`)—needs a calm, visually disciplined, keyboard-accessible presentation layer built on `@openboa/yogurt` (`0.2.0-local.1`), `@ui-sdk/react` (`0.1.0-beta.13`), and `@ui-sdk/core` (`0.1.0-beta.13`). Across all global chrome surfaces and the `17` mounted route branches of Sourdough's Modular Information Architecture, every control surface, strictly 4-slot header + `SidebarFooter` diagnostics, zero-drawer full-screen transition, inline human confirmation, chart, and agent interaction graph must derive directly from Sourdough's Core OS vs Trading domain boundaries without copying upstream design-system example templates.

## Observable outcome

The Board Client maps every global chrome surface, route, and custom domain composition unambiguously to pinned `@openboa/yogurt` (`0.2.0-local.1`) tokens, CSS `@layer` stylesheets, verified primitives, or documented `@ui-sdk/react` / `@ui-sdk/core` (`0.1.0-beta.13`) compositions:

1. **Desktop shell, palette, and zero-drawer discipline:** Visual hierarchy uses quiet warm-neutral (`#FFFFFF` / `#F4F4F1`) and dark (`#0A0A0D` < `#131317` < `#1D1D22`) surfaces, monochrome `--yg-inverse` everyday controls, strict `#175174` commitment-CTA reservation, `Rethink Sans` typography with `tabular-nums`, concentric `{4, 6, 8, 12, 16, 20, 999}px` radii, and zero CSS gradients or static card shadows. Navigation relies on a strictly 4-slot `<header className="sd-topbar">` (`Brand`, `Approvals` pill, `⌘K` `Dialog`, `Theme Toggle`), `SidebarFooter` `<Status>` diagnostics -> `/audit?tab=daemon`, sub-header `Notice` / `CommandOutboxPanel`, and **zero right slide-over drawers (`Y.Sheet` and right inspector panes prohibited across 100% of screens and breakpoints)**.
2. **Board-vs-CEO governance & `ControlledInlineConfirm`:** Board 1:1 chat is strictly scoped to the designated Founder/CEO (`/messages`, zero inline command execution buttons), while routine HR/offboarding remains CEO-delegated (read-only Custody & Handover Audit Ledger on `/reports` with zero Board retirement write controls). Every mutating Board command confirms inline via `ControlledInlineConfirm` (`@ui-sdk/react/button` `SDKButton`) contrasting plain-language **What changes** against **What stays untouched** (`RecordFacts` `<dl>`), collapsing technical preconditions inside `"Technical details"`, preserving revision latching, stale-decision warnings, one-shot guards, and explicit `details?.source !== "agent"` rejection alongside `createYogurtAgent` (`allow: () => false`) `AgentReady` redaction (`data-agent-confirm`, `data-agent-secret`, `data-agent-hidden`).
3. **Honest telemetry & modular domain observability:** Missing telemetry and high-baseline financial series render `"Unavailable"` / `"No reading"` and honest `0%`/`$0` baseline SVG path breaks (`PortfolioChart` + `Shadow Benchmark`) without `0`-coercion or flattened curves, while `AgentsActivityView` (`/agents`) scales across `Graph | List` modes with opaque `.desk/drafts/*` handling and `AgentDetailView` (`/agents/:agentId`) conditionally shows the `Trading Sandbox` tab only when authorized/active.

## Scope of discretion

- **Tokens, CSS cascade, and layout grammar:** Define exact `@openboa/yogurt` token tiers, `21 x 11` palette rules, `#175174` vs `--yg-inverse` signal discipline, typography/space/radius/control grids, `@layer` cascade, `@container` responsiveness, unified dual-halo focus rings, `--yg-off` Switch track contrast, `AgentRow` `<= 44px` caps, 5-state surface rules, zero-drawer spatial grammar, and Core/Trading domain derivation.
- **Component bindings, domain compositions, and `AgentReady` safety:** Map `AppShell` (`sd-topbar`, `SidebarLayout`, `SidebarFooter`, `CommandOutboxPanel`, Privacy Shield) and all `17` `App.tsx` route branches to verified `@openboa/yogurt` and `@ui-sdk/react` / `@ui-sdk/core` primitives; specify `ControlledInlineConfirm` and `createYogurtAgent` / WebMCP guardrails; and specify `PortfolioChart` / `Chart` high-baseline and `null`-gap behavior.
- **Strict presentation isolation:** Keep all UI packages, stylesheets, font assets, and agent-DOM attributes strictly inside the Client presentation boundary; no company, trading, or control-plane service depends on UI code or assets.

## Development changes

- [Tokens, layout grammar, and presentation discipline](tokens-and-layout/spec.md)
- [Verified Yogurt component bindings, domain compositions, and safety](yogurt-components/spec.md)
