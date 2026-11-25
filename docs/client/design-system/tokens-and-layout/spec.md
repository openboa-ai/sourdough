# Tokens, layout grammar, and presentation discipline

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client build contract](../../build.md) · [Component bindings](../yogurt-components/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Pin the exact `@openboa/yogurt` (`0.2.0-local.1`), `@ui-sdk/react` (`0.1.0-beta.13`), and `@ui-sdk/core` (`0.1.0-beta.13`) styling cascade, palette tokens, typography, geometry grids, spatial placement grammar, five-state visual rules, and domain-derived layout constraints for the React 19 Board Client. Presentation tokens and layout rules apply solely inside the Client build; no Python service, SQLite schema, or UDS control-plane handler imports or depends on UI stylesheets or layout state.

Input contracts: [Client architecture](../../architecture.md) · [Client build contract](../../build.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Base presentation](../../inspect/present/spec.md). These are public client and service contracts, not downstream dependencies from company systems onto UI assets.

## Design decision and review

### 1. Package pins, CSS `@layer` cascade, and root wrapper

- **Pinned package contract (`package.json` / lockfile only):**
  - `@openboa/yogurt`: `0.2.0-local.1` (`--target web` distribution only; `react` peer dependency `>=19.2.0 <20`)
  - `@ui-sdk/react`: `0.1.0-beta.13`
  - `@ui-sdk/core`: `0.1.0-beta.13`
- **CSS `@layer` import order:** Import `@openboa/yogurt/styles.css` (`design/styles/ui.css`) as the first stylesheet entry in the Client application. It establishes the explicit cascade:
  ```css
  @layer ui-sdk, yg.tokens, yg.base, yg.components, yg.patterns, yg.utilities;
  ```
  Every application-level layout stylesheet added by the Board Client MUST declare an explicit `@layer` (for example `@layer yg.patterns` or `@layer yg.utilities`) and never emit unlayered rules that override Yogurt component or focus-ring selectors.
- **Root wrapper (`@openboa/yogurt/yogurt`):** Wrap the Client shell in `<Yogurt appearance="auto" material="neutral">` (supporting operator selection of `appearance="auto" | "light" | "dark"` and `material="neutral" | "paper" | "mist"`), keeping `<div className="yg-root" data-appearance data-material>` as the CSS custom-property scope (`light-dark(...)` resolution and base typography).

### 2. Typography and quantitative scale grids

- **Typography (`design/tokens/brand.json`, `design/styles/ui.css`):**
  - **Primary typeface:** `Rethink Sans` bundled locally as variable `.woff2` font assets (`rethink-sans.woff2` normal and `rethink-sans-italic.woff2` italic, weights `400..800`, `font-feature-settings: "cv02", "cv03", "cv04", "ss01"`). Never fetch fonts from external CDNs.
  - **Role weights:** `heading: 520`, `body: 420`, `ui: 500`, `value: 540`, `wordmark: 700`.
  - **Type ramp & tracking:** Sizes `{cap: 10, caption: 11, help: 12, ui: 13, body: 14, card-title: 14, subheading: 16, section: 18, title: 24, value: 28, headline: 32}px` with paired tracking (`body -0.006`, `label -0.012`, `heading -0.022`, `display -0.03`, `meta 0.04`, `cap 0.06`, `value -0.024`).
  - **Monospace & tabular numerals:** Use `--boa-font-mono` (`ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace`) for digests (`sha256`, `payload_sha256`, `bundle_sha256`), IDs (`company_id`, `command_id`, `run_id`, `order_id`), and `CodeBlock` payloads. Apply `font-variant-numeric: tabular-nums` on every monetary amount, quantity, fill price, basis-point spread, revision, `as_of_ns`, `event_seq`, and `pending_age` counter.
- **Strict space scale (`--boa-space-*`):** `[2, 4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64]px`. Off-grid spacing values (such as `14px` or `18px`) are prohibited.
- **Strict even concentric radius grid (`--yg-radius-*`):** Strictly `{4, 6, 8, 12, 16, 20, 999}px`:
  - `inset: 4px` (nested inner badges/wells)
  - `small / chip: 6px`
  - `control: 8px` (compact inputs/select controls)
  - `menu / panel: 12px` (menus and nested subpanels)
  - `surface / card / popover / sheet: 16px` (`Card`, `Dialog`, `Sheet`, `Popover`, `Notice`, `Receipt`)
  - `feature: 20px` (elevated composer/drop regions)
  - `round / pill: 999px` (pill buttons, badges, search fields, toasts, segmented pills)
  Off-scale radii (`4.5, 5, 5.5, 7, 9, 10, 11, 14, 22, 26px`) are prohibited.
- **Control height tiers (`--yg-size-*`):** `[28, 32, 36, 44]px` (`compact: 28px`, `regular: 32px`, `row / comfort: 36px`, `touch / spacious / featured: 44px`).
- **Container breakpoints:** `{ compact: 360, narrow: 480, medium: 720, wide: 1024 }px` driving `@container` layout rules.

### 3. `21 x 11` Palette, CTA signal discipline, and surface polarity

- **Unified `21 x 11` (`231`-color) palette:**
  - `3` neutral families (`neutral`, `paper`, `mist`) + `18` chromatic families (`slate`, `blue`, `indigo`, `violet`, `purple`, `magenta`, `rose`, `red`, `terracotta`, `orange`, `ochre`, `yellow`, `lime`, `green`, `sage`, `teal`, `cyan`, `earth`), each stepped monotonically across `50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950`.
  - Retired legacy tokens (`gray-green`, `coral`, `--boa-field`, `--boa-ink`, `#f2f2ed`, `--radix-popper`) must never be referenced.
- **Strict `#175174` commitment-CTA reservation vs monochrome `--yg-inverse`:**
  - **Primary OpenBoa Ink Blue (`--yg-brand` / `--yg-action` = `slate.700` `#175174`, `--yg-on-brand` = `neutral.50` `#FFFFFF`, `8.52:1` contrast in both Light and Dark):** Reserved exclusively for primary human commitment CTAs (`Confirm` in `ControlledConfirmDialog` for non-destructive approvals, `Register Owner`, `Activate Strategy Release`, `Cut Checkpoint`, `Resume Risk`, `Release Quarantine`). Destructive governance CTAs (`Suspend New Risk`, `Pause Company`, `Revoke Grant`, `Retire Employee`, `Interrupt Run`, `Cancel Work`, `Quarantine Review Tool`, `Rollback Strategy`) use `tone="danger"`.
  - **Readable brand ink (`--yg-brand-ink` / `--yg-action-ink` / `--yg-focus-ring`):** Adaptive `#175174` (`slate.700`) in Light and `#79B3D6` (`slate.300`) in Dark for text links, `:focus-visible` rings (`1.5px` ring + `3.5px` `--yg-focus-halo`, with `forced-colors: active` `CanvasText` outline fallback), progress fills, `Chart` strokes, and `6px` unread indicators.
  - **Monochrome everyday controls (`--yg-inverse` = `#0A0A0D` Light / `#F4F4F1` Dark, `--yg-on-inverse` = `#FFFFFF` Light / `#0A0A0D` Dark):** Applied to everyday controls (`Button` neutral emphasis, `Switch`, `Checkbox`, `RadioGroup`, `Toggle`, `ToggleGroup`, `Tabs` active pill, `Composer` Send, standalone `Toast`).
  - **Opt-in secondary editorial accent (`--yg-accent` = `terracotta.600` `#A64F3C` Light / `terracotta.300` `#D88F7D` Dark):** Strictly `<= 1` region per viewport for non-operational editorial callouts; **never** render `Button[data-tone="accent"]`, focus rings, or operational statuses using `--yg-accent`.
- **Surface luminance polarity & separation:**
  - **Light (`neutral`):** `backdrop (#E5E5E0) < surface / surface-list / surface-inspector / field (#F4F4F1) < canvas == raised == input-surface (#FFFFFF)`; `hover (#E5E5E0) < field (#F4F4F1)`. Inputs, active sidebar pills, `Notice`, `CodeBlock`, `RecordFacts`, and `Table` rows must maintain clear edge/fill separation against `#F4F4F1` stages.
  - **Dark (`neutral`):** `backdrop / surface / input-surface / surface-list / surface-inspector (#0A0A0D) < canvas / raised (#131317) < popover / elevated / field (#1D1D22) < hover (#2A2A30)`. Inputs sit recessed (`#0A0A0D`) inside `#131317` cards.
- **Surface, elevation, and DOM discipline:**
  - **Zero CSS gradients:** `linear-gradient`, `radial-gradient`, and `conic-gradient` are forbidden across all stylesheets.
  - **Zero static shadows:** Static `.yg-card`, `.yg-receipt`, `.yg-facts`, and `.yg-table-scroll` surfaces enforce `box-shadow: none` with `1px solid var(--yg-border-subtle)` hairlines. Elevation shadows (`--yg-float-shadow`, `--yg-panel-shadow`) apply strictly to transient overlays (`Toast`, `Popover`, `ActionMenu`, `ContextMenu`, `Select`, `Dialog`, `Sheet`).
  - **Opaque disabled filled buttons:** Disabled or `aria-disabled` filled buttons use opaque neutral step-down (`neutral.200` Light / `neutral.800` Dark + `--yg-text-disabled`, `opacity: 1`), never low-opacity alpha washes.
  - **Table header scoping:** Shade only `thead th, th[scope="col"]` (`background: var(--yg-surface)`); keep `tbody th` `background: transparent`.
  - **No raw HTML UI tags in consumer views:** Consumer React routes and domain compositions MUST NOT emit raw `<button>`, `<input>`, `<select>`, `<textarea>`, `<a>`, `<details>`, `<h1>`–`<h6>`, or `<p>` elements bypassing `@openboa/yogurt` (`Heading`, `Text`, `Link`, `Button`, `TextField`, `Select`, `Textarea`, `DisclosureRow`) or `@ui-sdk/react` primitives.
  - **Accessibility contrast floor:** Text roles maintain `>= 4.5:1` contrast and interactive control borders, focus rings, and state fills maintain `>= 3.0:1` across `neutral`, `paper`, and `mist` materials.

### 4. Spatial placement grammar and 5-state surface rules

- **Inline metadata proximity (`inlineMeta`) & badge discipline:** Place short status signals inline next to row titles (`ListRow.inlineMeta`, `WorkEvent.inlineMeta`), capped at **`<= 1` `Badge` plus optional `6px` unread dot** per row (`Badge` precedes unread dot). Pure numeric counts omit decorative dots: `Badge`'s automatic `isNumeric` dot suppression matches bare numbers/digits only (`/^\d+$/`), so count+noun strings such as `"3 causes"` or `"12 runs"` MUST pass `dot={false}` explicitly (or use `Count`).
- **Trailing action isolation (`.yg-row-split`):** Whenever a `ListRow` is interactive (`href` or `onActivate`) and carries trailing `value` or `actions`, split the primary row trigger from the sibling `.yg-row-end` container so interactive controls are never nested.
- **Baseline/height parity & intrinsic button widths:** Controls aligned on one horizontal toolbar or form line (`FilterBar`, `ButtonGroup`, `Inline`) must match height tier (`[28, 32, 36, 44]px`). Action buttons (`Button`, `SDKButton`) keep intrinsic content width inside toolbars, cards, and dialogs—never stretching submit buttons full-width (`width: 100%`) across wide dialogs or toolbars.
- **Container-query responsiveness (`@container`):** Multi-pane inspectors, decision trace cards, and ledger splits adapt via `@container` inline-size queries (`{360, 480, 720, 1024}px`) rather than viewport `@media` queries so panels inside `SidebarLayout` or `Resizable` splitters reflow cleanly regardless of sidebar collapse state.

#### Five-state visual matrix across all routes

| State | Visual & Token Contract | Interactive / Command State | Honesty & Privacy Rule |
| --- | --- | --- | --- |
| **`empty`** | `EmptyResult` (search query active -> `"No matches"` + `"Clear search"`) or `Result` inside `Card` explaining domain emptiness (e.g., `"Paper profile not enabled on this core-only company"`). | Navigational links remain enabled; inapplicable commands hidden/disabled. | Never substitute numeric `0` or `$0.00` for an absent profile or empty collection. |
| **`loading`** | `Skeleton` (`role="status"`, `lines`) inside route body while keeping Persistent Global Status Bar mounted. | Only initial cold load shows skeletons; routine refreshes keep verified `Data State` visible. | Never flashes or blank-wipes previously verified projections during background refresh. |
| **`error`** | `Notice` (`tone="danger"`, `urgent={true}`, `role="alert"`) displaying HTTP status / `denial_code` and `Retry` action. | On `401`/`403`/scope mismatch: immediate `clearData()`, `controls_locked = true`. | Zero stale cached financial/decision DOM survives `401`/`403` or scope change. |
| **`stale`** | `Notice` (`tone="warning"`, `urgent={true}`, `role="alert"`) pinned above preserved `Data State` showing server `as_of_ns` and disconnect elapsed time. | `link_state = "disconnected"`, `controls_locked = true`; all mutating buttons disabled (opaque neutral step-down). | Never blank-wipes verified `Data State` on transient UDS disconnect or daemon restart. |
| **`locked`** | **(a) `controls_locked`:** `Status` / `Badge` (`tone="warning"`) on Global Status Bar + `Notice` naming lock reason (`bootstrap_locked`, `board_viewer`, `stale`, `pending`/`unknown` command).<br>**(b) `host_locked`:** Full-viewport opaque Privacy Shield (`background: var(--yg-backdrop)`, `data-agent-hidden`) replacing/concealing protected DOM. | (a) Read views active; mutating triggers disabled.<br>(b) Only OS/session unlock trigger active; unlock requires `GET /v1/meta` + projection verification. | Host lock MUST NOT alter `as_of_ns` or mark fresh data `stale`. |

### 5. Non-imitation and Sourdough domain derivation

Every layout, sidebar group, route hierarchy, and inspector pane is derived strictly from Sourdough's seven operational domains, 33 routes, four global shells, and `environment == "paper"` / `private_desk` / `offline-trial-registry-v1` boundaries:

- **Overview-first, never conversation-first:** The root route is `/overview` (company health, `charter_revision`, active `RestrictionView` causes, incidents, open reserved decisions, projection freshness)—never a 3-pane agent chat stream or bot/channel directory.
- **No assistant-directory or support-desk navigation patterns:** Top-level navigation reflects Sourdough's seven institutional areas (`Overview & Alerts`, `Decisions & Governance`, `Work & Runs`, `Organization & Collaboration`, `Paper Fund & Risk`, `Operations & Continuity`, `Offline Evaluations`) rather than chat threads, general assistant directories, customer-support consoles, plugin marketplaces, or generic multi-step workflow builders.
- **No desk-peeking drawers:** Active `.desk/drafts/*` remain opaque (`private_desk.manifest_revision`, `draft_count`, and `.desk/logs/` metadata only); no side inspector mirrors live agent drafts or screens.
- **No unconfirmed inline approval cards:** Every mutating decision routes through `ControlledConfirmDialog` with composite revision preconditions and `CommandOutboxPanel` receipt verification—never silent one-click inline approvals inside message streams.

## Functional and quality requirements

- **FR.** Enforce `@openboa/yogurt` `0.2.0-local.1` `@layer` cascade, `Yogurt` root scoping, `Rethink Sans` + `tabular-nums`, `21 x 11` palette, `#175174` commitment-CTA reservation, `{4, 6, 8, 12, 16, 20, 999}px` radii, `@container` breakpoints, and all five UI states (`empty`, `loading`, `error`, `stale`, `locked`) across every Client surface.
- **NFR.** Guarantee zero CSS gradients, zero static card shadows, opaque disabled filled buttons (`opacity: 1`), `thead th`-only table shading, intrinsic button widths, `>= 4.5:1` text / `>= 3.0:1` UI contrast, zero raw HTML primitive bypasses, and complete structural derivation from Sourdough's domain architecture.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Token & CSS cascade audit | Inspect compiled client CSS bundle and style sources. Confirm `@layer ui-sdk, yg.tokens, yg.base, yg.components, yg.patterns, yg.utilities;` is first, `0` gradients, `0` static card shadows, all radii within `{4, 6, 8, 12, 16, 20, 999}px`, `Rethink Sans` `.woff2` served locally, and `#175174` appears only on human commitment CTAs / readable brand ink. |
| Contrast & light/dark polarity | Render `neutral`, `paper`, and `mist` appearances in Light and Dark. Verify inputs, active sidebar pills, `Notice`, `CodeBlock`, `RecordFacts`, and `Table` rows maintain `>= 3.0:1` boundary and `>= 4.5:1` text contrast with `tbody th` unshaded. |
| Responsive container & 5-state matrix | Resize inspector and ledger panes across `360px`, `480px`, `720px`, and `1024px` container widths and cycle `empty`, `loading`, `error`, `stale`, `controls_locked`, and `host_locked`. Confirm `@container` reflow without horizontal clipping or stale blank-wipes. |
| Non-imitation & raw-tag check | Audit built JSX/TSX consumer views and rendered IA tree. Confirm zero raw `<button>`/`<input>`/`<select>`/`<textarea>`/`<a>`/`<details>`/`<h1..h6>`/`<p>` tags bypassing Yogurt primitives and zero non-Sourdough chat/channel/roster/marketplace layouts. |

**Oracle:** Cross-verify built client stylesheets and DOM snapshots against [Client build contract](../../build.md), [Verified Yogurt component bindings](../yogurt-components/spec.md), and [Client screens and navigation](../../inspect/screens/spec.md).

## Operations and change review

Any upgrade to `@openboa/yogurt` or `@ui-sdk/react` requires re-verifying CSS `@layer` precedence, palette step contrast, concentric radius compliance, and clean-build font bundling inside the Client workspace before updating the pinned lockfile.
