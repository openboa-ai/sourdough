# Manage role definitions and scoped assignments

Design draft · Verification **UNRUN** · Responsible component: Authority

[Feature intent](../intent.md) · [Principal binding](../bind/spec.md) · [Admission](../check/spec.md) · [Revocation](../../revoke-access/retire/spec.md)

## Responsibility

Use roles to administer reusable sets of named permissions. A permission names an operation on a resource kind. A role definition contains permissions; a role assignment binds one verified principal to one exact role revision within a company, resource and environment scope, with optional expiry. An organizational title, project membership or provider session does not assign a role. Companies can compose roles as work requires them; the product does not instantiate fixed departments.

Authentication establishes the principal. The Authority service owns role definitions, assignments, delegation limits and their revisions. The owning domain supplies authoritative facts such as the current work claim, project audience, account and risk state. A verified principal may be a human, an Agent or a company service. The Client and Agent tools only submit commands and display permitted projections. A connector credential proves nothing about the caller's company authority.

## Administration and admission

- **Define and assign.** The service owns the permission catalog. Use flat, immutable role revisions with explicit permissions, without wildcards or role inheritance in the first implementation. Only a currently authorized administrator can assign, replace or revoke a role. The proposed permissions and scope must fit an explicit delegation limit; permission to administer a role is distinct from permission to use its actions. Agents cannot approve their own authority expansion. Editing a role does not silently expand its existing assignments. Show the affected principals and permission changes before an authorized replacement.
- **Enforce at use.** Default to denial. Authorize a verified principal, action and resource only if one active assignment covers that action and scope and all applicable conditions hold. Do not combine an action from one assignment with a resource scope from another. Require current company membership, resource relationship and work generation where applicable; use expiry and explicit incompatibilities such as a change author not qualifying the same change. Filter lists and direct lookups by the same policy. Recheck new effects at the owning service, regardless of what a Client or harness exposes.
- **Keep domain limits separate.** A paper-order permission does not authorize a live destination, exceed a budget, or bypass suspension. A handoff permission does not grant an asset audience or transfer an owner's authority. The service evaluates and records admission against current policy in the same transaction as a local mutation. External dispatch requires a current narrow admission before sending; reconciliation of an already admitted effect uses separate service authority and retains the original receipt.
- **Withdraw and audit.** An assignment replacement, revocation or departure advances the relevant authority revision. A cached session, delayed callback or prior allow cannot admit new work after the change. Record principal, operation, resource reference, role and policy revisions, decision reason and action receipt as applicable. Keep history and avoid storing raw credentials, prompts or private content in authorization records.

Keep this in the current service and transactional store. Do not add an external policy engine, a second source of role assignments, or a new authority to change real trading permissions.

## Rollout

Map existing standing grants to explicit scoped assignments with the same or narrower permissions; reject unmapped cases instead of creating a broad default role. Check the proposed decisions against current founding, hires, project handoff and paper admission without authorizing live effects in a parallel policy path. Switch each protected entry point only when its positive and negative parity cases and revocation ordering are verified. Do not broaden founder or Board authority as a migration convenience.

## Acceptance and independent evidence

| Case | Required observation |
| --- | --- |
| Identity and role revision | A verified Agent receives a scoped role and reads its project. A caller-supplied role, wrong company or a role edit without assignment replacement cannot gain access. Compare stored assignments and actual effects. |
| Scope and separation | A reviewer can accept an eligible handoff but cannot accept one outside its audience. A proposal author cannot qualify their own change. A paper grant never sends to another account or destination. |
| Delegation and revocation | Refuse an out-of-scope assignment and self-escalation. Revoke during an active run and reject the next delayed request while independently reconciling an already admitted effect once. |
| Delivery parity | Existing company and paper flows still run within their original scope; an unmapped action and every unauthenticated request produce no effect and leave a traceable denial. |
