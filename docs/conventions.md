# Design conventions

Applies to every system diagram, intent, development spec, acceptance scenario and proposed interface. [Glossary](glossary.md) · [Architecture](architecture.md) · [Development lifecycle](engineering/lifecycle.md).

## State the situation before the solution

Every feature intent links to one primary [business situation](capabilities.md); its development units inherit it through that intent. Start with **Company** (continuing obligation, assets, authority) and **Employee** (judgment, accepted duty, accountability). Then identify the company system or Agent capability that implements a responsibility. Only then bind a business profile, connector, provider, UI or test fixture if needed.

Name the trigger, accountable party, permitted alternatives including inaction, durable outcome, outstanding responsibility and independent evidence. Not every situation starts with a hire, ends in an external write or includes a crash. A technical unit such as adapter registration supports staffing but does not hire anyone. Keep domain-specific invariants in their profile rather than weakening them into generic statements.

## Name one fact once

- Pick the actor, owner, lifetime and stable identity before naming an entity. Use **Agent** for one actor created with a pinned execution profile. Never use “replaceable” to mean interchangeable identity: restart a session on the same binding, configure a new Agent on another, or hand a company duty to a successor. Record which occurred. Hiring, staffing and retirement describe its organizational lifecycle. **Agent Harness** is the product coordinator, **Agent Runtime** the execution environment and **test driver** the acceptance tool. Do not use “employee” as a second technical identity.
- Company-managed software is operated by the company even if authored elsewhere; an external service is operated by another party and may own independent source records. A company-owned program can be Agent-authored or adopted. Neither origin nor a sample role dictates system ownership, deployment layout or a fixed department.
- Humans query or control the company only through Client. Client absence must not prevent authorized Agent work, unattended recovery or domain-owned control. Distinguish command submission, authorization, application and receipt.
- Use nouns for entities and read models, verbs for commands and past-tense facts for events. Examples: `Agent`, `WorkTimeline`, `SuspendNewRisk`, `RiskSuspensionApplied`. In proposed records use scoped identifiers such as `company_id`, `agent_id`, `work_id`, `run_id`, `request_id` and `revision`. State identity and replay semantics explicitly.
- Qualify ambiguous terms: *feature intent*, *order intent*, *runtime adapter*, *resource connector*, *company general ledger*, *investment subledger*. A **projection** is service-owned read data, **presentation** is rendering and a **command receipt** records a request's disposition.
- Add an entry to the glossary when a term changes owner or meaning. A business synonym belongs in that entry, not in a second entity, service or directory. Preserve external API and source names.

## Organize by ownership, not technology

- The repository is the product. Start at the [map](README.md); do not add a product-name wrapper under `docs/`. Company owns organization, authority, shared work/assets, connectors, finance, accounting and health. Agent owns execution, desk and learning capabilities, not legal ownership of the assets. Company and Employee are the semantic entry points: company systems implement the former and an Agent realizes the latter.
- Under the technical owner (`docs/company/systems`, `docs/employee/agent`, `docs/company/profiles/quant` or `docs/client`), put a feature at `<capability>/<feature>/intent.md` and its changes at `<feature>/<change>/spec.md`. When a capability is itself one feature (e.g. Agent Desk), put the intent directly there; never repeat the name to pad the hierarchy. `acceptance/` holds cross-domain trials and `engineering/` holds delivery practices, not operating capabilities.
- Each feature has one parent and one `intent.md`; each reviewable unit has one `spec.md`. Share contracts by link, not duplicate definitions. Agent Learning owns candidates, evaluation and per-audience method selection. Company grants determine who may adopt for a wider audience; the company remains custodian of shared artifacts.
- Quant may consume company and Agent contracts. No base spec may require a quant account, instrument or order schema. The client consumes service **query and command contracts**. No service, projection producer, control handler or recovery job depends on client implementation or UI assets. An end-to-end screen observation is not an input to financial reconciliation.
- In each spec, **Input contracts** are prerequisites, not downstream consumers. Draw runtime calls, ownership and build dependencies separately. A callback or link to a client test does not introduce a client dependency.
- System diagrams show actors, runtime components, durable state, failure boundaries and **named flows**. Keep design systems, component libraries, install steps and build tools in the consuming client's build notes, not system or operating-episode diagrams. A capability domain need not be a separate process.

## Views are not additional owners

Use the [architecture atlas](views.md) for a different question about the same fact. Keep feature requirements in one intent and one spec per change; reference them from the [capability index](capabilities.md), never fork them into an organization, deployment or UI copy. A new feature needs an owning system, a functional entry, and a check of the journeys, boundaries and evidence it changes.

## Boundary review

Identify the owner, authoritative record, inputs, effects, failure outcome and independent evidence. Can the company continue if the client closes? Can a query mutate state? Can a stale generation act? Does the base operate without the fund profile? Does adoption have a grant for the intended audience? Approval of a draft, successful build and observed operation are distinct claims.

For each feature intent state its operating need, accountable owner, boundary, observable outcome and downstream consumer. For each reviewable spec state input and effective authority, one owning record or transition, failure behavior and an independently observable acceptance case. Reuse the owning contract by reference rather than copying its rules. Separate process deployment is not implied by a logical owner; do not impose trading failure machinery on unrelated reads. A necessary distinction remains when merging it would lose ownership, safety or an observable outcome.
