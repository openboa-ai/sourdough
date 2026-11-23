# Identify an actor and authorize an action

Design draft · [Identity and authority](../README.md) · [Company MVP map](../../../../README.md) · [Development lifecycle](../../../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../../../capabilities.md#mandate).

## Operating need

A runtime session, role title or API credential is not an Agent identity or a grant.

## Observable outcome

The company can identify the principal and explain why a particular request was allowed or denied.

## Scope of discretion

An effective grant is checked again at the point of use. Resource assignment, conversation, connector registration, code authorship and a budget reservation do not themselves confer an external operation or deployment grant. Resolution of an already admitted obligation remains separately scoped.

Grants are scoped to company, resource, operation, environment, time and applicable budget.

## Development changes

- [Bind a principal to a company identity](bind/spec.md)
- [Manage role definitions and scoped assignments](manage-roles/spec.md)
- [Decide a scoped capability at use time](check/spec.md)
