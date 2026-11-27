# Inspect authorized company state

Design draft · [Board Client](../README.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

A board member needs an executive-first view of company health, agent activity, financial ledgers, governance approvals, and—when the Trading domain module is installed—symmetric Live and Paper trading desk performance, causal order traces, and strategy trials while the company operates independently.

## Observable outcome

For Core-only companies (`installed_domains = {'core'}`), the client lands on `/agents` (`AgentsActivityView`) and mounts Core Company OS routes (`/approvals`, `/agents`, `/agents/:agentId`, `/messages`, `/finance`, `/reports`, `/controls`, `/backups`, `/audit`, `/settings`) without Trading navigation or risk controls. When `"trading"` is installed (`DomainModule` `domainKey: "trading"`), the client dynamically mounts `TradingDeskView` (`/`, `/trading/live`, `/trading/paper`), `OrderTraceView` (`/orders/:orderId`), and `StrategiesView` (`/trading/strategies`). Across every route, it collapses technical metadata behind `"Technical details"` or `/audit?tab=daemon`, maintains zero right slide-over drawers, and displays permitted records with source revision, as-of time, and explicit unknowns (`"Unavailable"` / `"No reading"`). Inspection issues no operational command.

## Scope of discretion

Select, format, and navigate across Tier 0 workspace views, Tier 1 in-place causal drill-downs (zero right slide-over drawers), and System views (`/controls`, `/backups`, `/audit`, `/settings`). Do not create records of fact, promote a method, wake a worker, peek into `.desk/drafts/*` bodies, or calculate a replacement financial result.

## Development changes

- [Present a work projection](present/spec.md)
- [Present Trading Desk, Order Trace, and Strategies](investment-panel/spec.md)
- [Present Agents & Activity and Agent Detail](office-floor-map/spec.md)
- [Present client screens and navigation](screens/spec.md)
- [Observe all company and trading flows](flow-observability/spec.md)
