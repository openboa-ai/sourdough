# Tokens, layout grammar, and presentation discipline

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client build contract](../../build.md) · [Component bindings](../yogurt-components/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Pin the exact `@openboa/yogurt` (`0.2.0-local.1`), `@ui-sdk/react` (`0.1.0-beta.13`), and `@ui-sdk/core` (`0.1.0-beta.13`) styling cascade, palette tokens, typography, geometry grids, spatial placement grammar, five-state visual rules, zero-drawer shell invariants, and domain-derived layout constraints for the React 19 Board Client. Presentation tokens and layout rules apply solely inside the Client build; no Python service, SQLite schema, or UDS control-plane handler imports or depends on UI stylesheets or layout state.

Input contracts: [Client architecture](../../architecture.md) · [Client build contract](../../build.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Base presentation](../../inspect/present/spec.md) · [Trading Desk & Strategies](../../inspect/investment-panel/spec.md) · [Agents & Activity](../../inspect/office-floor-map/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [Approvals & Custody](../../governance/approvals-and-custody/spec.md). These are public client and service contracts, not downstream dependencies from company systems onto UI assets.

## Design decision and review

### 1. Package pins, CSS `@layer` cascade, and root wrapper

- **Pinned package contract (`apps/client/package.json` / lockfile only):**
  - `@openboa/yogurt`: `0.2.0-local.1` (`--target web` distribution only; `react` peer dependency `>=19.2.0 <20`)
  - `@ui-sdk/react`: `0.1.0-beta.13`
  - `@ui-sdk/core`: `0.1.0-beta.13`
- **CSS `@layer` import order:** Import `@openboa/yogurt/styles.css` first in `apps/client/src/main.tsx` before `./styles/app.css` (`@layer yg.patterns`) and `./styles/graph.css`. It establishes the explicit cascade:
  ```css
  @layer ui-sdk, yg.tokens, yg.base, yg.components, yg.patterns, yg.utilities;
  ```
  Every application-level layout stylesheet added by the Board Client MUST declare an explicit `@layer` and never emit unlayered rules that override Yogurt component or focus-ring selectors.
- **Root wrapper (`@openboa/yogurt/yogurt`):** Wrap the Client shell in `<Yogurt appearance={appearance} material={material} className="sd-stage">` (`appearance: "light" | "dark" | "auto"`, default `"light"`; `material: "neutral" | "paper" | "mist"`, default `"neutral"`), keeping `<div className="yg-root" data-appearance data-material>` as the CSS custom-property scope (`light-dark(...)` resolution and base typography).

### 2. Typography and quantitative scale grids

- **Typography (`design/tokens/brand.json`, `design/styles/ui.css`):**
  - **Primary typeface:** `Rethink Sans` bundled locally as variable `.woff2` font assets (`rethink-sans.woff2` normal and `rethink-sans-italic.woff2` italic, weights `400..800`, `font-feature-settings: "cv02", "cv03", "cv04", "ss01"`). Never fetch fonts from external CDNs.
  - **Role weights:** `heading: 520`, `body: 420`, `ui: 500`, `value: 540`, `wordmark: 700`.
  - **Type ramp & tracking:** Sizes `{cap: 10, caption: 11, help: 12, ui: 13, body: 14, card-title: 14, subheading: 16, section: 18, title: 24, value: 28, headline: 32}px` with paired tracking (`body -0.006`, `label -0.012`, `heading -0.022`, `display -0.03`, `meta 0.04`, `cap 0.06`, `value -0.024`).
  - **Minimum readable text floor on `/agents` (`AgentsActivityView`):** Across both `Graph` and `List` modes, readable agent labels, statuses, duty summaries, and counts MUST remain `>= 12px` (`help` tier or above).
  - **Monospace & tabular numerals:** Use `--boa-font-mono` (`ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace`) at `0.8125rem` (`13px`) / `1.55` line-height on `CodeBlock` payloads and `Trace` indices, as well as digests (`sha256`, `payload_sha256`, `bundle_sha256`) and IDs (`company_id`, `command_id`, `run_id`, `order_id`). Apply `font-variant-numeric: tabular-nums` on every monetary amount, return percentage, quantity, fill price, basis-point spread, revision, `as_of_ns`, `event_seq`, and `pending_age` counter.
- **Strict space scale (`--boa-space-*`):** `[2, 4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64]px`. Off-grid spacing values are prohibited.
- **Strict even concentric radius grid (`--yg-radius-*`):** Strictly `{4, 6, 8, 12, 16, 20, 999}px`:
  - `inset: 4px`, `small / chip: 6px`, `control: 8px`, `menu / panel: 12px`, `surface / card / popover / dialog: 16px` (note `Y.Sheet` desktop right slide-over drawers are **prohibited**), `feature: 20px`, `round / pill: 999px`.
- **Control height tiers (`--yg-size-*`):** `[28, 32, 36, 44]px` (`compact: 28px`, `regular: 32px`, `row / comfort: 36px`, `touch / spacious / featured: 44px`). Global Header height is locked to `48px` (`--boa-space-48`).
- **Container breakpoints:** `{ compact: 360, narrow: 480, medium: 720, wide: 1024 }px` driving `@container` layout rules.

### 3. `21 x 11` Palette, CTA signal discipline, and surface polarity

- **Unified `21 x 11` (`231`-color) palette:**
  - `3` neutral families (`neutral`, `paper`, `mist`) + `18` chromatic families (`slate`, `blue`, `indigo`, `violet`, `purple`, `magenta`, `rose`, `red`, `terracotta`, `orange`, `ochre`, `yellow`, `lime`, `green`, `sage`, `teal`, `cyan`, `earth`), each stepped monotonically across `50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950`.
  - Retired legacy tokens (`gray-green`, `coral`, `--boa-field`, `--boa-ink`, `#f2f2ed`, `--radix-popper`) must never be referenced.
- **Strict `#175174` commitment-CTA reservation vs monochrome `--yg-inverse`:**
  - **Primary OpenBoa Ink Blue (`--yg-brand` / `--yg-action` = `slate.700` `#175174`, `--yg-on-brand` = `neutral.50` `#FFFFFF`, `8.52:1` contrast in both Light and Dark):** Reserved exclusively for primary human commitment CTAs (`Confirm` / `Approve` / `Decline` confirmations in `ControlledInlineConfirm` for non-destructive governance actions, `Register Owner`, `Activate Strategy Release`, `Cut Checkpoint`, `Promote to Live`, `Resume`). Destructive governance CTAs (`Pause Company`, `Interrupt Run`, `Cancel Work`, `Demote Live`, `Rollback Strategy`) use `tone="danger"`. Routine employee retirement is CEO-delegated inside `ResidentCompanyService`—there is zero Board retirement CTA or write control anywhere in the Client.
  - **Readable brand ink (`--yg-brand-ink` / `--yg-action-ink` / `--yg-focus-ring`):** Adaptive `#175174` (`slate.700`) in Light and `#79B3D6` (`slate.300`) in Dark for text links, progress fills, `Chart` / `PortfolioChart` primary strokes, `6px` unread indicators, and `:focus-visible` rings.
  - **Unified dual-halo `:focus-visible` ring & border hover darkening:** Focus states MUST set `outline: none` paired with the single unified dual-halo ring (`box-shadow: 0 0 0 1.5px var(--yg-focus-ring), 0 0 0 3.5px var(--yg-focus-halo);`), preserving `@media (forced-colors: active)` `CanvasText` solid outline fallback. Interactive hairlines transition from resting `--yg-border-chrome` to `--yg-border-strong` on hover.
  - **Monochrome everyday controls (`--yg-inverse` = `#0A0A0D` Light / `#F4F4F1` Dark, `--yg-on-inverse` = `#FFFFFF` Light / `#0A0A0D` Dark):** Applied to everyday controls (`Button` neutral emphasis, `Switch`, `Checkbox`, `RadioGroup`, `Toggle`, `ToggleGroup`, `Tabs` active pill, `Composer` Send, standalone `Toast`).
  - **Opt-in secondary editorial accent (`--yg-accent` = `terracotta.600` `#A64F3C` Light / `terracotta.300` `#D88F7D` Dark):** Strictly `<= 1` region per viewport for non-operational editorial callouts; **never** render `Button[data-tone="accent"]`, focus rings, or operational statuses using `--yg-accent`.
- **Surface luminance polarity & separation:**
  - **Light (`neutral`):** `backdrop (#E5E5E0) < surface / surface-list / surface-inspector / field (#F4F4F1) < canvas == raised == input-surface (#FFFFFF)`; `hover (#E5E5E0) < field (#F4F4F1)`.
  - **Dark (`neutral`):** `backdrop / surface / input-surface / surface-list / surface-inspector (#0A0A0D) < canvas / raised (#131317) < popover / elevated / field (#1D1D22) < hover (#2A2A30)`.
- **Surface, elevation, and DOM discipline:**
  - **Zero CSS gradients:** `linear-gradient`, `radial-gradient`, and `conic-gradient` are forbidden across all stylesheets.
  - **Zero static shadows & banned `Y.Sheet` overlays:** Static `.yg-card`, `.yg-surface`, `.yg-receipt`, `.yg-trace`, `.yg-facts`, `.yg-composer`, `.yg-agent-row`, and `.yg-table-scroll` surfaces enforce `box-shadow: none` with `1px solid var(--yg-border-subtle)` hairlines. Elevation shadows (`--yg-float-shadow`, `--yg-panel-shadow`) apply strictly to transient overlays (`Toast`, `Popover`, `ActionMenu`, `ContextMenu`, `Select`, `Dialog`). **`Y.Sheet` (desktop right slide-over drawer) and right inspector panes are strictly banned across 100% of screens and breakpoints.**
  - **Opaque disabled filled buttons & `Switch` off-track contrast:** Disabled or `aria-disabled` filled buttons use opaque neutral step-down (`neutral.200` Light / `neutral.800` Dark + `--yg-text-disabled`, `opacity: 1`). Always reference semantic `var(--yg-off)` on `Switch` off tracks and verify `>= 3:1` contrast between `var(--yg-off)`, the adjacent surface, and `--yg-thumb`.
  - **Table header scoping:** Shade only `thead th, th[scope="col"]` (`background: var(--yg-surface)`); keep `tbody th` `background: transparent`.
  - **No raw HTML UI tags in consumer views:** Consumer React routes and domain compositions MUST NOT emit raw `<button>`, `<input>`, `<select>`, `<textarea>`, `<a>`, `<details>`, `<h1>`–`<h6>`, or `<p>` elements bypassing `@openboa/yogurt` or `@ui-sdk/react` primitives.
  - **Accessibility contrast floor:** Text roles maintain `>= 4.5:1` contrast and interactive control borders, focus rings, and state fills maintain `>= 3.0:1` across `neutral`, `paper`, and `mist` materials.

### 4. Spatial placement grammar and 5-state surface rules

- **Inline metadata proximity (`inlineMeta`), `AgentRow` cap, and `Badge` dot rules:** Place short status signals inline next to row titles, capped at **`<= 1` `Badge` plus optional `6px` unread dot** per row. Count+noun strings (`"3 causes"`, `"12 runs"`) pass `dot={false}` explicitly (or use `Count`). Constrain `AgentRow` entries to **`<= 44px` height** with `AgentAvatar` presence dot and `·` separator.
- **Trailing action isolation (`.yg-row-split`):** Split interactive row triggers from sibling `.yg-row-end` controls so interactive controls never nest.
- **Baseline/height parity & intrinsic button widths:** Controls aligned on one horizontal toolbar or form line match height tier (`[28, 32, 36, 44]px`) and keep intrinsic content width inside toolbars, cards, and confirmation wells.
- **Container-query responsiveness (`@container`):** Multi-pane views adapt via `@container` inline-size queries (`{360, 480, 720, 1024}px`) rather than viewport `@media` queries.

#### Five-state visual matrix across all routes

| State | Visual & Token Contract | Interactive / Command State | Honesty & Privacy Rule |
| --- | --- | --- | --- |
| **`empty`** | `EmptyResult` or `Result` inside `Card` explaining domain emptiness (e.g., core-only company `/agents`, `Live Execution Standby` on `/trading/live`). | Navigational links remain enabled; inapplicable domain routes unmounted (`TRADING_DOMAIN_MODULE` gated). | Never substitute numeric `0` or `$0.00` for missing close/cost figures or an inactive live tier. |
| **`loading`** | `Skeleton` (`role="status"`, `lines`) inside route body while keeping the **4-slot `sd-topbar` header** and `SidebarLayout` mounted. | Only initial cold load shows skeletons; routine refreshes keep verified `Data State` visible. | Never flashes or blank-wipes previously verified projections during background refresh. |
| **`error`** | Consolidated sub-header `Notice` (`tone="danger"`, `urgent={true}`, `role="alert"`) displaying HTTP status / `denial_code`. | On `401`/`403`/scope mismatch: immediate `clearData()`, `controls_locked = true`. | Zero stale cached financial/decision DOM survives `401`/`403` or scope change. |
| **`stale`** | Consolidated sub-header `Notice` (`"Live connection interrupted — showing last known snapshot"`) pinned below `sd-topbar` above preserved `Data State`. | `link_state = "disconnected"`, `controls_locked = true`; mutating buttons disabled. | Never blank-wipes verified `Data State` on transient UDS disconnect or daemon restart. |
| **`locked`** | **(a) `controls_locked`:** `SidebarFooter` `<Status>` + sub-header `Notice` naming lock reason (`bootstrap_locked`, `board_viewer`, `stale`, `unknown` command) + inline `CommandOutboxPanel`.<br>**(b) `host_locked`:** Full-viewport opaque Privacy Shield (`background: var(--yg-backdrop)`, `data-agent-hidden`). | (a) Read views active; mutating triggers disabled.<br>(b) Only unlock trigger active; unlock requires `GET /v1/meta` + projection verification. | Host lock MUST NOT alter `as_of_ns` or mark fresh data `stale`. |

### 5. Modular Company OS domain derivation

Every layout, header slot, sidebar group, route hierarchy, and domain composition derives strictly from Sourdough's **Modular IA** (`Approvals`, dynamic `Trading` `DomainModule`, `Company`, `System`) across the `17` mounted `App.tsx` route branches—without copying upstream example templates or hardcoding Trading assumptions into Core Company OS chrome:

- **Dynamic Domain Entry (`/` -> `TradingDeskView` vs `/agents`):** When `active_domains.includes("trading")`, `TRADING_DOMAIN_MODULE` mounts `/`, `/trading/live`, `/trading/paper`, `/orders/:orderId`, and `/trading/strategies`. When `"trading"` is absent (`homeMode === "core_only"`), `/` redirects (`replace: true`) to `/agents` and Trading navigation/routes are absent.
- **Strictly 4-slot `<header className="sd-topbar">` + `SidebarFooter`:** `sd-topbar` holds solely: (1) `Brand` (`SidebarTrigger` + Company link, zero `"Paper"` badge), (2) `Approvals` pill (`pendingApprovals > 0`), (3) `⌘K` Command Palette `Dialog`, and (4) `Theme Toggle`. Connection `<Status>` lives in `SidebarFooter` linking directly to `/audit?tab=daemon`.
- **Zero right slide-over drawers (`Y.Sheet` banned):** Selections transition in-place (`Tier 0 -> Tier 1`) with `Breadcrumb` (`<nav aria-label="Breadcrumb">`) and deterministic focus restoration on Back/`Escape`.
- **Board-vs-CEO boundary & `ControlledInlineConfirm`:** `/messages` is strictly a 1:1 writable Board ↔ Founder/CEO thread (`send_ceo_message`) plus read-only internal channels; `/reports` renders CEO-delegated HR custody read-only (`"Managed by CEO · Board view is read-only"`). Mutating governance/trading commands confirm inline via `ControlledInlineConfirm` contrasting **What changes** vs **What stays untouched** (`RecordFacts` `<dl>`) with technical details collapsed.

## Functional and quality requirements

- **FR.** Enforce `@openboa/yogurt` `0.2.0-local.1` `@layer` cascade, `Yogurt` root scoping, `Rethink Sans` + `--boa-font-mono` (`0.8125rem / 1.55`) + `tabular-nums`, `>= 12px` readable graph/list text, `21 x 11` palette, `#175174` commitment-CTA reservation, `{4, 6, 8, 12, 16, 20, 999}px` radii, strictly 4-slot `sd-topbar` header + `SidebarFooter` diagnostics, zero `Y.Sheet`/right drawers, `@container` breakpoints, and all five UI states (`empty`, `loading`, `error`, `stale`, `locked`).
- **NFR.** Guarantee zero CSS gradients, zero static card shadows, `outline: none` unified dual-halo focus rings, `--yg-border-chrome` -> `--yg-border-strong` hover darkening, opaque disabled filled buttons (`opacity: 1`), `var(--yg-off)` `>= 3:1` Switch off-track contrast, `AgentRow <= 44px` caps, `Badge` `dot={false}` on mixed count+noun labels, `thead th`-only table shading, intrinsic button widths, `>= 4.5:1` text / `>= 3.0:1` UI contrast, and zero raw HTML primitive bypasses.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Token, focus ring & CSS cascade audit | Run `npm --prefix apps/client run check:yogurt` and inspect compiled CSS. Confirm `@layer ui-sdk, yg.tokens, yg.base, yg.components, yg.patterns, yg.utilities;` is first, `0` gradients, `0` static card shadows, unified `outline: none` dual-halo `:focus-visible` ring, all radii within `{4, 6, 8, 12, 16, 20, 999}px`, `Rethink Sans` `.woff2` served locally, and `#175174` reserved for commitment CTAs. |
| Contrast, `--yg-off` & Light/Dark polarity | Render `neutral`, `paper`, and `mist` appearances in Light and Dark. Verify inputs, active sidebar pills, `Notice`, `CodeBlock`, `RecordFacts`, `Switch`, and `Table` rows maintain `>= 3.0:1` boundary and `>= 4.5:1` text contrast with `tbody th` unshaded. |
| 4-slot header, zero drawers, `@container` & 5 states | Audit viewport/container sizes (`360px`, `480px`, `720px`, `1024px`) and cycle `empty`, `loading`, `error`, `stale`, `controls_locked`, and `host_locked` across `/`, `/agents`, `/messages`, and `/approvals`. Confirm strictly 4 `sd-topbar` slots, `SidebarFooter` `<Status>` -> `/audit?tab=daemon`, zero `Y.Sheet`/right drawers, and `@container` reflow without horizontal clipping. |
| Non-imitation & raw-tag check | Audit built JSX/TSX consumer views and rendered IA tree. Confirm core-only `/` redirects to `/agents`, zero raw `<button>`/`<input>`/`<select>`/`<textarea>`/`<a>`/`<details>`/`<h1..h6>`/`<p>` tags bypass Yogurt primitives, and zero copied template layouts exist. |

**Oracle:** Cross-verify built client stylesheets and DOM snapshots against [Client build contract](../../build.md), [Verified Yogurt component bindings](../yogurt-components/spec.md), and [Client screens and navigation](../../inspect/screens/spec.md).

## Operations and change review

Any upgrade to `@openboa/yogurt` or `@ui-sdk/react` requires re-verifying CSS `@layer` precedence, palette step contrast (including `var(--yg-off)` `Switch` track states), concentric radius compliance, zero-drawer enforcement, and `npm --prefix apps/client run check:yogurt` before updating the pinned lockfile.
