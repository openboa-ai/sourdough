# Take a hired Agent to the first accepted duty

Design draft · Verification **UNRUN** · Responsible component: Organization service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume a committed hire receipt. Track hired → provisioning → ready → first-duty-accepted; blocked retains a reason, owner and bounded next attempt. Correlate workspace, the hire’s exact runtime/model profile, budget and first duty with the Agent ID.

Input contracts: [hire/issue-grant](../issue-grant/spec.md) · [desk/reopen](../../../../../employee/agent/desk/reopen/spec.md) · [execute/tool-turn](../../../../../employee/agent/harness/execute/tool-turn/spec.md) · [discover-work/decide](../../../work/discover-work/decide/spec.md).

## Design decision and review

Keep hire atomic but provision resources in resumable steps. Use an idempotent operation key per hire and step, not a new identity on retry.

**Concern for the responsible component:** No worker should run with default privileges while its workspace or budget is incomplete. A role title is not proof of readiness.

## Functional and quality requirements

- **FR.** A real run reads the mandate and explicitly accepts its first assigned duty. Granting access, creating a folder or delivering a mention does not count.
- **NFR.** Zero duplicate Agents, grants, allocations or first duties on a lost acknowledgment. Onboarding cannot expand scope to make a run succeed.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Interrupt between grant and desk creation; resume automatically and observe the same hire in a live first turn accepting the original duty. |
| Failure or contention | Deny the workspace temporarily; show hired/blocked rather than working. Deliver duplicate readiness events and an out-of-scope write: no new hire and zero unauthorized effects. |

**Oracle:** Independently compare roster, budget reservation, desk inventory, runtime start and accepted work receipt. Run [H1](../../../../../acceptance/episodes.md#h1-hiring).

## Operations and change review

Operations can inspect the last completed step. Retry within grant and deadline; if exhausted keep the cost and duty attributable to the company.
