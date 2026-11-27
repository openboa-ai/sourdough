# Sourdough development roadmap · baseline v1.9

Design for review; product evidence **NOT_RUN**. [System boundaries](../company-agent-quant.md) · [Feature outcomes](feature-boundaries.md) · [Owning specs](work-packages.md)

**First deliverable: a working Sourdough system in which an employee Agent makes its first paper trade.** Build the minimum real implementation at each boundary, not a script that sends a broker order. Stop the first Goal at a confirmed fill and the corresponding booked position. Long-term company-net return is the business objective; neither profitability nor the quality of the Agent's strategy is this milestone's acceptance test.

## Operating slice

Company founding and mandate → due work → Agent on its pinned runtime → scoped order request → paper-account/risk admission and reservation → execution destination → confirmed fill → booked position. The Client may be closed. A fixed paper quote is sufficient. Do not confuse a fixture for a market price with a fixture making the Agent's decision. An inconclusive submission is not a fill; do not send a second order to make the test pass.

Start with one Python company service and SQLite transactions. Keep the Agent provider and execution venue behind their respective adapters. Sourdough configures and runs its selected trading runtime, NautilusTrader; an independently observable local paper venue can demonstrate the first connected trade without qualifying a specific external binding. Record the actual runtime, versions and mode. Do not build a new production matching engine, a message bus or a service per component. Independent macOS supervision and post-fill crash qualification belong to the follow-up reliability work. Live order credentials and capital remain blocked until separate venue qualification and explicit Board promotion ([flow-paper-live-migration](flow-paper-live-migration/spec.md), [flow-core-domain-cleanup](flow-core-domain-cleanup/spec.md)).

## Implementation order

| ID | Outcome | Evidence / dependencies |
| --- | --- | --- |
| 01 | Found the company, create its founder, grant and first duty. | Verify their identifiers through the normal company entry; distinguish initial setup from opening an existing store. |
| 02 | Run the first duty through the founder's real harness. | Link a scoped tool request to the authenticated Agent and duty. Local fixtures help wire the port; they do not qualify an actual Agent. |
| 03 | Connect one paper account and instrument to the fund path. | Link an authorized order, reservation, venue fill and position. Deny excessive exposure; duplicate notifications do not double-post. No live-market feed required. |
| 04 | Demonstrate the [first Agent paper trade](flow-delivery/goal-01-04.md) through the combined system. | One same-candidate trace from actual Agent to paper fill and resulting books. No forced second decision, market research benchmark, crash, or manual order. Stop here for Goal 01–04. |
| 05 | Extend trading into unattended shifts; qualify founder continuity separately. | Observe the next actual decision with the prior position. For the [founder resilience test](../acceptance/first-shift/spec.md), cut the company after a confirmed fill but before receipt; fence the former writer, reconcile once and resume without the board. Do not require this test for 04. |
| 06 | Hire a distinct Agent on demonstrated need and provision its scope, budget and desk. | [Hired-Agent continuity](../acceptance/hired-agent-continuity/spec.md) starts before the hire and follows that Agent's own research, paper fill, recovery and next duty; keep the non-trading case separate. |
| 07 | Qualify trading environments on the same company interface. | [Parity test](../acceptance/execution-parity/spec.md): historical replay, current-market paper and separately authorized external functional tests. Verify market source, execution destination and fill durability per binding. Testnet is not a performance reference. |
| 08 | Measure the first operating revision, including all company costs. | Register risk limits, dates, revision and current-market paper binding first. [Close the books](../acceptance/net-return/spec.md) against fees, funding, flows, marks and operating costs. A loss is measurable; missing coverage is unknown. |
| 09 | Test a candidate against the recorded incumbent. | Only after 08, compare isolated accounts in a new matched forward period with the same risk and accounting perimeter. Retain the incumbent if improvement is not demonstrated. |
| 10 | Complete the employee lifecycle. | [H1, W1, O1](../acceptance/episodes.md): personal work, selective sharing, accountable handoff and revocation. A message alone never transfers custody. |
| 11 | Add human projection/control and operational restore. | Read-only views do not operate the company. Suspension blocks new risk, not reconciliation of existing obligations. Restoring a store must not create a second sender. |
| 12 | Extend company tools and maintain the installed system. | Independently verify an asset before activation. Distinguish issue closure, release publication, local adoption, original defect retest and notification. |
| 13 | Evaluate Agent decisions and learning. | [L1](../acceptance/episodes.md) checks later use and rollback. Test [RSI](../acceptance/recursive-learning/spec.md) separately on unseen tasks at matched budgets. One profit or a model change is not proof. |

