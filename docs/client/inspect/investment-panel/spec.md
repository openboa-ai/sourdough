# Present investment evidence

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Load only when the quant profile (`paper` domain) is installed and `paper_binding.environment == "paper"` is verified. Render lineage, adapter telemetry, and reported values; the panel has zero venue credentials, zero live-trading UI flows, zero capital-transfer UI flows, and zero financial writers.

Input contracts: [Base presentation](../present/spec.md) · [Quant trace projection](../../../company/profiles/quant/fund-books/decision-trace/project/spec.md) · [Return projection](../../../company/profiles/quant/fund-books/fund-economics/projection/spec.md) · [Execution environment](../../../company/profiles/quant/trading/execution-environment/intent.md) · [Cancel order](../../../company/profiles/quant/trading/cancel-order/cancel/spec.md) · [Strategy comparison](../../../company/profiles/quant/investments/research/compare-operating-revisions/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Consume versioned projections over the daemon UDS Control Plane, never a finance SDK or direct SQLite connection.

1. **Strict Paper-Only Boundary (`environment == "paper"`):** Enforce and prominently display `environment: paper` sourced from `paper_binding` (`CHECK (environment = 'paper')`). Never render live-order entry, live-venue API key forms, or capital deposit/withdrawal flows.
2. **Swappable Execution Adapter Telemetry (`PaperDestination` / `PaperExecution`):** Bind order, fill, cancellation, position, and statement displays strictly to `PaperExecution` DTOs (`VenueExecution`, `VenueCancellation`, `VenuePosition`, `PaperStatement`), keeping venue internals behind the adapter boundary. Surface a read-only adapter telemetry card displaying `connector_revision` (`FixedQuoteVenue`, `nautilus-spot-sandbox-v1`, or `nautilus-offline-spot-replay-v1`), `destination_id`, `account_id`, `instrument_rules_digest`, `BoundedVenueCoordinator` segment state (`8 <= segment_seconds <= 600`, `drain_margin_seconds >= 5`, active `sequence` and `work_seconds`), `paper_sender_lease` lock state (`0o600` `.writer-lock`), and any active `paper_binding_change` transition with its exact `blocked_by` drain blocker (`"outstanding-company-effect"` | `"unverified-flat-destination"`).
3. **`cancel != flat` Partial-Fill Remainder Honesty:** When a `paper_cancellation` records `outcome = "cancelled"` with `filled_quantity > 0`, never render or infer a flat/zero position from the `"cancelled"` order status. Display **Order Terminal Outcome** (`cancelled` on unfilled `remaining_quantity`), **Filled Quantity** (sum of `paper_fill` rows), **Released Reservation** (`open_reservation_minor_units = 0`), and **Active Booked Position** (`booked_position > 0`, marked with an open-exposure indicator) as distinct fields.
4. **Two-Layer P&L & Honest Missing Coverage:** Present **Trading BOR** (`trading` / `trading_net` from `PaperStatement`) separately from **Agent LLM Operating Costs** (`operating_cost_atoms` / `WindowCosts`) and **Company Net** (`company` / `company_net`). Whenever `ReturnMeasurement.missing`, `WindowCosts.missing`, `CloseIssue`, or `paper_funding_settlement.status === "unavailable"` leaves `pnl_minor === null`, `time_weighted_return === null`, or `operating_cost_atoms === null`, render **`"Unavailable"`** in summary fact lists and **`"No reading"`** in table cells alongside the exact `missing` / `CloseIssue` reason tags—never coerce missing values to `0`, `$0.00`, or `0.00%`. Note that `trading` may be complete while `company` is `unavailable` due to an open `CloseIssue`. Frame time-series charts using percentage-return or cumulative-P&L-from-zero series (or explicit baseline framing paired with period delta columns) so high-baseline account values do not flatten visible movement. See [Swappable paper-only execution boundary](../../control-plane/paper-boundary/spec.md) and [Meta and authorized read projections](../../control-plane/projections/spec.md) for `as_of_ns` and DTO contracts.
5. **Flow 09 Promotion vs Flow 13 Separation:** Display Flow 09 `StrategyRelease` fields, paired `ChallengerTrial` verdicts (`PROMOTE | REJECT | INCONCLUSIVE`), `research_trial_count_n`, and `StrategyDeployment` forward rollback status within Paper Fund & Risk, while keeping Flow 13 `offline-trial-registry-v1` blind evaluations in the physically separated **Offline Evaluations** workspace.

**Concern for the responsible component:** Do not merge stale period values with a corrected cumulative revision, treat an unsupported profile or missing close as zero return, or mask an open partial-fill position behind a cancelled order status.

## Functional and quality requirements

- **FR.** Trace position to fill, fee, order, original decision, and next duty; display `environment: paper`, swappable adapter/segment/sender-lease state, `cancel != flat` partial-fill remainder exposure, and separate Trading BOR vs Company Net figures with units, coverage, `missing`/`CloseIssue` tags, and as-of time.
- **NFR.** Drill-down respects the principal and scope of each evidence reference. Normal company operation continues if the panel is absent.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Open the client only after the next shift for G5. For [P1](../../../acceptance/episodes.md#p1-total-return), capture the running screen and compare both series with the independent worksheet. |
| Partial fill + cancel | Execute a `0.002 BTC` order with `0.0008 + 0.0002 BTC` fills and `0.001 BTC` remainder cancellation. Confirm the UI shows `status = "cancelled"`, `filled = 0.001 BTC`, `open_reservation = 0`, and `booked_position = 0.001 BTC` (not flat). |
| Missing coverage | Open a window where Trading BOR is complete (`missing = ()`) and `accounting_close` has `CloseIssue("missing-cost-receipt")`. Confirm Trading BOR renders numeric values while Operating Costs and Company Net render `"Unavailable"` / `"No reading"` with the exact issue tag (never `$0.00`). |
| Failure or contention | Disconnect and change the period, or inspect a blocked `paper_binding_change`. Do not label cached figures as current, issue a venue refresh, hide `blocked_by`, or calculate missing values in the client. |

**Oracle:** Compare the rendered values, `environment == "paper"` badge, `cancel != flat` position, and visible `"Unavailable"` states to sealed query replies and independent operational and finance records.

## Operations and change review

Monitor projection revision mismatch separately from venue and book health. Roll back presentation, not financial state.
