# Request a governance change

Design draft · [Board Client](../README.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

A board member may want to change the standing mandate without supervising ordinary work.

The client can also offer an explicitly authorized request to configure or start the company's trading runtime where that profile is enabled. The company and its permitted Agents can perform ordinary runtime setup and operation without the client.

## Observable outcome

The client submits an explicit authorized request and displays the server receipt or an unresolved state across reconnect.

## Scope of discretion

A visible button or optimistic state is not a permission or an applied revision. The control plane authorizes; the domain owner enforces.

Humans access company state and submit company control only through Client. The control plane routes each authorized command to its owning service: organization and authority for mandate or grants, operations for lifecycle, and the enabled domain for its risk controls. Submitted, unknown and applied are distinct. Agent and company operation do not depend on Client.

## Development changes

- [Submit a command and resolve its receipt](submit/spec.md)
