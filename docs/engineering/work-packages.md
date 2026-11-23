# Development work packages and owning specifications

[Roadmap and handoff](development-roadmap.md) · [Feature boundaries](feature-boundaries.md)

Each spec has one primary verification owner. Earlier steps implement only the part needed by the connected operating path. Later qualification is not an admission gate for the first trade. Acceptance is a combined judgment, not a runtime component. All product tests are NOT_RUN.

## 01 · Company founding

- [authority/authorize-actor/bind](../company/systems/authority/authorize-actor/bind/spec.md)
- [organization/found-company/bootstrap](../company/systems/organization/found-company/bootstrap/spec.md)
- [configuration/compose](../employee/agent/configuration/compose/spec.md)
- [engineering/repository-bootstrap](../engineering/repository-bootstrap/spec.md)

## 02 · First Agent duty

- [investments/market-snapshot/snapshot](../company/profiles/quant/investments/market-snapshot/snapshot/spec.md)
- [authority/authorize-actor/check](../company/systems/authority/authorize-actor/check/spec.md)
- [resources/company-assets/assign](../company/systems/resources/company-assets/assign/spec.md)
- [work/discover-work/decide](../company/systems/work/discover-work/decide/spec.md)
- [desk/reopen](../employee/agent/desk/reopen/spec.md)
- [harness/execute/context-pack](../employee/agent/harness/execute/context-pack/spec.md)
- [finance/capital-budget/allocate](../company/systems/finance/capital-budget/allocate/spec.md)
- [finance/capital-budget/reserve-spend](../company/systems/finance/capital-budget/reserve-spend/spec.md)
- [harness/adapters/register](../employee/agent/harness/adapters/register/spec.md)
- [harness/execute/tool-turn](../employee/agent/harness/execute/tool-turn/spec.md)

## 03 · Paper order and books

- [fund-books/book-fills/post](../company/profiles/quant/fund-books/book-fills/post/spec.md)
- [trading/admit-intent/reserve](../company/profiles/quant/trading/admit-intent/reserve/spec.md)
- [trading/paper-account/bind](../company/profiles/quant/trading/paper-account/bind/spec.md)
- [trading/simulated-venue/orders](../company/profiles/quant/trading/simulated-venue/orders/spec.md)
- [connectors/connect-resource/invoke](../company/systems/connectors/connect-resource/invoke/spec.md)
- [connectors/connect-resource/register](../company/systems/connectors/connect-resource/register/spec.md)

## 04 · First Agent paper trade

- [First Agent paper trade: flow acceptance](flow-delivery/goal-01-04.md)
- [fund-books/decision-trace/project](../company/profiles/quant/fund-books/decision-trace/project/spec.md)

## 05 · Continuing operation and founder resilience

- [work/discover-work/continue-after-result](../company/systems/work/discover-work/continue-after-result/spec.md)
- [trading/admit-intent/continue-execution](../company/profiles/quant/trading/admit-intent/continue-execution/spec.md)
- [acceptance/first-shift](../acceptance/first-shift/spec.md)
- [client/inspect/present](../client/inspect/present/spec.md)
- [investments/research/decision-note](../company/profiles/quant/investments/research/decision-note/spec.md)
- [operations/work-timeline/read](../company/systems/operations/work-timeline/read/spec.md)
- [harness/execute/settle-run](../employee/agent/harness/execute/settle-run/spec.md)
- [trading/recover-order/reconcile](../company/profiles/quant/trading/recover-order/reconcile/spec.md)
- [work/resume-work/recover-shift](../company/systems/work/resume-work/recover-shift/spec.md)
- [work/resume-work/supervise](../company/systems/work/resume-work/supervise/spec.md)

## 06 · Hired-Agent continuity

- [acceptance/hired-agent-continuity](../acceptance/hired-agent-continuity/spec.md)
- [organization/hire/issue-grant](../company/systems/organization/hire/issue-grant/spec.md)
- [organization/hire/onboard](../company/systems/organization/hire/onboard/spec.md)
- [organization/organize-work/staffing-decision](../company/systems/organization/organize-work/staffing-decision/spec.md)

## 07 · Execution environments

