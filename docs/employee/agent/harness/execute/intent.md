# Run the actual Agent with limited tools

Design draft · [Agent execution and runtime adapters](../README.md) · [Company MVP map](../../../../README.md) · [Development lifecycle](../../../../engineering/lifecycle.md)

Business situation: [Choose and perform work](../../../../capabilities.md#judgment).

## Operating need

A real Agent must be able to inspect records and make an authenticated tool call when the board is offline.

## Observable outcome

The company prepares a bounded context, observes a real provider start and typed tool request, persists the independent effect receipt, and settles work and usage separately. Session replacement must preserve duties without carrying over revoked access. See the [harness protocol](../protocol.md).

## Scope of discretion

Local and remote runtimes may connect through the same port after qualification. A new session can serve the original Agent only on its recorded binding. A different runtime requires a distinct Agent; a provider string or session ID alone identifies neither. No named adapter is assumed to work.

The Agent can request company-system changes, research, resource access or external operations within its grant. The harness mediates the request and current identity; it does not approve changes, expand authority or write the domain result.

## Development changes

- [Bind a runtime turn to company tools](tool-turn/spec.md)
- [Prepare a bounded Agent context pack](context-pack/spec.md)
- [Settle a turn without losing its obligations](settle-run/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
