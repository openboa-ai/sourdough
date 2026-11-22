# Admit and reopen a paper mandate

Design draft · Verification **UNRUN** · Responsible component: Quant profile service

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Accept the company, current charter, simulated account identity, scoped connector revision, instrument rules digest and envelope. Independently query the venue identity and starting account facts before enabling dispatch.

Input contracts: [found-company/bootstrap](../../../../../systems/organization/found-company/bootstrap/spec.md) · [connect-resource/register](../../../../../systems/connectors/connect-resource/register/spec.md) · [authorize-actor/check](../../../../../systems/authority/authorize-actor/check/spec.md).

## Design decision and review

Persist one verified binding rather than having the first Agent implicitly choose an account. Repeat setup by request identity; update the binding only as a new authorized revision.

**Concern for the responsible component:** Do not switch an account with outstanding orders. Quarantine mismatches without preventing independent company work.

## Functional and quality requirements

- **FR.** Bind only the selected synthetic environment. Include the binding revision in each order and restore it before recovery queries.
- **NFR.** Opening an existing company makes zero account creations. A changed environment, store or instrument revision cannot inherit dispatch authority.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Open the company twice and compare binding, independent venue account, initial book and first duty. At restart load the bound account, not the original starting balance. |
| Failure or contention | Substitute another account or remove the binding while an order is unresolved. Dispatch stops, the original duty is retained and no replacement order is sent. |

**Oracle:** Compare binding history and destination journal from outside the company; count account creations and submissions.

## Operations and change review

Expose binding status, reason and oldest unresolved order. Revalidate the original destination under the existing mandate, never widen it automatically.
