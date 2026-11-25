# Request a governance change

Design draft · [Board Client](../README.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

A board member needs to register owner authority, amend the standing mandate, answer reserved human decisions, manage scoped roles and hiring grants, exercise paper risk or company-wide emergency controls, trigger checkpoints and restore un-quarantine, activate or roll back strategy releases and review tools, or swap paper execution bindings without supervising ordinary work.

The client can also offer an explicitly authorized request (`configure_paper_runtime` / `start_paper_runtime` under `paper-runtime-command-v1`) to configure or start the company's paper trading runtime where that profile is enabled (`environment == "paper"`). The company and its permitted Agents can perform ordinary runtime setup and operation without the client.

## Observable outcome

The client submits an explicit authorized request and displays the server receipt or an unresolved state across reconnect.

## Scope of discretion

A visible button or optimistic state is not a permission or an applied revision. The control plane authorizes; the domain owner enforces.

Humans access company state and submit company control only through Client. The control plane routes each authorized command to its owning service: organization and authority for owner bootstrap, mandate amendments, role/grant changes, hiring decisions, and offboarding custody; operations for checkpoints, restore quarantine, work interruption/cancellation, and review-tool qualification/quarantine; and the enabled paper profile for risk suspension/resume, paper binding changes, and strategy deployment/rollback. Submitted, unknown, accepted, applied, denied, and retry-choice states are distinct. Agent and company operation do not depend on Client.

## Development changes

- [Submit a command and resolve its receipt](submit/spec.md)
