# Worked example: a newly hired Agent at a paper fund

[Role-independent architecture atlas](../../views.md) · [System map](../../README.md) · [Feature index](../../capabilities.md) · [Glossary](../../glossary.md)

## The case shared by all eight views

The [role-independent integration contract](spec.md) follows **hire → evidence-based judgment → durable result → crash → verified recovery → next duty**. These eight drawings illustrate its paper-execution binding, not a job description for all Agents. In the company-only binding the durable result is an assessment revision and the next duty reads that revision; there is no venue, order or position.

A manager operating under the standing mandate judges that a hire is warranted. Organization hires **Agent A** on immutable execution profile **P**. A accepts a general observation duty **W1**, chooses a research question, publishes a finding, and, in the illustrated branch, decides to propose a **simulated** order. The independent venue commits a full fill. The company service dies **before receiving either reply or notification**. It must recover the position and the same A/P must make a fresh decision on a distinct duty **W2**. The board and client are absent throughout normal operation.

These are example correlation labels, not implementation IDs. **P** is the Agent's execution profile; the paper account binding is separate. Hiring and trading are discretionary: declining to hire or choosing to hold can be sound decisions, but do not exercise this branch. This is a joined **design walkthrough, UNRUN**. [H1](../episodes.md#h1-hiring) tests an actual hire; the mandatory [G1–G5](../first-shift/spec.md#first-integration-run) starts from an existing founder. Neither their separate results nor this diagram proves a hired Agent completed both in one run.

