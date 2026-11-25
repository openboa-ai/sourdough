# Feature purpose, boundaries and observable outcomes

Design inventory · Product and acceptance evidence **UNRUN** · [System map](../README.md#system-hierarchy) · [Business situations](../capabilities.md) · [Conventions](../conventions.md)

This inventory traces every feature intent to its purpose and outcome. The linked intent owns the feature contract and lists its development-unit specs; each spec owns its detailed inputs, authority, failure behavior and acceptance evidence. The rows do not create additional writers, requirements or processes. A single case here cannot replace a spec's adverse cases. All examples are design expectations, not observed results.

## Company: organization and authority

| Feature | Purpose and boundary | Observable outcome |
| --- | --- | --- |
| [Found company](../company/systems/organization/found-company/intent.md) | Organization retains the mandate; Client submits human control, not direct mutations. | One founded company has a durable charter; an authorized amendment produces the effective revision. |
| [Staff work](../company/systems/organization/organize-work/intent.md) | Judge whether to do, delegate, automate, hire or wait; a title is not a grant. | A staffing decision cites a need and leaves a bounded next action. |
| [Hire](../company/systems/organization/hire/intent.md) | Create an Agent under a staffing grant and provision its initial work. | A distinct Agent accepts a first duty with only its authorized access. |
| [Offboard](../company/systems/organization/offboard/intent.md) | End access while custody and unresolved work stay with the company. | The former Agent is fenced and duties and company assets remain accountable. |
| [Authorize](../company/systems/authority/authorize-actor/intent.md) | Authority owns actor binding and effective grants, not the business result. | A current actor may perform only its granted operation; cross-company or stale access is denied. |
| [Revoke](../company/systems/authority/revoke-access/intent.md) | End new authority without erasing admitted effects. | An old grant cannot cause a new mutation; in-flight obligations retain a recovery owner. |

## Company: work, resources and external connections

| Feature | Purpose and boundary | Observable outcome |
| --- | --- | --- |
| [Discover work](../company/systems/work/discover-work/intent.md) | The Agent chooses its action; Work records the decision and next condition. | A decision or justified wait is durable and a later wake is armed. |
| [Resume work](../company/systems/work/resume-work/intent.md) | Supervisor restarts the company; Work fences obsolete claims. | One current claimant resumes, followed by a distinct later duty without Client. |
| [Converse](../company/systems/work/conversations/intent.md) | Preserve scoped dialogue and notices; a message is not a command or acceptance. | Authorized recipients can retrieve the thread without acquiring its attachments or duties automatically. |
| [Handoff](../company/systems/work/collaborate/intent.md) | Work owns responsibility; asset custody and access have separate owners. | The original owner remains responsible until an eligible successor accepts. |
| [Company assets](../company/systems/resources/company-assets/intent.md) | Resources owns asset custody and assignment, not effective grants or accepted duties. | An authorized successor finds the same company asset while another private desk stays inaccessible. |
| [Provision resource](../company/systems/resources/provision-resource/intent.md) | Join authority, budget and the existing assignment; do not create a second asset record. | An allowed Agent can use its scoped resource; denial or retirement prevents new access. |
| [External connection](../company/systems/connectors/connect-resource/intent.md) | Bind and transport scoped operations; the destination and domain own external facts and meaning. | A read-only source works without write authority; an uncertain write stays tied to its original identity. |

## Company: money and operations

| Feature | Purpose and boundary | Observable outcome |
| --- | --- | --- |
| [Budget](../company/systems/finance/capital-budget/intent.md) | Finance owns spending limits and commitments, not booked actuals. | Concurrent requests cannot spend the same available budget. |
| [Treasury](../company/systems/finance/treasury/intent.md) | Forecast liquidity from commitments and accounting actuals without moving cash. | A projected shortfall is visible and an obligation is not counted twice. |
| [General ledger](../company/systems/accounting/general-ledger/intent.md) | Accounting owns journaled company economic events. | One source produces one balanced posting; correction preserves its history. |
| [Accounting close](../company/systems/accounting/accounting-close/intent.md) | Reconcile company books against source evidence, independently of risk admission. | A discrepancy or missing source prevents a falsely complete close. |
| [Company change](../company/systems/operations/manage-change/intent.md) | Operations supervises independently checked changes; the domain owner records the effective version. | An authorized, verified company asset runs under its recorded revision or remains safely unactivated. |
| [Maintenance request](../company/systems/operations/request-maintenance/intent.md) | Track external product repair without giving the maintainer control of the company. | Issue, release, local application and original-defect verification have distinct dispositions. |
| [Monitor company](../company/systems/operations/monitor-company/intent.md) | Operations detects and contains incidents; it cannot fabricate domain facts. | An affected scope has an owner, next check and independently supported closure. |
| [Restore company](../company/systems/operations/restore-company/intent.md) | Restore the original company's durable identity and obligations after store loss. | A verified restore fences the old writer; missing evidence blocks dependent effects. |
| [Work timeline](../company/systems/operations/work-timeline/intent.md) | Project authorized work history without modifying it. | A reader sees the as-of work record and coverage; the query starts no work. |

## Agent

| Feature | Purpose and boundary | Observable outcome |
| --- | --- | --- |
| [Configure Agent](../employee/agent/configuration/intent.md) | Bind a new Agent to a runtime/model choice; configuration is not hiring or a grant. | Restart preserves the binding; a different binding denotes a different Agent. |
| [Agent desk](../employee/agent/desk/intent.md) | Give an Agent scoped private work on company assets, with explicit publication. | The Agent reopens its own work and shares only the selected authorized output. |
| [Runtime adapters](../employee/agent/harness/adapters/intent.md) | Admit compatible runtime choices without equating a session with Agent identity. | A qualified binding serves the correct Agent; a mismatched binding is rejected. |
| [Agent execution](../employee/agent/harness/execute/intent.md) | Harness supplies context and mediates tools; the Agent judges and domain services own effects. | A real bounded turn uses current authority and settles or retains its duty. |
| [Evaluate judgment](../employee/agent/learning/evaluate/intent.md) | Assess the decision available at the time, not the luck of a later fill. | An independent evaluation records evidence, uncertainty and the observed decision. |
| [Improve methods](../employee/agent/learning/methods/intent.md) | Compare and adopt work-method changes within audience grants. | A tested method is adopted or rejected and a later shift demonstrates use or rollback. |

## Fund: investment and trading

| Feature | Purpose and boundary | Observable outcome |
| --- | --- | --- |
| [Market snapshot](../company/profiles/quant/investments/market-snapshot/intent.md) | Record what data was available at a cut, including gaps. | A later correction cannot be presented as information known earlier. |
| [Research](../company/profiles/quant/investments/research/intent.md) | Agent tests an investment hypothesis; research does not authorize trades. | A reproducible finding or negative result cites available evidence and costs. |
| [Backtest](../company/profiles/quant/investments/backtest/intent.md) | Replay historical observations and strategy under an isolated environment. | Pinned inputs reproduce a result with explicit fill and cost assumptions. |
| [Portfolio target](../company/profiles/quant/investments/portfolio-target/intent.md) | Represent desired exposure separately from actual positions. | A target's delta accounts for existing positions and pending commitments. |
| [Strategy program](../company/profiles/quant/investments/strategy/intent.md) | Register and activate company code; authorship and order authority differ. | A verified active revision proposes orders under its own grant and can be fenced. |
| [Paper account](../company/profiles/quant/trading/paper-account/intent.md) | Bind a fund to a scoped simulated account. | Restart reopens the same account without granting live order access. |
| [Risk control](../company/profiles/quant/trading/board-control/intent.md) | Apply authorized mandate changes without routine human approval of orders. | New risk is suspended at the recorded revision while existing effects are reconciled. |
| [Admit order](../company/profiles/quant/trading/admit-intent/intent.md) | Risk admits eligible Agent or program requests; investment ledger owns reservation. | Concurrent orders cannot consume the same headroom and a retry reuses its identity. |
| [Execution environment](../company/profiles/quant/trading/execution-environment/intent.md) | Company configures and supervises the selected trading runtime under common controls. | Backtest, paper and separately authorized live bindings remain isolated and share company invariants. |
| [Live paper](../company/profiles/quant/trading/live-paper/intent.md) | Run virtual orders on current production market observations. | Market reads use actual current data while no real order destination is reachable. |
| [Simulated venue](../company/profiles/quant/trading/simulated-venue/intent.md) | Supply an independently observable simulated order and fill source. | Its committed fill remains observable after the company process dies. |
| [Cancel order](../company/profiles/quant/trading/cancel-order/intent.md) | Cancel the original remaining quantity; a reply alone does not establish all fills. | Only the proven unfilled reservation is released after late fills are reconciled. |
| [Recover order](../company/profiles/quant/trading/recover-order/intent.md) | Reconcile the original external identity without reissuing an unknown order. | A confirmed fill is posted once or affected risk remains reserved with a recovery duty. |
| [Qualify venue](../company/profiles/quant/trading/venue-qualification/intent.md) | Observe actual external adapter behavior separately from simulator claims. | Each authorized testnet behavior is observed, unsupported or unrun; none implies live approval. |

## Fund books and Client

| Feature | Purpose and boundary | Observable outcome |
| --- | --- | --- |
| [Book fills](../company/profiles/quant/fund-books/book-fills/intent.md) | Investment ledger owns positions and reservations; accounting owns corporate entries. | A repeated source fill changes the position and mapped economic source once. |
| [Fund economics](../company/profiles/quant/fund-books/fund-economics/intent.md) | Combine reconciled investment results, costs, marks and external flows. | Company-net performance is complete only with covered sources; missing costs remain unknown. |
| [Decision trace](../company/profiles/quant/fund-books/decision-trace/intent.md) | Explain a financial effect from domain evidence without changing it. | A reader sees the decision, source fill, resulting position and gaps at a named revision. |
| [Client inspection](../client/inspect/intent.md) | Present authorized service projections without owning operational state. | Closing the Client does not stop work; reading emits no control command. |
| [Client command](../client/commands/intent.md) | Submit human governance commands to the owning service. | The Client distinguishes submitted, unknown and effective receipts. |
| [Local control plane](../client/control-plane/intent.md) | Serve single-writer-guarded UDS projections, RBAC, NDJSON events, idempotent commands, and paper-only adapter boundaries. | Control-plane enforcement stays in `CompanyDaemon`; company operation does not require Client connection. |
| [Client design system](../client/design-system/intent.md) | Align Board Client UI presentation, layout grammar, agent confirmation gates, and missing-data charts with `@openboa/yogurt` (`0.2.0-local.1`). | Presentation choices remain strictly inside Client UI without modifying Python/SQLite domain owners. |

The product [intent](../intent.md) defines net-return purpose and the Agent-native hypothesis; it is not a feature with a separate development unit. Cross-domain [acceptance](../README.md#integration-and-completion) retains founder, hired-Agent, trading parity, net-return and recursive-learning evidence separately. The founder and hired-Agent paper paths each require one uninterrupted research, fill, post-fill crash, automatic recovery and same-Agent next-decision attempt. No row establishes product readiness or actual profitability.
