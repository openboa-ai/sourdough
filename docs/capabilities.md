# Company and Employee situations

[Business hierarchy](README.md#system-hierarchy) · [Eight architecture views](views.md) · [Design conventions](conventions.md)

For the company/Agent/quant hierarchy and hiring, personal work, collaboration, trading, recovery, learning, net-return and offboarding diagrams, use the [operating-flow view](company-agent-quant.md). Each diagram points back to the owning intents, specs and evidence contracts indexed here.

Start with **what has changed for the company or its employees**, not an API, department or trade. The families below are independent situations, not steps all employees follow. One duty can visit several; each feature has one primary entry here and one technical owner. A role, a business profile and a concrete test fixture are different choices.

<a id="mandate"></a>

## Set direction and inspect it

**Company.** A mandate is founded, queried or lawfully amended. A read returns the permitted revision; only a separately authorized command can change it. A staff member can keep working with the board offline.

- Company → systems: [Found or amend the company](company/systems/organization/found-company/intent.md).
- Company → systems: [Authenticate and authorize](company/systems/authority/authorize-actor/intent.md).
- Company → systems: [Withdraw an existing grant](company/systems/authority/revoke-access/intent.md).
- Quant specialization: [Suspend new investment risk](company/profiles/quant/trading/board-control/intent.md).
- optional client: [Present an authorized projection](client/inspect/intent.md).
- optional client: [Submit an explicit command](client/commands/intent.md).

<a id="staffing"></a>

## Match capacity to an obligation

**Company and Employee.** A need exceeds current capacity. Doing, reassigning, automating, hiring or deferring are alternatives. A hire establishes a new accountable employee; a title or an available runtime is not evidence that the person-equivalent has accepted a duty.

- Company → systems: [Choose how to staff a need](company/systems/organization/organize-work/intent.md).
- Company → systems: [Hire and onboard](company/systems/organization/hire/intent.md).
- Employee → Agent: [Compose a runtime/model profile](employee/agent/configuration/intent.md).
- Employee → Agent: [Register and qualify an adapter](employee/agent/harness/adapters/intent.md).

<a id="judgment"></a>

## Choose and perform work

**Employee.** An obligation or observation becomes due. The employee inspects the evidence available at that point, decides to act, investigate, decline or wait, and leaves a result or a review condition. A company program can perform a delegated operation but is not thereby a staff member.

- Company → systems: [Find work and decide](company/systems/work/discover-work/intent.md).
- Employee → Agent: [Run the bound Agent](employee/agent/harness/execute/intent.md).
- Quant specialization: [Seal as-of observations](company/profiles/quant/investments/market-snapshot/intent.md).
- Quant specialization: [Test a hypothesis and publish a finding](company/profiles/quant/investments/research/intent.md).
- Quant specialization: [Select a target](company/profiles/quant/investments/portfolio-target/intent.md).
- Quant specialization: [Deploy or roll back a strategy program](company/profiles/quant/investments/strategy/intent.md).

<a id="collaboration"></a>

## Work alone or with others

**Employee and Company.** An employee needs information, a resource or another worker. Access is scoped; notes remain private until an authorized publication. A message, delivery, accepted assignment and transfer of custody are different facts.

- Employee → Agent: [Reopen or publish desk work](employee/agent/desk/intent.md).
- Company → systems: [Exchange messages](company/systems/work/conversations/intent.md).
- Company → systems: [Accept an explicit handoff](company/systems/work/collaborate/intent.md).
- Company → systems: [Assign or transfer company assets](company/systems/resources/company-assets/intent.md).

<a id="learning"></a>

## Improve a method

**Employee.** Repeated experience suggests a change. Test a candidate against the incumbent and adopt, reject or roll it back within the permitted scope. A useful personal note does not automatically become company policy.

- Employee → Agent: [Evaluate judgment](employee/agent/learning/evaluate/intent.md).
- Employee → Agent: [Test and adopt a method](employee/agent/learning/methods/intent.md).

<a id="effects"></a>

## Authorize and resolve an effect

**Company and Employee.** A proposed action would change a resource. The resource owner admits or denies it; an uncertain response leaves a named follow-up and the original action key. Cancellation is another authorized request, not proof of reversal. Many duties need no external effect.

- Company → systems: [Bind a scoped resource](company/systems/connectors/connect-resource/intent.md).
- Company → systems: [Provision a scoped resource](company/systems/resources/provision-resource/intent.md), [activate a company-owned change](company/systems/operations/manage-change/intent.md), or [request external product maintenance](company/systems/operations/request-maintenance/intent.md).
- Quant specialization: [Bind the simulated account](company/profiles/quant/trading/paper-account/intent.md).
- Quant specialization: [Reserve risk for an intent](company/profiles/quant/trading/admit-intent/intent.md).
- Quant specialization: [Submit and observe at the venue](company/profiles/quant/trading/simulated-venue/intent.md).
- Quant specialization: [Cancel the unfilled remainder](company/profiles/quant/trading/cancel-order/intent.md).
- Quant specialization: [Resolve an uncertain order](company/profiles/quant/trading/recover-order/intent.md).
- Quant specialization: [Qualify an external venue separately](company/profiles/quant/trading/venue-qualification/intent.md).

<a id="stewardship"></a>

## Steward resources and explain outcomes

**Company.** Capacity is committed before actual costs are known. Keep a commitment, an actual, and a derived measure distinct. Reconcile source records and disclose missing inputs rather than reporting estimates as facts.

- Company → systems: [Allocate or reserve operating spend](company/systems/finance/capital-budget/intent.md).
- Company → systems: [Forecast liquidity](company/systems/finance/treasury/intent.md).
- Company → systems: [Post company actuals](company/systems/accounting/general-ledger/intent.md).
- Company → systems: [Reconcile and close](company/systems/accounting/accounting-close/intent.md).
- Quant specialization: [Book venue fills](company/profiles/quant/fund-books/book-fills/intent.md).
- Quant specialization: [Compute and explain total return](company/profiles/quant/fund-books/fund-economics/intent.md).

<a id="continuity"></a>

## Detect interruption and continue

**Company and Employee.** A worker, process or information source disappears. Determine what was committed; protect pending duties, restore verified state, and continue only the work whose dependencies are satisfied. Restoring the original worker and assigning a different one are separate outcomes.

- Company → systems: [Detect and contain incidents](company/systems/operations/monitor-company/intent.md).
- Company → systems: [Restore lost company storage](company/systems/operations/restore-company/intent.md).
- Company → systems: [Recover an interrupted shift](company/systems/work/resume-work/intent.md).

<a id="departure"></a>

## End employment without losing obligations

**Company and Employee.** Withdraw authority at a known cutoff, keep assets in company custody and assign unfinished work explicitly. Historical authorship stays with the original worker. Stopping a process alone is not retirement.

- Company → systems: [Retire and settle custody](company/systems/organization/offboard/intent.md).

<a id="accountability"></a>

## Explain what happened

**Company.** A stakeholder asks why a result changed and who owes the next step. Serve a projection joined to authoritative evidence, with gaps and as-of time. Viewing it does not act on the world.

- Company → systems: [Serve the work projection](company/systems/operations/work-timeline/intent.md).
- Quant specialization: [Trace a financial effect](company/profiles/quant/fund-books/decision-trace/intent.md).
