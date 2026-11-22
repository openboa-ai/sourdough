# Company and Employee: eight architecture views

[Business hierarchy](README.md#system-hierarchy) · [Situation catalog](capabilities.md) · [Worked paper-fund example](acceptance/hired-agent-continuity/paper-walkthrough.md)

Read each situation at three levels: **Company and Employee** describe who is responsible and what must remain true; **company systems and Agent** implement those responsibilities; **business profiles and fixtures** supply specialized rules and observations. The eight views below are lenses on the same model, not eight additional layers.

## Company model

**1 · Meaning.** The company owns the mandate, assets and outstanding obligations. An employee exercises judgment and answers for accepted duties. Neither is defined by the software that runs it.

```mermaid
flowchart TD
    Board["Board"] -->|sets standing mandate| Company["Company"]
    Company -->|employs and delegates to| Employee["Employee"]
    Company -->|operates through| Systems["Company systems"]
    Employee -->|realized in software by| Agent["Agent"]
    Agent -->|uses scoped company interfaces| Systems
    Profile["Optional business profile"] -->|specializes selected operations| Systems
```

## Business capabilities

**2 · Situations.** An unmet obligation creates a choice, not an automatic hire or operation. Other situations, such as learning, retirement or reporting, can start independently; the [catalog](capabilities.md) maps every feature.

```mermaid
flowchart TD
    Need["Company need or employee observation"] --> Choice{"Who should act?"}
    Choice -->|existing capacity| Assign["Offer or select a duty"]
    Choice -->|capacity missing| Staff["Compare staffing alternatives"]
    Staff --> Assign
    Choice -->|not warranted now| Defer["Record reason and next review"]
    Assign --> Judge["Employee examines evidence"]
    Judge --> Result["Produce work, request a change, hand off or wait"]
```

## Decision and write authority

**3 · Authority.** Business authorship is not write authority over a resource. The employee can recommend a change; a named system must decide whether to admit it.

```mermaid
flowchart LR
    Company["Company mandate"] --> Grant["Authority: current scoped grant"]
    Employee["Employee"] -->|accepts| Duty["Work: accountable duty"]
    Employee -->|authors| Decision["Decision or work product"]
    Grant --> Gate["Owning system: admission"]
    Decision -->|only if an effect is needed| Gate
    Gate -->|denied or committed| Record["Authoritative disposition"]
```

## Logical components and interfaces

**4 · Software.** Company systems expose work, resource and authority contracts. The employee is implemented by an Agent, not by a session, tool, or provider. The optional client consumes service contracts, not vice versa.

```mermaid
flowchart TD
    Work["Company Work"] -->|claimed duty| Harness["Agent Harness"]
    Config["Bound Agent configuration"] --> Harness
    Desk["Agent Desk and approved methods"] --> Harness
    Harness <-->|scoped turn and tool requests| Runtime["Assigned runtime"]
    Harness -->|request under current grant| Owner["Owning company or profile system"]
    Owner -->|only when external access is needed| Connector["Resource connector"]
    UI["Optional client"] -->|query or explicit command| API["Service APIs"]
```

## Session recovery versus succession

**5 · Behavior.** The same duty can be resumed; a later duty is not a retry. When the worker changes, responsibility transfers only on an authorized handoff.

```mermaid
flowchart TD
    Due["Need or due condition"] --> Claim["Accept or claim duty"]
    Claim --> Choice["Judge available evidence"]
    Choice --> Saved["Record disposition and next responsibility"]
    Saved --> Later["Distinct later duty when due"]
    Claim -->|interruption| Restore["Recover original facts and fence stale claim"]
    Restore -->|original Agent and binding| Claim
    Restore -->|unavailable| Transfer["Keep owner pending or offer explicit handoff"]
    Transfer -->|successor accepts| New["Different employee, retained attribution"]
```

## Records and lineage

**6 · Information.** Stable references explain a result without copying an employee's private desk. There need not be a remote action. The owner of a specialized record interprets its contents; Work holds the reference.

```mermaid
flowchart TD
    Mandate["Mandate and grant revision"] --> Duty["Duty, employee and generation"]
    Input["Available evidence and revision"] --> Decision["Employee decision and reasons"]
    Duty --> Decision
    Decision --> Result["Work product, effect receipt or reason for waiting"]
    Result --> Later["Later duty and fresh decision"]
    Decision -->|next condition| Later
    Result --> Read["Authorized projection with source coverage"]
```

## Local fault boundaries

**7 · Deployment.** The employee is not a process. Losing a turn, losing the service, losing the store and retiring an employee have different recovery owners. External systems have independent truth only where the duty actually uses one.

```mermaid
flowchart LR
    Sup["Product supervisor"] -->|relaunches| Service["Company service process"]
    Service --> Store[("Durable company store")]
    Service <-->|bounded turn| Runtime["Agent runtime"]
    Service <-->|optional connector| Resource["Independently owned resource"]
    Client["Optional client"] -.->|no operating dependency| Service
```

## Quality scenarios

**8 · Evidence.** Select a situation first, then choose its oracle and failure cut. A staffing test needs proof of first-duty acceptance; an advisory duty can succeed with zero external writes. Outcome evidence must come from the record owner, not the Agent's narrative or the UI.

```mermaid
flowchart LR
    Situation["Business situation and allowed alternatives"] --> Invariant["Named owner and invariant"]
    Invariant --> Test["Binding: fixture, oracle and observation window"]
    Test --> Observation["Actual action or justified inaction"]
    Observation --> Compare{"Independent evidence matches?"}
    Compare -->|yes| Scoped["Pass for this binding only"]
    Compare -->|missing or contradictory| Gap["Incomplete or failed with retained evidence"]
```

## Follow one decision across views

For example, when an employee leaves, the organizational change is the end of delegated responsibility. Authority owns the cutoff, Work owns any transfer, and Resources owns custody. The Agent's last process exit establishes none of them. Verify the effective cutoff, who accepted pending duties, and which assets remain accessible, using the [offboarding episode](acceptance/episodes.md#o1-offboarding).

For a different question, compare the [generic newly hired employee continuity contract](acceptance/hired-agent-continuity/spec.md) with its [eight-view paper binding](acceptance/hired-agent-continuity/paper-walkthrough.md) and the separate [founder integration](acceptance/first-shift/spec.md#first-integration-run). A local assessment and an independently committed external action have different crash witnesses; the employee's obligation to use the verified result on a later duty is the same.

These are design views, not observed product behavior.
