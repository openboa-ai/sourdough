# Present live trading and paper portfolio evidence

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Office Floor Map](../office-floor-map/spec.md) · [CEO Direct Line](../../governance/ceo-direct-line/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Serve as the **Primary Entry View (`/`, Live Trading & Portfolio)** plus its Tier 1 causal drill-downs (`/orders/:orderId`, `/performance`) when `active_domains.includes("paper")` in `GET /v1/meta` (`paper` installed) and `paper_binding.environment == "paper"` is verified. Client core-only vs. paper UI gating MUST check `active_domains.includes("paper")` (NOT `supported_commands` alone, because `LIVE_UDS` `src/sourdough/ipc/server.py:424-430` unconditionally returns `suspend_new_risk` regardless of `active_domains` since `paper_risk.py:330` calls `enable_domain_v1(connection, "paper")`). When `"paper"` is absent from `active_domains` on a core-only company, `/` degrades gracefully to an honest core-only company summary (`"Paper profile not enabled on this company"` — zero `$0.00` balance placeholders) alongside direct links to `/office`, `/ceo`, and `/company/approvals`. Render lineage, adapter telemetry, and reported values with zero venue credentials, zero live-trading UI flows, zero capital-transfer UI flows, and zero financial writers.

Input contracts: [Base presentation](../present/spec.md) · [Quant trace projection](../../../company/profiles/quant/fund-books/decision-trace/project/spec.md) · [Return projection](../../../company/profiles/quant/fund-books/fund-economics/projection/spec.md) · [Execution environment](../../../company/profiles/quant/trading/execution-environment/intent.md) · [Cancel order](../../../company/profiles/quant/trading/cancel-order/cancel/spec.md) · [Strategy comparison](../../../company/profiles/quant/investments/research/compare-operating-revisions/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Consume versioned projections over the daemon UDS Control Plane, never a finance SDK or direct SQLite connection.

### 1. Strict paper-only boundary & sealed valuation vs. live quote honesty

- **Strict `environment == "paper"` boundary (`[LIVE_UDS]`/`[V1_SQLITE_NEEDS_UDS]`):** Enforce and prominently display the persistent **`Paper`** mode marker sourced from `paper_binding` (`CHECK (environment = 'paper')`). Never render live-order entry, live-venue API key forms, or capital deposit/withdrawal flows.
- **Sealed window NAV vs. missing live intraday quote feed (`[V1_SQLITE_NEEDS_UDS]` vs. `[BACKEND_GAP #5]`):** In `origin/main` `6694fa9`, `paper_valuation.py:181-264` captures `trading_nav_atoms` only at discrete sealed operating-window slots or explicit `capture_paper_valuation()` calls; there is no continuous streaming live market quote or intraday unrealized mark-to-market feed in `CompanyDaemon` (`BACKEND_GAP #5`). The UI MUST label valuation timestamps clearly (`as_of_ns`, `valuation_source: "sealed_window" | "on_demand" | "live_quote" | null`) and render `"Unavailable"` / `"No live market tick"` when a live quote stream is absent—never fabricating intraday MTM ticks or `$0.00`.

### 2. 3-second 4-KPI top band (`/` primary landing surface)

Anchor the top band of `/` left-to-right on four executive KPIs formatted with one dominant `tabular-nums` primary line and muted `source · as_of_ns · coverage` subtext underneath:

1. **`Paper Portfolio Value & Return %`** (`[V1_SQLITE_NEEDS_UDS]` / `[BACKEND_GAP #5]`): Persistent **`Paper`** badge; latest verified Trading NAV + flow-adjusted TWR `%` (or `"Unavailable"` if valuation/interval coverage is incomplete).
2. **`Trading BOR P&L`** (`[V1_SQLITE_NEEDS_UDS]`): Trading Book-of-Record net P&L (`trading.pnl_minor`) net of venue fees (`paper_fill_fee`) and booked funding (`paper_funding_settlement`).
3. **`Agent LLM Opex`** (`[V1_SQLITE_NEEDS_UDS]`): Operating costs (`operating_cost_atoms` / `WindowCosts`) including Agent Harness/tool costs and cost per settled outcome where available.
4. **`Company Net P&L & Risk Gate`** (`[V1_SQLITE_NEEDS_UDS]` + `[LIVE_UDS]` Risk Gate): Consolidated net P&L (`company.pnl_minor`) paired with live Risk Gate status (`Active` / `New Risk Suspended` / `Paused` / `Restore Quarantine`).

### 3. Single canonical collapsible `Since Last Visit` brief (`Digest`)

Mount the single canonical collapsible **`Since Last Visit`** brief **only** on `/` directly below the 4-KPI band (persisting collapsed state locally per `(company_id, principal_id)`). Backed read-only by `GET /v1/projections/company-overview?since_seq=<int>` (`[BACKEND_GAP #8]`), it concisely summarizes overnight/since-last-visit deltas—completed work runs, filled/cancelled paper orders, pending approval count, newly published reports, and resolved/active restrictions—without duplicating navigation buttons inside the brief (pending approvals are surfaced cleanly via the left-sidebar `Approvals [N]` badge). High-fidelity live dynamic entry/transition polish for incoming fills and KPI ticks is required during implementation while honoring `prefers-reduced-motion: reduce`.

### 4. Two-Layer P&L reconciliation bridge, `0%`/`$0` baseline charts & `Unavailable != 0`

- **Two-Layer P&L reconciliation bridge (`[V1_SQLITE_NEEDS_UDS]`):** Present an explicit structured bridge on `/` and `/performance`:
  `Gross Trading Return` -> `- Venue Fees (paper_fill_fee)` -> `+/- Booked Funding (paper_funding_settlement)` -> `= Trading BOR Net` -> `- Agent LLM & Operating Opex (WindowCosts)` -> `= Company Net P&L`.
- **Honest missing coverage (`Unavailable != 0` / `"No reading"`):** Whenever `ReturnMeasurement.missing`, `WindowCosts.missing`, `CloseIssue`, or `paper_funding_settlement.status === "unavailable"` leaves `pnl_minor === null`, `time_weighted_return === null`, or `operating_cost_atoms === null`, keep verified partial subtotals readable (for example, `Trading BOR Net` remains numeric when `trading.missing = ()` while `accounting_close` holds `CloseIssue("missing-cost-receipt")`) and render **`"Unavailable"`** in summary fact lists and **`"No reading"`** in table cells alongside the exact `missing` / `CloseIssue` reason tags—never coerce missing values to `0`, `$0.00`, or `0.00%`.
- **`0%` / `$0` baseline cumulative chart with `null`-gap breaks:** Frame performance line charts on **Cumulative Return % (`0%` baseline)** or **Cumulative P&L (`$0` baseline)** with a dashed zero reference line (`trading` vs `company` series), never flattening high-baseline NAV values against zero. Break SVG line segments cleanly across `null` intervals (`ReturnMeasurement.missing` / `CloseIssue`) rather than interpolating across gaps.

### 5. `100–250ms` coalesced grouped causal execution tape & `cancel != flat`

- **Coalesced live execution tape (`[LIVE_UDS]` summary + `[V1_SQLITE_NEEDS_UDS]` detail + `[BACKEND_GAP #8]` `<1s` push):** Coalesce incoming `GET /v1/events` projection updates in `100–250ms` UI frames with locked burst sort stability (preventing row jumping while reading) and brief (`<= 400ms`) subtle new-fill highlights (`prefers-reduced-motion: reduce` safe).
- **Grouped `Decision -> Order -> Fill` rows & `cancel != flat` honesty:** Group child `paper_fill`, `paper_fill_fee`, and `paper_cancellation` rows under their parent `Decision -> Order` row with six distinct columns:
  1. **`Ordered`** (`ordered_quantity`)
  2. **`Filled`** (`filled_quantity`, sum of `paper_fill` rows)
  3. **`Remaining Open`** (`remaining_open_quantity = 0` / `remaining_quantity = 0` when terminal in `paper-fund-v1` `[V1_SQLITE_NEEDS_UDS]`; when rendering from `LIVE_UDS` `work-timeline` fallback, `work_projection.py:219` computes `remaining_quantity = ordered_quantity - filled_quantity` — the unfilled/cancelled remainder, e.g. `10` on S04 `20` ordered / `10` filled / `10` cancelled — and `work_projection.py:206` returns `price_minor_units = null` whenever `COUNT(p.fill_id) > 1`, so the Client MUST derive **`Remaining Open`** as `status === "unresolved" ? remaining_quantity : 0` and never treat multi-fill `price_minor_units = null` as `$0`)
  4. **`Order Status`** (`submitted | partially_filled | filled | cancelled | rejected | denied | expired`)
  5. **`Released Reservation`** (`open_reservation_minor_units`, `0` after cancellation)
  6. **`Resulting Net Position`** (`booked_position`, marked with an open-exposure badge whenever `> 0`)
  - For S04 (`20` ordered = `0.002 BTC` with `8 + 2` fills = `0.0008 + 0.0002 BTC` and `10` lots = `0.001 BTC` remainder cancellation): `LIVE_UDS` `work-timeline` returns `remaining_quantity: 10`, `open_reservation_minor_units: 0`, `price_minor_units: null`, `status: "cancelled"`, `booked_position: 10`, while `paper-fund-v1` returns `remaining_open_quantity = 0` (`remaining_quantity = 0`) when terminal. Applying `status === "unresolved" ? remaining_quantity : 0` on `LIVE_UDS` `work-timeline`, the tape renders `Order Status = "cancelled"`, `Filled = 0.001 BTC`, **`Remaining Open = 0`**, `Released Reservation = 0`, and **`Resulting Net Position = 0.001 BTC` (`cancel != flat`)**.
- **Zero-drawer full-screen causal drill-down (`/orders/:orderId`):** Clicking any order row transitions the main workspace view to `/orders/:orderId` (`Duty -> DutyRun -> ResearchFinding/Decision -> Order -> Fill -> Fee -> Cancellation -> Position -> Next Duty`), with structural `<nav aria-label="Breadcrumb">`, bidirectional links to the authoring `/office/agents/:agentId` and `/company/org/publications/:assetId`, `"Ask CEO about this item"` escalation to `/ceo`, and deterministic focus restoration on return.

### 6. Swappable `PaperExecution` telemetry & Flow 09 vs Flow 13 separation

- **Adapter telemetry (`/performance` & `/system/daemon`, `[V1_SQLITE_NEEDS_UDS]`):** Bind order, fill, cancellation, position, and statement displays strictly to `PaperExecution` DTOs (`VenueExecution`, `VenueCancellation`, `VenuePosition`, `PaperStatement`), surfacing read-only `connector_revision` (`fixed-quote-v1`, `nautilus-spot-sandbox-v1`, `nautilus-offline-spot-replay-v1`), `destination_id`, `account_id`, `instrument_rules_digest`, `BoundedVenueCoordinator` segments (`8 <= segment_seconds <= 600`, `drain_margin_seconds >= 5`), `paper_sender_lease` (`0o600` `.writer-lock`), and `paper_binding_change.blocked_by` (`"outstanding-company-effect"` | `"unverified-flat-destination"`).
- **Flow 09 vs. Flow 13 isolation:** Keep Flow 09 `StrategyRelease` / `ChallengerTrial` / `StrategyDeployment` inside `/company/strategy-trials`, physically separated from Flow 13 `offline-trial-registry-v1` under `/system/evaluations`.

**Concern for the responsible component:** Do not demote live paper trading beneath generic alerts, merge stale period values with a corrected cumulative revision, treat an absent profile or missing close as `$0.00`, fabricate live intraday ticks where only sealed window NAV exists, or mask an open partial-fill position behind a `"cancelled"` order status.

## Functional and quality requirements

- **FR.** Serve `/` as the primary entry cockpit with the 3-sec 4-KPI top band, collapsible `Since Last Visit`, `0%`/`$0` baseline cumulative chart with `null`-gap breaks, Two-Layer P&L reconciliation bridge, `100–250ms` coalesced grouped causal execution tape (`<= 400ms` highlight, `cancel != flat` 6-column display), and full-screen `/orders/:orderId` + `/performance` drill-downs; degrade cleanly on core-only companies.
- **NFR.** Enforce `Unavailable != 0` / `"No reading"` honesty, distinguish sealed window NAV (`V1_SQLITE_NEEDS_UDS`) from absent live intraday quote streams (`BACKEND_GAP #5`), preserve principal/scope redaction, and keep normal company operation independent of Client availability.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal & 3-sec scan | Open `/` after G5 / [P1](../../../acceptance/episodes.md#p1-total-return). Confirm 4-KPI top band renders `Paper` badge, `Paper Portfolio Value & Return %`, `Trading BOR P&L`, `Agent LLM Opex`, `Company Net P&L & Risk Gate`, single collapsible `Since Last Visit`, and matching Two-Layer P&L figures against the independent worksheet. |
| Partial fill + cancel (`cancel != flat`) | Execute a `0.002 BTC` order with `0.0008 + 0.0002 BTC` fills and `0.001 BTC` remainder cancellation. Confirm the grouped tape shows `Ordered = 0.002`, `Filled = 0.001`, `Remaining Open = 0`, `Order Status = "cancelled"`, `Released Reservation = 0`, and `Resulting Net Position = 0.001 BTC` (not flat). |
| Missing coverage & `null`-gap chart | Open a window where Trading BOR is complete (`missing = ()`) and `accounting_close` has `CloseIssue("missing-cost-receipt")`. Confirm Trading BOR renders numeric values while Agent LLM Opex and Company Net render `"Unavailable"` / `"No reading"` with exact `CloseIssue` tags (never `$0.00`), and the `0%`/`$0` baseline chart breaks SVG line segments across `null` gaps. |
| Core-only fallback & live-quote gap | Open `/` on a core-only store (no `paper` domain) -> confirm `"Paper profile not enabled on this company"` without `$0.00` balances; on a `paper` store without `BACKEND_GAP #5`, confirm sealed valuation `as_of_ns` + `"No live market tick"` honesty. |

**Oracle:** Compare rendered values, `Paper` badge, `cancel != flat` position, `0%`/`$0` chart segments, and `"Unavailable"` states against sealed query replies and independent operational/finance records.

## Operations and change review

Monitor projection revision mismatch separately from venue and book health. Roll back presentation, not financial state.
