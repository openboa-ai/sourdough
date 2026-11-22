# Implementation architecture

**Scope:** a local company that can complete its first Agent-originated paper trade with the Board Client absent. This document fixes how modules fit together. The [responsibility map](../company-agent-quant.md) names the owners; each [unit spec](work-packages.md) defines its behavior. Design is not evidence of a working system.

## What runs where

```text
Human → Board Client → query / control API
                              │
┌─ Sourdough company service ─┴────────────────────────────────────────┐
│ Company: identity and grants → work scheduler → Agent Harness       │
│ Agent Harness → scoped tool → optional fund: admission and booking  │
│ Each owning module → its records in the company SQLite store        │
└─────────────────────────────────────────────────────────────────────┘
Harness ↔ runtime adapter ↔ Agent runtime (judgment, no book access)
Fund ↔ connector / execution adapter ↔ destination (original fills)
```

The entry point wires enabled modules, the SQLite store and adapters. Within the service, call the owning module's typed operation directly. Domain decisions take typed values; they do not import UI code, SQL rows or a provider SDK. The consumer defines the narrow interface it needs; the SQLite, Agent or venue adapter implements it. Only add an interface where an effect, a second implementation or an independent test requires one. No internal HTTP hops, generic repository per table, or message broker.

The base company can start and assign work without the fund profile. The fund may call company capabilities; company work does not import order types. A conversation can notify another Agent, but only an [accepted handoff](../company/systems/work/collaborate/handoff/spec.md) changes the duty owner. Likewise an Agent can request a trade, but cannot write a position. These rules hold even when modules share a process.

## Interfaces and trust

- **Human:** the service owns its versioned query, command and receipt contracts; the [native desktop Client](../client/build.md) consumes them, never the database. Queries authorize at read time and return source revision, as-of time and coverage. Commands require explicit intent, current authority and expected revision. A disconnected Client cannot queue a mutation for automatic replay.
- **Local delivery:** Goal 01–04 needs no public listener or GUI. In the Client unit, bind the same service contracts over authenticated, same-host IPC. Verify one real native query and one explicit command/receipt before adopting the concrete transport. A local connection is not identity. An unsupported command version produces no effect; a lost reply to an earlier command remains unknown until its own receipt is read.
- **Agent:** the [harness](../employee/agent/harness/protocol.md) binds the principal, duty, generation, tool scope and budget. Desk content, provider output and connector responses are data, not grants. No SDK hook, prompt, Client setting or company program can bypass admission. Bind paper execution separately from price data; never fall back to a live destination.

## One owner per fact, one commit per local transition

| Transition | Commit boundary | What stays outside |
| --- | --- | --- |
| [Found company](../company/systems/organization/found-company/bootstrap/spec.md) | Company, founder, grant, first duty and founding receipt together. | Opening an existing store is not founding. No model call or account creation in this transaction. |
| [Admit order](../company/profiles/quant/trading/admit-intent/reserve/spec.md) | Under account serialization, check scope and limits; commit frozen terms, submission identity and the subledger reservation together. | Dispatch only after commit. Never hold a write transaction during a model, network or venue call. |
| [Book fill](../company/profiles/quant/fund-books/book-fills/post/spec.md) | Only the subledger posts the identified fill, fee and remaining reservation, and records the pending accounting source together. | The destination owns fill and position evidence; the company may retain immutable observations for work and accounting. The [general-ledger bridge](../company/profiles/quant/fund-books/book-fills/journal-source/spec.md) delivers the source separately; a late accounting receipt cannot trigger another order. |

Enforce identities with scoped unique keys and relationships with checked foreign keys on every store connection. Compare immutable terms on replay, not just the key. Exact prices, amounts and quantities carry units and representable precision; reject ambiguous inputs before dispatch. Preserve source time and first availability separately when a decision uses dated evidence. Logs, balances cached by Risk, and Client projections are not alternative books.

**Example:** after the destination acknowledges an order, the company loses the reply. Retain the reservation and look up the original submission identity; a timeout is neither a rejection nor permission to send again. Two deliveries of the same fill must leave one posting. A different fill under the same order is not a duplicate. These are local correctness rules, not a claim that the later crash drill passed.

## Change, operate and verify

| Concern | Decision and evidence at the point of change |
| --- | --- |
| Data and release compatibility | Stamp the company store with its identity and schema revision at founding. Refuse a missing expected store or unsupported revision before scheduling work. Before the first schema upgrade, the storage owner supplies an ordered migration, a consistent checkpoint and an upgrade test from the last supported revision. Reopen it and compare identity, outstanding work and economic records. Roll back a binary only if it can read **and safely write** the effective schema; never rewind external effects by restoring an old file. [Restore contract](../company/systems/operations/restore-company/restore/spec.md) |
| Bounded operation | Start with one company, founder, paper account and instrument in the acceptance run. Give each turn a deadline and authorized spend limit; bound store waits and connector attempts. At the bound, retain the duty or unresolved effect; do not spin, spawn another worker or retry an uncertain write. Revisit concurrency or process placement only when measured queue age or lock waits violate a duty's declared bound. |
| Diagnosis and data handling | Correlate company, Agent, duty/run, action, destination order/fill and booking receipt. Report the last completed step, reason and next owner; absence of a receipt is not success. Log identifiers, revisions, timing and dispositions, not secret values, desk contents or raw model transcripts. Diagnostic logs may expire; they must not be the only copy of a receipt needed to settle a duty or the books. |
| Dependencies and code quality | At first use or upgrade, the adapter owner verifies the pinned release's public interface and runs the smallest contract test against it. A fixture verifies the consumer, not the installed SDK. Reuse the company interface across paper, replay and authorized external modes, not the destination binding or assumptions about fills. Do not add a framework for a hypothetical second deployment. |

**Review the change, not a universal checklist.** For each unit, name the changed owner and consumer, one invariant it must preserve, and the observation that would disprove it. A schema or public-contract change also names its compatibility path. Reuse the owning spec and PR; do not create a parallel design record.

**Order of proof:** deterministic CI checks the changed transition; the integrated [first-trade Goal](flow-delivery/goal-01-04.md) additionally needs one actual Agent request → independent paper fill → booked position on the same candidate. Subsequent shifts and fault recovery, measured net return, then comparison with the incumbent and separate RSI evaluation stay in their [own milestones](development-roadmap.md). A green test or profitable trade alone proves none of those later claims.
