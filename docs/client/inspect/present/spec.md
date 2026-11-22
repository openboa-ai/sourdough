# Present a work projection

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume a read model and its authorization, coverage and revision metadata. Bind fields to accessible client views.

Input contracts: [Authorized work timeline](../../../company/systems/operations/work-timeline/read/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Keep local selection, sorting and intended control changes separate from observed company facts. Closing the client stops only its presentation.

**Concern for the responsible component:** A cached response is not proof of present access. Clear private state on sign-out or principal change; never show sensitive cached contents after access loss.

## Functional and quality requirements

- **FR.** Render owner, next duty and evidence gaps with the server’s as-of time. An absent business profile is normal.
- **NFR.** Navigation, filtering, reopening and reconnecting issue zero operational commands. Present status in text and support keyboard navigation.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Run a core-only company while the client is closed; reopen and compare its timeline with the authorized projection and independent evidence. |
| Failure or contention | Delay a response, invalidate an attachment grant and switch principal. Do not show an old record as current or leak content from the previous principal. |

**Oracle:** Capture the actual screen, keyboard path, query revision and service command log.

## Operations and change review

Record the client build and supported contract revisions. A failed client can be rolled back independently; see the [client build contract](../../build.md).
