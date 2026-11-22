# Q1 · Quant research and portfolio management

Design draft · [Company and quant architecture](../../../../README.md#system-hierarchy) · [Development lifecycle](../../../../engineering/lifecycle.md)

**Responsibility:** Owns point-in-time market observations, hypotheses, target exposure and reproducible strategy code. Proposing a target or registering code does not authorize an order.

## Data and experiments

[Historical backtest](backtest/intent.md) replays the same company trading contracts against an isolated account and pinned past observations; it does not replace current-market paper trading or the experiment registry.

### [Observe what was knowable at decision time](market-snapshot/intent.md)

Give the Agent and researcher the same market and account evidence available at a particular cut.

- [snapshot](market-snapshot/snapshot/spec.md) — Record source time, arrival time, revisions and missing coverage; a late correction must not leak into an earlier decision.

### [Test a hypothesis before trusting it](research/intent.md)

Compare an investment idea with a baseline on data not used to tune the strategy.

- [experiment](research/experiment/spec.md) — Seal the hypothesis, data windows, execution assumptions and costs. Replay the experiment and retain negative results and excluded observations.
- [compare-operating-revisions](research/compare-operating-revisions/spec.md) — After an initial net observation, register a matched prospective comparison of complete incumbent and candidate operating configurations; performance remains the return owner.
- [decision-note](research/decision-note/spec.md) — Bind an Agent observation and reproducible comparison to the decision that consumes it.

## Portfolio and strategy programs

### [Choose one account-level target](portfolio-target/intent.md)

Convert competing ideas into desired exposure without counting already committed capital twice.

- [adopt](portfolio-target/adopt/spec.md) — Version the target and derive a delta from current position and outstanding commitments. Unknown dependent exposure blocks an increase.

### [Operate company-owned trading code](strategy/intent.md)

Permit a program, not just a conversational Agent, to propose trades; editing code is not trading authority.

- [register](strategy/register/spec.md) — Pin source, dependencies and research evidence as a reproducible candidate with no deployment rights.
- [activate](strategy/activate/spec.md) — Start an approved simulated deployment under its own revocable grant; rollback fences the retired revision without abandoning its orders.