- [acceptance/execution-parity](../acceptance/execution-parity/spec.md)
- [investments/backtest/replay](../company/profiles/quant/investments/backtest/replay/spec.md)
- [trading/execution-environment/bind](../company/profiles/quant/trading/execution-environment/bind/spec.md)
- [trading/execution-environment/launch](../company/profiles/quant/trading/execution-environment/launch/spec.md)
- [trading/execution-environment/qualify-engine](../company/profiles/quant/trading/execution-environment/qualify-engine/spec.md)
- [trading/live-paper/run](../company/profiles/quant/trading/live-paper/run/spec.md)
- [trading/venue-qualification/observe](../company/profiles/quant/trading/venue-qualification/observe/spec.md)
- [trading/venue-qualification/testnet](../company/profiles/quant/trading/venue-qualification/testnet/spec.md)

## 08 · Initial company-net return

- [acceptance/net-return](../acceptance/net-return/spec.md)
- [fund-books/book-fills/journal-source](../company/profiles/quant/fund-books/book-fills/journal-source/spec.md)
- [fund-books/fund-economics/close-period](../company/profiles/quant/fund-books/fund-economics/close-period/spec.md)
- [fund-books/fund-economics/reconcile-sources](../company/profiles/quant/fund-books/fund-economics/reconcile-sources/spec.md)
- [fund-books/fund-economics/total-return](../company/profiles/quant/fund-books/fund-economics/total-return/spec.md)
- [accounting/accounting-close/reconcile-period](../company/systems/accounting/accounting-close/reconcile-period/spec.md)
- [accounting/general-ledger/post-journal](../company/systems/accounting/general-ledger/post-journal/spec.md)

## 09 · Candidate comparison

- [investments/portfolio-target/adopt](../company/profiles/quant/investments/portfolio-target/adopt/spec.md)
- [investments/research/compare-operating-revisions](../company/profiles/quant/investments/research/compare-operating-revisions/spec.md)
- [investments/research/experiment](../company/profiles/quant/investments/research/experiment/spec.md)
- [investments/strategy/activate](../company/profiles/quant/investments/strategy/activate/spec.md)
- [investments/strategy/register](../company/profiles/quant/investments/strategy/register/spec.md)

## 10 · Employee lifecycle

- [authority/revoke-access/retire](../company/systems/authority/revoke-access/retire/spec.md)
- [organization/offboard/fence](../company/systems/organization/offboard/fence/spec.md)
- [organization/offboard/settle-custody](../company/systems/organization/offboard/settle-custody/spec.md)
- [resources/company-assets/transfer](../company/systems/resources/company-assets/transfer/spec.md)
- [resources/provision-resource/provision](../company/systems/resources/provision-resource/provision/spec.md)
- [work/collaborate/handoff](../company/systems/work/collaborate/handoff/spec.md)
- [work/collaborate/respond](../company/systems/work/collaborate/respond/spec.md)
- [work/conversations/notify](../company/systems/work/conversations/notify/spec.md)
- [work/conversations/publish](../company/systems/work/conversations/publish/spec.md)
- [desk/publish](../employee/agent/desk/publish/spec.md)

## 11 · Client, operations and restore

- [client/commands/submit](../client/commands/submit/spec.md)
- [client/inspect/investment-panel](../client/inspect/investment-panel/spec.md)
- [fund-books/fund-economics/projection](../company/profiles/quant/fund-books/fund-economics/projection/spec.md)
- [trading/board-control/apply-control](../company/profiles/quant/trading/board-control/apply-control/spec.md)
- [trading/cancel-order/cancel](../company/profiles/quant/trading/cancel-order/cancel/spec.md)
- [trading/recover-order/reconcile-external](../company/profiles/quant/trading/recover-order/reconcile-external/spec.md)
- [finance/treasury/forecast](../company/systems/finance/treasury/forecast/spec.md)
- [operations/monitor-company/incident](../company/systems/operations/monitor-company/incident/spec.md)
- [operations/restore-company/restore](../company/systems/operations/restore-company/restore/spec.md)
- [organization/found-company/amend](../company/systems/organization/found-company/amend/spec.md)

## 12 · Company changes and maintenance

- [operations/manage-change/activate](../company/systems/operations/manage-change/activate/spec.md)
- [operations/request-maintenance/track](../company/systems/operations/request-maintenance/track/spec.md)

## 13 · Judgment and learning

- [acceptance/recursive-learning](../acceptance/recursive-learning/spec.md)
- [harness/adapters/qualify](../employee/agent/harness/adapters/qualify/spec.md)
- [learning/evaluate/run-trials](../employee/agent/learning/evaluate/run-trials/spec.md)
- [learning/methods/distill](../employee/agent/learning/methods/distill/spec.md)
- [learning/methods/evaluate](../employee/agent/learning/methods/evaluate/spec.md)
