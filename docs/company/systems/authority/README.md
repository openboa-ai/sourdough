# C2 · Identity and authority

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Owns who is acting, which resource and operation they may use, and when that authority ends. Neither a conversation nor a runtime credential is a grant.

## Principal and policy decision

### [Identify an actor and authorize an action](authorize-actor/intent.md)

A runtime session, role title or API credential is not an Agent identity or a grant.

- [bind](authorize-actor/bind/spec.md) — Bind a principal to a company identity. Change only the asserted company or replay a retired proof: zero issued credentials and zero resource reads.
- [manage roles](authorize-actor/manage-roles/spec.md) — Manage reusable permission sets and scoped assignments without granting power through a job title or runtime.
- [check](authorize-actor/check/spec.md) — Decide a scoped capability at use time. Use a stale revision or a valid Agent from another company; the independent resource audit shows zero protected effects.

## Withdrawal of authority

### [Withdraw delegated authority](revoke-access/intent.md)

Closing a UI or stopping a worker does not revoke its effective permissions.

- [retire](revoke-access/retire/spec.md) — Fence a retired grant. Submit an old delegated grant or delayed child request after revocation. Verify no new mutation; retain a duty for pre-revocation in-flight effects.
