# Replay a pinned strategy and historical feed

Design draft · Verification **UNRUN** · Responsible component: quant replay runner; existing company trading, investment book and performance contracts own their respective decisions and records. [Feature intent](../intent.md) · [Experiment registration](../../research/experiment/spec.md) · [Shared environment binding](../../../trading/execution-environment/bind/spec.md)

## Boundary and requirements

**FR-1:** Before a run pin strategy and shared trading-core digests, dependency versions, isolated company/account and grants, dataset provenance and immutable content digest, source availability-time policy, time range, warm-up, train/tune/holdout boundaries, virtual clock, initial capital and assumptions for fills, spread, fees, funding, margin and risk. The experiment registry preserves every attempted variant and evaluation window; the runner does not choose a favorable period after observing results.

**FR-2:** Replay in causal order using the same policy, admission, order, reconciliation, ledger and return interfaces as forward paper and external execution. A deterministic simulated venue and isolated company store cannot send external orders or change an operating company. At each decision expose only observations known by that simulated time. If historical publication/arrival times are unknown, record and justify the assumption; prohibit a claim of proven point-in-time availability. A bar's final values cannot be read before the bar closes.

**FR-3:** Persist per-decision inputs, action identity, order/fill lineage, clock and source coverage, fees, funding, unfilled outcomes, booked or explicitly modeled operating costs and report revision. Account for all strategy revisions and failed trials. Apply the existing cost-inclusive return calculator; where actual past model or operating cost is unavailable report the assumptions and keep it distinct from billed actuals.

**NFR:** Identical pinned inputs, clock, random seed and execution model produce independently reproducible records; any nondeterministic Agent/model calls must be captured as recorded inputs or separately reported as non-reproducible, not silently treated as deterministic. Missing data, unsupported order types or unknown costs produce incomplete or invalid results, never favorable synthetic fills.

## Independent cases

| Case | Expected observation |
| --- | --- |
| Reproducible causal replay | Using independently sealed input, expected fills and cost worksheet, replay the same revision twice and compare event order, exposure, costs and return. A strategy output cannot read a future bar, label, post-decision correction or another run's private state. |
| Unavailable history | Remove a source interval, publication-time evidence or funding input. Preserve the gap and report the affected metric or as-of claim incomplete. No real or testnet order is emitted. |
| Revision and windows | Evaluate a separately registered candidate without using its already observed tuning period as new holdout evidence. Retain the incumbent, all failed variants and the original observation period. |

Replay is historical, not prospective. A passing replay cannot satisfy a current-market paper observation or independently establish investment returns.

## First executable slice

The managed runtime can bind an isolated replay account to a supplied quote tape by its exact content digest. The tape specifies instrument terms, quote event times and later availability times; missing, unordered or off-increment observations are refused rather than corrected. NautilusTrader's offline backtest engine drives the replay clock and executes the same company-admitted order contract as the forward sandbox. The replay receipt preserves the observed event and decision availability cut, original fill and independent virtual account totals.

Opening research reads the price and the replay generation's sealed availability in one observation. The company selects only research records available at that instant, rounding down when the research source expresses availability in whole seconds. Persist the tape revision, event time and exact availability cut alongside the sealed inputs. A missing or inconsistent replay cut fails closed; the wall clock is not an alternative for selecting historical observations. The wall clock still records operational receipt times and does not authorize a later historical strategy decision.

The offline worker holds the sealed cut while it waits for a command, bounded by the configured activation duration. Live market freshness rules still apply to live-paper observations, not to a paused historical tick. Pausing does not advance simulated time, import another quote or authorize a command from another generation.

The first bounded observation uses one decision, one spot instrument, a full fill and an explicit zero-fee test policy. It does not qualify a complete evaluation window, operating costs or an economic result.
