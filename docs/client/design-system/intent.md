# Align Client presentation with the Yogurt design system

Design draft · [Board Client](../README.md) · [Client architecture](../architecture.md) · [Client build contract](../build.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

An operator inspecting company projections, work runs, paper-fund risk, continuity recovery, and offline evaluations—or submitting explicitly confirmed governance commands—needs a visually disciplined, keyboard-accessible, domain-grounded presentation layer built on `@openboa/yogurt` (`0.2.0-local.1`) and `@ui-sdk/react` (`0.1.0-beta.13`). Every control surface, confirmation boundary, chart, and navigation shell across the Client's four global shells and 33 routes must derive from Sourdough's company operations and authority invariants, preserve honest missing-data rendering, prevent unconfirmed autonomous agent mutations, and isolate presentation choices from Python/SQLite domain owners.

## Observable outcome

The Board Client maps every global shell, route, and domain composition unambiguously to pinned `@openboa/yogurt` (`0.2.0-local.1`) tokens, CSS `@layer` stylesheets, verified primitives, or documented `@ui-sdk/react` (`0.1.0-beta.13`) compositions:
1. Visual hierarchy relies on quiet warm-neutral/dark surfaces, monochrome default controls, strict `#175174` commitment-CTA reservation, `Rethink Sans` typography with `tabular-nums`, concentric radii, and zero static card shadows or gradients.
2. Every mutating command executes inside `ControlledConfirmDialog` on `@ui-sdk/react/button` (`SDKButton`), preserving revision latching, stale-decision warnings, one-shot guards, and explicit `details?.source !== "agent"` rejection.
3. Missing telemetry and high-baseline series render `"Unavailable"` / `"No reading"` and honest SVG path breaks without `0`-coercion or flattened curves.

## Scope of discretion

- **Tokens, CSS cascade, and layout grammar**: Define exact `@openboa/yogurt` token tiers, `21 x 11` palette rules, `#175174` vs `--yg-inverse` signal discipline, typography/space/radius/control grids, `@layer` cascade, container-query responsiveness, spatial placement grammar, 5-state surface rules, and domain-derivation constraints.
- **Component bindings, compositions, and agent safety**: Map all four global shells and 33 UI routes to verified `@openboa/yogurt` and `@ui-sdk/react` primitives, define the eight Sourdough domain compositions, specify `ControlledConfirmDialog` and `createYogurtAgent` / WebMCP guardrails, and specify `Chart` high-baseline and `null`-gap behavior.
- **Strict presentation isolation**: Keep all UI packages, stylesheets, font assets, and agent-DOM attributes strictly inside the Client presentation boundary; no company, quant, or control-plane service depends on UI code or assets.

## Development changes

- [Tokens, layout grammar, and presentation discipline](tokens-and-layout/spec.md)
- [Verified Yogurt component bindings, domain compositions, and safety](yogurt-components/spec.md)
