# Responsibility and unattended-operation design audit

Design review · **2026-11-21** · Documentation only · All product and acceptance observations **UNRUN**. [Responsibility hierarchy and diagrams](../company-agent-quant.md) · [Ownership map](../README.md#system-hierarchy) · [Delivery lifecycle](lifecycle.md)

## Scope and method

### Purpose and boundary simplification

The [feature-boundary inventory](feature-boundaries.md) traces the product intent and each feature to its purpose, primary responsibility and observable outcome, with the intent linking its development units. The detailed owner and adverse cases remain in the relevant spec; the inventory is not a second source of requirements. Preserve functions and separately evaluate their contribution before changing their scope.

The review corrects two overlapping ownership descriptions: Authority alone owns grants while Resources owns asset assignment and custody; Work alone accepts duty handoffs. For company changes, the applicable domain activation service records the single effective version and Operations supervises it by reference. The strategy service owns its strategy deployment; its functional verification is distinct from economic comparison. These are logical boundaries, not mandated services or new approval stages.

The feature outcomes and links are a design trace, not evidence that the tests were executed. The founder and hired-Agent paper episodes, independent venue witness, economic baseline followed by matched comparison, and separate RSI transfer evaluation remain UNRUN. No safe live-capital use or completed self-improvement implementation follows from this review.

### Full-boundary inventory, 2026-11-21

The current inventory contains 48 intents and 79 specs: company 22/31, quant 17/28, Agent 6/11, client 2/3, product 1/0, acceptance 0/5 and engineering 0/1. These counts include the newly added provision, change and maintenance contracts. The inventory and local links are mechanically checked across the documentation tree; focused semantic review covered the cross-owner and acceptance contracts below. This is a design consistency review, not a claim that all individual behaviors, implementations or all possible failure paths were independently validated.

| Boundary reviewed | Current design disposition |
| --- | --- |
| Human, Client and company | Human queries and controls enter only through Client, route to their owning service, and do not make autonomous work depend on Client. Submission and application are distinct. |
| Agent, resources and organization | Agent identity remains bound to its selected runtime/model. Resources retain company ownership; [provisioning](../company/systems/resources/provision-resource/intent.md) connects request, authority, budget, assignment and retirement. Agent role names are illustrative. |
| Collaboration, work and operations | Messages and operational notices are not duty acceptance or state changes. Work owns the next wake and accepted handoff; operations owns incident and update status. |
| Candidate, operating revision and product foundation | [Company changes](../company/systems/operations/manage-change/intent.md) distinguish independent verification and activation from authorship; [product maintenance](../company/systems/operations/request-maintenance/intent.md) follows issue, release, local application and verification as distinct facts. |
| Fund, connector and managed runtime | Research and original-order lookup can use authorized connectors directly. Orders cannot bypass risk admission and durable reservation. NautilusTrader is company-managed; the venue is independently operated. Process-independent paper fill persistence remains unqualified. |
| Recovery, economics and learning | Founder and hired-Agent acceptance remain independent single-run obligations. First paper company-net measurement precedes matched candidate comparison; RSI uses a separate transfer evaluation and is not implemented by this design. |

New resource, change and maintenance contracts have independent normal and adverse acceptance cases, all **UNRUN**. They add no new standing grants, production credentials, outside disclosure permission, live trading authority or automatic release adoption policy. Actual rollout and external-source availability remain separate evidence.

The prior pass reviewed 42 `intent.md` and 69 `spec.md` files against the hierarchy, eight operating diagrams, normal and failure paths, initial and subsequent duties, authoritative records, and first economic observation. That inventory included the product intent, four cross-domain acceptance specs and repository bootstrap spec. Subsequent environment, historical-replay and forward-paper changes add intents and specs; they are covered by the separate [Agent and trading trace](../acceptance/research-trading-trace.md). Counts identify the reviewed document set, not implemented features or a claim of operational completeness.

| Area | Reviewed intents / specs | Authoritative boundary and independent claim |
| --- | ---: | --- |
| Company systems | 19 / 28 | Organization, authority, Work, Resources, Connectors, Finance, Accounting and Operations own distinct records. Hiring, desk publication, duty acceptance, cutoff and custody are different facts. |
| Agent | 6 / 11 | Configuration binds a new identity; Harness mediates current-generation tools; Desk scopes company assets; Learning proposes and evaluates methods. A fresh session on the same binding differs from succession. |
| Quant | 14 / 22 | Research supplies observations and decisions; Risk admits; the account-scoped investment subledger writes reservations and positions; the venue owns fills; accounting owns posted company actuals; performance owns return calculation. |
| Client | 2 / 3 | Projection presentation and authorized control requests are separate from all operational writers; a closed client cannot stop recovery or accounting. |
| Cross-domain acceptance | 0 / 4 | Founder shift, hired-Agent continuity, net-return comparison and recursive learning have separate scopes and witnesses. |
| Product / engineering | 1 / 1 | Product intent defines purpose; repository bootstrap defines delivery infrastructure, not a running fund. |

All feature-unit specs must have an owning intent and be listed by it. A cross-system input is a contract, not necessarily a previously completed instance: especially distinguish initial conditions from later observations. Preserve one complete, actual observation per integration claim; fixture, model transcript, source review and rendered UI are not substitutes.

## Corrections incorporated

| Design gap | Owning changes and required independent evidence |
| --- | --- |
| Initial workspace and first context depended on a future runtime turn or decision. | [Asset assignment](../company/systems/resources/company-assets/assign/spec.md) allows authorized pre-run provisioning; [context pack](../employee/agent/harness/execute/context-pack/spec.md) accepts a first claim without prior decisions. Demonstrate the founder's actual first turn and decision. |
| First finding, first target and first program activation could be read as requiring their own downstream decision, reservation or target. | [Finding](../company/profiles/quant/investments/research/decision-note/spec.md), [target](../company/profiles/quant/investments/portfolio-target/adopt/spec.md) and [activation](../company/profiles/quant/investments/strategy/activate/spec.md) distinguish input from later output. Demonstrate creation from a flat account without seeded downstream artifacts. |
| Per-order Agent finding and simulator-specific fill input could be misread as mandatory for all program and venue paths. | [Risk admission](../company/profiles/quant/trading/admit-intent/reserve/spec.md) separately specifies Agent and company-program lineage; [fill posting](../company/profiles/quant/fund-books/book-fills/post/spec.md) consumes independently bound venue evidence, with the simulator as the first binding. No external readiness follows from a local fixture. |
| Dispatch recovery and cancellation could release capacity before late fill coverage was complete. | [Connector invocation](../company/systems/connectors/connect-resource/invoke/spec.md), [original-order reconciliation](../company/profiles/quant/trading/recover-order/reconcile/spec.md) and [cancel](../company/profiles/quant/trading/cancel-order/cancel/spec.md) retain unknown outcomes and require cumulative identified fills before unfilled release. Test old sender, duplicate fill, terminal-before-fill-history and restart cuts against independent venue records. |
| Monitoring, a work projection or same-owner restore could require a decision, a client or a custody transfer that does not yet exist. | [Incident monitoring](../company/systems/operations/monitor-company/incident/spec.md), [timeline](../company/systems/operations/work-timeline/read/spec.md) and [restore](../company/systems/operations/restore-company/restore/spec.md) admit empty initial state and restore current custody without inventing a successor. |
| A paired paper trial isolated accounts but did not explicitly isolate mutable Agent state, effect destinations or actual shared cost billing. | [Operating-revision comparison](../company/profiles/quant/investments/research/compare-operating-revisions/spec.md) defines trial namespaces, current input visibility, forbidden cross-arm/production effects, and single actual cost accounting. [Paper simulation](../company/profiles/quant/trading/simulated-venue/orders/spec.md) allows later market events only when they are available by simulated fill time. |

## Remaining evidence and choices

- The [founder](../acceptance/first-shift/spec.md) and separately [hired Agent](../acceptance/hired-agent-continuity/spec.md) must each demonstrate research, original paper effect, process death, automatic reconciliation and a fresh same-Agent next decision in one observed run. A valid hold is not fill-branch coverage.
- A selected runtime must demonstrate unattended ordinary tool use and bounded budget; an adapter contract or scripted peer is not provider qualification. Failure to provision or loss of consent is **blocked**, not passed.
- The first [company-net paper result](../acceptance/net-return/spec.md) requires a registered source, cost perimeter, accountable marks, risk limits, timing and a realistic declared fill policy. Missing evidence remains incomplete. Numerical limits, product/account selection and actual provider expenditure remain separately authorized decisions; this audit chooses none.
- Compare a candidate against the recorded incumbent only in a later registered paired window. The [recursive learning trial](../acceptance/recursive-learning/spec.md) is separate. Neither positive paper P&L, a method receipt nor a reliable recovery demonstrates real-capital profit.
- Local-store restart, isolated checkpoint restoration, external venue/testnet qualification and live-capital operation are distinct claims. Do not infer one from another. Existing independent hiring, workspace, learning, offboarding and return episodes remain in force.