[1 Domain](#company-model) · [2 Capabilities](#business-capabilities) · [3 Authority](#decision-and-write-authority) · [4 Components](#logical-components-and-interfaces) · [5 Runtime](#session-recovery-versus-succession) · [6 Information](#records-and-lineage) · [7 Deployment](#local-fault-boundaries) · [8 Evidence](#quality-scenarios) · [Boundary review](#follow-one-decision-across-views)

## Company model

**1 · Context and domain.** Arrows are business relationships, not calls. A role may pass to someone else; authorship and identity do not. Company custody of a finding does not give every Agent access to its author's private desk.

```mermaid
flowchart TD
    Board["Board"] -->|sets standing mandate| Firm["Company"]
    Firm -->|delegates staffing to| Manager["Hiring manager"]
    Manager -->|justifies hire| A["Agent A · profile P"]
    Firm -->|owns| W["Duties W1 and W2"]
    A -->|accepts and decides on| W
    A -->|authors| F["Research finding"]
    Firm -->|retains custody of| F
    F -->|informs but cannot authorize| Proposal["Paper order proposal"]
    Proposal -->|only if admitted and submitted| Venue["Independent simulated venue"]
    Venue -->|establishes| Fill["Venue fill"]
    Fill -->|changes after posting| Position["Company position"]
    W -->|next decision considers| Position
```

The manager acts for the company, not the board. A new session on P can serve A; an alternative runtime requires another Agent. Neither a market venue nor a model provider is an officer of the company.

## Business capabilities

**2 · Outcomes.** These are abilities, not a fixed department chart or a script for the model. Two branches matter: a justified hold has no order to recover, and an unknown order has no proven fill to book. [All capabilities](../../capabilities.md).

```mermaid
flowchart TD
    Need["Recognize a staffing need"] -->|hire warranted| Ready["Hire and reach first accepted duty"]
    Need -->|no hire| Other["Reassign, automate or defer"]
    Ready --> Find["Choose a question and publish research"]
    Find --> Decide{"What does the evidence support?"}
    Decide -->|hold| Review["Record reason and next review"]
    Decide -->|trade| Paper["Admit risk and submit a paper order"]
    Paper --> Cut["Recover after venue fill and company crash"]
    Cut --> Known{"Original order outcome known?"}
    Known -->|no| Pending["Keep reserve and schedule lookup"]
    Known -->|confirmed fill| Books["Restore position and remaining exposure"]
    Books --> Next["A makes a fresh W2 decision"]
```

## Decision and write authority

**3 · Who may write which fact?** Arrows identify the decision maker or authoritative writer, not deployment boundaries. C/A/Q labels refer to the [ownership matrix](../../README.md#system-hierarchy).

```mermaid
flowchart LR
    Manager["Delegated manager"] -->|requests hire| Org["C1 Organization"]
    Auth["C2 Authority"] -->|delegation and scope| Org
    Fin["C8 Finance"] -->|budget envelope| Org
    Org -->|commits hire with bounded grant and allocation| Roster["Roster: A bound to P"]
    Agent["A under current grant"] -->|authors| Finding["Q1 finding and decision evidence"]
    Agent -->|proposes, cannot dispatch directly| Risk["Q2 Trading"]
    Auth -->|current paper permission| Risk
    Risk -->|admits reservation and original submission| Order["Paper order record"]
    Venue["Simulated venue"] -->|writes external fact| Fill["Fill"]
    Fill -->|confirmed evidence| Book["Q3 Fund Books: post once"]
    Book -->|mapped cash source| GL["C7 Accounting: post journal"]
    Work["C3 Work"] -->|owns claim and next wake| Duty["W1 then W2"]
```

A hire, title or finance allocation does not grant paper dispatch. The illustrated hire requests explicit paper scope within delegated limits; Q2 still checks that scope, account, mandate, evidence and remaining risk when the order is admitted. Without it, research can succeed with zero submissions.

## Logical components and interfaces

**4 · Which contracts cross the boundaries?** Arrows are calls or returned evidence. Company Work can run without Quant; enabling Quant registers tools. The connector is not an order approver, and the Board Client is not an operational dependency. [Component contracts](../../architecture.md#dependency-and-authority-boundaries).

```mermaid
flowchart TD
    Org["Organization"] -->|ready hire and first duty| Work["Company Work"]
    Work -->|claim A/P and generation| H["Agent Harness"]
    H -->|scoped context and tools| RT["Bound runtime for A"]
    RT -->|typed tool request| H
    H -->|registered research tool| Research["Quant Investments"]
    H -->|order tool with finding and decision| Trade["Quant Trading"]
    Trade -->|admitted submit or original lookup| Gateway["Company Connector"]
    Gateway -->|paper adapter| Venue["Simulated venue"]
    Venue -->|external observations| Gateway
    Gateway -->|original order evidence| Trade
    Trade -->|post confirmed fill by key| Fund["Fund Books"]
    Fund -->|posting receipt and position revision| Trade
    Trade -->|durable disposition for W1| Work
    UI["Optional Board Client"] -.->|query or explicit governance command| API["Projection and control APIs"]
```

The harness executes A's tool requests; it neither authors the research nor writes the financial record. The APIs expose service-owned records and authorized commands. Closing the client cannot interrupt a run or suppress a wake.

## Session recovery versus succession

**5 · Runtime behavior.** Read top to bottom. The response deliberately never reaches the original company process. A restart is not an order retry. The fresh work ID, not just the new process or run generation, identifies the next shift.

```mermaid
sequenceDiagram
    participant M as Delegated manager
    participant C as Organization, Work and Harness
    participant A as Agent A on P
    participant Q as Quant services
    participant V as Independent paper venue
    participant S as Product supervisor
    M->>C: Hire within scope and budget
    C->>A: Start bound turn for W1
    A->>C: Accept duty, choose question
    A->>Q: Research via harness and publish finding F
    Q-->>A: Return F and its digest
    A->>C: Record decision D citing F, action ID and next condition
    A->>Q: Via harness propose paper order for D
    Q->>Q: Admit risk and persist original submission O before sending
    Q->>V: Submit O through connector
    V->>V: Commit fill X and withhold all replies
    Note over C,Q: Company process exits before observing X
    S->>C: Relaunch on original store and fence old writer
    C->>Q: Resume unresolved O
    Q->>V: Look up O on original account without resending
    V-->>Q: History and account sync both report X
    Q->>Q: Apply X once and expose posting receipt and position
    Q-->>C: Return original order disposition and posting receipt
    C->>C: Complete W1 and arm the saved condition
    C->>A: Start distinct W2 on P when saved condition is due
    A->>C: Read recovered position and record fresh decision and next duty
```

If P is unavailable, block A's turn; Q2 can still reconcile O. A separately authorized successor can accept an explicit [handoff](../../company/systems/work/collaborate/handoff/spec.md), but cannot become A or pass A's W2 check. A delayed callback from the old generation cannot reopen W1.

## Records and lineage

**6 · What proves causality?** Arrows are durable references, not steps that can be recreated from a transcript. An auditor must be able to follow the decision backwards to the data A saw and W2 backwards to the posted position. [Research receipt](../../company/profiles/quant/investments/research/decision-note/spec.md) · [Fill posting](../../company/profiles/quant/fund-books/book-fills/post/spec.md).

```mermaid
flowchart TD
    Hire["Hire receipt H: Agent A / profile P"] --> W1["Accepted W1, run generation"]
    W1 --> F["Finding F: author, input cut, digest, calculation"]
    Snap["As-of snapshot and cost assumptions"] --> F
    F --> D["Decision D: rationale, action ID, next condition"]
    D --> O["Original submission O: account binding, terms, reservation"]
    O --> X["Venue evidence: venue, account, fill X"]
    X --> B["Unique posting B: quantity, fee, open remainder"]
    B --> J["Mapped economic source for company journal"]
    B --> Pos["Position revision"]
    Pos --> W2["Fresh decision: A/P, distinct W2"]
    D -->|saved review condition| W2
```

A matching finding ID alone is insufficient: independently reproduce F and check that D used its implications. Preserve the original attribution after recovery; a position snapshot alone does not show why the company traded.

## Local fault boundaries

**7 · Deployment and survival.** Boxes are failure units, not teams. The first cut kills **one company process**, not the whole machine, provider, supervisor, or venue. Restart must use the existing store and account; opening an empty company is not recovery.

```mermaid
flowchart TD
    subgraph Host["Local MVP host"]
        S["Supervisor survives"] -->|relaunch after observed exit| C["Company service process: Org, Work, Harness, Quant"]
        C -->|exclusive writer| Store[("Company store survives: H, A/P, W1, F, O, next condition")]
        UI["Optional client closed"] -.->|not required| C
    end
    C -->|bounded session, no venue credentials| RT["Runtime for profile P"]
    C -->|connector submits or looks up O| V["Independent simulated venue process"]
    V --> VB[("Venue store survives: O and committed X")]
    Fault["Fault injector: after X, before company receipt"] -.->|kill only this process| C
```

A machine/storage loss exercises a [different restore contract](../../company/systems/operations/restore-company/intent.md). Neither the observer nor an open UI relaunches the service. A venue that shares the company's process or rolls its fill back with the company cannot test this boundary.

## Quality scenarios

**8 · Evidence and falsification.** This is a join of independent observations with **AND** semantics, not a software pipeline or eight ways to count the same log. All evidence must refer to one continuous attempt. The test driver may inject the declared fault, never the research, order or next wake.

```mermaid
flowchart LR
    H["Roster, grant, budget and real W1 acceptance: one A/P"] --> Join{"All checks on one trace?"}
    F["As-of inputs: independently reproduce A's finding and its use"] --> Join
    V["Venue journal: one original submission and one committed X"] --> Join
    Crash["External observer: exit after venue commit, before company receives X"] --> Join
    B["Independent expected position, fee and reserve agree; X posted once"] --> Join
    W["Scheduler and decision records: same A/P, new W2, fresh decision"] --> Join
    Control["Command audit: no board or driver operating commands"] --> Join
    Join -->|all, within sealed deadlines| Yes["Joined case demonstrated"]
    Join -->|missing, contradictory or different attempt| No["Not demonstrated; retain failed or incomplete coverage"]
```

A correct hold, protected unresolved order, blocked hire, or safe transfer to B is reportable, not a pass for this case. Compare [G1–G5 and its failure scenarios](../first-shift/spec.md#first-integration-run), [H1](../episodes.md#h1-hiring) and the [judgment trials](../../employee/agent/learning/evaluate/run-trials/spec.md). This test establishes neither profit nor external exchange readiness.

## Follow one decision across views

| Boundary found by following A | Who owns the handoff; what breaks the tie | What would expose a gap |
| --- | --- | --- |
| **Roster vs readiness vs permission.** C1 creates the hire; A4 defines P; neither fact proves that A can work or trade. | [Onboarding](../../company/systems/organization/hire/onboard/spec.md) stays pending until A actually accepts W1. C2 checks grants and Q2 admits only bound paper risk. | Replay the hire after a lost reply; expect one A and allocation. With research-only scope expect a finding, a denial and zero sends. |
| **Research author vs evidence custodian.** W1 and the grant identify A, while Q1 stores F; neither a stored note nor a typed tool call proves judgment. | [Research](../../company/profiles/quant/investments/research/decision-note/spec.md) owns the immutable cut and digest; [risk admission](../../company/profiles/quant/trading/admit-intent/reserve/spec.md) checks the link before dispatch. | A prepared finding, a future input or an unrelated finding must not establish the research-to-order case. |
| **Lookup vs posting vs general journal.** Reconciliation and posting both described “applying” the same fill. | [Q2 reconciliation](../../company/profiles/quant/trading/recover-order/reconcile/spec.md) owns original lookup and outstanding risk; [Q3 posting](../../company/profiles/quant/fund-books/book-fills/post/spec.md) alone books a confirmed fill and its reservation adjustment. [C7](../../company/profiles/quant/fund-books/book-fills/journal-source/spec.md) posts only mapped cash effects by source. | Return X via two feeds; expect one position/fee change. Lose the journal ack; replay must not subtract the fee twice. Inconclusive lookup retains the reserve. |
| **Process restart vs financial recovery vs next shift.** A healthy process or an armed timer is not evidence of either. | Supervisor restarts; Q2 obtains the Q3 receipt; C3 resolves W1 and issues W2 from the saved condition. The actual A/P reads the recovered position before deciding. | Race a due timer with reconciliation. No position-dependent W2 decision on pre-fill state; stale W1 callback and redelivered wake produce no duplicate action. Unrelated work may continue. |
| **Coverage boundary: H1 ends at first acceptance; G1–G5 starts with a founder.** | The [hired-Agent contract](spec.md#scenario-bindings) owns the uninterrupted trial. For this binding, record H → A/P → W1 → F → O → X → B → W2 with no reset. | Combining two reports cannot prove this chain; a complete paper trace cannot establish continuity for a company without Quant. Both binding results are **UNRUN**. |

These drawings are design contracts, not execution evidence.