Later work is not a checklist for the first trade. Start recording authority, account isolation, order identity and price/cost basis when first used. Do not claim full qualification of any untested branch.

## Architecture convergence and Wave A delivery route *(Historical pre-cleanup log)*

*(Historical baseline note: Waves A–D below describe the initial pre-cleanup convergence route. Post-convergence Core Company OS vs Trading boundary separation, unified `v1/trading/` `trading_book_*` schema consolidation, `ResidentCompanyService`, Modular IA, and Board-gated `company_live` are governed by [flow-core-domain-cleanup/spec.md](flow-core-domain-cleanup/spec.md).)*

Converge the active flow branches onto the unified runtime and storage kernel in ordered waves so parallel implementation streams do not conflict on schema or scheduler plumbing:

1. **Close ready Flow 10 work first:** Merge ready `#160` into `feat/flow-10` before starting kernel convergence.
2. **Wave A & Wave B0 (`feat/flow-runtime-kernel`):** Deliver the unified SQLite storage kernel (`open_writer`/`open_reader`, WAL + `BEGIN IMMEDIATE`, canonical `v1` modular `STRICT` domain packs initially under `storage/schema/v1/{core,paper}/*.sql`—later consolidated to `v1/{core,trading}/`—with `PRAGMA user_version = 1`, `installed_domains` live schema verification and `!= 1` fail-closed semantics, WAL-normalized checkpoints and logical watermark cut checks), the three-entity Work kernel (`work_items`, `work_runs`, `work_responsibilities` + `work_conditions`), and unified runtime + `DutyRegistry` on `feat/flow-runtime-kernel` gated by `tests/flows/flow-runtime-kernel/test_flow.py`, preserving the separate two-process managed paper venue boundary (later standardized on `sender_lease` + bounded rotating segments). Pre-release changes edit canonical `v1` packs directly with zero incremental migrations until the complete working v1 product is assembled.
3. **Wave B (common company completion onto `main`):** Sequentially converge Flow 10 (Two-Plane Agent Harness with pinned macOS `sandbox_backend`, physically isolated per-Agent Desk drafts, and unified `work_peer_requests`), Flow 11 (unified checkpoint/restore adapters, core `command_receipt` + historical risk control gate later unified into `restriction_cause`, and Board Client hardening with Data/Link State separation and pre-submit pending-receipt persistence), and Flow 12 (deterministic `manage-change/activate` and `request-maintenance/track` on `core_v1` + `DutyRegistry`) onto `main`.
4. **Wave B-post, Wave C and Wave D:** Add the local UDS `/v1/` Control Plane daemon and switch Board UI transport behind its narrow adapter, then converge Flow 08 (cost-inclusive accounting close), Flow 09 (`StrategyRelease`, PIT catalogs, paired isolated company-arm comparison), and Flow 13 (physically separate evaluator SQLite store, itemized method deltas, 3-stage pre-commit gate, DAG quarantine rollback, blind position-swapped judging and two-generation RSI transfer), followed by full end-to-end verification and Core/Trading boundary cleanup on `main`.

## Development handoff

Sol is the development model for Goal 01–04. This does not select or authorize the employee Agent's runtime.

1. Verify this design with `shasum -a 256 -c docs/engineering/baseline-v1.9.sha256` from the repository root. This revision locks the pre-release canonical `v1` modular storage schema (`core` + opt-in `paper`, `PRAGMA user_version = 1`, zero pre-release incremental migrations) and common-company convergence order; the approved founding contract and first-trade finish line are unchanged. Retained manifests describe their historical trees: v1.8 at `7f632c4`, v1.7 at `a7b274a`, v1.6 at `d6ba0fab`, v1.5 at `1fbf55d`, v1.4 at `8865b9c`, v1.3 at `9e602d0`, v1.2 at `c19ed62`, v1.1 at `b6d7b53`.
2. Start with `01 / found-company/bootstrap`. Use the [first Goal](flow-delivery/goal-01-04.md), [work packages](work-packages.md) and [implementation conventions](flow-delivery/guide.md). Verify scope and accepted spec before product changes.
3. Implement a change that one consumer can observe. Run the affected case and the connected path available so far. Use one unit PR per reviewable behavior, not per file. Record the candidate revision, environment, expected/observed result, next action and owner in the flow Issue.
4. If something does not block the first trade or invalidate its evidence, leave it for its owning milestone. Report unavailable conditions honestly. No plugin, per-trade board approval, benchmark or live account is a prerequisite. Requirements revisions receive new manifests; never overwrite a historical manifest to make it pass.
