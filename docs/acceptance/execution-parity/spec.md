# Qualify shared trading core and environment isolation

Design draft · **UNRUN** · Integration owner: trading core; independent observer checks both destinations and company records. [Environment intent](../../company/profiles/quant/trading/execution-environment/intent.md) · [Environment binding](../../company/profiles/quant/trading/execution-environment/bind/spec.md) · [External qualification](../../company/profiles/quant/trading/venue-qualification/testnet/spec.md)

## Claim

Paper, historical replay and external destinations must use the **same core artifact and domain contracts**, not different trading systems that happen to display similar results. Market-data and execution bindings are independent: a production market-data source may feed a paper account without granting access to production orders. This trial proves a scoped adapter/financial-core contract against isolated fixtures only. It does not establish identical real-market fills, latency, spread, liquidity, slippage, margin rules or failure behavior, and does not authorize external or live trading.

## Setup

Pin one build digest, identical starting company state and grants except the environment-scoped account and destination, instrument rules and policy revision. Set up two separately durable, independently observable destinations: the paper venue and an external-protocol emulator. Their common subset and deliberately differing capabilities are sealed in advance. Clone state into isolated trial companies/accounts; neither has access to actual external credentials or a live network destination. The observer, not the runtime, compares normalized records and destination histories.

The local protocol fixture runs in a separate process with its own ledger and no network or credentials. Its private wire fields differ from the paper core's types. On an accepted request with a lost reply, the fixture witness can independently see the order while the company-facing history cannot yet see it. The harness advances only the venue's evidence visibility; it does not modify company orders, reservations or holdings.

The partial-fill pair uses the managed paper adapter's independent journal and the wire fixture. In both cases a terminal cancellation arrives before the last identified fill. Keep the remainder reserved until the reported cumulative quantity matches unique fills; run reconciliation in a fresh company process at each cut and compare its original order, fees, exposure and final position with the independently persisted venue facts. This fixture does not replace an actual SDK observation.

The unfenced-absence case differs from a lost reply to an already accepted order. The emulator retains a delayed submission separately from its order ledger, so an independent reader sees an empty order ledger before delivery. A limited retry budget may expire, but neither emptiness nor timeout may release the reservation or authorize another send. If the original becomes visible during the budget, recover that order once.

## Scenarios and oracle

| Case | Required comparison |
| --- | --- |
| Normal and deny | Feed equal decision-time data and a fixed admitted order into both bindings. Independently compare the core artifact, authorization and risk decisions, reservation, action lineage, normalized fill, resulting position and cost postings for the declared common inputs. An unauthorized or over-limit request produces no destination write in either. |
| Partial, duplicate and late | Script identical partial fills, duplicate observations and terminal cancellation with reconciled cumulative quantity. Compare one posting per unique fill, one release of verified unfilled remainder and remaining exposure, including company restart. |
| Unknown and missing guarantee | Return timeout after acceptance and later reveal the original fill. A duplicate lookup must not trigger another submission. In a separate emulator case, an empty lookup lacks absent-order fencing: retain unknown and reservation even if the paper simulator can seal absence. A difference in declared capabilities is reported, not forced to pass as equivalent. |
| Binding isolation | Attempt paper-to-live credential or endpoint substitution, stale generation and cross-account history. Independent network/destination logs show no forbidden dispatch, no inherited position and no rewritten original binding. |
| Changeover with unresolved order | Admit an order on A, lose its reply, select B and restart. Query only A for the original ID, preserve its reservation until conclusive, and never replay on B. B may accept new work only under its own independently verified authority and risk policy; no account state is silently transferred. |

**PASS:** every common-contract case matches independently computed expected invariants, zero unauthorized or cross-environment effects occur, all missing destination guarantees remain explicit, and one actual core digest appears in both traces. Capture configuration, account/binding revisions, command origin, action/order/fill IDs, time-ordered records, destination calls and unrun cases. A missing observation is **INCOMPLETE**, an invariant breach is **FAIL**, and an unexecuted case is **UNRUN**.

The paper run and emulator establish only fixture-level parity. Record real adapter source/conformance, separately authorized testnet observations and any later live qualification as distinct evidence. The founder and newly hired Agent must each still pass their own uninterrupted paper integration; this suite cannot replace either.

The required CI entry point runs on macOS 26 Arm64, as required by the pinned engine. It replays a sealed historical tape in two isolated stores and compares the causal quote and resulting fill and balance. The same suite covers zero fills and partial cancellation. It requires neither an Agent subscription nor Binance credentials. Current-market operation and process-cut observations remain separate acceptance evidence.
