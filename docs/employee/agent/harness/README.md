# A1 · Agent Harness and runtime adapters

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Verify the roster binding against registered adapter capabilities, execute turns and settle runs. [Configuration](../configuration/README.md) defines the profile, organization creates the Agent and authority issues credentials. The harness cannot rebind an existing Agent.

## Adapter registration and compatibility

### [Connect a conforming Agent Runtime](adapters/intent.md)

The same company tools can serve distinct Agents created on different admitted runtimes. A provider outage cannot silently turn one Agent into another.

- [register](adapters/register/spec.md) — Register a versioned runtime contract. Supply a text-only adapter for an effectful turn, disable its revision or omit required consent: record blocked selection, no silent downgrade or provider fallback.
- [qualify](adapters/qualify/spec.md) — Qualify distinct Agents on different runtimes. Deliver an old callback after a duty handoff, or a narrative claiming a tool effect with no receipt. Observe zero stale effects; deny silent fallback to a more privileged or billable provider.

## Scoped execution

### [Run a real Agent turn](execute/intent.md)

Execute the decision in an actual Agent session; generated text alone cannot establish a tool effect.

- [tool-turn](execute/tool-turn/spec.md) — Bind scoped tools, calls, usage and receipts to the work and current run. A consent prompt or missing receipt stays visible as blocked or unverified.
- [context-pack](execute/context-pack/spec.md) — Pin company context, method and visibility without relying on provider transcript state.
- [settle-run](execute/settle-run/spec.md) — Separate transport termination, durable work disposition and financial settlement.

**Cross-unit protocol:** [Agent Harness, adapter comparisons and qualification](protocol.md). The company owns the shift and economic effects; a provider owns only its bounded execution surface.
